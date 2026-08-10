from django.contrib import admin

from .models import SCMAuditLog


@admin.register(SCMAuditLog)
class SCMAuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "team", "action", "object_type", "object_repr", "actor")
    list_filter = ("action", "team")
    search_fields = ("object_repr", "object_id", "actor__email")
    ordering = ("-created_at",)
    readonly_fields = (
        "team",
        "actor",
        "action",
        "object_type",
        "object_id",
        "object_repr",
        "metadata",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
