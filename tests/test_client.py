import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from websockets.exceptions import ConnectionClosedError

import spectral_bridge.client as client_mod
from spectral_bridge.client import (
    DEFAULT_REQUEST_TIMEOUT,
    RelayClient,
    _headers_for_adapter,
)

# ── _headers_for_adapter ─────────────────────────────────────────────────────


def test_headers_for_adapter_strips_hop_by_hop():
    headers = {
        "connection": "keep-alive",
        "keep-alive": "timeout=5",
        "proxy-authenticate": "Basic",
        "proxy-authorization": "Basic abc",
        "te": "trailers",
        "trailers": "X-Foo",
        "transfer-encoding": "chunked",
        "upgrade": "websocket",
        "host": "relay.example.com",
        "content-length": "42",
    }
    result = _headers_for_adapter(headers)
    assert result == {}


def test_headers_for_adapter_preserves_other_headers():
    headers = {
        "content-type": "application/json",
        "authorization": "Bearer token",
        "x-custom": "value",
    }
    result = _headers_for_adapter(headers)
    assert result == headers


def test_headers_for_adapter_mixed():
    headers = {
        "content-type": "application/json",
        "host": "relay.example.com",
        "x-request-id": "abc123",
        "transfer-encoding": "chunked",
    }
    result = _headers_for_adapter(headers)
    assert result == {"content-type": "application/json", "x-request-id": "abc123"}


def test_headers_for_adapter_non_dict_input():
    assert _headers_for_adapter(None) == {}
    assert _headers_for_adapter("not-a-dict") == {}
    assert _headers_for_adapter(42) == {}


def test_headers_for_adapter_non_string_keys_dropped():
    headers = {42: "value", "host": "relay.example.com", "x-custom": "keep"}
    result = _headers_for_adapter(headers)
    assert result == {"x-custom": "keep"}


# ── RelayClient URL validation ────────────────────────────────────────────────


def test_validate_wss_accepted():
    # Should not raise
    RelayClient("wss://relay.example.com/connect", "key", "http://localhost:8000")


def test_validate_ws_rejected_without_flag():
    with pytest.raises(ValueError, match="wss://"):
        RelayClient("ws://localhost:9000/connect", "key", "http://localhost:8000")


def test_validate_ws_accepted_with_insecure_flag():
    # Should not raise
    RelayClient(
        "ws://localhost:9000/connect",
        "key",
        "http://localhost:8000",
        insecure_relay=True,
    )


def test_validate_http_rejected():
    with pytest.raises(ValueError, match="websocket scheme"):
        RelayClient("http://relay.example.com/connect", "key", "http://localhost:8000")


def test_validate_adapter_loopback_hosts_accepted():
    # Every loopback form the client must accept without raising.
    for adapter in (
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://[::1]:8000",
        "http://127.0.0.2:8000",  # all of 127.0.0.0/8 is loopback
        "https://127.0.0.1:8000",
    ):
        RelayClient("wss://relay.example.com/connect", "key", adapter)


def test_validate_adapter_non_loopback_rejected():
    with pytest.raises(ValueError, match="loopback"):
        RelayClient(
            "wss://relay.example.com/connect", "key", "http://evil.example.com"
        )


def test_validate_adapter_non_http_scheme_rejected():
    with pytest.raises(ValueError, match="http"):
        RelayClient(
            "wss://relay.example.com/connect", "key", "ftp://127.0.0.1:8000"
        )


def test_validate_adapter_non_loopback_allowed_with_insecure_flag():
    # Should not raise: explicit opt-out.
    RelayClient(
        "wss://relay.example.com/connect",
        "key",
        "http://evil.example.com",
        insecure_adapter=True,
    )


def test_validate_max_bytes_zero_raises():
    with pytest.raises(ValueError, match="max_ws_message_bytes"):
        RelayClient(
            "wss://relay.example.com/connect",
            "key",
            "http://localhost:8000",
            max_ws_message_bytes=0,
        )


# ── Integration helpers ───────────────────────────────────────────────────────

DEFAULT_BODY = {"model": "test", "messages": [{"role": "user", "content": "hello"}]}


def _client(relay_url: str, adapter_url: str, **kwargs) -> RelayClient:
    """Build a RelayClient with insecure_relay=True for the ws:// test URLs."""
    return RelayClient(relay_url, "any-key", adapter_url, insecure_relay=True, **kwargs)


def request_frame(
    request_id: str,
    body: dict | None = None,
    path: str = "/v1/chat/completions",
) -> dict:
    """A relay 'request' frame for `request_id` (default chat-completion body)."""
    return {
        "type": "request",
        "request_id": request_id,
        "payload": {
            "method": "POST",
            "path": path,
            "headers": {},
            "body": DEFAULT_BODY if body is None else body,
        },
    }


@asynccontextmanager
async def running_client(client: RelayClient):
    """Run `client.run()` in the background, cancelling it cleanly on exit."""
    task = asyncio.create_task(client.run())
    try:
        yield task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
def roundtrip(make_relay_server):
    """
    Drive the real client against a scripted relay. The relay sends one request
    frame per id; the client forwards each to `adapter_url` and the relay collects
    the responses. Returns {request_id: response_frame}.

    Only the relay is faked — the client and the adapter behind `adapter_url` are
    the real thing, so swapping `adapter_url` (echo, unreachable, slow) is all it
    takes to cover the different forwarding outcomes.
    """

    async def _roundtrip(
        adapter_url: str,
        request_ids: list[str],
        *,
        path: str = "/v1/chat/completions",
        body: dict | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> dict[str, dict]:
        responses: dict[str, dict] = {}
        done = asyncio.Event()

        async def relay(ws):
            await ws.send(json.dumps({"type": "connected"}))
            # send every request before reading any response, so concurrent
            # handling is exercised when more than one id is requested
            for request_id in request_ids:
                await ws.send(json.dumps(request_frame(request_id, body=body, path=path)))
            for _ in request_ids:
                frame = json.loads(await ws.recv())
                responses[frame["request_id"]] = frame
            done.set()
            await ws.recv()  # hold the connection open until the client is torn down

        url = await make_relay_server(relay)
        client = _client(url, adapter_url, request_timeout=request_timeout)
        async with running_client(client):
            await asyncio.wait_for(done.wait(), timeout=15)
        return responses

    return _roundtrip


# ── Connection and authentication ─────────────────────────────────────────────


async def test_connected_frame_accepted(make_relay_server, adapter_url):
    """Client connects and stays running after receiving the connected frame."""
    ready = asyncio.Event()

    async def relay(ws):
        await ws.send(json.dumps({"type": "connected"}))
        ready.set()
        await ws.recv()

    url = await make_relay_server(relay)
    async with running_client(_client(url, adapter_url)):
        await asyncio.wait_for(ready.wait(), timeout=5)


async def test_auth_failure_4001_stops_client(make_relay_server, adapter_url):
    """Server closes with code 4001 → run() returns without retrying."""

    async def relay(ws):
        await ws.close(code=4001, reason="unauthorized")

    url = await make_relay_server(relay)
    # run() must complete (not loop forever); the timeout proves it stops
    await asyncio.wait_for(_client(url, adapter_url).run(), timeout=5)


async def test_auth_failure_401_stops_client(adapter_url):
    """HTTP 401 during WebSocket upgrade → run() returns without retrying."""

    async def serve_401(reader, writer):
        await reader.read(4096)
        writer.write(
            b"HTTP/1.1 401 Unauthorized\r\n"
            b"Content-Length: 0\r\n"
            b"Connection: close\r\n\r\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(serve_401, "127.0.0.1", 0)
    url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        await asyncio.wait_for(_client(url, adapter_url).run(), timeout=5)
    finally:
        server.close()
        await server.wait_closed()


async def test_unexpected_first_frame_reconnects(make_relay_server, adapter_url):
    """An unexpected first frame raises ProtocolError; the retry loop reconnects."""
    connection_count = 0
    reconnected = asyncio.Event()

    async def relay(ws):
        nonlocal connection_count
        connection_count += 1
        if connection_count == 1:
            await ws.send(json.dumps({"type": "hello"}))  # not "connected"
        else:
            reconnected.set()
            await ws.recv()

    url = await make_relay_server(relay)
    async with running_client(_client(url, adapter_url)):
        await asyncio.wait_for(reconnected.wait(), timeout=10)
    assert connection_count == 2


# ── Request forwarding ────────────────────────────────────────────────────────


async def test_request_forwarded_and_response_echoed(roundtrip, adapter_url):
    """Client forwards a request to the adapter and echoes the response, id intact."""
    response = (await roundtrip(adapter_url, ["req-1"]))["req-1"]
    assert response["type"] == "response"
    assert response["request_id"] == "req-1"
    assert response["payload"]["status"] == 200
    assert response["payload"]["body"]["choices"][0]["message"]["content"] == "ok"


async def test_adapter_unavailable_sends_503(roundtrip):
    """An unreachable adapter yields a 503 response frame."""
    # Bind on port 0 then close immediately, so nothing is listening there.
    dead = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    dead_url = f"http://127.0.0.1:{dead.sockets[0].getsockname()[1]}"
    dead.close()
    await dead.wait_closed()

    response = (await roundtrip(dead_url, ["req-dead"]))["req-dead"]
    assert response["payload"]["status"] == 503


async def test_responses_frame_round_trips(roundtrip, responses_adapter_url):
    """A path:/v1/responses frame reaches the pass-through adapter's
    /v1/responses route (backed by the fake Responses target) and echoes back."""
    response = (
        await roundtrip(
            responses_adapter_url,
            ["resp-1"],
            path="/v1/responses",
            body={"model": "spectral-internal", "input": "ping"},
        )
    )["resp-1"]
    assert response["type"] == "response"
    assert response["request_id"] == "resp-1"
    assert response["payload"]["status"] == 200
    output = response["payload"]["body"]["output"]
    assert output[0]["content"][0]["text"] == "ping"


# ── Frame path forwarding ─────────────────────────────────────────────────────


def _path_recording_app(recorder: list):
    """ASGI app that records the request path and returns a minimal 200 JSON."""

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        await receive()
        recorder.append(scope["path"])
        body = b'{"ok": true}'
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

    return app


async def _drive_one_frame(make_relay_server, adapter_url: str, frame: dict) -> dict:
    """Send a single request frame to a real client; return the response frame."""
    result: dict = {}
    done = asyncio.Event()

    async def relay(ws):
        await ws.send(json.dumps({"type": "connected"}))
        await ws.send(json.dumps(frame))
        result["response"] = json.loads(await ws.recv())
        done.set()
        await ws.recv()  # hold open until the client is torn down

    url = await make_relay_server(relay)
    async with running_client(_client(url, adapter_url)):
        await asyncio.wait_for(done.wait(), timeout=15)
    return result["response"]


async def test_frame_path_forwarded_to_adapter(make_relay_server, make_asgi_server):
    """A frame carrying path:/v1/responses forwards to the adapter's /v1/responses."""
    recorder: list = []
    target_url = make_asgi_server(_path_recording_app(recorder))
    frame = {
        "type": "request",
        "request_id": "resp-1",
        "payload": {
            "method": "POST",
            "path": "/v1/responses",
            "headers": {},
            "body": {"input": "hi"},
        },
    }
    response = await _drive_one_frame(make_relay_server, target_url, frame)
    assert response["payload"]["status"] == 200
    assert recorder == ["/v1/responses"]


async def test_absent_frame_path_defaults_to_chat(make_relay_server, make_asgi_server):
    """A frame with no path field still forwards to /v1/chat/completions."""
    recorder: list = []
    target_url = make_asgi_server(_path_recording_app(recorder))
    frame = {
        "type": "request",
        "request_id": "chat-1",
        "payload": {"method": "POST", "headers": {}, "body": DEFAULT_BODY},  # no "path"
    }
    response = await _drive_one_frame(make_relay_server, target_url, frame)
    assert response["payload"]["status"] == 200
    assert recorder == ["/v1/chat/completions"]


async def test_frame_path_userinfo_injection_rejected(
    make_relay_server, make_asgi_server
):
    """A path that would smuggle a host via userinfo is rejected with 404 and
    never reaches the adapter (no off-loopback request is made)."""
    recorder: list = []
    target_url = make_asgi_server(_path_recording_app(recorder))
    frame = {
        "type": "request",
        "request_id": "evil-1",
        "payload": {
            "method": "POST",
            "path": "@evil.example.com/v1/chat/completions",
            "headers": {},
            "body": {"input": "hi"},
        },
    }
    response = await _drive_one_frame(make_relay_server, target_url, frame)
    assert response["payload"]["status"] == 404
    assert response["payload"]["body"]["error"]["message"] == "unknown adapter path"
    assert recorder == []


async def test_frame_path_traversal_rejected(make_relay_server, make_asgi_server):
    """A traversal path is rejected with 404 and never reaches the adapter."""
    recorder: list = []
    target_url = make_asgi_server(_path_recording_app(recorder))
    frame = {
        "type": "request",
        "request_id": "trav-1",
        "payload": {
            "method": "POST",
            "path": "/../..",
            "headers": {},
            "body": {},
        },
    }
    response = await _drive_one_frame(make_relay_server, target_url, frame)
    assert response["payload"]["status"] == 404
    assert recorder == []


async def test_frame_path_non_string_rejected(make_relay_server, make_asgi_server):
    """A non-string path (JSON array/object) is rejected with 404 rather than
    crashing the handler and leaving the request unanswered."""
    recorder: list = []
    target_url = make_asgi_server(_path_recording_app(recorder))
    frame = {
        "type": "request",
        "request_id": "nonstr-1",
        "payload": {
            "method": "POST",
            "path": [],
            "headers": {},
            "body": {},
        },
    }
    response = await _drive_one_frame(make_relay_server, target_url, frame)
    assert response["payload"]["status"] == 404
    assert recorder == []


# ── Resilience ────────────────────────────────────────────────────────────────


async def test_concurrent_requests_both_answered(roundtrip, adapter_url):
    """Two requests sent before either response is read are both answered."""
    responses = await roundtrip(adapter_url, ["req-a", "req-b"])
    assert set(responses) == {"req-a", "req-b"}
    assert all(r["payload"]["status"] == 200 for r in responses.values())


async def test_reconnects_after_disconnect(make_relay_server, adapter_url):
    """A clean server close (code 1001) triggers a reconnect."""
    connection_count = 0
    reconnected = asyncio.Event()

    async def relay(ws):
        nonlocal connection_count
        connection_count += 1
        if connection_count == 1:
            await ws.send(json.dumps({"type": "connected"}))
            await ws.close(code=1001, reason="going away")
        else:
            reconnected.set()
            await ws.recv()

    url = await make_relay_server(relay)
    async with running_client(_client(url, adapter_url)):
        await asyncio.wait_for(reconnected.wait(), timeout=10)
    assert connection_count == 2


async def test_abnormal_closure_reconnects(make_relay_server, adapter_url):
    """
    An infra-style disconnect — TCP severed with no close frame (the 1006
    "no close frame received or sent" case, e.g. a proxy or Cloud Run request-
    timeout recycle) — is transient: the client reconnects rather than stopping.
    """
    connection_count = 0
    reconnected = asyncio.Event()

    async def relay(ws):
        nonlocal connection_count
        connection_count += 1
        if connection_count == 1:
            await ws.send(json.dumps({"type": "connected"}))
            ws.transport.abort()  # sever TCP with no close frame -> client sees 1006
        else:
            reconnected.set()
            await ws.recv()

    url = await make_relay_server(relay)
    async with running_client(_client(url, adapter_url)):
        await asyncio.wait_for(reconnected.wait(), timeout=10)
    assert connection_count == 2


# ── Reconnect backoff ─────────────────────────────────────────────────────────


async def _capture_backoff_delays(monkeypatch, *, stable_threshold: float) -> list:
    """
    Drive run()'s reconnect loop with an always-failing connection, capturing the
    backoff delay used on each iteration. _connect is stubbed to fail immediately
    and asyncio.sleep is stubbed to record (not wait), so no real network or time
    is involved; only the loop's delay arithmetic is exercised.
    """
    client = _client("ws://relay.test/connect", "http://localhost:1")
    monkeypatch.setattr(client_mod, "BACKOFF_SCHEDULE", [1, 2, 4, 8])
    monkeypatch.setattr(client_mod, "STABLE_CONNECTION_THRESHOLD", stable_threshold)

    delays: list = []

    async def fake_connect():
        # abnormal closure, no close frame -> falls through to the backoff branch
        raise ConnectionClosedError(None, None)

    # Patching asyncio.sleep is global, so it also hits background servers (e.g. the
    # session-scoped adapter) running on other event loops. Record and short-circuit
    # only this loop's backoff sleeps; let every other loop sleep for real.
    loop = asyncio.get_running_loop()
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if asyncio.get_running_loop() is not loop:
            await real_sleep(delay)
            return
        delays.append(delay)
        if len(delays) >= 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(client, "_connect", fake_connect)
    monkeypatch.setattr(client_mod.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await client.run()
    return delays


async def test_backoff_escalates_without_stable_connection(monkeypatch):
    """Rapid failures (each connection well under the stable threshold) escalate."""
    delays = await _capture_backoff_delays(monkeypatch, stable_threshold=9999)
    assert delays == [1, 2, 4, 8]


async def test_backoff_resets_after_stable_connection(monkeypatch):
    """
    Each connection that stays up past the stable threshold resets the counter,
    so the backoff never escalates — the regression behind the 4→8→30→30 climb
    seen across healthy ~5-minute sessions.
    """
    # threshold 0 => every connection counts as stable => reset on every iteration
    delays = await _capture_backoff_delays(monkeypatch, stable_threshold=0)
    assert delays == [1, 1, 1, 1]


# ── Long-running completions ──────────────────────────────────────────────────


def _slow_completion_app(delay: float):
    """ASGI app that waits `delay` seconds before returning a valid completion."""

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        await receive()
        await asyncio.sleep(delay)
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

    return app


async def test_short_request_timeout_times_out_slow_adapter(
    roundtrip, make_asgi_server
):
    """A completion slower than request_timeout fails with 503 (adapter timed out)."""
    slow_url = make_asgi_server(_slow_completion_app(0.5))
    response = (await roundtrip(slow_url, ["slow"], request_timeout=0.2))["slow"]
    assert response["payload"]["status"] == 503


async def test_generous_request_timeout_allows_slow_adapter(
    roundtrip, make_asgi_server
):
    """The same slow completion succeeds when request_timeout is generous."""
    slow_url = make_asgi_server(_slow_completion_app(0.5))
    response = (await roundtrip(slow_url, ["slow"], request_timeout=5.0))["slow"]
    assert response["payload"]["status"] == 200
    assert response["payload"]["body"]["choices"][0]["message"]["content"] == "ok"


# ── Frame size limit ──────────────────────────────────────────────────────────


async def test_payload_too_big_stops_client(make_relay_server):
    """A frame larger than max_ws_message_bytes → PayloadTooBig → run() returns."""
    # The "connected" frame is ~23 bytes; the limit must admit it but reject the
    # 100-byte oversized frame that follows.
    max_bytes = 50

    async def relay(ws):
        await ws.send(json.dumps({"type": "connected"}))
        await ws.send("x" * 100)  # exceeds max_ws_message_bytes
        await ws.recv()

    url = await make_relay_server(relay)
    # run() must complete; PayloadTooBig triggers an immediate return, not a retry
    await asyncio.wait_for(
        _client(url, "http://localhost:1", max_ws_message_bytes=max_bytes).run(),
        timeout=5,
    )
