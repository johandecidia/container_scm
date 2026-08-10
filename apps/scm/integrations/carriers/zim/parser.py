from apps.scm.integrations.carriers.base import BaseCarrierParser


class ZimParser(BaseCarrierParser):
    """ZIM payload parser — stub, awaiting API access.

    parse_tracking_events() is inherited from BaseCarrierParser and raises
    CarrierNotImplementedError until the carrier response format is known.
    """

    provider_code = "zim"
