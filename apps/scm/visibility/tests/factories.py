"""Shared setup for the visibility tests.

Events come from the sanitised real Maersk ``public-events`` response and are put
through the same parser and the same ingestion path production uses. That matters:
the fixture this replaced was invented, put location and vessel flat on the event
where Maersk nests them inside ``transportCall``, and hid a parser bug behind a
green suite. Visibility is tested against the shape carriers actually send.
"""

from __future__ import annotations

import json
import pathlib

from apps.scm.containers.choices import (
    LocationResolutionMethod,
    LocationResolutionStatus,
    LocationSource,
    LocationType,
    MovementType,
)
from apps.scm.containers.models import Container, ContainerLocation, EquipmentType
from apps.scm.containers.movements import record_container_movement
from apps.scm.tracking.ingestion import persist_normalised_events
from apps.scm.tracking.models import TrackingEvent, TrackingProvider, TrackingSubscription
from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

MAERSK_FIXTURE = (
    pathlib.Path(__file__).parents[3]
    / "scm"
    / "integrations"
    / "tests"
    / "fixtures"
    / "carriers"
    / "maersk_public_events_response.json"
)

# The container number the fixture is about — Maersk's own published test reference.
FIXTURE_CONTAINER_NUMBER = "TRDU9258963"

TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def maersk_payload() -> dict:
    return json.loads(MAERSK_FIXTURE.read_text())


def payload_in_transit(eta: str = "2026-08-21T06:00:00+02:00") -> dict:
    """The same journey as the fixture, captured before the vessel arrived.

    The stored response is a completed journey: it carries an actual arrival, so
    the carrier's forecast has already been answered and there is correctly no ETA
    left to show. Testing an outstanding ETA needs the earlier snapshot.

    Built by removing the post-arrival events from the real response and turning
    its arrival into a forecast — rather than by inventing a flat event, which is
    the mistake that once hid a parser bug behind a green suite. Every event still
    has the carrier's own nested ``transportCall`` shape and still goes through the
    same parser.
    """
    payload = maersk_payload()
    arrival = next(
        event
        for event in payload["events"]
        if event.get("transportEventTypeCode") == "ARRI" and event.get("eventClassifierCode") == "ACT"
    )
    forecast = json.loads(json.dumps(arrival))
    forecast["eventClassifierCode"] = "EST"
    forecast["eventDateTime"] = eta

    payload["events"] = [
        event
        for event in payload["events"]
        # Everything that only happens at the destination end of the voyage.
        if event is not arrival and (event.get("transportCall") or {}).get("UNLocationCode") != "SEGOT"
    ]
    payload["events"].append(forecast)
    return payload


def make_user_and_team(username: str, team_slug: str) -> tuple[CustomUser, Team]:
    team = Team.objects.create(name=team_slug, slug=team_slug)
    user = CustomUser.objects.create_user(username=username, password="pass")
    team.members.add(user, through_defaults={"role": ROLE_MEMBER})
    return user, team


def equipment_type() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def with_check_digit(body: str) -> str:
    """Complete a ten-character ISO 6346 body into a valid container number.

    Computed rather than hard-coded so a test needing a hundred containers does not
    need a table of magic numbers. Lives here rather than in one test module because
    more than one of them generates fleets now.
    """
    from apps.scm.containers.utils import calculate_check_digit

    digit = calculate_check_digit(body[:3], body[3], body[4:10])
    return f"{body[:10]}{digit}"


def make_container(team: Team, number: str = FIXTURE_CONTAINER_NUMBER) -> Container:
    """Create the Container the fixture's events belong to."""
    return Container.objects.create(
        team=team,
        owner_code=number[:3],
        category_id=number[3],
        serial_number=number[4:10],
        check_digit=int(number[10]),
        equipment_type=equipment_type(),
    )


def make_provider(code: str = "maersk", name: str = "Maersk") -> TrackingProvider:
    return TrackingProvider.objects.get_or_create(code=code, defaults={"name": name})[0]


# ---------------------------------------------------------------------------
# Canonical locations, physical state and resolved evidence
#
# LOC-4 draws canonical places, so its tests need canonical places. Coordinates are
# passed in by each test rather than defaulted to somewhere real: a factory that
# quietly supplied Gothenburg's latitude would make "this location has no
# coordinates" untestable, and that is the state most of the data is actually in.
# ---------------------------------------------------------------------------


def make_location(
    team: Team,
    name: str,
    *,
    latitude: str | None = None,
    longitude: str | None = None,
    unlocode: str = "",
    parent: ContainerLocation | None = None,
    location_type: str = LocationType.TERMINAL,
) -> ContainerLocation:
    """Create one canonical location. Coordinates are omitted unless asked for."""
    return ContainerLocation.objects.create(
        team=team,
        name=name,
        location_type=location_type,
        unlocode=unlocode,
        parent_location=parent,
        latitude=latitude,
        longitude=longitude,
    )


def place_container_at(
    team: Team,
    container: Container,
    location: ContainerLocation | None,
    *,
    occurred_at=None,
    movement_type: str = MovementType.GATE_IN,
    source: str = LocationSource.MANUAL,
    related_shipment=None,
):
    """Record an accepted physical movement, through the real service.

    Deliberately not ``Container.objects.update(current_location=...)``: the whole
    point of a PHYSICAL position is that it is the *projection* of the movement
    history, and a test that set the column directly would prove the map can read a
    column rather than that it agrees with LOC-2.
    """
    return record_container_movement(
        team=team,
        container=container,
        movement_type=movement_type,
        to_location=location,
        occurred_at=occurred_at,
        source=source,
        related_shipment=related_shipment,
    )


def resolve_tracking_to(
    team: Team,
    container: Container,
    location: ContainerLocation,
    *,
    event_type: str | None = None,
    status: str = LocationResolutionStatus.RESOLVED,
) -> TrackingEvent | None:
    """Point the container's newest observed located event at a canonical location.

    Stands in for the resolver having succeeded, which in production happens during
    ingestion. Takes ``status`` so a test can produce the states the resolver really
    does produce — AMBIGUOUS leaves ``location`` NULL, exactly as
    ``resolve_location`` does, because that is the case a canonical marker must
    refuse.
    """
    events = TrackingEvent.objects.filter(
        team=team,
        container=container,
        event_time_type=TrackingEvent.EventTimeType.ACTUAL,
        event_datetime__isnull=False,
    )
    if event_type is not None:
        events = events.filter(event_type=event_type)
    event = events.order_by("-event_datetime", "-created_at").first()
    if event is None:
        return None
    event.location = location if status == LocationResolutionStatus.RESOLVED else None
    event.location_resolution_status = status
    event.location_resolution_method = LocationResolutionMethod.UNLOCODE
    event.save(update_fields=["location", "location_resolution_status", "location_resolution_method"])
    return event


def payload_for_container(payload: dict, container_number: str) -> dict:
    """Retarget a captured response at another container number.

    Ingestion deduplicates on the carrier's event ID, which is correct and is why
    replaying one captured response for several containers would otherwise produce
    one set of events shared between them. A real carrier issues distinct event IDs
    per equipment, so the ids are suffixed here and the equipment references
    rewritten — the structure the parser reads is untouched.
    """
    if container_number == FIXTURE_CONTAINER_NUMBER:
        return payload

    rewritten = json.loads(json.dumps(payload))
    for event in rewritten["events"]:
        event["eventID"] = f"{event['eventID']}-{container_number}"
        if event.get("equipmentReference"):
            event["equipmentReference"] = container_number
        for reference in event.get("references") or []:
            if reference.get("referenceType") == "EQ":
                reference["referenceValue"] = container_number
    return rewritten


def ingest_maersk_events(
    team: Team,
    container: Container,
    *,
    shipment=None,
    payload: dict | None = None,
) -> TrackingSubscription:
    """Run a real carrier payload through the real parser and ingestion path."""
    from apps.scm.integrations.carriers.maersk.parser import MaerskParser

    provider = make_provider()
    subscription = TrackingSubscription.objects.create(
        team=team,
        provider=provider,
        container=container,
        shipment=shipment,
        tracking_reference=container.container_id,
        status=TrackingSubscription.Status.ACTIVE,
        tracking_status=TrackingSubscription.TrackingStatus.TRACKING,
    )
    # Same arguments the sync engine passes, so events are linked to container and
    # shipment exactly as they are in production.
    persist_normalised_events(
        team=team,
        provider=provider,
        events=MaerskParser().parse_tracking_events(
            payload_for_container(payload or maersk_payload(), container.container_id)
        ),
        subscription=subscription,
        shipment=subscription.shipment,
        container=subscription.container,
    )
    return subscription
