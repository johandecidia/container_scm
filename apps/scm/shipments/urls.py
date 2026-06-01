from django.urls import path

from . import views

app_name = "shipments"

urlpatterns = [
    path("", views.shipment_list, name="list"),
    path("new/", views.shipment_create, name="create"),
    path("<int:pk>/", views.shipment_detail, name="detail"),
    path("<int:pk>/edit/", views.shipment_update, name="update"),
    path("<int:pk>/cancel/", views.shipment_cancel, name="cancel"),
    path("<int:pk>/status/", views.shipment_status_update, name="status_update"),
    path("<int:pk>/containers/add/", views.shipment_container_add, name="container_add"),
    path("<int:pk>/containers/<int:sc_pk>/remove/", views.shipment_container_remove, name="container_remove"),
    path("<int:pk>/timeline/", views.shipment_timeline_partial, name="timeline"),
]
