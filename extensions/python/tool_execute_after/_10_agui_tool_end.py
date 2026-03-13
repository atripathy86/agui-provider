"""Emit AG-UI ToolCallEnd + ToolCallResult after tool execution.

For the 'response' tool, emits TEXT_MESSAGE_CONTENT + TEXT_MESSAGE_END
instead of tool call events.
"""
import sys
from pathlib import Path
from helpers.extension import Extension
from agent import LoopData

_plugin_root = Path(__file__).resolve().parents[3]
if str(_plugin_root) not in sys.path:
    sys.path.insert(0, str(_plugin_root))


class AGUIToolEnd(Extension):
    async def execute(self, loop_data: LoopData = LoopData(),
                      tool_name: str = "", response=None, **kwargs):
        if not tool_name:
            return

        from agui_helpers.agui_server import get_run_id_for_context
        from agui_helpers.event_bus import emit
        from agui_helpers.agui_events import (
            encode_tool_call_end, encode_tool_call_result,
            encode_text_message_content, encode_text_message_end,
        )

        run_id = get_run_id_for_context(self.agent.context.id)
        if not run_id:
            return

        state = getattr(self.agent, '_agui_state', {})

        if tool_name == "response":
            # Response tool = user-facing text. Emit content and close the text message.
            msg_id = state.get("current_message_id", "unknown")
            text = ""
            if response and hasattr(response, 'message'):
                text = response.message
            elif isinstance(response, str):
                text = response
            if text:
                emit(run_id, encode_text_message_content(msg_id, text))
            emit(run_id, encode_text_message_end(msg_id))
            state["text_started"] = False
            self.agent._agui_state = state
            return

        # Regular tool -- emit tool call end + result
        tool_call_id = state.get("current_tool_call_id", "unknown")

        emit(run_id, encode_tool_call_end(tool_call_id))

        # Emit tool result
        result_text = ""
        if response and hasattr(response, 'message'):
            result_text = response.message
        elif isinstance(response, str):
            result_text = response

        if result_text:
            emit(run_id, encode_tool_call_result(tool_call_id, result_text))

        state.pop("current_tool_call_id", None)
        self.agent._agui_state = state
