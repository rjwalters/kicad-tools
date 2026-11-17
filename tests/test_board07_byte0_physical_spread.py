"""Board 07's SDRAM_BYTE0 must be matched on its real pad-to-pad paths (#9118).

``validate.py`` sums every copper segment on a net, so a same-net loop the
signal never travels counts as matched length.  ``physical_path.py`` measures
the shortest path through the copper instead.  On the post-#9102 copper DQ7 and
LDQM carried such loops and the real byte-0 spread was 7.51 mm against
``group_skew_mm`` 5.0.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1] / "boards/07-matchgroup-test/real_design"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"board07_{name}", ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def physical():
    return _load("physical_path")


@pytest.fixture(scope="module")
def record():
    return _load("repair_byte0_loops_9118")


@pytest.fixture(scope="module")
def tolerance():
    constraints = json.loads(
        (ROOT.parent / "output/sdram_constraints.json").read_text(encoding="utf-8")
    )
    return constraints["group_skew_mm"]


def _board(path: Path):
    from kicad_tools.schema.pcb import PCB

    return PCB.load(path)


def _pad(net: str, name: str, x: float, y: float):
    return SimpleNamespace(
        number=name,
        net_name=net,
        position=(x, y),
        size=(0.4, 0.4),
        rotation=0.0,
        layers=["F.Cu"],
    )


def _seg(net: str, a, b):
    return SimpleNamespace(net_name=net, start=a, end=b, width=0.18, layer="F.Cu")


def _synthetic(segments):
    pads = [_pad("N", "1", 0.0, 0.0), _pad("N", "2", 10.0, 0.0)]
    footprint = SimpleNamespace(reference="U", position=(0.0, 0.0), rotation=0.0, pads=pads)
    return SimpleNamespace(footprints=[footprint], vias=[], segments=segments)


def test_path_measure_ignores_a_loop_through_the_pad(physical):
    """A trunk that runs through its own pad and back is not matched length."""
    straight = [_seg("N", (0.0, 0.0), (10.0, 0.0))]
    # The trunk runs on past pad 2, doubles back along a parallel 0.05 mm away,
    # and merges with itself: summed copper grows by 12 mm, the path by none.
    looped = straight + [
        _seg("N", (10.0, 0.0), (16.0, 0.0)),
        _seg("N", (16.0, 0.0), (16.0, 0.05)),
        _seg("N", (16.0, 0.05), (10.0, 0.05)),
    ]
    assert physical.path_length(_synthetic(straight), "N")[0] == pytest.approx(10.0, abs=0.01)
    assert physical.path_length(_synthetic(looped), "N")[0] == pytest.approx(10.0, abs=0.01)


def test_path_measure_counts_real_series_length(physical):
    detour = [
        _seg("N", (0.0, 0.0), (3.0, 0.0)),
        _seg("N", (3.0, 0.0), (3.0, 2.0)),
        _seg("N", (3.0, 2.0), (4.0, 2.0)),
        _seg("N", (4.0, 2.0), (4.0, 0.0)),
        _seg("N", (4.0, 0.0), (10.0, 0.0)),
    ]
    assert physical.path_length(_synthetic(detour), "N")[0] == pytest.approx(14.0, abs=0.05)


def test_path_measure_rejects_a_net_with_an_open(physical):
    with pytest.raises(ValueError, match="not connected"):
        physical.path_length(_synthetic([_seg("N", (0.0, 0.0), (5.0, 0.0))]), "N")


def test_byte0_physical_spread_is_within_the_group_skew(physical, tolerance):
    lengths = physical.group_lengths(_board(ROOT / "reviewed-routing/sdram_demo.kicad_pcb"))
    assert set(lengths) == set(physical.BYTE0)
    assert physical.spread(lengths) <= tolerance, lengths


def test_pre_repair_byte0_copper_fails_the_physical_spread(physical, record, tolerance, tmp_path):
    """Strip the series length back out: the loop-free copper is 7.5 mm apart.

    The loops carry no signal, so deleting them changes no pad-to-pad length.
    Replacing each trombone with its straight host leg therefore reproduces the
    post-#9102 physical lengths, the figures #9118 was filed with.
    """
    text = (ROOT / "reviewed-routing/sdram_demo.kicad_pcb").read_text(encoding="utf-8")
    for net, layer, points in record.TROMBONES:
        for pair in zip(points, points[1:], strict=False):
            text = record.R.delete_segment(text, net, layer, pair)
        text = record.R.add_segments(
            text, [record.segment_block(net, layer, (points[0], points[-1]))]
        )
    stripped = tmp_path / "stripped.kicad_pcb"
    stripped.write_text(text, encoding="utf-8")
    lengths = physical.group_lengths(_board(stripped))
    assert physical.spread(lengths) > tolerance
    assert lengths["DQ7"] == pytest.approx(57.07, abs=0.05)
    assert lengths["LDQM"] == pytest.approx(56.02, abs=0.05)


def test_byte0_carries_no_loop_on_any_net(physical):
    """Summed copper must not exceed the real path by more than a stray staircase."""
    board = _board(ROOT / "reviewed-routing/sdram_demo.kicad_pcb")
    for net in physical.BYTE0:
        copper = sum(math.dist(s.start, s.end) for s in board.segments if s.net_name == net)
        path, _ = physical.path_length(board, net)
        assert copper - path < 1.5, f"{net}: {copper - path:.2f} mm of off-path copper"


def test_repair_leaves_tolerances_and_authored_source_alone(record):
    constraints = json.loads(
        (ROOT.parent / "output/sdram_constraints.json").read_text(encoding="utf-8")
    )
    assert constraints["group_skew_mm"] == 5.0
    assert constraints["clock_relative_tolerance_mm"] == 10.0
    assert (
        hashlib.sha256(record.AUTHORED.read_bytes()).hexdigest()
        == record.INPUT_SHA256[record.AUTHORED]
    )
    # Re-running the record on its own output must refuse, never re-apply.
    assert (
        hashlib.sha256(record.REVIEWED.read_bytes()).hexdigest()
        != record.INPUT_SHA256[record.REVIEWED]
    )


def test_repair_trombones_are_axis_or_45_and_add_the_stated_length(record):
    added = {"DQ7": 5.93, "LDQM": 6.98}
    for net, _layer, points in record.TROMBONES:
        for (x1, y1), (x2, y2) in zip(points, points[1:], strict=False):
            dx, dy = round(abs(x2 - x1), 4), round(abs(y2 - y1), 4)
            assert dx == 0 or dy == 0 or dx == dy
        run = sum(math.dist(a, b) for a, b in zip(points, points[1:], strict=False))
        assert run - math.dist(points[0], points[-1]) == pytest.approx(added[net], abs=0.001)


def test_reviewed_manifest_pins_the_repaired_copper():
    manifest = json.loads((ROOT / "reviewed-routing/manifest.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256((ROOT / "reviewed-routing/sdram_demo.kicad_pcb").read_bytes())
    assert manifest["sha256"]["sdram_demo.kicad_pcb"] == digest.hexdigest()
