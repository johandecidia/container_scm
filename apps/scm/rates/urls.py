from django.urls import path

from . import views

app_name = "rates"

urlpatterns = [
    path("", views.rate_list, name="list"),
    path("new/", views.rate_create, name="create"),
    path("<int:pk>/", views.rate_detail, name="detail"),
    path("<int:pk>/edit/", views.rate_update, name="update"),
    path("<int:pk>/delete/", views.rate_delete, name="delete"),
]
