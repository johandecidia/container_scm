from django.contrib import admin

from .models import Rate


@admin.register(Rate)
class RateAdmin(admin.ModelAdmin):
    list_display = ["origin", "destination", "carrier", "amount", "currency", "valid_from", "valid_to", "team"]
    list_filter = ["currency", "carrier"]
    search_fields = ["origin", "destination", "carrier"]
