import json
import logging
from html import escape
from urllib.parse import parse_qs

from channels.generic.websocket import AsyncWebsocketConsumer
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from apps.ai.types import (
    AgentTypes,
    FinalTextEvent,
    ThinkingTextEvent,
    ToolCallEvent,
    ToolResultEvent,
    WebSocketCommandType,
)
from apps.chat.models import MessageTypes
from apps.chat.sessions import ChatSession

logger = logging.getLogger("pegasus.ai")


class ChatConsumer(AsyncWebsocketConsumer):
    session: ChatSession
    agent_type: AgentTypes = AgentTypes.CHAT

    async def connect(self):
        self.user = self.scope["user"]
        chat_id = self.scope["url_route"]["kwargs"].get("chat_id", None)

        query_string = self.scope.get("query_string", b"").decode()
        query_params = parse_qs(query_string)
        self.is_embedded = query_params.get("embedded", ["false"])[0] == "true"
        agent_type = query_params.get("agent_type", [self.agent_type])[0]

        agent_type = AgentTypes.from_string(agent_type)

        if not self.user.is_authenticated:
            await self.close()
            return

        if agent_type == AgentTypes.ADMIN and not self.user.is_superuser:
            await self.close()
            return

        self.session = await ChatSession.create(self.user, chat_id, agent_type)
        await self.accept()

    async def disconnect(self, close_code):
        pass

    async def receive(self, text_data):
        text_data_json = json.loads(text_data)
        message_text = text_data_json["message"]

        # do nothing with empty messages
        if not message_text.strip():
            return

        message, chat_created = await self.session.add_message(message_text)
        if chat_created and not self.is_embedded:
            # Send a message to tell the front end to update its url if not in embedded mode.
            await self._send_command("pushURL", {"url": reverse("chat:single_chat", args=[self.session.chat.id])})

        # show user's message immediately before calling OpenAI API
        user_message_html = render_to_string(
            "chat/websocket_components/user_message.html",
            {
                "message_text": message_text,
            },
        )
        await self.send(text_data=user_message_html)

        # render an empty system message where we'll stream our response
        contents_div_id = f"message-response-{message.id}"
        system_message_html = render_to_string(
            "chat/websocket_components/system_message.html",
            {
                "contents_div_id": contents_div_id,
            },
        )
        await self.send(text_data=system_message_html)

        try:
            response_stream = self.session.get_response_streaming()
            response = ""
            has_tool_calls = False
            is_final = False
            thinking_id = f"thinking-{contents_div_id}"
            await self._show_thinking_section(thinking_id)
            async for event in response_stream:
                if isinstance(event, ThinkingTextEvent):
                    # stream thinking chunk
                    await self.send(
                        text_data=(
                            f'<div hx-swap-oob="beforeend:#{_thinking_content_id(thinking_id)}">'
                            f"{_format_token(event.text)}</div>"
                        )
                    )

                elif isinstance(event, ToolCallEvent):
                    has_tool_calls = True
                    # add tool call to thinking section
                    await self.send(
                        text_data=(
                            f'<div hx-swap-oob="beforeend:#{_thinking_content_id(thinking_id)}">'
                            f'<p class="text-sm opacity-60 my-0" id="{_tool_line_id(thinking_id, event.tool_call_id)}">'
                            f'{_("Calling")} <code class="text-xs">{escape(event.tool_name)}</code>...</p></div>'
                        )
                    )

                elif isinstance(event, ToolResultEvent):
                    # update tool call with result in thinking section
                    await self.send(
                        text_data=(
                            f'<p class="text-sm opacity-60 my-0" id="{_tool_line_id(thinking_id, event.tool_call_id)}"'
                            f' hx-swap-oob="true">'
                            f'{_("Called")} <code class="text-xs">{escape(event.tool_name)}</code> ✓</p>'
                        )
                    )

                elif isinstance(event, FinalTextEvent):
                    # collapse or hide thinking section if needed and stream final response
                    if not is_final:
                        if has_tool_calls:
                            await self._send_command("collapseThinking", {"targetId": thinking_id})
                        else:
                            await self._send_command("hideThinking", {"targetId": thinking_id})
                        is_final = True

                    await self.send(
                        text_data=f'<div hx-swap-oob="beforeend:#{contents_div_id}">{_format_token(event.text)}</div>'
                    )
                    response += event.text

        except Exception as e:
            logger.exception(e)
            response = None

        if not response:
            # if we didn't get a response we should show the user an error.
            error_html = render_to_string(
                "chat/websocket_components/final_system_message.html",
                {
                    "contents_div_id": contents_div_id,
                    "message": _("Sorry, there was an error with your message. Please try again."),
                },
            )
            await self.send(text_data=error_html)
        else:
            # once we've streamed the whole response, save it to the database
            system_message = await self.session.save_message(response, MessageTypes.AI)
            # replace with fully rendered version, so we can render markdown, etc.
            final_message_html = render_to_string(
                "chat/websocket_components/final_system_message.html",
                {
                    "contents_div_id": contents_div_id,
                    "message": system_message.content,
                },
            )
            await self.send(text_data=final_message_html)

    async def _send_command(self, command_type: WebSocketCommandType, data: dict) -> None:
        """Send a JSON command to the frontend."""
        await self.send(text_data=json.dumps({"type": command_type, "data": data}))

    async def _show_thinking_section(self, thinking_id: str) -> None:
        """Show the thinking section."""
        thinking_html = render_to_string(
            "chat/websocket_components/thinking_wrapper.html",
            {
                "thinking_id": thinking_id,
                "content_id": _thinking_content_id(thinking_id),
            },
        )
        await self.send(text_data=thinking_html)


def _format_token(token: str) -> str:
    # apply very basic formatting while we're rendering tokens in real-time
    token = escape(token)
    token = token.replace("\n", "<br>")
    return token


def _thinking_content_id(thinking_id: str) -> str:
    return f"{thinking_id}-content"


def _tool_line_id(thinking_id: str, tool_call_id: str) -> str:
    return f"{thinking_id}-tool-{tool_call_id}"
