from django.urls import path

from . import views

app_name = "procurement"

urlpatterns = [
    path("purchase-orders/", views.purchase_order_list, name="purchase_order_list"),
    path("purchase-orders/new/", views.purchase_order_create, name="purchase_order_create"),
    path("purchase-orders/<int:purchase_order_id>/", views.purchase_order_detail, name="purchase_order_detail"),
    path(
        "purchase-orders/<int:purchase_order_id>/delete/",
        views.purchase_order_delete,
        name="purchase_order_delete",
    ),
]
