# Integration credential service — single point of access for integration secrets.
#
# All reads and writes of sensitive credentials must go through this module so
# that encryption can be swapped in one place without touching callers.
#
# Credentials are encrypted at rest with Fernet (AES-128-CBC + HMAC) using the
# key resolved from settings.SCM_INTEGRATION_ENCRYPTION_KEY, falling back to a
# key derived from SECRET_KEY for local development. Legacy base64-encoded rows
# (from the previous placeholder implementation) are still readable so existing
# data keeps working; they are re-encrypted on the next write.
import base64
import hashlib
import json
import logging
import re

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

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


def _get_fernet() -> Fernet:
    """Build the Fernet cipher from configuration.

    Uses SCM_INTEGRATION_ENCRYPTION_KEY when set (must be a url-safe base64
    32-byte Fernet key). Otherwise derives a stable key from SECRET_KEY so
    development works without extra configuration — but in production
    (SCM_INTEGRATION_REQUIRE_ENCRYPTION_KEY) the dedicated key is mandatory and
    the fallback is refused. The key value is never logged.
    """
    configured = getattr(settings, "SCM_INTEGRATION_ENCRYPTION_KEY", "") or ""
    if configured:
        return Fernet(configured.encode())

    if getattr(settings, "SCM_INTEGRATION_REQUIRE_ENCRYPTION_KEY", False):
        raise ImproperlyConfigured(
            "SCM_INTEGRATION_ENCRYPTION_KEY must be set in production; the SECRET_KEY-derived "
            "fallback is only permitted in development and tests."
        )

    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encode(data: dict) -> str:
    """Encrypt a credential dict to a storable string."""
    token = _get_fernet().encrypt(json.dumps(data).encode())
    return token.decode()


def _decode(encoded: str) -> dict:
    """Decrypt a stored credential string back to a dict.

    Falls back to the legacy base64-JSON format for rows written before
    encryption was introduced. Returns an empty dict if the value cannot be
    read at all. Never logs the secret payload itself.
    """
    if not encoded:
        return {}
    try:
        decrypted = _get_fernet().decrypt(encoded.encode())
        return json.loads(decrypted.decode())
    except InvalidToken:
        # Legacy (unencrypted) base64-JSON payload — read it so old data works.
        try:
            return json.loads(base64.b64decode(encoded.encode()).decode())
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to decode legacy credential data: %s", type(exc).__name__)
            return {}
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to decrypt credential data: %s", type(exc).__name__)
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
