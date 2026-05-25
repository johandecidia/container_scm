from django.contrib import admin

from .models import Shipment


@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    list_display = ["reference", "status", "origin", "destination", "estimated_arrival", "team", "created_at"]
    list_filter = ["status"]
    search_fields = ["reference", "origin", "destination"]
