# Integration credential service — single point of access for integration secrets.
#
# All reads and writes of sensitive credentials must go through this module so
# that encryption can be swapped in one place without touching callers.
#
# Credentials are encrypted at rest with Fernet (AES-128-CBC + HMAC) using the
# key resolved from settings.SCM_INTEGRATION_ENCRYPTION_KEY, falling back to a
# key derived from SECRET_KEY for local development only. Stored values carry an
# explicit format marker ("fernet:v1:" / "legacy:base64:") so a decryption
# failure is never silently reinterpreted as a legacy value. Legacy rows are
# still readable and are re-encrypted on the next write (or via the
# migrate_integration_credentials management command).
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


# Explicit storage format markers. New writes always use FERNET_V1; LEGACY_B64 is
# only ever read (from the old placeholder implementation).
_FERNET_V1_PREFIX = "fernet:v1:"
_LEGACY_B64_PREFIX = "legacy:base64:"


class CredentialDecryptionError(Exception):
    """A stored credential value could not be decrypted/decoded.

    The message is sanitised — it never contains the ciphertext or the secret.
    """


def is_legacy_format(stored: str) -> bool:
    """True when the stored value is not the current fernet:v1 format."""
    return bool(stored) and not stored.startswith(_FERNET_V1_PREFIX)


def _encode(data: dict) -> str:
    """Encrypt a credential dict to a versioned, storable string."""
    token = _get_fernet().encrypt(json.dumps(data).encode()).decode()
    return f"{_FERNET_V1_PREFIX}{token}"


def _decrypt_fernet(ciphertext: str) -> dict:
    try:
        return json.loads(_get_fernet().decrypt(ciphertext.encode()).decode())
    except (InvalidToken, ValueError) as exc:
        raise CredentialDecryptionError(
            "Could not decrypt credential (corrupt ciphertext or wrong encryption key)"
        ) from exc


def _decode_legacy_b64(value: str) -> dict:
    try:
        return json.loads(base64.b64decode(value.encode()).decode())
    except Exception as exc:  # noqa: BLE001
        raise CredentialDecryptionError("Could not decode legacy credential value") from exc


def _decode(encoded: str) -> dict:
    """Decrypt a stored credential string back to a dict.

    Dispatches on the explicit format prefix:
      - ``fernet:v1:`` → decrypt; a failure raises CredentialDecryptionError and
        is NEVER silently reinterpreted as legacy base64.
      - ``legacy:base64:`` → decode the old placeholder format.
      - no prefix (transitional) → a raw Fernet token from the first release, else
        a pre-encryption base64-JSON value.

    Never logs the secret payload itself.
    """
    if not encoded:
        return {}
    if encoded.startswith(_FERNET_V1_PREFIX):
        return _decrypt_fernet(encoded[len(_FERNET_V1_PREFIX) :])
    if encoded.startswith(_LEGACY_B64_PREFIX):
        return _decode_legacy_b64(encoded[len(_LEGACY_B64_PREFIX) :])
    # Unprefixed transitional value: try the first-release raw Fernet token, then
    # fall back to the oldest base64-JSON placeholder.
    try:
        return _decrypt_fernet(encoded)
    except CredentialDecryptionError:
        return _decode_legacy_b64(encoded)


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
    # Always (re-)encrypt with the current format — this migrates legacy values on
    # write and rotates the Fernet IV.
    credential, created = IntegrationCredential.objects.get_or_create(
        integration=integration,
        defaults={"team": integration.team, "auth_type": auth_type, "encrypted_data": _encode(data)},
    )
    if not created:
        credential.auth_type = auth_type
        credential.encrypted_data = _encode(data)
        credential.save(update_fields=["auth_type", "encrypted_data", "updated_at"])

    logger.info("Credentials stored for integration %s (auth_type=%s)", integration.pk, auth_type)
    return credential


def get_integration_credentials(integration: Integration) -> dict:
    """Return the decoded credential data dict for the integration.

    Returns an empty dict if no credentials are stored. Raises
    CredentialDecryptionError if a stored value cannot be decrypted/decoded.
    """
    try:
        credential = IntegrationCredential.objects.get(integration=integration)
    except IntegrationCredential.DoesNotExist:
        return {}
    return _decode(credential.encrypted_data)


def reencrypt_legacy_credential(credential: IntegrationCredential) -> bool:
    """Re-encrypt a single credential to the current format if it is legacy.

    Returns True when a re-encryption was performed. Never logs secret values.
    """
    if not is_legacy_format(credential.encrypted_data):
        return False
    data = _decode(credential.encrypted_data)  # may raise CredentialDecryptionError
    credential.encrypted_data = _encode(data)
    credential.save(update_fields=["encrypted_data", "updated_at"])
    return True
