"""Which purchase order line each posted-receipt serial belongs to (stage 3b.4).

The chain, every step on a verified API field (sandbox, PO 100021):

  item ledger entry --documentNo--> posted purchase receipt   (receipt_resolution, stage 2)
  posted receipt    --orderNumber-> purchase order            (PurchaseOrder.po_number)
  receipt line      --sequence----> purchase order line       (PurchaseOrderLine.line_no)

Item ledger entries carry no order or document-line reference for purchases (their
``Order_No``/``Order_Line_No`` are empty), so the serial-to-*line* step goes through
the receipt's own lines: a receipt line repeats its order line's ``sequence``. That
step is only certain when the receipt has exactly one received line for the serial's
item. With the same item on two received lines the serial could belong to either,
and the serial is reported as unattributed rather than guessed.

Only serials that receipt resolution already accepted count: the receipt RESOLVED
(quantities reconcile, nothing ambiguous) and the serial a single, valid container
unit (net quantity one). Undone receipts, reversed serials and ambiguous postings
never reach a line.

A posted receipt is commercial evidence, not a physical receive. Nothing here
creates a container, a movement or a receive, sets a location or touches tracking.
The one write is :func:`link_posted_receipt_serials`, which records a
``ContainerAcquisition`` for a container that already exists in the team, through
the Business Central side of the procurement link API.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.scm.containers.intake import get_team_containers_by_parts, parse_and_validate_container_number
from apps.scm.containers.models import Container
from apps.scm.integrations.models import (
    BusinessCentralPurchaseReceipt,
    BusinessCentralReceiptContainer,
    Integration,
    ReceiptResolution,
)
from apps.scm.procurement.container_links import link_business_central_acquisition
from apps.scm.procurement.models import ContainerAcquisition, PurchaseOrder, PurchaseOrderLine, PurchaseOrderSource
from apps.teams.models import Team

logger = logging.getLogger(__name__)

R = ReceiptResolution
_ONE = Decimal(1)
# A serial is a received container unit once resolution has accepted it. A missing
# equipment type only matters for *creating* a container, not for which line it is on.
_ACCEPTED_SERIAL_STATUSES = (R.RESOLVED, R.MISSING_EQUIPMENT_TYPE)


class PostedSerialState(models.TextChoices):
    LINKED = "linked", _("Linked")  # the container's acquisition is this line
    UNLINKED = "unlinked", _("Not linked yet")  # the container exists, nothing links it yet
    NOT_IN_PEGASUS = "not_in_pegasus", _("Not in Pegasus")  # no container with this number in the team
    CONFLICT = "conflict", _("Conflict")  # the container was acquired through another line
    SEVERAL_ORDERS = "several_orders", _("Posted on several orders")  # also posted on another order


class UnattributedReason(models.TextChoices):
    SAME_ITEM_ON_SEVERAL_LINES = "same_item_on_several_lines", _("Same item on several receipt lines")
    NO_RECEIPT_LINE = "no_receipt_line", _("No receipt line for the item")
    ORDER_LINE_NOT_FOUND = "order_line_not_found", _("Order line not found")
    SEVERAL_LINES = "several_lines", _("Posted on several order lines")


@dataclass(frozen=True)
class PostedSerial:
    serial_no: str
    receipt_no: str
    posting_date: date | None
    container: Container | None
    state: str
    # For CONFLICT: the line the container was acquired through instead.
    other_line: PurchaseOrderLine | None = None
    # For SEVERAL_ORDERS: the other order numbers the serial was posted on.
    other_orders: tuple[str, ...] = ()

    @property
    def state_label(self) -> str:
        return str(PostedSerialState(self.state).label)


@dataclass(frozen=True)
class UnattributedSerial:
    serial_no: str
    receipt_no: str
    item_no: str
    reason: str

    @property
    def reason_label(self) -> str:
        return str(UnattributedReason(self.reason).label)


@dataclass
class OrderPostedSerials:
    by_line: dict[int, list[PostedSerial]] = field(default_factory=dict)
    unattributed: list[UnattributedSerial] = field(default_factory=list)

    def for_line(self, line_id: int) -> list[PostedSerial]:
        return self.by_line.get(line_id, [])


@dataclass
class LinkResult:
    orders: int = 0
    linked: int = 0
    already_linked: int = 0
    conflicts: int = 0
    not_in_pegasus: int = 0
    several_orders: int = 0
    unattributed: int = 0


def _integration_for(team: Team) -> Integration | None:
    return Integration.objects.filter(team=team, provider_code="business_central").first()


def _accepted_rows(integration: Integration):
    return BusinessCentralReceiptContainer.objects.filter(
        receipt__integration=integration,
        receipt__resolution_status=R.RESOLVED,
        resolution_status__in=_ACCEPTED_SERIAL_STATUSES,
        net_quantity=_ONE,
    )


def _receipts_for(integration: Integration, purchase_order: PurchaseOrder):
    receipts = BusinessCentralPurchaseReceipt.objects.filter(
        integration=integration, order_no=purchase_order.po_number, resolution_status=R.RESOLVED
    )
    if purchase_order.source_company_id:
        receipts = receipts.filter(company_id__iexact=purchase_order.source_company_id)
    return receipts


def _received_sequences(receipt: BusinessCentralPurchaseReceipt) -> dict[str, list[str]]:
    """Item → the ``sequence`` of every receipt line that received a positive quantity of it."""
    sequences: dict[str, list[str]] = defaultdict(list)
    for line in receipt.lines:
        item = str(line.get("lineObjectNumber") or "").upper()
        try:
            received = Decimal(str(line.get("quantity") or 0)) > 0
        except ArithmeticError:
            received = False
        if line.get("lineType") == "Item" and item and received:
            sequences[item].append(str(line.get("sequence") or ""))
    return sequences


def _containers_by_serial(team: Team, serials: set[str]) -> dict[str, Container]:
    parts = {}
    for serial in serials:
        try:
            parts[serial] = parse_and_validate_container_number(serial)
        except ValidationError:
            continue
    found = get_team_containers_by_parts(team, list(parts.values()))
    return {
        serial: container
        for serial, p in parts.items()
        if (container := found.get((p["owner_code"], p["category_id"], p["serial_number"])))
    }


def get_order_posted_serials(team: Team, purchase_order: PurchaseOrder) -> OrderPostedSerials:
    """The order's posted-receipt serials, per order line, with their container state.

    Read-only. Empty for an order Business Central does not own, or a team without a
    Business Central integration. Every lookup is scoped to ``team``.
    """
    result = OrderPostedSerials()
    if purchase_order.team_id != team.pk or purchase_order.source_system != PurchaseOrderSource.BUSINESS_CENTRAL:
        return result
    integration = _integration_for(team)
    if integration is None:
        return result

    receipts = list(_receipts_for(integration, purchase_order))
    rows = list(
        _accepted_rows(integration).filter(receipt__in=receipts).select_related("receipt").order_by("entry_no", "pk")
    )
    if not rows:
        return result

    lines = {line.line_no: line for line in purchase_order.lines.all()}
    sequences = {receipt.pk: _received_sequences(receipt) for receipt in receipts}
    serials = {row.serial_no for row in rows}
    containers = _containers_by_serial(team, serials)
    acquisitions = {
        a.container_id: a
        for a in ContainerAcquisition.objects.filter(team=team, container__in=containers.values()).select_related(
            "purchase_order_line__purchase_order"
        )
    }
    elsewhere = _posted_on_other_orders(integration, serials, purchase_order.po_number)

    attributed: dict[str, list[tuple[BusinessCentralReceiptContainer, PurchaseOrderLine]]] = defaultdict(list)
    for row in rows:
        item = row.item_no.upper()
        candidates = sequences[row.receipt_id].get(item, [])
        if len(candidates) != 1:
            reason = UnattributedReason.SAME_ITEM_ON_SEVERAL_LINES if candidates else UnattributedReason.NO_RECEIPT_LINE
            result.unattributed.append(UnattributedSerial(row.serial_no, row.receipt.receipt_no, row.item_no, reason))
            continue
        line = lines.get(candidates[0])
        if line is None or line.item_no.upper() != item:
            reason = UnattributedReason.ORDER_LINE_NOT_FOUND
            result.unattributed.append(UnattributedSerial(row.serial_no, row.receipt.receipt_no, row.item_no, reason))
            continue
        attributed[row.serial_no].append((row, line))

    by_line: dict[int, list[PostedSerial]] = defaultdict(list)
    for serial, found in attributed.items():
        if len({line.pk for _, line in found}) > 1:
            # Posted on two lines of this order (a correction in BC): neither is certain.
            for row, _ in found:
                reason = UnattributedReason.SEVERAL_LINES
                result.unattributed.append(UnattributedSerial(serial, row.receipt.receipt_no, row.item_no, reason))
            continue
        row, line = found[0]  # earliest posting; later receipts of the same box on the same line add nothing
        by_line[line.pk].append(_posted_serial(row, line, containers, acquisitions, elsewhere))
    result.by_line = dict(by_line)
    return result


def _posted_on_other_orders(integration: Integration, serials: set[str], po_number: str) -> dict[str, tuple[str, ...]]:
    """Serials also accepted on a receipt of another order — moved or corrected in BC."""
    others: dict[str, set[str]] = defaultdict(set)
    rows = (
        _accepted_rows(integration)
        .filter(serial_no__in=serials)
        .filter(~Q(receipt__order_no=po_number))
        .exclude(receipt__order_no="")
        .values_list("serial_no", "receipt__order_no")
    )
    for serial, order_no in rows:
        others[serial].add(order_no)
    return {serial: tuple(sorted(orders)) for serial, orders in others.items()}


def _posted_serial(row, line, containers, acquisitions, elsewhere) -> PostedSerial:
    container = containers.get(row.serial_no)
    common = {
        "serial_no": row.serial_no,
        "receipt_no": row.receipt.receipt_no,
        "posting_date": row.posting_date,
        "container": container,
    }
    acquisition = acquisitions.get(container.pk) if container else None
    if acquisition is not None and acquisition.purchase_order_line_id != line.pk:
        return PostedSerial(state=PostedSerialState.CONFLICT, other_line=acquisition.purchase_order_line, **common)
    if row.serial_no in elsewhere:
        # Linked or not, the box is posted on two orders: nobody decides between them here.
        return PostedSerial(state=PostedSerialState.SEVERAL_ORDERS, other_orders=elsewhere[row.serial_no], **common)
    if container is None:
        return PostedSerial(state=PostedSerialState.NOT_IN_PEGASUS, **common)
    if acquisition is not None:
        return PostedSerial(state=PostedSerialState.LINKED, **common)
    return PostedSerial(state=PostedSerialState.UNLINKED, **common)


def link_posted_receipt_serials(integration: Integration) -> LinkResult:
    """Record an acquisition for every posted serial whose line and container are certain.

    Only UNLINKED serials are written: an existing container, no acquisition yet, one
    order line, one order. A container acquired through another line is a conflict
    and is left as it is; a serial without a container stays unlinked (nothing is
    created). Idempotent — a second pass finds everything LINKED.
    """
    team = integration.team
    result = LinkResult()
    order_numbers = (
        _accepted_rows(integration).exclude(receipt__order_no="").values_list("receipt__order_no", flat=True).distinct()
    )
    orders = PurchaseOrder.objects.filter(
        team=team, source_system=PurchaseOrderSource.BUSINESS_CENTRAL, po_number__in=order_numbers
    ).order_by("pk")
    for purchase_order in orders:
        result.orders += 1
        posted = get_order_posted_serials(team, purchase_order)
        result.unattributed += len(posted.unattributed)
        lines = {line.pk: line for line in purchase_order.lines.all()}
        for line_id, serials in posted.by_line.items():
            for serial in serials:
                _apply(team, lines[line_id], serial, result)
    logger.info(
        "BC posted serial links: integration=%s orders=%d linked=%d already=%d conflicts=%d "
        "not_in_pegasus=%d several_orders=%d unattributed=%d",
        integration.pk,
        result.orders,
        result.linked,
        result.already_linked,
        result.conflicts,
        result.not_in_pegasus,
        result.several_orders,
        result.unattributed,
    )
    return result


def _apply(team: Team, line: PurchaseOrderLine, serial: PostedSerial, result: LinkResult) -> None:
    if serial.state == PostedSerialState.LINKED:
        result.already_linked += 1
    elif serial.state == PostedSerialState.CONFLICT:
        result.conflicts += 1
    elif serial.state == PostedSerialState.NOT_IN_PEGASUS:
        result.not_in_pegasus += 1
    elif serial.state == PostedSerialState.SEVERAL_ORDERS:
        result.several_orders += 1
    elif serial.state == PostedSerialState.UNLINKED and serial.container is not None:
        try:
            link_business_central_acquisition(team=team, purchase_order_line=line, container=serial.container)
        except ValidationError:
            # Acquired elsewhere between the read and the locked write.
            result.conflicts += 1
        else:
            result.linked += 1
