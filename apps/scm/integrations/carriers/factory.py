"""Factory for building carrier adapters with their team configuration injected.

This is the only place that connects a carrier class from the registry to a
team's ``Integration`` record. Clients never look up their own configuration, so
a client can never accidentally read another team's credentials.

Typical use::

    client = build_carrier_client("maersk", team=team)      # live, if configured
    parser = build_carrier_parser("maersk")

``build_carrier_client`` raises :class:`CarrierConfigurationError` when
``require_integration=True`` and the team has no active carrier integration for
that provider.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import BaseCarrierClient, BaseCarrierParser
from .exceptions import CarrierConfigurationError
from .registry import get_carrier_definition

if TYPE_CHECKING:
    from apps.scm.integrations.models import Integration
    from apps.teams.models import Team

logger = logging.getLogger(__name__)


def get_carrier_integration(team: Team, provider_code: str) -> Integration | None:
    """Return the team's active carrier integration for a provider, or None.

    Only ``provider_family=CARRIER`` integrations are considered, so a business
    system sharing a provider code can never be used as a carrier.
    """
    from apps.scm.integrations.models import Integration

    return Integration.objects.filter(
        team=team,
        provider_code=provider_code,
        provider_family=Integration.ProviderFamily.CARRIER,
        is_active=True,
    ).first()


def build_carrier_client(
    provider_code: str,
    *,
    team: Team | None = None,
    integration: Integration | None = None,
    require_integration: bool = False,
) -> BaseCarrierClient:
    """Build a carrier client for a provider code.

    Pass either an ``integration`` directly, or a ``team`` to have its active
    carrier integration resolved. With neither, an unconfigured client is
    returned — useful for capability inspection, and safe because every
    network-touching method raises without configuration.

    Raises :class:`UnknownCarrierError` for unregistered provider codes, and
    :class:`CarrierConfigurationError` when ``require_integration`` is set but no
    integration could be resolved.
    """
    definition = get_carrier_definition(provider_code)

    if integration is None and team is not None:
        integration = get_carrier_integration(team, provider_code)

    if integration is None and require_integration:
        raise CarrierConfigurationError(
            f"No active carrier integration configured for '{provider_code}'.",
            provider_code=provider_code,
        )

    return definition.client_class(integration)


def build_carrier_parser(provider_code: str) -> BaseCarrierParser:
    """Build the parser for a provider code.

    Parsers are stateless with respect to team data — they only normalise
    payloads — so they need no integration.
    """
    return get_carrier_definition(provider_code).parser_class()
