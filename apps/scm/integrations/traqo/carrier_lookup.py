"""Reading Traqo's carrier lookup as one piece of discovery *evidence*.

Container SCM already has a way to find out which carrier knows a container:
``integrations/carriers/carrier_discovery.py`` builds candidates from what somebody
chose, from the ISO 6346 owner prefix, and from the team's connected carriers, then
probes them and lets the one that answers with data win. Traqo's
``/carriers/lookup`` is a *fourth signal into that model*, not a replacement for it.
Nothing here imports or modifies that module.

Which is deliberate. If Traqo became the owner of carrier identity, then Container
SCM's answer to "who is moving this box" would be an opinion held by a third party
that has never seen the booking — and there would be no way to notice when it was
wrong, because nothing would disagree with it. So the reading below produces a
candidate, a confidence and a reason, and stops. The decision stays with the caller.

Three properties of the endpoint shape this module.

*It spends no shipment slot.* ``slot_consumed`` is read from the response and
reported, never assumed from documentation: a benchmark that trusted the docs would
be the last thing to notice if the behaviour changed, and the account has ten slots.

*It is a guess, and says so.* The response carries its own ``confidence``, ``source``,
``reason`` and rival ``candidates``. All of it is kept. A lookup that named one carrier
while listing two others at similar confidence is a materially weaker claim than one
that named a single candidate, and flattening that to "carrier = X" would destroy the
distinction the field exists to draw.

*It may be cached.* A repeated lookup can be served from Traqo's cache, which means
the answer may predate a change. Reported, not corrected.

The ISO 6346 prefix and the lookup are compared but never reconciled: a prefix names
the box's *owner*, the lookup guesses its *operator*, and a leased box travels under
whoever booked it. Where they disagree the disagreement is the finding.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .sealines import CARRIER_CODE_TO_SEALINE, UNREGISTERED_SEALINES

logger = logging.getLogger(__name__)

# How much weight the lookup deserves. Traqo states its own confidence; these are
# Container SCM's reading of what may be *done* with it, and they are benchmark
# vocabulary rather than routing policy — nothing consumes them automatically.
CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_LOW = "LOW"
CONFIDENCE_UNKNOWN = "UNKNOWN"

# What Container SCM would be willing to do on this evidence alone. Reported by the
# benchmark; deliberately not wired into anything that registers a subscription.
ACTION_AUTO_ACCEPT = "AUTO-ACCEPT"
ACTION_ACCEPT_WITH_CORROBORATION = "ACCEPT WITH CORROBORATION"
ACTION_MANUAL_VERIFICATION = "MANUAL/SECONDARY VERIFICATION REQUIRED"
ACTION_REJECT = "REJECT"

# Traqo's own confidence wording, mapped onto the three bands above. Read as exact
# tokens rather than substrings; anything unrecognised stays UNKNOWN instead of being
# rounded up to the nearest familiar word.
_CONFIDENCE_WORDS = {
    "high": CONFIDENCE_HIGH,
    "very_high": CONFIDENCE_HIGH,
    "certain": CONFIDENCE_HIGH,
    "exact": CONFIDENCE_HIGH,
    "medium": CONFIDENCE_MEDIUM,
    "moderate": CONFIDENCE_MEDIUM,
    "probable": CONFIDENCE_MEDIUM,
    "likely": CONFIDENCE_MEDIUM,
    "low": CONFIDENCE_LOW,
    "weak": CONFIDENCE_LOW,
    "guess": CONFIDENCE_LOW,
    "unlikely": CONFIDENCE_LOW,
}

# Numeric confidence thresholds, for a response that scores rather than labels.
_HIGH_SCORE = 0.85
_MEDIUM_SCORE = 0.6

# Keys the response may carry for each fact. Several are tried because the endpoint is
# new to this integration and its exact field names are not yet pinned by evidence; an
# unrecognised shape yields an empty value and keeps the raw response, rather than
# raising and losing the observation.
_SCAC_KEYS = ("scac", "carrier_scac", "sealine", "sealine_code", "carrier_code", "code")
_NAME_KEYS = ("carrier_name", "carrier", "sealine_name", "name")
_CONFIDENCE_KEYS = ("confidence", "confidence_level", "score", "confidence_score")
_SOURCE_KEYS = ("source", "detected_by", "matched_by", "method")
_REASON_KEYS = ("reason", "explanation", "detail", "message", "note")
_CANDIDATE_KEYS = ("candidates", "alternatives", "other_candidates", "possible_carriers")
_UNAVAILABLE_KEYS = ("unavailable_sources", "sources_unavailable", "unavailable", "failed_sources")
_CACHED_KEYS = ("cached", "from_cache", "is_cached", "cache_hit")
_SLOT_KEYS = ("slot_consumed", "slots_consumed", "shipment_created", "consumed_slot")


def _text(source: dict, keys: tuple[str, ...]) -> str:
    """Return the first non-empty value among ``keys``, stripped."""
    for key in keys:
        value = source.get(key)
        if isinstance(value, dict):
            # A nested carrier object, e.g. {"carrier": {"scac": "MAEU"}}.
            continue
        if value not in (None, "", [], {}):
            return str(value).strip()
    return ""


def _first_dict(source: dict, keys: tuple[str, ...]) -> dict:
    for key in keys:
        value = source.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _list(source: dict, keys: tuple[str, ...]) -> list:
    for key in keys:
        value = source.get(key)
        if isinstance(value, list):
            return value
    return []


def _flag(source: dict, keys: tuple[str, ...]) -> bool | None:
    """Return a tri-state boolean: True, False, or None when the field is absent.

    None is distinct from False on purpose. "Traqo said this consumed no slot" and
    "Traqo did not say" are different claims, and the second must not be reported as
    the first.
    """
    for key in keys:
        if key not in source:
            continue
        value = source[key]
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return bool(value)
        text = str(value).strip().lower()
        if text in ("true", "yes", "1"):
            return True
        if text in ("false", "no", "0"):
            return False
    return None


def read_confidence(value: str) -> str:
    """Classify Traqo's stated confidence into Container SCM's three bands.

    A word it does not recognise, or no statement at all, stays UNKNOWN. Rounding an
    unfamiliar label to the nearest familiar one would invent certainty.
    """
    text = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not text:
        return CONFIDENCE_UNKNOWN
    if text in _CONFIDENCE_WORDS:
        return _CONFIDENCE_WORDS[text]
    try:
        score = float(text)
    except ValueError:
        logger.debug("Traqo carrier lookup: unrecognised confidence %r", value)
        return CONFIDENCE_UNKNOWN
    # Some APIs score 0–100 rather than 0–1.
    score = score / 100.0 if score > 1.0 else score
    if score >= _HIGH_SCORE:
        return CONFIDENCE_HIGH
    if score >= _MEDIUM_SCORE:
        return CONFIDENCE_MEDIUM
    return CONFIDENCE_LOW


@dataclass(frozen=True)
class CarrierCandidateEvidence:
    """One carrier the lookup put forward, with its own confidence."""

    scac: str
    name: str = ""
    confidence: str = CONFIDENCE_UNKNOWN
    stated_confidence: str = ""

    def as_dict(self) -> dict:
        return {
            "scac": self.scac,
            "name": self.name,
            "confidence": self.confidence,
            "stated_confidence": self.stated_confidence,
        }


@dataclass(frozen=True)
class TraqoCarrierLookup:
    """What Traqo's lookup said about one reference. Evidence, not a decision."""

    reference: str
    scac: str = ""
    carrier_name: str = ""
    confidence: str = CONFIDENCE_UNKNOWN
    stated_confidence: str = ""
    source: str = ""
    reason: str = ""
    candidates: tuple[CarrierCandidateEvidence, ...] = field(default_factory=tuple)
    unavailable_sources: tuple[str, ...] = field(default_factory=tuple)
    cached: bool | None = None
    slot_consumed: bool | None = None
    # The response verbatim, so a field this reader did not recognise is not lost and
    # the parse can be revisited against real evidence.
    raw: dict = field(default_factory=dict)

    @property
    def identified(self) -> bool:
        return bool(self.scac)

    @property
    def rival_candidates(self) -> tuple[CarrierCandidateEvidence, ...]:
        """Candidates other than the one Traqo named.

        A lookup that named one carrier while listing rivals is a weaker claim than one
        that named a single candidate, so the rivals are counted rather than displayed
        as a flat list next to the winner.
        """
        return tuple(candidate for candidate in self.candidates if candidate.scac != self.scac)

    @property
    def carrier_supported_by_traqo(self) -> bool | None:
        """Whether Container SCM knows this SCAC as one Traqo publishes a sealine for.

        None when no SCAC was returned. This is a Container SCM fact — the sealine
        table, itself read from Traqo's own carrier list — not a claim Traqo made in
        this response. ``UNREGISTERED_SEALINES`` counts: Traqo can track those even
        though Container SCM has no direct adapter for them, which is precisely the
        breadth case the POC is investigating.
        """
        if not self.scac:
            return None
        return self.scac.upper() in set(CARRIER_CODE_TO_SEALINE.values()) | set(UNREGISTERED_SEALINES)

    @property
    def carrier_code(self) -> str:
        """The Container SCM carrier code for this SCAC, or "" if it has no adapter."""
        target = self.scac.upper()
        for code, sealine in CARRIER_CODE_TO_SEALINE.items():
            if sealine == target:
                return code
        return ""

    def as_dict(self) -> dict:
        return {
            "reference": self.reference,
            "scac": self.scac,
            "carrier_name": self.carrier_name,
            "confidence": self.confidence,
            "stated_confidence": self.stated_confidence,
            "source": self.source,
            "reason": self.reason,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "rival_candidate_count": len(self.rival_candidates),
            "unavailable_sources": list(self.unavailable_sources),
            "cached": self.cached,
            "slot_consumed": self.slot_consumed,
            "identified": self.identified,
            "carrier_supported_by_traqo": self.carrier_supported_by_traqo,
            "container_scm_carrier_code": self.carrier_code,
        }


def _read_candidate(entry) -> CarrierCandidateEvidence | None:
    """Read one entry of the candidates list, whatever shape it takes."""
    if isinstance(entry, str):
        scac = entry.strip().upper()
        return CarrierCandidateEvidence(scac=scac) if scac else None
    if not isinstance(entry, dict):
        return None

    scac = (_text(entry, _SCAC_KEYS) or _text(_first_dict(entry, _NAME_KEYS), _SCAC_KEYS)).upper()
    stated = _text(entry, _CONFIDENCE_KEYS)
    if not scac:
        return None
    return CarrierCandidateEvidence(
        scac=scac,
        name=_text(entry, _NAME_KEYS),
        confidence=read_confidence(stated),
        stated_confidence=stated,
    )


def read_carrier_lookup(payload: dict, *, reference: str) -> TraqoCarrierLookup:
    """Read one lookup response into evidence, losing nothing.

    Tolerant by design: the endpoint is new to this integration, so a field name this
    reader does not recognise leaves the corresponding value empty and the whole
    response is kept in ``raw``. An unreadable response yields a lookup that reports
    nothing identified — never an exception, because the observation itself is the
    thing being collected.
    """
    if not isinstance(payload, dict):
        return TraqoCarrierLookup(reference=reference)

    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        data = {}

    # The named carrier may sit inline or in a nested object.
    nested = _first_dict(data, ("carrier", "sealine", "detected_carrier", "best_match", "result"))
    inline_scac = _text(data, _SCAC_KEYS)
    scac = (inline_scac or _text(nested, _SCAC_KEYS)).upper()
    name = _text(nested, _NAME_KEYS) or _text(data, _NAME_KEYS)
    stated = _text(nested, _CONFIDENCE_KEYS) or _text(data, _CONFIDENCE_KEYS)

    candidates = tuple(
        candidate for candidate in (_read_candidate(entry) for entry in _list(data, _CANDIDATE_KEYS)) if candidate
    )

    unavailable = tuple(
        str(entry.get("source") or entry.get("name") or "").strip() if isinstance(entry, dict) else str(entry).strip()
        for entry in _list(data, _UNAVAILABLE_KEYS)
    )

    return TraqoCarrierLookup(
        reference=reference,
        scac=scac,
        carrier_name=name,
        confidence=read_confidence(stated),
        stated_confidence=stated,
        source=_text(nested, _SOURCE_KEYS) or _text(data, _SOURCE_KEYS),
        # Nested first: production puts source, confidence and reason on the carrier
        # object, and the envelope's `message` would otherwise shadow the real reason.
        reason=_text(nested, _REASON_KEYS) or _text(data, _REASON_KEYS) or _text(payload, _REASON_KEYS),
        candidates=candidates,
        unavailable_sources=tuple(entry for entry in unavailable if entry),
        # Read from wherever Traqo states them: the envelope carries account-level
        # facts, the data object carries per-answer ones.
        cached=_flag(data, _CACHED_KEYS) if _flag(data, _CACHED_KEYS) is not None else _flag(payload, _CACHED_KEYS),
        slot_consumed=(_flag(data, _SLOT_KEYS) if _flag(data, _SLOT_KEYS) is not None else _flag(payload, _SLOT_KEYS)),
        raw=payload,
    )


@dataclass(frozen=True)
class LookupAssessment:
    """What Container SCM would do on this lookup alone, and why.

    Benchmark output. It exists so the question "would we have registered this
    automatically" gets a recorded answer per container instead of an impression, and
    it is deliberately not consulted by anything that creates a subscription.
    """

    action: str
    rationale: str
    corroborated_by: tuple[str, ...] = field(default_factory=tuple)
    contradicted_by: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "rationale": self.rationale,
            "corroborated_by": list(self.corroborated_by),
            "contradicted_by": list(self.contradicted_by),
        }


def _names_carrier(lookup: TraqoCarrierLookup, carrier_code: str) -> bool:
    """Whether the lookup named this Container SCM carrier, as winner or candidate.

    Compared in *carrier-code* space, not SCAC space. Container SCM says ``maersk``
    where Traqo says ``MAEU``, and comparing the two strings directly would report
    every correct lookup as a contradiction — which is exactly what it did before this
    was fixed.
    """
    code = (carrier_code or "").strip().lower()
    if not code:
        return False
    sealine = CARRIER_CODE_TO_SEALINE.get(code, "")
    named = {lookup.scac.upper(), *(candidate.scac.upper() for candidate in lookup.candidates)}
    # The SCAC where the carrier has one; otherwise fall back to comparing the code
    # itself, so a carrier with no Traqo sealine is still checked rather than skipped.
    return (sealine.upper() in named) if sealine else (code.upper() in named)


def assess_lookup(
    lookup: TraqoCarrierLookup,
    *,
    prefix_suggestion: str = "",
    known_carrier_codes: tuple[str, ...] = (),
) -> LookupAssessment:
    """Say what this lookup would justify, given what Container SCM already knows.

    ``prefix_suggestion`` is the ISO 6346 owner-prefix hint and ``known_carrier_codes``
    the carriers already evidenced for this container by a shipment or a verified
    subscription. Both are Container SCM *carrier codes*, not SCACs, and are translated
    before comparison. Both are corroboration only. An absent prefix hint is not a
    contradiction — most prefixes are simply not in the registry — and that distinction
    is the reason the two lists are kept apart rather than summed into a score.
    """
    if not lookup.identified:
        return LookupAssessment(
            action=ACTION_REJECT,
            rationale="Traqo named no carrier, so there is nothing to act on.",
        )

    corroborated: list[str] = []
    contradicted: list[str] = []

    if prefix_suggestion:
        if _names_carrier(lookup, prefix_suggestion):
            corroborated.append(f"ISO 6346 owner prefix suggests {prefix_suggestion}")
        else:
            contradicted.append(f"ISO 6346 owner prefix suggests {prefix_suggestion}, which the lookup did not name")

    for code in known_carrier_codes:
        if _names_carrier(lookup, code):
            corroborated.append(f"Container SCM already evidences {code} for this container")
        else:
            contradicted.append(f"Container SCM already evidences {code}, which the lookup did not name")

    if lookup.carrier_supported_by_traqo is False:
        return LookupAssessment(
            action=ACTION_REJECT,
            rationale=(
                f"{lookup.scac} is not a sealine Container SCM knows Traqo can track, so a tracking "
                "call on it would spend a slot on a carrier that cannot answer."
            ),
            corroborated_by=tuple(corroborated),
            contradicted_by=tuple(contradicted),
        )

    if contradicted:
        return LookupAssessment(
            action=ACTION_MANUAL_VERIFICATION,
            rationale=(
                "The lookup disagrees with evidence Container SCM already holds. A disagreement about "
                "who is moving a box is exactly the case a human should settle."
            ),
            corroborated_by=tuple(corroborated),
            contradicted_by=tuple(contradicted),
        )

    if lookup.confidence == CONFIDENCE_HIGH and not lookup.rival_candidates:
        action = ACTION_AUTO_ACCEPT if corroborated else ACTION_ACCEPT_WITH_CORROBORATION
        return LookupAssessment(
            action=action,
            rationale=(
                "Traqo states high confidence and names no rival candidate"
                + (
                    "; independent Container SCM evidence agrees."
                    if corroborated
                    else ", but nothing independent agrees with it yet — one source at high confidence is "
                    "still one source."
                )
            ),
            corroborated_by=tuple(corroborated),
            contradicted_by=(),
        )

    if lookup.confidence in (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM):
        return LookupAssessment(
            action=ACTION_ACCEPT_WITH_CORROBORATION,
            rationale=(
                f"Traqo states {lookup.confidence.lower()} confidence with "
                f"{len(lookup.rival_candidates)} rival candidate(s). Good enough to try, not good enough "
                "to register unattended."
            ),
            corroborated_by=tuple(corroborated),
            contradicted_by=(),
        )

    return LookupAssessment(
        action=ACTION_MANUAL_VERIFICATION,
        rationale=(
            f"Traqo states {lookup.confidence.lower()} confidence"
            + (f" ({lookup.stated_confidence})" if lookup.stated_confidence else "")
            + ". A hint is worth recording and not worth spending a shipment slot on unattended."
        ),
        corroborated_by=tuple(corroborated),
        contradicted_by=(),
    )
