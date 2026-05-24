from collections.abc import AsyncGenerator

from apps.ai.agents import get_agent, run_agent, run_agent_streaming
from apps.ai.handlers import agent_event_stream_handler
from apps.ai.types import AgentStreamEvent, AgentTypes
from apps.chat.models import Chat, ChatMessage, MessageTypes
from apps.users.models import CustomUser


class ChatSession:
    """Unified chat session that handles both simple chats and agent chats."""

    user: CustomUser
    agent_type: AgentTypes
    chat: Chat | None = None
    messages: list[dict] = []

    def __init__(
        self,
        user: CustomUser,
        chat_id: int | None,
        agent_type: AgentTypes = AgentTypes.CHAT,
    ):
        self.user = user
        self.chat_id = chat_id
        self.agent_type = agent_type
        self.agent = get_agent(agent_type)
        self.messages = []

    async def _async_init(self):
        if self.chat_id:
            self.chat = await Chat.objects.aget(user=self.user, id=self.chat_id)
            self.messages.extend([m.to_openai_dict() async for m in ChatMessage.objects.filter(chat=self.chat)])
            # Use the agent type from the DB record, not what was passed in
            if self.chat.agent_type:
                self.agent_type = AgentTypes.from_string(self.chat.agent_type)
                self.agent = get_agent(self.agent_type)
        else:
            self.chat = None

    @classmethod
    def from_chat(cls, chat: Chat) -> ChatSession:
        if chat.user is None:
            raise ValueError(f"Chat {chat.id} has no user - cannot create session")
        session = cls(chat.user, chat.id, AgentTypes.from_string(chat.agent_type))
        session.chat = chat
        session.messages.extend([m.to_openai_dict() for m in ChatMessage.objects.filter(chat=chat)])
        return session

    @classmethod
    async def create(cls, user: CustomUser, chat_id: int | None, agent_type: AgentTypes = AgentTypes.CHAT):
        session = cls(user=user, chat_id=chat_id, agent_type=agent_type)
        await session._async_init()
        return session

    async def add_message(self, message_text: str) -> tuple[ChatMessage, bool]:
        """
        Returns whether the chat was created.
        """
        from apps.chat.tasks import set_chat_name

        # if no chat set, create one and set the name
        chat_created = False
        if not self.chat:
            chat_created = True
            chat_kwargs = {
                "user": self.user,
                "agent_type": self.agent_type,
            }

            if len(message_text) < 40:
                chat_kwargs["name"] = message_text
                self.chat = await Chat.objects.acreate(**chat_kwargs)
            else:
                self.chat = await Chat.objects.acreate(**chat_kwargs)
                # only try to set the chat name with AI if the message is long enough
                set_chat_name.delay(self.chat.id, message_text)

        message = await self.save_message(message_text, MessageTypes.HUMAN)
        return message, chat_created

    async def save_message(self, message_text: str, message_type: MessageTypes) -> ChatMessage:
        if self.chat is None:
            raise ValueError("Cannot save message: no chat session exists")
        # save the user's message to the DB
        message = await ChatMessage.objects.acreate(
            chat=self.chat,
            message_type=message_type,
            content=message_text,
        )
        self.messages.append(message.to_openai_dict())
        return message

    async def get_response(self) -> str:
        """Return the next message in the chat from the session's current set of messages."""
        return await run_agent(
            self.agent,
            self.user,
            self.messages[-1]["content"],
            message_history=self.messages[:-1],
            event_stream_handler=agent_event_stream_handler,
        )

    async def get_response_streaming(self) -> AsyncGenerator[AgentStreamEvent]:
        """Yield typed stream events for the thinking UI."""
        async for event in run_agent_streaming(
            self.agent,
            self.user,
            self.messages[-1]["content"],
            message_history=self.messages[:-1],
        ):
            yield event
