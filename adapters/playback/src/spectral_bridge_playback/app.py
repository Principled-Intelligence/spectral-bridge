"""Playback adapter.

Serves recorded spectrograph wire events from `POST /v1/spectrograph`, so an
end-to-end run needs no real agent. Configured by `PLAYBACK_FIXTURE` (required)
and `PLAYBACK_CLOSE_ON_END` (default "1").
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="spectral-bridge playback adapter")

# conversation_id -> the last turn answered
_answered: dict[str, int] = {}
_fixture: dict[str, Any] | None = None


def reset() -> None:
    """Forget every conversation and the loaded fixture."""
    global _fixture
    _answered.clear()
    _fixture = None


def _load() -> dict[str, Any]:
    global _fixture
    if _fixture is None:
        with open(os.environ["PLAYBACK_FIXTURE"], encoding="utf-8") as f:
            _fixture = json.load(f)
    return _fixture


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message}}, status_code=status)


def _batch(fixture: dict[str, Any], turn: int, text: str) -> list[dict[str, Any]]:
    events = copy.deepcopy(fixture["turns"][turn - 1])
    for event in events:
        item = event.get("item") or {}
        if event["event"] == "item_appended" and item.get("item") == "message" and item.get("role") == "user":
            item["content"] = text
    return events


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/spectrograph")
async def spectrograph(request: Request) -> JSONResponse:
    body = await request.json()
    conversation_id = body["conversation_id"]
    fixture = _load()

    if body.get("end") is True:
        if conversation_id not in _answered:
            return JSONResponse({"events": []})
        del _answered[conversation_id]
        events = fixture["end"]
        if os.environ.get("PLAYBACK_CLOSE_ON_END", "1") == "0":
            events = events[:-1]
        return JSONResponse({"events": events})

    turn = body["turn"]
    last = _answered.get(conversation_id, 0)
    if turn > len(fixture["turns"]):
        return _error(422, f"the fixture has {len(fixture['turns'])} turns")
    if turn == last + 1 or (turn == last and last > 0):
        _answered[conversation_id] = turn
        return JSONResponse({"events": _batch(fixture, turn, body.get("input", ""))})
    return _error(409, f"expected turn {last + 1}")
