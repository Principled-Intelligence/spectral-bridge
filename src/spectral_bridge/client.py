"""WebSocket relay client.

Maintains a persistent outbound WebSocket connection to the relay server,
forwarding incoming request frames to a local adapter and returning
the adapter's responses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

import httpx
import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import (
    ConnectionClosedError,
    InvalidStatus,
    ProtocolError,
    WebSocketException,
)

logger = logging.getLogger("spectral_bridge.client")

KEEPALIVE_INTERVAL = 30
KEEPALIVE_TIMEOUT = 10

BACKOFF_SCHEDULE = [1, 2, 4, 8, 30]
STABLE_CONNECTION_THRESHOLD = 60


ADAPTER_CHAT_PATH = "/v1/chat/completions"

DEFAULT_MAX_WS_MESSAGE_BYTES = 16 * 1024 * 1024

# Strip hop-by-hop / connection-specific fields when rebuilding a POST to localhost.
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


def _headers_for_adapter(payload_headers: object) -> dict[str, str]:
    """Map request-frame headers to an httpx headers dict for the adapter POST."""
    if not isinstance(payload_headers, dict):
        return {}

    return {
        name: value
        for name, value in payload_headers.items()
        if isinstance(name, str) and name.lower() not in _HOP_BY_HOP_HEADERS
    }


class RelayClient:
    def __init__(
        self,
        relay_url: str,
        api_key: str,
        adapter_url: str,
        *,
        insecure_relay: bool = False,
        max_ws_message_bytes: int = DEFAULT_MAX_WS_MESSAGE_BYTES,
    ) -> None:
        if max_ws_message_bytes < 1:
            raise ValueError("max_ws_message_bytes must be at least 1")
        self.relay_url = relay_url
        self.api_key = api_key
        self.adapter_url = adapter_url.rstrip("/")
        self._max_ws_message_bytes = max_ws_message_bytes
        self._http: httpx.AsyncClient | None = None
        self._tasks: set[asyncio.Task] = set()
        self._validate_relay_url(insecure_relay)

    def _validate_relay_url(self, insecure_relay: bool) -> None:
        parsed = urlparse(self.relay_url)
        scheme = (parsed.scheme or "").lower()
        if scheme == "wss":
            return
        if scheme == "ws":
            if insecure_relay:
                logger.warning("plain ws relay url, use wss in production")
                return
            raise ValueError(
                "relay url must use wss:// (plain ws:// requires --insecure-relay)"
            )
        raise ValueError("relay url must use wss:// or ws:// websocket scheme")

    async def _cancel_inflight_handlers(self) -> None:
        if not self._tasks:
            return
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self) -> None:
        attempt = 0
        self._http = httpx.AsyncClient(timeout=30)
        try:
            while True:
                try:
                    t0 = time.monotonic()
                    await self._connect()
                    if time.monotonic() - t0 >= STABLE_CONNECTION_THRESHOLD:
                        attempt = 0
                except ConnectionClosedError as exc:
                    if exc.rcvd is not None and exc.rcvd.code == 4001:
                        logger.error("authentication failed")
                        return
                    sent_1009 = exc.sent is not None and exc.sent.code == 1009
                    rcvd_1009 = exc.rcvd is not None and exc.rcvd.code == 1009
                    if sent_1009 or rcvd_1009:
                        logger.error("relay frame exceeded max size (%d bytes)", self._max_ws_message_bytes)
                        return
                    delay = BACKOFF_SCHEDULE[min(attempt, len(BACKOFF_SCHEDULE) - 1)]
                    logger.warning("disconnected (%s), reconnecting in %ds", exc, delay)
                    await asyncio.sleep(delay)
                    attempt += 1
                except InvalidStatus as exc:
                    if exc.response.status_code == 401:
                        logger.error("authentication failed")
                        return
                    raise
                except (OSError, WebSocketException) as exc:
                    delay = BACKOFF_SCHEDULE[min(attempt, len(BACKOFF_SCHEDULE) - 1)]
                    logger.warning("disconnected (%s), reconnecting in %ds", exc, delay)
                    await asyncio.sleep(delay)
                    attempt += 1
        finally:
            await self._cancel_inflight_handlers()
            if self._http is not None:
                await self._http.aclose()
                self._http = None

    async def _connect(self) -> None:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with websockets.connect(
            self.relay_url,
            additional_headers=headers,
            ping_interval=KEEPALIVE_INTERVAL,
            ping_timeout=KEEPALIVE_TIMEOUT,
            max_size=self._max_ws_message_bytes,
        ) as ws:
            # wait for the connected confirmation frame
            raw = await ws.recv()
            msg = json.loads(raw)
            if msg.get("type") != "connected":
                raise ProtocolError(f"unexpected first frame: {msg}")
            logger.info("connected to relay")

            # run listener; keepalive is handled by websockets library's
            # built-in ping_interval / ping_timeout
            await self._listen(ws)

    async def _listen(self, ws: ClientConnection) -> None:
        async for raw in ws:
            data = json.loads(raw)
            msg_type = data.get("type")
            if msg_type == "request":
                task = asyncio.create_task(
                    self._handle_request(ws, data["request_id"], data["payload"])
                )
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            else:
                logger.warning("unknown frame type=%s", msg_type)

    async def _handle_request(
        self,
        ws: ClientConnection,
        request_id: str,
        payload: dict[str, Any],
    ) -> None:
        body = payload.get("body", {})
        adapter_headers = _headers_for_adapter(payload.get("headers"))
        url = f"{self.adapter_url}{ADAPTER_CHAT_PATH}"

        try:
            resp = await self._http.post(url, json=body, headers=adapter_headers)
            response_body = resp.json()
            await ws.send(
                json.dumps(
                    {
                        "type": "response",
                        "request_id": request_id,
                        "payload": {
                            "status": resp.status_code,
                            "headers": {"content-type": "application/json"},
                            "body": response_body,
                        },
                    }
                )
            )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            logger.warning("adapter unavailable (%r)", exc)
            await ws.send(
                json.dumps(
                    {
                        "type": "response",
                        "request_id": request_id,
                        "payload": {
                            "status": 503,
                            "headers": {"content-type": "application/json"},
                            "body": {"error": {"message": "adapter unavailable"}},
                        },
                    }
                )
            )
        except Exception:
            logger.exception("error handling request %s", request_id)
            try:
                await ws.send(
                    json.dumps(
                        {
                            "type": "response",
                            "request_id": request_id,
                            "payload": {
                                "status": 500,
                                "headers": {"content-type": "application/json"},
                                "body": {
                                    "error": {"message": "internal relay client error"}
                                },
                            },
                        }
                    )
                )
            except Exception:
                pass
