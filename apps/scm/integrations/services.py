# Integration services — all business logic, write operations, and external API calls.
import logging
from datetime import datetime, timedelta

from django.utils import timezone

from apps.teams.models import Team

from .models import Integration, IntegrationRequestLog, IntegrationSyncRun

logger = logging.getLogger(__name__)

_BC_PROVIDER_CODE = "business_central"
_DEFAULT_SYNC_INTERVAL_MINUTES = 30
_DEFAULT_FAILURE_BACKOFF_MINUTES = 15
# A run stuck in "running" longer than this is treated as crashed (the advisory
# lock, released on crash, is the real concurrency guard).
_STALE_RUNNING_HOURS = 2


def _is_business_central_sync_due(integration: Integration, now: datetime) -> bool:
    """True when this BC integration's purchase-order sync is due to run again."""
    config = integration.config or {}
    interval = timedelta(
        minutes=int(config.get("purchase_order_sync_interval_minutes") or _DEFAULT_SYNC_INTERVAL_MINUTES)
    )
    backoff = timedelta(
        minutes=int(config.get("purchase_order_sync_failure_backoff_minutes") or _DEFAULT_FAILURE_BACKOFF_MINUTES)
    )

    latest = (
        IntegrationSyncRun.objects.filter(
            integration=integration,
            resource_type=IntegrationSyncRun.ResourceType.PURCHASE_ORDERS,
        )
        .order_by("-started_at")
        .first()
    )
    if latest is None:
        return True  # never synced

    # A genuinely-running sync is skipped; a stale one is allowed (lock still guards).
    if (
        latest.status == IntegrationSyncRun.Status.RUNNING
        and latest.started_at
        and latest.started_at > now - timedelta(hours=_STALE_RUNNING_HOURS)
    ):
        return False

    reference = latest.finished_at or latest.started_at
    if reference is None:
        return True
    gap = interval if latest.status == IntegrationSyncRun.Status.COMPLETED else backoff
    return reference + gap <= now


def get_due_business_central_integrations(now: datetime | None = None) -> list[Integration]:
    """Return active, sync-enabled BC integrations whose PO sync is due.

    Skips integrations that are inactive, have sync disabled, are not yet due per
    their configured interval, are backing off after a failure, or have a sync in
    progress.
    """
    now = now or timezone.now()
    integrations = Integration.objects.filter(
        provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
        provider_code=_BC_PROVIDER_CODE,
        is_active=True,
    )
    due = []
    for integration in integrations:
        if not (integration.config or {}).get("sync_enabled", True):
            continue
        if _is_business_central_sync_due(integration, now):
            due.append(integration)
    return due


# ── Sync run watermark ──────────────────────────────────────────────────────


def get_last_successful_watermark(integration: Integration, resource_type: str):
    """Return the watermark_to of the most recent fully completed sync run.

    Only ``COMPLETED`` runs advance the watermark — a failed or partially
    completed run must not move it forward, so those are excluded here. Returns
    None when there is no completed run yet (first sync).
    """
    run = (
        IntegrationSyncRun.objects.filter(
            integration=integration,
            resource_type=resource_type,
            status=IntegrationSyncRun.Status.COMPLETED,
            watermark_to__isnull=False,
        )
        .order_by("-watermark_to")
        .first()
    )
    return run.watermark_to if run else None


# ── Integration lifecycle ─────────────────────────────────────────────────────


def create_integration(
    team: Team,
    name: str,
    provider_code: str,
    provider_family: str = Integration.ProviderFamily.CARRIER,
    api_style: str = Integration.ApiStyle.UNKNOWN,
    config: dict | None = None,
) -> Integration:
    return Integration.objects.create(
        team=team,
        name=name,
        provider_code=provider_code,
        provider_family=provider_family,
        api_style=api_style,
        config=config or {},
    )


def connect_carrier_integration(
    team: Team,
    provider_code: str,
    credentials: dict,
    *,
    test_connection_reference: str = "",
) -> Integration:
    """Create or update a team's direct carrier integration and store its credentials.

    The write behind Settings → Tracking. Four things, in one place because doing three
    of them leaves a carrier that looks connected and cannot be tracked through:

      1. the ``Integration`` row, seeded from the carrier's shipped ``default_config``
         when it is new — an existing config is left alone, because a team may have
         pointed it at a contracted product;
      2. the CARRIER family and the active flags, which is what
         ``carriers.factory.get_carrier_integration`` looks for;
      3. the credentials, through the credential service, which encrypts them. The raw
         values are never logged and never written to ``config``;
      4. the ``TrackingProvider`` row a subscription has to point at.

    ``credentials`` may only contain the keys the carrier's auth style reads; anything
    else would be stored and never used. It is *merged* over what is already stored
    rather than replacing it, so one half of a client id/secret pair can be rotated
    without re-entering the other — which matters because the stored value is never
    rendered back to the person doing the rotating.

    ``test_connection_reference`` is a container number the account can see, written
    to ``config`` — not a secret, and the one thing a connection test cannot be run
    without. Two of the three carriers ship without one because a reference known to
    an account belongs to that account, not to this repository; supplying it here is
    what makes "Test connection" answer something other than "not configured".

    Raises :class:`UnknownCarrierError` for an unregistered code and ValueError for a
    carrier with no live configuration or for credential keys it does not read.
    """
    from .carriers.auto_link import get_or_create_tracking_provider
    from .carriers.dcsa.client import credential_fields_for_auth_style
    from .carriers.registry import get_carrier_definition
    from .credentials import get_integration_credentials, set_integration_credentials
    from .models import IntegrationCredential

    definition = get_carrier_definition(provider_code)
    if not definition.is_connectable:
        raise ValueError(f"'{provider_code}' has no live configuration and cannot be connected.")

    integration, created = Integration.objects.get_or_create(
        team=team,
        provider_code=provider_code,
        defaults={
            "name": definition.name,
            "provider_family": Integration.ProviderFamily.CARRIER,
            "api_style": Integration.ApiStyle.DCSA
            if definition.capabilities.supports_dcsa
            else Integration.ApiStyle.PROPRIETARY,
            "status": Integration.Status.PENDING,
            "config": dict(definition.default_config),
            "is_active": True,
        },
    )
    if created and test_connection_reference:
        integration.config["test_connection_reference"] = test_connection_reference
        integration.save(update_fields=["config", "updated_at"])
    elif not created:
        integration.provider_family = Integration.ProviderFamily.CARRIER
        integration.is_active = True
        if not integration.config:
            integration.config = dict(definition.default_config)
        if test_connection_reference:
            integration.config["test_connection_reference"] = test_connection_reference
        integration.save(update_fields=["provider_family", "is_active", "config", "updated_at"])

    auth_style = str((integration.config or {}).get("auth_style") or "")
    expected = credential_fields_for_auth_style(auth_style)
    unexpected = set(credentials) - set(expected)
    if unexpected:
        raise ValueError(f"Credential fields {sorted(unexpected)} are not read by auth_style '{auth_style}'.")

    auth_type = (
        IntegrationCredential.AuthType.API_KEY
        if expected == ("api_key",)
        else IntegrationCredential.AuthType.OAUTH2
        if auth_style == "oauth2_client_credentials"
        else IntegrationCredential.AuthType.CUSTOM
    )
    stored = get_integration_credentials(integration) if not created else {}
    set_integration_credentials(integration, auth_type, {**stored, **credentials})

    # Tracking subscriptions point at a provider row, so a carrier that can be routed
    # to must have one before the first activation rather than at it.
    get_or_create_tracking_provider(carrier_code=provider_code, carrier_name=definition.name)

    logger.info(
        "Carrier integration %s (%s) %s for team %s.",
        integration.pk,
        provider_code,
        "created" if created else "updated",
        team.pk,
    )
    return integration


def activate_integration(integration: Integration) -> Integration:
    integration.status = Integration.Status.ACTIVE
    integration.is_active = True
    integration.save(update_fields=["status", "is_active", "updated_at"])
    return integration


def deactivate_integration(integration: Integration) -> Integration:
    integration.status = Integration.Status.INACTIVE
    integration.is_active = False
    integration.save(update_fields=["status", "is_active", "updated_at"])
    return integration


# ── Request logging ───────────────────────────────────────────────────────────


def log_integration_request(
    team: Team,
    provider_code: str,
    method: str,
    endpoint: str,
    *,
    integration: Integration | None = None,
    status_code: int | None = None,
    duration_ms: int | None = None,
    request_id: str = "",
    success: bool = False,
    error_message: str = "",
) -> IntegrationRequestLog:
    """Create an IntegrationRequestLog entry.

    Never include tokens, secrets, or auth headers in error_message or endpoint.
    """
    return IntegrationRequestLog.objects.create(
        team=team,
        integration=integration,
        provider_code=provider_code,
        method=method,
        endpoint=endpoint,
        status_code=status_code,
        duration_ms=duration_ms,
        request_id=request_id,
        success=success,
        error_message=error_message,
    )


def mark_integration_success(integration: Integration) -> None:
    """Record a successful API interaction on the Integration model."""
    integration.status = Integration.Status.ACTIVE
    integration.last_success_at = timezone.now()
    integration.last_error_message = ""
    integration.save(update_fields=["status", "last_success_at", "last_error_message", "updated_at"])


def mark_integration_error(integration: Integration, error_message: str) -> None:
    """Record a failed API interaction on the Integration model."""
    integration.status = Integration.Status.ERROR
    integration.last_error_at = timezone.now()
    integration.last_error_message = error_message
    integration.save(update_fields=["status", "last_error_at", "last_error_message", "updated_at"])


# ── Connection test ───────────────────────────────────────────────────────────


def test_integration_connection(integration: Integration) -> dict:
    """Test connectivity and credentials for a carrier integration.

    1. Looks up the carrier in the registry.
    2. Builds the client with this integration (and its credentials) injected.
    3. Calls test_connection().
    4. Updates integration status and logs the result.
    Returns {"success": bool, "message": str}.
    """
    from .carriers.factory import build_carrier_client
    from .carriers.registry import UnknownCarrierError

    integration.last_tested_at = timezone.now()
    integration.save(update_fields=["last_tested_at", "updated_at"])

    started_at: datetime = timezone.now()
    try:
        client = build_carrier_client(integration.provider_code, integration=integration)
        result = client.test_connection()
        duration_ms = int((timezone.now() - started_at).total_seconds() * 1000)

        mark_integration_success(integration)
        log_integration_request(
            team=integration.team,
            provider_code=integration.provider_code,
            method="GET",
            endpoint="test_connection",
            integration=integration,
            duration_ms=duration_ms,
            success=True,
        )
        logger.info("Integration %s connection test succeeded.", integration.pk)
        return result if isinstance(result, dict) else {"success": True, "message": "OK"}

    except UnknownCarrierError as exc:
        error_message = str(exc)
        mark_integration_error(integration, error_message)
        logger.warning("Integration %s unknown carrier: %s", integration.pk, error_message)
        return {"success": False, "message": error_message}

    except Exception as exc:  # noqa: BLE001
        error_message = str(exc)
        duration_ms = int((timezone.now() - started_at).total_seconds() * 1000)
        mark_integration_error(integration, error_message)
        log_integration_request(
            team=integration.team,
            provider_code=integration.provider_code,
            method="GET",
            endpoint="test_connection",
            integration=integration,
            duration_ms=duration_ms,
            success=False,
            error_message=error_message,
        )
        logger.warning("Integration %s connection test failed: %s", integration.pk, error_message)
        return {"success": False, "message": error_message}
