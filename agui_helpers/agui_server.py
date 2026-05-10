"""
AG-UI SSE server — accepts RunAgentInput, streams AG-UI events.

Runs as a standalone aiohttp server on a configurable port (default 8401).
CopilotKit and other AG-UI compatible frontends connect here.

Threading model: The aiohttp server runs in the main event loop. Agent
processing runs in DeferredTask threads (separate event loops). The event_bus
uses stdlib queue.Queue for thread-safe communication between them.

Session model: Each AG-UI threadId maps to a persistent AgentContext
(type=USER) that lives across multiple sequential runs within the same
conversation. Contexts are created on first request for a threadId and
evicted after CONTEXT_IDLE_TTL_SECONDS of inactivity. Using USER type (not
BACKGROUND) makes these conversations visible and interactive in A0's native
web UI — a user can continue the same conversation from either interface.
"""

import asyncio
import hmac
import json
import logging
import pathlib
import secrets
import threading
import uuid
from datetime import datetime, timezone

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

# Evict contexts idle for longer than this
CONTEXT_IDLE_TTL_SECONDS = 7200  # 2 hours

_server_instance = None
_server_lock = threading.Lock()

# Module-level mapping: context_id → run_id (for extension callbacks)
_context_run_map: dict[str, str] = {}
_context_run_lock = threading.Lock()

# Active runs: thread_id → run metadata
_active_runs: dict[str, dict] = {}
_active_runs_lock = threading.Lock()

# Persistent contexts: thread_id → AgentContext (survives across turns)
_thread_contexts: dict[str, "AgentContext"] = {}
_thread_last_seen: dict[str, datetime] = {}
_thread_contexts_lock = threading.Lock()


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
        self.app.router.add_post("/reset", self._handle_reset)
        self.app.router.add_options("/reset", self._handle_options)
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

        # Start background TTL eviction loop
        asyncio.create_task(self._ttl_eviction_loop())

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
            return
        if origin == "*":
            response.headers["Access-Control-Allow-Origin"] = "*"
        else:
            req_origin = request.headers.get("Origin", "") if request else ""
            allowed = [o.strip() for o in origin.split(",")]
            if req_origin in allowed:
                response.headers["Access-Control-Allow-Origin"] = req_origin
                response.headers["Vary"] = "Origin"
            else:
                return
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, Accept"
        response.headers["Access-Control-Max-Age"] = "3600"

    async def _health(self, request: web.Request) -> web.Response:
        from agui_helpers.event_bus import get_active_runs
        with _thread_contexts_lock:
            active_threads = len(_thread_contexts)
        resp = web.json_response({
            "status": "ok",
            "protocol": "ag-ui",
            "version": "0.1.0",
            "active_runs": len(get_active_runs()),
            "active_threads": active_threads,
            "running": self._running,
        })
        self._add_cors_headers(resp, request)
        return resp

    def _check_auth(self, request: web.Request) -> bool:
        if not self.auth_token:
            return True
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {self.auth_token}"
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

    async def _handle_reset(self, request: web.Request) -> web.Response:
        """Destroy the A0 context for a threadId. Called when the user starts a new chat."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        thread_id = body.get("threadId", "").strip()
        if not thread_id:
            return web.json_response({"error": "threadId is required"}, status=400)

        context = _pop_thread_context(thread_id)
        if context:
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: _destroy_context(context, thread_id)
            )
            logger.info(f"AG-UI: reset context for thread {thread_id[:8]}")
            resp = web.json_response({"ok": True, "message": "Context reset"})
        else:
            resp = web.json_response({"ok": True, "message": "No active context for threadId"})

        self._add_cors_headers(resp, request)
        return resp

    async def _handle_run(self, request: web.Request) -> web.StreamResponse:
        """Main AG-UI endpoint: accept RunAgentInput, stream SSE events."""
        if not self._check_auth(request):
            return web.json_response(
                {"error": {"message": "Unauthorized", "type": "auth_error"}},
                status=401,
            )

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

        # Extract the latest user message from the full AG-UI message history
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

        try:
            from agent import AgentContext, UserMessage, AgentContextType
            from initialize import initialize_agent
        except ImportError:
            return web.json_response(
                {"error": {"message": "Agent runtime not available", "type": "server_error"}},
                status=500,
            )

        from agui_helpers.event_bus import subscribe, unsubscribe, emit, emit_finish
        from agui_helpers.agui_events import encode_run_started, encode_run_finished, encode_run_error

        queue = subscribe(run_id)

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

        emit(run_id, encode_run_started(thread_id, run_id))

        with _active_runs_lock:
            _active_runs[thread_id] = {
                "run_id": run_id,
                "thread_id": thread_id,
                "tools": tools,
                "state": state,
            }

        async def run_agent():
            context = None
            is_new_context = False
            try:
                # Look up or create a persistent context for this thread.
                # USER type makes the conversation visible in A0's native web UI.
                with _thread_contexts_lock:
                    context = _thread_contexts.get(thread_id)
                    if context is None:
                        cfg = initialize_agent()
                        short_id = thread_id[:8]
                        context = AgentContext(
                            cfg,
                            type=AgentContextType.USER,
                            name=f"AG-UI · {short_id}",
                        )
                        _thread_contexts[thread_id] = context
                        is_new_context = True
                    _thread_last_seen[thread_id] = datetime.now(timezone.utc)

                register_run(context.id, run_id)
                action = "created" if is_new_context else "resumed"
                logger.info(
                    f"AG-UI run {run_id[:8]}: context {context.id} {action} "
                    f"for thread {thread_id[:8]}"
                )

                task = context.communicate(UserMessage(user_message))
                result = await task.result()

                emit(run_id, encode_run_finished(thread_id, run_id, str(result)))

            except Exception as e:
                logger.exception("Agent run failed")
                err_type = type(e).__name__
                emit(run_id, encode_run_error(f"Agent error: {err_type}"))
            finally:
                emit_finish(run_id)
                with _active_runs_lock:
                    _active_runs.pop(thread_id, None)
                if context:
                    unregister_run(context.id)
                # Context is intentionally kept alive for subsequent turns.
                # It will be evicted by TTL or explicitly reset via POST /reset.

        agent_task = asyncio.create_task(run_agent())

        loop = asyncio.get_event_loop()
        try:
            while True:
                try:
                    event = await loop.run_in_executor(
                        None, lambda: queue.get(timeout=30.0)
                    )
                except Exception:
                    await response.write(b": keepalive\n\n")
                    continue

                if event is None:
                    break

                await response.write(event.encode("utf-8"))
        except (ConnectionResetError, ConnectionAbortedError):
            logger.info(f"Client disconnected from run {run_id}")
            agent_task.cancel()
        finally:
            unsubscribe(run_id, queue)

        try:
            await response.write_eof()
        except Exception:
            pass
        return response

    async def _ttl_eviction_loop(self):
        """Periodically evict AgentContexts idle for longer than CONTEXT_IDLE_TTL_SECONDS."""
        while self._running:
            await asyncio.sleep(600)  # check every 10 minutes
            now = datetime.now(timezone.utc)
            to_evict = []

            with _thread_contexts_lock:
                for tid, last_seen in list(_thread_last_seen.items()):
                    if (now - last_seen).total_seconds() > CONTEXT_IDLE_TTL_SECONDS:
                        to_evict.append(tid)

            for tid in to_evict:
                context = _pop_thread_context(tid)
                if context:
                    await asyncio.get_event_loop().run_in_executor(
                        None, lambda ctx=context, t=tid: _destroy_context(ctx, t)
                    )
                    logger.info(f"AG-UI: TTL-evicted idle context for thread {tid[:8]}")


# ── Thread-context helpers ──────────────────────────────────────────────────

def _pop_thread_context(thread_id: str) -> "AgentContext | None":
    """Remove and return the context for a thread (None if not present)."""
    with _thread_contexts_lock:
        context = _thread_contexts.pop(thread_id, None)
        _thread_last_seen.pop(thread_id, None)
    return context


def _destroy_context(context: "AgentContext", thread_id: str):
    """Tear down an AgentContext. Safe to call from any thread."""
    try:
        from agent import AgentContext
        from helpers.persist_chat import remove_chat
        context.reset()
        AgentContext.remove(context.id)
        remove_chat(context.id)
    except Exception:
        logger.debug(f"Context cleanup error for thread {thread_id[:8]}", exc_info=True)


# ── Run-ID ↔ context-ID mapping (used by extension hooks) ──────────────────

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


# ── Server lifecycle ────────────────────────────────────────────────────────

_server_loop = None
_server_thread = None


def get_server() -> AGUIServer | None:
    return _server_instance


def _run_server_loop(loop: asyncio.AbstractEventLoop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _ensure_auth_token(config: dict) -> dict:
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
        saved = plugins.get_plugin_config("agui-provider") or {}
        saved["auth_token"] = token
        plugins.save_plugin_config("agui-provider", "", "", saved)
        logger.info("AG-UI auth token generated and saved to plugin config")
    except Exception:
        logger.warning("Could not persist auth token — it will be regenerated on next restart")

    return config


async def ensure_running(config: dict) -> AGUIServer:
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


def ensure_running_sync(config: dict) -> AGUIServer:
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
