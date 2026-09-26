from django.urls import path

from . import platform_views, views

app_name = "tracking"

urlpatterns = [
    path("", views.tracking_list, name="list"),
    # Provider account status — plan, allowance, billing cycle. Superuser only, and the
    # decorator is what enforces it; see platform_views.py for why this is a separate
    # route rather than a panel on a team-facing page.
    path("providers/", platform_views.provider_status, name="provider_status"),
    path("new/", views.start_tracking, name="start"),
    path("<int:pk>/", views.tracking_detail, name="detail"),
    path("<int:pk>/pause/", views.pause_tracking, name="pause"),
    path("<int:pk>/resume/", views.resume_tracking, name="resume"),
    path("<int:pk>/sync/", views.manual_sync_tracking, name="sync"),
    path("<int:pk>/stop/", views.stop_tracking, name="stop"),
    path("<int:pk>/timeline/", views.tracking_timeline_partial, name="timeline"),
]
