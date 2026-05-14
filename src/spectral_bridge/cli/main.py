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
import subprocess
import sys
import time

import click
import httpx
from rich.logging import RichHandler

from spectral_bridge.client import DEFAULT_MAX_WS_MESSAGE_BYTES, RelayClient

logger = logging.getLogger("spectral_bridge.cli")

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
            stderr = proc.stderr.read() if proc.stderr else ""
            raise click.ClickException(
                f"adapter process exited with code {ret}\n{stderr}"
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
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc


@click.group()
def cli() -> None:
    """spectral-bridge — bridge local AI targets to the cloud."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(show_path=False, markup=False)],
    )


@cli.command("start-relay")
@click.option(
    "--relay-url",
    required=True,
    help="Relay server WebSocket URL (e.g. wss://relay.example.com/connect)",
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
    "--max-ws-message-bytes",
    type=click.IntRange(min=1),
    default=DEFAULT_MAX_WS_MESSAGE_BYTES,
    show_default=True,
    help="Maximum incoming WebSocket message size from the relay (bytes)",
)
def start_relay(
    relay_url: str,
    adapter_url: str,
    insecure_relay: bool,
    max_ws_message_bytes: int,
) -> None:
    """Connect the relay client to an already-running adapter."""
    api_key = _relay_api_key_from_env()
    try:
        client = RelayClient(
            relay_url,
            api_key,
            adapter_url,
            insecure_relay=insecure_relay,
            max_ws_message_bytes=max_ws_message_bytes,
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
    required=True,
    help="Relay server WebSocket URL (e.g. wss://relay.example.com/connect)",
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
def start(
    relay_url: str,
    adapter: str,
    target: str,
    port: int,
    insecure_relay: bool,
    max_ws_message_bytes: int,
) -> None:
    """Start a built-in adapter and connect the relay client."""
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
