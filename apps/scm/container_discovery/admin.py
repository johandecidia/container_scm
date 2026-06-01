from django.contrib import admin

from .models import ContainerDiscoveryEvent, ContainerPool


class ContainerDiscoveryEventInline(admin.TabularInline):
    model = ContainerDiscoveryEvent
    extra = 0
    fields = ("event_type", "carrier_code", "carrier_name", "detected_at", "created_at")
    readonly_fields = ("created_at",)


@admin.register(ContainerPool)
class ContainerPoolAdmin(admin.ModelAdmin):
    list_display = ("container_number", "status", "team", "created_at", "updated_at")
    list_filter = ("status", "team")
    search_fields = ("container_number", "notes")
    readonly_fields = ("created_at", "updated_at")
    inlines = [ContainerDiscoveryEventInline]


@admin.register(ContainerDiscoveryEvent)
class ContainerDiscoveryEventAdmin(admin.ModelAdmin):
    list_display = (
        "container_number",
        "event_type",
        "carrier_code",
        "carrier_name",
        "detected_at",
        "team",
        "created_at",
    )
    list_filter = ("event_type", "team")
    search_fields = ("container_number", "carrier_code", "carrier_name")
    readonly_fields = ("created_at", "updated_at")
