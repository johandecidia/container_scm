from django.urls import path

from . import views

app_name = "containers"

urlpatterns = [
    path("", views.container_list, name="list"),
    path("new/", views.container_create, name="create"),
    path("<int:pk>/", views.container_detail, name="detail"),
    path("<int:pk>/edit/", views.container_update, name="update"),
    path("<int:pk>/delete/", views.container_delete, name="delete"),
]
