import asyncio
import json

import pytest

from spectral_bridge.client import RelayClient, _headers_for_adapter

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


def test_validate_max_bytes_zero_raises():
    with pytest.raises(ValueError, match="max_ws_message_bytes"):
        RelayClient(
            "wss://relay.example.com/connect",
            "key",
            "http://localhost:8000",
            max_ws_message_bytes=0,
        )


# ── Integration helpers ───────────────────────────────────────────────────────


def _client(relay_url: str, adapter_url: str, **kwargs) -> RelayClient:
    """Convenience: build a RelayClient with insecure_relay=True for ws:// test URLs."""
    return RelayClient(relay_url, "any-key", adapter_url, insecure_relay=True, **kwargs)


# ── Connection and authentication ─────────────────────────────────────────────


async def test_connected_frame_accepted(make_relay_server, adapter_url):
    """Client connects and stays running after receiving the connected frame."""
    ready = asyncio.Event()

    async def handler(ws):
        await ws.send(json.dumps({"type": "connected"}))
        ready.set()
        await ws.recv()

    url = await make_relay_server(handler)
    task = asyncio.create_task(_client(url, adapter_url).run())
    await asyncio.wait_for(ready.wait(), timeout=5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_auth_failure_4001_stops_client(make_relay_server, adapter_url):
    """Server closes with code 4001 → run() returns without retrying."""

    async def handler(ws):
        await ws.close(code=4001, reason="unauthorized")

    url = await make_relay_server(handler)
    # run() must complete (not loop forever); 5-second timeout proves it stops
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
    port = server.sockets[0].getsockname()[1]
    url = f"ws://127.0.0.1:{port}"
    try:
        await asyncio.wait_for(_client(url, adapter_url).run(), timeout=5)
    finally:
        server.close()
        await server.wait_closed()


async def test_unexpected_first_frame_reconnects(make_relay_server, adapter_url):
    """
    Server sends an unexpected frame type before 'connected'.
    The client raises ProtocolError (a WebSocketException), which the retry loop
    catches — so the client reconnects rather than stopping.
    """
    connection_count = 0
    second_connection = asyncio.Event()

    async def handler(ws):
        nonlocal connection_count
        connection_count += 1
        if connection_count == 1:
            await ws.send(json.dumps({"type": "hello"}))  # not "connected"
        else:
            second_connection.set()
            await ws.recv()

    url = await make_relay_server(handler)
    task = asyncio.create_task(_client(url, adapter_url).run())
    # After ProtocolError + backoff (≥1 s), client reconnects
    await asyncio.wait_for(second_connection.wait(), timeout=10)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert connection_count == 2


# ── Request forwarding ────────────────────────────────────────────────────────


async def test_request_forwarded_and_response_echoed(make_relay_server, adapter_url):
    """Fake relay sends a request frame → client POSTs to adapter → relay receives a response frame."""
    received = asyncio.Queue()

    async def handler(ws):
        await ws.send(json.dumps({"type": "connected"}))
        await ws.send(
            json.dumps(
                {
                    "type": "request",
                    "request_id": "req-1",
                    "payload": {
                        "method": "POST",
                        "path": "/v1/chat/completions",
                        "headers": {},
                        "body": {
                            "model": "test",
                            "messages": [{"role": "user", "content": "hello"}],
                        },
                    },
                }
            )
        )
        raw = await ws.recv()
        await received.put(json.loads(raw))
        await ws.recv()

    url = await make_relay_server(handler)
    task = asyncio.create_task(_client(url, adapter_url).run())
    response = await asyncio.wait_for(received.get(), timeout=10)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert response["type"] == "response"
    assert response["payload"]["status"] == 200
    assert response["payload"]["body"]["choices"][0]["message"]["content"] == "ok"


async def test_response_request_id_matches(make_relay_server, adapter_url):
    """request_id in the response frame matches the one in the request frame exactly."""
    received = asyncio.Queue()
    distinctive_id = "my-unique-request-id-xyz"

    async def handler(ws):
        await ws.send(json.dumps({"type": "connected"}))
        await ws.send(
            json.dumps(
                {
                    "type": "request",
                    "request_id": distinctive_id,
                    "payload": {
                        "method": "POST",
                        "path": "/v1/chat/completions",
                        "headers": {},
                        "body": {
                            "model": "test",
                            "messages": [{"role": "user", "content": "hello"}],
                        },
                    },
                }
            )
        )
        raw = await ws.recv()
        await received.put(json.loads(raw))
        await ws.recv()

    url = await make_relay_server(handler)
    task = asyncio.create_task(_client(url, adapter_url).run())
    response = await asyncio.wait_for(received.get(), timeout=10)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert response["request_id"] == distinctive_id


async def test_adapter_unavailable_sends_503(make_relay_server):
    """Adapter URL is unreachable → client sends a response frame with status 503."""
    # Bind on port 0 (OS picks a free port), then close immediately so nothing is listening.
    _srv = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    _port = _srv.sockets[0].getsockname()[1]
    _srv.close()
    await _srv.wait_closed()
    dead_url = f"http://127.0.0.1:{_port}"

    received = asyncio.Queue()

    async def handler(ws):
        await ws.send(json.dumps({"type": "connected"}))
        await ws.send(
            json.dumps(
                {
                    "type": "request",
                    "request_id": "req-dead",
                    "payload": {
                        "method": "POST",
                        "path": "/v1/chat/completions",
                        "headers": {},
                        "body": {
                            "model": "test",
                            "messages": [{"role": "user", "content": "hello"}],
                        },
                    },
                }
            )
        )
        raw = await ws.recv()
        await received.put(json.loads(raw))
        await ws.recv()

    url = await make_relay_server(handler)
    task = asyncio.create_task(_client(url, dead_url).run())
    response = await asyncio.wait_for(received.get(), timeout=10)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert response["type"] == "response"
    assert response["payload"]["status"] == 503


# ── Resilience ────────────────────────────────────────────────────────────────


async def test_concurrent_requests_both_answered(make_relay_server, adapter_url):
    """Relay sends two request frames before reading any response → both handled concurrently → two response frames received with status 200."""
    responses = asyncio.Queue()

    async def handler(ws):
        await ws.send(json.dumps({"type": "connected"}))
        # Send both frames immediately, before reading any response
        for req_id in ("req-a", "req-b"):
            await ws.send(
                json.dumps(
                    {
                        "type": "request",
                        "request_id": req_id,
                        "payload": {
                            "method": "POST",
                            "path": "/v1/chat/completions",
                            "headers": {},
                            "body": {
                                "model": "test",
                                "messages": [{"role": "user", "content": "hello"}],
                            },
                        },
                    }
                )
            )
        # Collect both responses (order may vary)
        r1 = json.loads(await ws.recv())
        r2 = json.loads(await ws.recv())
        responses.put_nowait(r1)
        responses.put_nowait(r2)
        await ws.recv()

    url = await make_relay_server(handler)
    task = asyncio.create_task(_client(url, adapter_url).run())
    r1 = await asyncio.wait_for(responses.get(), timeout=10)
    r2 = await asyncio.wait_for(responses.get(), timeout=10)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert {r1["request_id"], r2["request_id"]} == {"req-a", "req-b"}
    assert r1["payload"]["status"] == 200
    assert r2["payload"]["status"] == 200


async def test_reconnects_after_disconnect(make_relay_server, adapter_url):
    """First relay connection closes with code 1001 → client reconnects → second connection is reached."""
    connection_count = 0
    reconnected = asyncio.Event()

    async def handler(ws):
        nonlocal connection_count
        connection_count += 1
        if connection_count == 1:
            await ws.send(json.dumps({"type": "connected"}))
            await ws.close(code=1001, reason="going away")
        else:
            reconnected.set()
            await ws.recv()

    url = await make_relay_server(handler)
    task = asyncio.create_task(_client(url, adapter_url).run())
    # Allow up to 10s to accommodate the 1s backoff after the first disconnect
    await asyncio.wait_for(reconnected.wait(), timeout=10)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert connection_count == 2


async def test_payload_too_big_stops_client(make_relay_server):
    """Relay sends a frame larger than max_ws_message_bytes → PayloadTooBig → run() returns without retrying."""
    # The "connected" frame is ~23 bytes; use a limit large enough to receive it
    # but smaller than the 100-byte oversized frame.
    max_bytes = 50

    async def handler(ws):
        await ws.send(json.dumps({"type": "connected"}))
        await ws.send("x" * 100)  # exceeds max_ws_message_bytes
        await ws.recv()

    url = await make_relay_server(handler)
    # run() must complete; PayloadTooBig triggers an immediate return, not a retry
    await asyncio.wait_for(
        _client(url, "http://localhost:1", max_ws_message_bytes=max_bytes).run(),
        timeout=5,
    )
