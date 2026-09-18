from django.urls import path

from . import link_views, views

app_name = "procurement"

urlpatterns = [
    path("purchase-orders/", views.purchase_order_list, name="purchase_order_list"),
    path("purchase-orders/new/", views.purchase_order_create, name="purchase_order_create"),
    # Order lines and container links are keyed by the line, not by the order: a line
    # id identifies its order, and repeating the order in the path would let the two
    # disagree. The literal `lines/`, `acquisitions/` and `loads/` segments cannot
    # collide with `<int:purchase_order_id>`.
    path(
        "purchase-orders/lines/<int:line_id>/edit/",
        views.purchase_order_line_update,
        name="purchase_order_line_update",
    ),
    path(
        "purchase-orders/lines/<int:line_id>/delete/",
        views.purchase_order_line_delete,
        name="purchase_order_line_delete",
    ),
    path(
        "purchase-orders/lines/<int:line_id>/acquired-container/",
        link_views.line_link_acquired_container,
        name="line_link_acquired_container",
    ),
    path(
        "purchase-orders/lines/<int:line_id>/load/",
        link_views.line_add_container_load,
        name="line_add_container_load",
    ),
    path(
        "purchase-orders/acquisitions/<int:acquisition_id>/unlink/",
        link_views.acquisition_unlink,
        name="acquisition_unlink",
    ),
    path(
        "purchase-orders/loads/<int:load_id>/remove/",
        link_views.container_load_remove,
        name="container_load_remove",
    ),
    path("purchase-orders/<int:purchase_order_id>/", views.purchase_order_detail, name="purchase_order_detail"),
    path("purchase-orders/<int:purchase_order_id>/edit/", views.purchase_order_update, name="purchase_order_update"),
    path(
        "purchase-orders/<int:purchase_order_id>/lines/new/",
        views.purchase_order_line_create,
        name="purchase_order_line_create",
    ),
    path(
        "purchase-orders/<int:purchase_order_id>/delete/",
        views.purchase_order_delete,
        name="purchase_order_delete",
    ),
]
