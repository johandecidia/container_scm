from django.contrib import admin

from .models import AnalyticsSnapshot, SavedFilter


@admin.register(AnalyticsSnapshot)
class AnalyticsSnapshotAdmin(admin.ModelAdmin):
    list_display = ["team", "date", "total_shipments", "active_shipments", "avg_transit_days"]
    list_filter = ["team"]
    ordering = ["-date"]


@admin.register(SavedFilter)
class SavedFilterAdmin(admin.ModelAdmin):
    list_display = ["name", "view_key", "user", "team", "created_at"]
    list_filter = ["view_key", "team"]
