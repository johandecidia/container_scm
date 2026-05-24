from apps.web.meta import websocket_absolute_url, websocket_reverse


def chat_websocket_url(request):
    """
    Context processor that provides a default chat WebSocket URL.
    Views can override this by setting 'chat_websocket_url' in their context.
    """
    default_url = websocket_absolute_url(websocket_reverse("ws_ai_new_chat"))
    return {"chat_websocket_url": default_url}
