"""Sync orchestration for Microsoft Business Central purchase orders.

Business Central is the system of record. This module performs an incremental,
read-only pull of purchase orders into SCM. The run lifecycle — validation,
per-resource locking, source binding, the IntegrationSyncRun record and
watermark handling — is shared with every BC resource (see ``lifecycle``). This
module supplies only the purchase-order step:

  - fetches only records modified since the last successful watermark *of the
    same source* (with a small overlap), maps them, and upserts them idempotently
  - reports the highest ``lastModifiedDateTime`` seen as the next watermark,
    which the lifecycle stores only when the run completes fully
"""

from __future__ import annotations

import logging
from datetime import UTC, timedelta

from apps.scm.integrations.models import Integration, IntegrationSyncRun

from .client import BusinessCentralClient
from .lifecycle import SyncContext, SyncOutcome, run_business_central_sync
from .mapper import BusinessCentralMapper

logger = logging.getLogger(__name__)

_mapper = BusinessCentralMapper()

_RESOURCE = IntegrationSyncRun.ResourceType.PURCHASE_ORDERS
# Re-fetch a little before the last watermark so records modified right on the
# boundary are not missed; the idempotent upsert absorbs the overlap.
_WATERMARK_OVERLAP = timedelta(minutes=5)


def sync_purchase_orders_from_business_central(
    integration: Integration,
    client: BusinessCentralClient | None = None,
    *,
    trigger_type: str = IntegrationSyncRun.TriggerType.SCHEDULED,
) -> IntegrationSyncRun:
    """Fetch, map, and upsert Business Central purchase orders for an integration.

    Args:
        integration: The BC business-system integration to sync. The team is
            taken from ``integration.team``.
        client: Optional client. Defaults to a live BusinessCentralClient built
            from the integration. Pass a dummy client (``use_dummy=True``) in
            tests; credential validation is skipped when a client is injected.
        trigger_type: How the sync was triggered (manual/scheduled/retry).

    Returns:
        The IntegrationSyncRun recording this run.

    Raises:
        BusinessCentralConfigurationError: integration invalid / not sync-enabled
            / missing credentials / bound to another source.
        BusinessCentralSyncInProgressError: another run holds the lock.
        BusinessCentralError: on a hard fetch/auth/response failure (the run is
            recorded as failed and the watermark is not advanced before re-raise).
    """
    return run_business_central_sync(integration, _RESOURCE, _sync_step, client, trigger_type=trigger_type)


def _sync_step(context: SyncContext) -> SyncOutcome:
    from apps.scm.procurement.models import PurchaseOrderSource
    from apps.scm.procurement.services import upsert_purchase_orders

    integration, client, source = context.integration, context.client, context.source
    modified_since = modified_since_filter(integration, context.watermark_from, context.started)

    raw_pos = client.fetch_purchase_orders(modified_since=modified_since)
    normalized_data = []
    mapping_errors: list[dict] = []
    max_modified = None
    for raw_po in raw_pos:
        identifier = _line_identifier(raw_po, client)
        raw_lines = client.fetch_purchase_order_lines(identifier) if identifier else []
        try:
            normalized = _mapper.map_purchase_order(raw_po, raw_lines)
        except ValueError as exc:
            # One malformed record fails in isolation (and holds the watermark).
            mapping_errors.append(
                {"external_id": raw_po.get("id") or "", "po_number": raw_po.get("number") or "", "error": str(exc)}
            )
            continue
        normalized_data.append(normalized.model_dump())
        last_modified = normalized.source_last_modified
        if last_modified and (max_modified is None or last_modified > max_modified):
            max_modified = last_modified

    result = upsert_purchase_orders(
        team=integration.team,
        purchase_orders_data=normalized_data,
        source_system=PurchaseOrderSource.BUSINESS_CENTRAL,
        source_company_id=source.get("company_id") or (integration.config or {}).get("company_id", ""),
    )
    return SyncOutcome(
        fetched=len(raw_pos),
        created=result.created,
        updated=result.updated,
        unchanged=result.unchanged,
        failed=result.failed + len(mapping_errors),
        errors=mapping_errors + result.errors,
        watermark_to=max_modified or context.watermark_from or context.started,
    )


def modified_since_filter(integration: Integration, watermark_from, started) -> str | None:
    """Build the OData modified-since filter value (ISO-8601 UTC) or None for full sync."""
    if watermark_from:
        return _isoformat_z(watermark_from - _WATERMARK_OVERLAP)
    initial_days = int((integration.config or {}).get("initial_sync_days") or 0)
    if initial_days > 0:
        return _isoformat_z(started - timedelta(days=initial_days))
    return None


def _isoformat_z(value) -> str:
    # Business Central expects UTC with a trailing Z.
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _line_identifier(raw_po: dict, client: BusinessCentralClient) -> str:
    """PO identifier for the line fetch ("" when there is none).

    Live BC requires the GUID (``id``) — a PO number is never a valid substitute;
    dummy fixtures are keyed by PO number.
    """
    if getattr(client, "use_dummy", False):
        return raw_po.get("number") or raw_po.get("id") or ""
    return raw_po.get("id") or ""
