"""Physical container state: what moved, what we accepted, and where the box is.

This module owns one invariant and everything needed to keep it:

.. code-block:: text

    Container.current_location
        = the location implied by the winning state-affecting ContainerMovement

``current_location`` is therefore a *projection*, not a field anybody sets. Views,
importers, carrier adapters and tracking ingestion all call
:func:`record_container_movement`; none of them decides state, and none of them
writes the column. That is the whole point: a carrier saying "DISCHARGED —
GOTHENBURG" reaches the database as evidence, and only an explicit domain decision
turns evidence into position.

**Time leads, provenance breaks ties.** Movements are ordered by ``occurred_at`` —
when the thing happened — never by ``created_at`` or by insertion order. Tracking
APIs deliver old events late, and a carrier event from 09:30 that arrives at 15:10
must not undo a gate-in an operator recorded at 14:32. At an identical
``occurred_at``, the more direct claim wins: somebody who saw the box outranks a
carrier who inferred it. See :func:`state_sort_key` for the exact total order.

**Recording is not accepting.** A movement can be worth keeping and still not be
where the box is: late carrier evidence stays in the history, visible on the
Activity tab, without moving anything. ``affects_current_state=False`` goes
further and keeps a row out of the projection entirely.

**No reverse inference.** A container whose ``current_location`` predates any
movement history keeps it. Nothing here invents the movements that would have
produced it — a fabricated history is worse than an honest gap, and the projection
simply leaves the column alone until a real movement arrives.

The from/to semantics of each movement type are defined here and nowhere else:

.. code-block:: text

    GATE_IN     to_location required     → current = to_location
    RECEIVED    to_location required     → current = to_location
    TRANSFER    from ≠ to, both required → current = to_location
    GATE_OUT    from_location required   → current = to_location (None when unknown)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .choices import DEFAULT_EVIDENCE_STRENGTH, EVIDENCE_STRENGTH_BY_SOURCE, LocationSource, MovementType
from .models import Container, ContainerLocation, ContainerMovement

if TYPE_CHECKING:
    from datetime import datetime

    from apps.scm.tracking.models import TrackingEvent
    from apps.teams.models import Team

# Movement types that assert the container is now somewhere, and therefore need a
# destination. A GATE_IN with nothing to be in is not an incomplete record, it is a
# contradictory one.
_REQUIRES_DESTINATION = frozenset(
    {
        MovementType.GATE_IN,
        MovementType.RECEIVED,
        MovementType.TRANSFER,
    }
)

# Movement types that assert the container left somewhere, and therefore need an
# origin — supplied, or safely derived from where we already believe it is.
_REQUIRES_ORIGIN = frozenset(
    {
        MovementType.GATE_OUT,
        MovementType.TRANSFER,
        MovementType.DEPARTED_DEPOT,
    }
)

# The four operational movements an authorised user performs by hand. Offered by the
# movement form; the rest of MovementType is history or machinery.
OPERATIONAL_MOVEMENT_TYPES = (
    MovementType.GATE_IN,
    MovementType.GATE_OUT,
    MovementType.RECEIVED,
    MovementType.TRANSFER,
)

# What counts as having arrived at a destination, for the arrival lifecycle. Narrow
# on purpose: a gate-in and a receipt are somebody saying the box is here, which is
# the fact that stops it being merely expected. A transfer into the yard from
# elsewhere in the same port is a move, not an arrival at the destination.
#
# LOC-3 reconsidered widening this to ARRIVED_AT_DEPOT and left it alone.
# ARRIVED_AT_DEPOT is a legacy value written before canonical locations existed, by
# importers whose idea of "depot" was a free-text place; reading those rows as
# arrivals at a canonical destination would reinterpret history the platform never
# recorded that precisely. Nothing produces the value now, and the two types below
# are what the operational movements actually emit.
ARRIVAL_MOVEMENT_TYPES = (
    MovementType.GATE_IN,
    MovementType.RECEIVED,
)


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------


def requires_destination(movement_type: str) -> bool:
    """True when this kind of movement is meaningless without a ``to_location``."""
    return movement_type in _REQUIRES_DESTINATION


def requires_origin(movement_type: str) -> bool:
    """True when this kind of movement is meaningless without a ``from_location``.

    The origin may still be filled in from the container's current position rather
    than supplied — see :func:`record_container_movement`.
    """
    return movement_type in _REQUIRES_ORIGIN


def state_sort_key(movement: ContainerMovement) -> tuple:
    """The total order that decides which movement is the container's state.

    Descending on every component; the winner is the maximum. Four components, each
    load-bearing:

    ``occurred_at``
        When the move physically happened. The only thing that should decide
        position. A carrier event received at 15:10 for something that happened at
        09:30 sorts to 09:30, so it cannot undo a 14:32 gate-in.

    evidence strength
        Breaks a tie at the same instant, so a carrier's version of a moment an
        operator also recorded loses to the operator. See
        :class:`~apps.scm.containers.choices.EvidenceStrength`.

    ``created_at``, ``pk``
        The documented tie-breaker of last resort: same instant, same provenance.
        Later record wins, and ``pk`` makes it deterministic when even ``created_at``
        matches — two rows written inside the same microsecond must still produce one
        stable answer rather than depend on the order the database returns them.

    Note what is *not* here: insertion order alone never decides anything, because
    it only ever reads after ``occurred_at`` and strength have both tied.
    """
    return (
        movement.occurred_at,
        EVIDENCE_STRENGTH_BY_SOURCE.get(movement.source, DEFAULT_EVIDENCE_STRENGTH),
        movement.created_at,
        movement.pk or 0,
    )


def _strength_annotation():
    """``state_sort_key``'s strength component, expressed for the database.

    Kept as a CASE rather than a stored column: provenance ranking is a domain rule
    that may be revised, and a denormalised rank would have to be backfilled every
    time it was. Built from the same table the Python side reads, so the two
    orderings cannot drift.
    """
    return Case(
        *[When(source=source, then=Value(int(strength))) for source, strength in EVIDENCE_STRENGTH_BY_SOURCE.items()],
        default=Value(int(DEFAULT_EVIDENCE_STRENGTH)),
        output_field=IntegerField(),
    )


def get_state_movements(team: Team, container: Container):
    """This container's movements that are claims about where it is, newest first.

    Ordered by :func:`state_sort_key`'s rule, in the database. The first row is the
    container's state.
    """
    return (
        ContainerMovement.objects.filter(team=team, container=container, affects_current_state=True)
        .annotate(evidence_strength=_strength_annotation())
        .select_related("from_location", "to_location")
        .order_by("-occurred_at", "-evidence_strength", "-created_at", "-pk")
    )


def get_current_state_movement(team: Team, container: Container) -> ContainerMovement | None:
    """The movement that ``Container.current_location`` reflects, or None.

    None means no movement has ever claimed a position for this container. The
    column may still hold a location — a legacy row, or an import that predates the
    movement history — and that is not a fault to repair. See the module docstring.
    """
    return get_state_movements(team, container).first()


def current_state_movements(team: Team, container_ids) -> dict[int, ContainerMovement]:
    """The winning state movement for many containers at once, keyed by container id.

    The bulk form of :func:`get_current_state_movement`, for the fleet-wide reads —
    the operational map, chiefly — where following one query per container would
    make the page cost grow with the number of boxes on it.

    The ordering is :func:`get_state_movements`' own, restated only in the column
    list ``DISTINCT ON`` needs; the precedence rule itself is
    :func:`state_sort_key`'s and is not duplicated. Both paths therefore name the
    same movement, which matters because this one explains what
    ``Container.current_location`` says: a caller that picked a different movement
    would print a source and a time that did not belong to the position beside them.
    """
    container_ids = list(container_ids)
    if not container_ids:
        return {}
    rows = (
        ContainerMovement.objects.filter(team=team, container_id__in=container_ids, affects_current_state=True)
        .annotate(evidence_strength=_strength_annotation())
        .select_related("to_location", "to_location__parent_location")
        .order_by("container_id", "-occurred_at", "-evidence_strength", "-created_at", "-pk")
        .distinct("container_id")
    )
    return {row.container_id: row for row in rows}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_tenancy(team: Team, container: Container, locations) -> None:
    """Every object in a movement must belong to one team.

    Checked on ids rather than by following the FKs, so a cross-tenant reference is
    rejected without loading the other tenant's row.
    """
    if container.team_id != team.pk:
        raise ValidationError({"container": _("The container belongs to a different team.")})
    for field, location in locations.items():
        if location is not None and location.team_id != team.pk:
            raise ValidationError({field: _("The location belongs to a different team.")})


def _validate_movement(
    *,
    movement_type: str,
    from_location: ContainerLocation | None,
    to_location: ContainerLocation | None,
) -> None:
    """Reject movements that cannot describe anything that happened.

    Strict here and deliberately not strict on :class:`TrackingEvent`. A carrier is
    allowed to send incomplete evidence and we store all of it; a movement is
    something we *accepted*, and accepting a self-contradictory one would put a
    wrong answer into ``current_location`` with our name on it.
    """
    if movement_type not in MovementType.values:
        raise ValidationError({"movement_type": _("Unknown movement type.")})

    if movement_type in _REQUIRES_DESTINATION and to_location is None:
        raise ValidationError(
            {"to_location": _("A %(movement)s needs a destination.") % {"movement": _label(movement_type)}}
        )

    if movement_type in _REQUIRES_ORIGIN and from_location is None:
        raise ValidationError(
            {
                "from_location": _(
                    "A %(movement)s needs an origin, and the container has no recorded location to take one from."
                )
                % {"movement": _label(movement_type)}
            }
        )

    if (
        movement_type == MovementType.TRANSFER
        and from_location is not None
        and to_location is not None
        and from_location.pk == to_location.pk
    ):
        raise ValidationError({"to_location": _("A transfer must move the container between two different places.")})


def _label(movement_type: str) -> str:
    return str(MovementType(movement_type).label)


# ---------------------------------------------------------------------------
# The state transition service
# ---------------------------------------------------------------------------


def record_container_movement(
    *,
    team: Team,
    container: Container,
    movement_type: str,
    to_location: ContainerLocation | None = None,
    from_location: ContainerLocation | None = None,
    occurred_at: datetime | None = None,
    source: str = LocationSource.MANUAL,
    gate_name: str = "",
    notes: str = "",
    related_tracking_event: TrackingEvent | None = None,
    related_shipment=None,
    related_supplier_delivery=None,
    affects_current_state: bool = True,
) -> ContainerMovement:
    """Record one accepted physical movement and re-project the container's state.

    The single path for changing where a container is. It validates tenancy and the
    movement's own coherence, stores the row, and then recomputes
    ``current_location`` from the whole history rather than from this movement
    alone — which is what makes a late-arriving carrier event harmless. Storing an
    old movement simply loses the ordering; it never overwrites a newer one.

    ``from_location`` defaults to where the container is currently believed to be,
    so "gate this box out" does not make the caller restate what the system already
    knows. It is *the last place we knew about*, not a claim that the box travelled
    directly from there — which is also why a caller recording a backdated movement
    should name the origin explicitly rather than let today's position stand in for
    the position at the time.

    The container row is locked for the duration. Two workers processing tracking
    for the same box therefore serialise here, and the second one re-projects over a
    history that already contains the first one's movement — so the winner is
    decided by ``occurred_at``, never by which worker got there first.
    """
    occurred_at = occurred_at or timezone.now()

    with transaction.atomic():
        # Re-read under the lock: the caller's instance may be stale, and the
        # projection below has to see the row it is about to write. `of=("self",)`
        # narrows the lock to the container. Without it the joined location would be
        # locked too — which Postgres refuses outright for a nullable join, and which
        # would serialise every container at a depot behind whichever one moved.
        locked = (
            Container.objects.select_for_update(of=("self",)).select_related("current_location").get(pk=container.pk)
        )

        if from_location is None:
            from_location = locked.current_location

        _validate_tenancy(team, locked, {"from_location": from_location, "to_location": to_location})
        _validate_movement(movement_type=movement_type, from_location=from_location, to_location=to_location)

        movement = ContainerMovement.objects.create(
            team=team,
            container=locked,
            from_location=from_location,
            to_location=to_location,
            movement_type=movement_type,
            occurred_at=occurred_at,
            source=source,
            gate_name=gate_name,
            notes=notes,
            related_tracking_event=related_tracking_event,
            related_shipment=related_shipment,
            related_supplier_delivery=related_supplier_delivery,
            affects_current_state=affects_current_state,
        )
        project_container_state(team=team, container=locked)

    # The caller's instance predates the projection; refresh it so a view rendering
    # straight after this call does not show the location the box used to be at.
    container.refresh_from_db()
    return movement


def project_container_state(team: Team, container: Container) -> ContainerMovement | None:
    """Recompute ``current_location`` from the movement history. Returns the winner.

    Idempotent and safe to re-run: it is a pure function of the movements, so
    replaying it after a backfill, a correction or a deleted row produces the same
    answer as the writes that led there.

    A container with no state-affecting movements is left exactly as it is. That is
    not an omission — see "No reverse inference" in the module docstring.
    """
    winner = get_current_state_movement(team, container)
    if winner is None:
        return None

    changed = []
    if container.current_location_id != winner.resolved_location_id:
        container.current_location_id = winner.resolved_location_id
        changed.append("current_location")
    if container.location_source != winner.source:
        container.location_source = winner.source
        changed.append("location_source")
    if container.last_location_update != winner.occurred_at:
        container.last_location_update = winner.occurred_at
        changed.append("last_location_update")

    if changed:
        # `Container.save` runs `full_clean`, so `update_fields` is about which
        # columns are written, not about skipping validation.
        container.save(update_fields=changed)
    return winner


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def arrival_movements(team: Team, container_ids, location_ids) -> dict[int, list[ContainerMovement]]:
    """Each container's accepted arrival evidence at any of *location_ids*, oldest first.

    What the arrival lifecycle reads, and deliberately not ``current_location ==
    destination``. A box that was gated in at Oceanterminalen last Tuesday and has
    since been trucked onward has arrived — it is not still expected — and a box a
    carrier merely reports near Gothenburg has not, however suggestive the event.
    Only :data:`ARRIVAL_MOVEMENT_TYPES` count, and only movements that are claims
    about position.

    The movements themselves rather than a set of location ids, because the
    lifecycle has to say *when* the box arrived and which movement said so, and
    ordered oldest first so the first row is the arrival rather than the latest
    handling of it. ``related_shipment`` travels with them: which shipment's arrival
    a movement is evidence of is the lifecycle's hardest question, and it is decided
    from that column — see
    :func:`~apps.scm.visibility.arrival_lifecycle._relevant_movements`.

    Keyed by container rather than flattened, because each caller compares against
    its own destination: an arrival at Gothenburg must not satisfy an expectation at
    Hamburg.

    One query for any number of containers and destinations.
    """
    container_ids = list(container_ids)
    location_ids = list(location_ids)
    if not container_ids or not location_ids:
        return {}

    rows = (
        ContainerMovement.objects.filter(
            team=team,
            container_id__in=container_ids,
            to_location_id__in=location_ids,
            movement_type__in=ARRIVAL_MOVEMENT_TYPES,
            affects_current_state=True,
        )
        .select_related("to_location")
        # `pk` breaks a tie at an identical instant, so a container with two
        # movements written in the same microsecond still produces one stable answer.
        .order_by("occurred_at", "pk")
    )

    arrivals: dict[int, list[ContainerMovement]] = {}
    for movement in rows:
        arrivals.setdefault(movement.container_id, []).append(movement)
    return arrivals


def get_container_movements(team: Team, container: Container, limit: int | None = None):
    """This container's movement history, newest first, whether or not it counts.

    Everything, including movements that lost the state and movements flagged as
    history only. The audit trail's job is to explain why ``current_location`` is
    what it is, which needs the rows that did not win as much as the one that did.
    """
    queryset = (
        ContainerMovement.objects.filter(team=team, container=container)
        .select_related("from_location", "to_location", "related_tracking_event")
        .order_by("-occurred_at", "-created_at")
    )
    return queryset[:limit] if limit else queryset


def has_movement_for_event(team: Team, event) -> bool:
    """True when *event* has already been interpreted into a movement."""
    return ContainerMovement.objects.filter(team=team, related_tracking_event=event).exists()


def location_activity(team: Team, location: ContainerLocation, limit: int = 25) -> list:
    """Movements into and out of *location*, newest first.

    Both directions: a location's activity is what came and what went, and the row
    itself says which. ``Q`` rather than two queries so the limit applies to the
    combined stream — the twenty-five most recent things that happened here, not the
    twenty-five most recent of each.
    """
    return list(
        ContainerMovement.objects.filter(team=team)
        .filter(Q(to_location=location) | Q(from_location=location))
        .select_related("container", "container__equipment_type", "from_location", "to_location")
        .order_by("-occurred_at", "-created_at")[:limit]
    )
