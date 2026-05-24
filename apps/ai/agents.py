from __future__ import annotations as _annotations

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable

from django.conf import settings
from django.utils import timezone
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    SystemPromptPart,
    TextPart,
    TextPartDelta,
    UserPromptPart,
)
from pydantic_ai.toolsets import AbstractToolset

from apps.ai.tools import admin_db, email_toolset, employee_toolset, weather_toolset
from apps.ai.types import (
    AgentStreamEvent,
    AgentTypes,
    FinalTextEvent,
    ThinkingTextEvent,
    ToolCallEvent,
    ToolResultEvent,
    UserDependencies,
)
from apps.chat.prompts import get_default_system_prompt
from apps.users.models import CustomUser

logger = logging.getLogger("pegasus.ai")

# Type alias for instruction items (can be strings or callables)
InstructionsType = str | Callable[[RunContext[UserDependencies]], Awaitable[str]] | Callable[[], str]


async def add_user_name(ctx: RunContext[UserDependencies]) -> str:
    return f"The user's name is {ctx.deps.user.get_display_name()}"


async def add_user_email(ctx: RunContext[UserDependencies]) -> str:
    return f"The user's email is {ctx.deps.user.email}"


async def current_datetime(ctx: RunContext[UserDependencies]) -> str:
    return f"The current datetime is {timezone.now()}"


DEFAULT_INSTRUCTIONS: list[InstructionsType] = [
    get_default_system_prompt(),
    add_user_name,
    add_user_email,
    current_datetime,
]


def get_agent(agent_type: AgentTypes = AgentTypes.CHAT) -> Agent[UserDependencies]:
    if agent_type == AgentTypes.CHAT:
        return get_chat_agent()
    elif agent_type == AgentTypes.WEATHER:
        return get_weather_agent()
    elif agent_type == AgentTypes.ADMIN:
        return get_admin_agent()
    elif agent_type == AgentTypes.EMPLOYEES:
        return get_employees_agent()
    else:
        raise ValueError(f"Invalid agent type: {agent_type}")


def get_chat_agent():
    """Simple chat agent with no tools."""
    return _get_agent([])


def get_weather_agent():
    return _get_agent([weather_toolset])


def get_admin_agent():
    return _get_agent([admin_db, email_toolset])


def get_employees_agent():
    return _get_agent([employee_toolset])


def _get_agent(toolsets: list[AbstractToolset]):
    return Agent(
        settings.DEFAULT_AI_MODEL,
        toolsets=toolsets,
        instructions=DEFAULT_INSTRUCTIONS,
        retries=2,
        deps_type=UserDependencies,
    )


def convert_openai_to_pydantic_messages(openai_messages: list[dict]) -> list[ModelMessage]:
    """Convert OpenAI-style messages to Pydantic AI ModelMessage format."""
    pydantic_messages: list[ModelMessage] = []

    for msg in openai_messages:
        role = msg.get("role")
        content = msg.get("content")

        if not isinstance(content, str):
            continue  # Skip messages without valid string content

        if role == "user":
            pydantic_messages.append(ModelRequest(parts=[UserPromptPart(content=content)]))
        elif role == "assistant":
            pydantic_messages.append(ModelResponse(parts=[TextPart(content=content)]))
        elif role in ["system", "developer"]:
            pydantic_messages.append(ModelRequest(parts=[SystemPromptPart(content=content)]))

    return pydantic_messages


async def run_agent(
    agent: Agent[UserDependencies],
    user: CustomUser,
    message: str,
    message_history: list[dict] | None = None,
    event_stream_handler: Callable | None = None,
):
    """Run an agent and return the response."""
    deps = UserDependencies(user=user)
    pydantic_messages = convert_openai_to_pydantic_messages(message_history) if message_history else None
    result = await agent.run(
        message, message_history=pydantic_messages, deps=deps, event_stream_handler=event_stream_handler
    )
    return result.output


async def run_agent_streaming(
    agent: Agent[UserDependencies],
    user: CustomUser,
    message: str,
    message_history: list[dict] | None = None,
) -> AsyncGenerator[AgentStreamEvent]:
    """Run an agent and stream typed events for the thinking UI.

    Uses agent.iter() to properly handle tool calls. Yields typed events:
    - ThinkingTextEvent: text preamble before tool calls
    - ToolCallEvent/ToolResultEvent: tool execution
    - FinalTextEvent: final answer text (single event after loop completes)
    """
    deps = UserDependencies(user=user)
    pydantic_messages = convert_openai_to_pydantic_messages(message_history) if message_history else None
    async with agent.iter(message, message_history=pydantic_messages, deps=deps) as agent_run:
        async for node in agent_run:
            if agent.is_model_request_node(node):
                async with node.stream(agent_run.ctx) as model_stream:
                    async for model_event in model_stream:
                        mapped_model = _map_model_event(model_event)
                        if mapped_model:
                            yield mapped_model
            elif agent.is_call_tools_node(node):
                tool_call_names: dict[str, str] = {}
                async with node.stream(agent_run.ctx) as tool_stream:
                    async for tool_event in tool_stream:
                        mapped_tool = _map_tool_event(tool_event, tool_call_names)
                        if mapped_tool:
                            yield mapped_tool

        # After the loop, grab the final answer from the completed run
        if agent_run.result:
            yield FinalTextEvent(text=agent_run.result.output)


def _map_model_event(event) -> ThinkingTextEvent | None:
    """Map a pydantic_ai model request stream event to a ThinkingTextEvent."""
    if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
        if event.part.content:
            return ThinkingTextEvent(text=event.part.content)
    elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
        return ThinkingTextEvent(text=event.delta.content_delta)
    return None


def _map_tool_event(event, tool_call_names: dict[str, str]) -> ToolCallEvent | ToolResultEvent | None:
    """Map a pydantic_ai tool stream event to a ToolCallEvent or ToolResultEvent.

    Mutates tool_call_names to track the mapping from tool_call_id to tool_name,
    since FunctionToolResultEvent only has tool_call_id.
    """
    if isinstance(event, FunctionToolCallEvent):
        tool_call_names[event.tool_call_id] = event.part.tool_name
        logger.debug("LLM calls tool=%r with args=%s", event.part.tool_name, event.part.args)
        return ToolCallEvent(tool_name=event.part.tool_name, args=str(event.part.args), tool_call_id=event.tool_call_id)
    elif isinstance(event, FunctionToolResultEvent):
        tool_name = tool_call_names.get(event.tool_call_id, "unknown")
        logger.debug("Tool call %r returned => %s", event.tool_call_id, event.result.content)
        return ToolResultEvent(tool_name=tool_name, result=str(event.result.content), tool_call_id=event.tool_call_id)
    return None
