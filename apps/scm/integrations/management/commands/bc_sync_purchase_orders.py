"""Run a Business Central purchase-order sync for one integration.

Usage:
    python manage.py bc_sync_purchase_orders --integration <id>
    python manage.py bc_sync_purchase_orders --integration <id> --dummy

For a limited first sync against a live sandbox, set ``initial_sync_days`` in the
integration config so only recently modified orders are pulled.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.scm.integrations.business_systems.business_central.client import BusinessCentralClient
from apps.scm.integrations.business_systems.business_central.exceptions import BusinessCentralError
from apps.scm.integrations.business_systems.business_central.sync import (
    sync_purchase_orders_from_business_central,
)
from apps.scm.integrations.models import Integration


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
        try:
            integration = Integration.objects.get(pk=options["integration"])
        except Integration.DoesNotExist as exc:
            raise CommandError(f"Integration {options['integration']} not found") from exc

        client = BusinessCentralClient(use_dummy=True) if options["dummy"] else None
        try:
            run = sync_purchase_orders_from_business_central(integration, client=client, trigger_type="manual")
        except BusinessCentralError as exc:
            raise CommandError(f"{type(exc).__name__}: {exc}") from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"Sync {run.status}: fetched={run.records_fetched} created={run.records_created} "
                f"updated={run.records_updated} unchanged={run.records_unchanged} failed={run.records_failed}"
            )
        )
        if run.error_summary:
            self.stdout.write(self.style.WARNING(run.error_summary))
