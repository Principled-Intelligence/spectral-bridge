import httpx
import pytest

import spectral_bridge_passthrough.app as adapter_mod


async def test_health_returns_ok(adapter_client):
    response = await adapter_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_health_includes_target_url(adapter_client):
    response = await adapter_client.get("/health")
    assert response.json()["target"] == "http://fake-target.example.com"


async def test_completions_forwards_body_to_target(adapter_client, mock_target):
    body = {"model": "test", "messages": [{"role": "user", "content": "hello"}]}
    mock_target.post.return_value = httpx.Response(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": "hi"}}]},
    )

    await adapter_client.post("/v1/chat/completions", json=body)

    call_kwargs = mock_target.post.call_args.kwargs
    assert call_kwargs["json"] == body


async def test_completions_returns_target_response(adapter_client, mock_target):
    target_body = {"choices": [{"message": {"role": "assistant", "content": "reply"}}]}
    mock_target.post.return_value = httpx.Response(201, json=target_body)

    response = await adapter_client.post(
        "/v1/chat/completions",
        json={"model": "test", "messages": []},
    )

    assert response.status_code == 201
    assert response.json() == target_body


async def test_completions_strips_hop_by_hop_headers(adapter_client, mock_target):
    mock_target.post.return_value = httpx.Response(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
    )

    await adapter_client.post(
        "/v1/chat/completions",
        json={"model": "test", "messages": []},
        headers={
            "connection": "keep-alive",
            "transfer-encoding": "chunked",
            "host": "relay.example.com",
            "x-custom": "value",
        },
    )

    forwarded_headers = mock_target.post.call_args.kwargs["headers"]
    assert "connection" not in forwarded_headers
    assert "transfer-encoding" not in forwarded_headers
    assert "host" not in forwarded_headers


async def test_completions_preserves_other_headers(adapter_client, mock_target):
    mock_target.post.return_value = httpx.Response(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
    )

    await adapter_client.post(
        "/v1/chat/completions",
        json={"model": "test", "messages": []},
        headers={"x-custom-header": "sentinel-value"},
    )

    forwarded_headers = mock_target.post.call_args.kwargs["headers"]
    assert forwarded_headers.get("x-custom-header") == "sentinel-value"


async def test_target_unreachable_returns_502(adapter_client, mock_target):
    mock_target.post.side_effect = httpx.ConnectError("connection refused")

    response = await adapter_client.post(
        "/v1/chat/completions",
        json={"model": "test", "messages": []},
    )

    assert response.status_code == 502
    assert "error" in response.json()


async def test_target_timeout_returns_502(adapter_client, mock_target):
    mock_target.post.side_effect = httpx.TimeoutException("timed out")

    response = await adapter_client.post(
        "/v1/chat/completions",
        json={"model": "test", "messages": []},
    )

    assert response.status_code == 502
    assert "error" in response.json()


async def test_missing_target_url_raises_on_startup():
    original = adapter_mod.TARGET_URL
    adapter_mod.TARGET_URL = ""
    try:
        with pytest.raises(RuntimeError, match="TARGET_URL"):
            async with adapter_mod.lifespan(adapter_mod.app):
                pass
    finally:
        adapter_mod.TARGET_URL = original
