from django.urls import path

from . import condition_views, intake_views, location_views, views

app_name = "containers"

urlpatterns = [
    path("", views.container_list, name="list"),
    # Adding containers: single, pasted list, CSV
    path("create/", intake_views.container_create, name="create"),
    path("create/check/", intake_views.container_number_check, name="number_check"),
    path("import/paste/", intake_views.container_import_paste, name="import_paste"),
    path("import/csv/", intake_views.container_import_csv, name="import_csv"),
    path("import/confirm/", intake_views.container_import_confirm, name="import_confirm"),
    path("<int:container_id>/", views.container_detail, name="detail"),
    path("<int:container_id>/edit/", views.container_update, name="update"),
    path("<int:container_id>/refresh-tracking/", views.container_refresh_tracking, name="refresh_tracking"),
    # Physical movement. `?type=gate_in` opens the modal on a movement; the form
    # accepts any of the four operational types, so the query string chooses the
    # starting point rather than restricting what may be recorded.
    path("<int:container_id>/movements/record/", views.container_record_movement, name="record_movement"),
    path("<int:container_id>/delete/", views.container_delete, name="delete"),
    # Container discovery
    path("discovery/", views.planned_container_dashboard, name="discovery_dashboard"),
    path("discovery/add/", views.planned_container_add, name="discovery_add"),
    path("discovery/run/", views.planned_container_run_discovery, name="discovery_run"),
    path("discovery/<int:pk>/cancel/", views.planned_container_cancel, name="discovery_cancel"),
    # Container conditions — the team's own grading vocabulary, under Settings.
    # There is deliberately no delete route; retiring is `condition_deactivate`.
    path("conditions/", condition_views.container_condition_list, name="condition_list"),
    path("conditions/create/", condition_views.container_condition_create, name="condition_create"),
    path("conditions/<int:condition_id>/edit/", condition_views.container_condition_update, name="condition_update"),
    path(
        "conditions/<int:condition_id>/deactivate/",
        condition_views.container_condition_deactivate,
        name="condition_deactivate",
    ),
    # Container locations. `create` is declared before `<int:location_id>` so the
    # literal segment is matched first; the detail route is the Location Workspace.
    path("locations/", location_views.container_location_list, name="location_list"),
    path("locations/create/", location_views.container_location_create, name="location_create"),
    path("locations/<int:location_id>/", location_views.container_location_detail, name="location_detail"),
    path("locations/<int:location_id>/edit/", location_views.container_location_update, name="location_update"),
    path(
        "locations/<int:location_id>/deactivate/",
        location_views.container_location_deactivate,
        name="location_deactivate",
    ),
    # External identity. Aliases hang off a location because that is what they name,
    # so the location's id is part of the path and scoping is not optional.
    path(
        "locations/<int:location_id>/aliases/add/",
        location_views.container_location_alias_create,
        name="location_alias_create",
    ),
    path(
        "locations/<int:location_id>/aliases/<int:alias_id>/delete/",
        location_views.container_location_alias_delete,
        name="location_alias_delete",
    ),
    # LOC-5's action, arrived at from the other direction: the operator is looking
    # at what a provider reported and says which canonical location it is. No
    # location id in the path — choosing one is the whole decision — and the
    # evidence travels in the query string and the form.
    path("locations/aliases/from-evidence/", location_views.location_evidence_alias, name="location_evidence_alias"),
]
