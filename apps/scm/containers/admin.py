from django.contrib import admin

from .models import Container, EquipmentType


@admin.register(EquipmentType)
class EquipmentTypeAdmin(admin.ModelAdmin):
    list_display = ["iso_code", "category", "length_ft", "high_cube", "description", "is_active"]
    list_filter = ["category", "length_ft", "high_cube", "is_active"]
    search_fields = ["iso_code", "description"]


@admin.register(Container)
class ContainerAdmin(admin.ModelAdmin):
    list_display = ["container_id", "team", "equipment_type", "status", "condition", "current_location"]
    list_filter = ["status", "condition", "equipment_type", "team"]
    search_fields = [
        "owner_code",
        "serial_number",
        "manufacturer",
        "manufacturer_id",
        "current_location",
    ]
    readonly_fields = ["created_at", "updated_at", "created_by", "updated_by"]
