from django.urls import path

from . import views

app_name = "visibility"

# The map-data endpoints live in this namespace rather than under shipments and
# containers: the GeoJSON contract is one thing, and splitting it across three apps
# would give it three places to drift.
urlpatterns = [
    path("", views.visibility_overview, name="overview"),
    path("map-data/", views.visibility_map_data, name="map_data"),
    path("panel/<str:kind>/<int:pk>/", views.visibility_object_panel, name="object_panel"),
    path("shipments/<int:pk>/map-data/", views.shipment_map_data, name="shipment_map_data"),
    path("containers/<int:pk>/map-data/", views.container_map_data, name="container_map_data"),
]
