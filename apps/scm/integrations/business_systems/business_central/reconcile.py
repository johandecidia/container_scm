"""Source reconciliation for Business Central purchase orders.

Ordinary incremental sync only fetches recently-modified records, so it must never
conclude that a record was deleted just because it is absent from a bounded result.
Reconciliation is a separate, explicit *full* source scan that detects purchase
orders / lines that no longer exist at the source and soft-deletes them:

  - sets source_active=False and source_deleted_at
  - never hard-deletes (business history and relations to supplier deliveries,
    shipments and events are preserved)

Reappearance policy: a record that shows up again is reactivated by the normal
upsert (which sets source_active=True and clears source_deleted_at).

Three independent concepts are kept distinct:
  - PurchaseOrder.status == "closed": the BC *document* is closed (still exists).
  - source_active == False: the record no longer exists at the source.
  - SCM logistics "completed": computed fulfilment state (selectors).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.utils import timezone

from apps.scm.integrations.models import Integration
from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderLine, PurchaseOrderSource

from .client import BusinessCentralClient
from .locks import purchase_order_lock_name, sync_lock
from .sync import _line_identifier, _validate_integration

logger = logging.getLogger(__name__)


@dataclass
class ReconcileResult:
    checked_pos: int = 0
    deactivated_pos: int = 0
    deactivated_lines: int = 0


def reconcile_purchase_orders(
    integration: Integration,
    client: BusinessCentralClient | None = None,
) -> ReconcileResult:
    """Full source scan: soft-delete BC purchase orders/lines absent from the source.

    Raises the same validation / lock errors as the sync.
    """
    _validate_integration(integration, client)
    with sync_lock(purchase_order_lock_name(integration)):
        return _reconcile(integration, client)


def _reconcile(integration: Integration, client: BusinessCentralClient | None) -> ReconcileResult:
    team = integration.team
    if client is None:
        client = BusinessCentralClient(integration=integration)

    # Full scan (no modified-since filter).
    raw_pos = client.fetch_purchase_orders()
    present_po_ids: set[str] = set()
    present_line_ids: dict[str, set[str]] = {}
    for raw_po in raw_pos:
        po_ext = raw_po.get("id") or raw_po.get("number")
        if not po_ext:
            # Nothing to match a stored purchase order against.
            continue
        present_po_ids.add(po_ext)
        identifier = _line_identifier(raw_po, client)
        lines = client.fetch_purchase_order_lines(identifier)
        present_line_ids[po_ext] = {line_id for line in lines if (line_id := line.get("id"))}

    now = timezone.now()
    result = ReconcileResult()

    active_pos = PurchaseOrder.objects.filter(
        team=team,
        source_system=PurchaseOrderSource.BUSINESS_CENTRAL,
        source_active=True,
    )
    for po in active_pos:
        result.checked_pos += 1
        if po.external_id not in present_po_ids:
            po.source_active = False
            po.source_deleted_at = now
            po.save(update_fields=["source_active", "source_deleted_at", "updated_at"])
            result.deactivated_pos += 1
            continue

        # PO still present — reconcile its lines.
        present = present_line_ids.get(po.external_id, set())
        stale_lines = PurchaseOrderLine.objects.filter(purchase_order=po, source_active=True).exclude(
            external_id__in=present
        )
        for line in stale_lines:
            line.source_active = False
            line.source_deleted_at = now
            line.save(update_fields=["source_active", "source_deleted_at", "updated_at"])
            result.deactivated_lines += 1

    logger.info(
        "BC reconcile: team=%s checked=%d deactivated_pos=%d deactivated_lines=%d",
        team.slug,
        result.checked_pos,
        result.deactivated_pos,
        result.deactivated_lines,
    )
    return result
