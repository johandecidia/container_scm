from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse

from apps.chat.models import Chat
from apps.web.meta import websocket_absolute_url, websocket_reverse


@login_required
def chat_home(request):
    chats = request.user.chats.order_by("-updated_at")
    return TemplateResponse(
        request,
        "chat/chat_home.html",
        {
            "active_tab": "ai-chat",
            "chats": chats,
        },
    )


@login_required
def new_chat_streaming(request):
    websocket_url = websocket_absolute_url(websocket_reverse("ws_ai_new_chat"))
    agent_type = request.GET.get("agent_type", "")
    if agent_type:
        websocket_url += f"?agent_type={agent_type}"
    return TemplateResponse(
        request,
        "chat/single_chat_streaming.html",
        {
            "active_tab": "ai-chat",
            "websocket_url": websocket_url,
        },
    )


@login_required
def single_chat_streaming(request, chat_id: int):
    chat = get_object_or_404(Chat, user=request.user, id=chat_id)
    websocket_url = websocket_absolute_url(websocket_reverse("ws_ai_continue_chat", args=[chat_id]))
    return TemplateResponse(
        request,
        "chat/single_chat_streaming.html",
        {
            "active_tab": "ai-chat",
            "websocket_url": websocket_url,
            "chat": chat,
        },
    )
