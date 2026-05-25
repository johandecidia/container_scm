from django.contrib import admin

from .models import ImportJob


@admin.register(ImportJob)
class ImportJobAdmin(admin.ModelAdmin):
    list_display = ["pk", "status", "submitted_by", "rows_processed", "team", "created_at"]
    list_filter = ["status"]
    readonly_fields = ["error_message", "rows_processed"]
