"""Everything the purchase order workspace needs, gathered once.

Four ideas shape this module.

**Two statuses, never conflated.** ``PurchaseOrder.status`` is the Business Central
*document* status: what the source system says about the paperwork. The operational
headline is ``PurchaseOrderLogisticsStatus``, computed here by the one canonical
selector from the fulfillment quantities. The workspace leads with the second and
keeps the first as source metadata, because "Released" tells an operator nothing
about whether the boxes are moving.

**Derive, don't duplicate.** Nothing below is stored on the purchase order: no
progress column, no container count, no shipped percentage, no workspace status.
Every figure is read from the PO lines, the supplier deliveries and the containers
those deliveries name, so a new delivery line changes the answer immediately and
there is nothing to reconcile.

**The article survives the join.** A container reaches a purchase order through
``Container → SupplierDeliveryLine → PurchaseOrderLine``, and that middle row
carries the ordered article. Keeping it on the container row is what lets somebody
see which boxes belong to which purchased line, rather than a flat list of numbers
that has lost the reason they are here.

**Completeness, not exceptions.** :attr:`PurchaseOrderWorkspace.gaps` reports what
is objectively missing — units nobody has booked, delivery lines with no container
number, containers on no shipment. These are contextual, derived on read, and
deliberately not a second exception engine: nothing is persisted, nothing is
severity-ranked, and a young purchase order with everything still to do reports
nothing at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from django.db.models import Sum
from django.utils.translation import gettext_lazy as _

from apps.teams.models import Team

from .models import PurchaseOrder, PurchaseOrderLogisticsStatus

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

    from apps.scm.containers.workspace import ContainerWorkspace

_ZERO = Decimal("0")


@dataclass(frozen=True)
class ContainerRow:
    """One container booked against this purchase order, with its transport state.

    ``articles`` is a list because one box can carry more than one ordered line. The
    tracking answers all come from the bulk-built container workspace, so a purchase
    order with eighty containers costs the same number of queries as one with two.
    """

    container: object
    workspace: ContainerWorkspace | None = None
    articles: list[str] = field(default_factory=list)
    delivery_references: list[str] = field(default_factory=list)

    @property
    def article_label(self) -> str:
        return " · ".join(self.articles)

    @property
    def shipment(self):
        return self.workspace.active_shipment if self.workspace else None

    @property
    def eta(self):
        return self.workspace.current_eta if self.workspace else None

    @property
    def current_status(self) -> str:
        return self.workspace.current_status if self.workspace else ""

    @property
    def journey_state(self) -> str:
        """The furthest milestone the carrier has confirmed for this box.

        The visibility layer's own function, imported rather than restated, so the
        purchase order and the Control Tower cannot disagree about what "arrived"
        means. Boxes nobody has tracked report ``unknown``, which is honest: no
        carrier has said anything about them.
        """
        from apps.scm.visibility.read_models import JourneyState, journey_state_from_observed

        if self.workspace is None:
            return JourneyState.UNKNOWN
        return journey_state_from_observed(self.workspace.observed_event_types)

    @property
    def needs_shipment(self) -> bool:
        return self.shipment is None

    @property
    def filter_buckets(self) -> str:
        """Which of the Containers tab's quick filters this row belongs to.

        A space-separated string because the filtering happens in the browser over
        rows that are already rendered — one attribute the client can test, rather
        than four booleans the template has to spell out. A row can be in more than
        one bucket: a box that arrived without ever being put on a shipment is both
        arrived and missing a shipment, and both filters should find it.
        """
        from apps.scm.visibility.read_models import JourneyState

        buckets = []
        if self.needs_shipment:
            buckets.append("needs-shipment")
        state = self.journey_state
        if state == JourneyState.IN_TRANSIT:
            buckets.append("moving")
        elif state in (JourneyState.ARRIVED, JourneyState.DELIVERED):
            buckets.append("arrived")
        return " ".join(buckets)


@dataclass(frozen=True)
class DeliveryRow:
    """One supplier delivery against this purchase order, with what is on it."""

    delivery: object
    quantity: Decimal = _ZERO
    container_count: int = 0
    line_count: int = 0

    @property
    def ship_date(self):
        """When it left, or when it is planned to — actual wins where recorded."""
        return self.delivery.actual_ship_date or self.delivery.planned_ship_date

    @property
    def ship_date_is_actual(self) -> bool:
        return self.delivery.actual_ship_date is not None

    @property
    def arrival_date(self):
        return self.delivery.actual_arrival_date or self.delivery.planned_arrival_date

    @property
    def arrival_date_is_actual(self) -> bool:
        return self.delivery.actual_arrival_date is not None


@dataclass(frozen=True)
class Gap:
    """One objectively missing thing, phrased as a sentence."""

    code: str
    message: StrOrPromise


@dataclass
class PurchaseOrderWorkspace:
    """Read model for the purchase order workspace.

    Built once by :func:`get_purchase_order_workspace`. Every property below is a
    derivation over what that function loaded — no property issues a query except
    :attr:`logistics_status`, which delegates to the canonical selector.
    """

    purchase_order: PurchaseOrder
    lines: list = field(default_factory=list)
    events: list = field(default_factory=list)
    container_rows: list[ContainerRow] = field(default_factory=list)
    delivery_rows: list[DeliveryRow] = field(default_factory=list)
    fulfillment: dict[str, Decimal] = field(default_factory=dict)

    # Quantity booked onto supplier delivery lines, whether or not those lines name
    # a container yet. Distinct from shipped: assigning stock to a delivery is a
    # planning act, and shipping it is a physical one.
    assigned_qty: Decimal = _ZERO
    # Of that, the quantity sitting on delivery lines with no container number.
    unassigned_container_qty: Decimal = _ZERO

    # -- identity and status ------------------------------------------------

    @property
    def logistics_status(self) -> str:
        """The SCM logistics status — the operational headline.

        Delegated to the one canonical implementation rather than recomputed from
        :attr:`fulfillment`, so there is a single definition of what "partially
        shipped" means.
        """
        from .selectors import get_purchase_order_logistics_status

        return get_purchase_order_logistics_status(self.purchase_order)

    @property
    def logistics_status_label(self) -> str:
        return str(PurchaseOrderLogisticsStatus(self.logistics_status).label)

    @property
    def logistics_status_tone(self) -> str:
        """A DaisyUI badge modifier for the logistics status.

        Presentation only. Nothing reads it back, and it never stands in for the
        status itself.
        """
        S = PurchaseOrderLogisticsStatus
        return {
            S.NOT_STARTED: "badge-ghost",
            S.PARTIALLY_SHIPPED: "badge-info",
            S.FULLY_SHIPPED: "badge-info",
            S.ARRIVED: "badge-success",
            S.PARTIALLY_RECEIVED: "badge-success",
            S.COMPLETED: "badge-success",
            S.EXCEPTION: "badge-error",
        }.get(self.logistics_status, "badge-ghost")

    @property
    def is_read_only(self) -> bool:
        """True when the source system owns this record and SCM must not edit it."""
        return self.purchase_order.is_business_central

    @property
    def source_label(self) -> str:
        return self.purchase_order.get_source_system_display()

    # -- progress -----------------------------------------------------------

    @property
    def ordered_qty(self) -> Decimal:
        return self.fulfillment.get("ordered_qty", _ZERO)

    @property
    def shipped_qty(self) -> Decimal:
        return self.fulfillment.get("shipped_qty", _ZERO)

    @property
    def in_transit_qty(self) -> Decimal:
        return self.fulfillment.get("in_transit_qty", _ZERO)

    @property
    def arrived_qty(self) -> Decimal:
        return self.fulfillment.get("arrived_qty", _ZERO)

    @property
    def received_qty(self) -> Decimal:
        return self.fulfillment.get("received_qty", _ZERO)

    @property
    def remaining_qty(self) -> Decimal:
        return self.fulfillment.get("remaining_qty", _ZERO)

    @property
    def unshipped_qty(self) -> Decimal:
        """Ordered but not yet shipped. Never negative."""
        return max(self.ordered_qty - self.shipped_qty, _ZERO)

    @property
    def unbooked_qty(self) -> Decimal:
        """Ordered but not yet put on any supplier delivery."""
        return max(self.ordered_qty - self.assigned_qty, _ZERO)

    def _percent(self, value: Decimal) -> float:
        if self.ordered_qty <= 0:
            return 0.0
        return min(float(value / self.ordered_qty * 100), 100.0)

    @property
    def shipped_percent(self) -> float:
        return self._percent(self.shipped_qty)

    @property
    def arrived_percent(self) -> float:
        return self._percent(self.arrived_qty)

    @property
    def received_percent(self) -> float:
        return self._percent(self.received_qty)

    @property
    def has_progress(self) -> bool:
        """True when there is an ordered quantity to measure progress against."""
        return self.ordered_qty > 0

    # -- containers ---------------------------------------------------------

    @property
    def containers(self) -> list:
        return [row.container for row in self.container_rows]

    @property
    def container_count(self) -> int:
        return len(self.container_rows)

    @property
    def containers_needing_shipment(self) -> list[ContainerRow]:
        return [row for row in self.container_rows if row.needs_shipment]

    @property
    def moving_containers(self) -> list[ContainerRow]:
        from apps.scm.visibility.read_models import JourneyState

        return [row for row in self.container_rows if row.journey_state == JourneyState.IN_TRANSIT]

    @property
    def arrived_containers(self) -> list[ContainerRow]:
        from apps.scm.visibility.read_models import JourneyState

        arrived = (JourneyState.ARRIVED, JourneyState.DELIVERED)
        return [row for row in self.container_rows if row.journey_state in arrived]

    @property
    def next_arrival(self) -> tuple | None:
        """The soonest ETA across this order's containers, and how many share it.

        ``(date, count)``, or None when no container has an ETA. Read off the
        container workspaces the page already built — this is not a second
        visibility engine, it is the earliest of the dates those workspaces derived.
        """
        etas = [row.eta for row in self.container_rows if row.eta is not None]
        if not etas:
            return None
        soonest = min(etas)
        return soonest, sum(1 for eta in etas if eta == soonest)

    @property
    def next_arrival_destinations(self) -> list[str]:
        """Where the containers arriving on the next ETA are headed."""
        arrival = self.next_arrival
        if arrival is None:
            return []
        soonest = arrival[0]
        places = {
            row.shipment.destination_port
            for row in self.container_rows
            if row.eta == soonest and row.shipment is not None and row.shipment.destination_port
        }
        return sorted(places)

    # -- deliveries ---------------------------------------------------------

    @property
    def delivery_count(self) -> int:
        return len(self.delivery_rows)

    # -- completeness -------------------------------------------------------

    @property
    def gaps(self) -> list[Gap]:
        """What is objectively missing, in the order it becomes actionable.

        Deliberately quiet for a purchase order that has simply not started: an
        order placed yesterday with nothing booked is normal, and reporting its
        whole quantity as a gap would train people to ignore the panel. A gap is
        only raised once the order has begun to be fulfilled.
        """
        gaps: list[Gap] = []
        started = self.assigned_qty > 0 or self.shipped_qty > 0 or bool(self.container_rows)
        if not started:
            return gaps

        if self.unbooked_qty > 0:
            gaps.append(
                Gap(
                    code="unbooked",
                    message=_("%(qty)s units are not on any supplier delivery yet.")
                    % {"qty": _quantity(self.unbooked_qty)},
                )
            )
        if self.unassigned_container_qty > 0:
            gaps.append(
                Gap(
                    code="no_container",
                    message=_("%(qty)s units are booked on a delivery but have no container number.")
                    % {"qty": _quantity(self.unassigned_container_qty)},
                )
            )
        without_shipment = len(self.containers_needing_shipment)
        if without_shipment:
            gaps.append(
                Gap(
                    code="no_shipment",
                    message=_("%(count)s containers are not on a shipment.") % {"count": without_shipment},
                )
            )
        if self.unshipped_qty > 0:
            gaps.append(
                Gap(
                    code="unshipped",
                    message=_("%(qty)s units have not shipped yet.") % {"qty": _quantity(self.unshipped_qty)},
                )
            )
        return gaps

    @property
    def has_gaps(self) -> bool:
        return bool(self.gaps)

    # -- order lines --------------------------------------------------------

    @property
    def total_order_amount(self):
        amounts = [line.line_amount for line in self.lines if line.line_amount is not None]
        return sum(amounts) if amounts else None


def _quantity(value: Decimal) -> str:
    """Render a quantity without its trailing zeros — 6, not 6.000."""
    normalised = value.normalize()
    # normalize() turns 100 into 1E+2; quantize back when the exponent went positive.
    if normalised.as_tuple().exponent > 0:
        normalised = normalised.quantize(Decimal(1))
    return f"{normalised:f}"


def get_purchase_order_workspace(team: Team, purchase_order: PurchaseOrder) -> PurchaseOrderWorkspace:
    """Gather everything the purchase order workspace renders, team-scoped throughout.

    The container rows are the expensive part and the reason this function exists:
    they are built from one delivery-line query plus one bulk container-workspace
    build, so the tracking, shipment and ETA of every container on the order cost a
    fixed number of queries rather than a query each.
    """
    from apps.scm.containers.workspace import get_container_workspaces
    from apps.scm.supplier_deliveries.models import SupplierDelivery, SupplierDeliveryLine

    from .selectors import get_purchase_order_events, get_purchase_order_lines
    from .services import calculate_purchase_order_fulfillment

    lines = list(get_purchase_order_lines(purchase_order=purchase_order))
    events = list(get_purchase_order_events(purchase_order=purchase_order))
    fulfillment = calculate_purchase_order_fulfillment(purchase_order=purchase_order)

    # Every delivery line on this order, once. It is the join that carries both the
    # article and the container, so the container rows and the delivery rows are
    # both built from it rather than from two overlapping queries.
    delivery_lines = list(
        SupplierDeliveryLine.objects.filter(
            team=team,
            purchase_order_line__purchase_order=purchase_order,
        ).select_related("delivery", "purchase_order_line", "container", "container__equipment_type")
    )

    assigned_qty = sum((line.delivery_qty for line in delivery_lines), _ZERO)
    unassigned_container_qty = sum(
        (line.delivery_qty for line in delivery_lines if line.container_id is None),
        _ZERO,
    )

    return PurchaseOrderWorkspace(
        purchase_order=purchase_order,
        lines=lines,
        events=events,
        container_rows=_container_rows(team, delivery_lines, get_container_workspaces),
        delivery_rows=_delivery_rows(team, purchase_order, delivery_lines, SupplierDelivery),
        fulfillment=fulfillment,
        assigned_qty=assigned_qty,
        unassigned_container_qty=unassigned_container_qty,
    )


def _container_rows(team: Team, delivery_lines, build_workspaces) -> list[ContainerRow]:
    """Turn this order's delivery lines into one row per distinct container.

    A container that appears on several delivery lines — two ordered articles in one
    box — is one row naming both articles, not two rows naming the same box.
    """
    containers: dict[int, object] = {}
    articles: dict[int, list[str]] = {}
    references: dict[int, list[str]] = {}

    for line in delivery_lines:
        if line.container_id is None:
            continue
        containers.setdefault(line.container_id, line.container)
        article = line.article or (line.purchase_order_line.item_no if line.purchase_order_line_id else "")
        if article and article not in articles.setdefault(line.container_id, []):
            articles[line.container_id].append(article)
        reference = line.delivery.delivery_reference
        if reference and reference not in references.setdefault(line.container_id, []):
            references[line.container_id].append(reference)

    if not containers:
        return []

    ordered = sorted(containers.values(), key=lambda container: (container.owner_code, container.serial_number))
    workspaces = build_workspaces(team, ordered)
    return [
        ContainerRow(
            container=container,
            workspace=workspaces.get(container.pk),
            articles=articles.get(container.pk, []),
            delivery_references=references.get(container.pk, []),
        )
        for container in ordered
    ]


def _delivery_rows(team: Team, purchase_order, delivery_lines, delivery_model) -> list[DeliveryRow]:
    """This order's supplier deliveries, each with what is booked on it.

    The quantities and container counts are folded out of ``delivery_lines``, which
    is already loaded, so the deliveries cost one query however many there are.
    Deliveries with no lines at all still appear — an empty delivery is a real
    planning state, and dropping it would hide a batch somebody created.
    """
    quantities: dict[int, Decimal] = {}
    containers: dict[int, set[int]] = {}
    counts: dict[int, int] = {}
    for line in delivery_lines:
        quantities[line.delivery_id] = quantities.get(line.delivery_id, _ZERO) + line.delivery_qty
        counts[line.delivery_id] = counts.get(line.delivery_id, 0) + 1
        if line.container_id is not None:
            containers.setdefault(line.delivery_id, set()).add(line.container_id)

    deliveries = delivery_model.objects.filter(team=team, purchase_order=purchase_order).order_by(
        "-planned_ship_date", "-created_at"
    )
    return [
        DeliveryRow(
            delivery=delivery,
            quantity=quantities.get(delivery.pk, _ZERO),
            container_count=len(containers.get(delivery.pk, ())),
            line_count=counts.get(delivery.pk, 0),
        )
        for delivery in deliveries
    ]


def get_purchase_order_line_summaries(workspace: PurchaseOrderWorkspace) -> list[dict]:
    """Per ordered line: how much of it is booked onto deliveries.

    The relationship section 10 of the brief asks to keep — which boxes belong to
    which purchased line — read from the delivery lines the workspace already
    loaded plus one aggregate. Business Central article codes describe equipment
    type, so this is what tells somebody that the CONT22G1 line is the one still
    missing containers.
    """
    from apps.scm.supplier_deliveries.models import SupplierDeliveryLine

    booked = {
        row["purchase_order_line_id"]: row["total"] or _ZERO
        for row in SupplierDeliveryLine.objects.filter(
            purchase_order_line__purchase_order=workspace.purchase_order,
        )
        .values("purchase_order_line_id")
        .annotate(total=Sum("delivery_qty"))
    }
    return [
        {
            "line": line,
            "booked_qty": booked.get(line.pk, _ZERO),
            "unbooked_qty": max(line.ordered_qty - booked.get(line.pk, _ZERO), _ZERO),
        }
        for line in workspace.lines
    ]
