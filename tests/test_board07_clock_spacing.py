"""Signal spacing must include both directions of a track/through-via pair."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1] / "boards/07-matchgroup-test/real_design/engineering"


@pytest.mark.parametrize("clock_is_via", [False, True])
def test_clock_guard_is_symmetric_across_through_via_layers(monkeypatch, clock_is_via):
    spec = importlib.util.spec_from_file_location("clock_spacing", ROOT / "clock_spacing.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    board = SimpleNamespace(
        footprints=[],
        segments=[
            SimpleNamespace(
                start=(0, 0),
                end=(4, 0),
                width=0.18,
                layer="In2.Cu",
                net_name="A0" if clock_is_via else "SDCLK",
            )
        ],
        vias=[
            SimpleNamespace(
                position=(2, 0.7),
                size=0.5,
                layers=["F.Cu", "B.Cu"],
                net_name="SDCLK" if clock_is_via else "A0",
            )
        ],
    )
    monkeypatch.setattr(module.PCB, "load", lambda _: board)
    report = module.audit(None)
    assert len(report["findings"]) == 1
    assert report["findings"][0]["edge_gap_mm"] == pytest.approx(0.36, abs=0.001)
    assert report["findings"][0]["layers"] == ["In2.Cu"]
    board.vias[0].position = (2, 1)
    assert module.audit(None)["findings"] == []
