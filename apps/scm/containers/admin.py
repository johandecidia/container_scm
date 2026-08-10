from django.contrib import admin

from .models import Container, ContainerLocation, ContainerMovement, EquipmentType


@admin.register(EquipmentType)
class EquipmentTypeAdmin(admin.ModelAdmin):
    list_display = ["iso_code", "category", "length_ft", "high_cube", "description", "is_active"]
    list_filter = ["category", "length_ft", "high_cube", "is_active"]
    search_fields = ["iso_code", "description"]


@admin.register(ContainerLocation)
class ContainerLocationAdmin(admin.ModelAdmin):
    list_display = ["name", "location_type", "country", "city", "team", "is_active"]
    list_filter = ["location_type", "is_active", "team"]
    search_fields = ["name", "city", "country", "external_reference", "owner_name"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(Container)
class ContainerAdmin(admin.ModelAdmin):
    list_display = ["container_id", "team", "equipment_type", "status", "condition", "current_location"]
    list_filter = ["status", "condition", "equipment_type", "team"]
    search_fields = [
        "owner_code",
        "serial_number",
        "manufacturer",
        "manufacturer_id",
        "location_text",
        "current_location__name",
    ]
    readonly_fields = ["created_at", "updated_at", "created_by", "updated_by"]


@admin.register(ContainerMovement)
class ContainerMovementAdmin(admin.ModelAdmin):
    list_display = ["container", "movement_type", "from_location", "to_location", "occurred_at", "source", "team"]
    list_filter = ["movement_type", "source", "team"]
    search_fields = ["container__owner_code", "container__serial_number", "notes"]
    readonly_fields = ["created_at", "updated_at"]
