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

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.tracking.ingestion import persist_normalised_events
from apps.scm.tracking.models import TrackingProvider, TrackingSubscription
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
