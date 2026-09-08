"""Where a container is, and how well we actually know it.

Carrier tracking gives positions of very different quality, and presenting them as
if they were the same is misleading in a way that costs money: a port's coordinates
say the container passed through that terminal, not that it is sitting there now,
and a vessel's position says where the ship is, not where the box is once it has
been discharged.

Every position therefore carries an explicit :class:`PositionType`, and callers are
expected to show it. Nothing here upgrades a facility coordinate into a GPS fix.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import cast

from django.db import models
from django.db.models import TextChoices
from django.utils.translation import gettext_lazy as _

from apps.scm.containers.choices import LocationResolutionStatus

from .models import TrackingEvent


class PositionType(TextChoices):
    """How a position was obtained — never to be inferred away."""

    GPS = "gps", _("GPS position")
    VESSEL = "vessel", _("Vessel position")
    FACILITY = "facility", _("Terminal or port")
    ESTIMATED = "estimated", _("Estimated")
    UNKNOWN = "unknown", _("Unknown")


@dataclass
class ContainerPosition:
    """The last place a container was reported, with the quality of that report."""

    position_type: str
    observed_at: object | None = None
    location_name: str = ""
    location_unlocode: str = ""
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    vessel_name: str = ""
    vessel_imo: str = ""
    voyage_number: str = ""
    event: TrackingEvent | None = None

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def is_realtime(self) -> bool:
        """True only for an actual GPS fix of the container itself.

        A vessel position or a terminal coordinate is not the container's real-time
        position and must not be drawn as one.
        """
        return self.position_type == PositionType.GPS

    @property
    def label(self) -> str:
        """A short human label for the place, falling back to the UN/LOCODE."""
        return self.location_name or self.location_unlocode or ""

    def get_position_type_display(self) -> str:
        return str(PositionType(self.position_type).label)


def classify_position(event: TrackingEvent) -> str:
    """Return the PositionType for an event, without upgrading its quality.

    An estimated event describes a forecast, so its place is estimated no matter how
    precise the coordinates look.

    Coordinates on a vessel movement locate the vessel, not the box once discharged.

    Coordinates that arrive alongside a named terminal or UN/LOCODE are that place's
    coordinates — DCSA carries them inside the event's location object. They read as
    a precise fix and are nothing of the kind: the container passed through that
    terminal, which is a facility position however many decimals it has.

    Only coordinates with no place attached to them are treated as a fix of the
    container itself.
    """
    if event.is_estimated:
        return PositionType.ESTIMATED

    has_coordinates = event.location_latitude is not None and event.location_longitude is not None
    if has_coordinates:
        on_a_vessel = bool(event.vessel_imo or event.vessel_name) and event.transport_mode in (
            TrackingEvent.TransportMode.VESSEL,
            TrackingEvent.TransportMode.BARGE,
        )
        if on_a_vessel:
            return PositionType.VESSEL
        if event.location_unlocode or event.location_name:
            return PositionType.FACILITY
        return PositionType.GPS

    if event.location_unlocode or event.location_name:
        return PositionType.FACILITY
    return PositionType.UNKNOWN


def position_from_event(event: TrackingEvent) -> ContainerPosition:
    """Build a ContainerPosition from a tracking event."""
    return ContainerPosition(
        position_type=classify_position(event),
        observed_at=event.event_datetime,
        location_name=event.location_name,
        location_unlocode=event.location_unlocode,
        latitude=event.location_latitude,
        longitude=event.location_longitude,
        vessel_name=event.vessel_name,
        vessel_imo=event.vessel_imo,
        voyage_number=event.voyage_number,
        event=event,
    )


# An event describes a place when the carrier attached one to it. Document
# milestones — a bill of lading drafted, issued, released — carry no place at all.
HAS_A_PLACE = (
    models.Q(location_unlocode__gt="") | models.Q(location_name__gt="") | models.Q(location_latitude__isnull=False)
)


def event_has_a_place(event: TrackingEvent) -> bool:
    """True when the event says where it happened — the in-Python form of HAS_A_PLACE.

    Stated next to the Q object so the two cannot drift: a caller that has already
    loaded events (the container journey) must apply the same rule as one that is
    still narrowing a queryset, or the same container would be reported at two
    different places depending on which path asked.
    """
    return bool(event.location_unlocode or event.location_name or event.location_latitude is not None)


def get_latest_container_position(team, container) -> ContainerPosition | None:
    """Return the container's last reported position, or None if never reported.

    Two preferences, in order.

    *Observed over forecast*: an estimate tells you where the carrier thinks the box
    will be, which is not a position. Only when there is no actual event at all does
    the estimate stand in, and then it is labelled ESTIMATED.

    *Located over placeless*: the last thing a carrier reports is often paperwork —
    a transport document released, say — which happens nowhere. Letting that be the
    position would throw away a place we know, and report "unknown" about a box last
    confirmed at a named terminal. The most recent *located* observation wins, and
    its timestamp is when the box was there.
    """
    # The provider travels with the anchor: the map read model names the feed that
    # carried the observation, and following the FK afterwards would be a query per
    # container on any page that asks about more than one.
    events = (
        TrackingEvent.objects.filter(team=team, container=container)
        .exclude(event_datetime__isnull=True)
        .select_related("provider")
    )

    # The created_at tiebreak makes the answer deterministic when a carrier reports
    # two events at the same instant, and matches what the bulk builder in
    # containers.workspace picks, so both paths name the same position.
    actual = events.filter(event_time_type=TrackingEvent.EventTimeType.ACTUAL).order_by(
        "-event_datetime", "-created_at"
    )
    anchor = actual.filter(HAS_A_PLACE).first() or actual.first()
    if anchor is None:
        anchor = events.order_by("-event_datetime", "-created_at").first()
    return position_from_event(anchor) if anchor else None


# Tracking evidence good enough to put a container on a canonical map: observed,
# dated, and resolved to one of MCR's own locations.
#
# Every clause is a refusal rather than a preference, which is why this is stated
# once and imported rather than assembled at each call site:
#
# ``event_time_type=ACTUAL``
#     A forecast says where a carrier expects the box to be. Drawing it would put a
#     container at a terminal it has not reached.
# ``event_datetime`` present
#     An undated report cannot be compared with anything, so it cannot be "latest",
#     and a marker with no time carries no freshness for the reader to judge.
# ``location`` set and ``RESOLVED``
#     The canonical identity LOC-1 established. AMBIGUOUS leaves ``location`` NULL
#     by design — the resolver refuses to pick between candidates — and UNRESOLVED
#     never had one. Neither may produce a canonical marker: the coordinates would
#     be somebody's guess about which place the carrier meant.
CANONICAL_TRACKING_EVIDENCE = (
    models.Q(event_time_type=TrackingEvent.EventTimeType.ACTUAL)
    & models.Q(event_datetime__isnull=False)
    & models.Q(location__isnull=False)
    & models.Q(location_resolution_status=LocationResolutionStatus.RESOLVED)
)


def get_canonical_tracking_positions(team, container_ids) -> dict[int, TrackingEvent]:
    """The newest canonically-located observation per container, in one query.

    The tracking half of LOC-4's position precedence. What comes back is evidence,
    not state: the caller decides whether it is allowed to speak for a container,
    and it never overrides an accepted physical position.

    ``DISTINCT ON`` keeps this to a single round trip however many containers are
    asked about, and the ``created_at`` tiebreak matches every other "latest event"
    read in the codebase, so two paths cannot name different events as the newest.

    The canonical location travels with the row — with its parent, since a terminal
    is labelled inside the port that contains it — because the caller reads a place
    and a coordinate off every one of these and following the FK per container is
    how a fleet-wide map grows a query per marker.
    """
    container_ids = list(container_ids)
    if not container_ids:
        return {}
    rows = (
        TrackingEvent.objects.filter(CANONICAL_TRACKING_EVIDENCE, team=team, container_id__in=container_ids)
        .select_related("location", "location__parent_location", "provider")
        .order_by("container_id", "-event_datetime", "-created_at")
        .distinct("container_id")
    )
    # Non-NULL: the queryset only asked for events on these containers. The same
    # cast the other bulk event readers use.
    return {cast(int, row.container_id): row for row in rows}
