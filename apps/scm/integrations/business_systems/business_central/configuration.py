"""Create, update and connection-test a team's Business Central integration.

Used by the Settings → Integrations UI and the connection-test Celery task. The
Entra client id and secret only ever land in the encrypted credential row (via the
credential service) — never in Integration.config, logs or return values.

New integrations are created with purchase-order sync and scheduled sync disabled,
the same safe default as ``setup_business_central_integration``. Saving or testing
an integration never imports or modifies Business Central data.
"""

from __future__ import annotations

import logging

from django.utils import timezone

from apps.scm.integrations.credentials import (
    CredentialDecryptionError,
    get_integration_credentials,
    set_integration_credentials,
)
from apps.scm.integrations.models import Integration, IntegrationCredential
from apps.scm.integrations.services import mark_integration_error, mark_integration_success
from apps.teams.models import Team

from .diagnostics import failure_diagnostic
from .exceptions import BusinessCentralError

logger = logging.getLogger(__name__)

PROVIDER_CODE = "business_central"
DEFAULT_API_VERSION = "v2.0"


def save_business_central_integration(
    team: Team,
    *,
    name: str,
    config: dict,
    is_active: bool,
    credentials: dict,
    integration: Integration | None = None,
) -> Integration:
    """Create (``integration=None``) or update the team's Business Central integration.

    ``config`` holds the non-secret connection settings (tenant_id, environment,
    company_id, api_version) and is merged over the stored config so sync settings
    are kept. ``credentials`` holds only the values being set or replaced; they are
    merged over what is stored, so a blank secret keeps the existing one.
    """
    if integration is None:
        integration = Integration.objects.create(
            team=team,
            name=name,
            provider_code=PROVIDER_CODE,
            provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
            api_style=Integration.ApiStyle.PROPRIETARY,
            status=Integration.Status.PENDING,
            is_active=is_active,
            config={**config, "sync_enabled": False, "scheduled_sync_enabled": False},
        )
        action = "created"
    else:
        if integration.team_id != team.pk or integration.provider_code != PROVIDER_CODE:
            raise ValueError("Integration does not belong to this team's Business Central configuration")
        integration.name = name
        integration.is_active = is_active
        integration.config = {**(integration.config or {}), **config}
        integration.save(update_fields=["name", "is_active", "config", "updated_at"])
        action = "updated"

    if credentials:
        has_stored = IntegrationCredential.objects.filter(integration=integration).exists()
        stored = get_integration_credentials(integration) if has_stored else {}
        set_integration_credentials(integration, IntegrationCredential.AuthType.OAUTH2, {**stored, **credentials})

    logger.info("Business Central integration %s %s for team %s.", integration.pk, action, team.pk)
    return integration


def get_stored_client_id(integration: Integration) -> str:
    """The stored Entra client id (an identifier, not a secret) for prefilling the edit form."""
    try:
        return get_integration_credentials(integration).get("client_id", "")
    except CredentialDecryptionError:
        return ""


def has_stored_client_secret(integration: Integration) -> bool:
    try:
        return bool(get_integration_credentials(integration).get("client_secret"))
    except CredentialDecryptionError:
        return False


def test_business_central_connection(integration: Integration) -> dict:
    """Run the client's read-only connection test and record the outcome on the integration.

    Checks authentication (OAuth2 client credentials), the environment, the company
    and a purchase-order read. Returns a sanitised ``{"success", "message", ...}``:
    messages come from the typed client exceptions, which never carry tokens,
    secrets or response bodies.

    On failure ``diagnostic`` describes the failed request and, for a 403, the
    denied Business Central object (see ``diagnostics.failure_diagnostic``). It is
    for Team Administrators only and is not stored on the integration.
    """
    from .client import BusinessCentralClient

    integration.last_tested_at = timezone.now()
    integration.save(update_fields=["last_tested_at", "updated_at"])
    diagnostic: dict = {}
    try:
        result = BusinessCentralClient(integration=integration).test_connection()
    except BusinessCentralError as exc:
        message = f"{exc.category}: {exc}"
        diagnostic = failure_diagnostic(exc)
    except CredentialDecryptionError as exc:
        message = f"Configuration error: {exc}"
    else:
        mark_integration_success(integration)
        return result

    mark_integration_error(integration, message)
    logger.warning("Business Central connection test failed for integration %s: %s", integration.pk, message)
    return {"success": False, "message": message, "diagnostic": diagnostic}
