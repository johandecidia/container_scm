from django.urls import path

from . import views

app_name = "shipments"

urlpatterns = [
    path("", views.shipment_list, name="list"),
    path("new/", views.shipment_create, name="create"),
    path("<int:pk>/", views.shipment_detail, name="detail"),
    path("<int:pk>/edit/", views.shipment_update, name="update"),
    path("<int:pk>/delete/", views.shipment_delete, name="delete"),
]
