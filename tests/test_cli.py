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
