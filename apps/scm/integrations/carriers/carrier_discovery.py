"""Finding out *which* carrier can tell us about a container number.

A container number on its own does not say who is moving the box. ISO 6346 names
its owner, not its operator, and a leased box travels under whichever carrier
booked it. So when nothing yet tracks a container, the only honest way to learn
its carrier is to ask the ones we can actually call:

    container number
        → candidate carriers, best signal first
        → probe each in turn
        → the first one that returns real events is the verified tracking source

That inverts the old rule. Previously a container without a known carrier was a
dead end the user had to resolve by hand; now carrier identity is something the
visibility layer discovers, and being *asked* still never makes a carrier this
container's carrier — only answering with data does.

What this module is careful about:

Candidates are only carriers worth asking.
    Registered, able to be pulled from (``supports_pull``), able to answer by
    container number (``supports_tracking_by_container``), and with an active
    carrier integration for this team. Anything else would be an HTTP call that
    cannot succeed, or a rate-limit budget spent on a certainty.

Signals order the list; they do not shorten it.
    An explicit carrier from a shipment or a planned container goes first because
    somebody chose it, and the ISO 6346 owner prefix goes next because it is often
    right. Neither is proof: a carrier that answers NOT_FOUND is simply wrong about
    this box, and discovery moves on to the rest of the team's carriers.

One carrier's bad day is not the answer.
    NOT_FOUND, SKIPPED and ERROR are kept apart and none of them stops the sweep,
    so Maersk having nothing and CMA CGM timing out still leaves COSCO free to win.

Nothing is written here.
    The result describes what was asked and what came back. Creating the
    subscription and storing the events is the caller's job, through the existing
    tracking write path — this module owns no persistence and no transport of its
    own, only :func:`~apps.scm.integrations.carriers.probe.probe_container_number`.

This is the third discovery use case in this package, and the one the other two
lean on when the carrier is unknown: shipment-based discovery lives in
``discovery_service`` (starting from a booking) and planned-container discovery in
``apps.scm.containers.discovery`` (starting from a number we expect to see).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .probe import ProbeOutcome, probe_container_number
from .registry import UnknownCarrierError, get_carrier_definition, resolve_carrier_code, suggest_carrier_for_owner_code

if TYPE_CHECKING:
    from apps.teams.models import Team

    from .base import BaseCarrierClient
    from .dcsa.schemas import NormalisedTrackingEvent

logger = logging.getLogger(__name__)

# Why a carrier is on the list, in the order the signals are trusted.
SOURCE_PREFERRED = "preferred"  # named by a shipment, a planned container, the caller
SOURCE_OWNER_PREFIX = "owner_prefix"  # suggested by ISO 6346 — a hint, never proof
SOURCE_CONFIGURED = "configured"  # simply connected for this team

# Why a candidate cannot be asked.
SKIP_UNKNOWN_CARRIER = "unknown_carrier"
SKIP_UNSUPPORTED = "unsupported"
SKIP_NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True)
class CarrierCandidate:
    """A carrier that might know this container, and whether we can ask it."""

    carrier_code: str
    carrier_name: str
    source: str
    usable: bool = True
    # Only set when ``usable`` is False.
    skip_reason: str = ""


@dataclass(frozen=True)
class CarrierAttempt:
    """What one candidate carrier had to say."""

    carrier_code: str
    carrier_name: str
    outcome: str  # a ProbeOutcome value
    source: str = SOURCE_CONFIGURED
    error_kind: str = ""
    # The carrier's own words. Can echo a response body or a credential, so it is
    # for logs and internal diagnostics only — never for anything a user reads.
    error_message: str = ""

    @property
    def answered(self) -> bool:
        """True when the carrier was actually reached, whatever it said."""
        return self.outcome in (ProbeOutcome.FOUND, ProbeOutcome.NOT_FOUND, ProbeOutcome.ERROR)


@dataclass
class CarrierDiscoveryOutcome:
    """The result of sweeping a container number across candidate carriers.

    ``found`` is the only thing that authorises a write. Everything else exists so
    the caller can tell "nobody has this box" from "we could not ask anybody".
    """

    carrier_code: str = ""
    carrier_name: str = ""
    events: list[NormalisedTrackingEvent] = field(default_factory=list)
    raw_payload: dict = field(default_factory=dict)
    attempts: list[CarrierAttempt] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.carrier_code and self.events)

    def _with_outcome(self, outcome: str) -> list[CarrierAttempt]:
        return [attempt for attempt in self.attempts if attempt.outcome == outcome]

    @property
    def not_found(self) -> list[CarrierAttempt]:
        """Carriers that answered and do not know this container."""
        return self._with_outcome(ProbeOutcome.NOT_FOUND)

    @property
    def skipped(self) -> list[CarrierAttempt]:
        """Carriers that could not be asked — not connected, or a stub adapter."""
        return self._with_outcome(ProbeOutcome.SKIPPED)

    @property
    def errored(self) -> list[CarrierAttempt]:
        """Carriers that were asked and failed technically."""
        return self._with_outcome(ProbeOutcome.ERROR)

    @property
    def answered(self) -> list[CarrierAttempt]:
        """Carriers that were actually reached."""
        return [attempt for attempt in self.attempts if attempt.answered]

    @property
    def error_kinds(self) -> list[str]:
        """The distinct technical failure kinds seen, for logging and triage."""
        return sorted({attempt.error_kind for attempt in self.errored if attempt.error_kind})

    def carrier_names(self, attempts: list[CarrierAttempt] | None = None) -> list[str]:
        """Display names, de-duplicated, in the order they were tried."""
        names: list[str] = []
        for attempt in self.attempts if attempts is None else attempts:
            if attempt.carrier_name not in names:
                names.append(attempt.carrier_name)
        return names


def build_carrier_candidates(
    *,
    team: Team,
    container_number: str = "",
    preferred_carrier_codes: list[str] | tuple[str, ...] = (),
    also_usable: frozenset[str] = frozenset(),
    exclude_carrier_codes: frozenset[str] = frozenset(),
) -> list[CarrierCandidate]:
    """Return the carriers worth asking about ``container_number``, best first.

    Order: explicitly preferred carriers (in the order given), then the carrier
    suggested by the ISO 6346 owner prefix, then the team's other active carrier
    integrations. Duplicates are dropped, keeping the strongest signal.

    A carrier that cannot be asked — unregistered, no pull support, no tracking by
    container number, or not connected for this team — is dropped, *except* when it
    was explicitly preferred. Those are kept, marked unusable, so the caller can say
    "the carrier your shipment names is not connected yet" rather than silently
    ignoring the one piece of evidence the user provided.

    ``also_usable`` names carrier codes to treat as connected regardless of the
    team's integrations; discovery uses it for injected test clients.

    ``exclude_carrier_codes`` drops carriers the caller has already answered its own
    question about — a continuation sweep passes the sources it has just polled, so
    the sweep spends its calls on carriers that might add something rather than
    re-asking one that has just said nothing new. They are dropped entirely, not
    marked unusable: they were not skipped for want of a connection.
    """
    configured = _configured_carrier_codes(team)
    excluded = frozenset(code for code in (resolve_carrier_code(value) for value in exclude_carrier_codes) if code)

    signals: list[tuple[str, str]] = []
    for value in preferred_carrier_codes:
        code = resolve_carrier_code(value)
        if code:
            signals.append((code, SOURCE_PREFERRED))
    # The owner prefix only ever moves a carrier up the list; it is never evidence.
    hint = suggest_carrier_for_owner_code(container_number[:4]) if container_number else None
    if hint:
        signals.append((hint, SOURCE_OWNER_PREFIX))
    # Sorted, because nothing distinguishes these: a stable order makes a sweep
    # reproducible, and keeps the same carrier from being asked first by accident.
    signals.extend((code, SOURCE_CONFIGURED) for code in sorted(configured))

    candidates: list[CarrierCandidate] = []
    seen: set[str] = set()
    for code, source in signals:
        if code in seen or code in excluded:
            continue
        seen.add(code)
        candidate = _describe_candidate(code, source, configured=configured | also_usable)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def discover_carrier_for_container(
    *,
    team: Team,
    container_number: str,
    preferred_carrier_codes: list[str] | tuple[str, ...] = (),
    clients: dict[str, BaseCarrierClient] | None = None,
    exclude_carrier_codes: frozenset[str] = frozenset(),
) -> CarrierDiscoveryOutcome:
    """Ask candidate carriers about ``container_number`` until one has real data.

    Stops at the first carrier that returns normalised events; every other outcome
    is recorded and the sweep continues. Never raises — the probe classifies every
    carrier failure, and the outcome always describes what happened.

    Stopping at the first hit is a decision about *this* sweep, not about the
    container: finding one carrier that knows the box does not mean no other does,
    and a later sweep is free to find a second. What ends discovery for a container
    is its journey being explained, not a carrier having once been found.

    ``clients`` may inject carrier adapters by provider code for testing; the codes
    it names are treated as connected. ``exclude_carrier_codes`` leaves carriers out
    of the sweep entirely — see :func:`build_carrier_candidates`.
    """
    clients = clients or {}
    candidates = build_carrier_candidates(
        team=team,
        container_number=container_number,
        preferred_carrier_codes=preferred_carrier_codes,
        also_usable=frozenset(clients),
        exclude_carrier_codes=exclude_carrier_codes,
    )

    outcome = CarrierDiscoveryOutcome()
    for candidate in candidates:
        if not candidate.usable:
            outcome.attempts.append(
                CarrierAttempt(
                    carrier_code=candidate.carrier_code,
                    carrier_name=candidate.carrier_name,
                    outcome=ProbeOutcome.SKIPPED,
                    source=candidate.source,
                    error_kind=candidate.skip_reason,
                    error_message=f"{candidate.carrier_code}: {candidate.skip_reason}",
                )
            )
            continue

        probe = probe_container_number(
            team=team,
            container_number=container_number,
            carrier_code=candidate.carrier_code,
            client=clients.get(candidate.carrier_code),
        )
        outcome.attempts.append(
            CarrierAttempt(
                carrier_code=candidate.carrier_code,
                carrier_name=candidate.carrier_name,
                outcome=probe.outcome,
                source=candidate.source,
                error_kind=probe.error_kind,
                error_message=probe.error_message,
            )
        )

        if probe.outcome != ProbeOutcome.FOUND:
            # Technical detail belongs in the log, never in the answer we return:
            # a carrier's error text can echo a response body or a credential.
            logger.info(
                "Carrier discovery: %s not resolved by %s → %s (%s).",
                container_number,
                candidate.carrier_code,
                probe.outcome.upper(),
                probe.error_message or probe.error_kind or "no detail",
            )
            continue

        outcome.carrier_code = candidate.carrier_code
        outcome.carrier_name = candidate.carrier_name
        outcome.events = probe.events
        outcome.raw_payload = probe.raw_payload
        logger.info(
            "Carrier discovery: %s resolved to %s after %d candidate(s), %d event(s).",
            container_number,
            candidate.carrier_code,
            len(outcome.attempts),
            len(probe.events),
        )
        break

    if not outcome.found:
        logger.info(
            "Carrier discovery: %s not resolved. %d asked, %d without data, %d skipped, %d failed (%s).",
            container_number,
            len(outcome.answered),
            len(outcome.not_found),
            len(outcome.skipped),
            len(outcome.errored),
            ", ".join(outcome.error_kinds) or "no errors",
        )
    return outcome


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _configured_carrier_codes(team: Team) -> frozenset[str]:
    """The provider codes this team has an active carrier integration for."""
    from apps.scm.integrations.models import Integration

    return frozenset(
        Integration.objects.filter(
            team=team,
            provider_family=Integration.ProviderFamily.CARRIER,
            is_active=True,
        ).values_list("provider_code", flat=True)
    )


def _describe_candidate(code: str, source: str, *, configured: frozenset[str]) -> CarrierCandidate | None:
    """Turn a carrier code into a candidate, or None when it is not worth reporting.

    An unusable carrier is only worth reporting when somebody named it explicitly;
    a hint or a merely-connected carrier that cannot answer this question is noise.
    """

    def unusable(name: str, reason: str) -> CarrierCandidate | None:
        if source != SOURCE_PREFERRED:
            return None
        return CarrierCandidate(carrier_code=code, carrier_name=name, source=source, usable=False, skip_reason=reason)

    try:
        definition = get_carrier_definition(code)
    except UnknownCarrierError:
        return unusable(code, SKIP_UNKNOWN_CARRIER)

    capabilities = definition.capabilities
    if not (capabilities.supports_pull and capabilities.supports_tracking_by_container):
        # Asking a webhook-only or booking-only carrier by container number cannot
        # work, so it never joins an interactive sweep.
        return unusable(definition.name, SKIP_UNSUPPORTED)

    if code not in configured:
        return unusable(definition.name, SKIP_NOT_CONFIGURED)

    return CarrierCandidate(carrier_code=code, carrier_name=definition.name, source=source)
