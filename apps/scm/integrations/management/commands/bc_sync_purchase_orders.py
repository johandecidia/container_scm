"""Run a Business Central purchase-order sync for one integration.

Usage:
    python manage.py bc_sync_purchase_orders --integration <id>
    python manage.py bc_sync_purchase_orders --integration <id> --dummy

Requires ``sync_enabled`` on the integration (see
setup_business_central_integration --enable-sync); independent of scheduled sync.
For a limited first sync against a live sandbox, set ``initial_sync_days`` in the
integration config so only recently modified orders are pulled.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.scm.integrations.business_systems.business_central.client import BusinessCentralClient
from apps.scm.integrations.business_systems.business_central.exceptions import BusinessCentralError
from apps.scm.integrations.business_systems.business_central.sync import (
    sync_purchase_orders_from_business_central,
)
from apps.scm.integrations.management.commands.bc_test_connection import (
    describe_target,
    get_business_central_integration,
)


class Command(BaseCommand):
    help = "Sync purchase orders from Business Central for one integration."

    def add_arguments(self, parser):
        parser.add_argument("--integration", type=int, required=True, help="Integration primary key")
        parser.add_argument(
            "--dummy",
            action="store_true",
            help="Use the dummy fixture client instead of the live API (no credentials needed).",
        )

    def handle(self, *args, **options):
        integration = get_business_central_integration(options["integration"])
        mode = "dummy fixtures" if options["dummy"] else "live API"
        self.stdout.write(f"Syncing purchase orders for {describe_target(integration)} from the {mode}…")

        client = BusinessCentralClient(use_dummy=True) if options["dummy"] else None
        try:
            run = sync_purchase_orders_from_business_central(integration, client=client, trigger_type="manual")
        except BusinessCentralError as exc:
            raise CommandError(f"{exc.category}: {exc}") from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"Sync {run.status}: fetched={run.records_fetched} created={run.records_created} "
                f"updated={run.records_updated} unchanged={run.records_unchanged} failed={run.records_failed}"
            )
        )
        if run.error_summary:
            self.stdout.write(self.style.WARNING(run.error_summary))
