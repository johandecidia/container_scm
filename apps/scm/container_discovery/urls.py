from django.urls import path

from . import views

app_name = "container_discovery"

urlpatterns = [
    path("", views.container_discovery_dashboard, name="dashboard"),
]
