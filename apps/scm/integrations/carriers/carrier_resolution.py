"""Who is moving this container, and how do we know?

One question, one place it is answered. Until now the only way to learn a container's
carrier was to probe the direct carrier APIs — which works, and is not enough: ONE has
no working direct tracking path here, so a box moving with ONE was unresolvable however
many carriers were swept. Aggregators can answer it, and the moment they can, "who is
the carrier" stops being the same question as "who supplies the tracking data".

This module answers only the first. :mod:`apps.scm.tracking.provider_routing` answers
the second, separately and afterwards, and this one holds no opinion about it: resolving
a carrier through Vizion and then tracking it through Traqo is a normal outcome.

The order, cheapest and strongest first::

    trusted carrier already known?          free, and better evidence than any provider
        → Traqo free carrier lookup         free
        → direct carrier API discovery      the team's own rate limits
        → Vizion ACI                        a paid reference, every time

Each step short-circuits the ones below it. That ordering *is* the cost policy: Vizion
creates a billable reference on every call, so it must never run for a container an
earlier step has already explained, and a container whose shipment names its carrier
must reach no provider at all.

Three properties this module is careful about.

**Nothing is written.** The result describes what was asked and what came back, exactly
as :mod:`.carrier_discovery` does. Creating a subscription, storing events and recording
provenance are the caller's job, through the existing tracking write path. In particular
``Shipment.carrier`` is never touched: the carrier a booking was made with and the
carrier a provider says is moving the box are separate facts, and reconciling them is
not resolution's decision to make.

**A failure is not an answer.** A Traqo timeout does not mean the container has no
carrier, and a Vizion 401 does not mean Vizion has never heard of it. Every step reports
FOUND / NOT_FOUND / NOT_CONFIGURED / ERROR separately, the chain continues past a
technical failure exactly as it continues past a NOT_FOUND, and a step that failed
technically is visible in :attr:`CarrierResolution.steps` so a caller can tell "nobody
has this box" from "we could not ask properly".

**A direct probe's payload is not thrown away.** When direct discovery is the step that
answers, its events and raw payload travel out on the result, so the caller stores what
was already fetched instead of asking the same carrier the same question twice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from apps.scm.tracking.models import CarrierSource

from .carrier_discovery import discover_carrier_for_container
from .registry import UnknownCarrierError, get_carrier_definition, resolve_carrier_code

if TYPE_CHECKING:
    from apps.scm.containers.models import Container
    from apps.teams.models import Team

    from .base import BaseCarrierClient
    from .carrier_discovery import CarrierDiscoveryOutcome
    from .dcsa.schemas import NormalisedTrackingEvent

logger = logging.getLogger(__name__)

# The steps of the chain, as values a caller and a log line can both name.
STEP_TRUSTED = "trusted_knowledge"
STEP_TRAQO_LOOKUP = "traqo_lookup"
STEP_DIRECT_API = "direct_api"
STEP_VIZION_ACI = "vizion_aci"

# What one step concluded. Shared vocabulary with the probe and with both aggregator
# discovery wrappers, so a chain of three unlike providers reads as one chain.
FOUND = "found"
NOT_FOUND = "not_found"
NOT_CONFIGURED = "not_configured"
ERROR = "error"
SKIPPED = "skipped"


@dataclass(frozen=True)
class ResolutionStep:
    """One step of the chain: what was asked, and what came back."""

    step: str
    outcome: str
    carrier_code: str = ""
    # Human-readable, for logs. May echo a provider's own words, so it is never
    # rendered to a user.
    detail: str = ""

    @property
    def found(self) -> bool:
        return self.outcome == FOUND

    def __str__(self) -> str:
        parts = [f"{self.step} → {self.outcome.upper()}"]
        if self.carrier_code:
            parts.append(self.carrier_code)
        if self.detail:
            parts.append(f"({self.detail})")
        return " ".join(parts)


@dataclass(frozen=True)
class CarrierResolution:
    """Who is moving a container, how we know, and what the chain had to do to find out.

    ``resolved`` is the only thing that authorises acting on the carrier. Everything else
    exists so a caller can tell apart the three ways resolution can come back
    empty — nobody has this box, nobody could be asked, and nobody answered — which need
    different things said to a user.
    """

    container_number: str
    carrier_code: str = ""
    carrier_name: str = ""
    source: str = ""
    # False for a carrier that is believed rather than proved. A shipment's carrier field
    # and an aggregator's lookup are both hints; a carrier that returned tracking events
    # for this very container is verified. The distinction survives into the
    # subscription, so the UI can say "identified via" rather than implying certainty.
    verified: bool = False
    steps: tuple[ResolutionStep, ...] = field(default_factory=tuple)

    # Direct discovery's payload, when direct discovery is what answered. Present so the
    # caller can store what has already been fetched rather than re-fetching it.
    events: tuple[NormalisedTrackingEvent, ...] = field(default_factory=tuple)
    raw_payload: dict = field(default_factory=dict)
    # The sweep itself, when one ran. Carries the per-carrier attempts the manual refresh
    # reports as "we checked these".
    discovery: CarrierDiscoveryOutcome | None = None
    # A Vizion reference that now exists, and was paid for, whether or not ACI answered.
    vizion_reference_id: str = ""

    @property
    def resolved(self) -> bool:
        return bool(self.carrier_code)

    @property
    def has_direct_payload(self) -> bool:
        """True when the caller can store events without another carrier call."""
        return bool(self.events)

    def step_for(self, step: str) -> ResolutionStep | None:
        for entry in self.steps:
            if entry.step == step:
                return entry
        return None

    @property
    def attempted_steps(self) -> tuple[str, ...]:
        """The steps that actually reached a provider, in order."""
        return tuple(entry.step for entry in self.steps if entry.outcome in (FOUND, NOT_FOUND, ERROR))

    @property
    def summary(self) -> str:
        """The whole chain on one line, for the log."""
        return "; ".join(str(entry) for entry in self.steps) or "no steps"


def get_trusted_carrier_for_container(team: Team, container) -> tuple[str, str, str]:
    """Return ``(carrier_code, carrier_name, source)`` already known for this container.

    "Trusted" means somebody chose it or a carrier proved it — not that the system
    inferred it. The order below is the order the evidence is worth:

    1. A carrier already verified for this container. A provider returned real tracking
       events against this very number, which is the strongest evidence there is.
    2. A carrier recorded for the container when it was planned. A person chose it.
    3. The carrier on the shipment the container is travelling on. A booking fact, and
       the weakest of the three — the field can be stale or name a forwarder rather than
       the operator — but still a deliberate human statement.

    The ISO 6346 owner prefix is deliberately absent. It names who *owns* the box, and a
    leased container travels under whoever booked it; direct discovery may use it to
    order a sweep, but it may never stand in for knowing.

    Returns ``("", "", "")`` when nothing is known, which is the normal state for a
    container nobody has looked at yet.
    """
    from apps.scm.tracking.selectors import get_verified_container_subscriptions

    # 1. Already verified. Newest first: a box that changed hands is now with whoever
    #    took it over, and the older source's leg is over.
    for subscription in reversed(get_verified_container_subscriptions(team, container)):
        if subscription.carrier_code:
            return (
                subscription.carrier_code,
                subscription.carrier_name or _registered_name(subscription.carrier_code),
                CarrierSource.EXISTING_VERIFIED_SOURCE,
            )

    # 2. Chosen when the container was planned.
    from apps.scm.containers.models import PlannedContainer

    planned = (
        PlannedContainer.objects.filter(team=team, container_number=container.container_id)
        .exclude(carrier="")
        .order_by("-created_at")
        .first()
    )
    if planned is not None:
        code = resolve_carrier_code(planned.carrier)
        if code:
            return code, _registered_name(code), CarrierSource.PLANNED_CONTAINER

    # 3. Named on the shipment.
    from apps.scm.shipments.models import ShipmentContainer

    link = (
        ShipmentContainer.objects.filter(container=container, shipment__team=team)
        .select_related("shipment")
        .order_by("-created_at")
        .first()
    )
    if link is not None:
        code = resolve_carrier_code(link.shipment.carrier)
        if code:
            return code, _registered_name(code), CarrierSource.SHIPMENT

    return "", "", ""


def resolve_carrier_for_container(
    *,
    team: Team,
    container: Container,
    preferred_carrier_codes: list[str] | tuple[str, ...] = (),
    clients: dict[str, BaseCarrierClient] | None = None,
    exclude_carrier_codes: frozenset[str] = frozenset(),
    use_trusted_knowledge: bool = True,
    use_traqo_lookup: bool = True,
    use_direct_discovery: bool = True,
    use_vizion_aci: bool = True,
    traqo_lookup=None,
    vizion_identify=None,
) -> CarrierResolution:
    """Establish which carrier is moving ``container``, cheapest evidence first.

    Never raises: every step classifies its own failures, and the result always describes
    what happened. Writes nothing.

    The four ``use_*`` flags turn steps off. They exist for callers with a different cost
    budget — a background sweep over thousands of containers has no business creating a
    Vizion reference for each — not as a way to reorder the chain, which is fixed.

    ``traqo_lookup`` and ``vizion_identify`` inject the two aggregator calls for testing;
    ``clients`` injects direct carrier adapters, exactly as
    :func:`.carrier_discovery.discover_carrier_for_container` takes them.

    ``preferred_carrier_codes`` and ``exclude_carrier_codes`` are passed through to the
    direct sweep unchanged. Both are *carrier* codes; a caller holding provider codes must
    translate first — see :func:`apps.scm.tracking.continuation.get_recently_checked_carrier_codes`.
    """
    reference = container.container_id
    steps: list[ResolutionStep] = []

    # --- 1. What we already know -------------------------------------------------
    if use_trusted_knowledge:
        code, name, source = get_trusted_carrier_for_container(team, container)
        if code:
            steps.append(ResolutionStep(step=STEP_TRUSTED, outcome=FOUND, carrier_code=code, detail=source))
            resolution = CarrierResolution(
                container_number=reference,
                carrier_code=code,
                carrier_name=name,
                source=source,
                # Only an existing verified source is proof. The other two are human
                # statements, which order a sweep but do not settle anything.
                verified=source == CarrierSource.EXISTING_VERIFIED_SOURCE,
                steps=tuple(steps),
            )
            _log(resolution)
            return resolution
        steps.append(ResolutionStep(step=STEP_TRUSTED, outcome=NOT_FOUND, detail="nothing recorded"))
    else:
        steps.append(ResolutionStep(step=STEP_TRUSTED, outcome=SKIPPED))

    # --- 2. Traqo's free lookup --------------------------------------------------
    if use_traqo_lookup:
        lookup = (traqo_lookup or _default_traqo_lookup)(reference)
        steps.append(
            ResolutionStep(
                step=STEP_TRAQO_LOOKUP,
                outcome=_traqo_outcome(lookup),
                carrier_code=lookup.carrier_code,
                detail=lookup.error_message or lookup.reason or lookup.scac,
            )
        )
        if lookup.found:
            resolution = CarrierResolution(
                container_number=reference,
                carrier_code=lookup.carrier_code,
                carrier_name=lookup.carrier_name or _registered_name(lookup.carrier_code),
                source=CarrierSource.TRAQO_LOOKUP,
                # A lookup is a guess with a confidence, however high. It has not seen
                # this box's events, so it names a carrier without proving one.
                verified=False,
                steps=tuple(steps),
            )
            _log(resolution)
            return resolution
    else:
        steps.append(ResolutionStep(step=STEP_TRAQO_LOOKUP, outcome=SKIPPED))

    # --- 3. The direct carrier APIs ---------------------------------------------
    if use_direct_discovery:
        outcome = discover_carrier_for_container(
            team=team,
            container_number=reference,
            preferred_carrier_codes=preferred_carrier_codes,
            clients=clients,
            exclude_carrier_codes=exclude_carrier_codes,
        )
        steps.append(
            ResolutionStep(
                step=STEP_DIRECT_API,
                outcome=_direct_outcome(outcome),
                carrier_code=outcome.carrier_code,
                detail=f"{len(outcome.answered)} asked, {len(outcome.skipped)} skipped",
            )
        )
        if outcome.found:
            resolution = CarrierResolution(
                container_number=reference,
                carrier_code=outcome.carrier_code,
                carrier_name=outcome.carrier_name,
                source=CarrierSource.DIRECT_API,
                # The carrier answered with this container's own events. Nothing is
                # stronger, and it is why the payload rides along: it is already proof.
                verified=True,
                steps=tuple(steps),
                events=tuple(outcome.events),
                raw_payload=outcome.raw_payload,
                discovery=outcome,
            )
            _log(resolution)
            return resolution
        # Kept even without a hit: the manual refresh reports which carriers answered.
        direct_outcome = outcome
    else:
        steps.append(ResolutionStep(step=STEP_DIRECT_API, outcome=SKIPPED))
        direct_outcome = None

    # --- 4. Vizion ACI, which costs a reference ---------------------------------
    if use_vizion_aci:
        identification = (vizion_identify or _default_vizion_identify)(reference)
        steps.append(
            ResolutionStep(
                step=STEP_VIZION_ACI,
                outcome=_vizion_outcome(identification),
                carrier_code=identification.carrier_code,
                detail=identification.error_message or identification.aci_state,
            )
        )
        if identification.found:
            resolution = CarrierResolution(
                container_number=reference,
                carrier_code=identification.carrier_code,
                carrier_name=identification.carrier_name or _registered_name(identification.carrier_code),
                source=CarrierSource.VIZION_ACI,
                # A carrier system returned recent shipment data for this box to Vizion.
                # That is an answer rather than a shape match — but the events are
                # Vizion's, not ours, so this is not the same proof a direct probe gives.
                verified=False,
                steps=tuple(steps),
                discovery=direct_outcome,
                vizion_reference_id=identification.reference_id,
            )
            _log(resolution)
            return resolution
        unresolved_reference = identification.reference_id
    else:
        steps.append(ResolutionStep(step=STEP_VIZION_ACI, outcome=SKIPPED))
        unresolved_reference = ""

    resolution = CarrierResolution(
        container_number=reference,
        steps=tuple(steps),
        discovery=direct_outcome,
        vizion_reference_id=unresolved_reference,
    )
    _log(resolution)
    return resolution


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _default_traqo_lookup(container_number: str):
    from apps.scm.integrations.traqo.discovery import lookup_carrier_for_container

    return lookup_carrier_for_container(container_number)


def _default_vizion_identify(container_number: str):
    from apps.scm.integrations.vizion.discovery import identify_carrier

    return identify_carrier(container_number)


def _traqo_outcome(lookup) -> str:
    from apps.scm.integrations.traqo import discovery as traqo_discovery

    if lookup.found:
        return FOUND
    if lookup.status == traqo_discovery.NOT_CONFIGURED:
        return NOT_CONFIGURED
    if lookup.status == traqo_discovery.ERROR:
        return ERROR
    # Includes a SCAC no registered carrier claims: Traqo answered, and the answer is
    # not one this system can act on.
    return NOT_FOUND


def _vizion_outcome(identification) -> str:
    from apps.scm.integrations.vizion import discovery as vizion_discovery

    if identification.found:
        return FOUND
    if identification.status == vizion_discovery.NOT_CONFIGURED:
        return NOT_CONFIGURED
    if identification.status == vizion_discovery.ERROR:
        return ERROR
    # PENDING included: Vizion is still looking, which is not "no carrier". The caller
    # sees it in ``steps`` and the reference stays alive for Vizion's own retries.
    return NOT_FOUND


def _direct_outcome(outcome: CarrierDiscoveryOutcome) -> str:
    if outcome.found:
        return FOUND
    if not outcome.attempts:
        return NOT_CONFIGURED
    if not outcome.answered:
        return NOT_CONFIGURED
    if not outcome.not_found and outcome.errored:
        return ERROR
    return NOT_FOUND


def _registered_name(carrier_code: str) -> str:
    if not carrier_code:
        return ""
    try:
        return get_carrier_definition(carrier_code).name
    except UnknownCarrierError:
        return carrier_code


def _log(resolution: CarrierResolution) -> None:
    """Record the whole chain in one line, so a decision can be reconstructed.

    Deliberately one line per resolution rather than one per step: the interesting fact
    is the *sequence* — which steps ran, which were short-circuited, and what stopped it.
    """
    logger.info(
        "Carrier resolution %s: %s → %s",
        resolution.container_number,
        resolution.summary,
        (
            f"carrier={resolution.carrier_code} source={resolution.source} verified={resolution.verified}"
            if resolution.resolved
            else "unresolved"
        ),
    )
