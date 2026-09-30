import asyncio
import json
import time
from contextlib import asynccontextmanager

import pytest
import websockets
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

CONNECTED = json.dumps({"type": "connected", "protocol": 2})


async def recv_response(ws) -> dict:
    """The next response frame from the client, skipping its request acks."""
    while True:
        frame = json.loads(await ws.recv())
        if frame["type"] == "response":
            return frame


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
            await ws.send(CONNECTED)
            # send every request before reading any response, so concurrent
            # handling is exercised when more than one id is requested
            for request_id in request_ids:
                await ws.send(
                    json.dumps(request_frame(request_id, body=body, path=path))
                )
            for _ in request_ids:
                frame = await recv_response(ws)
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
        await ws.send(CONNECTED)
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


async def _serve_http_status(status_line: bytes):
    """A bare TCP server that answers every WebSocket upgrade with ``status_line``."""

    async def serve(reader, writer):
        await reader.read(4096)
        writer.write(
            b"HTTP/1.1 " + status_line + b"\r\n"
            b"Content-Length: 0\r\n"
            b"Connection: close\r\n\r\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    return await asyncio.start_server(serve, "127.0.0.1", 0)


@pytest.mark.parametrize("status_line", [b"401 Unauthorized", b"403 Forbidden"])
async def test_auth_failure_http_status_stops_client(adapter_url, status_line):
    """HTTP 401/403 during WebSocket upgrade → run() returns without retrying.

    403 is what an ASGI relay sends when it closes the socket before accept().
    """
    server = await _serve_http_status(status_line)
    url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        await asyncio.wait_for(_client(url, adapter_url).run(), timeout=5)
    finally:
        server.close()
        await server.wait_closed()


async def test_transient_handshake_status_reconnects(make_relay_server, adapter_url):
    """A non-auth handshake rejection (e.g. 503 while the relay is being
    provisioned) is transient: the client keeps retrying instead of crashing."""
    connected = asyncio.Event()

    async def relay(ws):
        connected.set()
        await ws.recv()

    relay_url = await make_relay_server(relay)
    unavailable = await _serve_http_status(b"503 Service Unavailable")
    unavailable_port = unavailable.sockets[0].getsockname()[1]
    client = _client(f"ws://127.0.0.1:{unavailable_port}", adapter_url)

    try:
        async with running_client(client) as task:
            # first attempt hits the 503; the client must survive it and retry,
            # this time against the healthy relay
            await asyncio.sleep(0.2)
            assert not task.done()
            client.relay_url = relay_url
            await asyncio.wait_for(connected.wait(), timeout=10)
    finally:
        unavailable.close()
        await unavailable.wait_closed()


async def test_protocol_version_refused_by_relay_stops_client(
    make_relay_server, adapter_url
):
    """The relay refuses this client's protocol version (4002) → run() returns."""

    async def relay(ws):
        await ws.close(code=4002, reason="unsupported protocol version, expected 3")

    url = await make_relay_server(relay)
    await asyncio.wait_for(_client(url, adapter_url).run(), timeout=5)


async def test_relay_speaking_another_protocol_stops_client(
    make_relay_server, adapter_url
):
    """A relay whose connected frame names no (or another) version → run()
    returns: an older relay would neither ack nor deduplicate."""

    async def relay(ws):
        await ws.send(json.dumps({"type": "connected"}))
        await ws.recv()

    url = await make_relay_server(relay)
    await asyncio.wait_for(_client(url, adapter_url).run(), timeout=5)


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


@pytest.mark.parametrize(
    "frame",
    [
        "not json",
        '["not", "an", "object"]',
        json.dumps({"type": "request", "payload": {}}),
        json.dumps({"type": "request", "request_id": "req-0"}),
        json.dumps({"type": "ack", "request_id": ["req-0"]}),
        json.dumps({"type": "ping"}),
    ],
    ids=[
        "not-json",
        "not-an-object",
        "request-without-id",
        "request-without-payload",
        "ack-with-bad-id",
        "unknown-type",
    ],
)
async def test_malformed_frame_is_dropped(make_relay_server, adapter_url, frame):
    """A malformed frame is logged and dropped: the client keeps serving the
    connection, rather than stopping."""
    received: asyncio.Future = asyncio.get_running_loop().create_future()

    async def relay(ws):
        await ws.send(CONNECTED)
        await ws.send(frame)
        await ws.send(json.dumps(request_frame("req-1")))
        received.set_result(await recv_response(ws))
        await ws.recv()

    url = await make_relay_server(relay)
    async with running_client(_client(url, adapter_url)) as task:
        response = await asyncio.wait_for(received, timeout=10)
        assert not task.done()
    assert response["request_id"] == "req-1"


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
        await ws.send(CONNECTED)
        await ws.send(json.dumps(frame))
        result["response"] = await recv_response(ws)
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
            await ws.send(CONNECTED)
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
    "no close frame received or sent" case, e.g. a proxy or load balancer
    recycling the connection) — is transient: the client reconnects rather than
    stopping.
    """
    connection_count = 0
    reconnected = asyncio.Event()

    async def relay(ws):
        nonlocal connection_count
        connection_count += 1
        if connection_count == 1:
            await ws.send(CONNECTED)
            ws.transport.abort()  # sever TCP with no close frame -> client sees 1006
        else:
            reconnected.set()
            await ws.recv()

    url = await make_relay_server(relay)
    async with running_client(_client(url, adapter_url)):
        await asyncio.wait_for(reconnected.wait(), timeout=10)
    assert connection_count == 2


async def test_adapter_error_is_forwarded_and_logged(
    roundtrip, make_asgi_server, caplog
):
    """An adapter error reaches the relay unchanged, and is logged here, truncated."""
    error = {"error": {"message": "x" * 5000}}

    async def failing_app(scope, receive, send):
        if scope["type"] != "http":
            return
        await receive()
        body = json.dumps(error).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 500,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    url = make_asgi_server(failing_app)
    with caplog.at_level("WARNING", logger="spectral_bridge.client"):
        response = (await roundtrip(url, ["req-1"]))["req-1"]

    assert response["payload"]["status"] == 500
    assert response["payload"]["body"] == error
    [message] = [
        r.getMessage() for r in caplog.records if "adapter returned" in r.getMessage()
    ]
    assert message.startswith("adapter returned 500 for req-1: ")
    assert "chars truncated" in message


# ── Responses across reconnects ───────────────────────────────────────────────


async def _response_after_reconnect(
    make_relay_server, make_asgi_server, adapter_delay: float
) -> dict:
    """
    Send a request on a first connection, then sever it (no close frame, as a
    proxy recycling the connection does) while the adapter is still working.
    Return the response frame the client sends on its second connection.
    """
    slow_url = make_asgi_server(_slow_completion_app(adapter_delay))
    connection_count = 0
    received: asyncio.Future = asyncio.get_running_loop().create_future()

    async def relay(ws):
        nonlocal connection_count
        connection_count += 1
        await ws.send(CONNECTED)
        if connection_count == 1:
            await ws.send(json.dumps(request_frame("req-1")))
            ws.transport.abort()
        else:
            received.set_result(await recv_response(ws))
            await ws.recv()

    url = await make_relay_server(relay)
    async with running_client(_client(url, slow_url)):
        return await asyncio.wait_for(received, timeout=10)


async def test_response_held_while_disconnected_is_sent_on_reconnect(
    make_relay_server, make_asgi_server
):
    """The adapter answers during the reconnect backoff (~1s): the response is
    held, then sent on the new connection."""
    frame = await _response_after_reconnect(
        make_relay_server, make_asgi_server, adapter_delay=0.3
    )
    assert frame["request_id"] == "req-1"
    assert frame["payload"]["status"] == 200


async def test_response_after_reconnect_goes_to_the_new_connection(
    make_relay_server, make_asgi_server
):
    """The adapter answers once the client has reconnected: the response goes on
    the new connection, not the one the request came in on."""
    frame = await _response_after_reconnect(
        make_relay_server, make_asgi_server, adapter_delay=2.0
    )
    assert frame["request_id"] == "req-1"
    assert frame["payload"]["status"] == 200


async def test_held_response_past_its_deadline_is_dropped():
    """A response the relay has already given up on isn't sent on reconnect."""

    class _Ws:
        def __init__(self):
            self.sent = []

        async def send(self, raw):
            self.sent.append(json.loads(raw))

    client = _client("ws://relay.test/connect", "http://localhost:1")
    now = time.monotonic()
    expired = {"type": "response", "request_id": "old", "payload": {"status": 200}}
    fresh = {"type": "response", "request_id": "new", "payload": {"status": 200}}
    client._unacked = {"old": (now - 1, expired), "new": (now + 60, fresh)}
    ws = _Ws()

    await client._resend_unacked(ws)

    assert [frame["request_id"] for frame in ws.sent] == ["new"]
    # kept until acked
    assert list(client._unacked) == ["new"]


async def test_expired_responses_are_dropped_as_new_ones_are_held(monkeypatch):
    """Responses the relay never acked don't pile up while connected: expired
    ones go as soon as another response is held."""
    client = _client("ws://relay.test/connect", "http://localhost:1")
    expired = {"type": "response", "request_id": "old", "payload": {"status": 200}}
    client._unacked = {"old": (time.monotonic() - 1, expired)}

    async def call_adapter(request_id, payload):
        return {"status": 200, "headers": {}, "body": {}}

    monkeypatch.setattr(client, "_call_adapter", call_adapter)
    await client._handle_request("new", {})

    assert list(client._unacked) == ["new"]


# ── Acks ──────────────────────────────────────────────────────────────────────


def _counting_completion_app(delay: float = 0.0):
    """A slow completion app that records every request it serves."""
    calls: list[dict] = []
    slow = _slow_completion_app(delay)

    async def app(scope, receive, send):
        if scope["type"] == "http":
            calls.append(scope)
        await slow(scope, receive, send)

    return app, calls


async def test_request_is_acked_before_it_is_answered(
    make_relay_server, make_asgi_server
):
    """The client acks a request frame on receipt, ahead of its response."""
    frames: list[dict] = []
    done = asyncio.Event()

    async def relay(ws):
        await ws.send(CONNECTED)
        await ws.send(json.dumps(request_frame("req-1")))
        while len(frames) < 2:
            frames.append(json.loads(await ws.recv()))
        done.set()
        await ws.recv()

    url = await make_relay_server(relay)
    slow_url = make_asgi_server(_slow_completion_app(0.2))
    async with running_client(_client(url, slow_url)):
        await asyncio.wait_for(done.wait(), timeout=10)

    assert frames[0] == {"type": "ack", "request_id": "req-1"}
    assert frames[1]["type"] == "response"


async def _responses_across_reconnect(
    make_relay_server, adapter_url, *, ack: bool
) -> list[dict]:
    """
    On a first connection, send a request and receive its response, acking it
    or not, then sever the connection. Return the frames the client sends on
    the second connection, within a short window.
    """
    connection_count = 0
    second: list[dict] = []
    done = asyncio.Event()

    async def relay(ws):
        nonlocal connection_count
        connection_count += 1
        await ws.send(CONNECTED)
        if connection_count == 1:
            await ws.send(json.dumps(request_frame("req-1")))
            await recv_response(ws)
            if ack:
                await ws.send(json.dumps({"type": "ack", "request_id": "req-1"}))
                # let the client read the ack before the connection goes
                await asyncio.sleep(0.1)
            ws.transport.abort()
        else:
            try:
                while True:
                    second.append(json.loads(await asyncio.wait_for(ws.recv(), 0.5)))
            except TimeoutError:
                done.set()
            await ws.recv()

    url = await make_relay_server(relay)
    async with running_client(_client(url, adapter_url)):
        await asyncio.wait_for(done.wait(), timeout=10)
    return second


async def test_unacked_response_is_resent_after_reconnect(
    make_relay_server, adapter_url
):
    """A response sent fine, but never acked, may have been lost in transit: it
    is sent again on the next connection."""
    second = await _responses_across_reconnect(
        make_relay_server, adapter_url, ack=False
    )
    assert [(f["type"], f["request_id"]) for f in second] == [("response", "req-1")]


async def test_acked_response_is_not_resent_after_reconnect(
    make_relay_server, adapter_url
):
    second = await _responses_across_reconnect(make_relay_server, adapter_url, ack=True)
    assert second == []


async def test_request_received_again_reaches_adapter_once(
    make_relay_server, make_asgi_server
):
    """A request sent again while still being handled, and once answered but
    before the response is acked, is acked each time but forwarded only once;
    the second time, the response is sent again."""
    app, calls = _counting_completion_app(0.3)
    adapter = make_asgi_server(app)
    frames: list[dict] = []
    done = asyncio.Event()

    async def relay(ws):
        await ws.send(CONNECTED)
        await ws.send(json.dumps(request_frame("req-1")))
        await ws.send(json.dumps(request_frame("req-1")))  # still in flight
        frames.append(await recv_response(ws))
        await ws.send(json.dumps(request_frame("req-1")))  # answered, not acked
        frames.append(await recv_response(ws))
        done.set()
        await ws.recv()

    url = await make_relay_server(relay)
    async with running_client(_client(url, adapter)):
        await asyncio.wait_for(done.wait(), timeout=10)

    assert len(calls) == 1
    assert [f["request_id"] for f in frames] == ["req-1", "req-1"]
    assert frames[0] == frames[1]


# ── Shutdown ──────────────────────────────────────────────────────────────────


async def _drain(
    make_relay_server,
    make_asgi_server,
    *,
    adapter_delay: float,
    shutdown_grace: float,
    shutdowns: int = 1,
    during_drain=None,
) -> dict:
    """
    Send a request to a client whose adapter takes ``adapter_delay``, shut the
    client down (``shutdowns`` times) once it acked it, and ack each response.
    ``during_drain(ws)`` runs right after the shutdown. Return the response
    frames received, the close code, and how long the shutdown took.
    """
    app, calls = _counting_completion_app(adapter_delay)
    adapter = make_asgi_server(app)
    result: dict = {"responses": [], "calls": calls}
    closed = asyncio.Event()
    client: RelayClient

    async def relay(ws):
        await ws.send(CONNECTED)
        await ws.send(json.dumps(request_frame("req-1")))
        assert json.loads(await ws.recv())["type"] == "ack"
        result["started"] = time.monotonic()
        for _ in range(shutdowns):
            client.shutdown()
        if during_drain is not None:
            await during_drain(ws)
        try:
            while True:
                frame = json.loads(await ws.recv())
                if frame["type"] == "response":
                    result["responses"].append(frame)
                    await ws.send(
                        json.dumps({"type": "ack", "request_id": frame["request_id"]})
                    )
        except websockets.ConnectionClosed as exc:
            result["close_code"] = exc.rcvd.code if exc.rcvd else None
            closed.set()

    url = await make_relay_server(relay)
    client = _client(url, adapter, shutdown_grace=shutdown_grace)
    await asyncio.wait_for(client.run(), timeout=10)
    result["took"] = time.monotonic() - result["started"]
    await asyncio.wait_for(closed.wait(), timeout=2)
    return result


async def test_shutdown_finishes_in_flight_requests_then_closes(
    make_relay_server, make_asgi_server
):
    """In-flight requests finish, their responses are acked, and the client
    closes with 1001: the relay fails at once whatever is left."""
    result = await _drain(
        make_relay_server, make_asgi_server, adapter_delay=0.3, shutdown_grace=5
    )
    assert [f["payload"]["status"] for f in result["responses"]] == [200]
    assert result["close_code"] == 1001
    # closed once acked, not at the end of the grace
    assert result["took"] < 2


async def test_shutdown_refuses_new_requests(make_relay_server, make_asgi_server):
    """A request received while draining is answered 503 at once, without
    reaching the adapter, so the caller can retry it."""

    async def send_another(ws):
        await ws.send(json.dumps(request_frame("req-2")))

    result = await _drain(
        make_relay_server,
        make_asgi_server,
        adapter_delay=0.3,
        shutdown_grace=5,
        during_drain=send_another,
    )
    by_id = {f["request_id"]: f["payload"] for f in result["responses"]}
    assert by_id["req-2"]["status"] == 503
    assert by_id["req-2"]["body"] == {"error": {"message": "client shutting down"}}
    assert by_id["req-1"]["status"] == 200
    assert len(result["calls"]) == 1


async def test_shutdown_closes_once_the_grace_is_over(
    make_relay_server, make_asgi_server
):
    result = await _drain(
        make_relay_server, make_asgi_server, adapter_delay=5, shutdown_grace=0.3
    )
    assert result["responses"] == []
    assert result["close_code"] == 1001
    assert result["took"] < 2


async def test_second_shutdown_stops_at_once(make_relay_server, make_asgi_server):
    result = await _drain(
        make_relay_server,
        make_asgi_server,
        adapter_delay=5,
        shutdown_grace=30,
        shutdowns=2,
    )
    assert result["close_code"] == 1001
    assert result["took"] < 2


async def test_shutdown_while_reconnecting_returns(make_relay_server, adapter_url):
    """Shut down during the reconnect backoff, with nothing left to deliver: the
    client stops rather than waiting to reconnect."""
    connection_count = 0

    async def relay(ws):
        nonlocal connection_count
        connection_count += 1
        await ws.send(CONNECTED)
        ws.transport.abort()

    url = await make_relay_server(relay)
    client = _client(url, adapter_url)
    task = asyncio.create_task(client.run())
    async with asyncio.timeout(5):
        while connection_count < 2:  # past the immediate retry: backing off
            await asyncio.sleep(0.01)
    client.shutdown()
    await asyncio.wait_for(task, timeout=2)


# ── Reconnect backoff ─────────────────────────────────────────────────────────


async def test_rotation_reconnects_at_once_every_time(
    make_relay_server, adapter_url, caplog
):
    """The relay rotating connections (4003) is expected: the client reconnects
    at once each time, however short the connection was, and logs no warning."""
    connection_count = 0
    reconnected = asyncio.Event()

    async def relay(ws):
        nonlocal connection_count
        connection_count += 1
        await ws.send(CONNECTED)
        if connection_count <= 3:
            await ws.close(code=4003, reason="connection rotation")
        else:
            reconnected.set()
            await ws.recv()

    url = await make_relay_server(relay)
    started = time.monotonic()
    async with running_client(_client(url, adapter_url)):
        await asyncio.wait_for(reconnected.wait(), timeout=5)
    # a backoff would have waited 1s, then 2s
    assert time.monotonic() - started < 1
    assert not [r for r in caplog.records if "disconnected" in r.getMessage()]


async def test_reconnects_immediately_after_a_drop(make_relay_server, adapter_url):
    """The first retry is immediate: a drop is usually the infrastructure
    recycling the connection, and the relay is up."""
    dropped_at = None
    reconnected = asyncio.Event()
    connection_count = 0

    async def relay(ws):
        nonlocal connection_count, dropped_at
        connection_count += 1
        await ws.send(CONNECTED)
        if connection_count == 1:
            dropped_at = time.monotonic()
            ws.transport.abort()
        else:
            reconnected.set()
            await ws.recv()

    url = await make_relay_server(relay)
    async with running_client(_client(url, adapter_url)):
        await asyncio.wait_for(reconnected.wait(), timeout=5)
        assert time.monotonic() - dropped_at < 0.5


async def _capture_backoff_delays(monkeypatch, *, stable_threshold: float) -> list:
    """
    Drive run()'s reconnect loop with an always-failing connection, capturing the
    backoff delay used on each iteration. _connect is stubbed to fail immediately
    and the backoff sleep is stubbed to record (not wait), so no real network or
    time is involved; only the loop's delay arithmetic is exercised.
    """
    client = _client("ws://relay.test/connect", "http://localhost:1")
    monkeypatch.setattr(client_mod, "BACKOFF_SCHEDULE", [1, 2, 4, 8])
    monkeypatch.setattr(client_mod, "STABLE_CONNECTION_THRESHOLD", stable_threshold)

    delays: list = []

    async def fake_connect():
        # abnormal closure, no close frame -> falls through to the backoff branch
        raise ConnectionClosedError(None, None)

    async def fake_sleep(delay):
        delays.append(delay)
        if len(delays) >= 4:
            raise asyncio.CancelledError
        return False  # not stopped

    monkeypatch.setattr(client, "_connect", fake_connect)
    monkeypatch.setattr(client, "_sleep_unless_stopped", fake_sleep)

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
    # The "connected" frame is ~37 bytes; the limit must admit it but reject the
    # 100-byte oversized frame that follows.
    max_bytes = 50

    async def relay(ws):
        await ws.send(CONNECTED)
        await ws.send("x" * 100)  # exceeds max_ws_message_bytes
        await ws.recv()

    url = await make_relay_server(relay)
    # run() must complete; PayloadTooBig triggers an immediate return, not a retry
    await asyncio.wait_for(
        _client(url, "http://localhost:1", max_ws_message_bytes=max_bytes).run(),
        timeout=5,
    )
