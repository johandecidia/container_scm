from django.urls import path

from . import views, webhooks

app_name = "integrations"

urlpatterns = [
    path("", views.integration_list, name="list"),
    path("new/", views.integration_create, name="create"),
    path("<int:pk>/", views.integration_detail, name="detail"),
    path("<int:pk>/edit/", views.integration_update, name="update"),
    path("<int:pk>/delete/", views.integration_delete, name="delete"),
    path("<int:pk>/sync-now/", views.integration_sync_now, name="sync_now"),
    path("<int:pk>/test-connection/", views.integration_test_connection, name="test_connection"),
]

# Webhook URLs are added to team_urlpatterns (require team_slug) via the SCM app router.
team_urlpatterns = [
    path("integrations/webhooks/<str:provider_code>/", webhooks.carrier_webhook, name="carrier_webhook"),
]
