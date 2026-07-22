"""Pass-through adapter.

Forwards OpenAI-compatible requests (Chat Completions and/or Responses) to a
local endpoint. Use when the target already exposes /v1/chat/completions
and/or /v1/responses.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

log = logging.getLogger(__name__)

TARGET_URL = os.environ.get("TARGET_URL", "").rstrip("/")

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


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    messages: list[dict[str, Any]]
    model: str | None = None


class ResponsesRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    input: str | list = ""
    model: str | None = None


def _forward_headers(request: Request) -> dict[str, str]:
    return {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in _HOP_BY_HOP_HEADERS
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not TARGET_URL:
        raise RuntimeError("TARGET_URL environment variable is required")
    client = httpx.AsyncClient(timeout=30)
    app.state.http_client = client
    try:
        yield
    finally:
        await client.aclose()


app = FastAPI(title="spectral-bridge pass-through adapter", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    log.warning("request validation failed: %s", exc.errors())
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.get("/health")
async def health():
    return {"status": "ok", "target": TARGET_URL}


async def _forward(path: str, body: BaseModel, request: Request) -> JSONResponse:
    url = f"{TARGET_URL}{path}"
    headers = _forward_headers(request)
    client: httpx.AsyncClient = request.app.state.http_client

    try:
        resp = await client.post(
            url, json=body.model_dump(exclude_unset=True), headers=headers
        )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        log.warning("target unreachable: %s", exc)
        return JSONResponse(
            status_code=502,
            content={"error": {"message": "target unreachable"}},
        )

    try:
        content = resp.json()
    except Exception:
        content = {"error": {"message": resp.text}}

    if resp.status_code >= 400:
        log.warning("target returned %s: %s", resp.status_code, content)

    return JSONResponse(status_code=resp.status_code, content=content)


@app.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest, request: Request):
    return await _forward("/v1/chat/completions", body, request)


@app.post("/v1/responses")
async def responses(body: ResponsesRequest, request: Request):
    return await _forward("/v1/responses", body, request)
