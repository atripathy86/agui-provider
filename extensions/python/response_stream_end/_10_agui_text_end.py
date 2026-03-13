"""Finalize response stream -- extract headline from monologue JSON if available."""
import json
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
            encode_reasoning_message_end, encode_reasoning_end,
            encode_custom,
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

        # Parse buffered monologue JSON and emit headline as a status event
        buffer = state.get("response_buffer", "")
        if buffer:
            try:
                monologue = json.loads(buffer)
                headline = monologue.get("headline", "")
                if headline:
                    emit(run_id, encode_custom("agui:status", {"headline": headline}))
            except (json.JSONDecodeError, AttributeError):
                pass  # Not JSON -- response tool handles the text
            state["response_buffer"] = ""

        # text_started should be False (we no longer emit TEXT_MESSAGE_START in stream)
        # but clean up just in case
        state["text_started"] = False
        self.agent._agui_state = state
