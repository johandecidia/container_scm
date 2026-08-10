"""Sync orchestration for Microsoft Business Central purchase orders.

Business Central is the system of record. This module performs an incremental,
read-only pull of purchase orders into SCM:

  - validates the integration is an active, sync-enabled BC business system
  - guards against concurrent runs for the same integration with a cache lock
  - records the run as an IntegrationSyncRun
  - fetches only records modified since the last successful watermark (with a
    small overlap), maps them, and upserts them idempotently
  - advances the watermark only when the run completes fully
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, timedelta

from django.utils import timezone

from apps.scm.integrations.models import Integration, IntegrationSyncRun
from apps.scm.integrations.services import (
    get_last_successful_watermark,
    mark_integration_error,
    mark_integration_success,
)

from .client import BusinessCentralClient
from .exceptions import BusinessCentralConfigurationError
from .locks import DEFAULT_LOCK_TTL_SECONDS, purchase_order_lock_name, sync_lock
from .mapper import BusinessCentralMapper

logger = logging.getLogger(__name__)

_mapper = BusinessCentralMapper()

_RESOURCE = IntegrationSyncRun.ResourceType.PURCHASE_ORDERS
_PROVIDER_CODE = "business_central"
# Re-fetch a little before the last watermark so records modified right on the
# boundary are not missed; the idempotent upsert absorbs the overlap.
_WATERMARK_OVERLAP = timedelta(minutes=5)


def sync_purchase_orders_from_business_central(
    integration: Integration,
    client: BusinessCentralClient | None = None,
    *,
    trigger_type: str = IntegrationSyncRun.TriggerType.SCHEDULED,
) -> IntegrationSyncRun:
    """Fetch, map, and upsert Business Central purchase orders for an integration.

    Args:
        integration: The BC business-system integration to sync. The team is
            taken from ``integration.team``.
        client: Optional client. Defaults to a live BusinessCentralClient built
            from the integration. Pass a dummy client (``use_dummy=True``) in
            tests; credential validation is skipped when a client is injected.
        trigger_type: How the sync was triggered (manual/scheduled/retry).

    Returns:
        The IntegrationSyncRun recording this run.

    Raises:
        BusinessCentralConfigurationError: integration invalid / not sync-enabled
            / missing credentials.
        BusinessCentralSyncInProgressError: another run holds the lock.
        BusinessCentralError: on a hard fetch/auth/response failure (the run is
            recorded as failed and the watermark is not advanced before re-raise).
    """
    _validate_integration(integration, client)

    ttl = int((integration.config or {}).get("sync_lock_ttl_seconds") or DEFAULT_LOCK_TTL_SECONDS)
    # Raises BusinessCentralSyncInProgressError if a run is already in progress;
    # holds a DB advisory lock for the whole run (never proceeds unprotected).
    with sync_lock(purchase_order_lock_name(integration), ttl=ttl):
        return _run_sync(integration, client, trigger_type)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_integration(integration: Integration, client: BusinessCentralClient | None) -> None:
    if integration is None:
        raise BusinessCentralConfigurationError("integration is required")
    if integration.provider_family != Integration.ProviderFamily.BUSINESS_SYSTEM:
        raise BusinessCentralConfigurationError("integration is not a business system")
    if integration.provider_code != _PROVIDER_CODE:
        raise BusinessCentralConfigurationError(f"integration provider is not {_PROVIDER_CODE}")
    if not integration.is_active:
        raise BusinessCentralConfigurationError("integration is not active")
    if not (integration.config or {}).get("sync_enabled", True):
        raise BusinessCentralConfigurationError("purchase order sync is disabled for this integration")

    # A live client is built from stored credentials; a dummy/injected client
    # provides its own data, so credential presence is only required for live.
    if client is None:
        from apps.scm.integrations.credentials import get_integration_credentials

        creds = get_integration_credentials(integration)
        if not creds.get("client_id") or not creds.get("client_secret"):
            raise BusinessCentralConfigurationError("integration is missing client_id/client_secret credentials")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def _run_sync(
    integration: Integration,
    client: BusinessCentralClient | None,
    trigger_type: str,
) -> IntegrationSyncRun:
    from apps.scm.procurement.models import PurchaseOrderSource
    from apps.scm.procurement.services import upsert_purchase_orders

    team = integration.team
    started = timezone.now()
    watermark_from = get_last_successful_watermark(integration, _RESOURCE)
    modified_since = _modified_since(integration, watermark_from, started)

    run = IntegrationSyncRun.objects.create(
        team=team,
        integration=integration,
        resource_type=_RESOURCE,
        status=IntegrationSyncRun.Status.RUNNING,
        trigger_type=trigger_type,
        started_at=started,
        correlation_id=uuid.uuid4().hex,
        watermark_from=watermark_from,
    )

    if client is None:
        client = BusinessCentralClient(integration=integration)

    try:
        raw_pos = client.fetch_purchase_orders(modified_since=modified_since)
        normalized_data = []
        max_modified = None
        for raw_po in raw_pos:
            identifier = _line_identifier(raw_po, client)
            raw_lines = client.fetch_purchase_order_lines(identifier)
            normalized = _mapper.map_purchase_order(raw_po, raw_lines)
            normalized_data.append(normalized.model_dump())
            last_modified = normalized.source_last_modified
            if last_modified and (max_modified is None or last_modified > max_modified):
                max_modified = last_modified
    except Exception as exc:
        _finish_failed(run, integration, exc)
        raise

    upsert_result = upsert_purchase_orders(
        team=team,
        purchase_orders_data=normalized_data,
        source_system=PurchaseOrderSource.BUSINESS_CENTRAL,
        source_company_id=(integration.config or {}).get("company_id", ""),
    )
    watermark_to = max_modified or watermark_from or started
    _finish(run, integration, upsert_result, fetched=len(raw_pos), watermark_to=watermark_to)
    return run


def _modified_since(integration: Integration, watermark_from, started) -> str | None:
    """Build the OData modified-since filter value (ISO-8601 UTC) or None for full sync."""
    if watermark_from:
        return _isoformat_z(watermark_from - _WATERMARK_OVERLAP)
    initial_days = int((integration.config or {}).get("initial_sync_days") or 0)
    if initial_days > 0:
        return _isoformat_z(started - timedelta(days=initial_days))
    return None


def _isoformat_z(value) -> str:
    # Business Central expects UTC with a trailing Z.
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _line_identifier(raw_po: dict, client: BusinessCentralClient) -> str:
    """PO identifier for the line fetch.

    Live BC requires the GUID (``id``); dummy fixtures are keyed by PO number.
    """
    if getattr(client, "use_dummy", False):
        return raw_po.get("number") or raw_po.get("id") or ""
    return raw_po.get("id") or raw_po.get("number") or ""


def _finish(run, integration, result, *, fetched: int, watermark_to) -> None:
    completed = result.failed == 0
    run.status = IntegrationSyncRun.Status.COMPLETED if completed else IntegrationSyncRun.Status.PARTIALLY_COMPLETED
    run.finished_at = timezone.now()
    run.records_fetched = fetched
    run.records_created = result.created
    run.records_updated = result.updated
    run.records_unchanged = result.unchanged
    run.records_failed = result.failed
    if completed:
        run.watermark_to = watermark_to  # advance only on full success
    if result.errors:
        run.error_summary = _summarize_errors(result.errors)
        run.metadata = {"errors": result.errors[:50]}
    run.save()

    if completed:
        mark_integration_success(integration)
    else:
        mark_integration_error(integration, run.error_summary)

    logger.info(
        "BC sync %s: team=%s fetched=%d created=%d updated=%d unchanged=%d failed=%d",
        run.status,
        integration.team.slug,
        fetched,
        result.created,
        result.updated,
        result.unchanged,
        result.failed,
    )


def _finish_failed(run, integration, exc: Exception) -> None:
    run.status = IntegrationSyncRun.Status.FAILED
    run.finished_at = timezone.now()
    run.error_summary = f"{type(exc).__name__}: {exc}"[:1000]
    # Watermark deliberately left unset — a failed run must not advance it.
    run.save()
    mark_integration_error(integration, run.error_summary)
    logger.warning("BC sync failed: integration=%s error=%s", integration.pk, type(exc).__name__)


def _summarize_errors(errors: list[dict]) -> str:
    ids = ", ".join(str(e.get("external_id")) for e in errors[:10])
    return f"{len(errors)} record(s) failed to upsert: {ids}"[:1000]
