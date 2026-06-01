from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.teams.models import BaseTeamModel


class Integration(BaseTeamModel):
    """An external system integration configured for a team."""

    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        INACTIVE = "inactive", _("Inactive")
        ERROR = "error", _("Error")
        PENDING = "pending", _("Pending")

    class ProviderFamily(models.TextChoices):
        CARRIER = "carrier", _("Carrier")
        BUSINESS_SYSTEM = "business_system", _("Business System")
        FINANCE = "finance", _("Finance")
        OTHER = "other", _("Other")

    class ApiStyle(models.TextChoices):
        DCSA = "dcsa", _("DCSA")
        PROPRIETARY = "proprietary", _("Proprietary")
        EDI = "edi", _("EDI")
        AGGREGATOR = "aggregator", _("Aggregator")
        PORTAL = "portal", _("Portal")
        UNKNOWN = "unknown", _("Unknown")

    name = models.CharField(_("name"), max_length=100)
    provider_code = models.CharField(_("provider code"), max_length=100)
    provider_family = models.CharField(
        _("provider family"),
        max_length=20,
        choices=ProviderFamily.choices,
        default=ProviderFamily.CARRIER,
    )
    api_style = models.CharField(
        _("API style"),
        max_length=20,
        choices=ApiStyle.choices,
        default=ApiStyle.UNKNOWN,
    )
    status = models.CharField(_("status"), max_length=20, choices=Status.choices, default=Status.INACTIVE)
    config = models.JSONField(_("config"), default=dict, blank=True)

    # Health tracking
    last_tested_at = models.DateTimeField(_("last tested at"), null=True, blank=True)
    last_success_at = models.DateTimeField(_("last success at"), null=True, blank=True)
    last_error_at = models.DateTimeField(_("last error at"), null=True, blank=True)
    last_error_message = models.TextField(_("last error message"), blank=True)
    is_active = models.BooleanField(_("is active"), default=True)

    class Meta:
        ordering = ["name"]
        unique_together = [["team", "provider_code"]]

    def __str__(self):
        return f"{self.name} ({self.provider_code})"


class IntegrationCredential(BaseTeamModel):
    """Stores (encrypted) credentials for an integration.

    Never store raw secrets in plaintext — always go through the credential service.
    """

    class AuthType(models.TextChoices):
        API_KEY = "api_key", _("API Key")
        OAUTH2 = "oauth2", _("OAuth 2.0")
        BASIC = "basic", _("Basic Auth")
        BEARER = "bearer", _("Bearer Token")
        CERTIFICATE = "certificate", _("Certificate")
        CUSTOM = "custom", _("Custom")

    integration = models.OneToOneField(
        Integration,
        on_delete=models.CASCADE,
        related_name="credential",
        verbose_name=_("integration"),
    )
    auth_type = models.CharField(_("auth type"), max_length=20, choices=AuthType.choices)
    # Encrypted credential data — use the credential service to read/write.
    encrypted_data = models.TextField(_("encrypted data"), blank=True)
    expires_at = models.DateTimeField(_("expires at"), null=True, blank=True)
    last_refreshed_at = models.DateTimeField(_("last refreshed at"), null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Credential for {self.integration} ({self.auth_type})"


class IntegrationRequestLog(BaseTeamModel):
    """Logs outbound API requests made on behalf of an integration.

    Never store tokens, secrets, or full auth headers here.
    """

    integration = models.ForeignKey(
        Integration,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="request_logs",
        verbose_name=_("integration"),
    )
    provider_code = models.CharField(_("provider code"), max_length=100)
    method = models.CharField(_("HTTP method"), max_length=10, default="GET")
    endpoint = models.CharField(_("endpoint"), max_length=500)
    status_code = models.PositiveSmallIntegerField(_("status code"), null=True, blank=True)
    duration_ms = models.PositiveIntegerField(_("duration (ms)"), null=True, blank=True)
    request_id = models.CharField(_("request ID"), max_length=200, blank=True)
    success = models.BooleanField(_("success"), default=False)
    error_message = models.TextField(_("error message"), blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["team", "provider_code"]),
            models.Index(fields=["team", "integration"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"{self.method} {self.endpoint} → {self.status_code}"


class IntegrationWebhookEvent(BaseTeamModel):
    """Stores raw inbound webhook payloads from carrier or business system integrations."""

    class Status(models.TextChoices):
        RECEIVED = "received", _("Received")
        PROCESSING = "processing", _("Processing")
        PROCESSED = "processed", _("Processed")
        FAILED = "failed", _("Failed")
        IGNORED = "ignored", _("Ignored")

    integration = models.ForeignKey(
        Integration,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="webhook_events",
        verbose_name=_("integration"),
    )
    provider_code = models.CharField(_("provider code"), max_length=100)
    event_type = models.CharField(_("event type"), max_length=200, blank=True)
    external_event_id = models.CharField(_("external event ID"), max_length=200, blank=True)
    headers = models.JSONField(_("headers"), default=dict, blank=True)
    payload = models.JSONField(_("payload"), default=dict)
    processed_at = models.DateTimeField(_("processed at"), null=True, blank=True)
    status = models.CharField(_("status"), max_length=20, choices=Status.choices, default=Status.RECEIVED)
    error_message = models.TextField(_("error message"), blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["team", "provider_code"]),
            models.Index(fields=["status"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"WebhookEvent({self.provider_code}, {self.status}, {self.created_at})"
