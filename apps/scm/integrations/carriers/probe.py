"""Asking a carrier whether it knows a reference yet.

Both discovery flows need the same question answered — "does this carrier know
about this reference?" — and must interpret the answer the same way:

    found       the carrier returned at least one event
    not found   the call worked, the carrier has nothing yet (a valid answer)
    skipped     nothing was asked: no carrier chosen, stub adapter, not configured
    error       the call was attempted and failed, classified by kind

This module is the single place that maps a carrier call onto those four
outcomes, so planned-container discovery and shipment discovery cannot drift
apart in how they treat "no data".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .exceptions import (
    CarrierAuthenticationError,
    CarrierConfigurationError,
    CarrierError,
    CarrierInvalidResponseError,
    CarrierNoDataError,
    CarrierNotImplementedError,
    CarrierRateLimitError,
    CarrierServerError,
    CarrierTimeoutError,
    CarrierUnsupportedReferenceError,
)
from .registry import UnknownCarrierError

if TYPE_CHECKING:
    from apps.teams.models import Team

    from .base import BaseCarrierClient
    from .dcsa.schemas import NormalisedTrackingEvent

logger = logging.getLogger(__name__)


class ProbeOutcome:
    FOUND = "found"
    NOT_FOUND = "not_found"
    SKIPPED = "skipped"
    ERROR = "error"


# Error classes that mean the question was never asked.
_SKIP_ERRORS = (CarrierNotImplementedError, CarrierConfigurationError, CarrierUnsupportedReferenceError)

_ERROR_KINDS: list[tuple[type[CarrierError], str]] = [
    (CarrierAuthenticationError, "authentication"),
    (CarrierRateLimitError, "rate_limit"),
    (CarrierTimeoutError, "timeout"),
    (CarrierServerError, "server_error"),
    (CarrierInvalidResponseError, "invalid_response"),
]


@dataclass
class ProbeResult:
    """The result of asking one carrier about one reference."""

    outcome: str
    carrier_code: str = ""
    events: list[NormalisedTrackingEvent] = field(default_factory=list)
    raw_payload: dict = field(default_factory=dict)
    error_kind: str = ""
    error_message: str = ""

    @property
    def found(self) -> bool:
        return self.outcome == ProbeOutcome.FOUND

    @property
    def attempted(self) -> bool:
        """True when a carrier call was actually made."""
        return self.outcome in (ProbeOutcome.FOUND, ProbeOutcome.NOT_FOUND, ProbeOutcome.ERROR)


def probe_container_number(
    *,
    team: Team,
    container_number: str,
    carrier_code: str,
    client: BaseCarrierClient | None = None,
) -> ProbeResult:
    """Ask ``carrier_code`` whether it knows ``container_number`` yet.

    ``client`` may be injected for testing; otherwise the team's configured
    adapter is built through the carrier factory. Never raises — the outcome is
    always reported in the result.
    """
    from .factory import build_carrier_client, build_carrier_parser

    if not carrier_code:
        return ProbeResult(
            outcome=ProbeOutcome.SKIPPED,
            error_kind="no_carrier",
            error_message="No carrier chosen for this container number.",
        )

    try:
        if client is None:
            client = build_carrier_client(carrier_code, team=team)
        parser = build_carrier_parser(carrier_code)
    except UnknownCarrierError as exc:
        return ProbeResult(
            outcome=ProbeOutcome.SKIPPED,
            carrier_code=carrier_code,
            error_kind="unknown_carrier",
            error_message=str(exc),
        )

    try:
        payload = client.fetch_tracking(container_number=container_number)
    except CarrierNoDataError:
        return ProbeResult(outcome=ProbeOutcome.NOT_FOUND, carrier_code=carrier_code)
    except _SKIP_ERRORS as exc:
        return ProbeResult(
            outcome=ProbeOutcome.SKIPPED,
            carrier_code=carrier_code,
            error_kind="not_configured",
            error_message=str(exc),
        )
    except CarrierError as exc:
        return ProbeResult(
            outcome=ProbeOutcome.ERROR,
            carrier_code=carrier_code,
            error_kind=_error_kind(exc),
            error_message=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 — an adapter bug must not break discovery
        logger.exception("Unexpected error probing %s at %s", container_number, carrier_code)
        return ProbeResult(
            outcome=ProbeOutcome.ERROR,
            carrier_code=carrier_code,
            error_kind="unexpected",
            error_message=f"{type(exc).__name__}: {exc}",
        )

    raw_payload = payload if isinstance(payload, dict) else {"payload": payload}

    try:
        events = parser.parse_tracking_events(payload)
    except CarrierNotImplementedError as exc:
        return ProbeResult(
            outcome=ProbeOutcome.SKIPPED,
            carrier_code=carrier_code,
            raw_payload=raw_payload,
            error_kind="not_configured",
            error_message=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            outcome=ProbeOutcome.ERROR,
            carrier_code=carrier_code,
            raw_payload=raw_payload,
            error_kind="parse_error",
            error_message=f"{type(exc).__name__}: {exc}",
        )

    if not events:
        # The carrier answered and has nothing yet — a valid answer, not a failure.
        return ProbeResult(outcome=ProbeOutcome.NOT_FOUND, carrier_code=carrier_code, raw_payload=raw_payload)

    return ProbeResult(
        outcome=ProbeOutcome.FOUND,
        carrier_code=carrier_code,
        events=events,
        raw_payload=raw_payload,
    )


def _error_kind(exc: CarrierError) -> str:
    for error_class, kind in _ERROR_KINDS:
        if isinstance(exc, error_class):
            return kind
    return "unexpected"
