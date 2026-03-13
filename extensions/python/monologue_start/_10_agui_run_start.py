"""Monologue start hook — no-op.

Step lifecycle (STEP_STARTED/STEP_FINISHED) is handled per-iteration in
message_loop_start/_20_agui_step_start.py and message_loop_end/_10_agui_step_end.py.
"""
from helpers.extension import Extension
from agent import LoopData


class AGUIRunStart(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        pass
