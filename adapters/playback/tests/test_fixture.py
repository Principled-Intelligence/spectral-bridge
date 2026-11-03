import json
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent.parent / "fixtures" / "two_agents.json"


def test_the_fixture_folds_through_spectrograph_s_wire():
    wire = pytest.importorskip("traces.wire")  # spectrograph is not a dependency here
    fixture = json.loads(FIXTURE.read_text())
    state = wire.initial_state()
    for batch in fixture["turns"]:
        for event in batch:
            wire.fold(state, event)
    for event in fixture["end"]:
        wire.fold(state, event)
    assert state.trace.complete is True
    assert len(state.trace.nodes) == 2
