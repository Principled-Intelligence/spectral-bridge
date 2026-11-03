async def _turn(client, conversation_id, turn, text="hello"):
    return await client.post(
        "/v1/spectrograph",
        json={"conversation_id": conversation_id, "turn": turn, "input": text},
    )


async def _end(client, conversation_id):
    return await client.post(
        "/v1/spectrograph", json={"conversation_id": conversation_id, "end": True}
    )


async def test_health(client):
    r = await client.get("/health")
    assert r.json() == {"status": "ok"}


async def test_turn_one_returns_the_first_batch_with_input_as_the_user_message(client):
    r = await client.post("/v1/spectrograph", json={"conversation_id": "c", "turn": 1, "input": "hello"})
    events = r.json()["events"]
    assert events[0]["event"] == "trace_opened"
    users = [e for e in events if e["event"] == "item_appended" and e["item"].get("role") == "user"]
    assert [u["item"]["content"] for u in users] == ["hello"]


async def test_a_repeated_turn_returns_the_same_events(client):
    first = await _turn(client, "c", 1)
    again = await _turn(client, "c", 1)
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json()


async def test_an_out_of_order_turn_is_409(client):
    r = await _turn(client, "c", 2)
    assert r.status_code == 409
    assert r.json() == {"error": {"message": "expected turn 1"}}


async def test_a_turn_past_the_fixture_is_422(client):
    assert (await _turn(client, "c", 1)).status_code == 200
    assert (await _turn(client, "c", 2)).status_code == 200
    r = await _turn(client, "c", 3)
    assert r.status_code == 422
    assert r.json() == {"error": {"message": "the fixture has 2 turns"}}


async def test_end_returns_the_rest_and_closes(client):
    await _turn(client, "c", 1)
    await _turn(client, "c", 2)
    events = (await _end(client, "c")).json()["events"]
    assert events[-1] == {"event": "trace_closed", "complete": True}
    assert any(e.get("item", {}).get("source") == "tool:lookup" for e in events)


async def test_end_without_close_when_configured(client, monkeypatch):
    monkeypatch.setenv("PLAYBACK_CLOSE_ON_END", "0")
    await _turn(client, "c", 1)
    await _turn(client, "c", 2)
    events = (await _end(client, "c")).json()["events"]
    assert events
    assert all(e["event"] != "trace_closed" for e in events)


async def test_end_for_an_unknown_conversation_returns_no_events(client):
    assert (await _end(client, "nobody")).json() == {"events": []}


async def test_end_drops_the_conversation_state(client):
    await _turn(client, "c", 1)
    await _end(client, "c")
    assert (await _end(client, "c")).json() == {"events": []}
    assert (await _turn(client, "c", 1)).status_code == 200
