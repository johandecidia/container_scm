from apps.scm.integrations.carriers.base import BaseCarrierClient


class HapagLloydClient(BaseCarrierClient):
    """Hapag-Lloyd tracking client — stub, awaiting API access.

    Inherits the full contract from BaseCarrierClient, whose methods raise
    CarrierNotImplementedError. That keeps an unimplemented carrier from ever
    being mistaken for a carrier that returned no data.
    """

    provider_code = "hapag_lloyd"
