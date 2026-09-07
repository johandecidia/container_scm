"""Given a known carrier, which provider do we actually call for its tracking?

The second half of a decision :mod:`apps.scm.integrations.carriers.carrier_resolution`
makes the first half of. Resolution says *ONE is moving this box*; routing says *and
Traqo is who we ask about it*, because ONE has no working direct tracking path here.
Those are different questions with different answers, and this module exists so the
second one is answered in exactly one place.

The order::

    1. the carrier's own API      when registered, able to answer by container
                                  number, and connected for this team
    2. Traqo                      when enabled, and publishing a sealine for
                                  this carrier
    3. nothing                    a clean NOT_CONFIGURED, not a guess

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
    alternatives: tuple[str, ...] = ()

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

    ``container`` is accepted for symmetry with the rest of the tracking layer and for
    logging; routing does not currently vary by container, and a future rule that made it
    vary — a carrier that only answers about boxes on a booking, say — would belong here
    rather than at a call site.

    Never raises. An unregistered or empty carrier gets a route with no provider and
    ``CARRIER_UNKNOWN``, because there is nothing to route.
    """
    code = resolve_carrier_code(carrier_code) or ""
    if not code:
        return TrackingRoute(carrier_code=(carrier_code or "").strip(), reason=CARRIER_UNKNOWN)

    definition = _definition(code)
    carrier_name = definition.name if definition is not None else code

    direct = _direct_route(team=team, code=code, carrier_name=carrier_name, definition=definition)
    aggregator = _traqo_route(code=code, carrier_name=carrier_name)
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
