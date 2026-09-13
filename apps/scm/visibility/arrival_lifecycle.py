"""The operational arrival lifecycle: expected, approaching, here, received.

One interpreter, read on demand, answering the five questions operations actually
ask about inbound freight:

.. code-block:: text

    What is expected to arrive?         EXPECTED
    What is approaching?                ARRIVING
    What has physically arrived?        ARRIVED
    What has been received?             RECEIVED
    What still requires action?         is_overdue / is_awaiting_receipt

**Nothing here is stored.** There is no ``arrival_status`` column, and nothing
writes one after a tracking event. The lifecycle is a deterministic reading of
evidence that already exists — the shipment's canonical destination, its ETA, and
the accepted :class:`~apps.scm.containers.models.ContainerMovement` history — so it
cannot drift out of step with ``shipment.status``, ``container.status``, the
tracking journey or ``current_location``. Those four keep answering their own
questions; this layer answers a fifth one over the top of them.

**ETA is evidence, not state.** An ETA of 14:00 and a clock reading 14:30 means
EXPECTED and overdue. It does not mean ARRIVED, and neither does a carrier's
``VESSEL_ARRIVED``: the ship reaching the port is not the box reaching the depot.
Only an accepted physical movement moves the lifecycle past ARRIVING, which is the
whole reason :mod:`apps.scm.tracking.physical_movements` is as conservative as it
is.

**Direction matters at the destination.** Arrival is evaluated against
``Shipment.destination_location`` and its subtree — the destination itself, or
somewhere inside it. A box gated into Oceanterminalen has arrived at the Göteborg
port that contains it; a box gated into Göteborg has *not* arrived at
Oceanterminalen, because the port contains terminals this shipment was not routed
to. The containment walk is LOC-1's, imported rather than restated.

**Arrival is an event, not a position.** ``arrived_at`` and ``received_at`` come
from the movement history and stay true afterwards. A box gated in, received, and
trucked out again is still RECEIVED for that inbound shipment; deriving the
lifecycle from ``current_location`` would send it back to EXPECTED the moment it
left, which is the bug this reads history to avoid.

**One shipment's arrival cannot be satisfied by another's.** See
:func:`arrival_cycle_start`: a gate-in that happened before this shipment's cycle
began is somebody else's arrival, and a movement recorded against another shipment
is that shipment's evidence however well the destination matches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING

from django.conf import settings
from django.db.models import TextChoices
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from apps.scm.containers.models import Container, ContainerLocation, ContainerMovement
    from apps.scm.shipments.models import Shipment
    from apps.teams.models import Team


class ArrivalState(TextChoices):
    """How far one inbound container has got towards its canonical destination.

    Four states, and deliberately no LATE and no CLOSED. "Late" is not a stage of
    arriving — it is a fact about the ETA, carried separately as
    :attr:`ArrivalLifecycle.is_overdue`, so that a delayed box keeps saying what it
    is actually doing. "Closed" would need a business event that objectively ends
    the inbound process, and the domain has none: a receipt is the last thing the
    platform can honestly observe.
    """

    EXPECTED = "expected", _("Expected")
    ARRIVING = "arriving", _("Arriving")
    ARRIVED = "arrived", _("Arrived")
    RECEIVED = "received", _("Received")


# How far advanced each state is. Used to roll several containers up into one answer
# for a shipment, where the *least* advanced container decides — see
# :attr:`ArrivalProgress.state`.
STATE_ORDER: dict[str, int] = {
    ArrivalState.EXPECTED: 0,
    ArrivalState.ARRIVING: 1,
    ArrivalState.ARRIVED: 2,
    ArrivalState.RECEIVED: 3,
}

# The states that mean the box is not here yet, and so are what the Expected
# Arrivals queue is about.
OUTSTANDING_STATES = frozenset({ArrivalState.EXPECTED, ArrivalState.ARRIVING})


def arrival_window_hours() -> int:
    """How close to its ETA something has to be to count as ARRIVING."""
    return int(settings.SCM_ARRIVAL_WINDOW_HOURS)


# ---------------------------------------------------------------------------
# The ETA rules, shared by the container and the shipment read models
#
# Kept as functions rather than duplicated properties so a shipment with no
# containers linked yet answers "expected or arriving?" by exactly the rule its
# containers would have used.
# ---------------------------------------------------------------------------


def is_in_arrival_window(eta: date | None, eta_at: datetime | None) -> bool:
    """True when arrival is due within the configured window, and not already past.

    The carrier's forecast is compared to the hour where there is one, because a
    slip from 06:00 to 22:00 is a working day and rounding it to a date would hide
    it. A shipment's own ETA is a date, and is compared as one.

    An ETA that has already passed is *not* in the window. It has stopped being an
    imminent arrival and become an overdue one — see :attr:`ArrivalLifecycle.is_overdue`.
    """
    now = timezone.now()
    horizon = now + timedelta(hours=arrival_window_hours())
    if eta_at is not None:
        return now <= eta_at <= horizon
    if eta is None:
        return False
    return timezone.localdate() <= eta <= timezone.localdate(horizon)


def is_eta_past(eta: date | None, eta_at: datetime | None) -> bool:
    """True when the expected arrival time has come and gone."""
    if eta_at is not None:
        return eta_at < timezone.now()
    return eta is not None and eta < timezone.localdate()


def arrival_cycle_start(shipment: Shipment | None) -> datetime | None:
    """The earliest moment a movement could belong to *shipment*'s arrival.

    The answer to LOC-3's central hazard: a depot that has handled the same box
    before has old gate-ins at the same destination, and none of them is evidence
    about a shipment booked months later. Movements before this instant are
    somebody else's arrival.

    The anchors, in order, are all facts the domain already records:

    ``actual_departure_at``
        The box cannot arrive before it left. The strongest bound there is, and the
        one to prefer whenever tracking or an operator has established it.
    ``etd``
        The planned departure, when the actual one is unknown. Taken as the start of
        that day in the active timezone, which is the widest reading of a date and
        so the one least likely to discard a real arrival.
    ``created_at``
        When the shipment became known to the platform. Not a departure, but a
        floor: a shipment recorded in September was not satisfied by an arrival in
        June.

    Returns None when there is no shipment, in which case there is no arrival cycle
    to bound — a container tracked on its own is not inbound to anywhere.

    Note what this is *not* used for: a movement explicitly linked to the shipment
    is accepted whatever its timestamp. Somebody recording ``related_shipment`` has
    stated the association directly, and a derived bound must not overrule it.
    """
    if shipment is None:
        return None
    if shipment.actual_departure_at is not None:
        return shipment.actual_departure_at
    if shipment.etd is not None:
        return timezone.make_aware(datetime.combine(shipment.etd, time.min))
    return shipment.created_at


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArrivalLifecycle:
    """One container's inbound progress towards one canonical destination.

    Frozen: it is an interpretation of the evidence at the moment it was read, and
    a caller that mutated it would be inventing a fact rather than recording one.
    """

    container: Container
    shipment: Shipment | None = None
    destination: ContainerLocation | None = None
    eta: date | None = None
    eta_at: datetime | None = None

    # The lower bound the movements below were selected against — see
    # :func:`arrival_cycle_start`. Kept so the answer can be interrogated: a reader
    # who expected an arrival to count can see which instant excluded it.
    cycle_start: datetime | None = None

    # When the box physically reached the destination, and when somebody took it
    # into their own records. Both from the movement history, and both permanent:
    # a later departure does not undo them.
    arrived_at: datetime | None = None
    received_at: datetime | None = None

    # The newest accepted movement belonging to this arrival cycle, for a row that
    # wants to say *why* it reads as it does.
    latest_relevant_movement: ContainerMovement | None = None

    @property
    def state(self) -> str:
        if self.received_at is not None:
            return ArrivalState.RECEIVED
        if self.arrived_at is not None:
            return ArrivalState.ARRIVED
        if is_in_arrival_window(self.eta, self.eta_at):
            return ArrivalState.ARRIVING
        return ArrivalState.EXPECTED

    @property
    def state_label(self) -> str:
        return str(ArrivalState(self.state).label)

    @property
    def has_arrived(self) -> bool:
        return self.arrived_at is not None

    @property
    def is_received(self) -> bool:
        return self.received_at is not None

    @property
    def is_outstanding(self) -> bool:
        return not self.has_arrived

    @property
    def is_awaiting_receipt(self) -> bool:
        """Physically here, and nobody has taken it into their records yet.

        An operational fact rather than an exception: without an SLA there is no
        defensible threshold at which waiting to be received becomes a problem, so
        this reports the state and leaves the judgment to the person reading it.
        """
        return self.has_arrived and not self.is_received

    @property
    def is_overdue(self) -> bool:
        """The ETA has passed and nothing has physically arrived.

        Not a state, and not a second delay engine either: this is one comparison
        between the current ETA and the movement history, with no grace period and
        no threshold of its own. Whether the *shipment* is delayed remains
        :mod:`apps.scm.tracking.delay_detection`'s answer.
        """
        return not self.has_arrived and is_eta_past(self.eta, self.eta_at)

    @property
    def is_evaluable(self) -> bool:
        """True when arrival can be detected at all.

        False means the shipment has no canonical destination, so there is nowhere
        for the box to have arrived *at*. The lifecycle still reads EXPECTED or
        ARRIVING from the ETA — that much is true — but it can never progress, and a
        caller about to say "not arrived" as though it were a finding should check
        this first.
        """
        return self.destination is not None


@dataclass(frozen=True)
class ArrivalProgress:
    """A shipment's inbound arrival, or a standalone container's, as one answer.

    A shipment of four boxes can be two received, one arrived and one still at sea,
    and that is not a single state. The counts are the honest summary; :attr:`state`
    exists for the places that can only show one badge, and is defined so that it
    can never overstate — see below.
    """

    lifecycles: list[ArrivalLifecycle] = field(default_factory=list)
    shipment: Shipment | None = None
    destination: ContainerLocation | None = None
    eta: date | None = None
    eta_at: datetime | None = None

    # -- counts ------------------------------------------------------------

    @property
    def total(self) -> int:
        return len(self.lifecycles)

    @property
    def arrived(self) -> int:
        return sum(1 for lifecycle in self.lifecycles if lifecycle.has_arrived)

    @property
    def received(self) -> int:
        return sum(1 for lifecycle in self.lifecycles if lifecycle.is_received)

    @property
    def outstanding(self) -> int:
        return self.total - self.arrived

    @property
    def awaiting_receipt(self) -> int:
        return sum(1 for lifecycle in self.lifecycles if lifecycle.is_awaiting_receipt)

    @property
    def arrived_percent(self) -> int:
        return self._percent(self.arrived)

    @property
    def received_percent(self) -> int:
        return self._percent(self.received)

    def _percent(self, count: int) -> int:
        """Derived on read, never stored: a percentage of a changing total.

        Rounded to whole numbers, because a decimal place would suggest the domain
        measures arrival more finely than one box at a time.
        """
        return round(count * 100 / self.total) if self.total else 0

    # -- state -------------------------------------------------------------

    @property
    def state(self) -> str:
        """The least advanced container's state, and nothing more optimistic.

        One box gated in must not make a shipment of twenty read as ARRIVED, so the
        roll-up is a minimum rather than a latest-event or an any-of. It reaches
        ARRIVED only when every container has, and RECEIVED only when every
        container has been received.

        A shipment with no containers linked yet answers from its ETA alone: the
        expectation is real even though there is nothing to have arrived.
        """
        if not self.lifecycles:
            return ArrivalState.ARRIVING if is_in_arrival_window(self.eta, self.eta_at) else ArrivalState.EXPECTED
        return min((lifecycle.state for lifecycle in self.lifecycles), key=lambda state: STATE_ORDER[state])

    @property
    def state_label(self) -> str:
        return str(ArrivalState(self.state).label)

    @property
    def has_arrived(self) -> bool:
        """True when every container has accepted arrival evidence at the destination.

        A shipment with no containers has not arrived. There is nothing to have
        arrived, and reporting an empty shipment as complete would take a real
        expectation off the queue.
        """
        return self.total > 0 and self.arrived == self.total

    @property
    def is_received(self) -> bool:
        return self.total > 0 and self.received == self.total

    @property
    def is_outstanding(self) -> bool:
        return not self.has_arrived

    @property
    def is_awaiting_receipt(self) -> bool:
        """Everything is here and something still has to be received."""
        return self.has_arrived and not self.is_received

    @property
    def is_overdue(self) -> bool:
        return not self.has_arrived and is_eta_past(self.eta, self.eta_at)

    @property
    def is_evaluable(self) -> bool:
        return self.destination is not None

    # -- times -------------------------------------------------------------

    @property
    def first_arrived_at(self) -> datetime | None:
        """When the first of these containers reached the destination."""
        times = [lifecycle.arrived_at for lifecycle in self.lifecycles if lifecycle.arrived_at]
        return min(times) if times else None

    @property
    def last_received_at(self) -> datetime | None:
        times = [lifecycle.received_at for lifecycle in self.lifecycles if lifecycle.received_at]
        return max(times) if times else None

    @property
    def latest_relevant_movement(self) -> ContainerMovement | None:
        movements = [
            lifecycle.latest_relevant_movement
            for lifecycle in self.lifecycles
            if lifecycle.latest_relevant_movement is not None
        ]
        return max(movements, key=lambda movement: movement.occurred_at) if movements else None

    @property
    def latest_relevant_movement_at(self) -> datetime | None:
        movement = self.latest_relevant_movement
        return movement.occurred_at if movement is not None else None


@dataclass(frozen=True)
class ArrivalQuestion:
    """One inbound arrival to interpret: these boxes, this booking, this ETA.

    The ETA is supplied rather than derived. The visibility read model and the
    container workspace already decide which of a shipment's date and a carrier's
    forecast to believe, and a second implementation of that choice here would
    eventually disagree with the page it is rendered on. The destination is *not*
    supplied — it is read from the shipment, because ``destination_location`` is the
    only thing arrival may be evaluated against.
    """

    containers: list[Container] = field(default_factory=list)
    shipment: Shipment | None = None
    eta: date | None = None
    eta_at: datetime | None = None


# ---------------------------------------------------------------------------
# The interpreter
# ---------------------------------------------------------------------------


def get_arrival_progress(team: Team, questions: Sequence[ArrivalQuestion]) -> list[ArrivalProgress]:
    """Interpret many inbound arrivals at once, in the order they were asked.

    A fixed number of queries whatever the number of questions: the destination
    subtrees are walked once per distinct destination, and every container's arrival
    evidence arrives in one query. The arrivals queue and the Control Tower both
    cover a whole team, so anything per object here would grow with the fleet.

    ``question.shipment`` should arrive with ``destination_location`` already
    selected — every caller in the visibility layer does, since it reads a
    destination for every row.
    """
    questions = list(questions)
    if not questions:
        return []

    destination_ids = {
        question.shipment.destination_location_id
        for question in questions
        if question.shipment is not None and question.shipment.destination_location_id is not None
    }
    subtrees = destination_subtrees(team, destination_ids)

    container_ids = {container.pk for question in questions for container in question.containers}
    location_ids: set[int] = set().union(*subtrees.values()) if subtrees else set()
    evidence = _arrival_evidence(team, container_ids, location_ids)

    return [_progress(question, subtrees, evidence) for question in questions]


def get_container_arrival_lifecycle(
    team: Team,
    container: Container,
    shipment: Shipment | None = None,
    *,
    eta: date | None = None,
    eta_at: datetime | None = None,
) -> ArrivalLifecycle:
    """Interpret one container's inbound arrival against *shipment*'s destination.

    The single-object entry point, for callers outside the visibility read models —
    the movement form prefilling a destination, a management command, a test. It
    runs the same interpreter as the queues, so a container cannot be ARRIVED on one
    page and EXPECTED on another.

    ``eta`` defaults to the shipment's own, which is the planned date the business
    works to. A caller holding a carrier forecast should pass it.
    """
    if eta is None and shipment is not None:
        eta = shipment.eta
    question = ArrivalQuestion(containers=[container], shipment=shipment, eta=eta, eta_at=eta_at)
    progress = get_arrival_progress(team, [question])[0]
    return progress.lifecycles[0]


def get_shipment_arrival_progress(team: Team, shipment: Shipment, containers: Iterable[Container] | None = None):
    """Interpret one shipment's arrival across all of its containers.

    ``containers`` lets a caller that has already loaded them avoid the query.
    """
    if containers is None:
        from apps.scm.containers.models import Container
        from apps.scm.shipments.models import ShipmentContainer

        container_ids = ShipmentContainer.objects.filter(shipment=shipment, shipment__team=team).values_list(
            "container_id", flat=True
        )
        containers = Container.objects.filter(team=team, pk__in=container_ids)
    question = ArrivalQuestion(containers=list(containers), shipment=shipment, eta=shipment.eta)
    return get_arrival_progress(team, [question])[0]


def destination_subtrees(team: Team, location_ids: Iterable[int]) -> dict[int, set[int]]:
    """Each of *location_ids* together with everything inside it, as ids.

    Team-scoped by the lookup itself, so an id belonging to another tenant is absent
    from the result rather than resolving to that tenant's subtree — arrival is then
    simply not detectable for it, which is the safe failure.

    The containment walk is :func:`~apps.scm.containers.selectors.get_location_subtree_ids`,
    imported rather than reimplemented: the arrivals filter, the location workspace
    and this interpreter must agree exactly about what is inside Göteborg.
    """
    from apps.scm.containers.models import ContainerLocation
    from apps.scm.containers.selectors import get_location_subtree_ids

    wanted = {location_id for location_id in location_ids if location_id is not None}
    if not wanted:
        return {}
    locations = ContainerLocation.objects.filter(team=team, pk__in=wanted)
    return {location.pk: set(get_location_subtree_ids(team=team, location=location)) for location in locations}


def destination_subtree_ids(team: Team, location_id: int) -> set[int]:
    """One destination's subtree, as ids. Empty when the id is not this team's."""
    return destination_subtrees(team, [location_id]).get(location_id, set())


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _arrival_evidence(team: Team, container_ids, location_ids) -> dict[int, list[ContainerMovement]]:
    from apps.scm.containers.movements import arrival_movements

    return arrival_movements(team, container_ids, location_ids)


def _progress(
    question: ArrivalQuestion,
    subtrees: dict[int, set[int]],
    evidence: dict[int, list[ContainerMovement]],
) -> ArrivalProgress:
    shipment = question.shipment
    destination = shipment.destination_location if shipment is not None else None
    wanted = subtrees.get(destination.pk, set()) if destination is not None else set()
    cycle_start = arrival_cycle_start(shipment)

    lifecycles = [
        _lifecycle(
            container=container,
            question=question,
            destination=destination,
            cycle_start=cycle_start,
            movements=_relevant_movements(
                evidence.get(container.pk, []),
                shipment=shipment,
                location_ids=wanted,
                cycle_start=cycle_start,
            ),
        )
        for container in question.containers
    ]
    return ArrivalProgress(
        lifecycles=lifecycles,
        shipment=shipment,
        destination=destination,
        eta=question.eta,
        eta_at=question.eta_at,
    )


def _lifecycle(
    *,
    container: Container,
    question: ArrivalQuestion,
    destination: ContainerLocation | None,
    cycle_start: datetime | None,
    movements: list[ContainerMovement],
) -> ArrivalLifecycle:
    from apps.scm.containers.choices import MovementType

    # Oldest first, so the first qualifying movement is when the box got here rather
    # than the last time somebody touched it.
    receipts = [movement for movement in movements if movement.movement_type == MovementType.RECEIVED]
    return ArrivalLifecycle(
        container=container,
        shipment=question.shipment,
        destination=destination,
        eta=question.eta,
        eta_at=question.eta_at,
        cycle_start=cycle_start,
        arrived_at=movements[0].occurred_at if movements else None,
        received_at=receipts[0].occurred_at if receipts else None,
        latest_relevant_movement=movements[-1] if movements else None,
    )


def _relevant_movements(
    movements: list[ContainerMovement],
    *,
    shipment: Shipment | None,
    location_ids: set[int],
    cycle_start: datetime | None,
) -> list[ContainerMovement]:
    """The arrival evidence that belongs to *this* arrival cycle, oldest first.

    Three rules, in precedence order, and each of them is about not borrowing
    somebody else's arrival:

    1. **A movement explicitly linked to this shipment counts, whenever it
       happened.** Somebody recorded the association; a derived time bound must not
       overrule a stated fact.
    2. **A movement linked to a different shipment never counts.** Two shipments to
       Oceanterminalen a month apart are two arrivals, and the linkage is what tells
       them apart.
    3. **An unlinked movement counts only from the cycle start on.** This is the
       ordinary case — most movements name no shipment — and the bound is what stops
       a depot's history of the same box satisfying a new booking. See
       :func:`arrival_cycle_start`.

    Movements outside the destination subtree are excluded before any of that: a
    receipt at the wrong place is not this arrival at all.
    """
    if not location_ids:
        # No canonical destination, or one belonging to another team. There is
        # nowhere to have arrived, so no movement can be evidence of arriving there.
        return []

    relevant = []
    for movement in movements:
        if movement.to_location_id not in location_ids:
            continue
        if movement.related_shipment_id is not None:
            if shipment is not None and movement.related_shipment_id == shipment.pk:
                relevant.append(movement)
            continue
        if cycle_start is not None and movement.occurred_at < cycle_start:
            continue
        relevant.append(movement)
    return relevant
