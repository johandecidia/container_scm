from django.urls import path

from . import views

app_name = "containers"

urlpatterns = [
    path("", views.container_list, name="list"),
    path("create/", views.container_create, name="create"),
    path("<int:container_id>/", views.container_detail, name="detail"),
    path("<int:container_id>/edit/", views.container_update, name="update"),
    path("<int:container_id>/delete/", views.container_delete, name="delete"),
]
