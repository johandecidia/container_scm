from django.urls import path

from . import member_views, tracking_views, views

app_name = "team_settings"

urlpatterns = [
    path("", views.settings_home, name="home"),
    # Members
    path("members/", member_views.members, name="members"),
    path("members/<int:membership_id>/role/", member_views.member_role_update, name="member_role_update"),
    path("members/<int:membership_id>/remove/", member_views.member_remove, name="member_remove"),
    path("members/invite/", member_views.invitation_send, name="invitation_send"),
    path("members/invite/<uuid:invitation_id>/resend/", member_views.invitation_resend, name="invitation_resend"),
    path("members/invite/<uuid:invitation_id>/cancel/", member_views.invitation_cancel, name="invitation_cancel"),
    # Tracking integrations. Keyed by provider code rather than integration pk: the
    # page lists carriers, most of which have no integration row yet.
    path("tracking/", tracking_views.tracking, name="tracking"),
    path(
        "tracking/carriers/<str:provider_code>/credentials/",
        tracking_views.carrier_credentials,
        name="carrier_credentials",
    ),
    path(
        "tracking/carriers/<str:provider_code>/test/",
        tracking_views.carrier_test_connection,
        name="carrier_test_connection",
    ),
    path("tracking/carriers/<str:provider_code>/toggle/", tracking_views.carrier_toggle, name="carrier_toggle"),
]
