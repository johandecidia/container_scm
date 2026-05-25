from django.urls import path

from . import views

app_name = "imports"

urlpatterns = [
    path("", views.import_list, name="list"),
    path("new/", views.import_create, name="create"),
    path("<int:pk>/", views.import_detail, name="detail"),
]
