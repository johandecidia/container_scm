# Integration credential service — single point of access for integration secrets.
#
# All reads and writes of sensitive credentials must go through this module so
# that encryption can be added (or swapped) in one place without touching callers.
#
# Current implementation: base64-encoded JSON (NOT encrypted — placeholder only).
# TODO: replace with Django encrypted fields or a KMS-backed solution before
#       storing real API keys or OAuth tokens in production.
import base64
import json
import logging
import re

from .models import Integration, IntegrationCredential

logger = logging.getLogger(__name__)

_SECRET_PATTERN = re.compile(r"(key|secret|token|password|credential)", re.IGNORECASE)


def mask_secret(value: str) -> str:
    """Return a masked version of a secret string for logging/display.

    The first 4 chars and last 4 chars are shown; the rest is replaced by ***.
    Strings shorter than 10 chars are fully masked.
    """
    if not value or len(value) < 10:
        return "***"
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


def _encode(data: dict) -> str:
    """Encode a credential dict to a storable string.

    TODO: replace with encrypted storage before production use.
    """
    return base64.b64encode(json.dumps(data).encode()).decode()


def _decode(encoded: str) -> dict:
    """Decode a stored credential string back to a dict."""
    try:
        return json.loads(base64.b64decode(encoded.encode()).decode())
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to decode credential data: %s", exc)
        return {}


def set_integration_credentials(
    integration: Integration,
    auth_type: str,
    data: dict,
) -> IntegrationCredential:
    """Create or update the credential record for an integration.

    ``data`` should contain the raw credential fields, e.g.
    {"api_key": "...", "client_id": "...", "client_secret": "..."}.
    Never log or expose the raw ``data`` dict.
    """
    credential, _ = IntegrationCredential.objects.get_or_create(
        integration=integration,
        defaults={
            "team": integration.team,
            "auth_type": auth_type,
            "encrypted_data": _encode(data),
        },
    )
    if credential.auth_type != auth_type or credential.encrypted_data != _encode(data):
        credential.auth_type = auth_type
        credential.encrypted_data = _encode(data)
        credential.save(update_fields=["auth_type", "encrypted_data", "updated_at"])

    logger.info("Credentials stored for integration %s (auth_type=%s)", integration.pk, auth_type)
    return credential


def get_integration_credentials(integration: Integration) -> dict:
    """Return the decoded credential data dict for the integration.

    Returns an empty dict if no credentials are stored.
    Raises IntegrationCredential.DoesNotExist only if the record is corrupted.
    """
    try:
        credential = IntegrationCredential.objects.get(integration=integration)
    except IntegrationCredential.DoesNotExist:
        return {}
    return _decode(credential.encrypted_data)
