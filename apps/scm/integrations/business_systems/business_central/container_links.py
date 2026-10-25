"""Link Business Central item ledger entries to Pegasus containers.

One physical container, one identity: a container is the team's ``Container`` with
the entry's valid ISO 6346 number (owner, category, serial). The BC item number is
*not* part of the identity — the same container bought as one item, reclassified to
another and sold again is one Container with many entries.

``match_item_ledger_entries`` (operation A) only reads containers and only writes
the entries' ``container`` / ``link_status`` / ``item_classification``. It never
creates, edits or moves a container, and never touches Business Central. Every
status is recomputed from the current rules on each pass, so a corrected rule or a
newly registered container takes effect on the next pass, and nothing duplicates.

Per entry, in order:

  missing serial → MISSING_SERIAL; invalid ISO → INVALID_SERIAL;
  item needs a variant → VARIANT_UNRESOLVED; unclassified → UNCLASSIFIED_ITEM;
  not a container → NON_CONTAINER_ITEM;
  container item, container exists → LINKED (or EQUIPMENT_TYPE_CONFLICT when the
  mapped type differs — linked, the container's type is left alone);
  container item, no container → CONTAINER_NOT_FOUND (a creation candidate) or
  MISSING_EQUIPMENT_TYPE when the item has no type mapped.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from django.utils import timezone

from apps.scm.containers.intake import get_team_containers_by_parts, parse_and_validate_container_number
from apps.scm.containers.models import Container
from apps.scm.integrations.models import BusinessCentralItemLedgerEntry, Integration

from .item_rules import ItemResolution, ItemRuleSet, entry_variant_code
from .locks import sync_lock

logger = logging.getLogger(__name__)

Entry = BusinessCentralItemLedgerEntry
LinkStatus = Entry.LinkStatus
Classification = Entry.ItemClassification

_CHUNK_SIZE = 1000
_LINK_FIELDS = ["container", "link_status", "item_classification", "link_updated_at", "updated_at"]


@dataclass
class MatchResult:
    checked: int = 0
    changed: int = 0
    by_status: Counter[str] = field(default_factory=Counter)


def matching_lock_name(integration: Integration) -> str:
    return f"{integration.pk}:item_ledger_matching"


def match_item_ledger_entries(integration: Integration, *, serial_numbers: Iterable[str] | None = None) -> MatchResult:
    """Recompute the container link of the integration's entries (all, or only ``serial_numbers``).

    Raises BusinessCentralSyncInProgressError if a matching pass for this
    integration is already running.
    """
    with sync_lock(matching_lock_name(integration)):
        rules = ItemRuleSet.for_integration(integration)
        entries = Entry.objects.filter(integration=integration).order_by("pk")
        if serial_numbers is not None:
            entries = entries.filter(serial_no__in=list(serial_numbers))
        result = MatchResult()
        chunk: list[Entry] = []
        for entry in entries.iterator(chunk_size=_CHUNK_SIZE):
            chunk.append(entry)
            if len(chunk) >= _CHUNK_SIZE:
                _match_chunk(integration, rules, chunk, result)
                chunk = []
        if chunk:
            _match_chunk(integration, rules, chunk, result)
    logger.info(
        "BC item ledger matching: integration=%s checked=%d changed=%d %s",
        integration.pk,
        result.checked,
        result.changed,
        dict(result.by_status),
    )
    return result


def _match_chunk(integration: Integration, rules: ItemRuleSet, chunk: list[Entry], result: MatchResult) -> None:
    valid = {e.pk: parse_and_validate_container_number(e.serial_no) for e in chunk if _is_valid(e)}
    # One query per chunk, always scoped to the integration's own team.
    containers = get_team_containers_by_parts(integration.team, list(valid.values()))
    now = timezone.now()
    changed: list[Entry] = []
    for entry in chunk:
        parts = valid.get(entry.pk)
        container = (
            containers.get((parts["owner_code"], parts["category_id"], parts["serial_number"])) if parts else None
        )
        resolution = rules.resolve(entry.item_no, entry_variant_code(entry))
        status, linked = _evaluate(entry, resolution, container)
        classification = resolution.classification
        result.checked += 1
        result.by_status[status] += 1
        if (entry.link_status, entry.container_id, entry.item_classification) != (
            status,
            linked.pk if linked else None,
            classification,
        ):
            entry.link_status, entry.container, entry.item_classification = status, linked, classification
            entry.link_updated_at = entry.updated_at = now
            changed.append(entry)
    Entry.objects.bulk_update(changed, _LINK_FIELDS)
    result.changed += len(changed)


def _is_valid(entry: Entry) -> bool:
    return entry.serial_status == Entry.SerialStatus.VALID_ISO and bool(entry.serial_no)


def _evaluate(entry: Entry, resolution: ItemResolution, container: Container | None) -> tuple[str, Container | None]:
    if entry.serial_status == Entry.SerialStatus.MISSING_SERIAL:
        return LinkStatus.MISSING_SERIAL, None
    if not _is_valid(entry):
        return LinkStatus.INVALID_SERIAL, None
    if resolution.needs_variant:
        return LinkStatus.VARIANT_UNRESOLVED, None
    if resolution.classification == Classification.UNCLASSIFIED:
        return LinkStatus.UNCLASSIFIED_ITEM, None
    if resolution.classification == Classification.NOT_CONTAINER:
        return LinkStatus.NON_CONTAINER_ITEM, None
    if container is not None:
        mapped = resolution.equipment_type
        if mapped is not None and mapped.pk != container.equipment_type_id:
            return LinkStatus.EQUIPMENT_TYPE_CONFLICT, container
        return LinkStatus.LINKED, container
    if resolution.equipment_type is None:
        return LinkStatus.MISSING_EQUIPMENT_TYPE, None
    return LinkStatus.CONTAINER_NOT_FOUND, None
