"""Buffer response stream chunks instead of emitting as text.

Agent Zero's response stream contains raw monologue JSON (thoughts, tool_name,
tool_args). This must NOT be shown to the user as text. The actual user-facing
response comes through the 'response' tool, handled in tool_execute_after.
"""
import sys
from pathlib import Path
from helpers.extension import Extension
from agent import LoopData

_plugin_root = Path(__file__).resolve().parents[3]
if str(_plugin_root) not in sys.path:
    sys.path.insert(0, str(_plugin_root))


class AGUITextStream(Extension):
    async def execute(self, loop_data: LoopData = LoopData(),
                      stream_data: dict = None, **kwargs):
        if not stream_data:
            return

        from agui_helpers.agui_server import get_run_id_for_context

        run_id = get_run_id_for_context(self.agent.context.id)
        if not run_id:
            return

        state = getattr(self.agent, '_agui_state', {})

        # Close reasoning block if it was open (transition from thinking -> response)
        if state.get("thinking_started"):
            from agui_helpers.event_bus import emit
            from agui_helpers.agui_events import (
                encode_reasoning_message_end,
                encode_reasoning_end,
            )
            reasoning_id = state.get("reasoning_message_id", "unknown")
            emit(run_id, encode_reasoning_message_end(reasoning_id))
            emit(run_id, encode_reasoning_end(reasoning_id))
            state["thinking_started"] = False
            self.agent._agui_state = state

        # Buffer the full response text -- don't emit as TEXT_MESSAGE_CONTENT.
        # The raw stream is A0's monologue JSON, not user-facing text.
        chunk = stream_data.get("chunk", "")
        if chunk:
            state["response_buffer"] = state.get("response_buffer", "") + chunk
            self.agent._agui_state = state
