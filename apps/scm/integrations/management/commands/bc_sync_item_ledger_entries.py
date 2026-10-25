"""Run a Business Central item-ledger-entry sync for one integration.

Usage:
    python manage.py bc_sync_item_ledger_entries --integration <id>
    python manage.py bc_sync_item_ledger_entries --integration <id> --until-complete
    python manage.py bc_sync_item_ledger_entries --integration <id> --dummy

Each run stores at most ``max_entries_per_run`` entries (see the integration's
``resources.item_ledger_entries`` config) and is resumable; ``--until-complete``
repeats runs until nothing is left (bounded by ``--max-runs``), which is how an
initial historical import is done by hand. Requires the resource to be enabled
(``resources.item_ledger_entries.enabled: true``) and ``sync_enabled``;
independent of scheduled sync. Only reads Business Central and only writes
item ledger entries — no containers are created or changed.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.scm.integrations.business_systems.business_central.client import BusinessCentralClient
from apps.scm.integrations.business_systems.business_central.exceptions import BusinessCentralError
from apps.scm.integrations.business_systems.business_central.item_ledger_sync import (
    sync_item_ledger_entries_from_business_central,
)
from apps.scm.integrations.management.commands.bc_test_connection import (
    describe_target,
    get_business_central_integration,
)


class Command(BaseCommand):
    help = "Sync item ledger entries from Business Central for one integration."

    def add_arguments(self, parser):
        parser.add_argument("--integration", type=int, required=True, help="Integration primary key")
        parser.add_argument(
            "--dummy",
            action="store_true",
            help="Use the dummy fixture client instead of the live API (no credentials needed).",
        )
        parser.add_argument(
            "--until-complete",
            action="store_true",
            help="Repeat runs until no more entries are left (initial import).",
        )
        parser.add_argument("--max-runs", type=int, default=100, help="Upper bound for --until-complete (default 100).")

    def handle(self, *args, **options):
        if options["max_runs"] < 1:
            raise CommandError("--max-runs must be at least 1")
        integration = get_business_central_integration(options["integration"])
        mode = "dummy fixtures" if options["dummy"] else "live API"
        self.stdout.write(f"Syncing item ledger entries for {describe_target(integration)} from the {mode}…")

        runs = 0
        while True:
            client = BusinessCentralClient(use_dummy=True) if options["dummy"] else None
            try:
                run = sync_item_ledger_entries_from_business_central(integration, client=client, trigger_type="manual")
            except BusinessCentralError as exc:
                raise CommandError(f"{exc.category}: {exc}") from exc
            runs += 1
            metadata = run.metadata or {}
            quality = metadata.get("quality", {})
            self.stdout.write(
                self.style.SUCCESS(
                    f"Run {runs} {run.status}: fetched={run.records_fetched} created={run.records_created} "
                    f"updated={run.records_updated} unchanged={run.records_unchanged} failed={run.records_failed} "
                    f"cursor={metadata.get('cursor_to', '—')} valid_iso={quality.get('valid_iso', 0)} "
                    f"invalid_iso={quality.get('invalid_iso', 0)} missing_serial={quality.get('missing_serial', 0)}"
                )
            )
            if run.error_summary:
                self.stdout.write(self.style.WARNING(run.error_summary))
            if not (options["until_complete"] and metadata.get("has_more") and run.status == "completed"):
                break
            if runs >= options["max_runs"]:
                self.stdout.write(self.style.WARNING(f"Stopped after {runs} runs; more entries remain."))
                break
