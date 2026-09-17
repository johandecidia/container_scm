"""Linking procurement lines to physical containers: the write API and the reads.

Two relationships, never conflated. A container can be *what was bought* or *what
the bought goods travel in*, and an operator looking at a box needs to know which:
"this is one of the twenty 45ft boxes on line 10000" and "this box has 300 of line
20000's doors in it" are different answers, and a single generic link would make
them indistinguishable. The two models this module writes —
:class:`~apps.scm.procurement.models.ContainerAcquisition` and
:class:`~apps.scm.procurement.models.ContainerLoad` — keep them apart at the
schema level, so nothing downstream has to guess.

Every write goes through a function here rather than through the ORM directly. The
rules — team integrity, one procurement origin per box, re-linking as a no-op — are
the same whether a person clicks the button or the Business Central import replays a
feed, and the import is the next caller. A second implementation of "is this box
already acquired?" is the failure this module exists to prevent.

The reads are deliberately here too, beside the writes they mirror: one function per
direction, each returning a small frozen read model the workspaces hold. The
templates then render fields instead of deciding anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.scm.containers.models import Container
from apps.teams.models import Team

from .models import ContainerAcquisition, ContainerLoad, PurchaseOrder, PurchaseOrderLine

# Containers are listed in ISO-number order wherever a list of them is shown, so the
# same box sits in the same place on the purchase order and on the container page.
_CONTAINER_ORDER = ("container__owner_code", "container__category_id", "container__serial_number")


def _assert_team(team: Team, *records) -> None:
    """Refuse a link whose ends do not all belong to ``team``.

    The models check this too, on save. Checking here as well means a cross-team
    attempt is refused before anything is written and before the caller has to
    interpret a model-level error — and it also covers the delete paths, where no
    save happens at all.
    """
    for record in records:
        if record is not None and record.team_id != team.pk:
            raise ValidationError(_("That record belongs to another team."))


# ---------------------------------------------------------------------------
# Acquisition — "this PO line acquired this box"
# ---------------------------------------------------------------------------


def link_acquired_container(
    *,
    team: Team,
    purchase_order_line: PurchaseOrderLine,
    container: Container,
) -> ContainerAcquisition:
    """Record that ``container`` was acquired through ``purchase_order_line``.

    Idempotent: linking a box to the line that already acquired it returns the
    existing row untouched, so replaying an import or double-clicking a button
    cannot produce a duplicate.

    Raises:
        ValidationError: if either end belongs to another team, or if the container
            already has a *different* procurement origin. A box came from one place;
            silently moving it to a second line would erase the first answer.
    """
    _assert_team(team, purchase_order_line, container)

    existing = (
        ContainerAcquisition.objects.filter(container=container)
        .select_related("purchase_order_line", "purchase_order_line__purchase_order")
        .first()
    )
    if existing is not None:
        if existing.purchase_order_line_id == purchase_order_line.pk:
            return existing
        raise ValidationError(
            _("%(container)s was already acquired through %(order)s line %(line)s. Unlink it there first.")
            % {
                "container": container.container_id,
                "order": existing.purchase_order_line.purchase_order.po_number,
                "line": existing.purchase_order_line.line_no,
            }
        )

    with transaction.atomic():
        return ContainerAcquisition.objects.create(
            team=team,
            purchase_order_line=purchase_order_line,
            container=container,
        )


def unlink_acquired_container(*, team: Team, acquisition: ContainerAcquisition) -> None:
    """Drop an acquisition link. The container and the purchase order line both survive."""
    _assert_team(team, acquisition)
    with transaction.atomic():
        acquisition.delete()


# ---------------------------------------------------------------------------
# Load — "goods from this PO line travel in this box"
# ---------------------------------------------------------------------------


def set_container_load(
    *,
    team: Team,
    purchase_order_line: PurchaseOrderLine,
    container: Container,
    quantity: Decimal | None = None,
) -> ContainerLoad:
    """Record that goods from ``purchase_order_line`` are loaded in ``container``.

    An upsert on the (line, container) pair, which is why it is ``set`` and not
    ``add``: calling it again for the same pair updates the quantity rather than
    creating a second row, and calling it again with the same quantity changes
    nothing. ``quantity`` is written as given, including ``None`` — that is how a
    recorded quantity is cleared back to "not known".

    Raises:
        ValidationError: if either end belongs to another team, or the quantity is
            negative.
    """
    _assert_team(team, purchase_order_line, container)

    with transaction.atomic():
        load, _created = ContainerLoad.objects.update_or_create(
            purchase_order_line=purchase_order_line,
            container=container,
            defaults={"team": team, "quantity": quantity},
        )
    return load


def remove_container_load(*, team: Team, load: ContainerLoad) -> None:
    """Drop a load link. The container and the purchase order line both survive."""
    _assert_team(team, load)
    with transaction.atomic():
        load.delete()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContainerProcurement:
    """What procurement knows about one physical container.

    Held by the container workspace and rendered by its Procurement section. Both
    halves are absent rather than empty-and-meaningful for a box nobody has linked:
    :attr:`has_any` is what the template asks before drawing a panel.
    """

    acquisition: ContainerAcquisition | None = None
    loads: list[ContainerLoad] = field(default_factory=list)

    @property
    def has_any(self) -> bool:
        return self.acquisition is not None or bool(self.loads)

    @property
    def acquired_line(self) -> PurchaseOrderLine | None:
        return self.acquisition.purchase_order_line if self.acquisition is not None else None

    @property
    def acquired_order(self) -> PurchaseOrder | None:
        line = self.acquired_line
        return line.purchase_order if line is not None else None


@dataclass(frozen=True)
class LineContainerLinks:
    """The containers linked to one purchase order line, split by what the link means."""

    acquired: list[ContainerAcquisition] = field(default_factory=list)
    loads: list[ContainerLoad] = field(default_factory=list)

    @property
    def has_any(self) -> bool:
        return bool(self.acquired) or bool(self.loads)


_EMPTY_LINKS = LineContainerLinks()


def get_container_procurement(team: Team, container: Container) -> ContainerProcurement:
    """Both procurement answers for one container, in two queries.

    Team-scoped on the links themselves, not only through the container, so a link
    row that somehow named another team's container cannot surface here.
    """
    acquisition = (
        ContainerAcquisition.objects.filter(team=team, container=container)
        .select_related("purchase_order_line", "purchase_order_line__purchase_order")
        .first()
    )
    loads = list(
        ContainerLoad.objects.filter(team=team, container=container)
        .select_related("purchase_order_line", "purchase_order_line__purchase_order")
        .order_by("purchase_order_line__purchase_order__po_number", "purchase_order_line__line_no")
    )
    return ContainerProcurement(acquisition=acquisition, loads=loads)


def get_container_links_by_line(purchase_order: PurchaseOrder) -> dict[int, LineContainerLinks]:
    """Every container link on one purchase order, keyed by purchase order line id.

    Two queries for the whole order however many lines it has, so the workspace's
    per-line rendering costs nothing per row. Lines with no links are absent from
    the mapping; :func:`get_line_container_links` is what callers use to read it, and
    returns an empty read model for those.
    """
    acquired: dict[int, list[ContainerAcquisition]] = {}
    for acquisition in (
        ContainerAcquisition.objects.filter(purchase_order_line__purchase_order=purchase_order)
        .select_related("container", "container__equipment_type")
        .order_by(*_CONTAINER_ORDER)
    ):
        acquired.setdefault(acquisition.purchase_order_line_id, []).append(acquisition)

    loads: dict[int, list[ContainerLoad]] = {}
    for load in (
        ContainerLoad.objects.filter(purchase_order_line__purchase_order=purchase_order)
        .select_related("container", "container__equipment_type")
        .order_by(*_CONTAINER_ORDER)
    ):
        loads.setdefault(load.purchase_order_line_id, []).append(load)

    return {
        line_id: LineContainerLinks(acquired=acquired.get(line_id, []), loads=loads.get(line_id, []))
        for line_id in set(acquired) | set(loads)
    }


def get_line_container_links(links_by_line: dict[int, LineContainerLinks], line_id: int) -> LineContainerLinks:
    """One line's links out of the mapping, empty rather than missing."""
    return links_by_line.get(line_id, _EMPTY_LINKS)
