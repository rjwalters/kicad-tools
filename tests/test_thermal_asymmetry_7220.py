"""Tests for the ``thermal_asymmetry`` rule (issue #7220).

Unit fixtures cover the passive finding (one terminal solid to its own-layer
fill, the other on a trace or thermal relief), the solid multi-plane PTH
finding and its exposed-pad exemption.  The fleet census pins the per-board
counts on the committed ``boards/*/output/*_routed.kicad_pcb`` files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.validate import DRCChecker
from kicad_tools.validate.rules.thermal_asymmetry import (
    NONE,
    SOLID,
    THERMAL,
    THERMAL_ASYMMETRY_RULE_ID,
    ThermalAsymmetryRule,
    asymmetric_passives,
    effective_mode,
)

REPO = Path(__file__).resolve().parents[1]

_LAYERS_2 = '(0 "F.Cu" signal) (31 "B.Cu" signal)'
_LAYERS_4 = '(0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal)'


def _board(body: str, *, layers: str = _LAYERS_2) -> str:
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers {layers} (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG")
  (net 3 "VCC")
{body}
)
"""


def _zone(
    net: int, name: str, layer: str, box: tuple[float, float, float, float], *, connect: str = ""
) -> str:
    x0, y0, x1, y1 = box
    pts = f"(xy {x0} {y0}) (xy {x1} {y0}) (xy {x1} {y1}) (xy {x0} {y1})"
    return f"""  (zone (net {net}) (net_name "{name}") (layer "{layer}") (uuid "z-{name}-{layer}")
    (connect_pads {connect}(clearance 0.2)) (min_thickness 0.2) (filled_areas_thickness no)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts {pts}))
    (filled_polygon (layer "{layer}") (pts {pts})))
"""


def _resistor(ref: str, *, pad1: str = "", pad2: str = "", net1=(1, "GND"), net2=(2, "SIG")) -> str:
    """0805 at (10, 10): pad 1 spans x 8.55..9.55, pad 2 spans 10.45..11.45."""
    return f"""  (footprint "Resistor_SMD:R_0805_2012Metric" (layer "F.Cu") (at 10 10)
    (property "Reference" "{ref}" (at 0 -2) (layer "F.SilkS"))
    (pad "1" smd roundrect (at -0.95 0) (size 1.0 1.25) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net {net1[0]} "{net1[1]}") {pad1})
    (pad "2" smd roundrect (at 0.95 0) (size 1.0 1.25) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net {net2[0]} "{net2[1]}") {pad2})
  )
"""


def _load(tmp_path: Path, text: str) -> PCB:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(text)
    return PCB.load(str(path))


def _findings(pcb: PCB):
    return ThermalAsymmetryRule().check(pcb, None).violations  # type: ignore[arg-type]


# Left fill reaches pad 1 only; right fill reaches pad 2 only.
_LEFT = (0.0, 0.0, 9.8, 20.0)
_RIGHT = (10.2, 0.0, 20.0, 20.0)


class TestEffectiveMode:
    @pytest.mark.parametrize(
        ("override", "zone", "pad_type", "expected"),
        [
            (None, "thermal_reliefs", "smd", THERMAL),
            (None, "solid", "smd", SOLID),
            (None, "none", "smd", NONE),
            (2, "thermal_reliefs", "smd", SOLID),
            (1, "solid", "smd", THERMAL),
            (0, "solid", "smd", NONE),
            (3, "solid", "smd", SOLID),
            (3, "solid", "thru_hole", THERMAL),
        ],
    )
    def test_pad_override_then_zone(self, override, zone, pad_type, expected):
        class _Pad:
            zone_connect = override
            type = pad_type

        assert effective_mode(_Pad(), zone) == expected  # type: ignore[arg-type]

    def test_pad_zone_connect_is_parsed(self, tmp_path):
        pcb = _load(tmp_path, _board(_resistor("R1", pad1="(zone_connect 2)")))
        pads = {p.number: p for p in pcb.footprints[0].pads}
        assert pads["1"].zone_connect == 2
        assert pads["2"].zone_connect is None


class TestPassiveFinding:
    def test_solid_terminal_vs_trace_terminal_fires(self, tmp_path):
        text = _board(_resistor("R1", pad1="(zone_connect 2)") + _zone(1, "GND", "F.Cu", _LEFT))
        found = _findings(_load(tmp_path, text))
        assert len(found) == 1
        v = found[0]
        assert v.rule_id == THERMAL_ASYMMETRY_RULE_ID
        assert v.severity == "warning"
        assert v.items == ("R1", "R1-1", "R1-2")
        assert v.layer == "F.Cu"
        assert "solid to 'GND' fill on F.Cu" in v.message
        assert "trace only" in v.message

    def test_solid_by_zone_setting_fires(self, tmp_path):
        text = _board(_resistor("C3") + _zone(1, "GND", "F.Cu", _LEFT, connect="yes "))
        assert [v.items[1] for v in _findings(_load(tmp_path, text))] == ["C3-1"]

    def test_solid_vs_thermal_fires(self, tmp_path):
        text = _board(
            _resistor("L1", pad1="(zone_connect 2)", net2=(3, "VCC"))
            + _zone(1, "GND", "F.Cu", _LEFT)
            + _zone(3, "VCC", "F.Cu", _RIGHT)
        )
        found = _findings(_load(tmp_path, text))
        assert len(found) == 1
        assert "thermal to 'VCC' fill on F.Cu" in found[0].message

    def test_both_terminals_thermal_does_not_fire(self, tmp_path):
        text = _board(
            _resistor("R1", net2=(3, "VCC"))
            + _zone(1, "GND", "F.Cu", _LEFT)
            + _zone(3, "VCC", "F.Cu", _RIGHT)
        )
        assert _findings(_load(tmp_path, text)) == []

    def test_both_terminals_solid_does_not_fire(self, tmp_path):
        text = _board(
            _resistor("R1", pad1="(zone_connect 2)", pad2="(zone_connect 2)", net2=(3, "VCC"))
            + _zone(1, "GND", "F.Cu", _LEFT)
            + _zone(3, "VCC", "F.Cu", _RIGHT)
        )
        assert _findings(_load(tmp_path, text)) == []

    def test_same_net_plane_on_inner_layer_only_does_not_fire(self, tmp_path):
        """The board-05 R9 false-positive shape: a +5V In2.Cu plane an F.Cu pad never touches."""
        text = _board(
            _resistor("R9", pad1="(zone_connect 2)") + _zone(1, "GND", "In2.Cu", (0, 0, 20, 20)),
            layers=_LAYERS_4,
        )
        assert _findings(_load(tmp_path, text)) == []

    def test_fill_that_does_not_reach_the_pad_does_not_fire(self, tmp_path):
        text = _board(
            _resistor("R1", pad1="(zone_connect 2)") + _zone(1, "GND", "F.Cu", (0, 0, 5, 5))
        )
        assert _findings(_load(tmp_path, text)) == []

    def test_pad_override_none_does_not_count_as_solid(self, tmp_path):
        text = _board(
            _resistor("R1", pad1="(zone_connect 0)")
            + _zone(1, "GND", "F.Cu", _LEFT, connect="yes ")
        )
        assert _findings(_load(tmp_path, text)) == []

    def test_non_passive_reference_is_ignored(self, tmp_path):
        text = _board(_resistor("U1", pad1="(zone_connect 2)") + _zone(1, "GND", "F.Cu", _LEFT))
        assert _findings(_load(tmp_path, text)) == []

    def test_unfilled_board_reports_nothing(self, tmp_path):
        assert _findings(_load(tmp_path, _board(_resistor("R1", pad1="(zone_connect 2)")))) == []

    def test_asymmetric_passives_reports_solid_and_other_pad(self, tmp_path):
        text = _board(
            _resistor("R1", pad2="(zone_connect 2)", net2=(1, "GND"), net1=(2, "SIG"))
            + _zone(1, "GND", "F.Cu", _RIGHT)
        )
        (asym,) = asymmetric_passives(_load(tmp_path, text))
        assert asym.solid_pad.number == "2"
        assert asym.other_pad.number == "1"
        assert asym.other_contacts == ()


_FULL = (0.0, 0.0, 20.0, 20.0)


def _pth_header(*, zone_connect: str = "(zone_connect 2)") -> str:
    return f"""  (footprint "Connector:Pin" (layer "F.Cu") (at 10 10)
    (property "Reference" "J1" (at 0 -2) (layer "F.SilkS"))
    (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask")
      (net 1 "GND") {zone_connect})
  )
"""


class TestPthFinding:
    def test_pth_solid_on_three_layers_fires(self, tmp_path):
        zones = "".join(_zone(1, "GND", layer, _FULL) for layer in ("F.Cu", "In1.Cu", "B.Cu"))
        found = _findings(_load(tmp_path, _board(_pth_header() + zones, layers=_LAYERS_4)))
        assert len(found) == 1
        v = found[0]
        assert v.items == ("J1", "J1-1")
        assert v.actual_value == 3.0
        assert "F.Cu, In1.Cu, B.Cu" in v.message

    def test_pth_solid_on_one_layer_does_not_fire(self, tmp_path):
        found = _findings(
            _load(
                tmp_path, _board(_pth_header() + _zone(1, "GND", "B.Cu", _FULL), layers=_LAYERS_4)
            )
        )
        assert found == []

    def test_pth_thermal_on_every_layer_does_not_fire(self, tmp_path):
        zones = "".join(_zone(1, "GND", layer, _FULL) for layer in ("F.Cu", "In1.Cu", "B.Cu"))
        text = _board(_pth_header(zone_connect="") + zones, layers=_LAYERS_4)
        assert _findings(_load(tmp_path, text)) == []

    def test_exposed_pad_thermal_vias_are_exempt(self, tmp_path):
        signal = "".join(
            f'    (pad "{n}" smd rect (at {x} 2) (size 0.3 0.8) (layers "F.Cu" "F.Mask") (net 2 "SIG"))\n'
            for n, x in (("1", -1.0), ("2", -0.33), ("3", 0.33), ("4", 1.0))
        )
        fp = f"""  (footprint "Package_DFN_QFN:QFN-8" (layer "F.Cu") (at 10 10)
    (property "Reference" "U1" (at 0 -3) (layer "F.SilkS"))
{signal}    (pad "9" smd rect (at 0 0) (size 2.5 2.5) (layers "F.Cu" "F.Mask") (net 1 "GND"))
    (pad "9" thru_hole circle (at 0.6 0.6) (size 0.6 0.6) (drill 0.3) (layers "*.Cu")
      (net 1 "GND") (zone_connect 2))
  )
"""
        zones = _zone(1, "GND", "F.Cu", _FULL) + _zone(1, "GND", "B.Cu", _FULL)
        assert _findings(_load(tmp_path, _board(fp + zones))) == []


class TestWiring:
    def test_checker_category_and_cli(self, tmp_path):
        from kicad_tools.cli import check_cmd
        from kicad_tools.cli.check_categories import CHECK_CATEGORIES

        assert "check_thermal_asymmetry" in DRCChecker.CHECK_ALL_METHODS
        assert "thermal_asymmetry" in CHECK_CATEGORIES
        for rule_id in ("thermal_asymmetry", "via_at_pad_edge"):
            assert DRCChecker.category_for_rule(rule_id) == DRCChecker.CATEGORY_ADVISORY

        text = _board(_resistor("R1", pad1="(zone_connect 2)") + _zone(1, "GND", "F.Cu", _LEFT))
        board = tmp_path / "board.kicad_pcb"
        board.write_text(text)
        pcb = PCB.load(str(board))
        found = DRCChecker(pcb, manufacturer="jlcpcb", layers=2).check_thermal_asymmetry()
        assert [v.rule_id for v in found.violations] == ["thermal_asymmetry"]

        # Warning severity: plain ``kct check`` stays exit 0, ``--strict`` blocks.
        rc = check_cmd.main([str(board), "--only", "thermal_asymmetry", "--drc-only"])
        assert rc == 0
        rc = check_cmd.main([str(board), "--only", "thermal_asymmetry", "--drc-only", "--strict"])
        assert rc != 0
        rc = check_cmd.main(
            [
                str(board),
                "--only",
                "clearance",
                "--skip",
                "thermal_asymmetry",
                "--drc-only",
                "--strict",
            ]
        )
        assert rc == 0


# ---------------------------------------------------------------------------
# Fleet census on the committed boards (issue #7220 acceptance criteria).
# Re-measured on the committed files and on kicad-cli 10.0.6 --refill-zones
# scratch copies; both agree.
# ---------------------------------------------------------------------------

_FLEET = {
    "00-simple-led/output/simple_led_routed.kicad_pcb": (["R1-1"], 0),
    "01-voltage-divider/output/voltage_divider_routed.kicad_pcb": (["R2-2"], 0),
    "02-charlieplex-led/output/charlieplex_3x3_routed.kicad_pcb": ([], 0),
    "03-usb-joystick/output/usb_joystick_routed.kicad_pcb": (
        ["C10-2", "C11-2", "J1-A1", "J1-A12", "J1-B1", "J1-B12"],
        0,
    ),
    "04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb": (
        ["C1-1", "C12-1", "C13-1", "C14-1", "C15-1", "C16-1", "C2-1", "C3-1", "R1-1"],
        0,
    ),
    "05-bldc-motor-controller/output/bldc_controller_routed.kicad_pcb": ([], 0),
    "06-diffpair-test/output/diffpair_test_routed.kicad_pcb": ([], 0),
    "07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb": ([], 0),
}


@pytest.mark.integration
@pytest.mark.parametrize("relative", sorted(_FLEET))
def test_fleet_census(relative):
    path = REPO / "boards" / relative
    if not path.exists():
        pytest.skip(f"{relative} not committed")
    expected_pads, expected_edge = _FLEET[relative]
    pcb = PCB.load(str(path))
    checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=len(pcb.copper_layers))
    thermal = checker.check_thermal_asymmetry().violations
    assert sorted(v.items[1] for v in thermal) == sorted(expected_pads)
    vip = checker.check_via_in_pad().violations
    assert [v.rule_id for v in vip if v.rule_id != "via_at_pad_edge"] == []
    assert sum(v.rule_id == "via_at_pad_edge" for v in vip) == expected_edge
