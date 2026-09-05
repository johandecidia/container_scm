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


class ContainerCondition(TextChoices):
    NEW = "NEW", _("New")
    GOOD = "GOOD", _("Good")
    FAIR = "FAIR", _("Fair")
    DAMAGED = "DAMAGED", _("Damaged")


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
    MANUAL = "manual", _("Manual")
    TRACKING_EVENT = "tracking_event", _("Tracking Event")
    SHIPMENT_UPDATE = "shipment_update", _("Shipment Update")
    SUPPLIER_DELIVERY = "supplier_delivery", _("Supplier Delivery")
    IMPORT = "import", _("Import")
    API = "api", _("API")


class MovementType(TextChoices):
    CREATED = "created", _("Created")
    POSITION_UPDATE = "position_update", _("Position Update")
    LOADED_ON_VESSEL = "loaded_on_vessel", _("Loaded on Vessel")
    DISCHARGED_AT_PORT = "discharged_at_port", _("Discharged at Port")
    ARRIVED_AT_DEPOT = "arrived_at_depot", _("Arrived at Depot")
    DEPARTED_DEPOT = "departed_depot", _("Departed Depot")
    MANUAL_ADJUSTMENT = "manual_adjustment", _("Manual Adjustment")
    UNKNOWN = "unknown", _("Unknown")
