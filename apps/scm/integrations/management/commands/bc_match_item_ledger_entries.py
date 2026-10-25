"""Link a Business Central integration's item ledger entries to existing containers.

Usage:
    python manage.py bc_match_item_ledger_entries --integration <id>

Operation A only: matches entries against containers that already exist in the
team, using the integration's item rules. Never creates or changes a container
and never calls Business Central, so it can be re-run after changing the rules.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.scm.integrations.business_systems.business_central.container_links import match_item_ledger_entries
from apps.scm.integrations.business_systems.business_central.exceptions import BusinessCentralError
from apps.scm.integrations.management.commands.bc_test_connection import (
    describe_target,
    get_business_central_integration,
)


class Command(BaseCommand):
    help = "Link Business Central item ledger entries to existing containers (creates nothing)."

    def add_arguments(self, parser):
        parser.add_argument("--integration", type=int, required=True, help="Integration primary key")

    def handle(self, *args, **options):
        integration = get_business_central_integration(options["integration"])
        self.stdout.write(f"Matching item ledger entries for {describe_target(integration)}…")
        try:
            result = match_item_ledger_entries(integration)
        except BusinessCentralError as exc:
            raise CommandError(f"{exc.category}: {exc}") from exc
        self.stdout.write(self.style.SUCCESS(f"Checked {result.checked} entries, {result.changed} changed."))
        for status, count in sorted(result.by_status.items()):
            self.stdout.write(f"  {status}: {count}")
