"""Which physical containers a posted purchase receipt contains (stage 2c).

BC's receipt lines carry no serial numbers and no ledger reference (verified,
stage 2). The link is BC's own posting record: posting a receipt with item tracking
writes one item ledger entry per serial, with ``documentType`` "Purchase Receipt"
and ``documentNo`` = the receipt number, in the same company. Posted receipt numbers
are unique per company, so the set of serials on a receipt is exact — no guessing
from item numbers or quantities.

Quantities are used only as a *check*: per container item, the receipt lines' total
must equal the net quantity of the serial entries. Anything else is not resolved:

  receipt  AMBIGUOUS        a serial nets to more than one, sits on two items or two
                            locations, or there are more serial entries than lines say
           MISSING_SERIAL   a container item has fewer serial entries than its lines
                            (also while the ledger sync has not caught up yet)
           UNCLASSIFIED_ITEM an item with serial entries has no classification rule
           REVERSED         every container on it was undone in BC (net zero)
           NOT_APPLICABLE   no container items at all
           RESOLVED         everything above holds

  serial   INVALID_SERIAL, UNCLASSIFIED_ITEM, NOT_APPLICABLE (not a container),
           REVERSED (net zero — undo receipt), AMBIGUOUS, MISSING_EQUIPMENT_TYPE (no
           container in Pegasus and no type to create one), RESOLVED

Only reads ledger entries, receipts, rules and containers; writes only the
resolution fields. Recomputed on every pass, so a corrected rule, a late ledger
sync or a newly registered container takes effect next time. Never touches BC.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.scm.containers.intake import get_team_containers_by_parts, parse_and_validate_container_number
from apps.scm.integrations.models import (
    BusinessCentralItemLedgerEntry,
    BusinessCentralPurchaseReceipt,
    BusinessCentralReceiptContainer,
    Integration,
    ReceiptResolution,
)

from .item_rules import ItemRuleSet
from .locks import sync_lock

logger = logging.getLogger(__name__)

Entry = BusinessCentralItemLedgerEntry
Receipt = BusinessCentralPurchaseReceipt
Line = BusinessCentralReceiptContainer
R = ReceiptResolution
Classification = Entry.ItemClassification

RECEIPT_DOCUMENT_TYPE = "Purchase Receipt"
PURCHASE_ENTRY_TYPE = "Purchase"
_ONE = Decimal(1)
_CHUNK_SIZE = 500
# Receipt status when several problems apply: the most serious wins.
_RECEIPT_PRECEDENCE = [R.AMBIGUOUS, R.MISSING_SERIAL, R.UNCLASSIFIED_ITEM]


@dataclass
class SerialGroup:
    """A receipt's item ledger entries for one serial number."""

    serial_no: str
    serial_status: str
    items: set[str] = field(default_factory=set)
    locations: set[str] = field(default_factory=set)
    net: Decimal = Decimal(0)
    first_positive_entry_no: int | None = None
    entry_no: int = 0
    posting_date: date | None = None

    def add(self, entry: Entry) -> None:
        quantity = entry.quantity or Decimal(0)
        self.items.add(entry.item_no.upper())
        self.locations.add(entry.location_code.upper())
        self.net += quantity
        self.entry_no = max(self.entry_no, entry.entry_no)
        if quantity > 0 and (self.first_positive_entry_no is None or entry.entry_no < self.first_positive_entry_no):
            self.first_positive_entry_no = entry.entry_no
            self.posting_date = entry.posting_date

    @property
    def item_no(self) -> str:
        return next(iter(self.items)) if len(self.items) == 1 else ""

    @property
    def location_code(self) -> str:
        return next(iter(self.locations)) if len(self.locations) == 1 else ""


@dataclass
class ResolutionResult:
    receipts: int = 0
    serials: int = 0
    by_receipt_status: Counter[str] = field(default_factory=Counter)
    by_serial_status: Counter[str] = field(default_factory=Counter)


def receipt_entries(integration: Integration):
    """The integration's item ledger entries posted by purchase receipts (with a serial)."""
    return Entry.objects.filter(
        integration=integration, document_type=RECEIPT_DOCUMENT_TYPE, entry_type=PURCHASE_ENTRY_TYPE
    ).exclude(serial_no="")


def processing_lock_name(integration: Integration) -> str:
    """One lock for resolving and receiving an integration's receipts: they never overlap."""
    return f"{integration.pk}:purchase_receipt_processing"


def resolve_purchase_receipts(
    integration: Integration, *, receipt_nos: Iterable[str] | None = None
) -> ResolutionResult:
    """Recompute the resolution of the integration's receipts (all, or only ``receipt_nos``).

    Raises BusinessCentralSyncInProgressError if the integration's receipts are
    already being processed.
    """
    with sync_lock(processing_lock_name(integration)):
        return resolve_purchase_receipts_locked(integration, receipt_nos=receipt_nos)


def resolve_purchase_receipts_locked(
    integration: Integration, *, receipt_nos: Iterable[str] | None = None
) -> ResolutionResult:
    """``resolve_purchase_receipts`` for a caller already holding the processing lock."""
    rules = ItemRuleSet.for_integration(integration)
    entries = receipt_entries(integration).only(
        "company_id", "document_no", "serial_no", "serial_status", "item_no", "location_code", "quantity",
        "entry_no", "posting_date",
    )  # fmt: skip
    receipts = Receipt.objects.filter(integration=integration).order_by("pk")
    if receipt_nos is not None:
        receipt_nos = list(receipt_nos)
        entries = entries.filter(document_no__in=receipt_nos)
        receipts = receipts.filter(receipt_no__in=receipt_nos)

    groups: dict[tuple[str, str], dict[str, SerialGroup]] = defaultdict(dict)
    for entry in entries.iterator(chunk_size=2000):
        by_serial = groups[(entry.company_id, entry.document_no)]
        group = by_serial.get(entry.serial_no) or SerialGroup(entry.serial_no, entry.serial_status)
        by_serial[entry.serial_no] = group
        group.add(entry)

    result = ResolutionResult()
    chunk: list[Receipt] = []
    for receipt in receipts.iterator(chunk_size=_CHUNK_SIZE):
        chunk.append(receipt)
        if len(chunk) >= _CHUNK_SIZE:
            _resolve_chunk(integration, rules, chunk, groups, result)
            chunk = []
    if chunk:
        _resolve_chunk(integration, rules, chunk, groups, result)
    logger.info(
        "BC receipt resolution: integration=%s receipts=%d serials=%d %s",
        integration.pk,
        result.receipts,
        result.serials,
        dict(result.by_receipt_status),
    )
    return result


def _resolve_chunk(integration, rules: ItemRuleSet, chunk: list[Receipt], groups, result: ResolutionResult) -> None:
    now = timezone.now()
    per_receipt = {r.pk: groups.get((r.company_id, r.receipt_no), {}) for r in chunk}
    parts = {
        serial: parse_and_validate_container_number(serial)
        for serials in per_receipt.values()
        for serial, group in serials.items()
        if group.serial_status == Entry.SerialStatus.VALID_ISO
    }
    containers = get_team_containers_by_parts(integration.team, list(parts.values()))
    existing = {(line.receipt_id, line.serial_no): line for line in Line.objects.filter(receipt__in=chunk)}

    changed_receipts, new_lines, changed_lines = [], [], []
    for receipt in chunk:
        serials = per_receipt[receipt.pk]
        status, details = _receipt_status(receipt, serials, rules)
        result.receipts += 1
        result.by_receipt_status[status] += 1
        if (receipt.resolution_status, receipt.resolution_details) != (status, details):
            receipt.resolution_status, receipt.resolution_details = status, details
            receipt.resolved_at = receipt.updated_at = now
            changed_receipts.append(receipt)

        for serial, group in serials.items():
            p = parts.get(serial)
            container = containers.get((p["owner_code"], p["category_id"], p["serial_number"])) if p else None
            line_status = _serial_status(group, rules, container)
            result.serials += 1
            result.by_serial_status[line_status] += 1
            values = {
                "item_no": group.item_no,
                "location_code": group.location_code,
                "posting_date": group.posting_date,
                "entry_no": group.first_positive_entry_no or group.entry_no,
                "net_quantity": group.net,
                "resolution_status": line_status,
                "container": container,
            }
            line = existing.get((receipt.pk, serial))
            if line is None:
                new_lines.append(
                    Line(team=integration.team, receipt=receipt, serial_no=serial, resolved_at=now, **values)
                )
            elif any(getattr(line, k) != v for k, v in values.items()):
                for name, value in values.items():
                    setattr(line, name, value)
                line.resolved_at = line.updated_at = now
                changed_lines.append(line)

    with transaction.atomic():
        Receipt.objects.bulk_update(
            changed_receipts, ["resolution_status", "resolution_details", "resolved_at", "updated_at"]
        )
        Line.objects.bulk_create(new_lines)
        Line.objects.bulk_update(changed_lines, [*_LINE_FIELDS, "resolved_at", "updated_at"])


_LINE_FIELDS = [
    "item_no", "location_code", "posting_date", "entry_no", "net_quantity", "resolution_status", "container",
]  # fmt: skip


def _receipt_status(receipt: Receipt, serials: dict[str, SerialGroup], rules: ItemRuleSet) -> tuple[str, dict]:
    """The receipt's status and, per container item, line quantity vs. net serial quantity."""
    line_qty: dict[str, Decimal] = defaultdict(Decimal)
    for line in receipt.lines:
        if line.get("lineType") == "Item" and line.get("lineObjectNumber"):
            line_qty[str(line["lineObjectNumber"]).upper()] += _decimal(line.get("quantity"))
    serial_qty: dict[str, Decimal] = defaultdict(Decimal)
    problems: set[str] = set()
    for group in serials.values():
        if len(group.items) != 1 or len(group.locations) != 1 or group.net not in (0, 1):
            problems.add(R.AMBIGUOUS)
        for item in group.items:
            serial_qty[item] += group.net if len(group.items) == 1 else 0

    items: dict[str, dict] = {}
    container_items = 0
    for item in sorted(set(line_qty) | set(serial_qty)):
        resolution = rules.resolve(item)
        has_serials = any(item in g.items for g in serials.values())
        if resolution.classification == Classification.NOT_CONTAINER:
            continue
        if resolution.classification != Classification.CONTAINER:
            if has_serials:  # an unclassified item without serials cannot be a tracked container here
                problems.add(R.UNCLASSIFIED_ITEM)
                items[item] = {"lines": str(line_qty[item]), "serials": str(serial_qty[item]), "classified": False}
            continue
        container_items += 1
        expected, found = line_qty[item], serial_qty[item]
        items[item] = {"lines": str(expected), "serials": str(found)}
        if found < expected:
            problems.add(R.MISSING_SERIAL)
        elif found > expected:
            problems.add(R.AMBIGUOUS)

    details = {"items": items}
    for status in _RECEIPT_PRECEDENCE:
        if status in problems:
            return status, details
    if not container_items:
        return R.NOT_APPLICABLE, details
    if serials and all(g.net == 0 for g in serials.values()):
        return R.REVERSED, details
    return R.RESOLVED, details


def _serial_status(group: SerialGroup, rules: ItemRuleSet, container) -> str:
    if group.serial_status != Entry.SerialStatus.VALID_ISO:
        return R.INVALID_SERIAL
    if len(group.items) != 1 or len(group.locations) != 1:
        return R.AMBIGUOUS
    resolution = rules.resolve(group.item_no)
    if resolution.classification == Classification.NOT_CONTAINER:
        return R.NOT_APPLICABLE
    if resolution.classification != Classification.CONTAINER:
        return R.UNCLASSIFIED_ITEM
    if group.net == 0:
        return R.REVERSED
    if group.net != _ONE:
        return R.AMBIGUOUS
    if container is None and resolution.equipment_type is None:
        return R.MISSING_EQUIPMENT_TYPE
    return R.RESOLVED


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value)) if value is not None and not isinstance(value, bool) else Decimal(0)
    except ArithmeticError:
        return Decimal(0)
