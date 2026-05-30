from django.contrib import admin

from .models import Shipment, ShipmentContainer, ShipmentEvent


@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    list_display = ["__str__", "status", "origin_port", "destination_port", "eta", "team", "created_at"]
    list_filter = ["status", "team"]
    search_fields = ["shipment_number", "reference", "customer_name", "carrier", "origin_port", "destination_port"]
    readonly_fields = ["created_at", "updated_at", "created_by", "last_tracking_sync_at"]


@admin.register(ShipmentContainer)
class ShipmentContainerAdmin(admin.ModelAdmin):
    list_display = ["shipment", "container", "sequence", "seal_number", "created_at"]
    list_filter = ["shipment__team"]
    search_fields = ["shipment__shipment_number", "container__owner_code", "seal_number"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(ShipmentEvent)
class ShipmentEventAdmin(admin.ModelAdmin):
    list_display = ["event_type", "shipment", "description", "occurred_at", "created_by"]
    list_filter = ["event_type"]
    search_fields = ["shipment__shipment_number", "description"]
    readonly_fields = ["created_at", "updated_at"]
