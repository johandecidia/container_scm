"""Which carriers are implemented, and which are still stubs.

Derived from the registry rather than hardcoded, so implementing a carrier moves it
between the two groups automatically and the guard tests keep applying to whatever
remains a stub.

A carrier counts as implemented when its client overrides ``fetch_tracking``. A stub
inherits the base method, which raises CarrierNotImplementedError.
"""

from apps.scm.integrations.carriers.base import BaseCarrierClient
from apps.scm.integrations.carriers.registry import list_carriers

ALL_CARRIER_CODES = [definition.provider_code for definition in list_carriers()]


def is_implemented(provider_code: str) -> bool:
    """True when the carrier's client provides its own fetch_tracking."""
    for definition in list_carriers():
        if definition.provider_code == provider_code:
            return definition.client_class.fetch_tracking is not BaseCarrierClient.fetch_tracking
    return False


IMPLEMENTED_CARRIER_CODES = [code for code in ALL_CARRIER_CODES if is_implemented(code)]
STUB_CARRIER_CODES = [code for code in ALL_CARRIER_CODES if not is_implemented(code)]
