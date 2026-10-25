"""Automatic receive from posted Business Central purchase receipts (stages 2d, 2e, 2g).

``process_purchase_receipts`` re-resolves the integration's receipts
(``receipt_resolution``) and then, for every serial row not yet final, decides —
in this order, stopping at the first that fails:

  1. the BC location has an active mapping that receives automatically: physical,
     a Pegasus location, the posted-receipt trigger, automatic receive on;
  2. the entry was posted after the mapping's activation boundary (``entryNo``,
     never the posting date) — otherwise HISTORICAL, and nothing is created;
  3. the entry has a posting date, and it is not in the future (BC allows future
     posting dates; then the order of events cannot be known — manual review);
  4. the receipt and the serial are RESOLVED (entydigt identified, valid ISO,
     classified as a container);
  5. the container exists in the team, or the mapping may create it and the item
     has an equipment type — created through ``containers.intake``;
  6. nothing physical contradicts it, checked under the container's row lock and
     against the *posting date* (start of that day on the location's clock), never
     against the time the receive is recorded: an arrival at the same location on or
     after the posting date, or the container already being there, is
     ALREADY_RECEIVED; any other movement on or after the posting date is
     NEWER_MOVEMENT (manual review — BC carries only a date, so the order cannot be
     trusted);
  7. the receive itself: ``containers.receive.receive_container`` — the same GATE_IN
     a pasted depot report records — at the moment Pegasus records it.

Two times are kept apart. The BC posting date is a bookkeeping date and stays on the
row (``posting_date``) and in the movement's notes; it is not a verified arrival
time, so no arrival is invented at 00:00 of it. The movement's ``occurred_at`` (and
``received_at``) is when Pegasus registered the receive. The age of a posting date
never blocks a receive on its own: the ``entryNo`` activation boundary is what keeps
history out, and step 6 is what keeps a late or backdated receipt from overriding
newer physical information.

A row ends RECEIVED (with its movement), ALREADY_RECEIVED, or NOT_RECEIVED with a
reason. RECEIVED and ALREADY_RECEIVED are final, so a receipt read again never
receives twice; NOT_RECEIVED is re-evaluated on the next pass. After a *new*
receive, the destination location's stop-tracking-on-receive setting is applied
through the receive path's shared policy — there is no BC-specific tracking behaviour.

A receipt undone in BC *after* it was received is never reversed here; the row keeps
RECEIVED and its resolution turns REVERSED, which the receipts page lists for review.

Never writes to Business Central.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, time

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.scm.containers.choices import LocationSource
from apps.scm.containers.intake import create_or_get_container
from apps.scm.containers.location_time import resolve_location_timezone
from apps.scm.containers.models import Container, ContainerLocation, ContainerMovement
from apps.scm.containers.movements import ARRIVAL_MOVEMENT_TYPES
from apps.scm.containers.receive import apply_receive_tracking_policy, receive_container
from apps.scm.integrations.models import (
    BusinessCentralLocationMapping,
    BusinessCentralReceiptContainer,
    Integration,
    ReceiptResolution,
    ReceiveReason,
    ReceiveStatus,
)

from .item_rules import ItemRuleSet
from .location_mappings import get_location_mappings
from .locks import sync_lock
from .receipt_resolution import processing_lock_name, resolve_purchase_receipts_locked

logger = logging.getLogger(__name__)

Line = BusinessCentralReceiptContainer
Mapping = BusinessCentralLocationMapping
FINAL_STATUSES = (ReceiveStatus.RECEIVED, ReceiveStatus.ALREADY_RECEIVED)
# A BC receive is a business system's record of the arrival, not somebody seeing the
# box: it ranks below a gate-in typed by an operator or reported by a depot.
BC_RECEIVE_SOURCE = LocationSource.API
_SERIAL_REASONS: dict[str, str] = {
    ReceiptResolution.INVALID_SERIAL: ReceiveReason.INVALID_SERIAL,
    ReceiptResolution.MISSING_EQUIPMENT_TYPE: ReceiveReason.MISSING_EQUIPMENT_TYPE,
}


@dataclass
class ProcessResult:
    checked: int = 0
    received: list[str] = field(default_factory=list)
    by_status: Counter[str] = field(default_factory=Counter)
    by_reason: Counter[str] = field(default_factory=Counter)


@dataclass(frozen=True)
class _Decision:
    status: str
    reason: str = ""


def process_purchase_receipts(integration: Integration, *, actor=None) -> ProcessResult:
    """Resolve the integration's receipts and receive what qualifies.

    Raises BusinessCentralSyncInProgressError if the integration's receipts are
    already being processed (one pass at a time per integration).
    """
    result = ProcessResult()
    received: list[Line] = []
    with sync_lock(processing_lock_name(integration)):
        resolve_purchase_receipts_locked(integration)
        mappings = get_location_mappings(integration)
        rules = ItemRuleSet.for_integration(integration)
        rows = (
            Line.objects.filter(receipt__integration=integration)
            .exclude(receive_status__in=FINAL_STATUSES)
            .select_related("receipt", "container")
            .order_by("entry_no", "pk")
        )
        for row in rows.iterator(chunk_size=500):
            if _process_row(integration, row, mappings, rules):
                received.append(row)
            result.checked += 1
            result.by_status[row.receive_status] += 1
            if row.receive_reason:
                result.by_reason[row.receive_reason] += 1

    # Tracking is secondary and runs outside the lock and any transaction, after the receive is committed.
    _apply_tracking_policy(integration, received, actor)
    result.received = [row.serial_no for row in received]
    logger.info(
        "BC receipt receive: integration=%s checked=%d received=%d %s",
        integration.pk,
        result.checked,
        len(received),
        dict(result.by_reason),
    )
    return result


def _process_row(integration: Integration, row: Line, mappings: dict[str, Mapping], rules: ItemRuleSet) -> bool:
    """Decide and, if everything holds, receive one row. True when a new receive was recorded."""
    decision = _check(integration, row, mappings.get(row.location_code.upper()))
    if decision is not None:
        _save(row, decision)
        return False

    mapping = mappings[row.location_code.upper()]
    location = mapping.pegasus_location
    assert location is not None  # guaranteed by _check (receives_automatically)
    container = row.container
    if container is None:
        if not mapping.auto_create_containers:
            _save(row, _Decision(ReceiveStatus.NOT_RECEIVED, ReceiveReason.CONTAINER_NOT_FOUND))
            return False
        equipment_type = rules.resolve(row.item_no).equipment_type
        if equipment_type is None:
            _save(row, _Decision(ReceiveStatus.NOT_RECEIVED, ReceiveReason.MISSING_EQUIPMENT_TYPE))
            return False
        try:
            container, row.container_created = create_or_get_container(
                team=integration.team, user=None, number=row.serial_no, attributes={"equipment_type": equipment_type}
            )
        except ValidationError:
            logger.warning("BC receive: could not create container %s", row.serial_no)
            _save(row, _Decision(ReceiveStatus.NOT_RECEIVED, ReceiveReason.CONTAINER_CREATION_FAILED))
            return False
        row.container = container

    posted_from = posting_day_start(integration, row, location)
    received_at = timezone.now()
    try:
        with transaction.atomic():
            # The same row lock every movement takes: no movement can land between the check and the receive.
            locked = (
                Container.objects.select_for_update(of=("self",)).only("pk", "current_location").get(pk=container.pk)
            )
            conflict = _physical_conflict(integration, locked, location, posted_from)
            if conflict is not None:
                _save(row, conflict)
                return False
            outcome = receive_container(
                team=integration.team,
                container=container,
                location=location,
                occurred_at=received_at,
                source=BC_RECEIVE_SOURCE,
                notes=_provenance(row),
            )
    except ValidationError:
        logger.warning("BC receive: receive_container refused %s", row.serial_no)
        _save(row, _Decision(ReceiveStatus.NOT_RECEIVED, ReceiveReason.RECEIVE_FAILED))
        return False
    row.movement = outcome.movement
    if outcome.created:
        row.received_at = received_at
        _save(row, _Decision(ReceiveStatus.RECEIVED))
        return True
    _save(row, _Decision(ReceiveStatus.ALREADY_RECEIVED))
    return False


def _check(integration: Integration, row: Line, mapping: Mapping | None) -> _Decision | None:
    """The first reason this row may not be received, or None when it may."""
    if mapping is None:
        return _Decision(ReceiveStatus.NOT_RECEIVED, ReceiveReason.UNKNOWN_LOCATION)
    if not mapping.is_active:
        return _Decision(ReceiveStatus.NOT_RECEIVED, ReceiveReason.LOCATION_INACTIVE)
    if mapping.location_type != Mapping.LocationType.PHYSICAL or mapping.pegasus_location_id is None:
        return _Decision(ReceiveStatus.NOT_RECEIVED, ReceiveReason.NOT_PHYSICAL)
    if not mapping.receives_automatically:
        return _Decision(ReceiveStatus.NOT_RECEIVED, ReceiveReason.AUTO_RECEIVE_DISABLED)
    if row.entry_no <= (mapping.auto_receive_after_entry_no or 0):
        return _Decision(ReceiveStatus.NOT_RECEIVED, ReceiveReason.HISTORICAL)
    if row.posting_date is None:
        return _Decision(ReceiveStatus.NOT_RECEIVED, ReceiveReason.SERIAL_UNRESOLVED)
    if posting_day_start(integration, row, mapping.pegasus_location) > timezone.now():
        return _Decision(ReceiveStatus.NOT_RECEIVED, ReceiveReason.FUTURE_POSTING_DATE)
    if row.receipt.resolution_status != ReceiptResolution.RESOLVED:
        return _Decision(ReceiveStatus.NOT_RECEIVED, ReceiveReason.RECEIPT_UNRESOLVED)
    if row.resolution_status != ReceiptResolution.RESOLVED:
        reason = _SERIAL_REASONS.get(row.resolution_status, ReceiveReason.SERIAL_UNRESOLVED)
        return _Decision(ReceiveStatus.NOT_RECEIVED, reason)
    return None


def posting_day_start(integration: Integration, row: Line, location) -> datetime:
    """The start of the BC posting date on the location's clock — what history is compared with.

    Not the time of the receive: BC records a date, so anything that happened on or
    after the start of that day may have happened after the posting, and is treated so.
    """
    assert row.posting_date is not None  # refused by _check
    zone = resolve_location_timezone(integration.team, location).zone
    return datetime.combine(row.posting_date, time.min, tzinfo=zone)


def _physical_conflict(
    integration: Integration, container: Container, location: ContainerLocation, posted_from: datetime
) -> _Decision | None:
    """Whether the container's movement history makes this receive redundant or out of date.

    Compared with the posting date, not with now: a movement recorded after the
    receipt's posting date is newer physical information whatever the receive's own
    timestamp will be.
    """
    later = ContainerMovement.objects.filter(
        team=integration.team, container=container, affects_current_state=True, occurred_at__gte=posted_from
    )
    if later.filter(movement_type__in=ARRIVAL_MOVEMENT_TYPES, to_location=location).exists():
        return _Decision(ReceiveStatus.ALREADY_RECEIVED)
    if later.exists():
        return _Decision(ReceiveStatus.NOT_RECEIVED, ReceiveReason.NEWER_MOVEMENT)
    if container.current_location_id == location.pk:
        return _Decision(ReceiveStatus.ALREADY_RECEIVED)
    return None


def _provenance(row: Line) -> str:
    return (
        f"Business Central purchase receipt {row.receipt.receipt_no} posted {row.posting_date} "
        f"(entry {row.entry_no}, item {row.item_no}, location {row.location_code}). "
        "Recorded when Pegasus registered it; the posting date is not an arrival time."
    )


_RECEIVE_FIELDS = [
    "receive_status",
    "receive_reason",
    "movement",
    "container",
    "container_created",
    "received_at",
    "receive_checked_at",
    "updated_at",
]


def _save(row: Line, decision: _Decision) -> None:
    row.receive_status, row.receive_reason = decision.status, decision.reason
    row.receive_checked_at = row.updated_at = timezone.now()
    row.save(update_fields=_RECEIVE_FIELDS)


def _apply_tracking_policy(integration: Integration, received: list[Line], actor) -> None:
    """The destination's stop-tracking-on-receive setting, for receives this pass recorded."""
    for row in received:
        if row.movement is None:
            continue
        row.tracking_result, _message = apply_receive_tracking_policy(
            team=integration.team, movement=row.movement, actor=actor, label=row.serial_no
        )
        row.save(update_fields=["tracking_result", "updated_at"])
