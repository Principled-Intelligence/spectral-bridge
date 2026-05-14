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
