"""Connect a team to Hapag-Lloyd's DCSA Track & Trace endpoint.

Writes the endpoint configuration onto the team's carrier Integration and stores the
portal-issued client id and secret through the credential service, which encrypts
them at rest.

Both are read from the environment and never printed, never written to the config
JSON, and never logged — the only place they land is the encrypted credential row.
They are deliberately not command-line arguments, because those end up in shell
history and in process listings.

Hapag-Lloyd's Track & Trace sits behind an IBM API Connect gateway: the client id
and secret go on every request as headers and there is nothing to exchange. Setting
HAPAG_TOKEN_URL switches the integration to the OAuth2 client-credentials grant
instead, for a subscribed product that requires one.

Usage:
    export HAPAG_CLIENT_ID='<client id>'
    export HAPAG_CLIENT_SECRET='<client secret>'
    python manage.py setup_hapag_lloyd_integration --team <team-slug> \
        --test-reference <a container the account can see>

Optional environment overrides, for a product whose gateway path differs from the
default this ships with:
    HAPAG_API_BASE_URL   e.g. https://api.hlag.com
    HAPAG_TRACKING_PATH  e.g. /hlag/external/v2/events
    HAPAG_TOKEN_URL      set only when the product uses an OAuth grant
    HAPAG_SCOPE          OAuth scope, when the token endpoint requires one
"""

import os

from django.core.management.base import BaseCommand, CommandError

from apps.scm.integrations.carriers.auto_link import get_or_create_tracking_provider
from apps.scm.integrations.carriers.dcsa.client import AUTH_CLIENT_ID_SECRET_HEADERS, AUTH_OAUTH2
from apps.scm.integrations.carriers.hapag_lloyd.client import (
    CARRIER_NAME,
    PROVIDER_CODE,
    TRACK_AND_TRACE_CONFIG,
)
from apps.scm.integrations.credentials import set_integration_credentials
from apps.scm.integrations.models import Integration, IntegrationCredential
from apps.teams.models import Team

CLIENT_ID_ENV_VAR = "HAPAG_CLIENT_ID"
CLIENT_SECRET_ENV_VAR = "HAPAG_CLIENT_SECRET"
BASE_URL_ENV_VAR = "HAPAG_API_BASE_URL"
TRACKING_PATH_ENV_VAR = "HAPAG_TRACKING_PATH"
TOKEN_URL_ENV_VAR = "HAPAG_TOKEN_URL"
SCOPE_ENV_VAR = "HAPAG_SCOPE"


def build_config(env: dict, *, test_reference: str = "", existing: dict | None = None) -> dict:
    """Return the Integration.config for Hapag-Lloyd, given the environment.

    Starts from the shipped defaults and applies only the values ``env`` actually
    supplies, so an unset override leaves the default in place rather than blanking
    it. A HAPAG_TOKEN_URL switches the auth style to the OAuth grant — the presence
    of a token endpoint is what distinguishes the two products, so it is read as the
    signal rather than asking for the style separately and letting the two disagree.

    ``existing`` is the config already on the integration; a previously configured
    test reference survives a re-run that supplies none, so rotating credentials
    cannot silently disable the connectivity check.
    """
    config = dict(TRACK_AND_TRACE_CONFIG)

    base_url = (env.get(BASE_URL_ENV_VAR) or "").strip()
    if base_url:
        config["base_url"] = base_url
    tracking_path = (env.get(TRACKING_PATH_ENV_VAR) or "").strip()
    if tracking_path:
        config["tracking_path"] = tracking_path

    token_url = (env.get(TOKEN_URL_ENV_VAR) or "").strip()
    if token_url:
        config["auth_style"] = AUTH_OAUTH2
        config["token_url"] = token_url
        scope = (env.get(SCOPE_ENV_VAR) or "").strip()
        if scope:
            config["scope"] = scope
        # The gateway headers are not sent under a bearer token, so leaving their
        # names configured would describe an integration this is not.
        config.pop("client_id_header_name", None)
        config.pop("client_secret_header_name", None)

    reference = (test_reference or "").strip().upper() or (existing or {}).get("test_connection_reference", "")
    if reference:
        config["test_connection_reference"] = reference

    return config


class Command(BaseCommand):
    help = "Configure a team's Hapag-Lloyd carrier integration and store its portal credentials (read from the env)."

    def add_arguments(self, parser):
        parser.add_argument("--team", required=True, help="Team slug to configure")
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
            help="Keep the existing Integration.config instead of replacing it with the defaults.",
        )

    def handle(self, *args, **options):
        try:
            team = Team.objects.get(slug=options["team"])
        except Team.DoesNotExist as exc:
            raise CommandError(f"No team with slug '{options['team']}'.") from exc

        client_id = (os.environ.get(CLIENT_ID_ENV_VAR) or "").strip()
        client_secret = (os.environ.get(CLIENT_SECRET_ENV_VAR) or "").strip()
        missing = [
            name
            for name, value in ((CLIENT_ID_ENV_VAR, client_id), (CLIENT_SECRET_ENV_VAR, client_secret))
            if not value
        ]
        if missing:
            raise CommandError(
                f"{' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} not set. Export the "
                "Hapag-Lloyd API portal credentials in those environment variables and run again; "
                "they are never passed on the command line."
            )

        integration, created = Integration.objects.get_or_create(
            team=team,
            provider_code=PROVIDER_CODE,
            defaults={
                "name": CARRIER_NAME,
                "provider_family": Integration.ProviderFamily.CARRIER,
                "api_style": Integration.ApiStyle.DCSA,
                "status": Integration.Status.ACTIVE,
                "config": build_config(os.environ, test_reference=options["test_reference"]),
                "is_active": True,
            },
        )
        if not created:
            integration.provider_family = Integration.ProviderFamily.CARRIER
            integration.api_style = Integration.ApiStyle.DCSA
            integration.status = Integration.Status.ACTIVE
            integration.is_active = True
            if not options["keep_config"]:
                integration.config = build_config(
                    os.environ,
                    test_reference=options["test_reference"],
                    existing=integration.config or {},
                )
            integration.save(
                update_fields=["provider_family", "api_style", "status", "is_active", "config", "updated_at"]
            )

        auth_style = (integration.config or {}).get("auth_style")
        set_integration_credentials(
            integration,
            IntegrationCredential.AuthType.OAUTH2
            if auth_style == AUTH_OAUTH2
            else IntegrationCredential.AuthType.API_KEY,
            {"client_id": client_id, "client_secret": client_secret},
        )

        # The tracking side needs a provider row for subscriptions to point at.
        get_or_create_tracking_provider(carrier_code=PROVIDER_CODE, carrier_name=CARRIER_NAME)

        self.stdout.write(
            self.style.SUCCESS(
                f"{'Created' if created else 'Updated'} {CARRIER_NAME} integration {integration.pk} "
                f"for team '{team.slug}'; client id and secret stored encrypted."
            )
        )
        config = integration.config or {}
        self.stdout.write(f"Endpoint: {config.get('base_url', '')}{config.get('tracking_path', '')}")
        self.stdout.write(
            f"Auth: {auth_style}"
            + (
                ""
                if auth_style == AUTH_OAUTH2
                else f" ({config.get('client_id_header_name')} + {config.get('client_secret_header_name')})"
            )
        )
        if auth_style == AUTH_CLIENT_ID_SECRET_HEADERS:
            self.stdout.write(
                self.style.WARNING(
                    "Confirm that endpoint against the OpenAPI spec of your subscribed product on the "
                    "Hapag-Lloyd API portal. A wrong path answers 404, which reads as 'no data for this "
                    f"container' rather than as a misconfiguration. Override it with {TRACKING_PATH_ENV_VAR}."
                )
            )
        if not config.get("test_connection_reference"):
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
