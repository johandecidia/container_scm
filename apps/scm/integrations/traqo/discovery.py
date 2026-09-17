"""Traqo's free carrier lookup, as a discovery step with four possible answers.

:mod:`.carrier_lookup` reads a lookup response into evidence and :func:`.service.lookup_traqo_carrier`
performs the call. Neither says what happened when the call did not happen. That
distinction is the entire value of this module to carrier resolution, because these are
four different situations and only one of them is about the container:

    FOUND           Traqo named a carrier
    NOT_FOUND       Traqo answered and named nobody
    NOT_CONFIGURED  no Traqo credential — we never asked
    ERROR           we asked and the call failed

A timeout is not "this box has no carrier", and a missing API key is not "Traqo has
never heard of it". Collapsing either into NOT_FOUND would make the next step in the
chain — a paid Vizion identification — run on the strength of our own outage.

What this module deliberately does not do: create a subscription, create a tracking
event, spend a shipment slot, or decide anything. The lookup endpoint is free and
writes nothing at Traqo either, which is why it sits first among the paid steps.

A named carrier is translated into the registry's own vocabulary through
:func:`~apps.scm.integrations.carriers.registry.resolve_carrier_code_from_scac`, not
through a mapping kept here. When Traqo names a SCAC no registered carrier claims, the
carrier is still *identified* — by that SCAC — and ``carrier_code`` stays empty rather
than being rounded to the nearest carrier we happen to have an adapter for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from apps.scm.integrations.carriers.exceptions import CarrierConfigurationError, CarrierError
from apps.scm.integrations.carriers.registry import get_carrier_definition, resolve_carrier_code_from_scac

from . import PROVIDER_CODE

logger = logging.getLogger(__name__)

FOUND = "found"
NOT_FOUND = "not_found"
NOT_CONFIGURED = "not_configured"
ERROR = "error"


@dataclass(frozen=True)
class TraqoCarrierDiscovery:
    """What Traqo's free lookup established about who is moving a container."""

    container_number: str
    status: str = NOT_FOUND
    # The registry code, when a registered carrier claims the SCAC Traqo named.
    carrier_code: str = ""
    carrier_name: str = ""
    # What Traqo actually said, kept whether or not a registered carrier claims it.
    scac: str = ""
    confidence: str = ""
    # Traqo's own reason, and rival candidate count — both weaken or strengthen the
    # claim without changing it, so they travel with it rather than being folded in.
    reason: str = ""
    rival_candidates: int = 0
    # For logs and diagnostics only. Can echo a response body, so it is never rendered.
    error_kind: str = ""
    error_message: str = ""
    evidence: dict = field(default_factory=dict)

    @property
    def found(self) -> bool:
        """True when Traqo named a carrier this system can act on.

        A SCAC with no registered carrier is not actionable: nothing could be routed to
        it and nothing could be asked of it, so it is reported (in ``scac``) and not
        treated as a resolution.
        """
        return self.status == FOUND and bool(self.carrier_code)

    @property
    def answered(self) -> bool:
        """True when Traqo was actually reached, whatever it said."""
        return self.status in (FOUND, NOT_FOUND)


def lookup_carrier_for_container(
    container_number: str,
    *,
    client=None,
    sandbox: bool = False,
) -> TraqoCarrierDiscovery:
    """Ask Traqo which carrier is moving ``container_number``. Never raises.

    ``client`` may be injected for testing; otherwise one is built from the
    installation's TRAQO_* settings. ``sandbox`` reaches Traqo's fixed demo data, which
    needs no credential.

    Every failure mode is classified into one of the four statuses above and returned,
    because a resolution chain that raised here would abandon the container instead of
    trying the next step.
    """
    number = (container_number or "").strip().upper()
    if not number:
        return TraqoCarrierDiscovery(
            container_number=number,
            status=NOT_FOUND,
            reason="No container number to look up.",
        )

    from .service import lookup_traqo_carrier

    try:
        lookup = lookup_traqo_carrier(reference=number, sandbox=sandbox, client=client)
    except CarrierConfigurationError as exc:
        # Never asked. Distinct from every other failure: nothing is wrong with Traqo
        # and nothing is wrong with the container.
        logger.info("Traqo carrier lookup for %s skipped — not configured.", number)
        return TraqoCarrierDiscovery(
            container_number=number,
            status=NOT_CONFIGURED,
            error_kind="not_configured",
            error_message=str(exc),
        )
    except CarrierError as exc:
        logger.warning(
            "Traqo carrier lookup for %s failed: %s (%s).",
            number,
            type(exc).__name__,
            exc,
        )
        return TraqoCarrierDiscovery(
            container_number=number,
            status=ERROR,
            error_kind=type(exc).__name__,
            error_message=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 — a reader bug must not end the chain
        logger.exception("Unexpected error in Traqo carrier lookup for %s.", number)
        return TraqoCarrierDiscovery(
            container_number=number,
            status=ERROR,
            error_kind="unexpected",
            error_message=f"{type(exc).__name__}: {exc}",
        )

    if not lookup.identified:
        logger.info("Traqo carrier lookup for %s: no carrier named.", number)
        return TraqoCarrierDiscovery(
            container_number=number,
            status=NOT_FOUND,
            reason=lookup.reason,
            evidence=lookup.as_dict(),
        )

    carrier_code = resolve_carrier_code_from_scac(lookup.scac) or ""
    carrier_name = _registered_name(carrier_code) or lookup.carrier_name
    logger.info(
        "Traqo carrier lookup for %s → %s (%s), %d rival candidate(s).",
        number,
        lookup.scac,
        carrier_code or "no registered carrier",
        len(lookup.rival_candidates),
    )
    return TraqoCarrierDiscovery(
        container_number=number,
        status=FOUND,
        carrier_code=carrier_code,
        carrier_name=carrier_name,
        scac=lookup.scac,
        confidence=lookup.confidence,
        reason=lookup.reason,
        rival_candidates=len(lookup.rival_candidates),
        evidence=lookup.as_dict(),
    )


def _registered_name(carrier_code: str) -> str:
    """The registry's own name for a carrier code, or "" when it is not registered."""
    from apps.scm.integrations.carriers.registry import UnknownCarrierError

    if not carrier_code:
        return ""
    try:
        return get_carrier_definition(carrier_code).name
    except UnknownCarrierError:  # pragma: no cover — the code came from the registry
        return ""


def is_traqo_configured() -> bool:
    """Whether live Traqo calls can be made at all.

    Read from settings rather than by attempting a call, so provider routing can rule
    Traqo out without spending a request. ``PROVIDER_CODE`` is re-exported through this
    module's name so routing has one import for everything Traqo.
    """
    from django.conf import settings

    return bool(getattr(settings, "TRAQO_ENABLED", False) and getattr(settings, "TRAQO_API_KEY", ""))


__all__ = [
    "ERROR",
    "FOUND",
    "NOT_CONFIGURED",
    "NOT_FOUND",
    "PROVIDER_CODE",
    "TraqoCarrierDiscovery",
    "is_traqo_configured",
    "lookup_carrier_for_container",
]
