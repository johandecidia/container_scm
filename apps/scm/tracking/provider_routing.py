"""Given a known carrier, which provider do we actually call for its tracking?

The second half of a decision :mod:`apps.scm.integrations.carriers.carrier_resolution`
makes the first half of. Resolution says *ONE is moving this box*; routing says *and
Traqo is who we ask about it*, because ONE has no working direct tracking path here.
Those are different questions with different answers, and this module exists so the
second one is answered in exactly one place.

The order::

    0. an explicit choice         when an administrator set one for this container,
                                  it is the whole decision — see below
    1. the carrier's own API      when registered, able to answer by container
                                  number, and connected for this team
    2. the team default           Traqo, when enabled and publishing a sealine for
                                  this carrier
    3. nothing                    a clean NOT_CONFIGURED, not a guess

**An explicit choice does not fall back.** Steps 1–3 are a search for somebody who
can answer; a container whose tracking source an administrator has *chosen* is not
a search. If the chosen provider cannot be asked — deactivated integration, Traqo
with no sealine for the carrier, a direct provider that is not the carrier moving
the box — routing returns an unavailable route saying so, rather than quietly
substituting the one it would have picked anyway. Silently tracking a container
through a provider somebody deselected is worse than not tracking it, because
nothing on the page would say it had happened. The preference itself lives in
:mod:`apps.scm.tracking.preferences`.

Vizion is absent on purpose. It can track, and its ACI already creates the reference
that would make tracking nearly free — but a reference is Vizion's billable unit and
routing to it would turn every unresolvable container into a purchase. It is a
*discovery* provider here, and the day that changes is the day a fourth branch is added
below rather than a Vizion call appearing somewhere else.

Direct comes first even where an aggregator would also work, for reasons that outlast
any one carrier: the carrier's own API is the primary record rather than a copy of it,
it carries detail aggregators normalise away, and it spends no third-party quota.

**Centralised on purpose.** The whole point is that ``if carrier == "one"`` appears
nowhere else in the codebase. A caller asks :func:`resolve_tracking_route` and acts on
what it gets; when ONE's direct adapter starts working, this module's answer changes and
nothing else has to.

Nothing here is written, called or fetched. Routing is a pure decision over the registry,
the team's integrations and installation settings — :mod:`apps.scm.tracking.activation`
is what makes a route real.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.scm.integrations.carriers.registry import (
    UnknownCarrierError,
    get_carrier_definition,
    resolve_carrier_code,
)

if TYPE_CHECKING:
    from apps.scm.containers.models import Container
    from apps.teams.models import Team

logger = logging.getLogger(__name__)

# What kind of provider was chosen — the carrier itself, or somebody who watches it.
DIRECT = "direct"
AGGREGATOR = "aggregator"
NONE = "none"

# Why. Values rather than sentences, so a caller branches on them and the UI decides
# what to say.
DIRECT_PROVIDER_AVAILABLE = "DIRECT_PROVIDER_AVAILABLE"
DIRECT_PROVIDER_UNAVAILABLE = "DIRECT_PROVIDER_UNAVAILABLE"
DIRECT_PROVIDER_NOT_CONNECTED = "DIRECT_PROVIDER_NOT_CONNECTED"
NO_PROVIDER_AVAILABLE = "NO_PROVIDER_AVAILABLE"
CARRIER_UNKNOWN = "CARRIER_UNKNOWN"
# An administrator chose this container's provider. The second value is the one that
# matters: it is a route with no provider, so nothing is fetched, and it is a
# *different* answer from NO_PROVIDER_AVAILABLE — somebody's setting is wrong rather
# than nobody being able to help.
OVERRIDE_PROVIDER_AVAILABLE = "OVERRIDE_PROVIDER_AVAILABLE"
OVERRIDE_PROVIDER_UNAVAILABLE = "OVERRIDE_PROVIDER_UNAVAILABLE"


@dataclass(frozen=True)
class TrackingRoute:
    """Which provider to ask about a carrier's container, and why that one.

    ``available`` is the only thing that authorises a fetch. A route with no provider is
    still a useful answer: it says the carrier is known and nothing can currently be
    asked about it, which is a configuration gap rather than an unknown container.
    """

    carrier_code: str = ""
    carrier_name: str = ""
    provider_code: str = ""
    provider_name: str = ""
    route_type: str = NONE
    reason: str = NO_PROVIDER_AVAILABLE
    # The provider's own handle for the carrier, when it needs one to be asked. Traqo's
    # sealine goes here; a direct carrier needs nothing beyond the container number.
    provider_reference: str = ""
    # Every provider that could have served this carrier, best first. The chosen one is
    # the first; the rest are what a fallback would try, and are worth reporting.
    #
    # Empty for an explicitly chosen provider, and that is the point: there is nothing
    # a fallback may try, so there is nothing to report.
    alternatives: tuple[str, ...] = ()
    # True when an administrator chose this container's provider rather than routing
    # searching for one. Read by callers that must explain *why* nothing was fetched.
    is_override: bool = False
    # The provider that was *asked for*, when one was chosen. Kept separately because
    # an unavailable override has no ``provider_code`` — nothing may be fetched — and
    # the message still has to name the choice that failed.
    requested_provider_code: str = ""

    @property
    def available(self) -> bool:
        return bool(self.provider_code)

    @property
    def is_direct(self) -> bool:
        return self.route_type == DIRECT

    @property
    def is_aggregator(self) -> bool:
        return self.route_type == AGGREGATOR

    def __str__(self) -> str:
        return f"{self.carrier_code or '?'} via {self.provider_code or 'nobody'} ({self.reason})"


def resolve_tracking_route(
    *,
    team: Team,
    carrier_code: str,
    container: Container | None = None,
) -> TrackingRoute:
    """Choose the provider to fetch ``carrier_code``'s tracking through.

    ``container`` carries the one thing that makes routing vary per box: the provider an
    administrator chose for it. Without a container there is no override to read, so the
    answer is the team's ordinary routing — which is what every caller that does not
    start from a container wants.

    Never raises. An unregistered or empty carrier gets a route with no provider and
    ``CARRIER_UNKNOWN``, because there is nothing to route.
    """
    code = resolve_carrier_code(carrier_code) or ""
    if not code:
        return TrackingRoute(carrier_code=(carrier_code or "").strip(), reason=CARRIER_UNKNOWN)

    definition = _definition(code)
    carrier_name = definition.name if definition is not None else code

    chosen = _container_override(container)
    if chosen:
        return _override_route(
            team=team,
            container=container,
            code=code,
            carrier_name=carrier_name,
            definition=definition,
            chosen=chosen,
        )

    direct = _direct_route(team=team, code=code, carrier_name=carrier_name, definition=definition)
    aggregator = _team_default_route(team=team, code=code, carrier_name=carrier_name)
    alternatives = tuple(
        provider for provider in (direct[0] if direct else "", aggregator[0] if aggregator else "") if provider
    )

    if direct is not None:
        route = TrackingRoute(
            carrier_code=code,
            carrier_name=carrier_name,
            provider_code=direct[0],
            provider_name=direct[1],
            route_type=DIRECT,
            reason=DIRECT_PROVIDER_AVAILABLE,
            alternatives=alternatives,
        )
    elif aggregator is not None:
        route = TrackingRoute(
            carrier_code=code,
            carrier_name=carrier_name,
            provider_code=aggregator[0],
            provider_name=aggregator[1],
            route_type=AGGREGATOR,
            reason=_why_not_direct(team=team, code=code, definition=definition),
            provider_reference=aggregator[2],
            alternatives=alternatives,
        )
    else:
        route = TrackingRoute(
            carrier_code=code,
            carrier_name=carrier_name,
            reason=NO_PROVIDER_AVAILABLE,
        )

    logger.info(
        "Tracking route %s: carrier=%s selected provider=%s (%s).",
        container.container_id if container is not None else "-",
        code,
        route.provider_code or "none",
        route.reason,
    )
    return route


def get_route_for_subscription(subscription) -> TrackingRoute:
    """Return the route an existing watch represents, read from what it recorded.

    Not a fresh decision — this is the watch's own history, so a subscription created
    when a direct adapter was down keeps reading as the aggregator route it is until
    something re-routes it. Used by the read models that answer "who supplies this
    container's tracking data".
    """
    provider = subscription.provider
    carrier_code = subscription.carrier_code
    if not carrier_code:
        return TrackingRoute(
            provider_code=provider.code,
            provider_name=provider.name or provider.code,
            route_type=DIRECT if _is_registered(provider.code) else AGGREGATOR,
            reason=CARRIER_UNKNOWN,
            provider_reference=subscription.provider_reference,
        )

    is_direct = carrier_code == provider.code
    return TrackingRoute(
        carrier_code=carrier_code,
        carrier_name=subscription.carrier_name or carrier_code,
        provider_code=provider.code,
        provider_name=provider.name or provider.code,
        route_type=DIRECT if is_direct else AGGREGATOR,
        reason=DIRECT_PROVIDER_AVAILABLE if is_direct else DIRECT_PROVIDER_UNAVAILABLE,
        provider_reference=subscription.provider_reference,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _container_override(container) -> str:
    """The provider an administrator chose for this container, or "".

    Imported lazily because :mod:`.preferences` reads the containers app, and routing
    has to stay callable from it.
    """
    if container is None:
        return ""
    from .preferences import get_container_provider_override

    return get_container_provider_override(container)


def _override_route(*, team, container, code: str, carrier_name: str, definition, chosen: str) -> TrackingRoute:
    """Route to the provider somebody chose, or report that it cannot be asked.

    No search and no fallback: the two branches are "the chosen provider can answer"
    and "it cannot, and here is a route with no provider saying so". The second is
    what stops a deselected provider quietly supplying a container's tracking again.

    ``alternatives`` stays empty even where another provider would have worked —
    reporting one would suggest routing is about to try it.
    """
    from apps.scm.integrations.traqo import PROVIDER_CODE as TRAQO_PROVIDER_CODE

    if chosen == TRAQO_PROVIDER_CODE:
        traqo = _traqo_route(code=code, carrier_name=carrier_name)
        resolved = (traqo[0], traqo[1], AGGREGATOR, traqo[2]) if traqo is not None else None
    else:
        # A direct provider is only legal for the carrier that is actually moving the
        # box: asking Maersk about a CMA CGM container is not a fallback, it is a
        # question with no answer. Carrier identity and tracking provider are separate
        # facts, and this is the one place the two have to agree.
        direct = (
            _direct_route(team=team, code=code, carrier_name=carrier_name, definition=definition)
            if chosen == code
            else None
        )
        resolved = (direct[0], direct[1], DIRECT, "") if direct is not None else None

    if resolved is None:
        logger.warning(
            "Container %s is set to track via %s, which cannot be asked about carrier %s.",
            container.container_id if container is not None else "-",
            chosen,
            code,
        )
        return TrackingRoute(
            carrier_code=code,
            carrier_name=carrier_name,
            route_type=NONE,
            reason=OVERRIDE_PROVIDER_UNAVAILABLE,
            is_override=True,
            requested_provider_code=chosen,
        )

    provider_code, provider_name, route_type, provider_reference = resolved
    route = TrackingRoute(
        carrier_code=code,
        carrier_name=carrier_name,
        provider_code=provider_code,
        provider_name=provider_name,
        route_type=route_type,
        reason=OVERRIDE_PROVIDER_AVAILABLE,
        provider_reference=provider_reference,
        is_override=True,
        requested_provider_code=chosen,
    )
    logger.info(
        "Tracking route %s: carrier=%s chosen provider=%s (%s).",
        container.container_id if container is not None else "-",
        code,
        provider_code,
        route.reason,
    )
    return route


def _definition(code: str):
    try:
        return get_carrier_definition(code)
    except UnknownCarrierError:
        return None


def _is_registered(code: str) -> bool:
    return _definition(code) is not None


def _direct_route(*, team, code: str, carrier_name: str, definition) -> tuple[str, str] | None:
    """Return ``(provider_code, provider_name)`` when the carrier can be called directly.

    Three conditions, the same three a discovery sweep applies — a registered adapter,
    the ability to answer by container number, and an active integration for this team.
    Anything less would route to a call that cannot succeed, which is worse than routing
    to an aggregator that can.
    """
    if definition is None:
        return None

    capabilities = definition.capabilities
    if not (capabilities.supports_pull and capabilities.supports_tracking_by_container):
        return None

    from apps.scm.integrations.carriers.factory import get_carrier_integration

    if get_carrier_integration(team, code) is None:
        return None

    return code, carrier_name


def _team_default_route(*, team, code: str, carrier_name: str) -> tuple[str, str, str] | None:
    """Return the team's default provider for this carrier, or None.

    The aggregator tier, named by ``TeamTrackingSettings.default_provider_code`` rather
    than hardcoded — which is what makes that field a setting instead of a label. It is
    Traqo for every team today, so the default value is Traqo and this dispatches to
    one branch; a second aggregator that could be polled on a schedule would add one
    here, and nothing else would change.
    """
    from apps.scm.integrations.traqo import PROVIDER_CODE as TRAQO_PROVIDER_CODE

    from .preferences import get_team_default_provider

    default = get_team_default_provider(team)
    if default == TRAQO_PROVIDER_CODE:
        return _traqo_route(code=code, carrier_name=carrier_name)

    logger.error(
        "Team %s has default tracking provider %r, which routing cannot reach; no aggregator route for %s.",
        getattr(team, "pk", team),
        default,
        code,
    )
    return None


def _traqo_route(*, code: str, carrier_name: str) -> tuple[str, str, str] | None:
    """Return ``(provider_code, provider_name, sealine)`` when Traqo can watch this carrier.

    Two conditions. Traqo has to be configured for live calls at all, and it has to
    publish a sealine for this carrier — asking about a carrier Traqo does not cover
    would spend a shipment slot on a call that cannot answer.
    """
    from apps.scm.integrations.traqo import PROVIDER_CODE, PROVIDER_NAME
    from apps.scm.integrations.traqo.discovery import is_traqo_configured
    from apps.scm.integrations.traqo.sealines import sealine_for_carrier_code

    if not is_traqo_configured():
        return None

    sealine = sealine_for_carrier_code(code)
    if not sealine:
        logger.info("Traqo publishes no sealine for %s (%s); it cannot be routed there.", code, carrier_name)
        return None

    return PROVIDER_CODE, PROVIDER_NAME, sealine


def _why_not_direct(*, team, code: str, definition) -> str:
    """Distinguish "this carrier has no usable direct adapter" from "not connected".

    Both send the container to an aggregator, and they need different things done about
    them: the first is ours to build, the second is a setting somebody can change.
    """
    if definition is None:
        return DIRECT_PROVIDER_UNAVAILABLE

    capabilities = definition.capabilities
    if not (capabilities.supports_pull and capabilities.supports_tracking_by_container):
        return DIRECT_PROVIDER_UNAVAILABLE

    from apps.scm.integrations.carriers.factory import get_carrier_integration

    if get_carrier_integration(team, code) is None:
        return DIRECT_PROVIDER_NOT_CONNECTED

    return DIRECT_PROVIDER_UNAVAILABLE  # pragma: no cover — direct would have been chosen
