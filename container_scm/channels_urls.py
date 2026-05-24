"""Channel URL routing configuration."""

from apps.chat.routing import websocket_urlpatterns as ai_chat_patterns
from apps.group_chat.routing import websocket_urlpatterns as group_chat_patterns

urlpatterns: list = [] + ai_chat_patterns + group_chat_patterns
