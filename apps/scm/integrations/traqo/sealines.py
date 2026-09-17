"""Translating a Container SCM carrier into the SCAC Traqo calls a ``sealine``.

Traqo's container endpoint requires a 4-character SCAC, and Container SCM already
knows which carrier a container belongs to — from its shipment, its planned-container
record or the carrier that already tracks it. This module is only the translation
between the two vocabularies. It deliberately contains no container-prefix
guessing: deciding *which* carrier to ask is carrier discovery's job, and Traqo
must not become a second place that answers that question.

Every code below was read from Traqo's own carrier list
(``GET /api/v1/sandbox/carriers``) and paired with the registry's provider code —
none is inferred from a container prefix or from documentation prose. Evergreen is
absent on purpose: Traqo does not publish a sealine for it, and inventing EGLV
here would produce calls that fail for a reason nobody could trace back.
"""

from __future__ import annotations

import re

from apps.scm.integrations.carriers.exceptions import CarrierConfigurationError
from apps.scm.integrations.carriers.registry import resolve_carrier_code

from . import PROVIDER_CODE

# Container SCM provider_code → the SCAC Traqo publishes for that carrier.
CARRIER_CODE_TO_SEALINE: dict[str, str] = {
    "maersk": "MAEU",
    "msc": "MSCU",
    "cma_cgm": "CMDU",
    "hapag_lloyd": "HLCU",
    "cosco": "COSU",
    "one": "ONEY",
    "yang_ming": "YMLU",
    "zim": "ZIMU",
    "hmm": "HDMU",
}

# Carriers Traqo lists that Container SCM has no adapter for. Kept so a caller can
# pass the SCAC straight through and still be told it is a code Traqo knows.
UNREGISTERED_SEALINES: frozenset[str] = frozenset({"OOLU"})

_SCAC_RE = re.compile(r"^[A-Z]{4}$")


def sealine_for_carrier_code(carrier_code: str) -> str | None:
    """Return the SCAC Traqo expects for a registry carrier code, or None.

    None means Traqo publishes no sealine for that carrier — the caller must say so
    rather than substitute a guess.
    """
    return CARRIER_CODE_TO_SEALINE.get((carrier_code or "").strip().lower())


def resolve_sealine(value: str) -> str:
    """Return the SCAC to send, from either a carrier code or a SCAC.

    Accepts what a caller is likely to have: a Container SCM carrier code
    ("maersk", "Hapag-Lloyd") or the SCAC itself ("MAEU"). Raises
    :class:`CarrierConfigurationError` for anything else, because a wrong or absent
    sealine makes Traqo answer about the wrong carrier or refuse the call — both
    worse than failing here with an explanation.
    """
    cleaned = (value or "").strip()
    if not cleaned:
        raise CarrierConfigurationError(
            "Traqo requires a sealine (4-character SCAC); none was supplied.",
            provider_code=PROVIDER_CODE,
        )

    carrier_code = resolve_carrier_code(cleaned)
    if carrier_code:
        sealine = sealine_for_carrier_code(carrier_code)
        if sealine:
            return sealine
        raise CarrierConfigurationError(
            f"Traqo publishes no sealine for carrier '{carrier_code}'; it cannot be tracked through Traqo.",
            provider_code=PROVIDER_CODE,
        )

    upper = cleaned.upper()
    if _SCAC_RE.match(upper):
        return upper

    raise CarrierConfigurationError(
        f"'{cleaned}' is neither a known carrier nor a 4-character SCAC.",
        provider_code=PROVIDER_CODE,
    )


def carrier_code_for_sealine(sealine: str) -> str | None:
    """Return the registry carrier code for a SCAC, or None when there is no adapter.

    The inverse lookup, so a Traqo response can be reported against the carrier the
    rest of the system names. None for a SCAC Traqo supports and Container SCM does
    not — the carrier is still identifiable by its SCAC.
    """
    upper = (sealine or "").strip().upper()
    for code, scac in CARRIER_CODE_TO_SEALINE.items():
        if scac == upper:
            return code
    return None
