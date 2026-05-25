from django.contrib import admin

from .models import Container


@admin.register(Container)
class ContainerAdmin(admin.ModelAdmin):
    list_display = ["container_number", "size", "status", "team", "created_at"]
    list_filter = ["status", "size"]
    search_fields = ["container_number"]
