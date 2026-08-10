from django.urls import path

from . import views

app_name = "supplier_deliveries"

urlpatterns = [
    path("", views.supplier_delivery_dashboard, name="dashboard"),
    path("deliveries/", views.supplier_delivery_list, name="list"),
    path("deliveries/create/", views.supplier_delivery_create, name="create"),
    path("deliveries/<int:delivery_id>/", views.supplier_delivery_detail, name="detail"),
    path("deliveries/<int:delivery_id>/update/", views.supplier_delivery_update, name="update"),
    path(
        "deliveries/<int:delivery_id>/mark-received/",
        views.supplier_delivery_mark_received,
        name="mark_received",
    ),
]
