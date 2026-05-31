# Timeline helpers — return tracking events as timeline-ready items.
# These are consumed by shipment detail views to display a unified timeline.
from dataclasses import dataclass, field
from datetime import datetime

from apps.teams.models import Team

from .models import TrackingEvent
from .selectors import get_tracking_events_for_shipment


@dataclass
class TrackingTimelineItem:
    """A normalised representation of a tracking event for display in a shipment timeline."""

    type: str
    title: str
    description: str
    datetime: datetime | None
    location: str
    source: str
    event_type: str
    raw_event: TrackingEvent | None = field(default=None, repr=False)


def get_tracking_timeline_items_for_shipment(team: Team, shipment) -> list[TrackingTimelineItem]:
    """Return tracking events for a shipment as timeline-ready items, sorted by datetime descending."""
    events = get_tracking_events_for_shipment(team=team, shipment=shipment)
    items = []
    for event in events:
        location = event.location_name
        if event.location_unlocode:
            location = f"{location} ({event.location_unlocode})" if location else event.location_unlocode
        items.append(
            TrackingTimelineItem(
                type="tracking",
                title=event.get_event_type_display(),
                description=event.description or event.status,
                datetime=event.event_datetime,
                location=location,
                source=event.provider.name if event.provider_id else "",
                event_type=event.event_type,
                raw_event=event,
            )
        )
    # Already ordered by -event_datetime from the selector, but sort None datetimes to the end.
    items.sort(key=lambda i: (i.datetime is None, -(i.datetime.timestamp() if i.datetime else 0)))
    return items
