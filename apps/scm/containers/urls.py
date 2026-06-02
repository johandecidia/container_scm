from django.urls import path

from . import views

app_name = "containers"

urlpatterns = [
    path("", views.container_list, name="list"),
    path("create/", views.container_create, name="create"),
    path("<int:container_id>/", views.container_detail, name="detail"),
    path("<int:container_id>/edit/", views.container_update, name="update"),
    path("<int:container_id>/delete/", views.container_delete, name="delete"),
    # Container discovery
    path("discovery/", views.planned_container_dashboard, name="discovery_dashboard"),
    path("discovery/add/", views.planned_container_add, name="discovery_add"),
    path("discovery/run/", views.planned_container_run_discovery, name="discovery_run"),
    path("discovery/<int:pk>/cancel/", views.planned_container_cancel, name="discovery_cancel"),
]
