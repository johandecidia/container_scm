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
