"""Turning visibility read models into GeoJSON for Mapbox.

Four rules hold everywhere in this module.

**Longitude first.** GeoJSON coordinates are ``[longitude, latitude]``. Getting
this backwards puts Gothenburg in Somalia and looks plausible enough to ship.

**Properties are already decided.** Which position wins, what kind of claim it is,
whether an event was observed or forecast, what a status is called — all of it is
resolved before it leaves here, so the browser never re-implements a domain rule to
decide what to draw. In particular the precedence physical > tracking > nothing is
:mod:`apps.scm.visibility.map_positions`', and arrives as a property.

**A line between two ports is not a route.** We know where events happened, not
what path the vessel took. Connections are labelled as event connections, and the
forecast continuation is marked as forecast, so the map cannot be read as a track.
Nothing is ever drawn between a container and its destination for the same reason.

**Every source, and which one.** A container's journey is drawn from all of its
tracking sources, not the newest one, and each point says who reported it. A gap
between two sources is deliberately *not* drawn as a line: there is nothing to
draw, and joining the two ends would assert a movement nobody observed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.timesince import timesince
from django.utils.translation import gettext as _

from apps.scm.tracking.models import TrackingEvent
from apps.scm.tracking.positions import PositionType, classify_position

LINE_ACTUAL = "actual_event_connection"
LINE_FORECAST = "forecast_continuation"


def feature_collection(features: list[dict]) -> dict:
    """Wrap features in a FeatureCollection, the only shape we ever return."""
    return {"type": "FeatureCollection", "features": features}


def map_feature_collection(operational_map) -> dict:
    """Return LOC-4's operational map as GeoJSON: one feature per marker.

    A marker is a canonical location and a position class, never a container — see
    :class:`~apps.scm.visibility.map_positions.MapLocationGroup`. The class is
    carried explicitly on every feature so the browser can style physical, tracking
    and destination markers differently without knowing why they differ, and every
    label is resolved here: which position wins, what it is called, how old it is
    and how it should be worded are all decided in the read model.

    Groups with no coordinates never reach this function. They are counted in
    ``MapCoverage`` instead, which is where "this place has no coordinates yet"
    belongs — a marker at 0, 0 would be a lie about the Gulf of Guinea.
    """
    return feature_collection([_group_point(group) for group in operational_map.groups])


def journey_feature_collection(events: list[TrackingEvent], *, container_number: str = "") -> dict:
    """Return one object's journey: its located events, plus how they connect.

    ``events`` must be oldest first. Events without coordinates are skipped for the
    map and remain on the timeline, which is the authoritative chronology.
    """
    located = [
        event for event in events if event.location_latitude is not None and event.location_longitude is not None
    ]
    features: list[dict] = []
    for event in located:
        point = _event_point(event, container_number=container_number)
        if point is not None:
            features.append(point)

    actual = [event for event in located if event.is_actual]
    forecast = [event for event in located if event.is_estimated]

    actual_line = _line(actual, LINE_ACTUAL)
    if actual_line is not None:
        features.append(actual_line)

    # The forecast leg starts where the box actually got to, so the dashed line
    # continues the solid one instead of floating off on its own.
    continuation = ([actual[-1]] if actual else []) + forecast
    forecast_line = _line(continuation, LINE_FORECAST)
    if forecast_line is not None:
        features.append(forecast_line)

    return feature_collection(features)


def container_journey_feature_collection(journey, *, container_number: str = "", positions=()) -> dict:
    """Return one container's journey as reported by every one of its sources.

    Built from the derived journey rather than from one provider's events, so a box
    that changed hands mid-voyage draws as one trip. Each point carries the source
    that reported it, and the point matching the derived current location is flagged
    so the map can mark it without deciding for itself which point that is.

    A point with no coordinates is absent from the map and present on the timeline,
    which is the authoritative chronology.

    ``positions`` are LOC-4's canonical markers for this container — where MCR has
    accepted the box to be, and where it is going. They are drawn *beside* the
    journey rather than into it, because they answer a different question: the
    journey is the evidence, and a canonical marker is the conclusion. There is
    deliberately no line joining a container to its destination — a straight line
    between two places is not a shipping route, and drawing one would be the same
    mistake as reading the event connections as a vessel track.

    When a canonical current marker is drawn, the journey's own "current" halo is
    dropped: two rings claiming *now* at two slightly different coordinates — the
    carrier's for the event, MCR's for the place — is a contradiction on the screen
    even though both are true. The canonical one is the stronger statement and
    keeps the claim.
    """
    number = container_number or (journey.container.container_id if journey.container is not None else "")
    current = journey.current_location
    current_point = current.point if current is not None else None

    plottable = [position for position in positions if position.is_plottable]
    features: list[dict] = [_position_point(position) for position in plottable]
    if any(position.is_current for position in plottable):
        current_point = None

    located = [point for point in journey.points if point.has_coordinates]
    for point in located:
        features.append(_journey_point_feature(point, container_number=number, is_current=point is current_point))

    actual = [point.event for point in located if point.is_actual and point.event is not None]
    forecast = [point.event for point in located if point.is_estimated and point.event is not None]

    actual_line = _line(actual, LINE_ACTUAL)
    if actual_line is not None:
        features.append(actual_line)

    # The forecast leg starts where the box actually got to, so the dashed line
    # continues the solid one instead of floating off on its own.
    forecast_line = _line(([actual[-1]] if actual else []) + forecast, LINE_FORECAST)
    if forecast_line is not None:
        features.append(forecast_line)

    return feature_collection(features)


# ---------------------------------------------------------------------------
# Points
# ---------------------------------------------------------------------------


def _group_point(group) -> dict:
    """One marker: a place, a position class and how many boxes.

    ``container_number`` is filled in only for a group of one. A marker covering
    eighty containers has no single number, and putting the first one on it would
    read as a label for the whole group.

    A marker standing on a place the resolver never matched carries no
    ``location_id`` and no ``panel_url``: there is no canonical location to open and
    nothing to count as also standing there. ``is_canonical`` states which kind it
    is, so the browser never has to infer it from a missing id.
    """
    from .map_positions import PositionClass

    lead = group.lead
    properties = {
        "object_type": "map_position",
        "position_class": group.position_class,
        "position_class_label": group.position_class_label,
        # Stated rather than inferred from the class, so nothing downstream has to
        # know that a destination is not a current position.
        "is_current": group.is_current,
        "is_destination": group.position_class == PositionClass.DESTINATION,
        "is_canonical": group.is_canonical,
        "location_id": group.location_id,
        "location_name": group.place_label,
        "location_unlocode": group.unlocode,
        "location_type_label": group.type_label,
        "container_count": group.count,
        "container_number": lead.container_number if lead is not None else "",
        # "At Oceanterminalen", never a coordinate: the point locates the terminal,
        # not the container standing somewhere inside it.
        "place_statement": lead.place_statement if lead is not None else _class_statement(group),
        "source_label": lead.source_label if lead is not None else "",
        "detail": lead.detail if lead is not None else "",
        "occurred_at": _datetime(group.latest_at),
        "occurred_at_display": _datetime_display(group.latest_at),
        # Pre-rendered freshness. The browser never turns a timestamp into an age:
        # it would do it in the visitor's clock and disagree with every other "6h
        # ago" on the page, which are all Django's.
        "age_display": _age_display(group.latest_at),
        "arrival_state": lead.arrival_state if lead is not None else "",
        "arrival_state_label": lead.arrival_state_label if lead is not None else "",
        "arrival_state_counts": [{"label": label, "count": count} for label, count in group.arrival_state_counts],
        "overdue_count": group.overdue_count,
        "eta_display": _date_display(lead.eta) if lead is not None else "",
        "destination_label": lead.destination_label if lead is not None else "",
        "panel_url": _panel_url(group),
        "container_url": reverse("containers:detail", args=[lead.container_id]) if lead is not None else "",
    }
    # Both coordinates are set: a group with either missing never becomes a marker.
    return cast(dict, _point(group.longitude, group.latitude, properties))


def location_feature_collection(location, container_count: int = 0) -> dict:
    """Return one canonical location as a single marker, or nothing to draw.

    The Location Workspace's map answers "where is this place, and how much is
    standing in it" — one marker, not one per container, because every container
    here shares the location's single coordinate and eighty stacked discs would
    say nothing eighty times.

    Inbound volume is *not* a second marker. It would land on exactly the same
    coordinate as the first and, more to the point, the page already states it in
    words beside the tab that lists it — a duplicate on the map would be read as
    extra traffic rather than the same traffic seen twice.

    An empty collection when the location has no coordinates. The page says so in
    words instead; see the coordinate status on the workspace header.
    """
    properties = {
        "object_type": "map_position",
        "position_class": "physical",
        "position_class_label": str(_("Physical location")),
        "is_current": True,
        "is_destination": False,
        "location_id": location.pk,
        "location_name": location.full_name,
        "location_unlocode": location.unlocode,
        "location_type_label": location.get_location_type_display(),
        "container_count": container_count,
        "container_number": "",
        "place_statement": str(_("At %(place)s") % {"place": location.full_name}),
        "source_label": "",
        "detail": "",
        "occurred_at": None,
        "occurred_at_display": "",
        "age_display": "",
        "arrival_state": "",
        "arrival_state_label": "",
        "arrival_state_counts": [],
        "overdue_count": 0,
        "eta_display": "",
        "destination_label": "",
        "panel_url": "",
        "container_url": "",
    }
    point = _point(location.longitude, location.latitude, properties)
    return feature_collection([point] if point is not None else [])


def _panel_url(group) -> str:
    """The marker's container list, or "" when the place has no canonical identity.

    Empty rather than absent, so the browser's click handler reads one property and
    finds nothing to open — see ``loadPanel`` in ``map.js``. The panel lists what is
    standing at a *location*; a place a carrier merely named has nothing to list.
    """
    location_id = group.location_id
    if location_id is None:
        return ""
    return reverse("visibility:map_location_panel", args=[group.position_class, location_id])


def _position_point(position) -> dict:
    """One marker for a single container.

    Built as a group of one rather than with its own property builder, so a marker
    on the Container Workspace and the same marker on the Control Tower carry
    identical properties and the browser needs one set of layers for both.
    """
    from .map_positions import MapLocationGroup

    group = MapLocationGroup(
        position_class=position.position_class,
        # Non-None: only plottable positions reach here, and those have a place.
        place=position.place,
        positions=[position],
    )
    return _group_point(group)


def _class_statement(group) -> str:
    """The place in words for a marker covering several containers."""
    from .map_positions import PositionClass

    if group.position_class == PositionClass.DESTINATION:
        return str(_("Bound for %(place)s") % {"place": group.place_label})
    if group.position_class == PositionClass.TRACKING:
        return str(_("Last reported at %(place)s") % {"place": group.place_label})
    return str(_("At %(place)s") % {"place": group.place_label})


def _event_point(event: TrackingEvent, *, container_number: str = "") -> dict | None:
    """One carrier event as a map point, with its own quality, never upgraded.

    None when the event has no coordinates; callers pass located events only.
    """
    properties = {
        "object_type": "event",
        "event_id": event.pk,
        "container_number": container_number or (event.container.container_id if event.container else ""),
        "position_type": classify_position(event),
        "position_type_label": str(PositionType(classify_position(event)).label),
        "position_label": event.location_name or event.location_unlocode,
        "is_realtime": classify_position(event) == PositionType.GPS,
        **_event_properties(event),
    }
    return _point(event.location_longitude, event.location_latitude, properties)


def _journey_point_feature(point, *, container_number: str, is_current: bool) -> dict:
    """One journey point as a map point, with the source that reported it.

    ``source_label`` is pre-joined rather than a list: feature properties travel
    through Mapbox as data, and a string survives that trip unambiguously.
    """
    properties = {
        "object_type": "event",
        "event_id": point.event_id,
        "container_number": container_number,
        "position_type": point.position_type,
        "position_type_label": str(PositionType(point.position_type).label),
        "position_label": point.place_label,
        "is_realtime": point.position_type == PositionType.GPS,
        "source_kind": point.source.kind,
        "source_name": point.source.name,
        "source_label": " · ".join(point.source_names),
        # True for the one point the domain says the container is at now — which is
        # not always the newest point, and is never decided in the browser.
        "is_current": is_current,
        **_event_properties(point.event),
    }
    # A point built from an event keeps the event's own wording; a physical
    # observation has no event, so its own title and place stand in.
    if point.event is None:
        properties["event_title"] = str(point.title)
        properties["location_name"] = point.location_name
        properties["event_unlocode"] = point.location_unlocode
        properties["is_actual"] = point.is_actual
        properties["occurred_at"] = _datetime(point.occurred_at)
        properties["occurred_at_display"] = _datetime_display(point.occurred_at)
    return cast(dict, _point(point.longitude, point.latitude, properties))


def _point(longitude, latitude, properties: dict) -> dict | None:
    if longitude is None or latitude is None:
        return None
    return {
        "type": "Feature",
        # GeoJSON order: longitude, then latitude.
        "geometry": {"type": "Point", "coordinates": [_number(longitude), _number(latitude)]},
        "properties": properties,
    }


# ---------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------


def _line(events: list[TrackingEvent], line_type: str) -> dict | None:
    """Connect event coordinates in order, or return None if there is nothing to join.

    Deliberately not a route. The properties say so, and the map styles the two
    kinds differently, because a straight line between two ports is a drawing of
    what we know rather than of where the ship went.
    """
    coordinates: list[list[float]] = []
    for event in events:
        point = [_number(event.location_longitude), _number(event.location_latitude)]
        if not coordinates or coordinates[-1] != point:
            coordinates.append(point)
    if len(coordinates) < 2:
        return None
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coordinates},
        "properties": {
            "object_type": "connection",
            "line_type": line_type,
            "is_forecast": line_type == LINE_FORECAST,
            # Stated explicitly so nothing downstream mistakes this for AIS data.
            "is_vessel_track": False,
        },
    }


# ---------------------------------------------------------------------------
# Shared property builders
# ---------------------------------------------------------------------------


def _event_properties(event: TrackingEvent | None) -> dict:
    if event is None:
        return {
            "event_type": "",
            "event_title": "",
            "event_time_type": "",
            "carrier_reference": "",
            "is_actual": False,
            "is_estimated": False,
            "location_name": "",
            "event_unlocode": "",
            "event_vessel_name": "",
            "event_voyage_number": "",
            "occurred_at": None,
            "occurred_at_display": "",
        }
    return {
        "event_type": event.event_type,
        # The carrier's own wording where we could not classify it — never "Unknown".
        "event_title": event.display_title,
        "event_time_type": event.event_time_type,
        "carrier_reference": event.carrier_reference if event.is_unclassified else "",
        "is_actual": event.is_actual,
        "is_estimated": event.is_estimated,
        "location_name": event.location_name,
        "event_unlocode": event.location_unlocode,
        "event_vessel_name": event.vessel_name,
        "event_voyage_number": event.voyage_number,
        "occurred_at": _datetime(event.event_datetime),
        "occurred_at_display": _datetime_display(event.event_datetime),
    }


def _number(value) -> float:
    return float(value) if isinstance(value, Decimal) else float(value)


def _datetime(value) -> str | None:
    return value.isoformat() if value else None


def _datetime_display(value) -> str:
    if not value:
        return ""
    local = timezone.localtime(value) if timezone.is_aware(value) else value
    return date_format(local, "d M Y H:i")


def _date_display(value) -> str:
    return date_format(value, "d M Y") if value else ""


def _age_display(value) -> str:
    """How long ago, in Django's own wording — e.g. "6 hours ago".

    Rendered server-side so a marker's freshness matches the "x ago" on every panel
    beside it. There is deliberately no threshold here and no "stale" flag: nothing
    in the domain defines when a position becomes too old to trust, and inventing a
    number for the map would be a second, quieter answer to a question the tracking
    layer already declines to guess at.
    """
    if not value:
        return ""
    return str(_("%(age)s ago") % {"age": timesince(value)})
