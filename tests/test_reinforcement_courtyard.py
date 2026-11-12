"""Tests for the ``wire_run_courtyard_overlap`` DRC rule (Issue #8820)."""

from __future__ import annotations

from kicad_tools.manufacturers import DesignRules
from kicad_tools.pcb.reinforce import reinforce_net
from kicad_tools.router.current_paths import CurrentPathSpec, PathEndpoint
from kicad_tools.schema.pcb import PCB, Footprint, FootprintGraphic, Pad
from kicad_tools.validate.rules.reinforcement_courtyard import ReinforcementCourtyardRule
from tests.test_pcb_reinforce import _add_th_pad_footprint, _straight_run_pcb

NET = "FUSED_LINE"


def _rules() -> DesignRules:
    return DesignRules(
        min_trace_width_mm=0.127,
        min_clearance_mm=0.127,
        min_via_drill_mm=0.2,
        min_via_diameter_mm=0.45,
        min_annular_ring_mm=0.13,
    )


def _spec(eligible: bool = True) -> CurrentPathSpec:
    return CurrentPathSpec(
        name="FORCE",
        net_name=NET,
        source=PathEndpoint("J1", "1"),
        sink=PathEndpoint("J2", "1"),
        continuous_a=10.0,
        reinforcement_eligible=eligible,
    )


def _board() -> PCB:
    pcb = _straight_run_pcb(length=100.0)
    _add_th_pad_footprint(pcb, ref="J1", x=20.0, y=50.0, net=NET, drill=1.0, size=2.0)
    _add_th_pad_footprint(pcb, ref="J2", x=120.0, y=50.0, net=NET, drill=1.0, size=2.0)
    reinforce_net(pcb, NET)
    return pcb


def _body(
    pcb: PCB,
    ref: str,
    x: float,
    y: float,
    *,
    side: str = "F",
    half: float = 3.0,
    courtyard: bool = True,
    pads: list[Pad] | None = None,
) -> Footprint:
    suffix = "Cu"
    layer = f"{side}.{suffix}"
    gl = [
        FootprintGraphic(
            graphic_type="rect",
            layer=f"{side}.CrtYd" if courtyard else f"{side}.Fab",
            stroke_width=0.05,
            start=(-half, -half),
            end=(half, half),
        )
    ]
    fp = Footprint(
        name="X:Body",
        layer=layer,
        position=(x, y),
        rotation=0.0,
        reference=ref,
        value="V",
        pads=pads or [],
        graphics=gl,
    )
    pcb._footprints.append(fp)
    return fp


def _run(pcb: PCB, eligible: bool = True):
    return ReinforcementCourtyardRule(specs=[_spec(eligible)]).check(pcb, _rules())


def _msgs(results) -> list[str]:
    return [v.message for v in results]


def test_baseline_clean():
    assert not list(_run(_board()))


def test_run_under_same_side_courtyard_errors():
    pcb = _board()
    _body(pcb, "R1", 70.0, 50.0)
    res = _run(pcb)
    runs = [v for v in res if "[run]" in v.message]
    assert runs and all(v.severity == "error" for v in runs)
    assert "R1" in runs[0].message


def test_run_on_empty_side_is_clean():
    pcb = _board()
    _body(pcb, "R1", 70.0, 50.0, side="B")
    assert not [v for v in _run(pcb) if "[run]" in v.message]


def test_no_courtyard_falls_back_to_fab_bbox():
    pcb = _board()
    _body(pcb, "R1", 70.0, 50.0, courtyard=False)
    runs = [v for v in _run(pcb) if "[run]" in v.message]
    assert runs and "R1" in runs[0].message


def test_opposite_side_tht_foreign_pin_errors_own_pin_clean():
    def tht(net_name: str) -> Pad:
        pcb_net = pcb.add_net(net_name)
        return Pad(
            number="1",
            type="thru_hole",
            shape="circle",
            position=(0.0, 0.0),
            size=(2.0, 2.0),
            layers=["*.Cu"],
            net_number=pcb_net.number,
            net_name=net_name,
            drill=1.0,
        )

    pcb = _board()
    _body(pcb, "Q1", 70.0, 51.0, side="B", courtyard=True, pads=[tht("OTHER")])
    # THT part placed on the back; run is on F.Cu (opposite side): only pins count.
    pcb._footprints[-1].graphics = []
    assert [v for v in _run(pcb) if "[run]" in v.message and "Q1" in v.message]

    pcb = _board()
    _body(pcb, "Q1", 70.0, 51.0, side="B", courtyard=False, pads=[tht(NET)])
    pcb._footprints[-1].graphics = []
    assert not [v for v in _run(pcb) if "Q1" in v.message]


def test_end_hole_under_opposite_side_body_errors():
    pcb = _board()
    # Anchor holes sit near the run ends; put a back-side body on the first one.
    anchors = [tuple(v.position) for v in pcb.vias]
    assert anchors
    x, y = anchors[0]
    _body(pcb, "U9", x, y, side="B", half=0.5)
    res = [v for v in _run(pcb) if "[end hole]" in v.message and "U9" in v.message]
    assert res and "back" in res[0].message


def test_end_hole_clear_is_clean():
    assert not [v for v in _run(_board()) if "[end hole]" in v.message]


def test_no_eligible_path_is_noop():
    pcb = _board()
    _body(pcb, "R1", 70.0, 50.0)
    assert not list(_run(pcb, eligible=False))


def test_registered():
    from kicad_tools.cli.check_categories import CHECK_CATEGORIES
    from kicad_tools.validate.checker import DRCChecker
    from kicad_tools.validate.coverage import BLOCKING_CATEGORIES

    assert "wire_run_courtyard_overlap" in CHECK_CATEGORIES
    assert "wire_run_courtyard_overlap" in BLOCKING_CATEGORIES
    assert "check_wire_run_courtyard_overlap" in DRCChecker.CHECK_ALL_METHODS


# --- reinforce consults the same overlap geometry (Issue #8821) -------------


def _bare_board() -> PCB:
    pcb = _straight_run_pcb(length=100.0)
    _add_th_pad_footprint(pcb, ref="J1", x=20.0, y=50.0, net=NET, drill=1.0, size=2.0)
    _add_th_pad_footprint(pcb, ref="J2", x=120.0, y=50.0, net=NET, drill=1.0, size=2.0)
    return pcb


def test_reinforce_refuses_run_under_same_side_courtyard():
    pcb = _bare_board()
    _body(pcb, "R1", 70.0, 50.0)
    before = len(pcb.vias)
    res = reinforce_net(pcb, NET, design_rules=_rules())
    assert res.placed_count == 0
    assert len(pcb.vias) == before
    leg = res.legs[0]
    assert leg.status == "unanchored"
    assert "R1" in leg.reason and "courtyard" in leg.reason


def test_reinforce_dry_run_reports_footprint():
    pcb = _bare_board()
    _body(pcb, "R1", 70.0, 50.0)
    res = reinforce_net(pcb, NET, design_rules=_rules(), dry_run=True)
    assert "R1" in res.legs[0].reason


def test_reinforce_clean_when_courtyard_on_other_side():
    pcb = _bare_board()
    _body(pcb, "R1", 70.0, 50.0, side="B")
    res = reinforce_net(pcb, NET, design_rules=_rules())
    assert res.legs[0].status == "full"
    assert not [v for v in _run(pcb) if "[run]" in v.message]


def test_reinforce_prefers_unobstructed_side():
    pcb = _bare_board()
    # Longer copper on F.Cu (blocked) plus a shorter run on B.Cu (clear).
    for x0 in (30.0, 50.0):
        pcb.add_trace((x0, 80.0), (x0 + 10.0, 80.0), width=2.0, layer="B.Cu", net=NET)
    _body(pcb, "R1", 70.0, 50.0)
    res = reinforce_net(pcb, NET, design_rules=_rules(), all_runs=True)
    assert res.layer == "B.Cu"


def test_reinforce_both_sides_blocked_refused_with_reason():
    pcb = _bare_board()
    pcb.add_trace((30.0, 80.0), (60.0, 80.0), width=2.0, layer="B.Cu", net=NET)
    _body(pcb, "R1", 70.0, 50.0)
    _body(pcb, "R2", 45.0, 80.0, side="B")
    res = reinforce_net(pcb, NET, design_rules=_rules(), all_runs=True, layer="B.Cu")
    assert res.placed_count == 0
    assert any("R2" in leg.reason for leg in res.legs if leg.layer == "B.Cu")
