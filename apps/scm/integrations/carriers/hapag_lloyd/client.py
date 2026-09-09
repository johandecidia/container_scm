"""Hapag-Lloyd Track & Trace client.

Hapag-Lloyd follows the DCSA standard, so this is the shared
:class:`DcsaCarrierClient` with Hapag-Lloyd's identity and capabilities. The carrier
adds no transport code of its own: no HTTP handling, no retry policy, no auth flow,
no parser.

What is Hapag-Lloyd's own is the gateway it sits behind. Its API portal issues a
client id and client secret and expects both as headers on every request, with no
token endpoint — so the auth style is ``client_id_secret_headers`` rather than
Maersk's single-header key or an OAuth grant. See ``README.md`` in this package.
"""

from __future__ import annotations

from apps.scm.integrations.carriers.base import CarrierCapability
from apps.scm.integrations.carriers.dcsa.client import DcsaCarrierClient, resolve_dcsa_config

PROVIDER_CODE = "hapag_lloyd"
CARRIER_NAME = "Hapag-Lloyd"

# Settings for Hapag-Lloyd's DCSA Track & Trace product. Configuration data, not
# secrets: the client id and secret are stored encrypted through the credential
# service and never appear here.
#
# Applied to a team's Integration by the ``setup_hapag_lloyd_integration`` command,
# which overrides ``base_url`` and ``tracking_path`` from the environment when they
# are set. A team on a different Hapag product overrides any of it on its own
# Integration.config.
#
# ``tracking_path`` is the one value that must be checked against the OpenAPI spec of
# the subscribed product on the portal: Hapag-Lloyd versions its gateway paths per
# product, and a wrong path answers 404, which this transport reads as "the carrier
# does not know this reference". The setup command prints the resolved URL for
# exactly that reason. Everything else here is from Hapag-Lloyd's own documentation.
TRACK_AND_TRACE_CONFIG: dict = {
    "base_url": "https://api.hlag.com",
    "tracking_path": "/hlag/external/v2/events",
    "auth_style": "client_id_secret_headers",
    "client_id_header_name": "X-IBM-Client-Id",
    "client_secret_header_name": "X-IBM-Client-Secret",
    "reference_params": {
        "container_number": "equipmentReference",
        "bill_of_lading_number": "transportDocumentReference",
        "booking_number": "carrierBookingReference",
    },
    "extra_headers": {
        "Accept": "application/json",
    },
    "request_timeout_seconds": 30,
    "max_retries": 3,
    "retry_backoff_seconds": 0.5,
    "no_data_statuses": [404],
}


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
