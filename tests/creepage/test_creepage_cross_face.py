"""Cross-face surface paths through NPTH bores, slots and the board edge (#9704).

The census historically paired conductors per copper layer, so a surface path
that crosses from one board face to the other -- down a non-plated bore, along
a milled slot wall, or around the board outline -- was never measured.  Each
path is ``d(conductor on face A, rim) + board thickness + d(rim, conductor on
face B)`` and is folded into the pair's creepage as a minimum, so results can
only get stricter, never looser.
"""

from __future__ import annotations

import pytest

from kicad_tools._shapely import has_shapely
from kicad_tools.creepage.engine import (
    compute_creepage_census,
    resolve_hv_nets,
    voltage_map_from_dict,
)
from kicad_tools.creepage.standards import DEFAULT_INSULATION, get_standard

from .fixtures import (
    board_cross_face_bore_source,
    board_cross_face_edge_wrap_source,
    board_cross_face_slot_source,
    board_cross_face_two_bore_both_faces_source,
    board_same_face_with_bore_source,
    board_source,
)

pytestmark = pytest.mark.skipif(not has_shapely(), reason="creepage requires shapely")


def _load(tmp_path, source):
    from kicad_tools.schema.pcb import PCB

    p = tmp_path / "board.kicad_pcb"
    p.write_text(source)
    return PCB.load(p)


def _hv_map():
    from kicad_tools.router.rules import net_class_map_from_dict

    return net_class_map_from_dict({"L_MAINS": {"name": "HV"}})


def _census(pcb, hv, **kw):
    return compute_creepage_census(pcb, hv, min_mm=6.3, board="board.kicad_pcb", **kw)


def _conductor_pair(report, net_b):
    for pair in report.pairs:
        if pair.kind == "conductor" and pair.net_b == net_b:
            return pair
    raise AssertionError(f"no conductor pair against {net_b!r} in census")


def _census_map(pcb, hv, vmap, *, insulation=DEFAULT_INSULATION):
    std = get_standard("iec60664")
    parsed, _edge = voltage_map_from_dict(vmap)
    return compute_creepage_census(
        pcb,
        hv,
        None,
        standard="iec60664",
        standard_edition=std.edition,
        pollution_degree=2,
        material_group="IIIa",
        voltage_map=parsed,
        standard_obj=std,
        insulation=insulation,
    )


# ---------------------------------------------------------------------------
# The Evidence geometry (issue #9704): bore path ~3.75 mm vs a 6.3 mm bound
# ---------------------------------------------------------------------------


def test_cross_face_bore_path_reported_failing(tmp_path):
    pcb = _load(tmp_path, board_cross_face_bore_source())
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = _census(pcb, hv)

    pair = _conductor_pair(report, "GND")
    # 1.4 (B.Cu run -> bore rim) + 1.6 (board thickness) + 0.75 (rim -> F.Cu
    # track) = 3.75 mm, well under the 6.3 mm requirement.
    assert pair.creepage_mm == pytest.approx(3.75, abs=0.01)
    assert not pair.passed
    # The binding row names the bore (as the island hop names its island).
    assert pair.via_bore == "MH1.1"
    assert pair.to_dict()["via_bore"] == "MH1.1"
    # Cross-face-only pair: the clearance is the in-plan projection (0 here,
    # the F.Cu track crosses the B.Cu run in plan), never above the creepage.
    assert pair.clearance_mm <= pair.creepage_mm
    assert pair.witness is not None


def test_cross_face_bore_path_map_mode_600v_basic(tmp_path):
    # The Evidence voltage model: 600 V vs 0 V, basic insulation -> 6.3 mm.
    pcb = _load(tmp_path, board_cross_face_bore_source())
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = _census_map(pcb, hv, {"L_MAINS": 600.0}, insulation="basic")

    pair = _conductor_pair(report, "GND")
    assert pair.required_creepage_mm == pytest.approx(6.3)
    assert pair.creepage_mm == pytest.approx(3.75, abs=0.01)
    assert not pair.passed


def test_far_bore_creates_no_cross_face_row(tmp_path):
    # Negative: bore far from either conductor -> the bore path is huge, and a
    # passing cross-face pair stays absent exactly as before #9704.
    pcb = _load(tmp_path, board_cross_face_bore_source(hole_x=135.0, hole_y=115.0))
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = _census(pcb, hv)

    assert not any(p.kind == "conductor" and p.net_b == "GND" for p in report.pairs)
    # The board-edge row (clearance 1.0 mm) still passes a 1.0 mm bound.
    report10 = compute_creepage_census(pcb, hv, min_mm=1.0, board="board.kicad_pcb")
    assert report10.passed


def test_cross_face_bore_binds_below_same_face_row(tmp_path):
    # The pair also shares F.Cu (a far-away L_MAINS stub, ~25 mm same-face
    # gap); the 3.75 mm bore path must bind by min(), not replace the row.
    pcb = _load(tmp_path, board_cross_face_bore_source(with_hv_fcu_stub=True))
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = _census(pcb, hv)

    pair = _conductor_pair(report, "GND")
    assert pair.creepage_mm == pytest.approx(3.75, abs=0.01)
    assert pair.via_bore == "MH1.1"
    assert not pair.passed
    # Same-face results unchanged: the binding same-face layer is still F.Cu
    # and the clearance is still the same-face straight-line gap.
    assert pair.layer == "F.Cu"


# ---------------------------------------------------------------------------
# Board outline wrap and milled-slot wall paths
# ---------------------------------------------------------------------------


def test_cross_face_board_edge_wrap(tmp_path):
    pcb = _load(tmp_path, board_cross_face_edge_wrap_source())
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = _census(pcb, hv)

    pair = _conductor_pair(report, "GND")
    # 1.5 (F.Cu -> left wall) + 1.6 (thickness) + 1.5 (wall -> B.Cu) = 4.6 mm.
    assert pair.creepage_mm == pytest.approx(4.6, abs=0.01)
    assert pair.via_bore == "Edge.Cuts"
    assert not pair.passed


def test_cross_face_slot_wall_path(tmp_path):
    pcb = _load(tmp_path, board_cross_face_slot_source())
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = _census(pcb, hv)

    pair = _conductor_pair(report, "GND")
    # The slot wall is crossed at ONE rim point: 1.0 (F.Cu pad -> near wall)
    # + 1.6 (thickness) + 3.0 (wall -> B.Cu pad across the 2 mm slot).
    assert pair.creepage_mm == pytest.approx(5.6, abs=0.05)
    assert pair.via_bore is not None and "slot" in pair.via_bore
    assert not pair.passed


# ---------------------------------------------------------------------------
# AC3: same-face results do not change
# ---------------------------------------------------------------------------


def test_same_face_row_unchanged_by_off_path_bore(tmp_path):
    baseline = _load(tmp_path, board_source(with_slot=False))
    bored = _load(tmp_path, board_same_face_with_bore_source())
    hv_a = resolve_hv_nets(baseline, "HV", _hv_map())
    hv_b = resolve_hv_nets(bored, "HV", _hv_map())

    ra = compute_creepage_census(baseline, hv_a, min_mm=1.5)
    rb = compute_creepage_census(bored, hv_b, min_mm=1.5)
    pa, pb = _conductor_pair(ra, "GND"), _conductor_pair(rb, "GND")
    assert pb.clearance_mm == pytest.approx(pa.clearance_mm, abs=1e-6)
    assert pb.creepage_mm == pytest.approx(pa.creepage_mm, abs=1e-6)
    assert pb.layer == pa.layer == "F.Cu"
    assert pb.passed == pa.passed


def test_same_face_pair_with_bore_between_unchanged(tmp_path):
    # #8548's fixture: both pads on F.Cu with an NPTH (bare and annular)
    # between them.  Same-face results must not change: the bore only ever
    # joins two DIFFERENT faces.  The annular variant binds through the
    # floating ring (the pre-existing #8548 island hop), not through the bore.
    from .fixtures import board_net0_npth_source

    pcb = _load(tmp_path, board_net0_npth_source(copper_ring=False))
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = compute_creepage_census(pcb, hv, min_mm=1.0)
    pair = _conductor_pair(report, "GND")
    # Straight-line 6 mm edge-to-edge gap, no same-face detour, no bore hop.
    assert pair.clearance_mm == pytest.approx(6.0, abs=1e-3)
    assert pair.creepage_mm == pytest.approx(6.0, abs=1e-3)
    assert pair.via_bore is None

    ringed = _load(tmp_path, board_net0_npth_source(copper_ring=True))
    hv_r = resolve_hv_nets(ringed, "HV", _hv_map())
    report_r = compute_creepage_census(ringed, hv_r, min_mm=1.0)
    pair_r = _conductor_pair(report_r, "GND")
    assert pair_r.creepage_mm == pytest.approx(1.6, abs=1e-3)  # #8548 island hop
    assert pair_r.via_floating is not None
    assert pair_r.via_bore is None


# ---------------------------------------------------------------------------
# Board thickness source
# ---------------------------------------------------------------------------


def test_thickness_defaults_to_1_6_without_stackup(tmp_path):
    pcb = _load(tmp_path, board_cross_face_bore_source())
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = _census(pcb, hv)

    assert report.board_thickness_mm == pytest.approx(1.6)
    assert report.board_thickness_source == "default"
    d = report.to_dict()
    assert d["board_thickness_mm"] == pytest.approx(1.6)
    assert d["board_thickness_source"] == "default"


def test_thickness_from_stackup_when_present(tmp_path):
    stackup = (
        "(setup (pad_to_mask_clearance 0)\n"
        "  (stackup\n"
        '    (layer "F.Cu" (type "copper") (thickness 0.035))\n'
        '    (layer "dielectric 1" (type "core") (thickness 1.5))\n'
        '    (layer "B.Cu" (type "copper") (thickness 0.035))\n'
        "  )\n"
        ")\n"
    )
    source = board_cross_face_bore_source().replace("(setup (pad_to_mask_clearance 0))\n", stackup)
    pcb = _load(tmp_path, source)
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = _census(pcb, hv)

    assert report.board_thickness_mm == pytest.approx(1.57)
    assert report.board_thickness_source == "stackup"
    # 1.4 + 1.57 + 0.75 = 3.72 mm through the bore.
    pair = _conductor_pair(report, "GND")
    assert pair.creepage_mm == pytest.approx(3.72, abs=0.01)


@pytest.mark.parametrize("short_first", [True, False])
def test_both_faces_two_bores_shorter_binds_regardless_of_order(tmp_path, short_first):
    # Both nets carry copper on both faces; the 2.6 mm bore must bind whether
    # the face ordering that finds it runs first or last (it used to be
    # overwritten by the later ordering's 5.0 mm path).
    pcb = _load(tmp_path, board_cross_face_two_bore_both_faces_source(short_first=short_first))
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = _census(pcb, hv)

    pair = _conductor_pair(report, "GND")
    assert pair.creepage_mm == pytest.approx(2.6, abs=0.01)
    assert pair.via_bore == "MHA.1"
    assert not pair.passed

    # A bound between the two paths must still FAIL (no false pass on 5.0 mm).
    report3 = compute_creepage_census(pcb, hv, min_mm=3.0, board="board.kicad_pcb")
    assert not report3.passed
