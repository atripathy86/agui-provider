"""Emit AG-UI StepStarted when a monologue loop begins."""
import sys
import uuid
from pathlib import Path
from helpers.extension import Extension
from agent import LoopData

_plugin_root = Path(__file__).resolve().parents[3]
if str(_plugin_root) not in sys.path:
    sys.path.insert(0, str(_plugin_root))


class AGUIRunStart(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        from agui_helpers.agui_server import get_run_id_for_context
        from agui_helpers.event_bus import emit
        from agui_helpers.agui_events import encode_step_started

        run_id = get_run_id_for_context(self.agent.context.id)
        if not run_id:
            return

        # Track message ID for this monologue iteration
        msg_id = str(uuid.uuid4())
        if not hasattr(self.agent, '_agui_state'):
            self.agent._agui_state = {}
        self.agent._agui_state["current_message_id"] = msg_id
        self.agent._agui_state["text_started"] = False
        self.agent._agui_state["thinking_started"] = False

        emit(run_id, encode_step_started(f"iteration-{loop_data.iteration}"))
