"""Turning visibility read models into GeoJSON for Mapbox.

Three rules hold everywhere in this module.

**Longitude first.** GeoJSON coordinates are ``[longitude, latitude]``. Getting
this backwards puts Gothenburg in Somalia and looks plausible enough to ship.

**Properties are already decided.** Position quality, whether an event was observed
or forecast, what a status is called — all of it is resolved here, so the browser
never re-implements a domain rule to decide what to draw.

**A line between two ports is not a route.** We know where events happened, not
what path the vessel took. Connections are labelled as event connections, and the
forecast continuation is marked as forecast, so the map cannot be read as a track.

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

from apps.scm.tracking.models import TrackingEvent
from apps.scm.tracking.positions import PositionType, classify_position

from .read_models import ObjectKind, VisibilityObject

# Distinct positions closer together than this are treated as the same place when
# aggregating a shipment's containers — five decimals is about a metre.
_COORDINATE_PRECISION = 5

LINE_ACTUAL = "actual_event_connection"
LINE_FORECAST = "forecast_continuation"


def feature_collection(features: list[dict]) -> dict:
    """Wrap features in a FeatureCollection, the only shape we ever return."""
    return {"type": "FeatureCollection", "features": features}


def overview_feature_collection(objects: list[VisibilityObject]) -> dict:
    """Return current positions for every visible object.

    One feature per distinct place, not per container: twenty boxes discharged at
    the same terminal are one point carrying a count, while boxes that have gone
    separate ways stay separate points. Objects whose last report carries no
    coordinates are simply absent from the map — they are still in the list beside
    it, which is where "no coordinates available" belongs.
    """
    features = []
    for obj in objects:
        for group in _position_groups(obj):
            feature = _object_point(obj, group)
            if feature is not None:
                features.append(feature)
    return feature_collection(features)


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


def container_journey_feature_collection(journey, *, container_number: str = "") -> dict:
    """Return one container's journey as reported by every one of its sources.

    Built from the derived journey rather than from one provider's events, so a box
    that changed hands mid-voyage draws as one trip. Each point carries the source
    that reported it, and the point matching the derived current location is flagged
    so the map can mark it without deciding for itself which point that is.

    A point with no coordinates is absent from the map and present on the timeline,
    which is the authoritative chronology. Our own physical observations are always
    in that position: a container location records which yard holds the box, not
    where on earth the yard is.
    """
    number = container_number or (journey.container.container_id if journey.container is not None else "")
    current = journey.current_location
    current_point = current.point if current is not None else None

    features: list[dict] = []
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


def _position_groups(obj: VisibilityObject) -> list[dict]:
    """Group an object's containers by where they were last reported."""
    groups: dict[tuple, dict] = {}
    for workspace in obj.workspaces:
        position = workspace.position
        if position is None or not position.has_coordinates:
            continue
        # Both coordinates are set: that is what has_coordinates asserts.
        latitude = cast(Decimal, position.latitude)
        longitude = cast(Decimal, position.longitude)
        key = (
            position.location_unlocode
            or (
                round(float(latitude), _COORDINATE_PRECISION),
                round(float(longitude), _COORDINATE_PRECISION),
            ),
            position.position_type,
        )
        group = groups.setdefault(key, {"position": position, "containers": []})
        group["containers"].append(workspace.container)
    return list(groups.values())


def _object_point(obj: VisibilityObject, group: dict) -> dict | None:
    position = group["position"]
    containers = group["containers"]
    event = position.event

    properties = {
        "object_type": obj.kind,
        "object_id": obj.object_id,
        "object_key": obj.key,
        "label": obj.label,
        "container_number": containers[0].container_id if len(containers) == 1 else "",
        "container_count": len(containers),
        "total_container_count": obj.container_count,
        "carrier": obj.carrier_name,
        "vessel_name": obj.vessel_name,
        "vessel_imo": obj.vessel_imo,
        "voyage_number": obj.voyage_number,
        "current_status": obj.current_status,
        "journey_state": obj.journey_state,
        "journey_state_label": obj.journey_state_label,
        "health": obj.health,
        "health_label": obj.health_label,
        "is_delayed": obj.is_delayed,
        "delay_days": obj.delay_days,
        "exception_count": obj.exception_count,
        "eta": _date(obj.current_eta),
        "eta_display": _date_display(obj.current_eta),
        "eta_source": obj.eta_source,
        "tracking_state": obj.tracking_state,
        "tracking_state_label": obj.tracking_state_label,
        "last_synced_at": _datetime(obj.last_synced_at),
        "next_check_at": _datetime(obj.next_check_at),
        "panel_url": reverse("visibility:object_panel", args=[obj.kind, obj.object_id]) if obj.object_id else "",
        **_position_properties(position),
        **_event_properties(event),
    }
    return _point(position.longitude, position.latitude, properties)


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


def _position_properties(position) -> dict:
    return {
        "position_type": position.position_type,
        "position_type_label": position.get_position_type_display(),
        "position_label": position.label,
        "unlocode": position.location_unlocode,
        # True only for a GPS fix of the box itself; a terminal or a vessel is not one.
        "is_realtime": position.is_realtime,
        "observed_at": _datetime(position.observed_at),
        "observed_at_display": _datetime_display(position.observed_at),
    }


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


def _date(value) -> str | None:
    return value.isoformat() if value else None


def _date_display(value) -> str:
    return date_format(value, "d M Y") if value else ""


def object_detail_urls(obj: VisibilityObject) -> dict:
    """Links for an object's info card — only the ones that actually exist."""
    urls = {}
    if obj.kind == ObjectKind.CONTAINER and obj.container is not None:
        urls["container_url"] = reverse("containers:detail", args=[obj.container.pk])
    if obj.shipment is not None:
        urls["shipment_url"] = reverse("shipments:detail", args=[obj.shipment.pk])
    return urls
