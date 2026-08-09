"""Connect a team to Maersk's public Track & Trace endpoint.

Writes the verified endpoint configuration onto the team's carrier Integration and
stores the consumer key through the credential service, which encrypts it at rest.

The key is read from the environment and never printed, never written to the config
JSON, and never logged — the only place it lands is the encrypted credential row.

Usage:
    export MAERSK_CONSUMER_KEY='<consumer key>'
    python manage.py setup_maersk_integration --team <team-slug>
"""

import os

from django.core.management.base import BaseCommand, CommandError

from apps.scm.integrations.carriers.auto_link import get_or_create_tracking_provider
from apps.scm.integrations.carriers.maersk.client import (
    CARRIER_NAME,
    PROVIDER_CODE,
    PUBLIC_TRACK_AND_TRACE_CONFIG,
)
from apps.scm.integrations.credentials import set_integration_credentials
from apps.scm.integrations.models import Integration, IntegrationCredential
from apps.teams.models import Team

DEFAULT_KEY_ENV_VAR = "MAERSK_CONSUMER_KEY"


class Command(BaseCommand):
    help = "Configure a team's Maersk carrier integration and store its consumer key (read from the environment)."

    def add_arguments(self, parser):
        parser.add_argument("--team", required=True, help="Team slug to configure")
        parser.add_argument(
            "--key-env",
            default=DEFAULT_KEY_ENV_VAR,
            help=f"Environment variable holding the consumer key (default: {DEFAULT_KEY_ENV_VAR})",
        )
        parser.add_argument(
            "--keep-config",
            action="store_true",
            help="Keep the existing Integration.config instead of replacing it with the verified defaults.",
        )

    def handle(self, *args, **options):
        try:
            team = Team.objects.get(slug=options["team"])
        except Team.DoesNotExist as exc:
            raise CommandError(f"No team with slug '{options['team']}'.") from exc

        api_key = (os.environ.get(options["key_env"]) or "").strip()
        if not api_key:
            raise CommandError(
                f"{options['key_env']} is not set. Export the Maersk consumer key in that "
                "environment variable and run again; it is never passed on the command line."
            )

        integration, created = Integration.objects.get_or_create(
            team=team,
            provider_code=PROVIDER_CODE,
            defaults={
                "name": CARRIER_NAME,
                "provider_family": Integration.ProviderFamily.CARRIER,
                "api_style": Integration.ApiStyle.DCSA,
                "status": Integration.Status.ACTIVE,
                "config": dict(PUBLIC_TRACK_AND_TRACE_CONFIG),
                "is_active": True,
            },
        )
        if not created:
            integration.provider_family = Integration.ProviderFamily.CARRIER
            integration.api_style = Integration.ApiStyle.DCSA
            integration.status = Integration.Status.ACTIVE
            integration.is_active = True
            if not options["keep_config"]:
                integration.config = dict(PUBLIC_TRACK_AND_TRACE_CONFIG)
            integration.save(
                update_fields=["provider_family", "api_style", "status", "is_active", "config", "updated_at"]
            )

        set_integration_credentials(integration, IntegrationCredential.AuthType.API_KEY, {"api_key": api_key})

        # The tracking side needs a provider row for subscriptions to point at.
        get_or_create_tracking_provider(carrier_code=PROVIDER_CODE, carrier_name=CARRIER_NAME)

        self.stdout.write(
            self.style.SUCCESS(
                f"{'Created' if created else 'Updated'} {CARRIER_NAME} integration {integration.pk} "
                f"for team '{team.slug}'; consumer key stored encrypted."
            )
        )
        self.stdout.write(
            f"Endpoint: {integration.config.get('base_url', '')}{integration.config.get('tracking_path', '')}"
        )
        self.stdout.write(
            f"Verify with: python manage.py test_maersk_tracking "
            f"{integration.config.get('test_connection_reference', '<container>')} --team {team.slug}"
        )
