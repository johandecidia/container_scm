"""Connect a team to CMA CGM's public Track & Trace endpoint.

Writes the verified endpoint configuration onto the team's carrier Integration and
stores the API key through the credential service, which encrypts it at rest.

The key is read from the environment and never printed, never written to the config
JSON, and never logged — the only place it lands is the encrypted credential row.

Usage:
    export CMA_CGM_API_KEY='<api key>'
    python manage.py setup_cma_cgm_integration --team <team-slug> \
        --test-reference <a container the account knows>
"""

import os

from django.core.management.base import BaseCommand, CommandError

from apps.scm.integrations.carriers.auto_link import get_or_create_tracking_provider
from apps.scm.integrations.carriers.cma_cgm.client import (
    CARRIER_NAME,
    PROVIDER_CODE,
    PUBLIC_TRACK_AND_TRACE_CONFIG,
)
from apps.scm.integrations.credentials import set_integration_credentials
from apps.scm.integrations.models import Integration, IntegrationCredential
from apps.teams.models import Team

DEFAULT_KEY_ENV_VAR = "CMA_CGM_API_KEY"


class Command(BaseCommand):
    help = "Configure a team's CMA CGM carrier integration and store its API key (read from the environment)."

    def add_arguments(self, parser):
        parser.add_argument("--team", required=True, help="Team slug to configure")
        parser.add_argument(
            "--key-env",
            default=DEFAULT_KEY_ENV_VAR,
            help=f"Environment variable holding the API key (default: {DEFAULT_KEY_ENV_VAR})",
        )
        parser.add_argument(
            "--test-reference",
            default="",
            help=(
                "A container number the account can see, stored as test_connection_reference "
                "and used only by the connectivity check. None ships in code."
            ),
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
                f"{options['key_env']} is not set. Export the CMA CGM API key in that "
                "environment variable and run again; it is never passed on the command line."
            )

        config = dict(PUBLIC_TRACK_AND_TRACE_CONFIG)
        test_reference = (options["test_reference"] or "").strip().upper()
        if test_reference:
            config["test_connection_reference"] = test_reference

        integration, created = Integration.objects.get_or_create(
            team=team,
            provider_code=PROVIDER_CODE,
            defaults={
                "name": CARRIER_NAME,
                "provider_family": Integration.ProviderFamily.CARRIER,
                "api_style": Integration.ApiStyle.DCSA,
                "status": Integration.Status.ACTIVE,
                "config": config,
                "is_active": True,
            },
        )
        if not created:
            integration.provider_family = Integration.ProviderFamily.CARRIER
            integration.api_style = Integration.ApiStyle.DCSA
            integration.status = Integration.Status.ACTIVE
            integration.is_active = True
            if not options["keep_config"]:
                # Keep a previously configured test reference when this run supplies none,
                # so re-running to rotate the key does not silently disable the check.
                existing_reference = (integration.config or {}).get("test_connection_reference")
                if existing_reference and not test_reference:
                    config["test_connection_reference"] = existing_reference
                integration.config = config
            integration.save(
                update_fields=["provider_family", "api_style", "status", "is_active", "config", "updated_at"]
            )

        set_integration_credentials(integration, IntegrationCredential.AuthType.API_KEY, {"api_key": api_key})

        # The tracking side needs a provider row for subscriptions to point at.
        get_or_create_tracking_provider(carrier_code=PROVIDER_CODE, carrier_name=CARRIER_NAME)

        self.stdout.write(
            self.style.SUCCESS(
                f"{'Created' if created else 'Updated'} {CARRIER_NAME} integration {integration.pk} "
                f"for team '{team.slug}'; API key stored encrypted."
            )
        )
        self.stdout.write(
            f"Endpoint: {integration.config.get('base_url', '')}{integration.config.get('tracking_path', '')}"
        )
        if not integration.config.get("test_connection_reference"):
            self.stdout.write(
                self.style.WARNING(
                    "No test_connection_reference is configured, so test_connection() will report it as "
                    "missing. Re-run with --test-reference <container> to enable the connectivity check."
                )
            )
        self.stdout.write(
            f"Verify with: python manage.py test_carrier_tracking <container> "
            f"--provider {PROVIDER_CODE} --team {team.slug}"
        )
