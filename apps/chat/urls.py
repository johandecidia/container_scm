from django.urls import path

from . import views

app_name = "chat"

urlpatterns = [
    path("", views.chat_home, name="chat_home"),
    path("chat/new/", views.new_chat_streaming, name="new_chat"),
    path("chat/<int:chat_id>/", views.single_chat_streaming, name="single_chat"),
]
