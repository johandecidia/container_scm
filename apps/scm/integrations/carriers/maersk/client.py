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

# The verified settings for Maersk's public Track & Trace events endpoint, which
# answers on an API key alone — no account number, no OAuth token exchange. This is
# configuration data, not a secret: the consumer key itself is stored encrypted
# through the credential service and never appears here.
#
# Applied to a team's Integration by the ``setup_maersk_integration`` management
# command. A team may still override any of it on its own Integration.config, e.g.
# to point at a contracted product with a different path or auth style.
PUBLIC_TRACK_AND_TRACE_CONFIG: dict = {
    "base_url": "https://api.maersk.com",
    "tracking_path": "/track-and-trace/public-events",
    "auth_style": "api_key_header",
    "reference_params": {
        "container_number": "equipmentReference",
    },
    "api_key_header_name": "consumer-key",
    "extra_headers": {
        "API-Version": "1",
        "Accept": "application/json",
    },
    "test_connection_reference": "TRDU9258963",
    "request_timeout_seconds": 30,
    "max_retries": 3,
    "retry_backoff_seconds": 0.5,
    "no_data_statuses": [404],
}


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
        # The public Track & Trace events endpoint authenticates on the consumer key
        # alone and returns data without an account number, so declaring one as
        # required would misdescribe the integration. Contracted Maersk products that
        # do need an account number carry it in their own Integration.config.
        requires_account_number=False,
    )
