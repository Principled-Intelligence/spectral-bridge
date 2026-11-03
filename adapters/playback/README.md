# playback adapter

A test adapter that serves recorded spectrograph wire events from `POST /v1/spectrograph`, so an end-to-end run
needs no real agent. It is not registered in the CLI's `ADAPTERS`; run it next to the relay client:

```bash
PLAYBACK_FIXTURE=adapters/playback/fixtures/two_agents.json \
  uvicorn spectral_bridge_playback.app:app --port 8000
spectral-bridge start-relay --adapter-url http://localhost:8000 ...
```

## Configuration

- `PLAYBACK_FIXTURE` (required): path of a fixture.
- `PLAYBACK_CLOSE_ON_END` (default `1`): with `0`, the end reply omits its final `trace_closed`, so Spectral's
  force close is exercised.

## Fixture shape

`{"turns": [[event, ...], ...], "end": [event, ...]}`. Each turn's batch holds exactly one `item_appended` of a
`{"item": "message", "role": "user"}` item, the persona's message; the adapter replaces its `content` with the
request's `input`. `end` ends with `{"event": "trace_closed", "complete": true}`. The bundled
`fixtures/two_agents.json` has two agents (`/root`, `/root/researcher`), two turns and a `tool:lookup` receipt at
the end.

## Behaviour

State is kept per `conversation_id`, in memory.

- Turn N (the next expected) answers batch N. A repeat of the last answered turn answers the same events again.
- Any other turn is 409 (`expected turn k`); a turn past the fixture is 422 (`the fixture has k turns`).
- `end: true` answers the `end` batch (without its final `trace_closed` when `PLAYBACK_CLOSE_ON_END=0`) and drops
  the conversation. An unknown conversation answers `{"events": []}`.
- `GET /health` answers `{"status": "ok"}`.

To check a fixture against spectrograph's wire:
`uv run --with ../spectrograph pytest adapters/playback/tests/test_fixture.py`.
