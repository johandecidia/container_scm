from django.contrib import admin, messages
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path

from .models import PurchaseOrder, PurchaseOrderEvent, PurchaseOrderLine


def _dummy_sync_for_team(team):
    """Run a dummy BC purchase-order sync for a team, ensuring a BC integration exists.

    Uses a get-or-create Business Central integration (so the sync-run tracking
    and validation path is exercised) with a dummy client — no live credentials.
    Returns the IntegrationSyncRun.
    """
    from apps.scm.integrations.business_systems.business_central.client import BusinessCentralClient
    from apps.scm.integrations.business_systems.business_central.sync import (
        sync_purchase_orders_from_business_central,
    )
    from apps.scm.integrations.models import Integration

    integration, _ = Integration.objects.get_or_create(
        team=team,
        provider_code="business_central",
        defaults={
            "name": "Business Central",
            "provider_family": Integration.ProviderFamily.BUSINESS_SYSTEM,
            "status": Integration.Status.ACTIVE,
            "is_active": True,
            "config": {"sync_enabled": True},
        },
    )
    client = BusinessCentralClient(use_dummy=True)
    return sync_purchase_orders_from_business_central(integration, client=client, trigger_type="manual")


@admin.action(description="Sync from Business Central (dummy data)")
def sync_from_bc_dummy(modeladmin, request, queryset):
    """Admin action: sync BC purchase orders for teams of selected rows."""
    teams = {po.team for po in queryset.select_related("team")}
    if not teams:
        modeladmin.message_user(request, "Inga rader valda.", messages.WARNING)
        return

    for team in teams:
        run = _dummy_sync_for_team(team)
        modeladmin.message_user(
            request,
            f"Team '{team.slug}': synkade {run.records_created + run.records_updated} purchase orders "
            f"({run.records_unchanged} oförändrade).",
            messages.SUCCESS,
        )


class PurchaseOrderLineInline(admin.TabularInline):
    model = PurchaseOrderLine
    extra = 0
    fields = ("line_no", "item_no", "description", "ordered_qty", "shipped_qty", "received_qty")
    readonly_fields = ("line_no", "item_no", "description", "ordered_qty", "shipped_qty", "received_qty")


class PurchaseOrderEventInline(admin.TabularInline):
    model = PurchaseOrderEvent
    extra = 0
    fields = ("event_type", "timestamp", "description")
    readonly_fields = ("event_type", "timestamp", "description")


_BC_PO_READONLY = (
    "po_number",
    "supplier_no",
    "supplier_name",
    "status",
    "order_date",
    "expected_receipt_date",
    "currency",
    "source_system",
    "source_company_id",
    "source_last_modified_at",
    "last_synced_at",
    "raw_payload",
    "sync_hash",
    "source_active",
    "source_deleted_at",
)


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = ("po_number", "supplier_name", "status", "source_system", "source_active", "order_date", "team")
    list_filter = ("status", "source_system", "source_active", "team")
    search_fields = ("po_number", "supplier_name", "supplier_no")
    readonly_fields = ("external_id", "created_at", "updated_at")
    inlines = [PurchaseOrderLineInline, PurchaseOrderEventInline]
    actions = [sync_from_bc_dummy]
    change_list_template = "admin/scm_procurement/purchaseorder/change_list.html"

    def get_readonly_fields(self, request, obj=None):
        # Business Central is master — its source fields are never editable here.
        if obj is not None and obj.is_business_central:
            return tuple(self.readonly_fields) + _BC_PO_READONLY
        return self.readonly_fields

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.is_business_central:
            return False
        return super().has_delete_permission(request, obj)

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "sync-bc-dummy/",
                self.admin_site.admin_view(self._sync_bc_dummy_view),
                name="scm_procurement_purchaseorder_sync_bc_dummy",
            ),
        ]
        return custom + urls

    def _sync_bc_dummy_view(self, request):
        from apps.teams.models import Team

        if request.method == "POST":
            team_id = request.POST.get("team_id")
            if team_id:
                try:
                    team = Team.objects.get(pk=team_id)
                except Team.DoesNotExist:
                    self.message_user(request, "Team hittades inte.", messages.ERROR)
                    return redirect(".")
                run = _dummy_sync_for_team(team)
                self.message_user(
                    request,
                    f"Team '{team.slug}': synkade {run.records_created + run.records_updated} purchase orders "
                    f"({run.records_unchanged} oförändrade).",
                    messages.SUCCESS,
                )
            return redirect("../")

        context = {
            **self.admin_site.each_context(request),
            "teams": Team.objects.all().order_by("name"),
            "opts": self.model._meta,
            "title": "Sync från Business Central (dummy)",
        }
        return TemplateResponse(
            request,
            "admin/scm_procurement/purchaseorder/sync_bc_dummy.html",
            context,
        )


_BC_LINE_READONLY = (
    "line_no",
    "item_no",
    "description",
    "ordered_qty",
    "shipped_qty",
    "received_qty",
    "unit_price",
    "expected_receipt_date",
    "source_last_modified_at",
    "last_synced_at",
    "raw_payload",
    "sync_hash",
    "source_active",
    "source_deleted_at",
)


@admin.register(PurchaseOrderLine)
class PurchaseOrderLineAdmin(admin.ModelAdmin):
    list_display = ("purchase_order", "line_no", "item_no", "description", "ordered_qty", "shipped_qty", "received_qty")
    list_filter = ("purchase_order__team",)
    search_fields = ("item_no", "description", "purchase_order__po_number")
    readonly_fields = ("external_id", "created_at", "updated_at")

    def get_readonly_fields(self, request, obj=None):
        if obj is not None and obj.purchase_order.is_business_central:
            return tuple(self.readonly_fields) + _BC_LINE_READONLY
        return self.readonly_fields

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.purchase_order.is_business_central:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(PurchaseOrderEvent)
class PurchaseOrderEventAdmin(admin.ModelAdmin):
    list_display = ("purchase_order", "event_type", "timestamp")
    list_filter = ("event_type",)
    readonly_fields = ("timestamp",)
