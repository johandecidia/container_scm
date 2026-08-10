from django.urls import path

from . import views

app_name = "analytics"

urlpatterns = [
    path("", views.analytics_dashboard, name="dashboard"),
    path("search/", views.scm_search, name="search"),
    path("filters/", views.saved_filter_create, name="saved_filter_create"),
    path("filters/<int:pk>/delete/", views.saved_filter_delete, name="saved_filter_delete"),
]
