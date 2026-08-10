from django.apps import AppConfig
from django.core.checks import register


class IntegrationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.scm.integrations"
    label = "scm_integrations"

    def ready(self):
        from .checks import check_credential_encryption_key

        register(check_credential_encryption_key)
