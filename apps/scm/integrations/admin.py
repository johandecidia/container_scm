from django.contrib import admin

from .models import Integration


@admin.register(Integration)
class IntegrationAdmin(admin.ModelAdmin):
    list_display = ["name", "provider_code", "provider_family", "status", "team", "created_at"]
    list_filter = ["status", "provider_family", "team"]
    search_fields = ["name", "provider_code"]
