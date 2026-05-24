from django.test import SimpleTestCase
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolCallPart,
    ToolCallPartDelta,
    ToolReturnPart,
)

from apps.ai.agents import _map_model_event, _map_tool_event
from apps.ai.types import ThinkingTextEvent, ToolCallEvent, ToolResultEvent


class TestMapModelEvent(SimpleTestCase):
    def test_part_start_with_text(self):
        event = PartStartEvent(index=0, part=TextPart(content="Hello"))
        result = _map_model_event(event)
        self.assertEqual(result, ThinkingTextEvent(text="Hello"))

    def test_part_start_with_empty_text(self):
        event = PartStartEvent(index=0, part=TextPart(content=""))
        result = _map_model_event(event)
        self.assertIsNone(result)

    def test_text_delta(self):
        event = PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="world"))
        result = _map_model_event(event)
        self.assertEqual(result, ThinkingTextEvent(text="world"))

    def test_tool_call_delta_ignored(self):
        event = PartDeltaEvent(index=0, delta=ToolCallPartDelta(args_delta="{}"))
        result = _map_model_event(event)
        self.assertIsNone(result)

    def test_part_end_ignored(self):
        event = PartEndEvent(index=0, part=TextPart(content="done"))
        result = _map_model_event(event)
        self.assertIsNone(result)


class TestMapToolEvent(SimpleTestCase):
    def test_tool_call(self):
        names: dict[str, str] = {}
        part = ToolCallPart(tool_name="weather_forecast", args='{"location": "Paris"}')
        event = FunctionToolCallEvent(part=part)
        result = _map_tool_event(event, names)
        self.assertEqual(
            result,
            ToolCallEvent(tool_name="weather_forecast", args='{"location": "Paris"}', tool_call_id=event.tool_call_id),
        )
        self.assertEqual(names[event.tool_call_id], "weather_forecast")

    def test_tool_result(self):
        part = ToolCallPart(tool_name="weather_forecast", args="{}")
        call_event = FunctionToolCallEvent(part=part)
        names = {call_event.tool_call_id: "weather_forecast"}

        result_part = ToolReturnPart(
            tool_name="weather_forecast",
            content="24°C and sunny",
            tool_call_id=call_event.tool_call_id,
        )
        result_event = FunctionToolResultEvent(result=result_part)
        result = _map_tool_event(result_event, names)
        self.assertEqual(
            result,
            ToolResultEvent(
                tool_name="weather_forecast", result="24°C and sunny", tool_call_id=call_event.tool_call_id
            ),
        )

    def test_tool_result_unknown_call_id(self):
        result_part = ToolReturnPart(
            tool_name="weather_forecast",
            content="24°C",
            tool_call_id="unknown-id",
        )
        result_event = FunctionToolResultEvent(result=result_part)
        result = _map_tool_event(result_event, {})
        self.assertEqual(result, ToolResultEvent(tool_name="unknown", result="24°C", tool_call_id="unknown-id"))

    def test_call_then_result_flow(self):
        """Test the full flow: call event populates names, result event reads it."""
        names: dict[str, str] = {}
        call_part = ToolCallPart(tool_name="execute_query", args='{"sql": "SELECT 1"}')
        call_event = FunctionToolCallEvent(part=call_part)

        call_result = _map_tool_event(call_event, names)
        self.assertEqual(
            call_result,
            ToolCallEvent(tool_name="execute_query", args='{"sql": "SELECT 1"}', tool_call_id=call_event.tool_call_id),
        )

        result_part = ToolReturnPart(
            tool_name="execute_query",
            content="1",
            tool_call_id=call_event.tool_call_id,
        )
        result_event = FunctionToolResultEvent(result=result_part)
        result_result = _map_tool_event(result_event, names)
        self.assertEqual(
            result_result,
            ToolResultEvent(tool_name="execute_query", result="1", tool_call_id=call_event.tool_call_id),
        )
