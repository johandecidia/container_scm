"""Asking Traqo about a few likely carriers, when its free lookup could not name one.

BBCU3273070 is why this module exists. Traqo's carrier lookup did not recognise the
number, so carrier resolution fell through to the direct sweep and then to a paid Vizion
identification — which named ONE, after which Traqo tracked the box perfectly well the
moment it was told ``sealine=ONEY``. Traqo knew the shipment all along. It just could not
answer "who moves this" without being told who to ask about.

So this is the step between the two: given a container nobody has named a carrier for,
ask Traqo's *container* endpoint about a short, ordered list of likely carriers and see
whether one of them comes back with the box's tracking data.

Two things separate it from :mod:`.discovery`, and both are about cost.

**The lookup is free; this is not.** ``carriers/lookup`` creates nothing at Traqo and
draws on its own quota, which is why resolution still tries it first and why this module
never replaces it. ``container`` may consume one of the account's shipment slots, so
probing is capped (:data:`MAX_TRAQO_PROBE_ATTEMPTS`), ordered by whatever evidence exists,
and stops at the first carrier that answers with data.

**A lookup names a carrier; a probe proves one.** Sending ``sealine=ONEY`` and receiving
HTTP 200 is not evidence: the sealine is the *question*. What makes a probe an answer is
Traqo returning this container's shipment with events in it — see :func:`_verify`. That is
the same standard a direct carrier probe meets, which is why a probe hit is recorded as
verified and a lookup hit is not.

Nothing here is written. The result carries the payload and the mapped events out so the
caller can store what has already been fetched instead of asking Traqo the same question
again; :mod:`apps.scm.tracking.activation` is what makes it real.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from apps.scm.integrations.carriers.carrier_discovery import SOURCE_OWNER_PREFIX, SOURCE_PREFERRED
from apps.scm.integrations.carriers.exceptions import (
    CarrierAuthenticationError,
    CarrierConfigurationError,
    CarrierError,
    CarrierNoDataError,
    CarrierRateLimitError,
    CarrierUnsupportedReferenceError,
)
from apps.scm.integrations.carriers.registry import (
    UnknownCarrierError,
    get_carrier_definition,
    resolve_carrier_code,
    suggest_carrier_for_owner_code,
)

from . import PROVIDER_CODE
from .discovery import ERROR, FOUND, NOT_CONFIGURED, NOT_FOUND, is_traqo_configured
from .sealines import carrier_code_for_sealine, sealine_for_carrier_code

logger = logging.getLogger(__name__)

# Outcome values are :mod:`.discovery`'s, so the probe, the lookup and carrier resolution
# all read as one vocabulary. SKIPPED is the one the lookup has no use for: a candidate
# Traqo publishes no sealine for was never asked, which is neither an answer nor a fault.
SKIPPED = "skipped"

# Why a carrier is on the list, weakest last. The first two are carrier discovery's own
# constants rather than copies — a candidate "preferred because the shipment says so"
# means the same thing here as it does in a direct sweep.
SOURCE_DISCOVERY_ORDER = "discovery_order"

# Which carriers to try, and in what order, when nothing about the container says.
#
# A *discovery priority*, not a carrier registry: identity, display name and SCAC all
# still come from the registry and from :mod:`.sealines`, and a code listed here that
# Traqo publishes no sealine for is simply skipped. ONE leads because it is the carrier
# this whole step was built for — the one Traqo can track and cannot recognise — and the
# rest follow global container volume, which is the best available proxy for "most likely
# to be the answer" in the absence of any evidence about this particular box.
#
# Order is the only thing tuning this changes. Nothing downstream reads it.
DEFAULT_TRAQO_DISCOVERY_ORDER: tuple[str, ...] = (
    "one",
    "maersk",
    "cma_cgm",
    "msc",
    "hapag_lloyd",
    "cosco",
    "yang_ming",
    "hmm",
    "zim",
)

# How many Traqo container calls one probe may spend.
#
# The point of a cap is that the list above is a guess. Five candidates is enough for the
# evidence-ordered cases to land and for an unguided probe to cover the carriers most
# boxes move with; beyond that the marginal chance of a hit falls off while every attempt
# still costs a request, and a container nobody can name is better handed to the next step
# in the chain than swept across every carrier Traqo covers.
MAX_TRAQO_PROBE_ATTEMPTS = 5


@dataclass(frozen=True)
class TraqoProbeCandidate:
    """One carrier worth asking Traqo about, and the SCAC to ask with."""

    carrier_code: str
    sealine: str
    source: str = SOURCE_DISCOVERY_ORDER

    @property
    def carrier_name(self) -> str:
        return _registered_name(self.carrier_code)


@dataclass(frozen=True)
class TraqoProbeAttempt:
    """What Traqo said when asked about one candidate carrier."""

    carrier_code: str
    sealine: str
    outcome: str
    source: str = SOURCE_DISCOVERY_ORDER
    # Diagnostics. ``error_message`` can echo a Traqo response body, so it is for logs
    # only and never rendered to a user. Neither ever contains a credential: the client
    # keeps the key out of URLs and Traqo's own messages never quote the header.
    error_kind: str = ""
    error_message: str = ""

    @property
    def answered(self) -> bool:
        """True when Traqo was actually reached, whatever it said."""
        return self.outcome in (FOUND, NOT_FOUND, ERROR)

    def __str__(self) -> str:
        suffix = f" ({self.error_kind or self.error_message})" if self.outcome == ERROR else ""
        return f"{self.sealine or self.carrier_code} → {self.outcome.upper()}{suffix}"


@dataclass(frozen=True)
class TraqoCarrierProbeResult:
    """What probing established, and what it cost to establish it.

    ``found`` is the only thing that authorises acting on the carrier. The rest exists so
    a caller can tell apart the situations that need different things said and done:
    Traqo has nothing under any of these carriers, Traqo could not be asked at all, and
    every call we made failed.
    """

    container_number: str
    outcome: str = SKIPPED
    carrier_code: str = ""
    carrier_name: str = ""
    # The SCAC the answer came under — Traqo's own, where it stated one. This is what a
    # later fetch needs in order to ask the same question again.
    sealine: str = ""
    events: tuple = ()
    raw_payload: dict = field(default_factory=dict)
    attempts: tuple[TraqoProbeAttempt, ...] = ()
    error_kind: str = ""
    error_message: str = ""

    @property
    def found(self) -> bool:
        """True when a carrier was established by this container's own tracking data."""
        return self.outcome == FOUND and bool(self.carrier_code) and bool(self.events)

    @property
    def calls_made(self) -> int:
        """Traqo container requests actually spent."""
        return len([attempt for attempt in self.attempts if attempt.answered])

    @property
    def summary(self) -> str:
        """Every attempt on one line, for the log."""
        return ", ".join(str(attempt) for attempt in self.attempts) or "no candidates"


def build_traqo_probe_candidates(
    *,
    container_number: str = "",
    preferred_carrier_codes: Sequence[str] = (),
    exclude_carrier_codes: frozenset[str] = frozenset(),
    use_owner_prefix_hint: bool = True,
    discovery_order: Sequence[str] = DEFAULT_TRAQO_DISCOVERY_ORDER,
) -> list[TraqoProbeCandidate]:
    """Return the carriers worth asking Traqo about, best first.

    Ordering, strongest signal first:

    1. ``preferred_carrier_codes``, in the order given. These are the carriers somebody
       named — a shipment, a planned container, the caller — which resolution did not
       treat as settled but which are still the best evidence in the room.
    2. The carrier the ISO 6346 owner prefix suggests, exactly as a direct sweep uses it:
       a tie-breaker and never truth. A leased box travels under whoever booked it, so
       this moves a carrier up the list and can never make it the answer — only Traqo
       returning the box's events does that.
    3. :data:`DEFAULT_TRAQO_DISCOVERY_ORDER`, for a container nothing is known about.

    Dropped: duplicates (keeping the strongest signal), anything in
    ``exclude_carrier_codes``, and any carrier Traqo publishes no sealine for — asking
    about a carrier Traqo does not cover cannot answer and would still cost a request.

    The list is not truncated to :data:`MAX_TRAQO_PROBE_ATTEMPTS`. Building the full
    ordered list and capping the *calls* keeps the two decisions separate, and lets a
    caller see what would have been tried next.
    """
    excluded = frozenset(code for code in (resolve_carrier_code(value) for value in exclude_carrier_codes) if code)

    signals: list[tuple[str, str]] = []
    for value in preferred_carrier_codes:
        code = resolve_carrier_code(value)
        if code:
            signals.append((code, SOURCE_PREFERRED))

    if use_owner_prefix_hint and container_number:
        hint = suggest_carrier_for_owner_code(container_number[:4])
        if hint:
            signals.append((hint, SOURCE_OWNER_PREFIX))

    for value in discovery_order:
        code = resolve_carrier_code(value) or (value or "").strip().lower()
        if code:
            signals.append((code, SOURCE_DISCOVERY_ORDER))

    candidates: list[TraqoProbeCandidate] = []
    seen: set[str] = set()
    for code, source in signals:
        if code in seen or code in excluded:
            continue
        seen.add(code)
        sealine = sealine_for_carrier_code(code)
        if not sealine:
            logger.debug("Traqo probe: %s has no Traqo sealine; not a candidate.", code)
            continue
        candidates.append(TraqoProbeCandidate(carrier_code=code, sealine=sealine, source=source))
    return candidates


def probe_candidate_carriers(
    *,
    container_number: str,
    candidate_carrier_codes: Sequence[str] = (),
    candidates: Sequence[TraqoProbeCandidate] | None = None,
    max_attempts: int = MAX_TRAQO_PROBE_ATTEMPTS,
    client=None,
    sandbox: bool = False,
    fetch=None,
) -> TraqoCarrierProbeResult:
    """Ask Traqo about each candidate carrier until one returns this container's data.

    Never raises: every Traqo failure is classified and reported, because a probe that
    raised would abandon the container instead of letting resolution try the next step.

    ``candidates`` takes the ordered list :func:`build_traqo_probe_candidates` produced;
    ``candidate_carrier_codes`` is the plain-codes form, translated to sealines here and
    with unsupported carriers skipped. ``client`` and ``fetch`` inject the Traqo call for
    testing. ``max_attempts`` caps the *requests*, not the candidates: a carrier skipped
    for want of a sealine costs nothing and does not use one up.

    Stopping at the first hit is a decision about this probe, not about the container —
    the same rule a direct sweep follows.
    """
    number = (container_number or "").strip().upper()
    resolved = list(candidates) if candidates is not None else _candidates_from_codes(candidate_carrier_codes)

    if not number:
        return TraqoCarrierProbeResult(container_number=number, outcome=SKIPPED, error_kind="no_container_number")

    if fetch is None:
        client, failure = _prepared_client(client=client, sandbox=sandbox, container_number=number)
        if failure is not None:
            return failure

    # One attempt is recorded per candidate asked, so the record of what was spent and
    # the cap on spending it are the same number — there is no second counter to keep in
    # step with it. Candidates Traqo cannot be asked about never reach here: they are
    # dropped while the list is built, and so cost nothing.
    cap = max(max_attempts, 0)
    attempts: list[TraqoProbeAttempt] = []
    for candidate in resolved:
        if len(attempts) >= cap:
            logger.info(
                "Traqo candidate probe %s: stopping at %d attempt(s); %s not tried.",
                number,
                len(attempts),
                candidate.sealine,
            )
            break

        found, attempt = _probe_one(
            container_number=number,
            candidate=candidate,
            client=client,
            sandbox=sandbox,
            fetch=fetch,
        )
        attempts.append(attempt)

        if found is not None:
            result = TraqoCarrierProbeResult(
                container_number=number,
                outcome=FOUND,
                carrier_code=found.carrier_code,
                carrier_name=found.carrier_name or _registered_name(found.carrier_code),
                sealine=found.sealine,
                events=tuple(found.response.events),
                raw_payload=found.response.payload,
                attempts=tuple(attempts),
            )
            _log(result)
            return result

        if attempt.outcome == ERROR and _is_fatal(attempt.error_kind):
            # Nothing about this container: something about the account or the request
            # that every further Traqo call would hit in the same way. Spending four more
            # requests to be told the same thing four more times is the one failure mode
            # a cap cannot protect against.
            result = TraqoCarrierProbeResult(
                container_number=number,
                outcome=ERROR,
                attempts=tuple(attempts),
                error_kind=attempt.error_kind,
                error_message=attempt.error_message,
            )
            _log(result)
            return result

    result = TraqoCarrierProbeResult(
        container_number=number,
        outcome=_outcome_for(attempts),
        attempts=tuple(attempts),
    )
    _log(result)
    return result


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Verified:
    """A candidate that Traqo answered about with this container's own data."""

    carrier_code: str
    carrier_name: str
    sealine: str
    response: object


def _candidates_from_codes(codes: Sequence[str]) -> list[TraqoProbeCandidate]:
    """Turn plain carrier codes into candidates, dropping those Traqo cannot be asked about."""
    candidates: list[TraqoProbeCandidate] = []
    seen: set[str] = set()
    for value in codes:
        code = resolve_carrier_code(value) or (value or "").strip().lower()
        if not code or code in seen:
            continue
        seen.add(code)
        sealine = sealine_for_carrier_code(code)
        if not sealine:
            continue
        candidates.append(TraqoProbeCandidate(carrier_code=code, sealine=sealine))
    return candidates


def _prepared_client(*, client, sandbox: bool, container_number: str):
    """Return ``(client, None)``, or ``(None, NOT_CONFIGURED result)``.

    The credential is checked once, before any candidate: "Traqo is not set up here" is
    not a statement about the container, and discovering it five times over would report
    five failures where there is one configuration gap.
    """
    from .client import TraqoClient

    if client is not None:
        return client, None

    if not sandbox and not is_traqo_configured():
        logger.info("Traqo candidate probe for %s skipped — not configured.", container_number)
        return None, TraqoCarrierProbeResult(
            container_number=container_number,
            outcome=NOT_CONFIGURED,
            error_kind="not_configured",
            error_message="Traqo is not enabled for live calls.",
        )

    try:
        return TraqoClient.from_settings(sandbox=sandbox), None
    except CarrierConfigurationError as exc:  # pragma: no cover — is_traqo_configured covers it
        return None, TraqoCarrierProbeResult(
            container_number=container_number,
            outcome=NOT_CONFIGURED,
            error_kind="not_configured",
            error_message=str(exc),
        )


def _probe_one(*, container_number, candidate, client, sandbox, fetch):
    """Ask Traqo about one candidate. Returns ``(verified or None, attempt)``."""
    from .service import fetch_and_map_traqo_container

    call = fetch or fetch_and_map_traqo_container
    try:
        response = call(
            container_number=container_number,
            sealine=candidate.sealine,
            sandbox=sandbox,
            client=client,
        )
    except CarrierNoDataError:
        # A real answer: Traqo has no shipment for this container under this sealine.
        # The commonest outcome by design — four of five candidates are expected to
        # answer this way — and the reason the cap exists rather than a wider sweep.
        return None, _attempt(candidate, NOT_FOUND)
    except CarrierError as exc:
        return None, _attempt(candidate, ERROR, error_kind=type(exc).__name__, error_message=str(exc))
    except Exception as exc:  # noqa: BLE001 — a reader bug must not end the chain
        logger.exception("Unexpected error probing Traqo for %s as %s.", container_number, candidate.sealine)
        return None, _attempt(candidate, ERROR, error_kind="unexpected", error_message=f"{type(exc).__name__}: {exc}")

    verified, reason = _verify(response=response, candidate=candidate, container_number=container_number)
    if verified is not None:
        return verified, _attempt(candidate, FOUND)

    if reason == "reference_mismatch":
        # Traqo answered about a different container. Not "this carrier does not have the
        # box" — a payload that cannot be trusted to be about the right box at all, which
        # must never be mapped onto this one.
        return None, _attempt(
            candidate,
            ERROR,
            error_kind=reason,
            error_message=f"Traqo returned {response.reported_reference} for {container_number}.",
        )

    return None, _attempt(candidate, NOT_FOUND, error_kind=reason)


def _verify(*, response, candidate: TraqoProbeCandidate, container_number: str):
    """Decide whether a Traqo answer establishes the carrier. Returns ``(verified, reason)``.

    Three conditions, and the smallest set that makes a probe evidence rather than an echo
    of the question:

    *The shipment is this container's.* ``reference_number`` must match what was asked, or
    be absent — Traqo answering about the container in the URL without echoing it.

    *There is tracking data.* The mapper has to produce events. A shipment Traqo knows of
    but has no movements for proves nothing about who is carrying the box, and is exactly
    the shape a false positive would take; the same standard makes a direct carrier probe
    an answer (``CarrierDiscoveryOutcome.found``).

    *The carrier is nameable and routable.* The SCAC is taken from the *response* where
    Traqo states one, not from the request, because the request is the question. On every
    payload seen so far the two agree; if they ever disagree, Traqo is describing the
    shipment it actually has. A SCAC no registered carrier claims — Traqo covers OOLU,
    this system has no adapter for it — is honestly not a resolution: nothing could be
    routed to it, so it is reported as NOT_FOUND rather than rounded to a near carrier.
    """
    if not response.is_for_requested_container:
        return None, "reference_mismatch"

    if not response.has_events:
        return None, "no_events"

    sealine = response.reported_sealine or candidate.sealine
    carrier_code = carrier_code_for_sealine(sealine)
    if not carrier_code:
        logger.info(
            "Traqo probe %s: answered under sealine %s, which no registered carrier claims.",
            container_number,
            sealine,
        )
        return None, "unregistered_sealine"

    if carrier_code != candidate.carrier_code:
        # Worth a line of its own: we asked about one carrier and Traqo described another.
        logger.info(
            "Traqo probe %s: asked about %s (%s), answered as %s (%s) — the response wins.",
            container_number,
            candidate.carrier_code,
            candidate.sealine,
            carrier_code,
            sealine,
        )

    return (
        _Verified(
            carrier_code=carrier_code,
            carrier_name=_registered_name(carrier_code),
            sealine=sealine,
            response=response,
        ),
        "",
    )


def _attempt(candidate: TraqoProbeCandidate, outcome: str, **kwargs) -> TraqoProbeAttempt:
    return TraqoProbeAttempt(
        carrier_code=candidate.carrier_code,
        sealine=candidate.sealine,
        outcome=outcome,
        source=candidate.source,
        **kwargs,
    )


# Failures that say nothing about the candidate and everything about the account, the
# credential or the request itself. Classified by consequence, following
# :mod:`.errors`: another sealine cannot fix a rejected key (401), an account with
# developer access off or payment overdue (403/402), a shipment quota that is full
# (402, rate-limit family) or a container number Traqo will not accept at all.
_FATAL_ERROR_KINDS: frozenset[str] = frozenset(
    {
        CarrierAuthenticationError.__name__,
        CarrierConfigurationError.__name__,
        CarrierRateLimitError.__name__,
        CarrierUnsupportedReferenceError.__name__,
        "TraqoShipmentLimitReachedError",
        "TraqoPaymentOverdueError",
        "TraqoDeveloperModeDisabledError",
    }
)


def _is_fatal(error_kind: str) -> bool:
    """Whether this failure makes every remaining Traqo call pointless.

    A timeout, a 5xx or an unreadable body is one candidate's bad moment and the probe
    carries on to the next — the whole reason ERROR and NOT_FOUND are separate outcomes.
    """
    return error_kind in _FATAL_ERROR_KINDS


def _outcome_for(attempts: list[TraqoProbeAttempt]) -> str:
    """Classify a probe that found nothing.

    Mirrors how the direct sweep is classified in carrier resolution, and for the same
    reason: "Traqo has nothing under any of these carriers" and "we could not ask Traqo
    properly" lead to different things being said and done next.
    """
    answered = [attempt for attempt in attempts if attempt.answered]
    if not answered:
        return SKIPPED
    if all(attempt.outcome == ERROR for attempt in answered):
        return ERROR
    return NOT_FOUND


def _registered_name(carrier_code: str) -> str:
    if not carrier_code:
        return ""
    try:
        return get_carrier_definition(carrier_code).name
    except UnknownCarrierError:  # pragma: no cover — codes come from the registry
        return carrier_code


def _log(result: TraqoCarrierProbeResult) -> None:
    """Record every candidate and its answer in one line.

    One line per probe rather than one per candidate: the interesting fact is the
    sequence — which carriers were asked, in what order, and which one stopped it.
    """
    logger.info(
        "Traqo candidate probe %s: %s → %s",
        result.container_number,
        result.summary,
        (
            f"carrier={result.carrier_code} sealine={result.sealine} events={len(result.events)}"
            if result.found
            else result.outcome.upper()
        ),
    )


__all__ = [
    "DEFAULT_TRAQO_DISCOVERY_ORDER",
    "ERROR",
    "FOUND",
    "MAX_TRAQO_PROBE_ATTEMPTS",
    "NOT_CONFIGURED",
    "NOT_FOUND",
    "PROVIDER_CODE",
    "SKIPPED",
    "TraqoCarrierProbeResult",
    "TraqoProbeAttempt",
    "TraqoProbeCandidate",
    "build_traqo_probe_candidates",
    "probe_candidate_carriers",
]
