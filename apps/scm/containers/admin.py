from django.contrib import admin

from .models import Container, ContainerLocation, ContainerMovement, EquipmentType, LocationAlias


@admin.register(EquipmentType)
class EquipmentTypeAdmin(admin.ModelAdmin):
    list_display = ["iso_code", "category", "length_ft", "high_cube", "description", "is_active"]
    list_filter = ["category", "length_ft", "high_cube", "is_active"]
    search_fields = ["iso_code", "description"]


class LocationAliasInline(admin.TabularInline):
    """External names, edited beside the location they name.

    Inline because an alias has no meaning apart from its location: reviewing "what
    do carriers call this place" is the same task as looking at the place.
    """

    model = LocationAlias
    extra = 0
    fields = ["source", "external_code", "external_name", "latitude", "longitude"]
    readonly_fields = ["normalized_name"]


@admin.register(ContainerLocation)
class ContainerLocationAdmin(admin.ModelAdmin):
    list_display = ["name", "location_type", "unlocode", "parent_location", "country", "city", "team", "is_active"]
    list_filter = ["location_type", "is_active", "team"]
    search_fields = ["name", "city", "country", "unlocode", "external_reference", "owner_name"]
    # normalized_name is derived on save; showing it read-only makes the matching
    # form visible without offering it as something to edit.
    readonly_fields = ["normalized_name", "created_at", "updated_at"]
    autocomplete_fields = ["parent_location"]
    inlines = [LocationAliasInline]


@admin.register(LocationAlias)
class LocationAliasAdmin(admin.ModelAdmin):
    list_display = ["source", "external_name", "external_code", "location", "team"]
    list_filter = ["source", "team"]
    search_fields = ["external_name", "external_code", "location__name"]
    readonly_fields = ["normalized_name", "created_at", "updated_at"]


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
