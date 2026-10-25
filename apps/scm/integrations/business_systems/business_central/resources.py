"""Which Business Central resources can be synced, and how often.

Three conditions are kept apart, and all three must hold before a resource is
synced on a schedule:

  1. *defined* — the resource is an ``IntegrationSyncRun.ResourceType``;
  2. *implemented* — a sync function is registered in ``_SYNC_IMPLEMENTATIONS``;
  3. *enabled* — the integration's config switches it on.

Per-resource settings live under ``Integration.config["resources"]``::

    {"resources": {"purchase_orders": {"enabled": true, "interval_minutes": 30,
                                       "failure_backoff_minutes": 15}}}

Purchase orders keep their legacy settings: with no ``resources`` entry they are
enabled, and ``purchase_order_sync_interval_minutes`` /
``purchase_order_sync_failure_backoff_minutes`` still apply. Every other
resource is disabled unless its ``enabled`` is literally ``true``.

The integration-wide switches are unchanged and apply to every resource:
``sync_enabled`` (default true) allows any sync; ``scheduled_sync_enabled``
(default false) additionally lets Celery Beat run it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from django.utils.module_loading import import_string

from apps.scm.integrations.models import Integration, IntegrationSyncRun

ResourceType = IntegrationSyncRun.ResourceType

DEFAULT_INTERVAL_MINUTES = 30
DEFAULT_FAILURE_BACKOFF_MINUTES = 15

# resource -> dotted path of ``sync(integration, client=None, *, trigger_type)``.
# Imported lazily: the sync modules import this one.
_SYNC_IMPLEMENTATIONS: dict[str, str] = {
    ResourceType.PURCHASE_ORDERS: (
        "apps.scm.integrations.business_systems.business_central.sync.sync_purchase_orders_from_business_central"
    ),
    ResourceType.ITEM_LEDGER_ENTRIES: (
        "apps.scm.integrations.business_systems.business_central.item_ledger_sync."
        "sync_item_ledger_entries_from_business_central"
    ),
}

# Resources that are on unless explicitly switched off (legacy behaviour).
_ENABLED_BY_DEFAULT = frozenset({ResourceType.PURCHASE_ORDERS})

# Pre-``resources`` config keys still honoured for a resource.
_LEGACY_KEYS: dict[str, dict[str, str]] = {
    ResourceType.PURCHASE_ORDERS: {
        "interval_minutes": "purchase_order_sync_interval_minutes",
        "failure_backoff_minutes": "purchase_order_sync_failure_backoff_minutes",
    },
}


@dataclass(frozen=True)
class ResourceSettings:
    resource_type: str
    implemented: bool
    enabled: bool
    interval: timedelta
    failure_backoff: timedelta

    @property
    def label(self) -> str:
        return str(ResourceType(self.resource_type).label)

    @property
    def runnable(self) -> bool:
        """Implemented and enabled (manual or scheduled; scheduling also needs the integration opt-in)."""
        return self.implemented and self.enabled


def is_defined(resource_type: str) -> bool:
    return resource_type in ResourceType.values


def is_implemented(resource_type: str) -> bool:
    return resource_type in _SYNC_IMPLEMENTATIONS


def get_sync_function(resource_type: str) -> Callable | None:
    """The registered sync function for a resource, or None when it has no implementation."""
    path = _SYNC_IMPLEMENTATIONS.get(resource_type)
    return import_string(path) if path else None


def get_resource_config(integration: Integration, resource_type: str) -> dict:
    """The integration's ``config["resources"][resource_type]`` entry ({} when absent or malformed)."""
    resources = (integration.config or {}).get("resources")
    entry = resources.get(resource_type) if isinstance(resources, dict) else None
    return entry if isinstance(entry, dict) else {}


def _minutes(integration: Integration, resource_type: str, key: str, default: int) -> timedelta:
    config = integration.config or {}
    value = get_resource_config(integration, resource_type).get(key)
    if value in (None, ""):
        legacy_key = _LEGACY_KEYS.get(resource_type, {}).get(key)
        value = config.get(legacy_key) if legacy_key else None
    return timedelta(minutes=int(value or default))


def get_resource_settings(integration: Integration, resource_type: str) -> ResourceSettings:
    if not is_defined(resource_type):
        raise ValueError(f"unknown Business Central resource type: {resource_type!r}")
    entry = get_resource_config(integration, resource_type)
    # Only a literal ``true`` switches a resource on; absent falls back to its default.
    enabled = entry["enabled"] is True if "enabled" in entry else resource_type in _ENABLED_BY_DEFAULT
    return ResourceSettings(
        resource_type=resource_type,
        implemented=is_implemented(resource_type),
        enabled=enabled,
        interval=_minutes(integration, resource_type, "interval_minutes", DEFAULT_INTERVAL_MINUTES),
        failure_backoff=_minutes(
            integration, resource_type, "failure_backoff_minutes", DEFAULT_FAILURE_BACKOFF_MINUTES
        ),
    )


def is_sync_enabled(integration: Integration) -> bool:
    """Integration-wide switch for any sync, manual or scheduled (default on)."""
    return bool((integration.config or {}).get("sync_enabled", True))


def is_scheduled_sync_enabled(integration: Integration) -> bool:
    """Integration-wide opt-in for Celery Beat (default off; only a literal ``true`` counts)."""
    return is_sync_enabled(integration) and (integration.config or {}).get("scheduled_sync_enabled") is True


def is_resource_schedulable(integration: Integration, resource_type: str) -> bool:
    """Defined, implemented, enabled, and the integration has opted in to scheduled sync."""
    if not is_defined(resource_type) or not is_scheduled_sync_enabled(integration):
        return False
    return get_resource_settings(integration, resource_type).runnable


def schedulable_resources(integration: Integration) -> list[str]:
    return [r for r in ResourceType.values if is_resource_schedulable(integration, r)]
