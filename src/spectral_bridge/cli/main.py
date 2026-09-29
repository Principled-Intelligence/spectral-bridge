"""spectral-bridge CLI.

Entry point for the `spectral-bridge` command.

Commands:
  start        — spawn a built-in adapter and connect the relay client
  start-relay  — connect the relay client to an already-running adapter
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
import threading
import time
from typing import IO

import click
import httpx
from rich.logging import RichHandler
from rich.text import Text

from spectral_bridge.client import (
    DEFAULT_MAX_WS_MESSAGE_BYTES,
    DEFAULT_REQUEST_TIMEOUT,
    RelayClient,
)
from spectral_bridge.cli.defaults import SPECTRAL_RELAY_URL

logger = logging.getLogger("spectral_bridge.cli")
adapter_logger = logging.getLogger("spectral_bridge.adapter")

# uvicorn's default log format: "WARNING:  message"
_UVICORN_LEVEL_PREFIX = re.compile(r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL):\s+")


class _SourceRichHandler(RichHandler):
    """Prefix each log line with where it comes from, docker-compose style."""

    def render_message(self, record: logging.LogRecord, message: str) -> Text:
        if record.name == adapter_logger.name:
            source, style = "adapter", "magenta"
        else:
            source, style = "bridge", "cyan"
        return Text.assemble(
            (f"{source:<7} | ", style), super().render_message(record, message)
        )


API_KEY_ENV = "SPECTRAL_BRIDGE_API_KEY"


def _relay_api_key_from_env() -> str:
    raw = os.environ.get(API_KEY_ENV, "").strip()
    if not raw:
        raise click.ClickException(f"set {API_KEY_ENV} to the relay API key")
    return raw


ADAPTERS = {
    "pass-through": {
        "module": "spectral_bridge_passthrough.app:app",
        "env_key": "TARGET_URL",
    },
}


def _wait_for_adapter(url: str, proc: subprocess.Popen, timeout: float = 10.0) -> None:
    """Block until the adapter's health endpoint responds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ret = proc.poll()
        if ret is not None:
            raise click.ClickException(
                f"adapter process exited with code {ret}, see its logs above"
            )
        try:
            r = httpx.get(f"{url}/health", timeout=2)
            if r.status_code == 200:
                return
        except (httpx.ConnectError, httpx.TimeoutException):
            pass
        time.sleep(0.2)
    raise click.ClickException("adapter did not become ready in time")


def _spawn_adapter(adapter: str, target: str, port: int) -> subprocess.Popen:
    """Spawn a built-in adapter as a subprocess via uvicorn."""
    spec = ADAPTERS[adapter]

    env = {**os.environ, spec["env_key"]: target}
    env.pop(API_KEY_ENV, None)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            spec["module"],
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    # the pipe must be drained for the adapter's whole life: once full (~64 KiB)
    # the adapter blocks on its next log write
    threading.Thread(
        target=_relog_adapter_output, args=(proc.stdout,), daemon=True
    ).start()
    return proc


def _relog_adapter_output(stream: IO[str]) -> None:
    """Re-log each line the adapter prints, under the adapter's logger."""
    # unprefixed lines (e.g. traceback frames) keep the previous line's level
    level = logging.WARNING
    for line in stream:
        line = line.rstrip()
        if not line:
            continue
        match = _UVICORN_LEVEL_PREFIX.match(line)
        if match:
            level = getattr(logging, match[1])
            line = line[match.end() :]
        adapter_logger.log(level, "%s", line)


@click.group()
def cli() -> None:
    """spectral-bridge — bridge local AI targets to the cloud."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[_SourceRichHandler(show_path=False, markup=False)],
    )


@cli.command("start-relay")
@click.option(
    "--relay-url",
    envvar="SPECTRAL_BRIDGE_RELAY_URL",
    default=SPECTRAL_RELAY_URL,
    show_default=True,
    help="Relay server WebSocket URL; defaults to the Spectral relay "
    "(override to use another platform, env: SPECTRAL_BRIDGE_RELAY_URL)",
)
@click.option(
    "--adapter-url",
    required=True,
    help="URL of the running adapter (e.g. http://localhost:8000)",
)
@click.option(
    "--insecure-relay",
    is_flag=True,
    help="Allow ws:// to the relay (not spec conforming; development only)",
)
@click.option(
    "--insecure-adapter",
    is_flag=True,
    help="Allow a non-loopback adapter URL (development only; the adapter is "
    "normally reached over localhost)",
)
@click.option(
    "--max-ws-message-bytes",
    type=click.IntRange(min=1),
    default=DEFAULT_MAX_WS_MESSAGE_BYTES,
    show_default=True,
    help="Maximum incoming WebSocket message size from the relay (bytes)",
)
@click.option(
    "--request-timeout",
    type=click.FloatRange(min=0, min_open=True),
    default=DEFAULT_REQUEST_TIMEOUT,
    show_default=True,
    help="Max seconds to wait for the adapter to return a completion "
    "(default is matched to the Spectral relay; on another platform keep "
    "it >= that relay's server-side timeout)",
)
def start_relay(
    relay_url: str,
    adapter_url: str,
    insecure_relay: bool,
    insecure_adapter: bool,
    max_ws_message_bytes: int,
    request_timeout: float,
) -> None:
    """Connect the relay client to an already-running adapter."""
    relay_url = relay_url.strip()
    api_key = _relay_api_key_from_env()
    try:
        client = RelayClient(
            relay_url,
            api_key,
            adapter_url,
            insecure_relay=insecure_relay,
            insecure_adapter=insecure_adapter,
            max_ws_message_bytes=max_ws_message_bytes,
            request_timeout=request_timeout,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        logger.info("shutting down")


@cli.command()
@click.option(
    "--relay-url",
    envvar="SPECTRAL_BRIDGE_RELAY_URL",
    default=SPECTRAL_RELAY_URL,
    show_default=True,
    help="Relay server WebSocket URL; defaults to the Spectral relay "
    "(override to use another platform, env: SPECTRAL_BRIDGE_RELAY_URL)",
)
@click.option(
    "--adapter",
    required=True,
    type=click.Choice(list(ADAPTERS)),
    help="Built-in adapter to start",
)
@click.option("--target", required=True, help="Target URL passed to the adapter")
@click.option(
    "--port", default=8840, type=int, help="Local port for the adapter (default: 8840)"
)
@click.option(
    "--insecure-relay",
    is_flag=True,
    help="Allow ws:// to the relay (not spec conforming; development only)",
)
@click.option(
    "--max-ws-message-bytes",
    type=click.IntRange(min=1),
    default=DEFAULT_MAX_WS_MESSAGE_BYTES,
    show_default=True,
    help="Maximum incoming WebSocket message size from the relay (bytes)",
)
@click.option(
    "--request-timeout",
    type=click.FloatRange(min=0, min_open=True),
    default=DEFAULT_REQUEST_TIMEOUT,
    show_default=True,
    help="Max seconds to wait for the adapter to return a completion "
    "(default is matched to the Spectral relay; on another platform keep "
    "it >= that relay's server-side timeout)",
)
def start(
    relay_url: str,
    adapter: str,
    target: str,
    port: int,
    insecure_relay: bool,
    max_ws_message_bytes: int,
    request_timeout: float,
) -> None:
    """Start a built-in adapter and connect the relay client."""
    relay_url = relay_url.strip()
    api_key = _relay_api_key_from_env()
    logger.info(
        "starting %s adapter, forwarding traffic from port %d to target %s",
        adapter,
        port,
        target,
    )
    proc = _spawn_adapter(adapter, target, port)

    adapter_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_adapter(adapter_url, proc)
    except click.ClickException:
        proc.terminate()
        raise

    logger.info("adapter ready")
    try:
        client = RelayClient(
            relay_url,
            api_key,
            adapter_url,
            insecure_relay=insecure_relay,
            max_ws_message_bytes=max_ws_message_bytes,
            request_timeout=request_timeout,
        )
    except ValueError as exc:
        proc.terminate()
        proc.wait(timeout=5)
        raise click.ClickException(str(exc)) from exc

    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        proc.terminate()
        proc.wait(timeout=5)


if __name__ == "__main__":
    cli()
