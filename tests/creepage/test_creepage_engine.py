"""Engine unit tests for the HV creepage/clearance audit (Issue #4327).

Covers:

* the pure surface-path geometry (``surface_path_length``): creepage ==
  clearance with no intervening slot; creepage > clearance (by the detour
  length) when a slot lies on the straight path; a mounting-hole cutout that
  is NOT between the pair does not lengthen the path; multiple slots in
  series.
* the board-level census (``compute_creepage_census``): a slot between the HV
  net and a neighbor yields creepage > clearance; the same board without the
  slot yields creepage == clearance; a board with no HV nets yields an empty
  census.
* HV net selection via a net-class map with a name-pattern fallback.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools._shapely import has_shapely
from kicad_tools.creepage.engine import (
    BOARD_EDGE_LABEL,
    compute_creepage_census,
    resolve_hv_nets,
    surface_path_length,
)

from .fixtures import board_no_hv_source, board_source

pytestmark = pytest.mark.skipif(not has_shapely(), reason="creepage requires shapely")


# ---------------------------------------------------------------------------
# Pure geometry: surface_path_length
# ---------------------------------------------------------------------------


def _box(x0, y0, x1, y1):
    from shapely.geometry import box

    return box(x0, y0, x1, y1)


def test_no_obstacle_creepage_equals_clearance():
    a = _box(0.0, -0.5, 1.0, 0.5)
    b = _box(9.0, -0.5, 10.0, 0.5)
    clearance, creepage = surface_path_length(a, b, obstacles=[])
    assert clearance == pytest.approx(8.0, abs=1e-6)
    assert creepage == pytest.approx(clearance, abs=1e-9)


def test_slot_on_path_lengthens_creepage():
    # Thin copper centered on y=0 so nearest points are (0,0) and (10,0).
    a = _box(-1.0, -1e-3, 0.0, 1e-3)
    b = _box(10.0, -1e-3, 11.0, 1e-3)
    slot = _box(4.9, -3.0, 5.1, 3.0)  # tall bar straddling the straight path

    clearance, creepage = surface_path_length(a, b, obstacles=[slot])
    assert clearance == pytest.approx(10.0, abs=1e-3)
    # Detour: (0,0)->(4.9,3)->(5.1,3)->(10,0) (or the symmetric bottom route).
    expected = 2 * math.hypot(4.9, 3.0) + 0.2
    assert creepage > clearance
    assert creepage == pytest.approx(expected, abs=0.05)


def test_offset_cutout_not_between_pair_does_not_lengthen():
    # A mounting-hole cutout well above the straight path must not detour it.
    a = _box(0.0, -0.5, 1.0, 0.5)
    b = _box(9.0, -0.5, 10.0, 0.5)
    hole = _box(4.5, 5.0, 5.5, 6.0)  # entirely off the y~0 straight line

    clearance, creepage = surface_path_length(a, b, obstacles=[hole])
    assert creepage == pytest.approx(clearance, abs=1e-9)


def test_multiple_slots_in_series_sum_detours():
    a = _box(-1.0, -1e-3, 0.0, 1e-3)
    b = _box(20.0, -1e-3, 21.0, 1e-3)
    slot1 = _box(4.9, -3.0, 5.1, 3.0)
    slot2 = _box(14.9, -3.0, 15.1, 3.0)

    clearance, creepage = surface_path_length(a, b, obstacles=[slot1, slot2])
    assert clearance == pytest.approx(20.0, abs=1e-3)
    # Two slots each force a detour up to y=3 and back -- creepage exceeds the
    # single-slot detour and, of course, the straight clearance.
    assert creepage > clearance + 1.0


def test_overlapping_geometries_have_zero_creepage():
    a = _box(0.0, 0.0, 5.0, 5.0)
    b = _box(4.0, 0.0, 9.0, 5.0)  # overlaps a
    clearance, creepage = surface_path_length(a, b, obstacles=[_box(2.0, 2.0, 3.0, 3.0)])
    assert clearance <= 0.0
    assert creepage == clearance


# ---------------------------------------------------------------------------
# Board-level census
# ---------------------------------------------------------------------------


def _load(tmp_path, source, name="board.kicad_pcb"):
    from kicad_tools.schema.pcb import PCB

    p = tmp_path / name
    p.write_text(source)
    return PCB.load(p)


def _hv_map():
    from kicad_tools.router.rules import net_class_map_from_dict

    return net_class_map_from_dict({"L_MAINS": {"name": "HV"}})


def _conductor_pair(report, net_b):
    for pair in report.pairs:
        if pair.kind == "conductor" and pair.net_b == net_b:
            return pair
    raise AssertionError(f"no conductor pair against {net_b!r} in census")


def test_census_no_slot_creepage_equals_clearance(tmp_path):
    pcb = _load(tmp_path, board_source(with_slot=False))
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    assert set(hv.values()) == {"L_MAINS"}

    report = compute_creepage_census(pcb, hv, min_mm=1.5)
    pair = _conductor_pair(report, "GND")
    assert pair.clearance_mm == pytest.approx(18.0, abs=1e-3)
    assert pair.creepage_mm == pytest.approx(pair.clearance_mm, abs=1e-6)


def test_census_slot_lengthens_creepage(tmp_path):
    pcb = _load(tmp_path, board_source(with_slot=True))
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = compute_creepage_census(pcb, hv, min_mm=1.5)

    pair = _conductor_pair(report, "GND")
    assert pair.clearance_mm == pytest.approx(18.0, abs=1e-3)
    # The milled slot forces the surface path to detour around it.
    assert pair.creepage_mm > pair.clearance_mm + 2.0
    # Clearance and creepage are genuinely distinct values.
    assert pair.creepage_mm != pytest.approx(pair.clearance_mm)


def test_census_reports_board_edge_pair(tmp_path):
    pcb = _load(tmp_path, board_source(with_slot=False))
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = compute_creepage_census(pcb, hv, min_mm=1.5)

    edge_pairs = [p for p in report.pairs if p.kind == "edge"]
    assert len(edge_pairs) == 1
    assert edge_pairs[0].net_b == BOARD_EDGE_LABEL
    assert edge_pairs[0].net_a == "L_MAINS"
    # L_MAINS pad edge at x=111 vs board edge at x=100 -> 11 mm to the wall.
    assert edge_pairs[0].clearance_mm == pytest.approx(9.0, abs=1.5)


def test_pass_flag_and_margin(tmp_path):
    pcb = _load(tmp_path, board_source(with_slot=False))
    hv = resolve_hv_nets(pcb, "HV", _hv_map())

    report = compute_creepage_census(pcb, hv, min_mm=1.5)
    assert report.passed  # all gaps are >> 1.5 mm
    pair = _conductor_pair(report, "GND")
    assert pair.margin_mm == pytest.approx(pair.creepage_mm - 1.5, abs=1e-6)

    strict = compute_creepage_census(pcb, hv, min_mm=100.0)
    assert not strict.passed  # 18 mm < 100 mm required
    assert not _conductor_pair(strict, "GND").passed


def test_no_hv_nets_yields_empty_census(tmp_path):
    pcb = _load(tmp_path, board_no_hv_source())
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    assert hv == {}

    report = compute_creepage_census(pcb, hv, min_mm=1.5)
    assert report.pairs == []
    assert report.hv_nets == []
    assert report.passed  # vacuously true -> exit 0


def test_name_pattern_fallback_without_map(tmp_path):
    # No map: 'GND' matches the built-in GROUND name pattern, so --net-class
    # ground selects it via classify_from_name (no new classifier).
    pcb = _load(tmp_path, board_source(with_slot=False))
    hv = resolve_hv_nets(pcb, "ground", net_class_map=None)
    assert set(hv.values()) == {"GND"}
