from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from apps.users.models import CustomUser


class AgentTypes(StrEnum):
    CHAT = "chat"  # Simple chat, no tools
    WEATHER = "weather"
    ADMIN = "admin"
    EMPLOYEES = "employees"

    @classmethod
    def from_string(cls, value: str) -> AgentTypes:
        """Convert a string to AgentTypes, defaulting to CHAT for invalid values."""
        try:
            return cls(value)
        except ValueError:
            return cls.CHAT


@dataclass
class UserDependencies:
    user: CustomUser


@dataclass
class ThinkingTextEvent:
    """Text preamble delta from the model during thinking (before final answer)."""

    text: str


@dataclass
class ToolCallEvent:
    """A tool is being called."""

    tool_name: str
    args: str
    tool_call_id: str


@dataclass
class ToolResultEvent:
    """A tool call completed."""

    tool_name: str
    result: str
    tool_call_id: str


@dataclass
class FinalTextEvent:
    """Text delta that is part of the final answer."""

    text: str


AgentStreamEvent = ThinkingTextEvent | ToolCallEvent | ToolResultEvent | FinalTextEvent

WebSocketCommandType = Literal["pushURL", "collapseThinking", "hideThinking"]
