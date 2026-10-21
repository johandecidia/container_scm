"""Verify connectivity to a Business Central integration (read-only).

Checks, in order: OAuth2 authentication, the configured environment, the
configured company and a purchase-order read. Authentication on its own is not
reported as a working connection. Never prints credentials, tokens or raw
authentication responses.

Usage:
    python manage.py bc_test_connection --integration <id>
"""

from django.core.management.base import BaseCommand, CommandError

from apps.scm.integrations.business_systems.business_central.client import BusinessCentralClient
from apps.scm.integrations.business_systems.business_central.exceptions import BusinessCentralError
from apps.scm.integrations.models import Integration


def get_business_central_integration(integration_id: int) -> Integration:
    """The Business Central integration ``integration_id`` or a CommandError."""
    try:
        integration = Integration.objects.select_related("team").get(pk=integration_id)
    except Integration.DoesNotExist as exc:
        raise CommandError(f"Integration {integration_id} not found") from exc
    if integration.provider_code != "business_central":
        raise CommandError(f"Integration {integration_id} is not a Business Central integration")
    return integration


def describe_target(integration: Integration) -> str:
    config = integration.config or {}
    return (
        f"integration {integration.pk} (team={integration.team.slug}, "
        f"environment={config.get('environment') or '—'}, company={config.get('company_id') or '—'})"
    )


class Command(BaseCommand):
    help = "Test the connection to a Business Central integration (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--integration", type=int, required=True, help="Integration primary key")

    def handle(self, *args, **options):
        integration = get_business_central_integration(options["integration"])
        self.stdout.write(f"Testing Business Central connection for {describe_target(integration)}…")
        try:
            result = BusinessCentralClient(integration=integration).test_connection()
        except BusinessCentralError as exc:
            # Messages are sanitised at the source (no tokens, secrets or response bodies).
            raise CommandError(f"{exc.category}: {exc}") from exc

        for check in result.get("checks", []):
            self.stdout.write(f"  OK  {check}")
        self.stdout.write(self.style.SUCCESS("Business Central connection: OK"))
