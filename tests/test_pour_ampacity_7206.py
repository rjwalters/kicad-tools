"""Copper pours are rated by their narrowest neck (Issue #7206, follow-up F3).

A net whose declared current flows through a pour used to read
``ampacity: unknown (zone-only, not measured)``.  The pour is now rated:
its own copper, eroded by half the IPC-2221 required width, must still join
every pad of the net.  A pour that cannot be rated stays ``unknown``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.cli.check_cmd import main as check_main
from kicad_tools.manufacturers import DesignRules
from kicad_tools.physics.ampacity import width_for_current
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate import coverage as cov
from kicad_tools.validate.pour_ampacity import measure_pour_ampacity
from kicad_tools.validate.rules.ampacity import AmpacityRule

REPO = Path(__file__).resolve().parents[1]
CURRENT_A = 3.0  # ~1.36 mm on 1 oz outer copper (IPC-2221, 10 C)


def _pad(ref: str, x: float, tht: bool = False) -> str:
    kind = 'thru_hole rect (drill 1) (layers "*.Cu")' if tht else 'smd rect (layers "F.Cu")'
    return f"""
  (footprint "R_2512" (layer "F.Cu") (at {x} 15)
    (property "Reference" "{ref}")
    (pad "1" {kind} (at 0 0) (size 2 2) (net 1 "+5V")))"""


def _zone(fill, layer: str = "F.Cu") -> str:
    """``fill``: one outline, a list of outlines, or ``None`` (unfilled)."""
    outlines = [] if fill is None else (fill if isinstance(fill[0], list) else [fill])
    filled = "".join(
        f'(filled_polygon (layer "{layer}") (pts '
        + " ".join(f"(xy {x} {y})" for x, y in pts)
        + "))"
        for pts in outlines
    )
    return f"""
  (zone (net 1) (net_name "+5V") (layer "{layer}")
    (uuid "00000000-0000-0000-0000-000000000001")
    (hatch edge 0.5) (connect_pads yes (clearance 0.2)) (min_thickness 0.15)
    (filled_areas_thickness no)
    (fill yes (thermal_gap 0.2) (thermal_bridge_width 0.5))
    (polygon (pts (xy 8 8) (xy 32 8) (xy 32 22) (xy 8 22)))
    {filled})"""


def dumbbell(neck_mm: float) -> list[tuple[float, float]]:
    """Two 6x6 mm lobes around the pads joined by a neck ``neck_mm`` wide."""
    h = neck_mm / 2
    return [
        (9, 12), (15, 12), (15, 15 - h), (25, 15 - h), (25, 12), (31, 12),
        (31, 18), (25, 18), (25, 15 + h), (15, 15 + h), (15, 18), (9, 18),
    ]  # fmt: skip


def board(tmp_path: Path, fill, *, pads=("12", "28"), extra: str = "", layer: str = "F.Cu") -> Path:
    tht = layer != "F.Cu"
    body = "".join(_pad(f"R{i}", float(x), tht) for i, x in enumerate(pads, 1))
    inner = '(1 "In1.Cu" signal) (2 "In2.Cu" signal)' if tht else ""
    text = f"""(kicad_pcb (version 20240108) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) {inner} (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "") (net 1 "+5V")
  {body}
  {extra}
  {_zone(fill, layer)}
)
"""
    path = tmp_path / "board.kicad_pcb"
    path.write_text(text)
    return path


def measure(path: Path, amps: float = CURRENT_A):
    return measure_pour_ampacity(PCB.load(str(path)), {"+5V": amps}, _rules())


def _rules(**overrides: float) -> DesignRules:
    return DesignRules(
        min_trace_width_mm=0.1,
        min_clearance_mm=0.1,
        min_via_drill_mm=0.2,
        min_via_diameter_mm=0.4,
        min_annular_ring_mm=0.1,
        **overrides,
    )


REQUIRED = width_for_current(CURRENT_A, copper_weight_oz=1.0, layer="external")


class TestPourMeasurement:
    def test_necked_pour_is_an_error(self, tmp_path):
        result = measure(board(tmp_path, dumbbell(0.5)))
        assert result.unmeasured == {}
        assert len(result.violations) == 1
        v = result.violations[0]
        assert v.rule_id == "ampacity" and v.severity == "error"
        assert v.items == ("+5V",)
        assert v.actual_value == pytest.approx(0.5, abs=0.01)
        assert v.required_value == pytest.approx(REQUIRED)
        assert "Pour on F.Cu too narrow for 3.0A" in v.message

    def test_wide_pour_passes_and_reports_its_neck(self, tmp_path):
        result = measure(board(tmp_path, dumbbell(3.0)))
        assert result.violations == [] and result.unmeasured == {}
        assert result.necks_mm["+5V"] == pytest.approx(3.0, abs=0.01)

    def test_neck_exactly_at_the_floor_passes(self, tmp_path):
        result = measure(board(tmp_path, dumbbell(REQUIRED)))
        assert result.violations == []

    def test_neck_just_under_the_floor_fails(self, tmp_path):
        result = measure(board(tmp_path, dumbbell(REQUIRED - 0.02)))
        assert len(result.violations) == 1

    def test_parallel_necks_are_not_summed(self, tmp_path):
        # Two 1.0 mm necks in parallel (2.0 mm in total) still fail 1.36 mm.
        def rect(x0, y0, x1, y1):
            return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]

        fill = [
            rect(9, 12, 15, 18),
            rect(25, 12, 31, 18),
            rect(14, 12, 26, 13),
            rect(14, 17, 26, 18),
        ]
        result = measure(board(tmp_path, fill))
        assert len(result.violations) == 1
        assert result.violations[0].actual_value == pytest.approx(1.0, abs=0.01)


class TestFailClosed:
    def test_zone_without_fill_is_unknown(self, tmp_path):
        result = measure(board(tmp_path, None))
        assert result.violations == []
        assert result.unmeasured == {"+5V": "zone fill absent"}

    def test_single_pad_is_unknown(self, tmp_path):
        result = measure(board(tmp_path, dumbbell(3.0), pads=("12",)))
        assert "no current path" in result.unmeasured["+5V"]

    def test_pad_not_reached_by_pour_is_unknown(self, tmp_path):
        # The right lobe stops short of R2: the copper never joins it.
        fill = [(9, 12), (20, 12), (20, 18), (9, 18)]
        result = measure(board(tmp_path, fill))
        assert result.violations == []
        assert "pour copper does not join pad(s)" in result.unmeasured["+5V"]

    def test_no_inner_copper_weight_is_unknown(self, tmp_path):
        pcb = PCB.load(str(board(tmp_path, dumbbell(3.0), layer="In1.Cu")))
        result = measure_pour_ampacity(pcb, {"+5V": CURRENT_A}, _rules(inner_copper_oz=0.0))
        assert result.unmeasured == {"+5V": "no copper weight for In1.Cu"}

    def test_inner_pour_uses_inner_copper(self, tmp_path):
        pcb = PCB.load(str(board(tmp_path, dumbbell(3.0), layer="In1.Cu")))
        result = measure_pour_ampacity(pcb, {"+5V": CURRENT_A}, _rules(inner_copper_oz=0.5))
        # 3 A on 0.5 oz internal copper needs far more than 3 mm.
        required = width_for_current(CURRENT_A, copper_weight_oz=0.5, layer="internal")
        assert required > 3.0
        assert len(result.violations) == 1
        assert result.violations[0].required_value == pytest.approx(required)

    def test_track_bridges_count_and_are_rated_by_the_track_rule(self, tmp_path):
        # Fill reaches only R1; a 2 mm track carries the current to R2.
        fill = [(9, 12), (20, 12), (20, 18), (9, 18)]
        track = '(segment (start 19 15) (end 28 15) (width 2.0) (layer "F.Cu") (net 1))'
        result = measure(board(tmp_path, fill, extra=track))
        assert result.unmeasured == {} and result.violations == []


class TestCoverage:
    def test_rule_records_unmeasured_nets(self, tmp_path):
        rule = AmpacityRule(specs={"+5V": CURRENT_A})
        rule.check(PCB.load(str(board(tmp_path, None))), _rules())
        assert rule.unmeasured_pour_nets == {"+5V": "zone fill absent"}

    def test_unknown_coverage_is_blocking(self):
        result = cov.ampacity_pour_unknown({"+5V": "zone fill absent"})
        assert result.state == cov.UNKNOWN and result.blocking
        assert "pour not measurable (+5V: zone fill absent)" in result.status

    @pytest.mark.parametrize(
        ("fill", "state", "errors"),
        [(dumbbell(3.0), "checked", 0), (dumbbell(0.5), "checked", 1), (None, "unknown", 0)],
        ids=["wide", "necked", "unfilled"],
    )
    def test_kct_check_coverage(self, tmp_path, capsys, fill, state, errors):
        pcb = board(tmp_path, fill)
        ncm = tmp_path / "net_class_map.json"
        ncm.write_text(json.dumps({"+5V": {"name": "Power", "target_ampacity": CURRENT_A}}))
        check_main(
            [str(pcb), "--mfr", "jlcpcb", "--format", "json", "--only", "ampacity"]
            + ["--net-class-map", str(ncm)]
        )
        captured = capsys.readouterr()
        out = captured.out
        data = json.loads(out[out.index("{") :])
        # The pour is rated, so the net is not "declared but not evaluated".
        assert "declared but not evaluated" not in captured.err
        assert data["coverage"]["ampacity"]["state"] == state
        found = [v for v in data["violations"] if v["rule_id"] == "ampacity"]
        assert len([v for v in found if v["severity"] == "error"]) == errors


def test_board04_pours_are_measured_not_unknown():
    out = REPO / "boards" / "04-stm32-devboard" / "output"
    ncm = json.loads((out / "net_class_map.json").read_text())
    specs = {net: entry["target_ampacity"] for net, entry in ncm.items()}
    pcb = PCB.load(str(out / "stm32_devboard_routed.kicad_pcb"))
    result = measure_pour_ampacity(pcb, specs, _rules())
    assert result.unmeasured == {}
    assert result.violations == []
    assert set(result.necks_mm) == {"+5V", "+3.3V"}
