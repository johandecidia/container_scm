from django.urls import path

from . import views

app_name = "visibility"

# The map-data endpoints live in this namespace rather than under shipments and
# containers: the GeoJSON contract is one thing, and splitting it across three apps
# would give it three places to drift.
urlpatterns = [
    path("", views.visibility_overview, name="overview"),
    # The work queues. Under visibility because they are views over the same read
    # models the Control Tower composes, not bounded contexts of their own.
    path("exceptions/", views.exceptions_queue, name="exceptions"),
    path("arrivals/", views.arrivals_queue, name="arrivals"),
    path("map-data/", views.visibility_map_data, name="map_data"),
    # What one map marker expands into. Keyed by position class as well as location:
    # "what is physically at Oceanterminalen" and "what is heading there" are two
    # markers on one place, and two different answers.
    path(
        "map-panel/<str:position_class>/<int:location_id>/",
        views.visibility_map_location_panel,
        name="map_location_panel",
    ),
    path("locations/<int:location_id>/map-data/", views.location_map_data, name="location_map_data"),
    path("shipments/<int:pk>/map-data/", views.shipment_map_data, name="shipment_map_data"),
    path("containers/<int:pk>/map-data/", views.container_map_data, name="container_map_data"),
]
