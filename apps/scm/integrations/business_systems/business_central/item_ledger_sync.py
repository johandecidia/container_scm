"""Sync Business Central item ledger entries into ``BusinessCentralItemLedgerEntry``.

The run lifecycle (validation, per-resource lock, source binding, sync run,
markers) is the shared one in ``lifecycle``; this module is the step.

Incremental by ``entryNo`` (verified: ``entryNo gt N`` + ``$orderby=entryNo asc``
+ keyset paging). Each run reads at most ``max_entries_per_run`` entries in
ascending order, whole pages at a time, and reports the last entry no it stored
as the cursor. The lifecycle stores that cursor only when the run completes
fully, so:

  - an initial import of a large ledger is a series of short runs, each resumable;
  - a run that fails part-way keeps the pages it already stored (each page is
    its own transaction) and the next run starts again from the old cursor —
    the re-read pages come back as *unchanged*, never as duplicates;
  - a record that cannot be stored holds the cursor, so nothing is skipped.

The cursor is scoped to the record filter (``marker_scope``): change the item
prefixes or ``serial_only`` and the next run starts from the beginning of the new
selection instead of skipping what the old filter excluded.

Read-only with respect to everything else: no containers, movements, receipts,
shipments or tracking are created or changed.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from functools import partial

from django.db import transaction
from django.utils import timezone

from apps.scm.integrations.models import BusinessCentralItemLedgerEntry, Integration, IntegrationSyncRun

from .client import BusinessCentralClient, require_item_prefix
from .exceptions import BusinessCentralConfigurationError, BusinessCentralResponseError
from .item_ledger import map_item_ledger_entry
from .lifecycle import SyncContext, SyncOutcome, run_business_central_sync
from .resources import get_resource_config

logger = logging.getLogger(__name__)

_RESOURCE = IntegrationSyncRun.ResourceType.ITEM_LEDGER_ENTRIES
DEFAULT_MAX_ENTRIES_PER_RUN = 5000
_MAX_ENTRIES_PER_RUN_LIMIT = 100_000

# Columns rewritten when BC sends a changed row (everything the mapper produces).
_UPDATE_FIELDS = [
    "item_no",
    "serial_no_raw",
    "serial_no",
    "serial_status",
    "lot_no",
    "entry_type",
    "document_type",
    "document_no",
    "posting_date",
    "quantity",
    "location_code",
    "source_type",
    "source_no",
    "raw_payload",
    "sync_hash",
    "synced_at",
    "updated_at",
]


@dataclass(frozen=True)
class ItemLedgerOptions:
    """Per-integration settings from ``config["resources"]["item_ledger_entries"]``."""

    item_no_prefixes: tuple[str, ...] = ()
    serial_only: bool = False
    max_entries_per_run: int = DEFAULT_MAX_ENTRIES_PER_RUN

    @property
    def scope(self) -> dict:
        """The record selection; cursors are only reused within the same scope."""
        return {"item_no_prefixes": list(self.item_no_prefixes), "serial_only": self.serial_only}


def get_item_ledger_options(integration: Integration) -> ItemLedgerOptions:
    """Validated options; raises BusinessCentralConfigurationError for unusable values.

    ``item_no_prefixes`` (default: none — every item) restricts the sync to items
    whose number starts with one of them; ``serial_only`` (default false) to
    entries with a serial number. Neither is assumed: item numbering differs per
    customer, and a prefix filter can hide container serials on other items.
    """
    entry = get_resource_config(integration, _RESOURCE)
    prefixes = entry.get("item_no_prefixes") or []
    if not isinstance(prefixes, list):
        raise BusinessCentralConfigurationError("item_ledger_entries.item_no_prefixes must be a list")
    cleaned = tuple(sorted({require_item_prefix(p) for p in prefixes}))
    serial_only = entry.get("serial_only", False)
    if not isinstance(serial_only, bool):
        raise BusinessCentralConfigurationError("item_ledger_entries.serial_only must be true or false")
    max_entries = entry.get("max_entries_per_run", DEFAULT_MAX_ENTRIES_PER_RUN)
    if isinstance(max_entries, bool) or not isinstance(max_entries, int):
        raise BusinessCentralConfigurationError("item_ledger_entries.max_entries_per_run must be an integer")
    if not 1 <= max_entries <= _MAX_ENTRIES_PER_RUN_LIMIT:
        raise BusinessCentralConfigurationError(
            f"item_ledger_entries.max_entries_per_run must be between 1 and {_MAX_ENTRIES_PER_RUN_LIMIT}"
        )
    return ItemLedgerOptions(item_no_prefixes=cleaned, serial_only=serial_only, max_entries_per_run=max_entries)


def sync_item_ledger_entries_from_business_central(
    integration: Integration,
    client: BusinessCentralClient | None = None,
    *,
    trigger_type: str = IntegrationSyncRun.TriggerType.SCHEDULED,
) -> IntegrationSyncRun:
    """Fetch and store the next batch of item ledger entries for an integration.

    ``run.metadata["has_more"]`` is true when the run stopped at
    ``max_entries_per_run`` and more entries may follow; ``run.metadata["quality"]``
    holds the serial-number counts of the entries read.

    Raises the same errors as the purchase-order sync (configuration, lock in
    progress, BC errors — the run is recorded as failed before re-raise).
    """
    options = get_item_ledger_options(integration)
    return run_business_central_sync(
        integration,
        _RESOURCE,
        partial(_sync_step, options=options),
        client,
        trigger_type=trigger_type,
        marker_scope=options.scope,
    )


def _sync_step(context: SyncContext, *, options: ItemLedgerOptions) -> SyncOutcome:
    integration, source = context.integration, context.source
    company_id = source.get("company_id") or (integration.config or {}).get("company_id", "")
    outcome = SyncOutcome(cursor_to=context.cursor_from)
    quality: Counter[str] = Counter()
    has_more = False
    last_entry_no = context.cursor_from

    pages = context.client.iter_item_ledger_entry_pages(
        after_entry_no=context.cursor_from,
        item_no_prefixes=options.item_no_prefixes,
        serial_only=options.serial_only,
    )
    for page in pages:
        if not page:
            break
        last_entry_no = _check_ascending(page, last_entry_no)
        counts = upsert_item_ledger_entries(integration, company_id, page)
        outcome.fetched += len(page)
        outcome.created += counts.created
        outcome.updated += counts.updated
        outcome.unchanged += counts.unchanged
        outcome.failed += counts.failed
        outcome.errors.extend(counts.errors)
        quality.update(counts.quality)
        outcome.cursor_to = last_entry_no
        if outcome.fetched >= options.max_entries_per_run:
            has_more = True  # stop on a page boundary; the next run continues from here
            break

    outcome.metadata = {
        "has_more": has_more,
        "quality": {
            "valid_iso": quality[BusinessCentralItemLedgerEntry.SerialStatus.VALID_ISO],
            "invalid_iso": quality[BusinessCentralItemLedgerEntry.SerialStatus.INVALID_ISO],
            "missing_serial": quality[BusinessCentralItemLedgerEntry.SerialStatus.MISSING_SERIAL],
            "unclassified": quality["unclassified"],
        },
    }
    return outcome


def _check_ascending(page: list[dict], last_entry_no: int | None) -> int | None:
    """Refuse a response that is not strictly ascending by entryNo.

    The cursor is the last entry stored; out-of-order data could put it past
    entries that were never seen, so such a run fails instead (marker held).
    """
    for raw in page:
        entry_no = raw.get("entryNo") if isinstance(raw, dict) else None
        if isinstance(entry_no, bool) or not isinstance(entry_no, int):
            continue  # recorded as a failed record by the upsert; holds the cursor
        if last_entry_no is not None and entry_no <= last_entry_no:
            raise BusinessCentralResponseError(
                f"Business Central returned item ledger entries out of order ({entry_no} after {last_entry_no})"
            )
        last_entry_no = entry_no
    return last_entry_no


@dataclass
class UpsertCounts:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    errors: list[dict] = field(default_factory=list)
    quality: Counter[str] = field(default_factory=Counter)


def upsert_item_ledger_entries(integration: Integration, company_id: str, raw_rows: list[dict]) -> UpsertCounts:
    """Create or update one page of entries in a single transaction (idempotent).

    Rows are matched on ``(integration, company_id, entry_no)``. An unchanged row
    (same ``sync_hash``) only gets ``synced_at`` bumped; a changed row is
    rewritten from BC's data. A row without an integer ``entryNo`` is not stored
    and is reported as failed.
    """
    counts = UpsertCounts()
    mapped: dict[int, dict] = {}
    for raw in raw_rows:
        try:
            fields = map_item_ledger_entry(raw if isinstance(raw, dict) else {})
        except ValueError as exc:
            counts.failed += 1
            counts.errors.append({"external_id": str((raw or {}).get("entryNo", "")), "error": str(exc)})
            continue
        mapped[fields["entry_no"]] = fields  # a repeated entryNo keeps BC's last version
    for fields in mapped.values():
        counts.quality[fields["serial_status"]] += 1
        counts.quality["unclassified"] += 1  # no item classification rule in stage 1a

    now = timezone.now()
    team = integration.team
    with transaction.atomic():
        existing = {
            entry.entry_no: entry
            for entry in BusinessCentralItemLedgerEntry.objects.select_for_update().filter(
                integration=integration, company_id=company_id, entry_no__in=list(mapped)
            )
        }
        to_create, to_update, unchanged_pks = [], [], []
        for entry_no, fields in mapped.items():
            entry = existing.get(entry_no)
            if entry is None:
                to_create.append(
                    BusinessCentralItemLedgerEntry(
                        team=team, integration=integration, company_id=company_id, synced_at=now, **fields
                    )
                )
            elif entry.sync_hash == fields["sync_hash"]:
                unchanged_pks.append(entry.pk)
            else:
                for name, value in fields.items():
                    setattr(entry, name, value)
                entry.synced_at = now
                entry.updated_at = now
                to_update.append(entry)
        BusinessCentralItemLedgerEntry.objects.bulk_create(to_create)
        BusinessCentralItemLedgerEntry.objects.bulk_update(to_update, _UPDATE_FIELDS)
        BusinessCentralItemLedgerEntry.objects.filter(pk__in=unchanged_pks).update(synced_at=now)

    counts.created, counts.updated, counts.unchanged = len(to_create), len(to_update), len(unchanged_pks)
    return counts
