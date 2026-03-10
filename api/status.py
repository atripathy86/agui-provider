"""AG-UI provider status API handler."""
import sys
from pathlib import Path
from helpers.api import ApiHandler, Request, Response

_plugin_root = Path(__file__).parent.parent
if str(_plugin_root) not in sys.path:
    sys.path.insert(0, str(_plugin_root))


class StatusHandler(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        action = input.get("action", "status")

        if action == "status":
            return self._status()
        elif action == "start":
            return await self._start()
        elif action == "stop":
            return await self._stop()
        return {"ok": False, "error": f"Unknown action: {action}"}

    def _status(self) -> dict:
        from agui_helpers.agui_server import get_server
        from agui_helpers.event_bus import get_active_runs

        server = get_server()
        if not server or not server._running:
            return {"ok": True, "running": False}

        return {
            "ok": True,
            "running": True,
            "port": server.port,
            "url": server.base_url,
            "active_runs": get_active_runs(),
            "active_run_count": len(get_active_runs()),
            "config": {
                "auth_enabled": bool(server.auth_token),
                "auth_token": server.auth_token,
                "cors_origins": server.cors_origins,
                "max_concurrent_runs": server.max_concurrent_runs,
                "max_body_size": server.max_body_size,
            },
        }

    async def _start(self) -> dict:
        from helpers import plugins
        from agui_helpers.agui_server import ensure_running
        config = plugins.get_plugin_config("agui-provider") or {}
        server = await ensure_running(config)
        return {"ok": True, "running": True, "url": server.base_url}

    async def _stop(self) -> dict:
        from agui_helpers.agui_server import get_server
        server = get_server()
        if server:
            await server.stop()
        return {"ok": True, "running": False}
