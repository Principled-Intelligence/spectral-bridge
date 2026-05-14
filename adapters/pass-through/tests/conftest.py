import httpx
import pytest
from unittest.mock import AsyncMock

import spectral_bridge_passthrough.app as adapter_mod


@pytest.fixture
async def adapter_client():
    original_url = adapter_mod.TARGET_URL
    adapter_mod.TARGET_URL = "http://fake-target.example.com"
    try:
        transport = httpx.ASGITransport(app=adapter_mod.app, raise_app_exceptions=True)
        async with adapter_mod.lifespan(adapter_mod.app):
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                yield client
    finally:
        adapter_mod.TARGET_URL = original_url


@pytest.fixture
async def mock_target(adapter_client):
    mock = AsyncMock()
    original = adapter_mod.app.state.http_client
    adapter_mod.app.state.http_client = mock
    yield mock
    adapter_mod.app.state.http_client = original
