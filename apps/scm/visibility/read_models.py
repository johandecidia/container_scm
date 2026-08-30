"""What visibility shows for one thing that is moving.

A :class:`VisibilityObject` is either a shipment with its containers, or a
container tracked on its own. Both appear on the map and in the list, and neither
is a second-class citizen: a container with no shipment still has a status, an
ETA, a vessel and a position, because the carrier told us all four.

Nothing here computes tracking facts. Every value is read from a
:class:`~apps.scm.containers.workspace.ContainerWorkspace`, from the shipment, or
from the existing delay and exception engines. This module decides only how those
answers are grouped and labelled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from django.db.models import TextChoices
from django.utils.translation import gettext_lazy as _

from apps.scm.containers.workspace import ContainerWorkspace
from apps.scm.shipments.models import Shipment
from apps.scm.tracking.delay_detection import DelayReport
from apps.scm.tracking.exception_detection import ExceptionIssue, ExceptionReport


class ObjectKind(TextChoices):
    SHIPMENT = "shipment", _("Shipment")
    CONTAINER = "container", _("Container")


class JourneyState(TextChoices):
    """Where something is in its journey, coarsely, for filtering and colouring.

    A presentation grouping of the status the tracking layer already derived — not
    a status in its own right, and never shown in place of one. The list and the
    map cards print ``current_status``, which is the carrier's own wording.
    """

    NOT_DEPARTED = "not_departed", _("Not departed")
    IN_TRANSIT = "in_transit", _("In transit")
    ARRIVED = "arrived", _("Arrived")
    DELIVERED = "delivered", _("Delivered")
    UNKNOWN = "unknown", _("Unknown")


def journey_state_from_observed(observed: set[str]) -> str:
    """Return the furthest milestone a set of *observed* event types proves.

    Milestones, not the most recent code, because carriers reuse codes at both ends of
    a journey: a box gated in at Gothenburg after discharge would otherwise bucket as
    "not departed". The milestone tuples are the shipment transport rules, imported
    rather than restated.

    A function rather than only a read-model property because the same question is
    asked outside the visibility read models — the Traqo benchmark has to decide
    whether a container is genuinely in transit before spending a provider request on
    it, and a second copy of this ordering would eventually disagree about what
    "arrived" means.
    """
    from apps.scm.shipments.transport_status import (
        ARRIVAL_EVENT_TYPES,
        DELIVERY_EVENT_TYPES,
        DEPARTURE_EVENT_TYPES,
    )

    if observed.intersection(DELIVERY_EVENT_TYPES):
        return JourneyState.DELIVERED
    if observed.intersection(ARRIVAL_EVENT_TYPES):
        return JourneyState.ARRIVED
    if observed.intersection(DEPARTURE_EVENT_TYPES):
        return JourneyState.IN_TRANSIT
    if observed:
        # Something has happened — a booking, an empty release, a gate move — but
        # nothing that proves the box has left.
        return JourneyState.NOT_DEPARTED
    return JourneyState.UNKNOWN


class Health(TextChoices):
    """How the arrival is going, where the domain can honestly say.

    There is deliberately no AT RISK: nothing in the domain distinguishes "likely
    to slip" from "on time", and inventing the distinction would make the platform
    look more certain than its data. UNKNOWN is used when there is no ETA to judge
    against, which is a different thing from being on time.
    """

    ON_TIME = "on_time", _("On time")
    DELAYED = "delayed", _("Delayed")
    EXCEPTION = "exception", _("Exception")
    UNKNOWN = "unknown", _("Unknown")


_STATE_BY_SHIPMENT_STATUS: dict[str, JourneyState] = {
    Shipment.Status.DRAFT: JourneyState.NOT_DEPARTED,
    Shipment.Status.BOOKED: JourneyState.NOT_DEPARTED,
    Shipment.Status.IN_TRANSIT: JourneyState.IN_TRANSIT,
    Shipment.Status.ARRIVED: JourneyState.ARRIVED,
    Shipment.Status.PARTIALLY_RECEIVED: JourneyState.ARRIVED,
    Shipment.Status.DELIVERED: JourneyState.DELIVERED,
}


@dataclass
class VisibilityObject:
    """A shipment or a standalone container, as the visibility layer sees it."""

    kind: str
    workspaces: list[ContainerWorkspace] = field(default_factory=list)
    shipment: Shipment | None = None
    delay: DelayReport | None = None
    exceptions: ExceptionReport | None = None

    # -- identity ----------------------------------------------------------

    @property
    def key(self) -> str:
        """A stable id for map features and HTMX targets."""
        return f"{self.kind}-{self.object_id}"

    @property
    def object_id(self) -> int | None:
        if self.kind == ObjectKind.SHIPMENT:
            return self.shipment.pk if self.shipment else None
        return self.container.pk if self.container else None

    @property
    def container(self):
        """The single container, for a standalone container object."""
        return self.workspaces[0].container if self.workspaces else None

    @property
    def containers(self) -> list:
        return [workspace.container for workspace in self.workspaces]

    @property
    def container_count(self) -> int:
        return len(self.workspaces)

    @property
    def label(self) -> str:
        if self.kind == ObjectKind.SHIPMENT and self.shipment is not None:
            return str(self.shipment)
        container = self.container
        return container.container_id if container else ""

    @property
    def lead(self) -> ContainerWorkspace | None:
        """The container whose tracking speaks for the object.

        The one with the most recent observed event: on a shipment whose boxes were
        discharged at different times, the newest event is the one that describes
        where the shipment has got to.
        """
        dated: list[tuple[datetime, ContainerWorkspace]] = []
        for workspace in self.workspaces:
            event = workspace.latest_tracking_event
            if event is not None and event.event_datetime is not None:
                dated.append((event.event_datetime, workspace))
        if dated:
            return max(dated, key=lambda pair: pair[0])[1]
        return self.workspaces[0] if self.workspaces else None

    # -- carrier and carriage ---------------------------------------------

    @property
    def carrier_name(self) -> str:
        """The carrier that actually returns events, else the one on the shipment."""
        for workspace in self.workspaces:
            if workspace.tracking_carrier_name:
                return workspace.tracking_carrier_name
        return self.shipment.carrier if self.shipment else ""

    @property
    def is_tracked(self) -> bool:
        return any(workspace.is_tracked for workspace in self.workspaces)

    @property
    def vessel_name(self) -> str:
        lead = self.lead
        return lead.vessel_name if lead else ""

    @property
    def vessel_imo(self) -> str:
        lead = self.lead
        event = lead.carriage_event if lead else None
        return event.vessel_imo if event else ""

    @property
    def voyage_number(self) -> str:
        lead = self.lead
        return lead.voyage_number if lead else ""

    # -- state -------------------------------------------------------------

    @property
    def current_status(self) -> str:
        """Where this is in its journey, in the carrier's own words where we have them.

        Tracking leads and the shipment's transport status stands in — the rule the
        container workspace already applies. Nothing here reads a forecast.
        """
        lead = self.lead
        tracked = lead.tracking_current_status if lead else ""
        if tracked:
            return tracked
        return self.shipment.get_status_display() if self.shipment else ""

    @property
    def observed_event_types(self) -> set[str]:
        """Every event type the carrier has actually observed, across containers."""
        observed: set[str] = set()
        for workspace in self.workspaces:
            observed |= workspace.observed_event_types
        return observed

    @property
    def journey_state(self) -> str:
        """The furthest milestone the carrier has confirmed, not the latest event.

        Only observed events count. A forecast arrival is a forecast. Where nothing
        has been observed at all, the shipment's own status is the fallback.
        """
        state = journey_state_from_observed(self.observed_event_types)
        if state == JourneyState.UNKNOWN and self.shipment is not None:
            return _STATE_BY_SHIPMENT_STATUS.get(self.shipment.status, JourneyState.UNKNOWN)
        return state

    @property
    def journey_state_label(self) -> str:
        return str(JourneyState(self.journey_state).label)

    @property
    def position(self):
        lead = self.lead
        return lead.position if lead else None

    @property
    def latest_event(self):
        lead = self.lead
        return lead.latest_tracking_event if lead else None

    @property
    def latest_actual_event(self):
        lead = self.lead
        return lead.latest_meaningful_actual_event if lead else None

    # -- arrival -----------------------------------------------------------

    @property
    def current_eta(self):
        """The shipment's planned date when there is one, else the carrier's forecast.

        The same priority the container workspace uses, anchored to *this* object's
        shipment so a container that also appears on an older shipment cannot pull
        in that shipment's date.
        """
        if self.shipment is not None and self.shipment.eta:
            return self.shipment.eta
        lead = self.lead
        return lead.tracking_eta if lead else None

    @property
    def current_eta_at(self):
        """The carrier's forecast to the hour, when the ETA came from tracking."""
        lead = self.lead
        return lead.tracking_eta_at if lead and self.eta_source == "tracking" else None

    @property
    def eta_source(self) -> str:
        if self.shipment is not None and self.shipment.eta:
            return "shipment"
        lead = self.lead
        return "tracking" if lead and lead.tracking_eta else ""

    @property
    def original_eta(self):
        return self.shipment.original_eta if self.shipment else None

    @property
    def eta_change_days(self) -> int | None:
        """How much later arrival is expected than first forecast, or None."""
        current, original = self.current_eta, self.original_eta
        return (current - original).days if current and original else None

    # -- freshness ---------------------------------------------------------

    @property
    def last_synced_at(self):
        """The most recent time any of this object's carriers was asked."""
        times = [w.last_refreshed_at for w in self.workspaces if w.last_refreshed_at]
        return max(times) if times else None

    @property
    def last_event_at(self):
        event = self.latest_event
        return event.event_datetime if event else None

    @property
    def next_check_at(self):
        """The soonest scheduled check across this object's live watches."""
        times = [w.next_check_at for w in self.workspaces if w.next_check_at]
        return min(times) if times else None

    @property
    def tracking_state(self) -> str:
        """The carrier-side tracking status code, e.g. ``no_data`` or ``error``.

        Kept as a code so the UI can tell NO_DATA, NOT_CONFIGURED and ERROR apart —
        they mean different things and must not collapse into one "no tracking".
        """
        lead = self.lead
        subscription = lead.active_subscription if lead else None
        return subscription.tracking_status if subscription else ""

    @property
    def tracking_state_label(self) -> str:
        lead = self.lead
        return lead.tracking_status if lead else ""

    @property
    def has_tracking_error(self) -> bool:
        return any(workspace.has_tracking_error for workspace in self.workspaces)

    # -- health ------------------------------------------------------------

    @property
    def is_delayed(self) -> bool:
        return bool(self.delay and self.delay.is_delayed)

    @property
    def delay_days(self) -> int:
        return self.delay.eta_drift_days if self.delay else 0

    @property
    def delay_reason(self) -> str:
        return self.delay.reason if self.delay else ""

    @property
    def exception_types(self) -> list[str]:
        return self.exceptions.exception_types if self.exceptions else []

    @property
    def exception_details(self) -> list[str]:
        return self.exceptions.details if self.exceptions else []

    @property
    def exception_issues(self) -> list[ExceptionIssue]:
        """Each exception paired with the engine's reason for raising it.

        What a work queue row needs: the flat type and detail lists de-duplicate
        differently across a shipment's containers, so they cannot be zipped.
        """
        return self.exceptions.issues if self.exceptions else []

    @property
    def exception_count(self) -> int:
        return len(self.exception_types)

    @property
    def has_exception(self) -> bool:
        return bool(self.exceptions and self.exceptions.has_exception)

    @property
    def health(self) -> str:
        if self.has_exception:
            return Health.EXCEPTION
        if self.is_delayed:
            return Health.DELAYED
        if self.current_eta is None:
            return Health.UNKNOWN
        return Health.ON_TIME

    @property
    def health_label(self) -> str:
        return str(Health(self.health).label)
