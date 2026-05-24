from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.ai.types import AgentTypes
from apps.utils.models import BaseModel


class MessageTypes(models.TextChoices):
    HUMAN = "HUMAN", _("Human")
    AI = "AI", _("AI")
    SYSTEM = "SYSTEM", _("System")


def get_agent_type_choices():
    # allow agent types to be set dynamically without introducing migrations
    # https://adamj.eu/tech/2025/05/03/django-choices-change-without-migration/
    return models.TextChoices("AgentType", " ".join(AgentTypes)).choices


class Chat(BaseModel):
    """
    A chat (session) instance.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True, related_name="chats"
    )
    agent_type = models.CharField(max_length=30, choices=get_agent_type_choices, default=AgentTypes.CHAT)
    name = models.CharField(max_length=100, default=_("Unnamed Chat"))

    def __str__(self):
        return f"{self.name} ({self.user})"


class ChatMessage(BaseModel):
    """
    A message in a Chat.
    """

    chat = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name="messages")
    message_type = models.CharField(max_length=10, choices=MessageTypes.choices)
    content = models.TextField()

    class Meta:
        ordering = ["created_at"]

    @property
    def is_ai_message(self) -> bool:
        return self.message_type == MessageTypes.AI

    @property
    def is_human_message(self) -> bool:
        return self.message_type == MessageTypes.HUMAN

    def to_openai_dict(self) -> dict:
        return {
            "role": self.get_openai_role(),
            "content": self.content,
        }

    def get_openai_role(self):
        if self.message_type == MessageTypes.HUMAN:
            return "user"
        elif self.message_type == MessageTypes.AI:
            return "assistant"
        else:
            return "system"
