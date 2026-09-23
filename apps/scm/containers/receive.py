"""Receiving containers at a canonical location: one, or a pasted report of many.

A receive is a ``GATE_IN`` movement, recorded through
:func:`~apps.scm.containers.movements.record_container_movement` like every other
physical movement. Nothing here writes ``current_location``, defines what "arrived"
means or decides which movement wins; the projection and the arrival lifecycle read
the movement exactly as they read one typed into the movement modal.

What this module adds is what a *report* of receives needs and a single movement
does not:

**Idempotency.** A receive is identified by container, movement type, destination
and ``occurred_at``. :func:`receive_container` checks for that movement under the
same container row lock ``record_container_movement`` takes, so the same report
pasted twice — or confirmed twice in two tabs — records each receive once.

**The pasted evidence stays evidence.** The report's site, ISO size and type are
compared with what MCR already knows and produce warnings; they never create a
location, a container or change a container's equipment type.

**Tracking is secondary.** After a *new* receive, and only when the team has asked
for it, :func:`~apps.scm.tracking.lifecycle.stop_container_tracking` is called —
the Stop button's own function, provider-neutral. It runs after the receive is
committed, and a failure is reported beside a receive that stands.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from .choices import LocationSource, MovementType
from .intake import get_team_containers_by_parts
from .location_identity import normalize_location_name
from .location_time import LocalTime, LocationTimezone, localize, resolve_location_timezone
from .models import Container, ContainerLocation, ContainerMovement, LocationAlias
from .movements import record_container_movement
from .receive_parser import ReceiveRow, parse_receive_text

if TYPE_CHECKING:
    from apps.teams.models import Team

logger = logging.getLogger(__name__)

RECEIVE_MOVEMENT_TYPE = MovementType.GATE_IN
# A depot's yard system reporting receipts it handled — which is what the pasted
# report is. An observed claim, so it ranks with a gate-in typed by hand.
RECEIVE_SOURCE = LocationSource.DEPOT

# Row states, shared by the preview and the result.
READY = "ready"
RECEIVED = "received"
ALREADY_RECEIVED = "already_received"
NOT_FOUND = "not_found"
INVALID = "invalid"
DUPLICATE = "duplicate"
FAILED = "failed"

# What happened to tracking for one row.
TRACKING_NOT_APPLIED = "not_applied"  # Policy off, or no new receive.
TRACKING_STOPPED = "stopped"
TRACKING_NOT_ACTIVE = "not_active"
TRACKING_STOP_FAILED = "stop_failed"


@dataclass(frozen=True)
class ReceiveOutcome:
    movement: ContainerMovement
    created: bool


def receive_container(
    *,
    team: Team,
    container: Container,
    location: ContainerLocation,
    occurred_at: datetime,
    source: str = RECEIVE_SOURCE,
    notes: str = "",
) -> ReceiveOutcome:
    """Record that *container* was gated in at *location* at *occurred_at*, once.

    Returns the existing movement with ``created=False`` when exactly this receive is
    already recorded. The check and the write happen inside one transaction holding the
    container's row lock, so two concurrent calls serialise and the second finds the
    first one's movement.
    """
    if container.team_id != team.pk:
        raise ValidationError({"container": _("The container belongs to a different team.")})
    if location.team_id != team.pk:
        raise ValidationError({"to_location": _("The location belongs to a different team.")})

    with transaction.atomic():
        Container.objects.select_for_update(of=("self",)).only("pk").get(pk=container.pk)
        existing = _find_receive(team, container.pk, location.pk, occurred_at)
        if existing is not None:
            return ReceiveOutcome(movement=existing, created=False)
        movement = record_container_movement(
            team=team,
            container=container,
            movement_type=RECEIVE_MOVEMENT_TYPE,
            to_location=location,
            occurred_at=occurred_at,
            source=source,
            notes=notes,
        )
    return ReceiveOutcome(movement=movement, created=True)


def _find_receive(team: Team, container_id: int, location_id: int, occurred_at: datetime):
    return ContainerMovement.objects.filter(
        team=team,
        container_id=container_id,
        movement_type=RECEIVE_MOVEMENT_TYPE,
        to_location_id=location_id,
        occurred_at=occurred_at,
    ).first()


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


@dataclass
class BulkReceiveRow:
    """One pasted line, what it matched and what Confirm does — or did — with it."""

    line_number: int
    container_number: str
    state: str
    occurred_at: datetime | None = None
    source_site: str = ""
    iso_code: str = ""
    container: Container | None = None
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    # A live watch: what "tracking is still active" means on an already-received row.
    is_tracked: bool = False
    # Anything Stop would act on, which includes a paused watch whose release is owed.
    has_stoppable_tracking: bool = False
    will_stop_tracking: bool = False
    tracking: str = TRACKING_NOT_APPLIED
    tracking_message: str = ""

    @property
    def is_actionable(self) -> bool:
        return self.state == READY


@dataclass
class BulkReceive:
    """A pasted report against one destination: the preview, and after Confirm the result."""

    location: ContainerLocation
    stop_tracking_on_receive: bool
    timezone: LocationTimezone
    rows: list[BulkReceiveRow] = field(default_factory=list)

    def count(self, state: str) -> int:
        return sum(1 for row in self.rows if row.state == state)

    def tracking_count(self, tracking: str) -> int:
        return sum(1 for row in self.rows if row.tracking == tracking)

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def ready_count(self) -> int:
        return self.count(READY)

    @property
    def counts(self) -> dict[str, int]:
        states = (READY, RECEIVED, ALREADY_RECEIVED, NOT_FOUND, INVALID, DUPLICATE, FAILED)
        trackings = (TRACKING_STOPPED, TRACKING_NOT_ACTIVE, TRACKING_STOP_FAILED)
        return {**{s: self.count(s) for s in states}, **{f"tracking_{t}": self.tracking_count(t) for t in trackings}}


def preview_bulk_receive(*, team: Team, location: ContainerLocation, text: str) -> BulkReceive:
    """What confirming *text* against *location* would do. Reads only.

    The report's times are read in the destination's timezone — see
    :func:`~apps.scm.containers.location_time.resolve_location_timezone` — because a
    gate-in happens on the site's clock, not the clock of whoever pastes it.

    A fixed number of queries whatever the length of the paste: the containers, the
    receives already recorded, their stoppable tracking, the destination's aliases and
    its parents' timezones.
    """
    from apps.scm.tracking.preferences import get_team_stop_tracking_on_receive

    if location.team_id != team.pk:
        raise ValidationError({"location": _("The location belongs to a different team.")})

    parsed = parse_receive_text(text)
    zone = resolve_location_timezone(team, location)
    stop_policy = get_team_stop_tracking_on_receive(team)
    result = BulkReceive(location=location, stop_tracking_on_receive=stop_policy, timezone=zone)

    times = {row.line_number: localize(row.local_time, zone.zone) for row in parsed.rows}
    containers = get_team_containers_by_parts(team, [row.parts for row in parsed.rows])
    matched = list(containers.values())
    context = _PreviewContext(
        location=location,
        stop_policy=stop_policy,
        containers=containers,
        received=_received_keys(team, location, matched, [time.instant for time in times.values()]),
        tracking=_tracking_statuses(team, matched),
        site_names=_destination_names(team, location),
    )

    seen: set[tuple[str, datetime]] = set()
    rows: list[BulkReceiveRow] = [
        BulkReceiveRow(
            line_number=e.line_number, container_number=e.container_number or e.raw, state=INVALID, error=e.error
        )
        for e in parsed.errors
    ]
    for parsed_row in parsed.rows:
        row = _preview_row(parsed_row, times[parsed_row.line_number], context)
        key = (parsed_row.container_number, times[parsed_row.line_number].instant)
        if key in seen:
            row.state, row.will_stop_tracking = DUPLICATE, False
        seen.add(key)
        rows.append(row)

    result.rows = sorted(rows, key=lambda row: row.line_number)
    return result


@dataclass(frozen=True)
class _PreviewContext:
    location: ContainerLocation
    stop_policy: bool
    containers: dict
    received: set[tuple[int, datetime]]
    # Container id → the statuses of its stoppable watches.
    tracking: dict[int, set[str]]
    site_names: set[str]


def _tracking_statuses(team: Team, containers) -> dict[int, set[str]]:
    """What Stop would act on, per container — the lifecycle's own definition, not a copy."""
    from apps.scm.tracking.lifecycle import stoppable_subscriptions

    statuses: dict[int, set[str]] = {}
    if not containers:
        return statuses
    rows = stoppable_subscriptions(team=team, container_ids=[c.pk for c in containers]).values_list(
        "container_id", "status"
    )
    for container_id, status in rows:
        statuses.setdefault(container_id, set()).add(status)
    return statuses


def _preview_row(parsed_row: ReceiveRow, time: LocalTime, context: _PreviewContext) -> BulkReceiveRow:
    from apps.scm.tracking.selectors import LIVE_SUBSCRIPTION_STATUSES

    parts = parsed_row.parts
    container = context.containers.get((parts["owner_code"], parts["category_id"], parts["serial_number"]))
    row = BulkReceiveRow(
        line_number=parsed_row.line_number,
        container_number=parsed_row.container_number,
        state=READY,
        occurred_at=time.instant,
        source_site=parsed_row.source_site,
        iso_code=parsed_row.iso_code,
        container=container,
    )
    if time.is_ambiguous:
        row.warnings.append(
            str(_("This local time occurs twice at the daylight-saving change; the earlier (summer-time) one is used."))
        )
    elif time.is_nonexistent:
        row.warnings.append(
            str(_("This local time does not exist at the daylight-saving change; it is read with the winter offset."))
        )
    if container is None:
        row.state = NOT_FOUND
        return row

    statuses = context.tracking.get(container.pk, set())
    row.is_tracked = bool(statuses & set(LIVE_SUBSCRIPTION_STATUSES))
    row.has_stoppable_tracking = bool(statuses)
    if (container.pk, time.instant) in context.received:
        row.state = ALREADY_RECEIVED
    else:
        row.will_stop_tracking = context.stop_policy and row.has_stoppable_tracking

    if parsed_row.iso_code and parsed_row.iso_code != (container.equipment_type_id or "").upper():
        row.warnings.append(
            str(
                _("Imported ISO type %(imported)s differs from the container's %(recorded)s.")
                % {"imported": parsed_row.iso_code, "recorded": container.equipment_type_id}
            )
        )
    if parsed_row.source_site and not _site_matches(parsed_row.source_site, context.site_names):
        row.warnings.append(
            str(
                _("Reported site '%(site)s' does not look like %(location)s.")
                % {"site": parsed_row.source_site, "location": context.location.name}
            )
        )
    return row


def _received_keys(team: Team, location: ContainerLocation, containers, times) -> set[tuple[int, datetime]]:
    if not containers or not times:
        return set()
    return set(
        ContainerMovement.objects.filter(
            team=team,
            container__in=containers,
            movement_type=RECEIVE_MOVEMENT_TYPE,
            to_location=location,
            occurred_at__in=set(times),
        ).values_list("container_id", "occurred_at")
    )


def _destination_names(team: Team, location: ContainerLocation) -> set[str]:
    """Every normalised name the destination is known by: its own and its aliases'."""
    aliases = LocationAlias.objects.filter(team=team, location=location).values_list("normalized_name", flat=True)
    return {name for name in (normalize_location_name(location.name), *aliases) if name}


def _site_matches(site: str, names: set[str]) -> bool:
    """Whether a reported site plausibly names the destination.

    For a warning only, never a resolution: the receive is recorded against the chosen
    location either way. "MCR AB - Oceanterminalen" names Oceanterminalen because the
    destination's name is in it; an operator site prefix is not something to alias.
    """
    normalised = normalize_location_name(site)
    return any(name in normalised for name in names)


# ---------------------------------------------------------------------------
# Confirm
# ---------------------------------------------------------------------------


def bulk_receive(*, team: Team, location: ContainerLocation, text: str, actor=None) -> BulkReceive:
    """Receive every ready row in *text* at *location*, then apply the tracking policy.

    The preview is recomputed here rather than trusted from the browser, and every row
    is written in its own transaction: one failing row does not cost the rest their
    receive.
    """
    result = preview_bulk_receive(team=team, location=location, text=text)
    for row in result.rows:
        # A ready row always matched a container and carries a time; the check says so to the type checker.
        container, occurred_at = row.container, row.occurred_at
        if row.state != READY or container is None or occurred_at is None:
            continue
        try:
            outcome = receive_container(
                team=team,
                container=container,
                location=location,
                occurred_at=occurred_at,
                notes=_provenance(row, result.timezone),
            )
        except ValidationError as exc:
            row.state, row.error, row.will_stop_tracking = FAILED, " ".join(exc.messages), False
            continue
        if not outcome.created:
            # Received between the preview and this write — another tab, another user.
            row.state, row.will_stop_tracking = ALREADY_RECEIVED, False
            continue
        row.state = RECEIVED
        if result.stop_tracking_on_receive:
            _stop_tracking(team=team, container=container, row=row, actor=actor)

    logger.info("Bulk receive at location %s for team %s: %s", location.pk, team.pk, result.counts)
    return result


def _provenance(row: BulkReceiveRow, zone: LocationTimezone) -> str:
    details = [str(_("Bulk receive (pasted report)."))]
    details.append(str(_("Local time read in %(zone)s.") % {"zone": zone.name}))
    if row.source_site:
        details.append(str(_("Reported site: %(site)s.") % {"site": row.source_site}))
    if row.iso_code:
        details.append(str(_("Reported ISO: %(iso)s.") % {"iso": row.iso_code}))
    return " ".join(details)


def _stop_tracking(*, team: Team, container: Container, row: BulkReceiveRow, actor) -> None:
    """Stop tracking a container just received. Never raises; the receive stands.

    Called with the receive already committed and outside any transaction of ours, the
    way the Stop button calls it: a provider release is an HTTP call, and holding a
    database transaction open across it would gain nothing.
    """
    from apps.scm.tracking.lifecycle import ALREADY_STOPPED, STOPPED, stop_container_tracking

    try:
        stop = stop_container_tracking(team=team, container=container, actor=actor)
    except Exception:  # noqa: BLE001 — a tracking bug must not undo or hide a receive
        logger.exception("Stopping tracking after receiving %s raised unexpectedly.", row.container_number)
        row.tracking = TRACKING_STOP_FAILED
        row.tracking_message = str(_("An unexpected error prevented tracking from stopping."))
        return

    if stop.state == STOPPED:
        row.tracking = TRACKING_STOPPED
    elif stop.state == ALREADY_STOPPED:
        row.tracking = TRACKING_NOT_ACTIVE
    else:
        row.tracking = TRACKING_STOP_FAILED
    row.tracking_message = str(stop.message)
