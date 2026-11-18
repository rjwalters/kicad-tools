"""Board 07's SDRAM_BYTE0 must be matched on its real pad-to-pad paths (#9118).

Before #9117, ``validate.py`` summed every copper segment on a net, so a
same-net loop the signal never travelled counted as matched length.
``TraceLengthAnalyzer.measure_net_path`` (#9117) walks the shared
copper-contact model instead and measures the shortest path through the
copper.  On the post-#9102 copper DQ7 and LDQM carried such loops and the
real byte-0 spread was 7.51 mm against ``group_skew_mm`` 5.0 (the shared
model reads 8.16 mm on the same stripped copper; the filed figures came from
the board-local model #9176 removed).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest

from kicad_tools.analysis.trace_length import TraceLengthAnalyzer
from kicad_tools.physics import Stackup
from kicad_tools.schema.pcb import PCB

ROOT = Path(__file__).resolve().parents[1] / "boards/07-matchgroup-test/real_design"

BYTE0 = ("DQ0", "DQ1", "DQ2", "DQ3", "DQ4", "DQ5", "DQ6", "DQ7", "LDQM")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"board07_{name}", ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def record():
    return _load("repair_byte0_loops_9118")


@pytest.fixture(scope="module")
def validate():
    return _load("validate")


@pytest.fixture(scope="module")
def tolerance():
    constraints = json.loads(
        (ROOT.parent / "output/sdram_constraints.json").read_text(encoding="utf-8")
    )
    return constraints["group_skew_mm"]


@pytest.fixture(scope="module")
def analyzer():
    return TraceLengthAnalyzer()


@pytest.fixture(scope="module")
def reviewed_board():
    return PCB.load(str(ROOT / "reviewed-routing/sdram_demo.kicad_pcb"))


def _measure_group(analyzer: TraceLengthAnalyzer, board: PCB, validate) -> dict[str, float]:
    """Per-net matched length, computed by the gate's own ``matched_length_mm``."""
    stack = Stackup.from_pcb(board)
    elevations = validate.copper_elevations(stack)
    vias_by_index = dict(enumerate(board.vias))
    lengths: dict[str, float] = {}
    for net in BYTE0:
        measurement = analyzer.measure_net_path(board, net)
        assert measurement.path_length_mm is not None, f"{net}: no connected path"
        segments = list(board.segments_in_net(board.get_net_by_name(net).number))
        lengths[net] = validate.matched_length_mm(measurement, segments, vias_by_index, elevations)
    return lengths


def _spread(lengths: dict[str, float]) -> float:
    return max(lengths.values()) - min(lengths.values())


def test_byte0_physical_spread_is_within_the_group_skew(
    analyzer, reviewed_board, validate, tolerance
):
    """Physical spread = pad-to-pad copper path plus via travel, per net.

    Lengths come from ``validate.matched_length_mm``, the same function the
    gate uses for ``group_skew_mm``, so this test and the gate cannot disagree
    (Issue #9198).  Via travel is included; the per-net lengths are in the
    assertion output.
    """
    lengths = _measure_group(analyzer, reviewed_board, validate)
    assert _spread(lengths) <= tolerance, lengths


def test_pre_repair_byte0_copper_fails_the_physical_spread(
    analyzer, record, validate, tolerance, tmp_path
):
    """Strip the series length back out: the loop-free copper is 8.16 mm apart.

    The loops carry no signal, so deleting them changes no pad-to-pad length.
    Replacing each trombone with its straight host leg therefore reproduces
    the post-#9102 physical lengths, the figures #9118 was filed with.
    Re-derived on the shared contact model (#9176): DQ7 57.02 mm and LDQM
    55.21 mm of copper path, where the removed ``physical_path.py`` model
    read 57.07/56.02.  Lengths here are the gate's matched length (#9198),
    copper path plus via travel: DQ7 57.59 mm (+0.57 mm of via travel) and
    LDQM 55.21 mm (no path vias), a spread of 8.63 mm.
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
    lengths = _measure_group(analyzer, PCB.load(str(stripped)), validate)
    assert _spread(lengths) > tolerance
    assert lengths["DQ7"] == pytest.approx(57.59, abs=0.05)
    assert lengths["LDQM"] == pytest.approx(55.21, abs=0.05)


def test_byte0_carries_no_loop_on_any_net(analyzer, reviewed_board, validate):
    """Off-path copper must stay under validate.py's off-path gate.

    The #9118 loops carried 6-7 mm of off-path copper per net; the repaired
    board reads at most 1.53 mm (LDQM) through the shared contact model.
    """
    for net in BYTE0:
        off_path = analyzer.measure_net_path(reviewed_board, net).off_path_length_mm
        assert off_path is not None
        assert off_path < validate.OFF_PATH_TOLERANCE_MM, (
            f"{net}: {off_path:.2f} mm of off-path copper"
        )


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
