"""
AG-UI SSE server — accepts RunAgentInput, streams AG-UI events.

Runs as a standalone aiohttp server on a configurable port (default 8401).
CopilotKit and other AG-UI compatible frontends connect here.

Threading model: The aiohttp server runs in the main event loop. Agent
processing runs in DeferredTask threads (separate event loops). The event_bus
uses stdlib queue.Queue for thread-safe communication between them.
"""

import asyncio
import hmac
import json
import logging
import pathlib
import secrets
import threading
import uuid

import yaml

_MODEL_CONFIG_DIR = pathlib.Path(__file__).parent.parent.parent / "_model_config"
_PRESETS_FILE = _MODEL_CONFIG_DIR / "presets.yaml"
_CONFIG_FILE = _MODEL_CONFIG_DIR / "config.json"

from aiohttp import web
from aiohttp.web import middleware

logger = logging.getLogger("agui-provider")

# Hardened defaults
MAX_BODY_SIZE = 1 * 1024 * 1024  # 1 MB
MAX_CONCURRENT_RUNS = 5

_server_instance = None
_server_lock = threading.Lock()

# Module-level mapping: context_id → run_id
_context_run_map: dict[str, str] = {}
_context_run_lock = threading.Lock()

# Active runs: thread_id → run metadata
_active_runs: dict[str, dict] = {}
_active_runs_lock = threading.Lock()


class AGUIServer:
    def __init__(self, config: dict):
        self.config = config
        self.port = int(config.get("port", 8401))
        self.auth_token = config.get("auth_token", "")
        self.cors_origins = config.get("cors_origins", "")
        self.max_concurrent_runs = int(config.get("max_concurrent_runs", MAX_CONCURRENT_RUNS))
        self.max_body_size = int(config.get("max_body_size", MAX_BODY_SIZE))
        self.app = None
        self.runner = None
        self._running = False

    @property
    def base_url(self) -> str:
        return f"http://0.0.0.0:{self.port}"

    async def start(self):
        if self._running:
            return

        self.app = web.Application(
            middlewares=[self._cors_middleware],
            client_max_size=self.max_body_size,
        )
        self.app.router.add_get("/health", self._health)
        self.app.router.add_post("/", self._handle_run)
        self.app.router.add_options("/", self._handle_options)
        self.app.router.add_get("/api/presets", self._get_presets)
        self.app.router.add_post("/api/presets", self._save_presets)
        self.app.router.add_post("/api/presets/apply", self._apply_preset)
        self.app.router.add_get("/api/model-config", self._get_model_config)

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "0.0.0.0", self.port)
        await site.start()
        self._running = True
        logger.info(f"AG-UI server listening on 0.0.0.0:{self.port}")

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
        self._running = False

    @middleware
    async def _cors_middleware(self, request: web.Request, handler):
        if request.method == "OPTIONS":
            return self._cors_response(request)
        response = await handler(request)
        self._add_cors_headers(response, request)
        return response

    def _cors_response(self, request: web.Request) -> web.Response:
        resp = web.Response(status=204)
        self._add_cors_headers(resp, request)
        return resp

    def _add_cors_headers(self, response: web.Response, request: web.Request = None):
        origin = self.cors_origins
        if not origin:
            # No CORS configured — don't add headers (same-origin only)
            return
        if origin == "*":
            # Wildcard — reflect any origin
            response.headers["Access-Control-Allow-Origin"] = "*"
        else:
            # Validate request origin against allowlist
            req_origin = request.headers.get("Origin", "") if request else ""
            allowed = [o.strip() for o in origin.split(",")]
            if req_origin in allowed:
                response.headers["Access-Control-Allow-Origin"] = req_origin
                response.headers["Vary"] = "Origin"
            else:
                return  # Origin not allowed — don't add headers
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, Accept"
        response.headers["Access-Control-Max-Age"] = "3600"

    async def _health(self, request: web.Request) -> web.Response:
        from agui_helpers.event_bus import get_active_runs
        resp = web.json_response({
            "status": "ok",
            "protocol": "ag-ui",
            "version": "0.1.0",
            "active_runs": len(get_active_runs()),
            "running": self._running,
        })
        self._add_cors_headers(resp, request)
        return resp

    def _check_auth(self, request: web.Request) -> bool:
        if not self.auth_token:
            return True
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {self.auth_token}"
        # Constant-time comparison to prevent timing attacks
        return hmac.compare_digest(auth.encode(), expected.encode())

    async def _handle_options(self, request: web.Request) -> web.Response:
        return self._cors_response(request)

    async def _get_presets(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            with open(_PRESETS_FILE, "r") as f:
                presets = yaml.safe_load(f)
            resp = web.json_response(presets)
            self._add_cors_headers(resp, request)
            return resp
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _save_presets(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            body = await request.json()
            with open(_PRESETS_FILE, "w") as f:
                yaml.dump(body, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            resp = web.json_response({"status": "ok"})
            self._add_cors_headers(resp, request)
            return resp
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _apply_preset(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            preset = await request.json()
            with open(_CONFIG_FILE, "r") as f:
                config = json.load(f)
            config["chat_model"] = preset.get("chat", config.get("chat_model", {}))
            config["utility_model"] = preset.get("utility", config.get("utility_model", {}))
            with open(_CONFIG_FILE, "w") as f:
                json.dump(config, f, indent=2)
            resp = web.json_response({"status": "ok"})
            self._add_cors_headers(resp, request)
            return resp
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _get_model_config(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            with open(_CONFIG_FILE, "r") as f:
                config = json.load(f)
            resp = web.json_response(config)
            self._add_cors_headers(resp, request)
            return resp
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_run(self, request: web.Request) -> web.StreamResponse:
        """Main AG-UI endpoint: accept RunAgentInput, stream SSE events."""
        if not self._check_auth(request):
            return web.json_response(
                {"error": {"message": "Unauthorized", "type": "auth_error"}},
                status=401,
            )

        # Enforce concurrent run limit
        with _active_runs_lock:
            if len(_active_runs) >= self.max_concurrent_runs:
                return web.json_response(
                    {"error": {"message": "Too many concurrent runs", "type": "rate_limit"}},
                    status=429,
                )

        try:
            body = await request.json()
        except web.HTTPRequestEntityTooLarge:
            return web.json_response(
                {"error": {"message": "Request body too large", "type": "invalid_request"}},
                status=413,
            )
        except Exception:
            return web.json_response(
                {"error": {"message": "Invalid JSON", "type": "invalid_request"}},
                status=400,
            )

        thread_id = body.get("threadId", body.get("thread_id", str(uuid.uuid4())))
        run_id = body.get("runId", body.get("run_id", str(uuid.uuid4())))
        messages = body.get("messages", [])
        tools = body.get("tools", [])
        state = body.get("state")
        forwarded_props = body.get("forwardedProps", body.get("forwarded_props", {}))

        # Extract user message from AG-UI messages
        user_message = ""
        for msg in reversed(messages):
            role = msg.get("role", "")
            if role == "user":
                content = msg.get("content", "")
                if isinstance(content, list):
                    parts = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            parts.append(part.get("text", ""))
                        elif isinstance(part, str):
                            parts.append(part)
                    user_message = "\n".join(parts)
                else:
                    user_message = str(content)
                break

        if not user_message:
            return web.json_response(
                {"error": {"message": "No user message found in messages", "type": "invalid_request"}},
                status=400,
            )

        # Import A0 internals
        try:
            from agent import AgentContext, UserMessage, AgentContextType
            from initialize import initialize_agent
            from helpers.persist_chat import remove_chat
        except ImportError:
            return web.json_response(
                {"error": {"message": "Agent runtime not available", "type": "server_error"}},
                status=500,
            )

        # Subscribe to events for this run (thread-safe stdlib queue)
        from agui_helpers.event_bus import subscribe, unsubscribe, emit, emit_finish
        from agui_helpers.agui_events import encode_run_started, encode_run_finished, encode_run_error

        queue = subscribe(run_id)

        # Start SSE response
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        self._add_cors_headers(response, request)
        await response.prepare(request)

        # Emit RunStarted
        emit(run_id, encode_run_started(thread_id, run_id))

        # Store run metadata so extensions can find it
        with _active_runs_lock:
            _active_runs[thread_id] = {
                "run_id": run_id,
                "thread_id": thread_id,
                "tools": tools,
                "state": state,
            }

        # Kick off agent in background
        async def run_agent():
            context = None
            try:
                # Create a fresh BACKGROUND context (same pattern as A2A handler)
                cfg = initialize_agent()
                context = AgentContext(cfg, type=AgentContextType.BACKGROUND)

                # Register context_id → run_id mapping for extensions
                register_run(context.id, run_id)

                logger.info(f"AG-UI run {run_id}: context {context.id} created")

                task = context.communicate(UserMessage(user_message))
                result = await task.result()

                emit(run_id, encode_run_finished(thread_id, run_id, str(result)))

            except Exception as e:
                logger.exception("Agent run failed")
                # Sanitize: only send the exception class name, not full traceback
                err_type = type(e).__name__
                emit(run_id, encode_run_error(f"Agent error: {err_type}"))
            finally:
                emit_finish(run_id)
                with _active_runs_lock:
                    _active_runs.pop(thread_id, None)

                # Clean up context (same pattern as A2A handler)
                if context:
                    unregister_run(context.id)
                    try:
                        context.reset()
                        AgentContext.remove(context.id)
                        remove_chat(context.id)
                    except Exception:
                        logger.debug(f"Context cleanup error for {context.id}", exc_info=True)

        agent_task = asyncio.create_task(run_agent())

        # Stream events from thread-safe queue to SSE
        loop = asyncio.get_event_loop()
        try:
            while True:
                try:
                    # Read from stdlib queue in executor (non-blocking for event loop)
                    event = await loop.run_in_executor(
                        None, lambda: queue.get(timeout=30.0)
                    )
                except Exception:
                    # queue.Empty on timeout — send keepalive
                    await response.write(b": keepalive\n\n")
                    continue

                if event is None:  # Sentinel — run finished
                    break

                await response.write(event.encode("utf-8"))
        except (ConnectionResetError, ConnectionAbortedError):
            logger.info(f"Client disconnected from run {run_id}")
            agent_task.cancel()
        finally:
            unsubscribe(run_id, queue)

        try:
            await response.write_eof()
        except (ConnectionResetError, ConnectionAbortedError, Exception):
            pass  # Client already gone
        return response


def register_run(context_id: str, run_id: str):
    """Register context_id → run_id mapping. Called before communicate()."""
    with _context_run_lock:
        _context_run_map[context_id] = run_id


def unregister_run(context_id: str):
    """Remove context_id → run_id mapping. Called after run completes."""
    with _context_run_lock:
        _context_run_map.pop(context_id, None)


def get_run_id_for_context(context_id: str) -> str | None:
    """Look up the AG-UI run_id for an A0 context. Called by extensions."""
    with _context_run_lock:
        return _context_run_map.get(context_id)


def get_active_run_ids() -> list[str]:
    with _active_runs_lock:
        return [m["run_id"] for m in _active_runs.values()]


# Module-level server management

_server_loop = None  # Dedicated event loop for aiohttp
_server_thread = None  # Background thread running the loop


def get_server() -> AGUIServer | None:
    return _server_instance


def _run_server_loop(loop: asyncio.AbstractEventLoop):
    """Run the dedicated event loop in a background thread."""
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _ensure_auth_token(config: dict) -> dict:
    """Use AGUI_TOKEN env var if set, else use config value, else auto-generate."""
    import os
    env_token = os.environ.get("AGUI_TOKEN", "").strip()
    if env_token:
        config["auth_token"] = env_token
        return config

    if config.get("auth_token"):
        return config

    token = secrets.token_urlsafe(32)
    config["auth_token"] = token

    try:
        from helpers import plugins
        # Load existing saved config, merge in the token, save back
        saved = plugins.get_plugin_config("agui-provider") or {}
        saved["auth_token"] = token
        plugins.save_plugin_config("agui-provider", "", "", saved)
        logger.info("AG-UI auth token generated and saved to plugin config")
    except Exception:
        logger.warning("Could not persist auth token — it will be regenerated on next restart")

    return config


async def ensure_running(config: dict) -> AGUIServer:
    """Start the AG-UI server in a dedicated background thread.

    aiohttp needs a persistent event loop. Flask/Starlette request handlers
    have transient async contexts, so we spin up a dedicated thread with its
    own loop that stays alive for the lifetime of the process.
    """
    global _server_instance, _server_loop, _server_thread

    # Generate auth token on first run if not configured
    config = _ensure_auth_token(config)

    with _server_lock:
        if _server_instance is None:
            _server_instance = AGUIServer(config)

    if _server_instance._running:
        return _server_instance

    # Create a dedicated event loop + thread
    if _server_loop is None or _server_loop.is_closed():
        _server_loop = asyncio.new_event_loop()
        _server_thread = threading.Thread(
            target=_run_server_loop,
            args=(_server_loop,),
            daemon=True,
            name="agui-server",
        )
        _server_thread.start()

    # Schedule server.start() on the dedicated loop and wait for it
    future = asyncio.run_coroutine_threadsafe(
        _server_instance.start(), _server_loop
    )
    future.result(timeout=10)  # Block until started (max 10s)
    return _server_instance


def ensure_running_sync(config: dict) -> AGUIServer:
    """Synchronous entry point for startup extensions (no event loop required)."""
    global _server_instance, _server_loop, _server_thread

    config = _ensure_auth_token(config)

    with _server_lock:
        if _server_instance is None:
            _server_instance = AGUIServer(config)

    if _server_instance._running:
        return _server_instance

    if _server_loop is None or _server_loop.is_closed():
        _server_loop = asyncio.new_event_loop()
        _server_thread = threading.Thread(
            target=_run_server_loop,
            args=(_server_loop,),
            daemon=True,
            name="agui-server",
        )
        _server_thread.start()

    future = asyncio.run_coroutine_threadsafe(
        _server_instance.start(), _server_loop
    )
    future.result(timeout=10)
    return _server_instance
