"""Emit AG-UI TextMessageContent for each streaming text chunk."""
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
        from agui_helpers.event_bus import emit
        from agui_helpers.agui_events import (
            encode_reasoning_message_end,
            encode_reasoning_end,
            encode_text_message_start,
            encode_text_message_content,
        )

        run_id = get_run_id_for_context(self.agent.context.id)
        if not run_id:
            return

        state = getattr(self.agent, '_agui_state', {})
        msg_id = state.get("current_message_id", "unknown")

        # Close reasoning block if it was open
        if state.get("thinking_started"):
            reasoning_id = state.get("reasoning_message_id", "unknown")
            emit(run_id, encode_reasoning_message_end(reasoning_id))
            emit(run_id, encode_reasoning_end(reasoning_id))
            state["thinking_started"] = False
            self.agent._agui_state = state

        # Start text message on first chunk
        if not state.get("text_started"):
            emit(run_id, encode_text_message_start(msg_id, "assistant"))
            state["text_started"] = True
            self.agent._agui_state = state

        chunk = stream_data.get("chunk", "")
        if chunk:
            encoded = encode_text_message_content(msg_id, chunk)
            if encoded:  # guards against empty delta
                emit(run_id, encoded)
