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

from django.db import models
from django.db.models import TextChoices
from django.utils.translation import gettext_lazy as _

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
    events = TrackingEvent.objects.filter(team=team, container=container).exclude(event_datetime__isnull=True)

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
