"""Maersk Track & Trace client.

Maersk follows the DCSA standard, so the transport is the shared
:class:`DcsaCarrierClient`; only the carrier's identity and capabilities are Maersk's
own. Every endpoint-specific value — base URL, path, query parameter names, auth
style — comes from the team's ``Integration.config``. Nothing is guessed and no URL
is hardcoded, so an unconfigured integration raises CarrierConfigurationError and
the sync layer records the run as SKIPPED.

See ``README.md`` in this package for the configuration and credential keys, and for
what must still be confirmed against Maersk's documentation and the customer
agreement before live traffic is enabled.
"""

from __future__ import annotations

from apps.scm.integrations.carriers.base import CarrierCapability
from apps.scm.integrations.carriers.dcsa.client import DcsaCarrierClient, resolve_dcsa_config

PROVIDER_CODE = "maersk"
CARRIER_NAME = "Maersk"


def resolve_config(config: dict, *, provider_code: str = PROVIDER_CODE):
    """Validate Maersk's live configuration, or explain exactly what is missing."""
    return resolve_dcsa_config(config, provider_code=provider_code, carrier_name=CARRIER_NAME)


class MaerskClient(DcsaCarrierClient):
    """Maersk Track & Trace client."""

    provider_code = PROVIDER_CODE
    carrier_name = CARRIER_NAME
    capabilities = CarrierCapability(
        supports_pull=True,
        supports_webhooks=True,
        supports_subscriptions=True,
        supports_tracking_by_container=True,
        supports_tracking_by_bl=True,
        supports_tracking_by_booking=True,
        supports_dcsa=True,
        supports_schedules=True,
        supports_discovery=True,
        requires_customer_approval=True,
        requires_account_number=True,
    )
