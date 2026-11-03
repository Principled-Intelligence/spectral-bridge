from pathlib import Path

import httpx
import pytest

FIXTURE = Path(__file__).parent.parent / "fixtures" / "two_agents.json"


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setenv("PLAYBACK_FIXTURE", str(FIXTURE))
    monkeypatch.delenv("PLAYBACK_CLOSE_ON_END", raising=False)
    import spectral_bridge_playback.app as app_mod

    app_mod.reset()
    transport = httpx.ASGITransport(app=app_mod.app, raise_app_exceptions=True)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
