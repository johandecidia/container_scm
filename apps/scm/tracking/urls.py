from django.urls import path

from . import views

app_name = "tracking"

urlpatterns = [
    path("", views.tracking_list, name="list"),
    path("new/", views.start_tracking, name="start"),
    path("<int:pk>/", views.tracking_detail, name="detail"),
    path("<int:pk>/pause/", views.pause_tracking, name="pause"),
    path("<int:pk>/resume/", views.resume_tracking, name="resume"),
    path("<int:pk>/sync/", views.manual_sync_tracking, name="sync"),
    path("<int:pk>/cancel/", views.cancel_tracking, name="cancel"),
    path("<int:pk>/timeline/", views.tracking_timeline_partial, name="timeline"),
]
