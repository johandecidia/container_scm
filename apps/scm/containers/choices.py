from enum import IntEnum

from django.db.models import TextChoices
from django.utils.translation import gettext_lazy as _


class EquipmentCategory(TextChoices):
    GP = "GP", _("General Purpose")
    RF = "RF", _("Reefer")
    OT = "OT", _("Open Top")
    FR = "FR", _("Flat Rack")
    PL = "PL", _("Platform")
    TK = "TK", _("Tank")
    BK = "BK", _("Bulk")
    VH = "VH", _("Vehicle")
    HH = "HH", _("High & Heavy")


class ContainerCategory(TextChoices):
    U = "U", _("Freight Container (U)")
    A = "A", _("Freight Container (A)")
    J = "J", _("Detachable Freight Container Unit (J)")
    Z = "Z", _("Trailer and Chassis (Z)")


class ContainerStatus(TextChoices):
    AVAILABLE = "AVAILABLE", _("Available")
    BOOKED = "BOOKED", _("Booked")
    IN_TRANSIT = "IN_TRANSIT", _("In Transit")
    REPAIR = "REPAIR", _("Under Repair")
    DECOMMISSIONED = "DECOMMISSIONED", _("Decommissioned")


# There is deliberately no ContainerCondition enum here. Conditions are a team's own
# master data — a row in `apps.scm.containers.models.ContainerCondition`, editable
# from Settings — because teams do not share one grading vocabulary and a shipped
# enum made every disagreement a code change. The set a new team starts with lives in
# `apps.scm.containers.conditions`.


class ColorSystem(TextChoices):
    RAL = "RAL", _("RAL")
    NCS = "NCS", _("NCS")
    PANTONE = "PANTONE", _("Pantone")
    CUSTOM = "CUSTOM", _("Custom")
    UNKNOWN = "UNKNOWN", _("Unknown")


class LocationType(TextChoices):
    """What kind of place a canonical location is.

    The first six values predate the canonical identity layer and are kept because
    existing rows use them: renaming a stored value would silently reclassify every
    location a team has already recorded. ``TERMINAL``, ``WAREHOUSE``, ``FACTORY``
    and ``OTHER`` are the additions LOC-1 needs to describe a port's interior.

    ``SUPPLIER_WAREHOUSE`` and ``WAREHOUSE`` therefore coexist on purpose — the
    first says whose warehouse it is, the second only that it is one. Neither is a
    synonym the other should absorb.

    There is deliberately no GATE. A gate is a point a container passes through, not
    a place it is at, and the events that would need one belong to LOC-2.
    """

    MANUFACTURER = "manufacturer", _("Manufacturer")
    SUPPLIER_WAREHOUSE = "supplier_warehouse", _("Supplier Warehouse")
    PORT = "port", _("Port")
    VESSEL = "vessel", _("Vessel")
    DEPOT = "depot", _("Depot")
    CUSTOMER = "customer", _("Customer")
    TERMINAL = "terminal", _("Terminal")
    WAREHOUSE = "warehouse", _("Warehouse")
    FACTORY = "factory", _("Factory")
    OTHER = "other", _("Other")
    UNKNOWN = "unknown", _("Unknown")


class LocationAliasSource:
    """Who called a place by an external name.

    Not a ``TextChoices``: most values are ``TrackingProvider.code`` — rows in the
    database, added when a carrier or aggregator is configured — so a closed enum
    here would have to be edited every time a provider is onboarded, and would go
    stale the moment one was not.

    The two constants are the sources that are *not* providers, and are reserved so
    they cannot collide with a provider code:

    ``UNLOCODE``
        The UN/LOCODE register itself, for recording that a code names this place.
    ``INTERNAL``
        A name MCR uses in-house that no provider ever sends.
    """

    UNLOCODE = "unlocode"
    INTERNAL = "internal"

    RESERVED = (UNLOCODE, INTERNAL)


class LocationResolutionStatus(TextChoices):
    """Whether an external location could be tied to a canonical one.

    ``AMBIGUOUS`` is a first-class answer, not a variety of failure: it says the
    evidence matched several canonical locations and the resolver refused to pick.
    Collapsing it into ``UNRESOLVED`` would lose the one fact an operator can act
    on — that an alias needs to be recorded to break the tie.
    """

    RESOLVED = "resolved", _("Resolved")
    UNRESOLVED = "unresolved", _("Unresolved")
    AMBIGUOUS = "ambiguous", _("Ambiguous")


class LocationResolutionMethod(TextChoices):
    """Which rule produced a resolution.

    Recorded rather than a confidence score. An explicit alias and a coordinate
    match within five kilometres are both "the location", but they are believable
    for entirely different reasons, and a number in between would invent a precision
    the domain does not have.
    """

    ALIAS = "alias", _("Explicit alias")
    EXTERNAL_CODE = "external_code", _("Provider code")
    UNLOCODE = "unlocode", _("UN/LOCODE")
    COORDINATES = "coordinates", _("Coordinates")
    NAME = "name", _("Normalised name")
    NONE = "none", _("None")


class LocationSource(TextChoices):
    """Who claims a container is where a movement says it is.

    The value is provenance, not confidence: it says who made the claim, and
    ``apps.scm.containers.movements`` turns that into the precedence that decides
    which claim becomes ``Container.current_location``.

    ``DEPOT`` is separate from ``MANUAL`` because they are different claims. A
    manual movement is an operator typing what they saw; a depot movement is a yard
    system reporting a receipt it handled. Both are direct observations of the box —
    they rank together — but merging them would lose which one to ask about a
    disputed row.
    """

    MANUAL = "manual", _("Manual")
    DEPOT = "depot", _("Depot")
    TRACKING_EVENT = "tracking_event", _("Tracking Event")
    SHIPMENT_UPDATE = "shipment_update", _("Shipment Update")
    SUPPLIER_DELIVERY = "supplier_delivery", _("Supplier Delivery")
    IMPORT = "import", _("Import")
    API = "api", _("API")


class EvidenceStrength(IntEnum):
    """How direct a claim about a container's position is.

    Three ranks, not a score. A number in between would invent a precision the
    domain does not have — the difference between an operator who saw the box and a
    carrier who inferred it from a vessel manifest is a difference in *kind*.

    ``OBSERVED``
        Somebody physically handled the container: an operator recording a gate
        move, a depot reporting a receipt, a supplier delivery being signed for.
    ``RECORDED``
        One of MCR's own systems asserting a position without anybody having seen
        the box — an import file, an API write, a shipment update.
    ``INFERRED``
        A carrier's report, interpreted by us into a movement. Real evidence, and
        the weakest thing here.

    Used only to break ties at an identical ``occurred_at``. Time leads; see
    ``apps.scm.containers.movements``.
    """

    INFERRED = 0
    RECORDED = 1
    OBSERVED = 2


# The rank of each provenance. An unrecognised or blank source — a legacy row, or a
# value written before this table existed — is RECORDED: it came from one of our own
# systems, which is neither an observation nor a carrier's guess.
EVIDENCE_STRENGTH_BY_SOURCE: dict[str, EvidenceStrength] = {
    LocationSource.MANUAL: EvidenceStrength.OBSERVED,
    LocationSource.DEPOT: EvidenceStrength.OBSERVED,
    LocationSource.SUPPLIER_DELIVERY: EvidenceStrength.OBSERVED,
    LocationSource.IMPORT: EvidenceStrength.RECORDED,
    LocationSource.SHIPMENT_UPDATE: EvidenceStrength.RECORDED,
    LocationSource.API: EvidenceStrength.RECORDED,
    LocationSource.TRACKING_EVENT: EvidenceStrength.INFERRED,
}

DEFAULT_EVIDENCE_STRENGTH = EvidenceStrength.RECORDED

OBSERVED_LOCATION_SOURCES = frozenset(
    source for source, strength in EVIDENCE_STRENGTH_BY_SOURCE.items() if strength == EvidenceStrength.OBSERVED
)


def evidence_strength(source: str) -> EvidenceStrength:
    """Return how direct a claim made by *source* is."""
    return EVIDENCE_STRENGTH_BY_SOURCE.get(source, DEFAULT_EVIDENCE_STRENGTH)


class MovementType(TextChoices):
    """What kind of physical move a :class:`ContainerMovement` records.

    The first eight values predate LOC-2 and are kept because rows use them:
    changing a stored value would silently reclassify history. ``POSITION_UPDATE``
    remains the honest label for "the location changed and nobody said how".

    ``GATE_IN``, ``GATE_OUT``, ``RECEIVED`` and ``TRANSFER`` are LOC-2's additions
    and are the four an operator actually performs. Their from/to semantics are
    defined once, in ``apps.scm.containers.movements``, and nothing else may
    restate them.

    **There are deliberately no carrier event names here.** ``VESSEL_ARRIVED``,
    ``BOOKED`` and the rest are :class:`~apps.scm.tracking.models.TrackingEvent`
    types — external evidence about a journey. A movement is an *accepted* physical
    change of position, and the two must not become the same vocabulary, because
    then the difference between what a carrier said and what we accepted would stop
    being visible. ``LOADED_ON_VESSEL`` and ``DISCHARGED_AT_PORT`` are the surviving
    exceptions, kept only because existing rows carry them.
    """

    # LOC-2 operational movements.
    GATE_IN = "gate_in", _("Gate In")
    GATE_OUT = "gate_out", _("Gate Out")
    RECEIVED = "received", _("Received")
    TRANSFER = "transfer", _("Transfer")

    # Pre-existing values, kept for the rows that already use them.
    CREATED = "created", _("Created")
    POSITION_UPDATE = "position_update", _("Position Update")
    LOADED_ON_VESSEL = "loaded_on_vessel", _("Loaded on Vessel")
    DISCHARGED_AT_PORT = "discharged_at_port", _("Discharged at Port")
    ARRIVED_AT_DEPOT = "arrived_at_depot", _("Arrived at Depot")
    DEPARTED_DEPOT = "departed_depot", _("Departed Depot")
    MANUAL_ADJUSTMENT = "manual_adjustment", _("Manual Adjustment")
    UNKNOWN = "unknown", _("Unknown")
