# Carrier registry — single source of truth for supported carriers and their capabilities.
from dataclasses import dataclass, field

from .base import BaseCarrierClient, BaseCarrierParser, CarrierCapability


class UnknownCarrierError(Exception):
    """Raised when an unsupported carrier provider_code is requested."""


@dataclass
class CarrierDefinition:
    """Full metadata and class references for a registered carrier."""

    provider_code: str
    name: str
    client_class: type[BaseCarrierClient]
    parser_class: type[BaseCarrierParser]
    capabilities: CarrierCapability
    # Publicly registered ISO 6346 / BIC owner prefixes for this carrier's own
    # containers. Only ever a *suggestion* of which carrier to ask: a container may
    # be leased or subchartered, so the prefix can be misleading and must never
    # override an explicitly chosen carrier.
    owner_prefixes: tuple[str, ...] = ()
    # The SCACs that identify this carrier to the outside world, canonical first.
    #
    # Unlike ``owner_prefixes`` these are *identity*, not a hint: a provider that
    # answers "this box is ONEY" has named the carrier. They live here so an
    # aggregator that speaks SCAC — Traqo's lookup, Vizion's ACI — translates into
    # the registry's own vocabulary through one table rather than each keeping its
    # own. More than one is normal: Yang Ming's SCAC moved from YMLU to YMJA in
    # 2023 and providers still report either.
    scac_codes: tuple[str, ...] = ()
    # The verified endpoint settings this carrier ships with, applied to a team's
    # ``Integration.config`` when that integration is first created. Configuration,
    # never a secret — an API key or client secret lives only in the encrypted
    # credential row.
    #
    # Empty is meaningful: it says this adapter has no working live configuration, so
    # Settings must not offer to connect it. That is what separates the three carriers
    # with real DCSA clients from the seven registered stubs, and it is a fact about
    # the adapter, so it belongs here rather than in a list of names kept by a view.
    default_config: dict = field(default_factory=dict)

    @property
    def is_connectable(self) -> bool:
        """Whether a team could actually configure and use this carrier today.

        Three things at once: a live configuration to start from, the ability to pull,
        and the ability to answer about a container number. Anything less and offering
        "Connect" would lead to a call that cannot succeed.
        """
        return bool(
            self.default_config and self.capabilities.supports_pull and self.capabilities.supports_tracking_by_container
        )


def _build_registry() -> dict[str, CarrierDefinition]:
    # Imports are deferred to avoid circular imports at module level.
    from .cma_cgm.client import PUBLIC_TRACK_AND_TRACE_CONFIG as CMA_CGM_CONFIG
    from .cma_cgm.client import CmaCgmClient
    from .cma_cgm.parser import CmaCgmParser
    from .cosco.client import CoscoClient
    from .cosco.parser import CoscoParser
    from .evergreen.client import EvergreenClient
    from .evergreen.parser import EvergreenParser
    from .hapag_lloyd.client import TRACK_AND_TRACE_CONFIG as HAPAG_LLOYD_CONFIG
    from .hapag_lloyd.client import HapagLloydClient
    from .hapag_lloyd.parser import HapagLloydParser
    from .hmm.client import HmmClient
    from .hmm.parser import HmmParser
    from .maersk.client import PUBLIC_TRACK_AND_TRACE_CONFIG as MAERSK_CONFIG
    from .maersk.client import MaerskClient
    from .maersk.parser import MaerskParser
    from .msc.client import MscClient
    from .msc.parser import MscParser
    from .one.client import OneClient
    from .one.parser import OneParser
    from .yang_ming.client import YangMingClient
    from .yang_ming.parser import YangMingParser
    from .zim.client import ZimClient
    from .zim.parser import ZimParser

    return {
        "maersk": CarrierDefinition(
            provider_code="maersk",
            name="Maersk",
            client_class=MaerskClient,
            parser_class=MaerskParser,
            capabilities=CarrierCapability(
                supports_pull=True,
                supports_webhooks=True,
                supports_subscriptions=True,
                supports_tracking_by_container=True,
                supports_tracking_by_bl=True,
                supports_tracking_by_booking=True,
                supports_tracking_by_purchase_order=False,
                supports_dcsa=True,
                supports_schedules=True,
                supports_booking=False,
                requires_customer_approval=True,
                # The public Track & Trace events endpoint works on the consumer key
                # alone — see carriers/maersk/client.py.
                requires_account_number=False,
            ),
            owner_prefixes=("MAEU", "MRKU", "MSKU", "MRSU"),
            scac_codes=("MAEU",),
            default_config=dict(MAERSK_CONFIG),
        ),
        "msc": CarrierDefinition(
            provider_code="msc",
            name="MSC (Mediterranean Shipping Company)",
            client_class=MscClient,
            parser_class=MscParser,
            capabilities=CarrierCapability(
                supports_pull=True,
                supports_webhooks=False,
                supports_subscriptions=False,
                supports_tracking_by_container=True,
                supports_tracking_by_bl=True,
                supports_tracking_by_booking=True,
                supports_tracking_by_purchase_order=False,
                supports_dcsa=False,
                supports_schedules=False,
                supports_booking=False,
                requires_customer_approval=False,
                requires_account_number=False,
            ),
            owner_prefixes=("MSCU", "MEDU"),
            scac_codes=("MSCU",),
        ),
        "cma_cgm": CarrierDefinition(
            provider_code="cma_cgm",
            name="CMA CGM",
            client_class=CmaCgmClient,
            parser_class=CmaCgmParser,
            capabilities=CarrierCapability(
                supports_pull=True,
                supports_webhooks=True,
                supports_subscriptions=True,
                supports_tracking_by_container=True,
                supports_tracking_by_bl=True,
                supports_tracking_by_booking=True,
                supports_tracking_by_purchase_order=False,
                supports_dcsa=True,
                supports_schedules=False,
                supports_booking=False,
                requires_customer_approval=True,
                # The public Track & Trace events endpoint answers on the keyId API
                # key alone — see carriers/cma_cgm/client.py.
                requires_account_number=False,
            ),
            owner_prefixes=("CMAU", "CGMU", "ECMU"),
            scac_codes=("CMDU",),
            default_config=dict(CMA_CGM_CONFIG),
        ),
        "cosco": CarrierDefinition(
            provider_code="cosco",
            name="COSCO Shipping",
            client_class=CoscoClient,
            parser_class=CoscoParser,
            capabilities=CarrierCapability(
                supports_pull=True,
                supports_webhooks=False,
                supports_subscriptions=False,
                supports_tracking_by_container=True,
                supports_tracking_by_bl=True,
                supports_tracking_by_booking=True,
                supports_tracking_by_purchase_order=False,
                supports_dcsa=False,
                supports_schedules=False,
                supports_booking=False,
                requires_customer_approval=False,
                requires_account_number=False,
            ),
            owner_prefixes=("COSU", "CBHU", "CCLU"),
            scac_codes=("COSU",),
        ),
        "hapag_lloyd": CarrierDefinition(
            provider_code="hapag_lloyd",
            name="Hapag-Lloyd",
            client_class=HapagLloydClient,
            parser_class=HapagLloydParser,
            capabilities=CarrierCapability(
                supports_pull=True,
                supports_webhooks=True,
                supports_subscriptions=True,
                supports_tracking_by_container=True,
                supports_tracking_by_bl=True,
                supports_tracking_by_booking=True,
                supports_tracking_by_purchase_order=False,
                supports_dcsa=True,
                supports_schedules=False,
                supports_booking=False,
                requires_customer_approval=True,
                requires_account_number=True,
            ),
            owner_prefixes=("HLXU", "HLCU", "HLBU"),
            scac_codes=("HLCU",),
            default_config=dict(HAPAG_LLOYD_CONFIG),
        ),
        "one": CarrierDefinition(
            provider_code="one",
            name="ONE (Ocean Network Express)",
            client_class=OneClient,
            parser_class=OneParser,
            capabilities=CarrierCapability(
                supports_pull=True,
                supports_webhooks=False,
                supports_subscriptions=False,
                supports_tracking_by_container=True,
                supports_tracking_by_bl=True,
                supports_tracking_by_booking=True,
                supports_tracking_by_purchase_order=False,
                supports_dcsa=False,
                supports_schedules=False,
                supports_booking=False,
                requires_customer_approval=False,
                requires_account_number=False,
            ),
            owner_prefixes=("ONEU", "NYKU", "MOLU"),
            scac_codes=("ONEY",),
        ),
        "evergreen": CarrierDefinition(
            provider_code="evergreen",
            name="Evergreen Line",
            client_class=EvergreenClient,
            parser_class=EvergreenParser,
            capabilities=CarrierCapability(
                supports_pull=False,
                supports_webhooks=False,
                supports_subscriptions=False,
                supports_tracking_by_container=True,
                supports_tracking_by_bl=True,
                supports_tracking_by_booking=False,
                supports_tracking_by_purchase_order=False,
                supports_dcsa=False,
                supports_schedules=False,
                supports_booking=False,
                requires_customer_approval=False,
                requires_account_number=False,
            ),
            owner_prefixes=("EGLV", "EISU", "EGHU"),
            scac_codes=("EGLV",),
        ),
        "hmm": CarrierDefinition(
            provider_code="hmm",
            name="HMM (Hyundai Merchant Marine)",
            client_class=HmmClient,
            parser_class=HmmParser,
            capabilities=CarrierCapability(
                supports_pull=True,
                supports_webhooks=False,
                supports_subscriptions=False,
                supports_tracking_by_container=True,
                supports_tracking_by_bl=True,
                supports_tracking_by_booking=True,
                supports_tracking_by_purchase_order=False,
                supports_dcsa=False,
                supports_schedules=False,
                supports_booking=False,
                requires_customer_approval=False,
                requires_account_number=False,
            ),
            owner_prefixes=("HMMU", "HDMU"),
            scac_codes=("HDMU",),
        ),
        "yang_ming": CarrierDefinition(
            provider_code="yang_ming",
            name="Yang Ming Marine Transport",
            client_class=YangMingClient,
            parser_class=YangMingParser,
            capabilities=CarrierCapability(
                supports_pull=False,
                supports_webhooks=False,
                supports_subscriptions=False,
                supports_tracking_by_container=True,
                supports_tracking_by_bl=True,
                supports_tracking_by_booking=True,
                supports_tracking_by_purchase_order=True,
                supports_dcsa=False,
                supports_schedules=False,
                supports_booking=False,
                requires_customer_approval=False,
                requires_account_number=False,
            ),
            owner_prefixes=("YMLU", "YMMU"),
            scac_codes=("YMLU", "YMJA"),
        ),
        "zim": CarrierDefinition(
            provider_code="zim",
            name="ZIM Integrated Shipping Services",
            client_class=ZimClient,
            parser_class=ZimParser,
            capabilities=CarrierCapability(
                supports_pull=False,
                supports_webhooks=False,
                supports_subscriptions=False,
                supports_tracking_by_container=True,
                supports_tracking_by_bl=True,
                supports_tracking_by_booking=False,
                supports_tracking_by_purchase_order=False,
                supports_dcsa=False,
                supports_schedules=False,
                supports_booking=False,
                requires_customer_approval=False,
                requires_account_number=False,
            ),
            owner_prefixes=("ZIMU", "ZCSU"),
            scac_codes=("ZIMU",),
        ),
    }


# Module-level registry (populated lazily on first access).
_REGISTRY: dict[str, CarrierDefinition] | None = None


def _get_registry() -> dict[str, CarrierDefinition]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build_registry()
    return _REGISTRY


# ── Public helper functions ────────────────────────────────────────────────────


def get_carrier_definition(provider_code: str) -> CarrierDefinition:
    """Return the CarrierDefinition for the given provider_code.

    Raises UnknownCarrierError for unregistered codes.
    """
    registry = _get_registry()
    if provider_code not in registry:
        raise UnknownCarrierError(
            f"No carrier registered for provider_code '{provider_code}'. Known carriers: {sorted(registry.keys())}"
        )
    return registry[provider_code]


def get_carrier_client_class(provider_code: str) -> type:
    """Return the client class for the given provider_code."""
    return get_carrier_definition(provider_code).client_class


def get_carrier_parser_class(provider_code: str) -> type:
    """Return the parser class for the given provider_code."""
    return get_carrier_definition(provider_code).parser_class


def list_carriers() -> list[CarrierDefinition]:
    """Return all registered carrier definitions, sorted by provider_code."""
    return sorted(_get_registry().values(), key=lambda d: d.provider_code)


def list_connectable_carriers() -> list[CarrierDefinition]:
    """The carriers a team can actually connect and track containers through, by name.

    Registered is not the same as usable: seven of the ten definitions are stubs whose
    client raises rather than fetching. Settings offers this list, so a team is never
    invited to paste an API key into an adapter that cannot call anything — see
    :attr:`CarrierDefinition.is_connectable`.
    """
    return sorted((d for d in _get_registry().values() if d.is_connectable), key=lambda d: d.name)


def resolve_carrier_code(value: str) -> str | None:
    """Map a free-text carrier value to a registered provider code, or None.

    Accepts a provider code ("maersk"), a registered name ("Hapag-Lloyd"), or a
    close variant of either. Returns None rather than guessing when nothing
    matches — an unrecognised carrier must not silently become the wrong one.
    """
    cleaned = (value or "").strip().lower()
    if not cleaned:
        return None

    registry = _get_registry()
    if cleaned in registry:
        return cleaned

    normalised = cleaned.replace("-", " ").replace("_", " ")
    for definition in registry.values():
        candidates = {
            definition.provider_code.replace("_", " "),
            definition.name.lower(),
            definition.name.lower().split("(")[0].strip(),
        }
        if normalised in {candidate.replace("-", " ") for candidate in candidates}:
            return definition.provider_code
    return None


def resolve_carrier_code_from_scac(scac: str) -> str | None:
    """Map a SCAC to a registered provider code, or None when no carrier claims it.

    The translation aggregators need. Traqo's lookup answers ``ONEY`` and Vizion's ACI
    answers ``ONEY``; both mean the registry's ``one``, and neither should have to hold
    its own copy of that fact — a second table would eventually disagree with this one
    about a carrier whose SCAC changed.

    None is a real answer, not a failure: it means a provider named a carrier Container
    SCM has no adapter for. The carrier is still identified *by its SCAC* and the caller
    can say so; what it cannot do is pretend the box belongs to a registered carrier.
    """
    code = (scac or "").strip().upper()
    if not code:
        return None
    for definition in _get_registry().values():
        if code in definition.scac_codes:
            return definition.provider_code
    return None


def carrier_scac(provider_code: str) -> str:
    """Return the canonical SCAC for a registered carrier, or "" when it has none."""
    definition = get_carrier_definition(provider_code)
    return definition.scac_codes[0] if definition.scac_codes else ""


def suggest_carrier_for_owner_code(owner_code: str) -> str | None:
    """Suggest which carrier to ask for a container, based on its owner prefix.

    This is a hint only. Containers are leased and interchanged, so the prefix is
    not proof of which carrier is actually moving the box; an explicitly chosen
    carrier always wins over this suggestion.
    """
    prefix = (owner_code or "").strip().upper()
    if len(prefix) < 4:
        return None
    for definition in _get_registry().values():
        if prefix[:4] in definition.owner_prefixes:
            return definition.provider_code
    return None


def carrier_supports(provider_code: str, capability_name: str) -> bool:
    """Return True if the carrier supports the named capability.

    Example: carrier_supports("maersk", "supports_webhooks")
    Raises UnknownCarrierError for unregistered codes.
    Raises AttributeError if capability_name is not a valid CarrierCapability field.
    """
    definition = get_carrier_definition(provider_code)
    return bool(getattr(definition.capabilities, capability_name))
