"""Sync orchestration for Microsoft Business Central purchase orders."""

import logging

from apps.teams.models import Team

from .client import BusinessCentralClient
from .mapper import BusinessCentralMapper

logger = logging.getLogger(__name__)

_mapper = BusinessCentralMapper()


def sync_purchase_orders_from_business_central(
    team: Team,
    client: BusinessCentralClient | None = None,
) -> list:
    """Fetch, map, and upsert purchase orders from Business Central.

    Args:
        team: The team that owns the purchase orders.
        client: Optional client instance. Defaults to live BusinessCentralClient.
                Pass a dummy client (use_dummy=True) in tests.

    Returns:
        List of PurchaseOrder instances created or updated.
    """
    from apps.scm.procurement.services import upsert_purchase_orders

    if client is None:
        client = BusinessCentralClient()

    raw_pos = client.fetch_purchase_orders()
    logger.debug("BC sync: fetched %d purchase orders for team %s", len(raw_pos), team.slug)

    normalized_data = []
    for raw_po in raw_pos:
        po_id = raw_po.get("number") or raw_po.get("id")
        raw_lines = client.fetch_purchase_order_lines(po_id)
        normalized = _mapper.map_purchase_order(raw_po, raw_lines)
        normalized_data.append(normalized.model_dump())

    orders = upsert_purchase_orders(team=team, purchase_orders_data=normalized_data)
    logger.info("BC sync: upserted %d purchase orders for team %s", len(orders), team.slug)
    return orders
