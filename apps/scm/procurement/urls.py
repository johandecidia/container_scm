from django.urls import path

from . import views

app_name = "procurement"

urlpatterns = [
    path("purchase-orders/", views.purchase_order_list, name="purchase_order_list"),
    path("purchase-orders/<int:purchase_order_id>/", views.purchase_order_detail, name="purchase_order_detail"),
]
