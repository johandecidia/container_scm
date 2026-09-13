from django.urls import path

from . import member_views, views

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
]
