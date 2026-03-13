"""Emit AG-UI ToolCallStart + ToolCallArgs before tool execution.

For the 'response' tool, emits TEXT_MESSAGE_START instead of tool call events,
since the response tool carries the user-facing text.
"""
import json
import sys
import uuid
from pathlib import Path
from helpers.extension import Extension
from agent import LoopData

_plugin_root = Path(__file__).resolve().parents[3]
if str(_plugin_root) not in sys.path:
    sys.path.insert(0, str(_plugin_root))


class AGUIToolStart(Extension):
    async def execute(self, loop_data: LoopData = LoopData(),
                      tool_name: str = "", tool_args: dict = None, **kwargs):
        if not tool_name:
            return

        from agui_helpers.agui_server import get_run_id_for_context
        from agui_helpers.event_bus import emit
        from agui_helpers.agui_events import (
            encode_text_message_start,
            encode_text_message_end,
            encode_reasoning_message_end,
            encode_reasoning_end,
            encode_tool_call_start,
            encode_tool_call_args,
        )

        run_id = get_run_id_for_context(self.agent.context.id)
        if not run_id:
            return

        state = getattr(self.agent, '_agui_state', {})

        # Close reasoning if open
        if state.get("thinking_started"):
            reasoning_id = state.get("reasoning_message_id", "unknown")
            emit(run_id, encode_reasoning_message_end(reasoning_id))
            emit(run_id, encode_reasoning_end(reasoning_id))
            state["thinking_started"] = False

        # Close text message if open (tool call interrupts text)
        if state.get("text_started"):
            msg_id = state.get("current_message_id", "unknown")
            emit(run_id, encode_text_message_end(msg_id))
            state["text_started"] = False

        if tool_name == "response":
            # Response tool = user-facing text. Start a text message block.
            msg_id = str(uuid.uuid4())
            state["current_message_id"] = msg_id
            state["text_started"] = True
            self.agent._agui_state = state
            emit(run_id, encode_text_message_start(msg_id, "assistant"))
            return

        # Regular tool -- emit tool call events
        tool_call_id = str(uuid.uuid4())
        state["current_tool_call_id"] = tool_call_id
        self.agent._agui_state = state

        parent_msg_id = state.get("current_message_id")
        emit(run_id, encode_tool_call_start(tool_call_id, tool_name, parent_msg_id))

        # Emit args as a single JSON chunk
        if tool_args:
            emit(run_id, encode_tool_call_args(tool_call_id, json.dumps(tool_args)))
