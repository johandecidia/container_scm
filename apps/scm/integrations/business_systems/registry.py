# Business system registry — mirrors the carrier registry pattern.
from dataclasses import dataclass

from .base import BusinessSystemCapability


class UnknownBusinessSystemError(Exception):
    """Raised when an unsupported system_code is requested."""


@dataclass
class BusinessSystemDefinition:
    """Full metadata and class references for a registered business system."""

    system_code: str
    name: str
    client_class: type
    mapper_class: type
    capabilities: BusinessSystemCapability


def _build_registry() -> dict[str, BusinessSystemDefinition]:
    from .business_central.client import BusinessCentralClient
    from .business_central.mapper import BusinessCentralMapper
    from .john_evans.client import JohnEvansClient
    from .john_evans.mapper import JohnEvansMapper

    return {
        "business_central": BusinessSystemDefinition(
            system_code="business_central",
            name="Microsoft Business Central",
            client_class=BusinessCentralClient,
            mapper_class=BusinessCentralMapper,
            capabilities=BusinessSystemCapability(
                supports_sales_orders=True,
                supports_purchase_orders=True,
                supports_customers=True,
                supports_vendors=True,
                supports_items=True,
                supports_webhooks=True,
                supports_polling=True,
            ),
        ),
        "john_evans": BusinessSystemDefinition(
            system_code="john_evans",
            name="John Evans International",
            client_class=JohnEvansClient,
            mapper_class=JohnEvansMapper,
            capabilities=BusinessSystemCapability(
                supports_polling=True,
            ),
        ),
    }


_REGISTRY: dict[str, BusinessSystemDefinition] | None = None


def _get_registry() -> dict[str, BusinessSystemDefinition]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build_registry()
    return _REGISTRY


def get_business_system_definition(system_code: str) -> BusinessSystemDefinition:
    """Return the BusinessSystemDefinition for the given system_code.

    Raises UnknownBusinessSystemError for unregistered codes.
    """
    registry = _get_registry()
    if system_code not in registry:
        raise UnknownBusinessSystemError(
            f"No business system registered for system_code '{system_code}'. Known systems: {sorted(registry.keys())}"
        )
    return registry[system_code]


def list_business_systems() -> list[BusinessSystemDefinition]:
    """Return all registered business system definitions, sorted by system_code."""
    return sorted(_get_registry().values(), key=lambda d: d.system_code)
