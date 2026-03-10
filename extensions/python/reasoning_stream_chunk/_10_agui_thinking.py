"""Emit AG-UI Reasoning events for CoT streaming."""
import sys
import uuid
from pathlib import Path
from helpers.extension import Extension
from agent import LoopData

_plugin_root = Path(__file__).resolve().parents[3]
if str(_plugin_root) not in sys.path:
    sys.path.insert(0, str(_plugin_root))


class AGUIThinking(Extension):
    async def execute(self, loop_data: LoopData = LoopData(),
                      stream_data: dict = None, **kwargs):
        if not stream_data:
            return

        from agui_helpers.agui_server import get_run_id_for_context
        from agui_helpers.event_bus import emit
        from agui_helpers.agui_events import (
            encode_reasoning_start,
            encode_reasoning_message_start,
            encode_reasoning_message_content,
        )

        run_id = get_run_id_for_context(self.agent.context.id)
        if not run_id:
            return

        state = getattr(self.agent, '_agui_state', {})

        # Start reasoning block on first chunk
        if not state.get("thinking_started"):
            reasoning_id = str(uuid.uuid4())
            state["reasoning_message_id"] = reasoning_id
            emit(run_id, encode_reasoning_start(reasoning_id))
            emit(run_id, encode_reasoning_message_start(reasoning_id))
            state["thinking_started"] = True
            self.agent._agui_state = state

        chunk = stream_data.get("chunk", "")
        if chunk:
            msg_id = state.get("reasoning_message_id", "unknown")
            emit(run_id, encode_reasoning_message_content(msg_id, chunk))
