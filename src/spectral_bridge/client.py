"""WebSocket relay client.

Maintains a persistent outbound WebSocket connection to the relay server,
forwarding incoming request frames to a local adapter and returning
the adapter's responses.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

import httpx
import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    InvalidStatus,
    ProtocolError,
    WebSocketException,
)

logger = logging.getLogger("spectral_bridge.client")

# the protocol version spoken with the relay (PROTOCOL.md §2.1): not negotiated,
# a relay speaking another one closes the connection with 4002
PROTOCOL_VERSION = 2

KEEPALIVE_INTERVAL = 30
KEEPALIVE_TIMEOUT = 10

BACKOFF_SCHEDULE = [0, 1, 2, 4, 8, 30]
STABLE_CONNECTION_THRESHOLD = 60


ADAPTER_CHAT_PATH = "/v1/chat/completions"
ADAPTER_RESPONSES_PATH = "/v1/responses"
ADAPTER_ALLOWED_PATHS = frozenset({ADAPTER_CHAT_PATH, ADAPTER_RESPONSES_PATH})

DEFAULT_MAX_WS_MESSAGE_BYTES = 16 * 1024 * 1024

# logged bodies are truncated: they are unbounded and may echo request content
MAX_LOG_BODY_CHARS = 1024

# Max time to wait for the adapter to return a completion. Long completions are
# real, so this is generous; the default is matched to the Spectral relay's
# server-side timeout. On another platform keep it >= that relay's timeout so
# the server is the authority on giving up rather than the client.
DEFAULT_REQUEST_TIMEOUT = 600.0
_CONNECT_TIMEOUT = 10.0

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
        insecure_adapter: bool = False,
        max_ws_message_bytes: int = DEFAULT_MAX_WS_MESSAGE_BYTES,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        if max_ws_message_bytes < 1:
            raise ValueError("max_ws_message_bytes must be at least 1")
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        self.relay_url = relay_url
        self.api_key = api_key
        self.adapter_url = adapter_url.rstrip("/")
        self._max_ws_message_bytes = max_ws_message_bytes
        self._request_timeout = request_timeout
        self._http: httpx.AsyncClient | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._ws: ClientConnection | None = None
        # request_id -> (deadline, response frame), until the relay acks it: a
        # response sent just before a disconnect may never arrive, so those not
        # acked are sent again on every reconnect, until the deadline past which
        # the relay has given up on them
        self._unacked: dict[str, tuple[float, dict[str, Any]]] = {}
        self._validate_relay_url(insecure_relay)
        self._validate_adapter_url(insecure_adapter)

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

    def _validate_adapter_url(self, insecure_adapter: bool) -> None:
        parsed = urlparse(self.adapter_url)
        scheme = (parsed.scheme or "").lower()
        if scheme not in ("http", "https"):
            raise ValueError("adapter url must use http:// or https://")
        host = parsed.hostname or ""
        if self._is_loopback_host(host):
            return
        if insecure_adapter:
            logger.warning("non-loopback adapter url, forwarding beyond localhost")
            return
        raise ValueError(
            "adapter url must be loopback (localhost/127.0.0.1/::1) "
            "unless --insecure-adapter is set"
        )

    @staticmethod
    def _is_loopback_host(host: str) -> bool:
        if host.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    async def _cancel_inflight_handlers(self) -> None:
        if not self._tasks:
            return
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self) -> None:
        attempt = 0
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(self._request_timeout, connect=_CONNECT_TIMEOUT),
            follow_redirects=False,
        )
        try:
            while True:
                t0 = time.monotonic()
                try:
                    await self._connect()
                    # clean close (server sent a close frame)
                    reason = "connection closed"
                except ConnectionClosedError as exc:
                    if exc.rcvd is not None and exc.rcvd.code == 4001:
                        logger.error("authentication failed")
                        return
                    if exc.rcvd is not None and exc.rcvd.code == 4002:
                        logger.error(
                            "relay refused protocol version %d (%s), "
                            "upgrade spectral-bridge",
                            PROTOCOL_VERSION,
                            exc.rcvd.reason,
                        )
                        return
                    sent_1009 = exc.sent is not None and exc.sent.code == 1009
                    rcvd_1009 = exc.rcvd is not None and exc.rcvd.code == 1009
                    if sent_1009 or rcvd_1009:
                        logger.error(
                            "relay frame exceeded max size (%d bytes)",
                            self._max_ws_message_bytes,
                        )
                        return
                    reason = str(exc)
                except _UnsupportedRelay as exc:
                    logger.error(
                        "relay speaks protocol version %s, expected %d",
                        exc.version,
                        PROTOCOL_VERSION,
                    )
                    return
                except InvalidStatus as exc:
                    if exc.response.status_code in (401, 403):
                        logger.error("authentication failed")
                        return
                    reason = f"handshake rejected with http {exc.response.status_code}"
                except (OSError, WebSocketException) as exc:
                    reason = str(exc)

                # A connection that stayed up long enough is considered healthy,
                # so the next disconnect starts backoff from scratch. This reset
                # must happen on every disconnect path (abnormal closures raise
                # rather than return), otherwise transient drops accumulate and
                # the delay keeps escalating even between healthy sessions.
                if time.monotonic() - t0 >= STABLE_CONNECTION_THRESHOLD:
                    attempt = 0
                delay = BACKOFF_SCHEDULE[min(attempt, len(BACKOFF_SCHEDULE) - 1)]
                logger.warning("disconnected (%s), reconnecting in %ds", reason, delay)
                await asyncio.sleep(delay)
                attempt += 1
        finally:
            await self._cancel_inflight_handlers()
            if self._http is not None:
                await self._http.aclose()
                self._http = None

    async def _connect(self) -> None:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Spectral-Bridge-Protocol": str(PROTOCOL_VERSION),
        }
        async with websockets.connect(
            self.relay_url,
            additional_headers=headers,
            ping_interval=KEEPALIVE_INTERVAL,
            ping_timeout=KEEPALIVE_TIMEOUT,
            max_size=self._max_ws_message_bytes,
        ) as ws:
            # wait for the connected confirmation frame
            msg = _parse_frame(await ws.recv())
            if msg is None or msg.get("type") != "connected":
                raise ProtocolError(f"unexpected first frame: {msg}")
            if msg.get("protocol") != PROTOCOL_VERSION:
                raise _UnsupportedRelay(msg.get("protocol"))
            logger.info("connected to relay")

            self._ws = ws
            try:
                await self._resend_unacked(ws)
                await self._listen(ws)
            finally:
                self._ws = None

    async def _listen(self, ws: ClientConnection) -> None:
        async for raw in ws:
            # a malformed frame is logged and dropped: it must not stop the
            # client, and every other request it's handling
            data = _parse_frame(raw)
            if data is None:
                logger.warning("malformed frame: %s", _for_log(raw))
                continue

            msg_type = data.get("type")
            if msg_type not in ("request", "ack"):
                logger.warning("unknown frame type=%s", msg_type)
                continue

            request_id = data.get("request_id")
            payload = data.get("payload")
            if not isinstance(request_id, str) or (
                msg_type == "request" and not isinstance(payload, dict)
            ):
                logger.warning("malformed frame: %s", _for_log(raw))
                continue

            if msg_type == "request":
                await self._on_request(ws, request_id, payload)
            else:
                self._unacked.pop(request_id, None)

    async def _on_request(
        self, ws: ClientConnection, request_id: str, payload: dict[str, Any]
    ) -> None:
        await _send(ws, {"type": "ack", "request_id": request_id})

        if request_id in self._tasks:
            # a request received again must never reach the adapter twice
            return

        if request_id in self._unacked:
            # answered already, but the response may have been lost too
            await _send(ws, self._unacked[request_id][1])
            return

        task = asyncio.create_task(self._handle_request(request_id, payload))
        self._tasks[request_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(request_id, None))

    async def _handle_request(self, request_id: str, payload: dict[str, Any]) -> None:
        # past this, the relay stops waiting for the response
        deadline = time.monotonic() + self._request_timeout
        response = await self._call_adapter(request_id, payload)
        frame = {"type": "response", "request_id": request_id, "payload": response}
        self._drop_expired_unacked()
        self._unacked[request_id] = (deadline, frame)
        ws = self._ws
        if ws is None or not await _send(ws, frame):
            logger.info("relay disconnected, holding response for %s", request_id)

    def _drop_expired_unacked(self) -> None:
        """Drop the responses the relay has given up on, never acked."""
        now = time.monotonic()
        for request_id, (deadline, _) in list(self._unacked.items()):
            if now > deadline:
                del self._unacked[request_id]
                logger.warning("dropped response for %s, never acked", request_id)

    async def _resend_unacked(self, ws: ClientConnection) -> None:
        self._drop_expired_unacked()
        resent = 0
        for _, frame in list(self._unacked.values()):
            if not await _send(ws, frame):
                break
            resent += 1
        if resent:
            logger.info("resent %d unacked responses", resent)

    async def _call_adapter(
        self, request_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        body = payload.get("body", {})
        adapter_headers = _headers_for_adapter(payload.get("headers"))
        path = payload.get("path", ADAPTER_CHAT_PATH)
        if not isinstance(path, str) or path not in ADAPTER_ALLOWED_PATHS:
            logger.warning("rejected unknown adapter path %r", path)
            return _error_payload(404, "unknown adapter path")
        url = f"{self.adapter_url}{path}"

        try:
            resp = await self._http.post(url, json=body, headers=adapter_headers)
            response_body = resp.json()
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            logger.warning("adapter unavailable (%r)", exc)
            return _error_payload(503, "adapter unavailable")
        except Exception:
            logger.exception("error handling request %s", request_id)
            return _error_payload(500, "internal relay client error")

        # we forward as is, even if we have an error, but we log here
        # as well for visibility
        if resp.status_code >= 400:
            logger.warning(
                "adapter returned %d for %s: %s",
                resp.status_code,
                request_id,
                _for_log(response_body),
            )

        return {
            "status": resp.status_code,
            "headers": {"content-type": "application/json"},
            "body": response_body,
        }


class _UnsupportedRelay(Exception):
    """The relay speaks another protocol version."""

    def __init__(self, version: object) -> None:
        super().__init__(f"relay speaks protocol version {version}")
        self.version = version


def _parse_frame(raw: str | bytes) -> dict[str, Any] | None:
    """A frame's JSON object, or None if it isn't one."""
    try:
        data = json.loads(raw)
    except ValueError:  # not JSON, or bytes that aren't UTF-8
        return None
    return data if isinstance(data, dict) else None


async def _send(ws: ClientConnection, frame: dict[str, Any]) -> bool:
    try:
        await ws.send(json.dumps(frame))
    except ConnectionClosed:
        return False
    return True


def _for_log(value: object) -> str:
    """Render a value for a log line, truncated past ``MAX_LOG_BODY_CHARS``."""
    text = json.dumps(value, default=str)
    extra = len(text) - MAX_LOG_BODY_CHARS
    if extra > 0:
        return f"{text[:MAX_LOG_BODY_CHARS]}... ({extra} chars truncated)"
    return text


def _error_payload(status: int, message: str) -> dict[str, Any]:
    return {
        "status": status,
        "headers": {"content-type": "application/json"},
        "body": {"error": {"message": message}},
    }
