"""Clean up AG-UI state when the process chain ends."""
from helpers.extension import Extension


class AGUIRunEnd(Extension):
    async def execute(self, data: dict = None, **kwargs):
        state = getattr(self.agent, '_agui_state', None)
        if state:
            del self.agent._agui_state
