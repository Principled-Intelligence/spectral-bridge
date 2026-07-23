from unittest.mock import patch

from click.testing import CliRunner

from spectral_bridge.cli.main import cli


def test_start_relay_rejects_non_loopback_adapter(monkeypatch):
    monkeypatch.setenv("SPECTRAL_BRIDGE_API_KEY", "key")
    result = CliRunner().invoke(
        cli,
        [
            "start-relay",
            "--relay-url",
            "wss://relay.example.com/connect",
            "--adapter-url",
            "http://evil.example.com",
        ],
    )
    assert result.exit_code != 0
    # click 8.3 captures stderr separately; ClickException prints there.
    assert "loopback" in result.stderr


def test_start_relay_insecure_adapter_flag_bypasses_loopback(monkeypatch):
    monkeypatch.setenv("SPECTRAL_BRIDGE_API_KEY", "key")
    # Patch out the blocking event loop; we only care that construction
    # (hence loopback validation) succeeded with the flag set.
    with patch(
        "spectral_bridge.cli.main.asyncio.run",
        side_effect=lambda coro: coro.close(),
    ) as fake_run:
        result = CliRunner().invoke(
            cli,
            [
                "start-relay",
                "--relay-url",
                "wss://relay.example.com/connect",
                "--adapter-url",
                "http://evil.example.com",
                "--insecure-adapter",
            ],
        )
    assert result.exit_code == 0, result.output
    assert fake_run.called


def test_start_relay_defaults_to_spectral_relay(monkeypatch):
    from spectral_bridge.cli.defaults import SPECTRAL_RELAY_URL

    monkeypatch.setenv("SPECTRAL_BRIDGE_API_KEY", "key")
    monkeypatch.delenv("SPECTRAL_BRIDGE_RELAY_URL", raising=False)
    with patch("spectral_bridge.cli.main.RelayClient") as fake_client, patch(
        "spectral_bridge.cli.main.asyncio.run"
    ):
        result = CliRunner().invoke(
            cli,
            ["start-relay", "--adapter-url", "http://localhost:8000"],
        )
    assert result.exit_code == 0, result.output
    assert fake_client.call_args.args[0] == SPECTRAL_RELAY_URL


def test_start_relay_env_var_overrides_default(monkeypatch):
    monkeypatch.setenv("SPECTRAL_BRIDGE_API_KEY", "key")
    monkeypatch.setenv(
        "SPECTRAL_BRIDGE_RELAY_URL", "wss://other-platform.example.com/connect"
    )
    with patch("spectral_bridge.cli.main.RelayClient") as fake_client, patch(
        "spectral_bridge.cli.main.asyncio.run"
    ):
        result = CliRunner().invoke(
            cli,
            ["start-relay", "--adapter-url", "http://localhost:8000"],
        )
    assert result.exit_code == 0, result.output
    assert fake_client.call_args.args[0] == "wss://other-platform.example.com/connect"


def test_start_relay_flag_overrides_env_var(monkeypatch):
    monkeypatch.setenv("SPECTRAL_BRIDGE_API_KEY", "key")
    monkeypatch.setenv(
        "SPECTRAL_BRIDGE_RELAY_URL", "wss://other-platform.example.com/connect"
    )
    with patch("spectral_bridge.cli.main.RelayClient") as fake_client, patch(
        "spectral_bridge.cli.main.asyncio.run"
    ):
        result = CliRunner().invoke(
            cli,
            [
                "start-relay",
                "--relay-url",
                "wss://flag-wins.example.com/connect",
                "--adapter-url",
                "http://localhost:8000",
            ],
        )
    assert result.exit_code == 0, result.output
    assert fake_client.call_args.args[0] == "wss://flag-wins.example.com/connect"


def test_start_defaults_to_spectral_relay(monkeypatch):
    from spectral_bridge.cli.defaults import SPECTRAL_RELAY_URL

    monkeypatch.setenv("SPECTRAL_BRIDGE_API_KEY", "key")
    monkeypatch.delenv("SPECTRAL_BRIDGE_RELAY_URL", raising=False)
    with patch("spectral_bridge.cli.main.RelayClient") as fake_client, patch(
        "spectral_bridge.cli.main.asyncio.run"
    ), patch("spectral_bridge.cli.main._spawn_adapter") as fake_spawn, patch(
        "spectral_bridge.cli.main._wait_for_adapter"
    ):
        result = CliRunner().invoke(
            cli,
            ["start", "--adapter", "pass-through", "--target", "http://localhost:9999"],
        )
    assert result.exit_code == 0, result.output
    assert fake_spawn.called
    assert fake_client.call_args.args[0] == SPECTRAL_RELAY_URL


def test_start_env_var_overrides_default(monkeypatch):
    monkeypatch.setenv("SPECTRAL_BRIDGE_API_KEY", "key")
    monkeypatch.setenv(
        "SPECTRAL_BRIDGE_RELAY_URL", "wss://other-platform.example.com/connect"
    )
    with patch("spectral_bridge.cli.main.RelayClient") as fake_client, patch(
        "spectral_bridge.cli.main.asyncio.run"
    ), patch("spectral_bridge.cli.main._spawn_adapter"), patch(
        "spectral_bridge.cli.main._wait_for_adapter"
    ):
        result = CliRunner().invoke(
            cli,
            ["start", "--adapter", "pass-through", "--target", "http://localhost:9999"],
        )
    assert result.exit_code == 0, result.output
    assert fake_client.call_args.args[0] == "wss://other-platform.example.com/connect"


def test_start_flag_overrides_env_var(monkeypatch):
    monkeypatch.setenv("SPECTRAL_BRIDGE_API_KEY", "key")
    monkeypatch.setenv(
        "SPECTRAL_BRIDGE_RELAY_URL", "wss://other-platform.example.com/connect"
    )
    with patch("spectral_bridge.cli.main.RelayClient") as fake_client, patch(
        "spectral_bridge.cli.main.asyncio.run"
    ), patch("spectral_bridge.cli.main._spawn_adapter"), patch(
        "spectral_bridge.cli.main._wait_for_adapter"
    ):
        result = CliRunner().invoke(
            cli,
            [
                "start",
                "--adapter",
                "pass-through",
                "--target",
                "http://localhost:9999",
                "--relay-url",
                "wss://flag-wins.example.com/connect",
            ],
        )
    assert result.exit_code == 0, result.output
    assert fake_client.call_args.args[0] == "wss://flag-wins.example.com/connect"


def test_start_relay_strips_relay_url_whitespace(monkeypatch):
    monkeypatch.setenv("SPECTRAL_BRIDGE_API_KEY", "key")
    with patch("spectral_bridge.cli.main.RelayClient") as fake_client, patch(
        "spectral_bridge.cli.main.asyncio.run"
    ):
        result = CliRunner().invoke(
            cli,
            [
                "start-relay",
                "--relay-url",
                "  wss://padded.example.com/connect  ",
                "--adapter-url",
                "http://localhost:8000",
            ],
        )
    assert result.exit_code == 0, result.output
    assert fake_client.call_args.args[0] == "wss://padded.example.com/connect"
