"""Sync posted purchase receipts into ``BusinessCentralPurchaseReceipt``.

The run lifecycle (validation, per-resource lock, source binding, sync run,
markers) is the shared one in ``lifecycle``; this module is the step.

Incremental by ``lastModifiedDateTime`` like the purchase-order sync (same overlap
and ``initial_sync_days``), with the lines expanded in the same request. Pages are
stored one transaction at a time; the watermark only advances when the whole run
completes, so a failed run re-reads pages that then come back as *unchanged*.

Read-only with respect to everything else: no containers, movements, receives or
tracking are created or changed. Matching receipts to containers and receiving
them are separate steps (``receipt_resolution``, ``receipt_receive``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.scm.integrations.models import BusinessCentralPurchaseReceipt, Integration, IntegrationSyncRun

from .client import BusinessCentralClient
from .item_ledger import compute_sync_hash, date_value, text_value
from .lifecycle import SyncContext, SyncOutcome, run_business_central_sync
from .sync import modified_since_filter

logger = logging.getLogger(__name__)

Receipt = BusinessCentralPurchaseReceipt
_RESOURCE = IntegrationSyncRun.ResourceType.PURCHASE_RECEIPTS
_UPDATE_FIELDS = [
    "bc_document_id",
    "posting_date",
    "vendor_no",
    "vendor_name",
    "order_no",
    "line_count",
    "bc_last_modified",
    "raw_payload",
    "sync_hash",
    "synced_at",
    "updated_at",
]


def sync_purchase_receipts_from_business_central(
    integration: Integration,
    client: BusinessCentralClient | None = None,
    *,
    trigger_type: str = IntegrationSyncRun.TriggerType.SCHEDULED,
) -> IntegrationSyncRun:
    """Fetch and store posted purchase receipts (with lines) modified since the last run.

    Raises the same errors as the purchase-order sync (configuration, lock in
    progress, BC errors — the run is recorded as failed before re-raise).
    """
    return run_business_central_sync(integration, _RESOURCE, _sync_step, client, trigger_type=trigger_type)


def _sync_step(context: SyncContext) -> SyncOutcome:
    integration, source = context.integration, context.source
    company_id = source.get("company_id") or (integration.config or {}).get("company_id", "")
    outcome = SyncOutcome()
    max_modified = None
    for page in context.client.iter_purchase_receipt_pages(
        modified_since=modified_since_filter(integration, context.watermark_from, context.started)
    ):
        if not page:
            break
        counts = upsert_purchase_receipts(integration, company_id, page)
        outcome.fetched += len(page)
        outcome.created += counts.created
        outcome.updated += counts.updated
        outcome.unchanged += counts.unchanged
        outcome.failed += counts.failed
        outcome.errors.extend(counts.errors)
        if counts.max_modified and (max_modified is None or counts.max_modified > max_modified):
            max_modified = counts.max_modified
    outcome.watermark_to = max_modified or context.watermark_from or context.started
    return outcome


def map_purchase_receipt(raw: dict) -> dict[str, Any]:
    """Model field values for one raw BC receipt; raises ValueError without a receipt number."""
    receipt_no = text_value(raw, "number").strip()
    if not receipt_no:
        raise ValueError("Business Central purchase receipt has no 'number'")
    lines = raw.get("purchaseReceiptLines")
    return {
        "receipt_no": receipt_no,
        "bc_document_id": text_value(raw, "id"),
        "posting_date": date_value(raw.get("postingDate")),
        "vendor_no": text_value(raw, "vendorNumber"),
        "vendor_name": text_value(raw, "vendorName")[:200],
        "order_no": text_value(raw, "orderNumber"),
        "line_count": len(lines) if isinstance(lines, list) else 0,
        "bc_last_modified": _last_modified(raw.get("lastModifiedDateTime")),
        "raw_payload": raw,
        "sync_hash": compute_sync_hash(raw),
    }


def _last_modified(value: Any) -> datetime | None:
    """BC's ``lastModifiedDateTime``; its blank value (year 1) means unknown."""
    if not isinstance(value, str) or value.startswith("0001-01-01"):
        return None
    try:
        return parse_datetime(value)
    except ValueError:
        return None


@dataclass
class UpsertCounts:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    errors: list[dict] = field(default_factory=list)
    max_modified: datetime | None = None


def upsert_purchase_receipts(integration: Integration, company_id: str, raw_rows: list[dict]) -> UpsertCounts:
    """Create or update one page of receipts in a single transaction (idempotent).

    Matched on ``(integration, company_id, receipt_no)``. An unchanged receipt (same
    ``sync_hash``) only gets ``synced_at`` bumped.
    """
    counts = UpsertCounts()
    mapped: dict[str, dict] = {}
    for raw in raw_rows:
        try:
            fields = map_purchase_receipt(raw if isinstance(raw, dict) else {})
        except ValueError as exc:
            counts.failed += 1
            counts.errors.append({"external_id": str((raw or {}).get("id", "")), "error": str(exc)})
            continue
        mapped[fields["receipt_no"]] = fields
        modified = fields["bc_last_modified"]
        if modified and (counts.max_modified is None or modified > counts.max_modified):
            counts.max_modified = modified

    now = timezone.now()
    with transaction.atomic():
        existing = {
            r.receipt_no: r
            for r in Receipt.objects.select_for_update().filter(
                integration=integration, company_id=company_id, receipt_no__in=list(mapped)
            )
        }
        to_create, to_update, unchanged_pks = [], [], []
        for receipt_no, fields in mapped.items():
            receipt = existing.get(receipt_no)
            if receipt is None:
                to_create.append(
                    Receipt(
                        team=integration.team, integration=integration, company_id=company_id, synced_at=now, **fields
                    )
                )
            elif receipt.sync_hash == fields["sync_hash"]:
                unchanged_pks.append(receipt.pk)
            else:
                for name, value in fields.items():
                    setattr(receipt, name, value)
                receipt.synced_at = receipt.updated_at = now
                to_update.append(receipt)
        Receipt.objects.bulk_create(to_create)
        Receipt.objects.bulk_update(to_update, _UPDATE_FIELDS)
        Receipt.objects.filter(pk__in=unchanged_pks).update(synced_at=now)

    counts.created, counts.updated, counts.unchanged = len(to_create), len(to_update), len(unchanged_pks)
    return counts
