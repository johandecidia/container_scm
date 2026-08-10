"""Full source reconciliation for a Business Central integration (soft-delete).

Usage:
    python manage.py bc_reconcile_purchase_orders --integration <id>
    python manage.py bc_reconcile_purchase_orders --integration <id> --dummy

Detects purchase orders / lines that no longer exist at the source and marks them
source_active=False (never hard-deleted). Run this deliberately, not on every sync.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.scm.integrations.business_systems.business_central.client import BusinessCentralClient
from apps.scm.integrations.business_systems.business_central.exceptions import BusinessCentralError
from apps.scm.integrations.business_systems.business_central.reconcile import reconcile_purchase_orders
from apps.scm.integrations.models import Integration


class Command(BaseCommand):
    help = "Reconcile Business Central purchase orders (soft-delete records absent at source)."

    def add_arguments(self, parser):
        parser.add_argument("--integration", type=int, required=True, help="Integration primary key")
        parser.add_argument("--dummy", action="store_true", help="Use the dummy fixture client.")

    def handle(self, *args, **options):
        try:
            integration = Integration.objects.get(pk=options["integration"])
        except Integration.DoesNotExist as exc:
            raise CommandError(f"Integration {options['integration']} not found") from exc

        client = BusinessCentralClient(use_dummy=True) if options["dummy"] else None
        try:
            result = reconcile_purchase_orders(integration, client=client)
        except BusinessCentralError as exc:
            raise CommandError(f"{type(exc).__name__}: {exc}") from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"Reconcile: checked={result.checked_pos} deactivated_pos={result.deactivated_pos} "
                f"deactivated_lines={result.deactivated_lines}"
            )
        )
