"""Vizion's Auto Carrier Identification, as the last and dearest discovery step.

:func:`.service.resolve_carrier_via_aci` performs the identification and returns
evidence. This module wraps it in the same five-valued vocabulary the rest of discovery
speaks, so carrier resolution can treat Traqo's lookup, a direct probe and an ACI answer
as steps of one chain:

    FOUND           Vizion attached a carrier to the reference
    NOT_FOUND       no supported carrier had data — Vizion keeps retrying for 7 days
    PENDING         Vizion has not answered yet; the reference is alive
    NOT_CONFIGURED  no Vizion credential — we never asked
    ERROR           we asked and the call failed

PENDING is kept apart from NOT_FOUND because identification is asynchronous: the create
returns before Vizion has searched, and reporting "no carrier" after sixty seconds would
be a statement about our patience rather than about the box.

**This step costs money.** Unlike Traqo's free lookup, creating the reference *is* the
billable unit, and it is created whether or not the carrier turns out to be trackable
elsewhere. That is why it is last in the chain and why the reference id comes back in
the result: a caller that has paid for a reference should be able to read it rather than
pay again. Nothing here starts full Vizion tracking — ``ingest_vizion_container`` is a
separate call a separate decision has to reach.

The carrier Vizion names is translated through
:func:`~apps.scm.integrations.carriers.registry.resolve_carrier_code_from_scac`. Vizion's
``carrier_code`` is already SCAC-shaped and stable across the SCAC changes a line may
make, which is exactly what the registry's per-carrier SCAC tuple is for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TypedDict

from apps.scm.integrations.carriers.exceptions import CarrierConfigurationError, CarrierError
from apps.scm.integrations.carriers.registry import get_carrier_definition, resolve_carrier_code_from_scac

from . import PROVIDER_CODE
from .schemas import ACI_FAILED, ACI_IDENTIFIED, ACI_NOT_FOUND

logger = logging.getLogger(__name__)

FOUND = "found"
NOT_FOUND = "not_found"
PENDING = "pending"
NOT_CONFIGURED = "not_configured"
ERROR = "error"


@dataclass(frozen=True)
class VizionCarrierIdentification:
    """What one ACI attempt established, and what it cost."""

    container_number: str
    status: str = PENDING
    carrier_code: str = ""
    carrier_name: str = ""
    scac: str = ""
    # The reference Vizion created. Present whenever the call reached Vizion at all,
    # including on NOT_FOUND and PENDING — it was paid for either way, and a later
    # fetch or a retry needs it.
    reference_id: str = ""
    aci_state: str = ""
    polls: int = 0
    waited_seconds: float = 0.0
    error_kind: str = ""
    error_message: str = ""
    evidence: dict = field(default_factory=dict)

    @property
    def found(self) -> bool:
        """True when Vizion named a carrier this system can act on."""
        return self.status == FOUND and bool(self.carrier_code)

    @property
    def answered(self) -> bool:
        """True when Vizion reached a conclusion, either way."""
        return self.status in (FOUND, NOT_FOUND)

    @property
    def reference_created(self) -> bool:
        """True when a billable Vizion reference now exists for this container."""
        return bool(self.reference_id)


class _AciFacts(TypedDict):
    """What every answered ACI attempt carries, whatever it concluded.

    A TypedDict rather than a plain dict so the four ``**`` expansions below stay
    checked against :class:`VizionCarrierIdentification`'s own field types: these are
    the fields that are true of the attempt rather than of the verdict, and a typo in
    one of them would otherwise only surface as a missing piece of billing evidence.
    """

    container_number: str
    reference_id: str
    aci_state: str
    polls: int
    waited_seconds: float
    evidence: dict


def identify_carrier(
    container_number: str,
    *,
    client=None,
    demo: bool = False,
    poll_attempts: int | None = None,
    poll_interval_seconds: float | None = None,
) -> VizionCarrierIdentification:
    """Ask Vizion which carrier is moving ``container_number``. Never raises.

    No carrier hint is sent and none can be — the request body is the container number
    alone, which is what invokes ACI. ``client`` may be injected for testing.

    ``poll_attempts`` and ``poll_interval_seconds`` default to the service's own values;
    a caller with somebody waiting on a web request should lower them and accept PENDING
    rather than hold the worker.
    """
    number = (container_number or "").strip().upper()
    if not number:
        return VizionCarrierIdentification(container_number=number, status=NOT_FOUND)

    from .service import DEFAULT_ACI_POLL_ATTEMPTS, DEFAULT_ACI_POLL_INTERVAL_SECONDS, resolve_carrier_via_aci

    try:
        result = resolve_carrier_via_aci(
            container_number=number,
            demo=demo,
            client=client,
            poll_attempts=DEFAULT_ACI_POLL_ATTEMPTS if poll_attempts is None else poll_attempts,
            poll_interval_seconds=(
                DEFAULT_ACI_POLL_INTERVAL_SECONDS if poll_interval_seconds is None else poll_interval_seconds
            ),
        )
    except CarrierConfigurationError as exc:
        logger.info("Vizion ACI for %s skipped — not configured.", number)
        return VizionCarrierIdentification(
            container_number=number,
            status=NOT_CONFIGURED,
            error_kind="not_configured",
            error_message=str(exc),
        )
    except CarrierError as exc:
        # A 401 means our credential is wrong. It says nothing whatever about whether
        # Vizion knows this container, and must never be read as NOT_FOUND.
        logger.warning("Vizion ACI for %s failed: %s (%s).", number, type(exc).__name__, exc)
        return VizionCarrierIdentification(
            container_number=number,
            status=ERROR,
            error_kind=type(exc).__name__,
            error_message=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 — an adapter bug must not end the chain
        logger.exception("Unexpected error in Vizion ACI for %s.", number)
        return VizionCarrierIdentification(
            container_number=number,
            status=ERROR,
            error_kind="unexpected",
            error_message=f"{type(exc).__name__}: {exc}",
        )

    reference = result.reference
    common: _AciFacts = {
        "container_number": number,
        "reference_id": reference.reference_id,
        "aci_state": reference.aci_state,
        "polls": result.polls,
        "waited_seconds": result.waited_seconds,
        "evidence": reference.as_dict(),
    }

    if reference.aci_state == ACI_IDENTIFIED:
        scac = reference.carrier_identifier
        carrier_code = resolve_carrier_code_from_scac(scac) or ""
        logger.info(
            "Vizion ACI for %s → %s (%s) after %d poll(s).",
            number,
            scac,
            carrier_code or "no registered carrier",
            result.polls,
        )
        return VizionCarrierIdentification(
            status=FOUND,
            carrier_code=carrier_code,
            carrier_name=_registered_name(carrier_code) or reference.carrier_name,
            scac=scac,
            **common,
        )

    if reference.aci_state == ACI_NOT_FOUND:
        return VizionCarrierIdentification(status=NOT_FOUND, **common)

    if reference.aci_state == ACI_FAILED:
        return VizionCarrierIdentification(
            status=ERROR,
            error_kind="aci_failed",
            error_message=reference.deactivate_reason or reference.last_update_status,
            **common,
        )

    # Still looking. The reference stays alive and Vizion retries on its own schedule.
    return VizionCarrierIdentification(status=PENDING, **common)


def _registered_name(carrier_code: str) -> str:
    from apps.scm.integrations.carriers.registry import UnknownCarrierError

    if not carrier_code:
        return ""
    try:
        return get_carrier_definition(carrier_code).name
    except UnknownCarrierError:  # pragma: no cover — the code came from the registry
        return ""


def is_vizion_configured() -> bool:
    """Whether Vizion calls can be made at all, read from settings rather than tried."""
    from django.conf import settings

    return bool(getattr(settings, "VIZION_ENABLED", False) and getattr(settings, "VIZION_API_KEY", ""))


__all__ = [
    "ERROR",
    "FOUND",
    "NOT_CONFIGURED",
    "NOT_FOUND",
    "PENDING",
    "PROVIDER_CODE",
    "VizionCarrierIdentification",
    "identify_carrier",
    "is_vizion_configured",
]
