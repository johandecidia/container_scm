from django.contrib import admin

from .models import TrackingEvent, TrackingProvider, TrackingRawPayload, TrackingSubscription, TrackingSyncRun


@admin.register(TrackingProvider)
class TrackingProviderAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "provider_type", "is_active", "created_at"]
    list_filter = ["provider_type", "is_active"]
    search_fields = ["name", "code"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(TrackingSubscription)
class TrackingSubscriptionAdmin(admin.ModelAdmin):
    list_display = [
        "tracking_reference",
        "reference_type",
        "provider",
        "status",
        "team",
        "last_synced_at",
        "consecutive_failures",
        "created_at",
    ]
    list_filter = ["status", "reference_type", "provider", "team"]
    search_fields = ["tracking_reference"]
    readonly_fields = ["created_at", "updated_at", "last_synced_at", "last_error_at"]
    date_hierarchy = "created_at"


@admin.register(TrackingEvent)
class TrackingEventAdmin(admin.ModelAdmin):
    list_display = [
        "event_type",
        "location_name",
        "event_datetime",
        "provider",
        "subscription",
        "team",
        "source_event_id",
        "created_at",
    ]
    list_filter = ["event_type", "provider", "team"]
    search_fields = ["source_event_id", "description", "location_name", "location_unlocode"]
    readonly_fields = ["created_at", "updated_at", "raw_data"]
    date_hierarchy = "event_datetime"


@admin.register(TrackingRawPayload)
class TrackingRawPayloadAdmin(admin.ModelAdmin):
    list_display = ["provider", "subscription", "payload_type", "received_at", "parsed_successfully", "team"]
    list_filter = ["payload_type", "parsed_successfully", "provider", "team"]
    search_fields = ["payload_hash"]
    readonly_fields = ["created_at", "updated_at", "payload_json", "payload_hash", "received_at"]
    date_hierarchy = "received_at"


@admin.register(TrackingSyncRun)
class TrackingSyncRunAdmin(admin.ModelAdmin):
    list_display = [
        "subscription",
        "provider",
        "status",
        "started_at",
        "finished_at",
        "events_created",
        "events_updated",
        "team",
    ]
    list_filter = ["status", "provider", "team"]
    search_fields = ["subscription__tracking_reference"]
    readonly_fields = ["created_at", "updated_at", "started_at", "finished_at"]
    date_hierarchy = "started_at"
