"""Shipment-based carrier discovery.

Finds the containers on a shipment by asking its carrier about the booking
reference, bill of lading or shipment reference.

We ask the shipment's own carrier — never every registered carrier in turn. Fanning
out blindly would burn rate limits at nine carriers to answer one question, and a
match at the wrong carrier is worse than no match at all. A shipment whose carrier
cannot be resolved is reported as skipped, so the gap is visible instead of hidden
behind a broad sweep.

A booking reference is also what makes fanning out unnecessary here: it only means
anything at the carrier that issued it. A *container* number is different — it is
globally unique and any carrier can be asked about it — so when the carrier is
unknown there, ``carrier_discovery`` sweeps the team's configured carriers instead.
Planned-container discovery (starting from a container number rather than a
booking) lives in ``apps.scm.containers.discovery`` and uses that sweep. All three
share this package's registry, factory, error model and auto-link service.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import ReferenceKind
from .exceptions import (
    CarrierConfigurationError,
    CarrierError,
    CarrierNoDataError,
    CarrierNotImplementedError,
)
from .registry import UnknownCarrierError, get_carrier_definition, resolve_carrier_code
from .schemas import ContainerDiscoveryResult

if TYPE_CHECKING:
    from apps.scm.shipments.models import Shipment

logger = logging.getLogger(__name__)

# Which shipment field feeds which reference, in the order we prefer to ask.
# A booking reference identifies the shipment at the carrier most reliably; the
# internal reference is the weakest and is only tried when the carrier supports it.
_REFERENCE_PREFERENCE = (
    (ReferenceKind.BOOKING_NUMBER, "carrier_booking_reference", "supports_tracking_by_booking"),
    (ReferenceKind.BILL_OF_LADING, "bill_of_lading_number", "supports_tracking_by_bl"),
    (ReferenceKind.SHIPMENT_REFERENCE, "reference", "supports_tracking_by_shipment_reference"),
)

_DISCOVERY_KWARG = {
    ReferenceKind.BOOKING_NUMBER: "booking_number",
    ReferenceKind.BILL_OF_LADING: "bill_of_lading_number",
    ReferenceKind.SHIPMENT_REFERENCE: "shipment_reference",
}


def _empty_summary(**overrides) -> dict:
    summary = {
        "discovered_count": 0,
        "containers_created": 0,
        "containers_linked": 0,
        "subscriptions_created": 0,
        "errors": [],
        "skipped": False,
        "carrier_code": "",
        "reference_used": "",
    }
    summary.update(overrides)
    return summary


def get_shipment_carrier_code(shipment: Shipment) -> str | None:
    """Return the registered provider code for a shipment's carrier, or None.

    ``Shipment.carrier`` is free text, so it is resolved against the registry by
    code and by name. An unrecognised value returns None rather than a guess.
    """
    return resolve_carrier_code(shipment.carrier)


def discover_containers_for_shipment(
    shipment: Shipment,
    providers: list | None = None,
) -> dict:
    """Discover and link the containers on a shipment.

    ``providers`` may inject carrier clients for testing; when given, they are used
    instead of resolving the shipment's carrier through the factory.

    Returns a summary dict with:
      discovered_count, containers_created, containers_linked,
      subscriptions_created, errors, skipped, carrier_code, reference_used

    ``skipped`` is True when nothing could be asked — no reference, no resolvable
    carrier, or an adapter that is not implemented/configured. That is deliberately
    distinct from asking and finding nothing.
    """
    from .auto_link import create_or_link_discovered_container

    clients, carrier_code = _resolve_clients(shipment, providers)
    if not clients:
        logger.debug(
            "Shipment %s has no usable carrier for discovery (carrier=%r) — skipping.",
            shipment.pk,
            shipment.carrier,
        )
        return _empty_summary(skipped=True, carrier_code=carrier_code or "")

    all_results: list[ContainerDiscoveryResult] = []
    errors: list[str] = []
    reference_used = ""
    attempted = False

    for client in clients:
        query = _build_query(shipment, client)
        if query is None:
            continue
        kind, kwargs = query
        attempted = True
        try:
            results = client.discover_containers(**kwargs)
        except CarrierNoDataError:
            reference_used = reference_used or kind
            continue
        except (CarrierNotImplementedError, CarrierConfigurationError) as exc:
            logger.debug("Discovery unavailable for shipment %s: %s", shipment.pk, exc)
            attempted = False
            continue
        except CarrierError as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            logger.warning("Discovery error for shipment %s: %s", shipment.pk, exc)
            continue
        except Exception as exc:  # noqa: BLE001 — an adapter bug must not break the batch
            errors.append(f"{client.__class__.__name__}: {exc}")
            logger.exception("Unexpected discovery error for shipment %s", shipment.pk)
            continue

        reference_used = reference_used or kind
        all_results.extend(results or [])

    if not attempted and not errors:
        return _empty_summary(skipped=True, carrier_code=carrier_code or "")

    summary = _empty_summary(
        discovered_count=len(all_results),
        errors=errors,
        carrier_code=carrier_code or "",
        reference_used=reference_used,
    )

    for result in all_results:
        try:
            link_summary = create_or_link_discovered_container(
                team=shipment.team,
                shipment=shipment,
                result=result,
            )
        except Exception as exc:  # noqa: BLE001 — one bad container must not lose the others
            errors.append(f"Link failed for {result.container_number}: {exc}")
            logger.exception("Auto-link error for shipment %s", shipment.pk)
            continue
        summary["containers_created"] += int(bool(link_summary.get("container_created")))
        summary["containers_linked"] += int(bool(link_summary.get("shipment_container_created")))
        summary["subscriptions_created"] += int(bool(link_summary.get("subscription_created")))

    return summary


def _resolve_clients(shipment: Shipment, providers: list | None) -> tuple[list, str | None]:
    """Return (clients to ask, resolved carrier code).

    Injected providers are used as-is. Otherwise the shipment's own carrier is
    resolved and built for the shipment's team.
    """
    from .factory import build_carrier_client

    if providers is not None:
        return list(providers), get_shipment_carrier_code(shipment)

    carrier_code = get_shipment_carrier_code(shipment)
    if not carrier_code:
        return [], None

    try:
        definition = get_carrier_definition(carrier_code)
    except UnknownCarrierError:
        return [], carrier_code
    if not definition.capabilities.supports_pull:
        logger.debug("Carrier %s does not support pull-based discovery.", carrier_code)
        return [], carrier_code

    return [build_carrier_client(carrier_code, team=shipment.team)], carrier_code


def _build_query(shipment: Shipment, client) -> tuple[str, dict] | None:
    """Pick the best reference this client supports, or None when there is none."""
    capabilities = getattr(client, "capabilities", None)
    for kind, field_name, capability in _REFERENCE_PREFERENCE:
        value = (getattr(shipment, field_name, "") or "").strip()
        if not value:
            continue
        # An injected test client may not declare capabilities; then any reference is fair.
        if capabilities is not None and not getattr(capabilities, capability, False):
            continue
        return kind, {_DISCOVERY_KWARG[kind]: value}
    return None
