from django.urls import path

from . import consumers

websocket_urlpatterns = [
    path(r"ws/aichat/", consumers.ChatConsumer.as_asgi(), name="ws_ai_new_chat"),
    path(r"ws/aichat/<slug:chat_id>/", consumers.ChatConsumer.as_asgi(), name="ws_ai_continue_chat"),
]
