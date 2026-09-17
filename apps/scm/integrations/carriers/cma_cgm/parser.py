"""CMA CGM payload parser.

CMA CGM publishes DCSA Track & Trace 2.2.0 events, so the shared
:class:`DcsaCarrierParser` handles them. This subclass exists to name the provider and
to hold any genuine CMA CGM deviation, of which there is none: ``carrierSpecificData``
and every other extension field survive on the event's ``raw_payload``, which is
stored verbatim, so nothing is lost by not modelling it here.
"""

from __future__ import annotations

from apps.scm.integrations.carriers.dcsa.carrier_parser import DcsaCarrierParser

PROVIDER_CODE = "cma_cgm"


class CmaCgmParser(DcsaCarrierParser):
    """Parses CMA CGM DCSA Track & Trace responses into normalised events."""

    provider_code = PROVIDER_CODE
