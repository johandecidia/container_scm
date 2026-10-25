from django.urls import path

from . import item_views, location_views, receipt_views, views, webhooks

app_name = "integrations"

urlpatterns = [
    path("", views.integration_list, name="list"),
    path("new/", views.integration_create, name="create"),
    path("<int:pk>/", views.integration_detail, name="detail"),
    path("<int:pk>/edit/", views.integration_update, name="update"),
    path("<int:pk>/delete/", views.integration_delete, name="delete"),
    path("<int:pk>/sync-now/", views.integration_sync_now, name="sync_now"),
    path("<int:pk>/test-connection/", views.integration_test_connection, name="test_connection"),
    path("<int:pk>/toggle/", views.integration_toggle, name="toggle"),
    path("<int:pk>/items/", item_views.item_rules, name="item_rules"),
    path("<int:pk>/items/<int:rule_pk>/edit/", item_views.item_rule_update, name="item_rule_update"),
    path("<int:pk>/items/<int:rule_pk>/delete/", item_views.item_rule_delete, name="item_rule_delete"),
    path("<int:pk>/containers/", item_views.container_links, name="container_links"),
    path("<int:pk>/containers/match/", item_views.container_links_match, name="container_links_match"),
    path("<int:pk>/containers/mode/", item_views.container_links_mode, name="container_links_mode"),
    path("<int:pk>/containers/create/", item_views.container_links_create, name="container_links_create"),
    path("<int:pk>/locations/", location_views.location_mappings, name="location_mappings"),
    path(
        "<int:pk>/locations/<int:mapping_pk>/edit/",
        location_views.location_mapping_update,
        name="location_mapping_update",
    ),
    path(
        "<int:pk>/locations/<int:mapping_pk>/delete/",
        location_views.location_mapping_delete,
        name="location_mapping_delete",
    ),
    path("<int:pk>/receipts/", receipt_views.receipts, name="receipts"),
    path("<int:pk>/receipts/process/", receipt_views.receipts_process, name="receipts_process"),
]

# Webhook URLs are added to team_urlpatterns (require team_slug) via the SCM app router.
team_urlpatterns = [
    path("integrations/webhooks/<str:provider_code>/", webhooks.carrier_webhook, name="carrier_webhook"),
]
