from apps.scm.integrations.carriers.base import BaseCarrierParser


class HapagLloydParser(BaseCarrierParser):
    """Hapag-Lloyd payload parser — stub, awaiting API access.

    parse_tracking_events() is inherited from BaseCarrierParser and raises
    CarrierNotImplementedError until the carrier response format is known.
    """

    provider_code = "hapag_lloyd"
