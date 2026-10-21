"""Create or update a team's Business Central integration and store its credentials.

Non-secret settings come from arguments. The Entra client id and secret are read
from the environment and only ever land in the encrypted credential row — never
on the command line, in Integration.config, or in output and logs.

Safe by default: purchase-order sync stays disabled until ``--enable-sync`` is
passed, and scheduled (Celery Beat) sync is never enabled by this command.

Usage:
    export BC_CLIENT_ID='<entra application (client) id>'
    export BC_CLIENT_SECRET='<client secret value>'
    python manage.py setup_business_central_integration --team <team-slug> \\
        --tenant-id <tenant-guid> --environment <environment-name> --company-id <company-guid>
"""

import os

from django.core.management.base import BaseCommand, CommandError

from apps.scm.integrations.business_systems.business_central.client import _resolve_config
from apps.scm.integrations.business_systems.business_central.exceptions import BusinessCentralConfigurationError
from apps.scm.integrations.credentials import get_integration_credentials, set_integration_credentials
from apps.scm.integrations.models import Integration, IntegrationCredential
from apps.teams.models import Team

PROVIDER_CODE = "business_central"
_SOURCE_KEYS = ("tenant_id", "environment", "company_id")


class Command(BaseCommand):
    help = "Configure a team's Business Central integration (credentials are read from the environment)."

    def add_arguments(self, parser):
        parser.add_argument("--team", required=True, help="Team slug to configure")
        parser.add_argument("--tenant-id", required=True, help="Entra tenant id (GUID)")
        parser.add_argument("--environment", required=True, help="Business Central environment name")
        parser.add_argument("--company-id", required=True, help="Business Central company id (GUID)")
        parser.add_argument("--initial-sync-days", type=int, help="Bound the first sync to orders modified recently")
        parser.add_argument("--client-id-env", default="BC_CLIENT_ID", help="Env var holding the client id")
        parser.add_argument("--client-secret-env", default="BC_CLIENT_SECRET", help="Env var holding the secret")
        parser.add_argument(
            "--enable-sync",
            action="store_true",
            help="Allow manual purchase-order syncs (sync_enabled=true). Scheduled sync stays off.",
        )

    def handle(self, *args, **options):
        try:
            team = Team.objects.get(slug=options["team"])
        except Team.DoesNotExist as exc:
            raise CommandError(f"No team with slug '{options['team']}'.") from exc

        source = {
            "tenant_id": options["tenant_id"].strip(),
            "environment": options["environment"].strip(),
            "company_id": options["company_id"].strip(),
        }
        try:
            _resolve_config(source)  # validates required fields and GUID format
        except BusinessCentralConfigurationError as exc:
            raise CommandError(str(exc)) from exc

        integration = Integration.objects.filter(team=team, provider_code=PROVIDER_CODE).first()
        if integration is not None:
            current = {key: str((integration.config or {}).get(key) or "").lower() for key in _SOURCE_KEYS}
            if any(current.values()) and current != {key: value.lower() for key, value in source.items()}:
                config = integration.config or {}
                raise CommandError(
                    f"Team '{team.slug}' is already configured for environment '{config.get('environment')}' / "
                    f"company {config.get('company_id')}. Use a separate Team for another environment."
                )

        credentials = self._read_credentials(
            options, has_stored=integration is not None and self._has_creds(integration)
        )

        config = {
            **((integration.config or {}) if integration else {}),
            **source,
            "api_version": "v2.0",
            "sync_enabled": bool(options["enable_sync"]),
            "scheduled_sync_enabled": False,
        }
        if options["initial_sync_days"] is not None:
            config["initial_sync_days"] = options["initial_sync_days"]

        if integration is None:
            integration = Integration.objects.create(
                team=team,
                name=f"Business Central ({source['environment']})",
                provider_code=PROVIDER_CODE,
                provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
                api_style=Integration.ApiStyle.PROPRIETARY,
                status=Integration.Status.PENDING,
                is_active=True,
                config=config,
            )
            action = "Created"
        else:
            integration.config = config
            integration.save(update_fields=["config", "updated_at"])
            action = "Updated"

        if credentials:
            stored = get_integration_credentials(integration) if self._has_creds(integration) else {}
            set_integration_credentials(integration, IntegrationCredential.AuthType.OAUTH2, {**stored, **credentials})

        self.stdout.write(
            self.style.SUCCESS(
                f"{action} Business Central integration {integration.pk} for team '{team.slug}' "
                f"(environment={source['environment']}, sync_enabled={config['sync_enabled']}, "
                "scheduled_sync_enabled=False)."
            )
        )
        self.stdout.write("Credentials: " + ("stored (encrypted)" if credentials else "unchanged"))

    @staticmethod
    def _has_creds(integration: Integration) -> bool:
        return IntegrationCredential.objects.filter(integration=integration).exists()

    @staticmethod
    def _read_credentials(options, *, has_stored: bool) -> dict:
        values = {
            "client_id": (os.environ.get(options["client_id_env"]) or "").strip(),
            "client_secret": (os.environ.get(options["client_secret_env"]) or "").strip(),
        }
        supplied = {key: value for key, value in values.items() if value}
        if not has_stored and len(supplied) < 2:
            raise CommandError(
                f"Set {options['client_id_env']} and {options['client_secret_env']} in the environment; "
                "credentials are never passed on the command line."
            )
        return supplied
