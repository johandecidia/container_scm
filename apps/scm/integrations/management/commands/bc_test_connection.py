"""Verify connectivity to a Business Central integration (read-only).

Usage:
    python manage.py bc_test_connection --integration <id>
"""

from django.core.management.base import BaseCommand, CommandError

from apps.scm.integrations.business_systems.business_central.client import BusinessCentralClient
from apps.scm.integrations.business_systems.business_central.exceptions import BusinessCentralError
from apps.scm.integrations.models import Integration


class Command(BaseCommand):
    help = "Test the connection to a Business Central integration (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--integration", type=int, required=True, help="Integration primary key")

    def handle(self, *args, **options):
        try:
            integration = Integration.objects.get(pk=options["integration"])
        except Integration.DoesNotExist as exc:
            raise CommandError(f"Integration {options['integration']} not found") from exc

        self.stdout.write(
            f"Testing connection for integration {integration.pk} "
            f"(team={integration.team.slug}, provider={integration.provider_code})…"
        )
        try:
            client = BusinessCentralClient(integration=integration)
            result = client.test_connection()
        except BusinessCentralError as exc:
            # Message is already sanitised (no tokens/secrets).
            raise CommandError(f"{type(exc).__name__}: {exc}") from exc

        self.stdout.write(self.style.SUCCESS(result.get("message", "Connected")))
