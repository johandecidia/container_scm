"""Inspect a few Business Central purchase orders without importing them.

Runs the connection test, then fetches at most ``--limit`` purchase orders (the
limit is sent to Business Central as ``$top``) and at most ``--line-limit`` lines
per order, and prints a sanitised summary next to how SCM would map each record.

Read-only: performs no writes to procurement models (only the usual sanitised
IntegrationRequestLog rows for the HTTP calls). Never prints credentials, tokens
or full raw payloads; ``--show-fields`` lists the returned fields with
sensitive-looking names masked.

Usage:
    python manage.py bc_inspect_purchase_orders --integration <id> --limit 3
    python manage.py bc_inspect_purchase_orders --integration <id> --limit 1 --show-fields
"""

import argparse

from django.core.management.base import BaseCommand, CommandError

from apps.scm.integrations.business_systems.business_central.client import BusinessCentralClient
from apps.scm.integrations.business_systems.business_central.exceptions import BusinessCentralError
from apps.scm.integrations.business_systems.business_central.mapper import BusinessCentralMapper
from apps.scm.integrations.credentials import is_sensitive_field
from apps.scm.integrations.management.commands.bc_test_connection import (
    describe_target,
    get_business_central_integration,
)

_MAX_LIMIT = 50
_MAX_LINE_LIMIT = 200
_MAX_VALUE_CHARS = 80


def _bounded(maximum: int):
    def parse(value: str) -> int:
        number = int(value)
        if not 1 <= number <= maximum:
            raise argparse.ArgumentTypeError(f"must be between 1 and {maximum}")
        return number

    return parse


class Command(BaseCommand):
    help = "Fetch and display a few Business Central purchase orders (read-only, nothing is imported)."

    def add_arguments(self, parser):
        parser.add_argument("--integration", type=int, required=True, help="Integration primary key")
        parser.add_argument(
            "--limit",
            type=_bounded(_MAX_LIMIT),
            default=3,
            help=f"Purchase orders to fetch (1-{_MAX_LIMIT}, default 3)",
        )
        parser.add_argument(
            "--line-limit",
            type=_bounded(_MAX_LINE_LIMIT),
            default=10,
            help=f"Lines to fetch per purchase order (1-{_MAX_LINE_LIMIT}, default 10)",
        )
        parser.add_argument(
            "--show-fields",
            action="store_true",
            help="Also list every returned field (sensitive-looking names masked, values truncated).",
        )

    def handle(self, *args, **options):
        integration = get_business_central_integration(options["integration"])
        self.stdout.write(f"Inspecting {describe_target(integration)} — read-only, nothing is imported.")
        mapper = BusinessCentralMapper()
        try:
            client = BusinessCentralClient(integration=integration)
            connection = client.test_connection()
            self.stdout.write(self.style.SUCCESS("Business Central connection: OK"))
            self.stdout.write(f"Environment: {connection.get('environment')}")
            self.stdout.write(f"Company: {connection.get('company_name') or '—'}")

            raw_pos = client.fetch_purchase_orders(top=options["limit"])
            self.stdout.write(f"Purchase orders returned: {len(raw_pos)} (limit {options['limit']})")
            for raw_po in raw_pos:
                po_id = str(raw_po.get("id") or "")
                raw_lines = client.fetch_purchase_order_lines(po_id, top=options["line_limit"]) if po_id else []
                self._print_purchase_order(raw_po, raw_lines, mapper, show_fields=options["show_fields"])
        except BusinessCentralError as exc:
            raise CommandError(f"{exc.category}: {exc}") from exc

    def _print_purchase_order(self, raw_po: dict, raw_lines: list[dict], mapper, *, show_fields: bool) -> None:
        self.stdout.write("")
        self.stdout.write(f"Purchase Order: {raw_po.get('number') or '—'}")
        self.stdout.write(f"BC ID: {raw_po.get('id') or '—'}")
        self.stdout.write(f"Vendor: {raw_po.get('vendorNumber') or '—'} {raw_po.get('vendorName') or ''}".rstrip())
        try:
            mapped = mapper.map_purchase_order(raw_po, raw_lines)
        except ValueError as exc:
            self.stdout.write(self.style.WARNING(f"Would NOT import — mapping error: {exc}"))
            mapped = None
        mapped_status = f" (SCM status: {mapped.status})" if mapped else ""
        self.stdout.write(f"Status: {raw_po.get('status') or '—'}{mapped_status}")
        self.stdout.write(f"Order Date: {raw_po.get('orderDate') or '—'}")
        self.stdout.write(f"Last Modified: {raw_po.get('lastModifiedDateTime') or '—'}")
        if show_fields:
            self._print_fields(raw_po, indent="  ")

        self.stdout.write(f"Lines ({len(raw_lines)} fetched):")
        if not raw_lines:
            self.stdout.write("  (none)")
        for raw_line in raw_lines:
            self.stdout.write(f"  Line {raw_line.get('sequence') or '—'} [{raw_line.get('lineType') or '—'}]")
            self.stdout.write(f"  Item: {raw_line.get('lineObjectNumber') or '—'}")
            self.stdout.write(f"  Quantity: {raw_line.get('quantity')}")
            self.stdout.write(f"  Unit Price: {raw_line.get('directUnitCost')}")
            self.stdout.write(f"  Expected Receipt Date: {raw_line.get('expectedReceiptDate') or '—'}")
            if show_fields:
                self._print_fields(raw_line, indent="    ")
            self.stdout.write("")

    def _print_fields(self, raw: dict, *, indent: str) -> None:
        for name in sorted(raw):
            if is_sensitive_field(name):
                value = "*** (masked)"
            else:
                value = repr(raw[name])
                if len(value) > _MAX_VALUE_CHARS:
                    value = value[:_MAX_VALUE_CHARS] + "…"
            self.stdout.write(f"{indent}· {name} = {value}")
