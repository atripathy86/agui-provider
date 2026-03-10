"""Auto-start the AG-UI server on first agent message loop."""
import sys
from pathlib import Path
from helpers.extension import Extension
from helpers import plugins
from agent import LoopData

_plugin_root = Path(__file__).resolve().parents[3]
if str(_plugin_root) not in sys.path:
    sys.path.insert(0, str(_plugin_root))


class AGUIAutoStart(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        config = plugins.get_plugin_config("agui-provider", self.agent)
        if not config or not config.get("auto_start", True):
            return

        from agui_helpers.agui_server import get_server, ensure_running
        server = get_server()
        if server and server._running:
            return

        await ensure_running(config)
