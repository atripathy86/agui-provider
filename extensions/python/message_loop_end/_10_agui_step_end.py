"""Emit AG-UI StepFinished at end of each message loop iteration."""
import sys
from pathlib import Path
from helpers.extension import Extension
from agent import LoopData

_plugin_root = Path(__file__).resolve().parents[3]
if str(_plugin_root) not in sys.path:
    sys.path.insert(0, str(_plugin_root))


class AGUIStepEnd(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        from agui_helpers.agui_server import get_run_id_for_context
        from agui_helpers.event_bus import emit
        from agui_helpers.agui_events import encode_step_finished

        run_id = get_run_id_for_context(self.agent.context.id)
        if not run_id:
            return

        emit(run_id, encode_step_finished(f"iteration-{loop_data.iteration}"))
