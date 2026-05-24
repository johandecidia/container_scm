from celery import shared_task
from django.conf import settings
from pydantic_ai import Agent

from apps.chat.models import Chat
from apps.chat.prompts import get_chat_naming_prompt


@shared_task
def set_chat_name(chat_id: int, message: str):
    chat = Chat.objects.get(id=chat_id)
    if not message:
        return
    elif len(message) < 30:
        # for short messages, just use them as the chat name. the summary won't help
        chat.name = message
        chat.save()
    else:
        agent = Agent(
            settings.DEFAULT_AI_MODEL,
            instructions=get_chat_naming_prompt(),
        )
        result = agent.run_sync(f"Summarize the following text: '{message}'")
        chat.name = result.output[:100].strip()
        chat.save()
