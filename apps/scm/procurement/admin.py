from django.contrib import admin

from .models import PurchaseOrder, PurchaseOrderEvent, PurchaseOrderLine


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


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = ("po_number", "supplier_name", "status", "order_date", "expected_receipt_date", "team")
    list_filter = ("status", "team")
    search_fields = ("po_number", "supplier_name", "supplier_no")
    readonly_fields = ("external_id", "created_at", "updated_at")
    inlines = [PurchaseOrderLineInline, PurchaseOrderEventInline]


@admin.register(PurchaseOrderLine)
class PurchaseOrderLineAdmin(admin.ModelAdmin):
    list_display = ("purchase_order", "line_no", "item_no", "description", "ordered_qty", "shipped_qty", "received_qty")
    list_filter = ("purchase_order__team",)
    search_fields = ("item_no", "description", "purchase_order__po_number")
    readonly_fields = ("external_id", "created_at", "updated_at")


@admin.register(PurchaseOrderEvent)
class PurchaseOrderEventAdmin(admin.ModelAdmin):
    list_display = ("purchase_order", "event_type", "timestamp")
    list_filter = ("event_type",)
    readonly_fields = ("timestamp",)
