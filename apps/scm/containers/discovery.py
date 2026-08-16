"""Discovery of planned container numbers.

A planned container is a container number we expect to see (from an order, a
supplier, a packing list) but which the carrier may not know about yet. This
service polls carriers until the number appears, then promotes it to a real
Container with tracking attached:

    planned → detected → in_transit → arrived
            ↘ expired (gave up: too many attempts, or past its expiry)

It is one of two discovery use cases and deliberately kept separate from
shipment-based discovery in
``apps.scm.integrations.carriers.discovery_service``, which starts from a
booking or bill of lading instead of a container number. Both share the same
carrier registry, credential resolution, probe semantics and auto-link service —
only the starting reference differs.

Which carrier is asked is not this module's decision either. ``PlannedContainer``
may name one, but a number registered from a packing list often does not, and the
one it names can be wrong. So each pass hands the question to the shared
:func:`~apps.scm.integrations.carriers.carrier_discovery.discover_carrier_for_container`
— the same sweep the container detail page's "Refresh tracking" uses — which tries
the planned carrier first and falls back to the team's other connected carriers.
The carrier that actually answers is the one the container is promoted with.

One pass is one attempt, however many carriers it had to ask: the attempt budget
and backoff exist to limit how often *this number* is chased, not how many carriers
a single chase touches. Each carrier is asked at most once per pass, so a carrier
that rate-limits us is not hammered — it simply waits for the next interval like
everyone else.

Nothing is invented when a carrier has no data: no container, no shipment and no
event is created until the carrier actually reports the number.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.teams.models import Team

from .models import PlannedContainer, PlannedContainerResult, PlannedContainerStatus
from .utils import parse_container_id, validate_container_id

logger = logging.getLogger(__name__)

# How long to wait between attempts, and how many to make before giving up.
DEFAULT_CHECK_INTERVAL_MINUTES = 360  # 6h — a booked box rarely appears sooner
DEFAULT_MAX_ATTEMPTS = 40  # ~10 days at the default interval
# Back off further once a number has been looked for repeatedly without success.
MAX_CHECK_INTERVAL_MINUTES = 1440


# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------


def get_planned_containers(team: Team, status: str | None = None) -> QuerySet[PlannedContainer]:
    """Return planned containers for a team, optionally filtered by status."""
    qs = PlannedContainer.objects.filter(team=team).select_related("shipment", "container")
    if status:
        qs = qs.filter(status=status)
    return qs.order_by("-created_at")


def get_planned_containers_for_discovery(team: Team | None = None) -> QuerySet[PlannedContainer]:
    """Return planned containers that are due for another discovery attempt.

    Due means: still PLANNED, and either never checked or past ``next_check_at``.
    Numbers that have exhausted their attempts or passed their expiry are excluded
    (they are expired by :func:`expire_exhausted_planned_containers`).
    """
    now = timezone.now()
    qs = PlannedContainer.objects.filter(status=PlannedContainerStatus.PLANNED).filter(
        Q(next_check_at__isnull=True) | Q(next_check_at__lte=now)
    )
    if team is not None:
        qs = qs.filter(team=team)
    return qs.select_related("team", "shipment").order_by("created_at")


def max_attempts_for(planned: PlannedContainer) -> int:
    return planned.max_attempts or DEFAULT_MAX_ATTEMPTS


def is_exhausted(planned: PlannedContainer, *, now=None) -> bool:
    """True when we should stop looking for this container number."""
    now = now or timezone.now()
    if planned.expires_at and planned.expires_at <= now:
        return True
    return planned.attempts >= max_attempts_for(planned)


# ---------------------------------------------------------------------------
# Lifecycle services
# ---------------------------------------------------------------------------


def add_planned_container(
    team: Team,
    container_number: str,
    carrier: str = "",
    shipment=None,
    notes: str = "",
    *,
    max_attempts: int | None = None,
    expires_in_days: int | None = None,
) -> PlannedContainer:
    """Register a container number to watch for.

    The number is validated as ISO 6346 (format *and* check digit) and normalised
    to upper case before storage — a typo caught here saves days of pointless
    carrier polling.

    ``carrier`` is the carrier to ask. When omitted, the owner prefix is used only
    as a suggestion; an explicit value always wins.

    Raises ValidationError for an invalid container number. Registering an existing
    number returns the existing record unchanged.
    """
    normalised = (container_number or "").strip().upper()
    parts = parse_container_id(normalised)  # raises ValidationError on bad format
    validate_container_id(
        parts["owner_code"],
        parts["category_id"],
        parts["serial_number"],
        parts["check_digit"],
    )

    carrier_code = _resolve_carrier(carrier=carrier, owner_code=normalised[:4])
    expires_at = timezone.now() + timedelta(days=expires_in_days) if expires_in_days else None

    planned, created = PlannedContainer.objects.get_or_create(
        team=team,
        container_number=normalised,
        defaults={
            "carrier": carrier_code,
            "shipment": shipment,
            "notes": notes,
            "max_attempts": max_attempts,
            "expires_at": expires_at,
        },
    )
    if created:
        logger.info(
            "Added planned container %s for team %s (carrier=%s)",
            normalised,
            team.slug,
            carrier_code or "unknown",
        )
    return planned


def _resolve_carrier(*, carrier: str, owner_code: str) -> str:
    """Return the carrier to ask: the chosen one, else a prefix-based suggestion."""
    from apps.scm.integrations.carriers.registry import resolve_carrier_code, suggest_carrier_for_owner_code

    if carrier:
        # Keep the user's value even if it is not a registered code — the probe will
        # report it as unknown rather than silently substituting another carrier.
        return resolve_carrier_code(carrier) or carrier
    return suggest_carrier_for_owner_code(owner_code) or ""


def mark_planned_container_detected(planned: PlannedContainer, container=None, carrier: str = "") -> PlannedContainer:
    """Transition a planned container from PLANNED → DETECTED.

    ``carrier`` records which carrier actually reported the number, which may differ
    from the one it was registered with. Unlike a guess before the fact, this is the
    carrier's own answer, so it is worth keeping.
    """
    now = timezone.now()
    planned.status = PlannedContainerStatus.DETECTED
    planned.last_result = PlannedContainerResult.DETECTED
    planned.detected_at = now
    planned.last_checked_at = now
    planned.next_check_at = None
    planned.last_error_message = ""
    if container is not None:
        planned.container = container
    if carrier:
        planned.carrier = carrier
    planned.save(
        update_fields=[
            "status",
            "last_result",
            "detected_at",
            "last_checked_at",
            "next_check_at",
            "last_error_message",
            "container",
            "carrier",
            "updated_at",
        ]
    )
    return planned


def mark_planned_container_in_transit(planned: PlannedContainer) -> PlannedContainer:
    """Transition DETECTED → IN_TRANSIT when tracking shows movement."""
    planned.status = PlannedContainerStatus.IN_TRANSIT
    planned.last_checked_at = timezone.now()
    planned.save(update_fields=["status", "last_checked_at", "updated_at"])
    return planned


def mark_planned_container_arrived(planned: PlannedContainer) -> PlannedContainer:
    """Transition IN_TRANSIT → ARRIVED when arrival event is confirmed."""
    planned.status = PlannedContainerStatus.ARRIVED
    planned.last_checked_at = timezone.now()
    planned.save(update_fields=["status", "last_checked_at", "updated_at"])
    return planned


def cancel_planned_container(planned: PlannedContainer) -> PlannedContainer:
    """Cancel a planned container (terminal state)."""
    planned.status = PlannedContainerStatus.CANCELLED
    planned.next_check_at = None
    planned.save(update_fields=["status", "next_check_at", "updated_at"])
    return planned


def expire_planned_container(planned: PlannedContainer, reason: str = "") -> PlannedContainer:
    """Stop looking for a container number that never appeared."""
    planned.status = PlannedContainerStatus.EXPIRED
    planned.next_check_at = None
    planned.last_error_message = reason
    planned.save(update_fields=["status", "next_check_at", "last_error_message", "updated_at"])
    logger.info("Planned container %s expired: %s", planned.container_number, reason or "attempts exhausted")
    return planned


# ---------------------------------------------------------------------------
# Discovery run
# ---------------------------------------------------------------------------


def run_discovery_for_team(team: Team, providers: list | None = None) -> dict:
    """Run one discovery pass over the team's due planned containers.

    ``providers`` may inject carrier clients for testing: a list of clients whose
    ``provider_code`` is matched against the planned container's carrier. In
    production the team's configured adapter is resolved through the carrier
    factory instead.

    Returns a summary dict: checked, detected, not_found, skipped, expired, errors.
    """
    injected = {client.provider_code: client for client in (providers or []) if getattr(client, "provider_code", "")}
    summary: dict[str, Any] = {"checked": 0, "detected": 0, "not_found": 0, "skipped": 0, "expired": 0, "errors": []}

    for planned in get_planned_containers_for_discovery(team=team):
        if is_exhausted(planned):
            expire_planned_container(planned, reason="No carrier data within the configured attempts/timeout.")
            summary["expired"] += 1
            continue

        summary["checked"] += 1
        try:
            outcome = check_planned_container(planned, client=injected.get(planned.carrier))
        except Exception as exc:  # noqa: BLE001 — one container must not stop the pass
            message = f"{planned.container_number}: {type(exc).__name__}: {exc}"
            logger.exception("Discovery error for %s", planned.container_number)
            summary["errors"].append(message)
            _record_attempt(planned, result=PlannedContainerResult.ERROR, error_message=message)
            continue

        if outcome == PlannedContainerResult.DETECTED:
            summary["detected"] += 1
        elif outcome == PlannedContainerResult.NOT_FOUND:
            summary["not_found"] += 1
        elif outcome == PlannedContainerResult.SKIPPED:
            summary["skipped"] += 1
        else:
            summary["errors"].append(f"{planned.container_number}: {planned.last_error_message}")

    return summary


def check_planned_container(planned: PlannedContainer, *, client=None) -> str:
    """Ask the candidate carriers about one planned container and record the outcome.

    The planned carrier is tried first when there is one; a NOT_FOUND from it is not
    the end of the pass, because the number may well be moving with a different
    carrier than the one recorded. Whichever carrier answers with data is the one the
    container is promoted with.

    Returns the PlannedContainerResult that was recorded, summarising the whole pass
    as a single attempt. On a hit, the Container, its shipment link and its tracking
    subscription are created atomically.

    ``client`` injects an adapter for the planned carrier in tests.
    """
    from apps.scm.integrations.carriers.carrier_discovery import discover_carrier_for_container

    outcome = discover_carrier_for_container(
        team=planned.team,
        container_number=planned.container_number,
        preferred_carrier_codes=[planned.carrier] if planned.carrier else (),
        clients={planned.carrier: client} if client is not None and planned.carrier else None,
    )

    if outcome.found:
        _promote_detected_container(planned, outcome)
        return PlannedContainerResult.DETECTED

    if outcome.not_found:
        # At least one carrier gave a real answer, so the pass counts — even if
        # another carrier happened to be unreachable at the same time.
        _record_attempt(planned, result=PlannedContainerResult.NOT_FOUND)
        return PlannedContainerResult.NOT_FOUND

    if outcome.errored:
        _record_attempt(
            planned,
            result=PlannedContainerResult.ERROR,
            error_message=_attempt_summary(outcome.errored),
        )
        return PlannedContainerResult.ERROR

    # Nothing was asked, so this does not count as an attempt against the limit.
    _record_attempt(
        planned,
        result=PlannedContainerResult.SKIPPED,
        error_message=_attempt_summary(outcome.skipped),
        count_attempt=False,
    )
    return PlannedContainerResult.SKIPPED


def _attempt_summary(attempts) -> str:
    """Summarise why carriers did not answer, for the record on the planned number.

    Internal diagnostics: carrier error text can echo a response body, so this is
    stored and logged but never rendered to a user.
    """
    return "; ".join(attempt.error_message or attempt.error_kind or attempt.carrier_code for attempt in attempts)


def _promote_detected_container(planned: PlannedContainer, outcome) -> None:
    """Create the Container, its shipment link and its tracking subscription.

    Uses the carrier that actually returned data, which is not always the carrier
    the number was registered with. Done in one transaction so a partial promotion
    cannot leave a container without tracking, or tracking without a container.
    """
    from apps.scm.integrations.carriers.auto_link import create_or_link_discovered_container
    from apps.scm.integrations.carriers.schemas import ContainerDiscoveryResult

    discovery_result = ContainerDiscoveryResult(
        container_number=planned.container_number,
        carrier_code=outcome.carrier_code,
        carrier_name=outcome.carrier_name or outcome.carrier_code,
        shipment_reference=planned.shipment.reference if planned.shipment else None,
    )

    with transaction.atomic():
        link_summary = create_or_link_discovered_container(
            team=planned.team,
            shipment=planned.shipment,
            result=discovery_result,
        )
        container = link_summary.get("container")
        mark_planned_container_detected(planned, container=container, carrier=outcome.carrier_code)

    logger.info(
        "Planned container %s detected at %s (container_created=%s, subscription_created=%s)",
        planned.container_number,
        outcome.carrier_code,
        link_summary.get("container_created"),
        link_summary.get("subscription_created"),
    )


def _record_attempt(
    planned: PlannedContainer,
    *,
    result: str,
    error_message: str = "",
    count_attempt: bool = True,
) -> None:
    """Record one discovery attempt and schedule the next check."""
    now = timezone.now()
    if count_attempt:
        planned.attempts += 1
    planned.last_checked_at = now
    planned.last_result = result
    planned.last_error_message = error_message
    planned.next_check_at = _next_check_at(planned)
    planned.save(
        update_fields=[
            "attempts",
            "last_checked_at",
            "last_result",
            "last_error_message",
            "next_check_at",
            "updated_at",
        ]
    )

    if is_exhausted(planned, now=now):
        expire_planned_container(planned, reason="No carrier data within the configured attempts/timeout.")


def _next_check_at(planned: PlannedContainer):
    """Space out checks, widening the gap the longer a number stays unknown."""
    minutes = DEFAULT_CHECK_INTERVAL_MINUTES
    if planned.attempts > 10:
        minutes = min(MAX_CHECK_INTERVAL_MINUTES, DEFAULT_CHECK_INTERVAL_MINUTES * 2)
    return timezone.now() + timedelta(minutes=minutes)


def expire_exhausted_planned_containers(team: Team | None = None) -> int:
    """Expire planned containers that ran out of attempts or passed their expiry.

    Returns the number expired. Runs independently of a discovery pass so a paused
    or backlogged queue still gets cleaned up.
    """
    now = timezone.now()
    qs = PlannedContainer.objects.filter(status=PlannedContainerStatus.PLANNED)
    if team is not None:
        qs = qs.filter(team=team)

    expired = 0
    for planned in qs.iterator(chunk_size=200):
        if is_exhausted(planned, now=now):
            expire_planned_container(planned, reason="No carrier data within the configured attempts/timeout.")
            expired += 1
    return expired
