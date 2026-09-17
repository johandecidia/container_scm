"""One container's journey, assembled from every source that has reported it.

A container does not have *a* tracking source. It has however many have proved
themselves over the course of one physical journey, and they cover different legs
of it:

    China → Born            reported by the ocean carrier
    Born → Gothenburg       reported by whoever moved it onward, if anyone
    Gothenburg → depot      observed by us, physically

Nothing here assumes the newest subscription speaks for the whole trip. Every
verified provider contributes, oldest events included, and each point carries the
source that reported it so the reader can see who knows what.

Four ideas hold this module together.

**Derived, never stored.** A journey is a read over ``TrackingEvent`` and the
container's own location record. There is no leg model, no second event table and
no reconstruction of the route between two reported places — we know where the box
was reported, not the path it took.

**Sources are kept apart, not blended.** A carrier's report and our own physical
observation are different kinds of knowledge. Both are journey points; which one
is the container's *current* location is decided explicitly, in
:meth:`ContainerJourney.current_location`, and the loser stays on the timeline.

**De-duplication is conservative.** Two carriers describing the same discharge
minutes apart become one point carrying two sources. Anything less than an exact
match on event type, place and classifier is left as two points — losing a real
event is worse than showing a near-duplicate, and the source labels make the
near-duplicate readable.

**Persistence is untouched.** Event fingerprints stay provider-scoped, so a second
provider's version of an event is stored, not dropped. The merging here is a
presentation decision, made fresh on every read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from django.db.models import TextChoices
from django.utils.translation import gettext_lazy as _

from .models import TrackingEvent, TrackingSubscription
from .positions import ContainerPosition, PositionType, classify_position, event_has_a_place

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

    from apps.scm.containers.models import Container
    from apps.teams.models import Team

# Sorts undated points to one end without ever being compared to a real timestamp:
# the sort keys test for None first, so this only ever meets itself.
_EPOCH = datetime.min.replace(tzinfo=UTC)

# How far apart two providers' reports of the same physical event may be and still
# be treated as that one event. Carriers timestamp the same discharge minutes
# apart; they do not report it hours apart.
CORROBORATION_WINDOW = timedelta(minutes=15)

# Our own physical record of where the box is — the depot that received it, the
# yard that stacked it, the person who saw it. One source among many and not a
# carrier: it reports position, never a voyage.
PHYSICAL_SOURCE_CODE = "mcr"
PHYSICAL_SOURCE_NAME = _("MCR")


class JourneySourceKind(TextChoices):
    """Who reported a journey point, and therefore what kind of claim it is."""

    CARRIER = "carrier", _("Carrier")
    PHYSICAL = "physical", _("Physical observation")


class CurrentLocationBasis(TextChoices):
    """What the derived current location rests on — never to be hidden from the reader.

    A physical observation of the box and a carrier's forecast for it are both
    "where it is" in a UI that shows only one line, and they are not remotely the
    same claim.
    """

    PHYSICAL = "physical", _("Physically observed")
    CARRIER_ACTUAL = "carrier_actual", _("Reported by carrier")
    CARRIER_FORECAST = "carrier_forecast", _("Carrier forecast")


@dataclass(frozen=True)
class JourneySource:
    """A source that has reported something about this container."""

    kind: str
    code: str
    name: str

    @property
    def is_physical(self) -> bool:
        return self.kind == JourneySourceKind.PHYSICAL

    @property
    def is_carrier(self) -> bool:
        return self.kind == JourneySourceKind.CARRIER


PHYSICAL_SOURCE = JourneySource(
    kind=JourneySourceKind.PHYSICAL,
    code=PHYSICAL_SOURCE_CODE,
    # Cast at build time so templates and comparisons see a plain string; the label
    # is fixed, not per-request.
    name=str(PHYSICAL_SOURCE_NAME),
)


@dataclass(frozen=True)
class JourneyCorroboration:
    """A second source's version of a point another source already reported."""

    source: JourneySource
    occurred_at: datetime | None
    event: TrackingEvent | None = None


@dataclass
class JourneyPoint:
    """One thing that happened to the container, and who says so."""

    source: JourneySource
    occurred_at: datetime | None
    title: StrOrPromise
    location_name: str = ""
    location_unlocode: str = ""
    # Other names for the same place — a location's city and country, say. Used only
    # to recognise that two sources mean the same place; never displayed as the
    # place itself.
    location_aliases: tuple[str, ...] = ()
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    position_type: str = PositionType.UNKNOWN
    time_type: str = TrackingEvent.EventTimeType.ACTUAL
    event_type: str = ""
    transport_mode: str = ""
    vessel_name: str = ""
    vessel_imo: str = ""
    voyage_number: str = ""
    description: str = ""
    # The stored event this point came from, for the carrier points. None for our
    # own physical observation, which is not an event.
    event: TrackingEvent | None = None
    # Other sources that reported the same physical event. Never a replacement for
    # this point's own source — an addition to it.
    corroborations: list[JourneyCorroboration] = field(default_factory=list)

    @property
    def is_actual(self) -> bool:
        return self.time_type == TrackingEvent.EventTimeType.ACTUAL

    @property
    def is_estimated(self) -> bool:
        return self.time_type in (TrackingEvent.EventTimeType.ESTIMATED, TrackingEvent.EventTimeType.PLANNED)

    @property
    def is_physical(self) -> bool:
        return self.source.is_physical

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def has_a_place(self) -> bool:
        """True when this point says where it happened.

        Delegated for a carrier point so the rule lives once, beside the queryset
        form of it in ``positions.py``.
        """
        if self.event is not None:
            return event_has_a_place(self.event)
        return bool(self.location_name or self.location_unlocode or self.latitude is not None)

    @property
    def place_label(self) -> str:
        return self.location_name or self.location_unlocode or ""

    @property
    def event_id(self) -> int | None:
        """The id the map and the timeline agree on, or None for a physical point."""
        return self.event.pk if self.event is not None else None

    @property
    def sources(self) -> list[JourneySource]:
        """Every source behind this point, the one that reported it first leading."""
        return [self.source, *(corroboration.source for corroboration in self.corroborations)]

    @property
    def source_names(self) -> list[str]:
        return [source.name for source in self.sources]

    @property
    def corroborating_source_names(self) -> list[str]:
        return [corroboration.source.name for corroboration in self.corroborations]

    def to_position(self) -> ContainerPosition:
        """Express this point as a ContainerPosition.

        Lets the existing position component render a physical observation and a
        carrier event through one template, with the quality of each preserved.
        """
        return ContainerPosition(
            position_type=self.position_type,
            observed_at=self.occurred_at,
            location_name=self.location_name,
            location_unlocode=self.location_unlocode,
            latitude=self.latitude,
            longitude=self.longitude,
            vessel_name=self.vessel_name,
            vessel_imo=self.vessel_imo,
            voyage_number=self.voyage_number,
            event=self.event,
        )


@dataclass(frozen=True)
class DerivedCurrentLocation:
    """Where the container is now, and what that answer rests on."""

    point: JourneyPoint
    basis: str

    @property
    def label(self) -> str:
        return self.point.place_label

    @property
    def occurred_at(self) -> datetime | None:
        return self.point.occurred_at

    @property
    def source(self) -> JourneySource:
        return self.point.source

    @property
    def is_physical(self) -> bool:
        return self.basis == CurrentLocationBasis.PHYSICAL

    @property
    def basis_label(self) -> str:
        return str(CurrentLocationBasis(self.basis).label)

    @property
    def position(self) -> ContainerPosition:
        return self.point.to_position()


@dataclass
class ContainerJourney:
    """Everything known about where one container has been, from every source."""

    container: Container
    # Oldest first. Undated points sort last: they cannot be placed in time, and
    # dropping them would lose events a carrier did send.
    points: list[JourneyPoint] = field(default_factory=list)
    subscriptions: list[TrackingSubscription] = field(default_factory=list)
    physical_observation: JourneyPoint | None = None

    # -- sources -----------------------------------------------------------

    @property
    def sources(self) -> list[JourneySource]:
        """Every source contributing to this journey, carriers first.

        Built from the verified subscriptions rather than from the events, because a
        subscription *is* the record that a carrier proved itself. A provider with
        events but no surviving subscription is added too, so history that outlived
        its watch is still attributed.
        """
        sources: list[JourneySource] = []
        seen: set[str] = set()

        def add(source: JourneySource) -> None:
            if source.code not in seen:
                seen.add(source.code)
                sources.append(source)

        for subscription in self.subscriptions:
            if subscription.provider_id:
                add(_carrier_source(subscription.provider))
        for point in self.points:
            for source in point.sources:
                if source.is_carrier:
                    add(source)
        if self.physical_observation is not None:
            add(PHYSICAL_SOURCE)
        return sources

    @property
    def carrier_sources(self) -> list[JourneySource]:
        return [source for source in self.sources if source.is_carrier]

    @property
    def has_multiple_sources(self) -> bool:
        return len(self.sources) > 1

    # -- points ------------------------------------------------------------

    @property
    def newest_first(self) -> list[JourneyPoint]:
        """The journey as a timeline reads it. Undated points stay last."""
        dated = [point for point in self.points if point.occurred_at is not None]
        undated = [point for point in self.points if point.occurred_at is None]
        return list(reversed(dated)) + undated

    @property
    def carrier_points(self) -> list[JourneyPoint]:
        return [point for point in self.points if point.source.is_carrier]

    @property
    def latest_carrier_point(self) -> JourneyPoint | None:
        return _last_dated(self.carrier_points)

    @property
    def latest_actual_carrier_point(self) -> JourneyPoint | None:
        """The carrier's last *observation* — never a forecast.

        A forecast says where a carrier expects the box to be, which is not a
        position and must never displace one.
        """
        return _last_dated([point for point in self.carrier_points if point.is_actual])

    @property
    def latest_located_actual_carrier_point(self) -> JourneyPoint | None:
        """The last observed carrier point that names a place.

        The last thing a carrier reports is often paperwork, which happens nowhere.
        Letting that be the position would report "unknown" about a box last
        confirmed at a named terminal — the same preference ``positions.py`` applies.
        """
        located = [point for point in self.carrier_points if point.is_actual and point.has_a_place]
        return _last_dated(located) or self.latest_actual_carrier_point

    # -- current location --------------------------------------------------

    @property
    def current_location(self) -> DerivedCurrentLocation | None:
        """Where the container is now, from whichever source knows most recently.

        A physical observation of the box wins when it is at least as recent as the
        carrier's last observation: somebody saw the container, which outranks a
        report about the leg it was on. An older physical record does not win — a
        carrier that has since observed the box moving is the newer truth.

        A forecast is only ever used when there is no observation of any kind, and
        is labelled as a forecast when it is.
        """
        carrier = self.latest_located_actual_carrier_point
        physical = self.physical_observation

        if physical is not None and physical.occurred_at is not None and _at_least_as_recent(physical, carrier):
            return DerivedCurrentLocation(point=physical, basis=CurrentLocationBasis.PHYSICAL)
        if carrier is not None:
            return DerivedCurrentLocation(point=carrier, basis=CurrentLocationBasis.CARRIER_ACTUAL)

        forecast = _last_dated([point for point in self.carrier_points if point.has_a_place])
        if forecast is not None:
            return DerivedCurrentLocation(point=forecast, basis=CurrentLocationBasis.CARRIER_FORECAST)
        return None


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def get_container_journey(
    team: Team,
    container: Container,
    *,
    events=None,
    subscriptions=None,
) -> ContainerJourney:
    """Return the container's journey across every source that has reported it.

    ``events`` and ``subscriptions`` let a caller that has already loaded them —
    the container workspace does — hand them over instead of paying for the same
    two queries again. Both must be this container's and this team's; when they are
    omitted they are loaded here, which is the only place team scoping is applied.
    """
    if events is None:
        events = list(
            TrackingEvent.objects.filter(team=team, container=container)
            .select_related("provider")
            .order_by("event_datetime", "created_at")
        )
    if subscriptions is None:
        subscriptions = list(
            TrackingSubscription.objects.filter(team=team, container=container)
            .exclude(status=TrackingSubscription.Status.CANCELLED)
            .select_related("provider")
            .order_by("created_at")
        )

    physical = build_physical_observation(container)
    carrier_points = _merge_corroborating([_point_from_event(event) for event in _in_order(events)])
    all_points = [*carrier_points, physical] if physical is not None else list(carrier_points)

    return ContainerJourney(
        container=container,
        points=_in_time_order(all_points),
        subscriptions=list(subscriptions),
        physical_observation=physical,
    )


def build_physical_observation(container: Container) -> JourneyPoint | None:
    """Return our own physical record of where this container is, or None.

    Three conditions, each load-bearing:

    *dated* — an undated location cannot be compared with a carrier event, so it
    can neither win the current location nor evidence a gap. It stays on the
    container record, where it is already shown.

    *placed* — a location row or a free-text location. Without either there is
    nothing to report.

    *ours* — a location whose source is a tracking event was derived from carrier
    data. Treating it as an independent observation would let one carrier report
    corroborate itself, and would manufacture a gap out of a rounding difference
    between the event and the location it wrote.
    """
    from apps.scm.containers.choices import LocationSource

    if container.last_location_update is None:
        return None
    if container.location_source == LocationSource.TRACKING_EVENT:
        return None

    location = container.current_location
    name = location.name if location is not None else (container.location_text or "")
    if not name:
        return None

    return JourneyPoint(
        source=PHYSICAL_SOURCE,
        occurred_at=container.last_location_update,
        title=_physical_title(location),
        location_name=name,
        location_unlocode=physical_unlocode(location),
        location_aliases=_physical_aliases(location),
        # A named place, not a fix of the box: our record says which yard holds it,
        # not where in the yard. ContainerLocation carries no coordinates, so a
        # physical observation never reaches the map — it reaches the timeline, the
        # panel and the gap check, which is where it decides anything.
        position_type=PositionType.FACILITY,
        time_type=TrackingEvent.EventTimeType.ACTUAL,
        description=_physical_description(location),
    )


def physical_unlocode(location) -> str:
    """Return the UN/LOCODE recorded for a container location, or "".

    ``ContainerLocation`` has no UN/LOCODE field, so ``external_reference`` is read
    as one when it is shaped like one. That is what makes matching a depot against
    a carrier's port exact rather than a comparison of names in two languages; when
    it is absent, place matching falls back to names.
    """
    if location is None:
        return ""
    candidate = (location.external_reference or "").strip().upper()
    if len(candidate) == 5 and candidate[:2].isalpha() and candidate[2:].isalnum():
        return candidate
    return ""


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _carrier_source(provider) -> JourneySource:
    return JourneySource(
        kind=JourneySourceKind.CARRIER,
        code=provider.code,
        name=provider.name or provider.code,
    )


def _point_from_event(event: TrackingEvent) -> JourneyPoint:
    return JourneyPoint(
        source=_carrier_source(event.provider),
        occurred_at=event.event_datetime,
        title=event.display_title,
        location_name=event.location_name,
        location_unlocode=event.location_unlocode,
        latitude=event.location_latitude,
        longitude=event.location_longitude,
        position_type=classify_position(event),
        time_type=event.event_time_type,
        event_type=event.event_type,
        transport_mode=event.transport_mode,
        vessel_name=event.vessel_name,
        vessel_imo=event.vessel_imo,
        voyage_number=event.voyage_number,
        description=event.description or event.status,
        event=event,
    )


def _in_order(events) -> list[TrackingEvent]:
    """Sort events oldest first however the caller happened to order them.

    The workspace loads its events newest first for the timeline; the journey needs
    the other direction, and re-querying for it would double the cost of the page.
    """
    events = list(events)
    return sorted(
        events,
        key=lambda event: (
            event.event_datetime is None,
            event.event_datetime or _EPOCH,
            event.created_at,
            event.pk or 0,
        ),
    )


def _in_time_order(points: list[JourneyPoint]) -> list[JourneyPoint]:
    return sorted(points, key=lambda point: (point.occurred_at is None, point.occurred_at or _EPOCH))


def _at_least_as_recent(point: JourneyPoint, other: JourneyPoint | None) -> bool:
    """True when ``point`` is no older than ``other``, which may not exist.

    An undated or absent ``other`` cannot be the newer of the two: it says nothing
    about when anything happened.
    """
    if other is None or other.occurred_at is None:
        return True
    return point.occurred_at is not None and point.occurred_at >= other.occurred_at


def _last_dated(points: list[JourneyPoint]) -> JourneyPoint | None:
    """Return the newest dated point in an already-chronological list."""
    for point in reversed(points):
        if point.occurred_at is not None:
            return point
    return None


def _merge_corroborating(points: list[JourneyPoint]) -> list[JourneyPoint]:
    """Fold a second provider's version of the same event into the first's point.

    Chronological in, chronological out. The earliest report of an event stays the
    point — it is the closest thing to the observation — and the later one becomes a
    corroborating source on it, keeping its own event so nothing is lost.

    Only an exact match merges: same internal event type, same classifier, same
    place, a different provider, and minutes apart. Two events that fail any of
    those stay two points; the source label on each is what makes them readable.
    """
    merged: list[JourneyPoint] = []
    for point in points:
        target = _corroborated_point(merged, point)
        if target is None:
            merged.append(point)
            continue
        target.corroborations.append(
            JourneyCorroboration(source=point.source, occurred_at=point.occurred_at, event=point.event)
        )
    return merged


def _corroborated_point(merged: list[JourneyPoint], point: JourneyPoint) -> JourneyPoint | None:
    """Return the already-kept point ``point`` is another report of, or None."""
    if point.occurred_at is None or not point.event_type:
        return None
    if point.event_type == TrackingEvent.EventType.UNKNOWN:
        # An event we could not classify has no identity to match on. Two of them
        # may or may not be the same thing, and guessing wrong loses one.
        return None

    for candidate in reversed(merged):
        if candidate.occurred_at is None:
            continue
        if point.occurred_at - candidate.occurred_at > CORROBORATION_WINDOW:
            # Chronological input: everything earlier is further away still.
            return None
        if candidate.event_type != point.event_type or candidate.time_type != point.time_type:
            continue
        if point.source.code in {source.code for source in candidate.sources}:
            # The same provider reporting twice is two events, not a duplicate —
            # its own fingerprint already settled that at write time.
            continue
        if _same_place_exactly(candidate, point):
            return candidate
    return None


def _same_place_exactly(first: JourneyPoint, second: JourneyPoint) -> bool:
    """True only when two points name the identical place.

    UN/LOCODE when both carry one, otherwise identical names. Deliberately strict:
    this decides whether to collapse two stored events into one row, and a wrong
    "yes" hides an event that really happened.
    """
    if first.location_unlocode and second.location_unlocode:
        return first.location_unlocode.upper() == second.location_unlocode.upper()
    if first.location_name and second.location_name:
        return first.location_name.strip().casefold() == second.location_name.strip().casefold()
    return False


def _physical_title(location) -> StrOrPromise:
    """Name the observation after the kind of place, e.g. "At depot"."""
    from apps.scm.containers.choices import LocationType

    if location is None or location.location_type in ("", LocationType.UNKNOWN):
        return _("Physical location")
    return _("At %(place)s") % {"place": str(location.get_location_type_display()).lower()}


def _physical_description(location) -> str:
    if location is None:
        return ""
    return ", ".join(_physical_aliases(location))


def _physical_aliases(location) -> tuple[str, ...]:
    """The city and country of a location, as further names for the same place."""
    if location is None:
        return ()
    return tuple(part for part in (location.city, location.country) if part)
