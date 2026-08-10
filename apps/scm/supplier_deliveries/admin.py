from django.contrib import admin

from .models import SupplierDelivery, SupplierDeliveryLine


class SupplierDeliveryLineInline(admin.TabularInline):
    model = SupplierDeliveryLine
    extra = 0
    fields = ("purchase_order_line", "article", "delivery_qty", "unit", "container")


@admin.register(SupplierDelivery)
class SupplierDeliveryAdmin(admin.ModelAdmin):
    list_display = (
        "delivery_reference",
        "purchase_order",
        "supplier",
        "status",
        "planned_ship_date",
        "planned_arrival_date",
        "actual_ship_date",
        "actual_arrival_date",
        "team",
    )
    list_filter = ("status", "team")
    search_fields = ("delivery_reference", "supplier", "purchase_order__po_number")
    readonly_fields = ("created_at", "updated_at")
    inlines = [SupplierDeliveryLineInline]


@admin.register(SupplierDeliveryLine)
class SupplierDeliveryLineAdmin(admin.ModelAdmin):
    list_display = ("delivery", "purchase_order_line", "article", "delivery_qty", "unit", "container")
    list_filter = ("delivery__team",)
    search_fields = ("article", "delivery__delivery_reference")
    readonly_fields = ("created_at", "updated_at")
