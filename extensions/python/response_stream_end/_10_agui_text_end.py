"""Emit AG-UI TextMessageEnd when response streaming finishes."""
import sys
from pathlib import Path
from helpers.extension import Extension
from agent import LoopData

_plugin_root = Path(__file__).resolve().parents[3]
if str(_plugin_root) not in sys.path:
    sys.path.insert(0, str(_plugin_root))


class AGUITextEnd(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        from agui_helpers.agui_server import get_run_id_for_context
        from agui_helpers.event_bus import emit
        from agui_helpers.agui_events import (
            encode_text_message_end, encode_reasoning_message_end, encode_reasoning_end,
        )

        run_id = get_run_id_for_context(self.agent.context.id)
        if not run_id:
            return

        state = getattr(self.agent, '_agui_state', {})

        # Close reasoning if still open
        if state.get("thinking_started"):
            reasoning_id = state.get("reasoning_message_id", "unknown")
            emit(run_id, encode_reasoning_message_end(reasoning_id))
            emit(run_id, encode_reasoning_end(reasoning_id))
            state["thinking_started"] = False

        # Close text message if it was started
        if state.get("text_started"):
            msg_id = state.get("current_message_id", "unknown")
            emit(run_id, encode_text_message_end(msg_id))
            state["text_started"] = False

        self.agent._agui_state = state
