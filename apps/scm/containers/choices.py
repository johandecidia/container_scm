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
    MANUFACTURER = "manufacturer", _("Manufacturer")
    SUPPLIER_WAREHOUSE = "supplier_warehouse", _("Supplier Warehouse")
    PORT = "port", _("Port")
    VESSEL = "vessel", _("Vessel")
    DEPOT = "depot", _("Depot")
    CUSTOMER = "customer", _("Customer")
    UNKNOWN = "unknown", _("Unknown")


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
