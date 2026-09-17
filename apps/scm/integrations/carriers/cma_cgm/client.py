"""CMA CGM Track & Trace client.

CMA CGM publishes DCSA Track & Trace 2.2.0, so the transport is the shared
:class:`DcsaCarrierClient` and this package holds only CMA CGM's identity,
capabilities and verified endpoint settings. No HTTP handling, no retry policy, no
auth flow and no parser are repeated here — the third DCSA carrier adds none of that.

Every endpoint-specific value comes from the team's ``Integration.config``; a missing
one raises CarrierConfigurationError, which the sync layer records as SKIPPED rather
than as "synced, no events".

See ``README.md`` in this package for the configuration keys, the credential key and
what is deliberately out of MVP scope (OAuth2 private events, webhooks,
subscriptions).
"""

from __future__ import annotations

from apps.scm.integrations.carriers.base import CarrierCapability
from apps.scm.integrations.carriers.dcsa.client import DcsaCarrierClient, resolve_dcsa_config

PROVIDER_CODE = "cma_cgm"
CARRIER_NAME = "CMA CGM"

# The settings for CMA CGM's public Track & Trace events endpoint, taken from the
# CMA CGM Swagger (DCSA T&T 2.2.0, CMA API 1.2.9, server path
# /operation/trackandtrace/v1). It answers on an API key in the ``keyId`` header
# alone — no account number and no token exchange.
#
# This is configuration data, not a secret: the API key itself is stored encrypted
# through the credential service and never appears here.
#
# Applied to a team's Integration by the ``setup_cma_cgm_integration`` management
# command. A team may override any of it on its own Integration.config, e.g. to point
# at the contracted commercial product with OAuth2.
#
# ``test_connection_reference`` is deliberately absent: a reference known to the
# account belongs to that account, not to this repository. Without it,
# ``test_connection()`` reports the missing key rather than probing a guessed box.
PUBLIC_TRACK_AND_TRACE_CONFIG: dict = {
    "base_url": "https://apis.cma-cgm.net",
    "tracking_path": "/operation/trackandtrace/v1/events",
    "auth_style": "api_key_header",
    "reference_params": {
        "container_number": "equipmentReference",
        "bill_of_lading_number": "transportDocumentReference",
        "booking_number": "carrierBookingReference",
    },
    "api_key_header_name": "keyId",
    "extra_headers": {
        "Accept": "application/json",
    },
    # CMA CGM pages /events with ``limit`` and ``cursor``, advertising the next
    # cursor in a ``Next-Page`` response header. Followed by the shared DCSA client.
    "pagination": {
        "cursor_param": "cursor",
        "next_page_header": "Next-Page",
        "limit_param": "limit",
        "page_size": 100,
        "max_pages": 20,
    },
    "request_timeout_seconds": 30,
    "max_retries": 3,
    "retry_backoff_seconds": 0.5,
    "no_data_statuses": [404],
}


def resolve_config(config: dict, *, provider_code: str = PROVIDER_CODE):
    """Validate CMA CGM's live configuration, or explain exactly what is missing."""
    return resolve_dcsa_config(config, provider_code=provider_code, carrier_name=CARRIER_NAME)


class CmaCgmClient(DcsaCarrierClient):
    """CMA CGM Track & Trace client.

    TODO: CMA CGM's ``/events`` endpoint accepts ``equipmentReference`` and
    ``carrierBookingReference`` together, which would pin a container to one
    commercial cycle instead of returning every cycle it has been part of. The
    carrier contract in :func:`carriers.base.resolve_tracking_reference` deliberately
    allows exactly one reference per call, so that precision is not available yet;
    combining them means widening the shared contract for every carrier, not working
    around it here.
    """

    provider_code = PROVIDER_CODE
    carrier_name = CARRIER_NAME
    capabilities = CarrierCapability(
        supports_pull=True,
        # Provider capability, as everywhere in this layer: CMA CGM's platform offers
        # webhooks and subscriptions, and Container SCM has not implemented either
        # yet. Whether a team can actually use one is the Integration record's job.
        supports_webhooks=True,
        supports_subscriptions=True,
        supports_tracking_by_container=True,
        supports_tracking_by_bl=True,
        supports_tracking_by_booking=True,
        supports_dcsa=True,
        supports_discovery=True,
        requires_customer_approval=True,
        # The public Track & Trace endpoint authenticates on the API key alone and
        # returns data without an account number, so declaring one as required would
        # misdescribe the integration.
        requires_account_number=False,
    )
