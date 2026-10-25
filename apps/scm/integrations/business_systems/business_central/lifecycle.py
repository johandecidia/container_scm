"""Shared run lifecycle for every Business Central resource sync.

A resource sync (purchase orders today; receipts, ledger entries, … later)
supplies only a *step*: fetch, map, validate and upsert, returning a
:class:`SyncOutcome`. Everything around it is handled here, once:

  - validates the integration is an active, sync-enabled BC business system and
    that the resource is implemented and enabled for it
  - holds a per-resource lock (cache + DB advisory lock), so the same resource
    never runs twice concurrently while different resources may run in parallel
  - enforces the source binding: every resource of one integration reads the
    same Business Central tenant, environment and company
  - records the run as an IntegrationSyncRun (source, markers, counts, errors)
  - advances sync markers (timestamp watermark, integer cursor) only when the
    run completes fully; a failed or partial run never moves them forward
  - updates the integration's shared health fields without letting one
    resource's success hide another resource's outstanding error
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.scm.integrations.models import Integration, IntegrationSyncRun
from apps.scm.integrations.services import (
    STALE_RUNNING_HOURS,
    get_last_successful_cursor,
    get_last_successful_watermark,
    mark_integration_error,
    mark_integration_sync_success,
)

from .client import BusinessCentralClient
from .exceptions import BusinessCentralConfigurationError
from .locks import DEFAULT_LOCK_TTL_SECONDS, sync_lock, sync_lock_name
from .resources import get_resource_settings, is_sync_enabled

logger = logging.getLogger(__name__)

PROVIDER_CODE = "business_central"


@dataclass(frozen=True)
class SyncContext:
    """What a resource step needs to know about the run it is part of."""

    integration: Integration
    client: BusinessCentralClient
    run: IntegrationSyncRun
    source: dict
    started: datetime
    watermark_from: datetime | None
    cursor_from: int | None


@dataclass
class SyncOutcome:
    """What a resource step returns. Markers are only stored if ``failed == 0``."""

    fetched: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    errors: list[dict] = field(default_factory=list)
    watermark_to: datetime | None = None
    cursor_to: int | None = None


SyncStep = Callable[[SyncContext], SyncOutcome]


def run_business_central_sync(
    integration: Integration,
    resource_type: str,
    step: SyncStep,
    client: BusinessCentralClient | None = None,
    *,
    trigger_type: str = IntegrationSyncRun.TriggerType.SCHEDULED,
) -> IntegrationSyncRun:
    """Run ``step`` for ``resource_type`` inside the shared lifecycle.

    Raises:
        BusinessCentralConfigurationError: integration invalid / not sync-enabled /
            resource not implemented or not enabled / missing credentials / bound
            to another source.
        BusinessCentralSyncInProgressError: this resource is already syncing.
        Exception: anything the step raises (the run is recorded as failed and no
            marker is advanced before re-raise).
    """
    validate_integration(integration, client, resource_type)
    if client is None:
        client = BusinessCentralClient(integration=integration)

    ttl = int((integration.config or {}).get("sync_lock_ttl_seconds") or DEFAULT_LOCK_TTL_SECONDS)
    # Raises BusinessCentralSyncInProgressError if this resource is already running;
    # holds a DB advisory lock for the whole run (never proceeds unprotected).
    with sync_lock(sync_lock_name(integration, resource_type), ttl=ttl):
        source = client.source_identity()
        started = timezone.now()
        run = _start_run(integration, resource_type, trigger_type, source, started)
        context = SyncContext(
            integration=integration,
            client=client,
            run=run,
            source=source,
            started=started,
            watermark_from=run.watermark_from,
            cursor_from=(run.metadata or {}).get("cursor_from"),
        )
        try:
            outcome = step(context)
        except Exception as exc:
            _finish_failed(run, integration, exc)
            raise
        _finish(run, integration, outcome)
        return run


# ---------------------------------------------------------------------------
# Validation and source binding
# ---------------------------------------------------------------------------


def validate_integration(
    integration: Integration,
    client: BusinessCentralClient | None,
    resource_type: str = IntegrationSyncRun.ResourceType.PURCHASE_ORDERS,
) -> None:
    if integration is None:
        raise BusinessCentralConfigurationError("integration is required")
    if integration.provider_family != Integration.ProviderFamily.BUSINESS_SYSTEM:
        raise BusinessCentralConfigurationError("integration is not a business system")
    if integration.provider_code != PROVIDER_CODE:
        raise BusinessCentralConfigurationError(f"integration provider is not {PROVIDER_CODE}")
    if not integration.is_active:
        raise BusinessCentralConfigurationError("integration is not active")
    if not is_sync_enabled(integration):
        raise BusinessCentralConfigurationError("Business Central sync is disabled for this integration")
    try:
        settings = get_resource_settings(integration, resource_type)
    except ValueError as exc:
        raise BusinessCentralConfigurationError(str(exc)) from None
    if not settings.implemented:
        raise BusinessCentralConfigurationError(f"{resource_type} has no Business Central sync implementation")
    if not settings.enabled:
        raise BusinessCentralConfigurationError(f"{resource_type} sync is not enabled for this integration")

    # A live client is built from stored credentials; a dummy/injected client
    # provides its own data, so credential presence is only required for live.
    if client is None:
        from apps.scm.integrations.credentials import get_integration_credentials

        creds = get_integration_credentials(integration)
        if not creds.get("client_id") or not creds.get("client_secret"):
            raise BusinessCentralConfigurationError("integration is missing client_id/client_secret credentials")


def check_source_binding(integration: Integration, source: dict) -> None:
    """Refuse to sync an integration from a different Business Central source.

    A Business Central sandbox is often a copy of production with the *same*
    company id and record GUIDs, so records from two environments would collide
    on ``external_id`` and overwrite each other. Once an integration has
    imported data (a completed or partially completed run of *any* resource) it
    is bound to that run's source — tenant, environment and company. A run that
    is still in progress (and not stale) binds too, so two resources starting at
    the same time cannot read two sources. Runs recorded before sources were
    tracked count as a different, unknown source. To read another environment,
    use a separate Team with its own integration.
    """
    stale_before = timezone.now() - timedelta(hours=STALE_RUNNING_HOURS)
    binding = IntegrationSyncRun.objects.filter(integration=integration).filter(
        Q(status__in=[IntegrationSyncRun.Status.COMPLETED, IntegrationSyncRun.Status.PARTIALLY_COMPLETED])
        | Q(status=IntegrationSyncRun.Status.RUNNING, started_at__gt=stale_before)
    )
    # ``~Q(metadata__source=...)`` alone is NULL (not true) for runs without a
    # "source" key, so unrecorded sources are matched explicitly.
    if binding.filter(~Q(metadata__has_key="source") | ~Q(metadata__source=source)).exists():
        raise BusinessCentralConfigurationError(
            f"integration {integration.pk} has already synced data from a different (or unrecorded) "
            f"Business Central source; refusing to sync from {_describe_source(source)}. "
            "Use a separate Team for this environment."
        )


def _describe_source(source: dict) -> str:
    if source.get("mode") != "live":
        return "the dummy fixture client"
    return f"environment '{source.get('environment')}' / company {source.get('company_id')}"


# ---------------------------------------------------------------------------
# Run start / finish
# ---------------------------------------------------------------------------


def _start_run(
    integration: Integration, resource_type: str, trigger_type: str, source: dict, started: datetime
) -> IntegrationSyncRun:
    """Check the source binding and claim the run in one transaction.

    The integration row lock serialises check-and-claim across resources (each
    resource has its own sync lock), so parallel runs cannot both pass the
    binding check from different sources.
    """
    with transaction.atomic():
        list(Integration.objects.select_for_update().filter(pk=integration.pk).values_list("pk", flat=True))
        check_source_binding(integration, source)
        cursor_from = get_last_successful_cursor(integration, resource_type, source=source)
        metadata: dict = {"source": source}
        if cursor_from is not None:
            metadata["cursor_from"] = cursor_from
        return IntegrationSyncRun.objects.create(
            team=integration.team,
            integration=integration,
            resource_type=resource_type,
            status=IntegrationSyncRun.Status.RUNNING,
            trigger_type=trigger_type,
            started_at=started,
            correlation_id=uuid.uuid4().hex,
            watermark_from=get_last_successful_watermark(integration, resource_type, source=source),
            metadata=metadata,
        )


def _finish(run: IntegrationSyncRun, integration: Integration, outcome: SyncOutcome) -> None:
    completed = outcome.failed == 0
    run.status = IntegrationSyncRun.Status.COMPLETED if completed else IntegrationSyncRun.Status.PARTIALLY_COMPLETED
    run.finished_at = timezone.now()
    run.records_fetched = outcome.fetched
    run.records_created = outcome.created
    run.records_updated = outcome.updated
    run.records_unchanged = outcome.unchanged
    run.records_failed = outcome.failed
    metadata = dict(run.metadata or {})
    if completed:
        # Markers advance only on full success.
        run.watermark_to = outcome.watermark_to
        if outcome.cursor_to is not None:
            metadata["cursor_to"] = outcome.cursor_to
    if outcome.errors:
        run.error_summary = _summarize_errors(outcome.errors)
        metadata["errors"] = outcome.errors[:50]
    run.metadata = metadata
    run.save()

    if completed:
        mark_integration_sync_success(integration, run.resource_type)
    else:
        mark_integration_error(integration, run.error_summary)

    logger.info(
        "BC sync %s %s: team=%s fetched=%d created=%d updated=%d unchanged=%d failed=%d",
        run.resource_type,
        run.status,
        integration.team.slug,
        outcome.fetched,
        outcome.created,
        outcome.updated,
        outcome.unchanged,
        outcome.failed,
    )


def _finish_failed(run: IntegrationSyncRun, integration: Integration, exc: Exception) -> None:
    run.status = IntegrationSyncRun.Status.FAILED
    run.finished_at = timezone.now()
    run.error_summary = f"{type(exc).__name__}: {exc}"[:1000]
    # Markers deliberately left unset — a failed run must not advance them.
    run.save()
    mark_integration_error(integration, run.error_summary)
    logger.warning(
        "BC sync failed: integration=%s resource=%s error=%s", integration.pk, run.resource_type, type(exc).__name__
    )


def _summarize_errors(errors: list[dict]) -> str:
    ids = ", ".join(str(e.get("external_id")) for e in errors[:10])
    return f"{len(errors)} record(s) failed to import: {ids}"[:1000]
