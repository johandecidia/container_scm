from django.urls import path

from . import views

app_name = "integrations"

urlpatterns = [
    path("", views.integration_list, name="list"),
    path("new/", views.integration_create, name="create"),
    path("<int:pk>/", views.integration_detail, name="detail"),
    path("<int:pk>/edit/", views.integration_update, name="update"),
    path("<int:pk>/delete/", views.integration_delete, name="delete"),
]
