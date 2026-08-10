"""System checks for the integrations app."""

from django.conf import settings
from django.core.checks import Error


def check_credential_encryption_key(app_configs, **kwargs):
    """Require a dedicated credential encryption key in production.

    Errors when production settings are in use (integration credentials can be
    stored and decrypted) but SCM_INTEGRATION_ENCRYPTION_KEY is not set. The
    SECRET_KEY-derived fallback is only acceptable for development/tests.

    The key value itself is never read or logged here.
    """
    require = getattr(settings, "SCM_INTEGRATION_REQUIRE_ENCRYPTION_KEY", False)
    configured = bool(getattr(settings, "SCM_INTEGRATION_ENCRYPTION_KEY", "") or "")
    if require and not configured:
        return [
            Error(
                "SCM_INTEGRATION_ENCRYPTION_KEY must be set in production.",
                hint=(
                    "Set the SCM_INTEGRATION_ENCRYPTION_KEY environment variable to a Fernet key "
                    "(cryptography.fernet.Fernet.generate_key()). The SECRET_KEY-derived fallback "
                    "is only for development and tests."
                ),
                id="scm_integrations.E001",
            )
        ]
    return []
