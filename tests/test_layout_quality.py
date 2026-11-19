"""Tests for layout-quality metrics (issue #9594).

Covers the shared ``analyze_layout_quality`` metric set on synthetic PCBs
with known counts, the via classification rule, the buried-vertex
exemption, arcs, the KiCad-10 name-only net dialect, rerun stability, and
the fleet-router benchmark's fingerprint integration (the block is
reported per case and deliberately excluded from the placement
comparison).
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import pytest

from kicad_tools.analysis import analyze_layout_quality
from kicad_tools.schema.pcb import PCB

REPO_ROOT = Path(__file__).resolve().parent.parent

# One of each corner class, each on its own net, plus vias of each kind
# and a differential pair with a known skew. Corner geometry:
#   SIG  -- 90 deg interior at (15, 5)
#   BEND -- 45 deg direction change (135 deg interior): not a corner
#   FOLD -- 135 deg direction change (45 deg interior): acute
#   TURN -- ~5.2 deg interior at (35, 25): turn-back
KNOWN_COUNTS_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "SIG")
  (net 2 "BEND")
  (net 3 "FOLD")
  (net 4 "TURN")
  (net 5 "GND")
  (net 6 "USB_D+")
  (net 7 "USB_D-")

  (gr_rect (start 0 0) (end 50 30) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (segment (start 5 5) (end 15 5) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 15 5) (end 15 15) (width 0.2) (layer "F.Cu") (net 1))

  (segment (start 5 25) (end 12 25) (width 0.2) (layer "F.Cu") (net 2))
  (segment (start 12 25) (end 15.5355 21.4645) (width 0.2) (layer "F.Cu") (net 2))

  (segment (start 25 5) (end 35 5) (width 0.2) (layer "F.Cu") (net 3))
  (segment (start 35 5) (end 27.93 12.07) (width 0.2) (layer "F.Cu") (net 3))

  (segment (start 25 25) (end 35 25) (width 0.2) (layer "F.Cu") (net 4))
  (segment (start 35 25) (end 25.2 25.9) (width 0.2) (layer "F.Cu") (net 4))

  (segment (start 5 15) (end 10 15) (width 0.2) (layer "F.Cu") (net 6))
  (segment (start 5 17) (end 15 17) (width 0.2) (layer "F.Cu") (net 7))

  (via (at 45 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 5))
  (via (at 45 25) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
  (via (at 45 15) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu"))
)
"""

# A 90 deg join whose vertex sits at the centre of a same-net pad: a
# joint, not a bend -- skipped like sharp_trace_corner skips it. A second
# join on the same net away from copper stays counted.
BURIED_VERTEX_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "PNET")

  (gr_rect (start 0 0) (end 50 40) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (footprint "TestPoint:TestPoint_Pad"
    (layer "F.Cu")
    (at 20 20)
    (property "Reference" "TP1")
    (pad "1" smd rect (at 0 0) (size 1.5 1.5) (layers "F.Cu") (net 1 "PNET"))
  )

  (segment (start 15 20) (end 20 20) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 20 20) (end 20 28) (width 0.2) (layer "F.Cu") (net 1))

  (segment (start 30 10) (end 40 10) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 40 10) (end 40 20) (width 0.2) (layer "F.Cu") (net 1))
)
"""

# Arcs contribute length but no corners; collinear pass-throughs (180 deg
# interior) are not corners either.
ARCS_AND_PASSTHROUGH_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "ARCNET")
  (net 2 "STRAIGHT")

  (gr_rect (start 0 0) (end 60 30) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (segment (start 0 0) (end 5 0) (width 0.2) (layer "F.Cu") (net 1))
  (arc (start 45 15) (mid 40 20) (end 35 15) (width 0.2) (layer "F.Cu") (net 1))

  (segment (start 10 25) (end 15 25) (width 0.2) (layer "F.Cu") (net 2))
  (segment (start 15 25) (end 20 25) (width 0.2) (layer "F.Cu") (net 2))
)
"""

# KiCad-10 name-only dialect: segments carry (net "NAME") with no number,
# so grouping must key on the name -- two "NA" segments form one corner
# while an "NB" segment touching the same vertex stays a different net.
NAME_ONLY_PCB = """(kicad_pcb
  (version 20250114)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "NA")
  (net 2 "NB")

  (gr_rect (start 0 0) (end 40 40) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (segment (start 10 10) (end 20 10) (width 0.2) (layer "F.Cu") (net "NA"))
  (segment (start 20 10) (end 20 20) (width 0.2) (layer "F.Cu") (net "NA"))
  (segment (start 20 10) (end 30 5) (width 0.2) (layer "F.Cu") (net "NB"))
)
"""

# A 100x50 gr_rect outline with a 10x10 gr_line-ring cutout inside: the
# cutout must subtract. Regression for the review finding on #9614: the
# old add/subtract parity never fired because polygonize_full returns the
# outer face with the hole already punched, so every face sat at depth 0
# and the gross 5000 mm^2 was reported.
CUTOUT_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")

  (gr_rect (start 0 0) (end 100 50) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (gr_line (start 20 10) (end 30 10) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_line (start 30 10) (end 30 20) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_line (start 30 20) (end 20 20) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_line (start 20 20) (end 20 10) (stroke (width 0.1)) (layer "Edge.Cuts"))
)
"""

# A 20x20 cutout with a 5x5 island inside it: outer - cutout + island.
NESTED_ISLAND_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")

  (gr_rect (start 0 0) (end 100 50) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (gr_line (start 15 5) (end 35 5) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_line (start 35 5) (end 35 25) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_line (start 35 25) (end 15 25) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_line (start 15 25) (end 15 5) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (gr_line (start 22 12) (end 27 12) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_line (start 27 12) (end 27 17) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_line (start 27 17) (end 22 17) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_line (start 22 17) (end 22 12) (stroke (width 0.1)) (layer "Edge.Cuts"))
)
"""

# A gr_circle hole is not modelled by the segment accessor, so the
# segment set is incomplete: the documented policy is ``None`` rather
# than the gross rect area the circle would silently vanish into.
CIRCLE_CUTOUT_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")

  (gr_rect (start 0 0) (end 100 50) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_circle (center 25 15) (end 30 15) (stroke (width 0.1)) (layer "Edge.Cuts"))
)
"""


def _load_pcb(text: str) -> PCB:
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".kicad_pcb") as fh:
        fh.write(text)
        fh.flush()
        return PCB.load(fh.name)


def test_known_counts():
    report = analyze_layout_quality(_load_pcb(KNOWN_COUNTS_PCB))

    assert (report.right_angle_corners, report.acute_corners, report.turn_back_corners) == (1, 1, 1)
    assert report.corners_per_net == {
        "SIG": {"right_angle": 1, "acute": 0, "turn_back": 0},
        "FOLD": {"right_angle": 0, "acute": 1, "turn_back": 0},
        "TURN": {"right_angle": 0, "acute": 0, "turn_back": 1},
    }
    assert "BEND" not in report.corners_per_net  # 135 deg interior: not a corner

    assert (report.track_vias, report.ground_vias) == (2, 1)  # netless via is a track via

    expected = (
        math.dist((5, 5), (15, 5))
        + math.dist((15, 5), (15, 15))
        + math.dist((5, 25), (12, 25))
        + math.dist((12, 25), (15.5355, 21.4645))
        + math.dist((25, 5), (35, 5))
        + math.dist((35, 5), (27.93, 12.07))
        + math.dist((25, 25), (35, 25))
        + math.dist((35, 25), (25.2, 25.9))
        + math.dist((5, 15), (10, 15))
        + math.dist((5, 17), (15, 17))
    )
    assert report.total_track_length_mm == pytest.approx(expected, abs=1e-6)
    assert report.outline_area_mm2 == pytest.approx(50 * 30, abs=1e-6)


def test_to_dict_shape_and_rounding():
    data = analyze_layout_quality(_load_pcb(KNOWN_COUNTS_PCB)).to_dict()
    assert data["via"] == {"track": 2, "ground": 1, "total": 3}
    assert data["corners"]["right_angle"] == 1
    assert data["corners"]["acute"] == 1
    assert data["corners"]["turn_back"] == 1
    assert set(data["corners"]["per_net"]) == {"FOLD", "SIG", "TURN"}  # sorted keys
    assert data["total_track_length_mm"] == round(data["total_track_length_mm"], 3)
    assert data["outline_area_mm2"] == 1500.0
    assert len(data["diff_pair_skew"]) == 1
    skew = data["diff_pair_skew"][0]
    assert skew["net_positive"] == "USB_D+"
    assert skew["net_negative"] == "USB_D-"
    assert skew["length_positive_mm"] == pytest.approx(5.0, abs=1e-6)
    assert skew["length_negative_mm"] == pytest.approx(10.0, abs=1e-6)
    assert skew["skew_mm"] == pytest.approx(5.0, abs=1e-6)


def test_buried_vertex_skipped_open_corner_counted():
    report = analyze_layout_quality(_load_pcb(BURIED_VERTEX_PCB))
    # The pad-centre joint is exempt; the free bend on the same net counts.
    assert report.corners_per_net == {"PNET": {"right_angle": 1, "acute": 0, "turn_back": 0}}
    assert report.right_angle_corners == 1


def test_arcs_add_length_no_corners_and_passthrough_not_counted():
    report = analyze_layout_quality(_load_pcb(ARCS_AND_PASSTHROUGH_PCB))
    assert report.corners_per_net == {}
    assert (report.right_angle_corners, report.acute_corners, report.turn_back_corners) == (0, 0, 0)
    # 5 mm segment + semicircular arc (r=5 -> 5*pi mm) + 10 mm collinear pair.
    assert report.total_track_length_mm == pytest.approx(5 + 5 * math.pi + 10, abs=1e-6)
    assert report.outline_area_mm2 == pytest.approx(60 * 30, abs=1e-6)


def test_name_only_net_dialect_groups_by_name():
    report = analyze_layout_quality(_load_pcb(NAME_ONLY_PCB))
    # Exactly one 90 deg corner on NA; the NB leg meeting at the same
    # vertex is a different net and joins nothing.
    assert report.corners_per_net == {"NA": {"right_angle": 1, "acute": 0, "turn_back": 0}}


def test_outline_cutout_subtracts():
    area = analyze_layout_quality(_load_pcb(CUTOUT_PCB)).outline_area_mm2
    assert area == pytest.approx(100 * 50 - 10 * 10, abs=1e-6)


def test_nested_island_inside_cutout_adds_back():
    area = analyze_layout_quality(_load_pcb(NESTED_ISLAND_PCB)).outline_area_mm2
    assert area == pytest.approx(100 * 50 - 20 * 20 + 5 * 5, abs=1e-6)


def test_unmodelled_edge_cuts_shape_returns_none():
    # gr_circle on Edge.Cuts is missing from the segment accessor: refuse
    # the area instead of reporting the gross rect (docstring policy).
    assert analyze_layout_quality(_load_pcb(CIRCLE_CUTOUT_PCB)).outline_area_mm2 is None


def test_rerun_stability():
    pcb = _load_pcb(KNOWN_COUNTS_PCB)
    assert analyze_layout_quality(pcb).to_dict() == analyze_layout_quality(pcb).to_dict()


def test_ground_via_classification():
    data = analyze_layout_quality(_load_pcb(KNOWN_COUNTS_PCB)).to_dict()
    # The partition always totals the via count, netless vias included.
    assert data["via"]["total"] == data["via"]["track"] + data["via"]["ground"] == 3


# ---------------------------------------------------------------------------
# Fleet-router benchmark integration
# ---------------------------------------------------------------------------

BENCH_SCRIPT = REPO_ROOT / "benchmarks" / "external" / "fleet_routers" / "run.py"


def _load_bench():
    name = "fleet_routers_run_layout_quality_test"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, BENCH_SCRIPT)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


@pytest.fixture(scope="module")
def bench():
    return _load_bench()


def _write_pcb(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "bench.kicad_pcb"
    path.write_text(text)
    return path


def test_fingerprint_reports_layout_quality_and_is_stable(tmp_path, bench):
    path = _write_pcb(tmp_path, KNOWN_COUNTS_PCB)
    fp1 = bench.fingerprint(path)
    fp2 = bench.fingerprint(path)
    assert fp1["layout_quality"] == fp2["layout_quality"]
    assert fp1["layout_quality"]["corners"]["right_angle"] == 1
    assert fp1["layout_quality"]["via"] == {"track": 2, "ground": 1, "total": 3}


def test_compare_fingerprints_ignores_layout_quality(tmp_path, bench):
    path = _write_pcb(tmp_path, KNOWN_COUNTS_PCB)
    before = bench.fingerprint(path)
    after = bench.fingerprint(path)
    # Routing legitimately changes the quality numbers: a router that adds
    # copper is not a placement violation.
    after["layout_quality"]["corners"]["right_angle"] += 7
    after["layout_quality"]["total_track_length_mm"] += 123.456
    assert bench.compare_fingerprints(before, after) == []
    # The comparison still guards the invariants beside it.
    after["pad_nets"]["TP1:1"] = "Hijacked"
    assert bench.compare_fingerprints(before, after) != []
