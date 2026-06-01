from django.urls import path

from . import views

app_name = "imports"

urlpatterns = [
    path("", views.import_list, name="list"),
    path("upload/", views.import_upload, name="upload"),
    path("<int:pk>/", views.import_detail, name="detail"),
    path("<int:pk>/parse/", views.import_parse, name="parse"),
    path("<int:pk>/validate/", views.import_validate, name="validate"),
    path("<int:pk>/confirm/", views.import_confirm, name="confirm"),
]
