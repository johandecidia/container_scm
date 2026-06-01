from django.contrib import admin

from .models import Integration, IntegrationCredential, IntegrationRequestLog, IntegrationWebhookEvent


@admin.register(Integration)
class IntegrationAdmin(admin.ModelAdmin):
    list_display = ["name", "provider_code", "provider_family", "api_style", "status", "team", "created_at"]
    list_filter = ["status", "provider_family", "api_style", "team"]
    search_fields = ["name", "provider_code"]
    readonly_fields = [
        "last_tested_at",
        "last_success_at",
        "last_error_at",
        "last_error_message",
        "created_at",
        "updated_at",
    ]


@admin.register(IntegrationCredential)
class IntegrationCredentialAdmin(admin.ModelAdmin):
    list_display = ["integration", "auth_type", "expires_at", "last_refreshed_at", "created_at"]
    list_filter = ["auth_type"]
    search_fields = ["integration__name", "integration__provider_code"]
    readonly_fields = ["encrypted_data", "created_at", "updated_at"]

    def has_add_permission(self, request):
        # Credentials must be created through the credential service, not via admin.
        return False

    def get_fields(self, request, obj=None):
        fields = super().get_fields(request, obj)
        # Always show encrypted_data as read-only — never expose raw values.
        return fields


@admin.register(IntegrationRequestLog)
class IntegrationRequestLogAdmin(admin.ModelAdmin):
    list_display = [
        "provider_code",
        "method",
        "endpoint",
        "status_code",
        "success",
        "duration_ms",
        "team",
        "created_at",
    ]
    list_filter = ["provider_code", "success", "team"]
    search_fields = ["provider_code", "endpoint", "request_id"]
    readonly_fields = [
        "team",
        "integration",
        "provider_code",
        "method",
        "endpoint",
        "status_code",
        "duration_ms",
        "request_id",
        "success",
        "error_message",
        "created_at",
        "updated_at",
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(IntegrationWebhookEvent)
class IntegrationWebhookEventAdmin(admin.ModelAdmin):
    list_display = ["provider_code", "event_type", "status", "team", "created_at"]
    list_filter = ["provider_code", "status", "team"]
    search_fields = ["provider_code", "event_type", "external_event_id"]
    readonly_fields = [
        "team",
        "integration",
        "provider_code",
        "event_type",
        "external_event_id",
        "headers",
        "payload",
        "processed_at",
        "status",
        "error_message",
        "created_at",
        "updated_at",
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        # Allow status updates (e.g. marking failed events for retry) but not payload edits.
        return True

    def get_readonly_fields(self, request, obj=None):
        # Payload and headers are always read-only.
        return self.readonly_fields
