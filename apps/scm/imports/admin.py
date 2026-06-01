from django.contrib import admin

from .models import ImportError, ImportJob, ImportRow, ImportTemplate


@admin.register(ImportJob)
class ImportJobAdmin(admin.ModelAdmin):
    list_display = ["pk", "status", "import_type", "team", "created_by", "created_at"]
    list_filter = ["status", "import_type"]
    readonly_fields = ["total_rows", "valid_rows", "invalid_rows", "processed_rows", "created_at", "updated_at"]


@admin.register(ImportRow)
class ImportRowAdmin(admin.ModelAdmin):
    list_display = ["pk", "import_job", "row_number", "status"]
    list_filter = ["status"]
    raw_id_fields = ["import_job"]
    readonly_fields = ["raw_data", "mapped_data", "validated_data", "errors"]


@admin.register(ImportError)
class ImportErrorAdmin(admin.ModelAdmin):
    list_display = ["pk", "import_job", "import_row", "code", "severity", "field_name"]
    list_filter = ["severity"]
    raw_id_fields = ["import_job", "import_row"]


@admin.register(ImportTemplate)
class ImportTemplateAdmin(admin.ModelAdmin):
    list_display = ["name", "import_type", "team", "is_default"]
    list_filter = ["import_type", "is_default"]
