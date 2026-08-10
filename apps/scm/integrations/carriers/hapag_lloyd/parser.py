"""Hapag-Lloyd payload parser.

Hapag-Lloyd publishes DCSA-conformant Track & Trace events, so the shared
:class:`DcsaCarrierParser` handles them. This subclass exists to name the provider
and to hold any genuine Hapag-Lloyd deviation, of which there is none yet.
"""

from __future__ import annotations

from apps.scm.integrations.carriers.dcsa.carrier_parser import DcsaCarrierParser

PROVIDER_CODE = "hapag_lloyd"


class HapagLloydParser(DcsaCarrierParser):
    """Parses Hapag-Lloyd DCSA Track & Trace responses into normalised events."""

    provider_code = PROVIDER_CODE
