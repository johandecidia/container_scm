"""What a map is allowed to draw, and what each point actually claims.

LOC-4's read model. It owns no facts. Every position here is a place the domain has
already established, re-expressed as something a map can render — and the reason it
exists is that three different statements about location look identical once they
are dots on a tile, which is how an operator comes to believe a container is
somewhere it has never been.

The three, kept strictly apart:

``PHYSICAL``
    ``Container.current_location``: the projection of the accepted movement history
    (LOC-2). The strongest thing the platform says about where a box is.
``TRACKING``
    The newest observed carrier event that resolved to one of MCR's own canonical
    locations (LOC-1). External evidence, labelled as evidence.
``DESTINATION``
    ``Shipment.destination_location``. Where something is *going*. Not a position,
    never counted as one, and drawn only when somebody asks for it.

**Precedence is decided here and nowhere else.** PHYSICAL, then TRACKING, then no
marker at all. A destination is never promoted into a current position: a container
whose carrier has gone quiet is somewhere we do not know, and drawing it on its
destination would assert the arrival the whole of LOC-3 exists to be careful about.
The browser receives the answer, never the rule — see
:mod:`apps.scm.visibility.geojson`.

**Nothing is recalculated.** The physical position is read off the projection, the
arrival state off the LOC-3 interpreter, the canonical identity off LOC-1's
resolver. A second implementation of any of them here would eventually disagree
with the page the map is drawn on.

**Coordinates come from the canonical location, and are never invented.** A
location with no latitude and longitude is a valid canonical place that cannot be
drawn yet — not an error, and not an invitation to geocode its name.
:class:`MapCoverage` reports how much of the fleet that accounts for, because an
empty map with no explanation is indistinguishable from an empty business.

**A canonical coordinate locates the place, not the box inside it.**
Oceanterminalen is one point; the eighty containers standing in it are *at that
terminal*, not at that latitude. Positions are therefore grouped by canonical
location and every label says "at" the place rather than quoting a fix. Scattering
them apart with jitter would draw a precision the data does not have.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from django.db.models import TextChoices
from django.utils.translation import gettext_lazy as _

from .arrival_lifecycle import OUTSTANDING_STATES, ArrivalState

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import datetime
    from decimal import Decimal

    from apps.scm.containers.models import Container, ContainerLocation
    from apps.scm.shipments.models import Shipment
    from apps.teams.models import Team

    from .read_models import VisibilityObject


class PositionClass(TextChoices):
    """What kind of claim a plotted point is making.

    ``UNKNOWN`` exists for counting, not for drawing. A container the platform
    cannot place is a real and reportable state — see
    :attr:`MapCoverage.unplottable_containers` — but it has no coordinates, so it
    never becomes a marker.
    """

    PHYSICAL = "physical", _("Physical location")
    TRACKING = "tracking", _("Tracking position")
    DESTINATION = "destination", _("Destination")
    UNKNOWN = "unknown", _("No plottable position")


# The classes that answer "where is it now". DESTINATION is deliberately not one of
# them, and this tuple is what every count of current positions is built from.
CURRENT_POSITION_CLASSES: tuple[str, ...] = (PositionClass.PHYSICAL, PositionClass.TRACKING)


@dataclass(frozen=True)
class MapPosition:
    """One container, one place, and what kind of statement that is.

    Frozen: it is an interpretation of the evidence at the moment it was read, and a
    caller that mutated one would be inventing a position rather than reporting one.

    ``occurred_at`` is the freshness of *this* claim — when the movement was
    accepted, or when the carrier observed the event. It is not when the platform
    last looked, which is a different fact and lives on the tracking panel.
    """

    container: Container
    position_class: str
    location: ContainerLocation | None = None
    occurred_at: datetime | None = None

    # Who says so, and what they said: "Manual" / "Gate In", or "Maersk" /
    # "Discharged". Carried as resolved labels because the map is not the place to
    # start re-deriving a provider's wording.
    source_label: str = ""
    detail: str = ""

    shipment: Shipment | None = None
    destination: ContainerLocation | None = None

    # The visibility read model's own ETA answer, carried rather than re-derived:
    # which of a shipment's date and a carrier's forecast to believe is decided in
    # :attr:`~apps.scm.visibility.read_models.VisibilityObject.current_eta`, and a
    # second choice here would put a different date on the marker than on the row.
    eta: object | None = None

    # LOC-3's answer for this container, read off the attached lifecycle. Never
    # recomputed here.
    arrival_state: str = ArrivalState.EXPECTED

    # Named as the visibility read model names it, so the one arrival-state badge
    # component renders a map marker and a queue row without a second contract.
    is_arrival_overdue: bool = False

    @property
    def container_id(self) -> int:
        return self.container.pk

    @property
    def container_number(self) -> str:
        return self.container.container_id

    @property
    def shipment_id(self) -> int | None:
        return self.shipment.pk if self.shipment is not None else None

    @property
    def latitude(self) -> Decimal | None:
        return self.location.latitude if self.location is not None else None

    @property
    def longitude(self) -> Decimal | None:
        return self.location.longitude if self.location is not None else None

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def is_plottable(self) -> bool:
        """True when this position can honestly be drawn.

        A position with a canonical location and no coordinates is not plottable and
        is not broken either — the place is real, MCR simply has not recorded where
        on earth it is.
        """
        return self.location is not None and self.has_coordinates

    @property
    def is_current(self) -> bool:
        """True for PHYSICAL and TRACKING. A destination is not where something is."""
        return self.position_class in CURRENT_POSITION_CLASSES

    @property
    def place_label(self) -> str:
        """The canonical place, inside its parent where it has one."""
        return self.location.full_name if self.location is not None else ""

    @property
    def destination_label(self) -> str:
        return self.destination.full_name if self.destination is not None else ""

    @property
    def position_class_label(self) -> str:
        return str(PositionClass(self.position_class).label)

    @property
    def arrival_state_label(self) -> str:
        return str(ArrivalState(self.arrival_state).label)

    @property
    def place_statement(self) -> str:
        """The place in words that cannot be read as a fix of the container.

        The wording is the point. "At Oceanterminalen" is what the domain knows;
        "57.69, 11.85" is what the tile happens to render, and the two are not the
        same claim — the coordinate belongs to the terminal, not to the box standing
        somewhere inside it. See the module docstring.
        """
        place = self.place_label
        if not place:
            return ""
        if self.position_class == PositionClass.PHYSICAL:
            return str(_("At %(place)s") % {"place": place})
        if self.position_class == PositionClass.TRACKING:
            return str(_("Last reported at %(place)s") % {"place": place})
        if self.position_class == PositionClass.DESTINATION:
            return str(_("Bound for %(place)s") % {"place": place})
        return place


@dataclass(frozen=True)
class MapLocationGroup:
    """Every container of one position class standing at one canonical location.

    The unit the map draws, rather than the container, because a terminal's
    coordinate is the terminal's: eighty boxes at Oceanterminalen are eighty
    identical markers stacked on one point, and the honest rendering of that is one
    marker saying eighty.

    Grouped by ``(position_class, location)`` and never by coordinate alone. Two
    canonical locations that happen to share a coordinate are two places MCR chose
    to record separately, and a physical position and a destination that coincide
    are two different statements which must not merge into one marker.
    """

    position_class: str
    location: ContainerLocation
    positions: list[MapPosition] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.positions)

    @property
    def is_single(self) -> bool:
        return self.count == 1

    @property
    def lead(self) -> MapPosition | None:
        """The one position, when there is only one — for a marker that can name it."""
        return self.positions[0] if self.is_single else None

    @property
    def latitude(self) -> Decimal | None:
        return self.location.latitude

    @property
    def longitude(self) -> Decimal | None:
        return self.location.longitude

    @property
    def place_label(self) -> str:
        return self.location.full_name

    @property
    def position_class_label(self) -> str:
        return str(PositionClass(self.position_class).label)

    @property
    def is_current(self) -> bool:
        return self.position_class in CURRENT_POSITION_CLASSES

    @property
    def latest_at(self) -> datetime | None:
        """The freshest claim in the group, which is the best the marker can offer.

        The newest rather than the oldest: the marker says how current the *group's*
        information is, and a single container's own time is on its row when the
        group is opened.
        """
        times = [position.occurred_at for position in self.positions if position.occurred_at is not None]
        return max(times) if times else None

    @property
    def overdue_count(self) -> int:
        return sum(1 for position in self.positions if position.is_arrival_overdue)

    @property
    def container_numbers(self) -> list[str]:
        return [position.container_number for position in self.positions]

    @property
    def arrival_state_counts(self) -> list[tuple[str, int]]:
        """How far along the containers here are, in lifecycle order.

        Only the states actually present. An absent state is not a zero.
        """
        counts: dict[str, int] = {}
        for position in self.positions:
            counts[position.arrival_state] = counts.get(position.arrival_state, 0) + 1
        return [(str(ArrivalState(state).label), counts[state]) for state in ArrivalState.values if state in counts]


@dataclass(frozen=True)
class MapCoverage:
    """How much of the fleet the map can actually show, and why not the rest.

    Data-quality visibility, not an exception report. A location without
    coordinates is a normal state of the master data — LOC-1 deliberately did not
    invent any — and the only wrong thing to do with it is to leave the operator
    looking at a sparse map with no idea whether that means the fleet is small or
    the coordinates are missing.
    """

    physical_containers: int = 0
    tracking_containers: int = 0

    # Containers with a canonical current position whose location carries no
    # coordinates. A subset of the unplottable, separated because it is the one part
    # an operator can fix, and the fix is a location form rather than an
    # investigation.
    containers_missing_coordinates: int = 0

    # Containers with no drawable current position at all: no accepted physical
    # location, and no canonically-resolved observation either.
    unplottable_containers: int = 0

    # The distinct canonical locations that a current position named and that have
    # no coordinates. These are the rows to edit.
    locations_missing_coordinates: list[ContainerLocation] = field(default_factory=list)

    @property
    def plotted_containers(self) -> int:
        return self.physical_containers + self.tracking_containers

    @property
    def locations_missing_coordinates_count(self) -> int:
        return len(self.locations_missing_coordinates)

    @property
    def total_containers(self) -> int:
        return self.plotted_containers + self.unplottable_containers

    @property
    def has_gaps(self) -> bool:
        return bool(self.unplottable_containers or self.locations_missing_coordinates)


@dataclass(frozen=True)
class MapFilters:
    """What the map has been asked to show. Presentation only.

    Deliberately narrow. The Control Tower's own filters — status, carrier, ETA,
    delayed, exceptions, search — already decide *which containers* the map is
    about, and they reach it because the map's data URL carries them. These two
    decide only which classes of position to draw out of that same universe, so the
    map and the list beside it never describe different fleets.
    """

    # Empty means both current classes, which is the default operational map.
    position_classes: frozenset[str] = frozenset()
    show_destinations: bool = False

    def includes(self, position_class: str) -> bool:
        if position_class == PositionClass.DESTINATION:
            return self.show_destinations
        if not self.position_classes:
            return True
        return position_class in self.position_classes

    @property
    def is_active(self) -> bool:
        return bool(self.position_classes) or self.show_destinations

    @property
    def selected_position_class(self) -> str:
        """The single chosen class, or "" for the default of both.

        The map's control is a one-of choice rather than two checkboxes, because
        unchecking both would ask for a map with nothing on it — which is a bug
        report, not a filter. This is what the control renders as selected.
        """
        return next(iter(self.position_classes)) if len(self.position_classes) == 1 else ""


@dataclass(frozen=True)
class OperationalMap:
    """Everything a map view renders: the markers, and what it cannot show."""

    groups: list[MapLocationGroup] = field(default_factory=list)
    coverage: MapCoverage = field(default_factory=MapCoverage)
    filters: MapFilters = field(default_factory=MapFilters)

    @property
    def current_groups(self) -> list[MapLocationGroup]:
        return [group for group in self.groups if group.is_current]

    @property
    def destination_groups(self) -> list[MapLocationGroup]:
        return [group for group in self.groups if group.position_class == PositionClass.DESTINATION]

    @property
    def has_markers(self) -> bool:
        return bool(self.groups)

    @property
    def plotted_container_count(self) -> int:
        """Containers on the map *now*. Destination markers are not counted.

        The number that must never absorb the destination overlay: "where are my
        containers" and "where is inbound volume heading" are two questions, and one
        of them has already been answered wrongly by every map that added them up.
        """
        return sum(group.count for group in self.current_groups)

    @property
    def position_class_choices(self):
        """The classes a view may offer as filters — the current ones only."""
        return [(value, str(PositionClass(value).label)) for value in CURRENT_POSITION_CLASSES]


def parse_map_filters(params) -> MapFilters:
    """Read the map's own view state out of a request's query parameters.

    Unrecognised values are dropped rather than rejected: these arrive from a query
    string, and a hand-edited or stale link should show the default map rather than
    a 500. Asking for no valid class is the same as asking for the default, because
    a map with every layer switched off is a bug report, not a filter.
    """
    wanted = {value for value in params.getlist("position") if value in CURRENT_POSITION_CLASSES}
    return MapFilters(
        position_classes=frozenset(wanted),
        show_destinations=params.get("destinations") == "1",
    )


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def get_operational_map(
    team: Team,
    objects: Sequence[VisibilityObject],
    filters: MapFilters | None = None,
) -> OperationalMap:
    """Build the map for a list of visibility objects the caller already has.

    Takes the objects rather than fetching them so the map and the page around it
    describe exactly the same selection: the Control Tower filters its list once,
    and the map is a second reading of that result rather than a second query with
    its own idea of what is active.

    Two queries whatever the size of the fleet — the winning state movements, and
    the newest canonical observations — both keyed by container id. The canonical
    locations themselves arrive with the containers and shipments the caller loaded.
    """
    filters = filters or MapFilters()
    positions, coverage = build_map_positions(team, objects)
    wanted = [
        position
        for position in positions
        # Plottable *and* asked for. A position at a canonical place with no
        # coordinates is a real answer the read model returns and a marker it must
        # not produce — the coverage counts are where it is reported.
        if position.is_plottable and filters.includes(position.position_class)
    ]
    return OperationalMap(groups=group_positions(wanted), coverage=coverage, filters=filters)


def get_container_map_positions(team: Team, container: Container, workspace=None) -> list[MapPosition]:
    """One container's canonical markers: where it is, and where it is going.

    The single-object entry point, for the Container Workspace. It runs the same
    classifier the Control Tower does — precedence, coordinates, the lot — so a box
    drawn as physically at Oceanterminalen on the fleet map cannot be drawn as
    tracking-derived on its own page.

    ``workspace`` lets the detail view hand over the workspace it has already
    built, so the page does not load this container's tracking a second time.

    Returns an empty list when the container has neither a position nor a canonical
    destination. Both may be present and both may be absent, and either may be
    unplottable; the caller reads ``is_current`` to tell them apart rather than
    relying on the order, and ``is_plottable`` to know whether it can be drawn.
    """
    from .selectors import get_container_visibility

    obj = get_container_visibility(team=team, container=container, workspace=workspace)
    positions, _coverage = build_map_positions(team, [obj])
    return positions


def build_map_positions(
    team: Team,
    objects: Sequence[VisibilityObject],
) -> tuple[list[MapPosition], MapCoverage]:
    """Classify every container in *objects*, and report what could not be drawn.

    Returns every position of every class, including the ones that cannot be drawn.
    A canonical place with no coordinates is something the domain knows and the map
    cannot show, and both halves are needed: the Container Workspace has to be able
    to say "at John Evans Depot — not on the map, no coordinates yet", which it
    could not do if this filtered them out. Callers that draw filter on
    :attr:`MapPosition.is_plottable`; see :func:`get_operational_map`.

    The coverage counts describe the whole selection rather than whichever layers
    happen to be switched on, for the same reason.
    """
    from apps.scm.containers.movements import current_state_movements
    from apps.scm.tracking.positions import get_canonical_tracking_positions

    containers = [container for obj in objects for container in obj.containers]
    container_ids = [container.pk for container in containers]
    movements = current_state_movements(team, container_ids)
    observations = get_canonical_tracking_positions(team, container_ids)

    positions: list[MapPosition] = []
    physical = tracking = missing_coordinates = unplottable = 0
    without_coordinates: dict[int, ContainerLocation] = {}

    for obj in objects:
        lifecycles = _lifecycles_by_container(obj)
        for container in obj.containers:
            current = _current_position(
                container,
                obj=obj,
                lifecycle=lifecycles.get(container.pk),
                movement=movements.get(container.pk),
                observation=observations.get(container.pk),
            )
            if current is not None:
                positions.append(current)
            if current is None:
                unplottable += 1
            elif not current.is_plottable:
                # A real canonical place with no coordinates on it. Counted twice on
                # purpose: it cannot be drawn, and it is the fixable kind.
                unplottable += 1
                missing_coordinates += 1
                # `location` is set — that is what distinguishes this from None.
                location = current.location
                if location is not None:
                    without_coordinates.setdefault(location.pk, location)
            elif current.position_class == PositionClass.PHYSICAL:
                physical += 1
            else:
                tracking += 1

            destination = _destination_position(container, obj=obj, lifecycle=lifecycles.get(container.pk))
            if destination is not None:
                positions.append(destination)

    coverage = MapCoverage(
        physical_containers=physical,
        tracking_containers=tracking,
        containers_missing_coordinates=missing_coordinates,
        unplottable_containers=unplottable,
        locations_missing_coordinates=sorted(without_coordinates.values(), key=lambda row: row.name),
    )
    return positions, coverage


def group_positions(positions: Iterable[MapPosition]) -> list[MapLocationGroup]:
    """Collapse positions onto one marker per canonical location per class.

    Ordered largest group first, so the busiest place is the one a reader's eye and
    a legend both reach first, with the place name breaking ties deterministically.
    """
    grouped: dict[tuple[str, int], list[MapPosition]] = {}
    locations: dict[int, ContainerLocation] = {}
    for position in positions:
        # Belt and braces beside get_operational_map's own filter: a group with no
        # coordinates cannot become a marker, and one built anyway would be a
        # feature with a null geometry.
        if not position.is_plottable:
            continue
        # Non-None: that is half of what is_plottable asserts.
        location = cast("ContainerLocation", position.location)
        locations[location.pk] = location
        grouped.setdefault((position.position_class, location.pk), []).append(position)

    groups = [
        MapLocationGroup(position_class=position_class, location=locations[location_id], positions=members)
        for (position_class, location_id), members in grouped.items()
    ]
    return sorted(groups, key=lambda group: (-group.count, group.place_label, group.position_class))


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _lifecycles_by_container(obj: VisibilityObject) -> dict[int, object]:
    """This object's LOC-3 lifecycles keyed by container, or empty if none attached.

    Empty is not "not arrived": :class:`MapPosition` keeps LOC-3's own defaults for
    a container nobody has interpreted, rather than inventing a state for it here.
    """
    if obj.arrival is None:
        return {}
    return {lifecycle.container.pk: lifecycle for lifecycle in obj.arrival.lifecycles}


def _current_position(
    container: Container,
    *,
    obj: VisibilityObject,
    lifecycle,
    movement,
    observation,
) -> MapPosition | None:
    """The container's current position, or None when the domain cannot place it.

    The precedence, and the reason for each step:

    1. **PHYSICAL.** ``current_location`` is the projection of accepted movements.
       Somebody handled the box, or a claim strong enough to stand as state was
       recorded about it. Nothing outranks that — in particular a carrier event that
       arrived later but *happened* earlier does not, because the projection has
       already weighed the two and this reads its answer instead of re-deciding.
    2. **TRACKING.** No accepted physical position, but a carrier observed the box
       somewhere MCR recognises. Weaker, and labelled as the carrier's word.
    3. **None.** No marker. The container is in the list beside the map, and
       :class:`MapCoverage` counts it, which is where "we cannot place this" belongs.

    There is deliberately no fourth step. Falling back to the destination would draw
    a container at a place it has not reached.
    """
    if container.current_location_id is not None:
        return MapPosition(
            container=container,
            position_class=PositionClass.PHYSICAL,
            location=container.current_location,
            # The movement's own time when a movement is behind the position, and the
            # container's stamp when the position predates the movement history —
            # both are records of when this became true, and neither is invented.
            occurred_at=(movement.occurred_at if movement is not None else container.last_location_update),
            source_label=_physical_source_label(container, movement),
            detail=str(movement.get_movement_type_display()) if movement is not None else "",
            shipment=obj.shipment,
            destination=obj.destination_location,
            eta=obj.current_eta,
            **_lifecycle_fields(lifecycle),
        )

    if observation is not None:
        return MapPosition(
            container=container,
            position_class=PositionClass.TRACKING,
            location=observation.location,
            occurred_at=observation.event_datetime,
            source_label=observation.provider.name if observation.provider_id else "",
            detail=observation.display_title,
            shipment=obj.shipment,
            destination=obj.destination_location,
            eta=obj.current_eta,
            **_lifecycle_fields(lifecycle),
        )

    return None


def _destination_position(container: Container, *, obj: VisibilityObject, lifecycle) -> MapPosition | None:
    """Where this container is headed, for the overlay. None when it is not headed anywhere.

    Only while the arrival is still outstanding. A box that has been gated in and
    received is not inbound volume, and leaving it on the overlay would make the
    answer to "what is still coming to Oceanterminalen" include everything that has
    already got there.

    A container with no canonical destination produces nothing at all rather than
    being matched to a location by the booking's free text — the false positive
    LOC-1's canonical layer exists to prevent.
    """
    destination = obj.destination_location
    if destination is None:
        return None
    fields = _lifecycle_fields(lifecycle)
    if fields["arrival_state"] not in OUTSTANDING_STATES:
        return None
    return MapPosition(
        container=container,
        position_class=PositionClass.DESTINATION,
        location=destination,
        # A destination has no observation time. The ETA is the shipment's, and it
        # travels as the object's ETA rather than being restated as a position time
        # that somebody could mistake for an observation.
        occurred_at=None,
        shipment=obj.shipment,
        destination=destination,
        eta=obj.current_eta,
        **fields,
    )


def _lifecycle_fields(lifecycle) -> dict:
    """LOC-3's answer for one container, or its documented defaults."""
    if lifecycle is None:
        return {"arrival_state": ArrivalState.EXPECTED, "is_arrival_overdue": False}
    return {"arrival_state": lifecycle.state, "is_arrival_overdue": lifecycle.is_overdue}


def _physical_source_label(container: Container, movement) -> str:
    """Who claimed the accepted position, as a label.

    The same rule the container workspace's physical panel applies: the movement's
    source when there is one, falling back to the container's own
    ``location_source`` for a position that predates the movement history.
    """
    from apps.scm.containers.choices import LocationSource

    source = movement.source if movement is not None else container.location_source
    return str(LocationSource(source).label) if source else ""
