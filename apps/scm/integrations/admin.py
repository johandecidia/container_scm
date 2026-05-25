from django.contrib import admin

from .models import Integration


@admin.register(Integration)
class IntegrationAdmin(admin.ModelAdmin):
    list_display = ["name", "provider", "status", "team", "created_at"]
    list_filter = ["status", "provider"]
    search_fields = ["name", "provider"]
