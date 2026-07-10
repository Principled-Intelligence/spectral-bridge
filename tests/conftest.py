import json
import threading
import time

import pytest
import uvicorn
import websockets


# ── Minimal ASGI echo target ──────────────────────────────────────────────────

async def _echo_asgi(scope, receive, send):
    """Accepts any POST and returns a minimal valid completion response."""
    if scope["type"] == "http":
        await receive()  # consume request body
        body = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})


async def _read_asgi_body(receive) -> bytes:
    """Read a full ASGI http.request body (handles chunked more_body)."""
    chunks = []
    more_body = True
    while more_body:
        message = await receive()
        chunks.append(message.get("body", b""))
        more_body = message.get("more_body", False)
    return b"".join(chunks)


async def _responses_echo_asgi(scope, receive, send):
    """Accepts any POST and echoes `input` into a minimal Responses output[]."""
    if scope["type"] == "http":
        raw = await _read_asgi_body(receive)
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {}
        text = payload.get("input", "")
        if not isinstance(text, str):
            text = json.dumps(text)
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    }
                ]
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})


# ── Uvicorn background server helper ─────────────────────────────────────────

class _BackgroundServer(uvicorn.Server):
    def install_signal_handlers(self) -> None:
        pass  # prevent signal handler installation in non-main thread


def _start_background_server(app, *, host: str = "127.0.0.1") -> tuple[_BackgroundServer, threading.Thread, str]:
    """Start an ASGI app in a daemon thread. Returns (server, thread, url)."""
    config = uvicorn.Config(app, host=host, port=0, log_level="error")
    server = _BackgroundServer(config=config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("background server failed to start within 10s")
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, thread, f"http://{host}:{port}"


# ── Session fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def echo_target_url():
    server, thread, url = _start_background_server(_echo_asgi)
    yield url
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def adapter_url(echo_target_url):
    import spectral_bridge_passthrough.app as adapter_mod
    adapter_mod.TARGET_URL = echo_target_url
    server, thread, url = _start_background_server(adapter_mod.app)
    yield url
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def responses_target_url():
    server, thread, url = _start_background_server(_responses_echo_asgi)
    yield url
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def responses_adapter_url(responses_target_url):
    import spectral_bridge_responses.app as adapter_mod
    adapter_mod.TARGET_URL = responses_target_url
    server, thread, url = _start_background_server(adapter_mod.app)
    yield url
    server.should_exit = True
    thread.join(timeout=5)


# ── Per-test ASGI server factory ──────────────────────────────────────────────

@pytest.fixture
def make_asgi_server():
    """
    Factory fixture. Call `url = make_asgi_server(app)` to start an arbitrary
    ASGI app in a background thread; used for adapters with custom behaviour
    (e.g. a deliberately slow target). All servers stop when the test ends.
    """
    started = []

    def factory(app) -> str:
        server, thread, url = _start_background_server(app)
        started.append((server, thread))
        return url

    yield factory

    for server, thread in started:
        server.should_exit = True
        thread.join(timeout=5)


# ── Per-test relay server factory ─────────────────────────────────────────────

@pytest.fixture
async def make_relay_server():
    """
    Factory fixture. Call `url = await make_relay_server(handler)` inside a test
    to spin up a fresh WebSocket server whose behaviour is defined by `handler`.
    All servers are closed when the test ends.
    """
    servers = []

    async def factory(handler):
        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        servers.append(server)
        return f"ws://127.0.0.1:{port}"

    yield factory

    for server in servers:
        server.close()
        await server.wait_closed()
