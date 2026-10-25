"""Business Central location codes → Pegasus locations, and the automatic-receive switch.

Configured per integration by a Team Administrator (Settings → Integrations →
Business Central → Locations); nothing is created automatically. A location code
without an active mapping is never received automatically.

Switching automatic receive on records the *activation boundary*: the highest item
ledger ``entryNo`` already synced. Only receipt entries above it can be received,
so enabling the switch never replays historical receipts — whatever their posting
date says (BC allows backdating; ``entryNo`` is posting order). The boundary can
only be set once the item ledger sync has caught up with BC; otherwise entries
imported later would sit above it while being history.
"""

from __future__ import annotations

from django.db.models import Max
from django.utils import timezone

from apps.scm.integrations.models import (
    BusinessCentralItemLedgerEntry,
    BusinessCentralLocationMapping,
    Integration,
    IntegrationSyncRun,
)
from apps.scm.integrations.services import get_last_successful_cursor

from .exceptions import BusinessCentralConfigurationError

Mapping = BusinessCentralLocationMapping
_ILE = IntegrationSyncRun.ResourceType.ITEM_LEDGER_ENTRIES


def normalise_location_code(value: str) -> str:
    """BC location codes are Code[10]-style identifiers: trimmed and upper-case."""
    return (value or "").strip().upper()


def get_location_mappings(integration: Integration) -> dict[str, Mapping]:
    """The integration's mappings keyed by normalised BC location code."""
    mappings = Mapping.objects.filter(integration=integration).select_related("pegasus_location")
    return {m.bc_location_code: m for m in mappings}


def activation_boundary(integration: Integration) -> int:
    """The item ledger entry no. automatic receive starts after.

    Raises BusinessCentralConfigurationError unless the latest completed item
    ledger sync read everything BC had (``has_more`` false).
    """
    latest = (
        IntegrationSyncRun.objects.filter(
            integration=integration, resource_type=_ILE, status=IntegrationSyncRun.Status.COMPLETED
        )
        .order_by("-finished_at", "-pk")
        .first()
    )
    if latest is None or (latest.metadata or {}).get("has_more") is not False:
        raise BusinessCentralConfigurationError(
            "the item ledger entries must be fully synced before automatic receive can be enabled"
        )
    cursor = get_last_successful_cursor(integration, _ILE)
    stored = BusinessCentralItemLedgerEntry.objects.filter(integration=integration).aggregate(m=Max("entry_no"))["m"]
    candidates = [value for value in (cursor, stored) if value is not None]
    return max(candidates) if candidates else 0


def apply_auto_receive_switch(mapping: Mapping, *, was_enabled: bool) -> None:
    """Set or clear the activation boundary when the switch changes (before save).

    Re-enabling sets a new boundary: receipts posted while it was off stay history.
    """
    if mapping.auto_receive_enabled and not was_enabled:
        mapping.auto_receive_after_entry_no = activation_boundary(mapping.integration)
        mapping.auto_receive_enabled_at = timezone.now()
    elif not mapping.auto_receive_enabled:
        mapping.auto_receive_after_entry_no = None
        mapping.auto_receive_enabled_at = None
        mapping.auto_create_containers = False
