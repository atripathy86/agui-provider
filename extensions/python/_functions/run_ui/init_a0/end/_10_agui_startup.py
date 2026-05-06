"""Start the AG-UI server at process startup (init_a0/end extension point)."""
import asyncio
import sys
from pathlib import Path
from helpers.extension import Extension
from helpers import plugins

_plugin_root = Path(__file__).resolve().parents[6]
if str(_plugin_root) not in sys.path:
    sys.path.insert(0, str(_plugin_root))


class AGUIStartup(Extension):
    def execute(self, **kwargs):
        config = plugins.get_plugin_config("agui-provider")
        if not config or not config.get("auto_start", True):
            return

        from agui_helpers.agui_server import get_server, ensure_running_sync
        server = get_server()
        if server and server._running:
            return

        ensure_running_sync(config)
