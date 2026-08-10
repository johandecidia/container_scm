"""Hapag-Lloyd Track & Trace client.

Hapag-Lloyd follows the DCSA standard, so this is the shared
:class:`DcsaCarrierClient` with Hapag-Lloyd's identity and capabilities. The second
carrier therefore adds no transport code of its own: no HTTP handling, no retry
policy, no auth flow, no parser.

As with Maersk, nothing endpoint-specific is hardcoded — see ``README.md`` in this
package for the configuration that must come from Hapag-Lloyd's documentation and
the customer agreement before live traffic is enabled.
"""

from __future__ import annotations

from apps.scm.integrations.carriers.base import CarrierCapability
from apps.scm.integrations.carriers.dcsa.client import DcsaCarrierClient, resolve_dcsa_config

PROVIDER_CODE = "hapag_lloyd"
CARRIER_NAME = "Hapag-Lloyd"


def resolve_config(config: dict, *, provider_code: str = PROVIDER_CODE):
    """Validate Hapag-Lloyd's live configuration, or explain exactly what is missing."""
    return resolve_dcsa_config(config, provider_code=provider_code, carrier_name=CARRIER_NAME)


class HapagLloydClient(DcsaCarrierClient):
    """Hapag-Lloyd Track & Trace client."""

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
        supports_discovery=True,
        requires_customer_approval=True,
        requires_account_number=True,
    )
