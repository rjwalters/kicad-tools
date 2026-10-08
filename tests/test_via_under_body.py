"""Tests for the via-under-package-body rule (``via_under_body``).

Unit tests build in-memory ``PCB(SExp(...))`` fixtures (mirroring
``tests/test_connector_edge_access.py``); fixture/CLI tests load
``tests/fixtures/via_under_body.kicad_pcb`` and drive ``check_cmd.main``.

Fixture layout (sheet-absolute):

* U1 ``QFN-8-1EP_3x3mm`` at (110, 115): F.Fab body +-1.5mm (pin-1 chamfer),
  1.5x1.5mm exposed pad "9" on GND.
* U2 ``SOIC-8`` at (122, 115): not selected by the default pattern.
* via aaaaaaaa (SIG) at (110, 116.1): under U1's body, clear of the EP.
* via bbbbbbbb (SIG) at (110, 116.85): 0.05mm outside U1's body.
* via cccccccc (GND) at (110.3, 115.3): thermal via inside U1's EP.
* via dddddddd (SIG) at (122, 115): under U2 (SOIC, not selected).

Findings name a via by its geometry (``Via@x/y:span:d<drill>/s<size>``,
Issue #6088), not by its UUID.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kicad_tools.cli import check_cmd
from kicad_tools.schema.pcb import PCB, Footprint, FootprintGraphic, Pad, Via
from kicad_tools.sexp import SExp
from kicad_tools.validate.checker import DRCChecker
from kicad_tools.validate.rules.via_under_body import (
    VIA_UNDER_BODY_RULE_ID,
    ViaUnderBodyRule,
    exposed_pads,
)

pytest.importorskip("shapely")

FIXTURE = Path(__file__).parent / "fixtures" / "via_under_body.kicad_pcb"

QFN_NAME = "Package_DFN_QFN:QFN-8-1EP_3x3mm_P0.65mm_EP1.5x1.5mm"


# ---------------------------------------------------------------------------
# In-memory helpers
# ---------------------------------------------------------------------------


def _fab_rect(half: float, layer: str = "F.Fab") -> FootprintGraphic:
    return FootprintGraphic(
        graphic_type="rect",
        layer=layer,
        stroke_width=0.1,
        start=(-half, -half),
        end=(half, half),
    )


def _crtyd_rect(half: float, layer: str = "F.CrtYd") -> FootprintGraphic:
    return FootprintGraphic(
        graphic_type="rect",
        layer=layer,
        stroke_width=0.05,
        start=(-half, -half),
        end=(half, half),
    )


def _qfn(
    *,
    reference: str = "U1",
    name: str = QFN_NAME,
    position: tuple[float, float] = (50.0, 50.0),
    rotation: float = 0.0,
    layer: str = "F.Cu",
    graphics: list[FootprintGraphic] | None = None,
    ep_net: tuple[int, str] = (1, "GND"),
) -> Footprint:
    cu = "B.Cu" if layer.startswith("B.") else "F.Cu"
    pads = [
        Pad(
            number=str(i + 1),
            type="smd",
            shape="rect",
            position=(-1.45 if i < 4 else 1.45, -0.975 + 0.65 * (i % 4)),
            size=(0.8, 0.3),
            layers=[cu],
            net_number=2,
            net_name="SIG",
        )
        for i in range(8)
    ]
    pads.append(
        Pad(
            number="9",
            type="smd",
            shape="rect",
            position=(0.0, 0.0),
            size=(1.5, 1.5),
            layers=[cu],
            net_number=ep_net[0],
            net_name=ep_net[1],
            rotation=rotation,
        )
    )
    return Footprint(
        name=name,
        layer=layer,
        position=position,
        rotation=rotation,
        reference=reference,
        value="QFN",
        pads=pads,
        graphics=[_fab_rect(1.5)] if graphics is None else graphics,
    )


def _via(
    x: float, y: float, *, net: tuple[int, str] = (2, "SIG"), size: float = 0.6, uuid: str = ""
) -> Via:
    return Via(
        position=(x, y),
        size=size,
        drill=0.3,
        layers=["F.Cu", "B.Cu"],
        net_number=net[0],
        net_name=net[1],
        uuid=uuid,
    )


def _pcb(footprints: list[Footprint], vias: list[Via]) -> PCB:
    pcb = PCB(SExp(name="kicad_pcb"))
    pcb._footprints.extend(footprints)
    pcb._vias.extend(vias)
    return pcb


def _run(pcb: PCB, **kwargs):
    return ViaUnderBodyRule(**kwargs).check(pcb, None).filter_by_rule(VIA_UNDER_BODY_RULE_ID)


# ---------------------------------------------------------------------------
# Core geometry
# ---------------------------------------------------------------------------


class TestGeometry:
    def test_via_under_qfn_body_flagged(self):
        pcb = _pcb([_qfn()], [_via(50.0, 51.1, uuid="aaaaaaaa-1")])
        found = _run(pcb)
        assert len(found) == 1
        v = found[0]
        assert v.severity == "warning"
        assert v.items == ("Via@50/51.1:F.Cu-B.Cu:d0.3/s0.6", "U1")
        assert v.nets == ("SIG",)
        assert "U1" in v.message

    def test_via_just_outside_body_ok(self):
        # Body edge at y=51.5; via copper spans 51.55..52.15.
        pcb = _pcb([_qfn()], [_via(50.0, 51.85)])
        assert _run(pcb) == []

    def test_via_copper_grazing_body_edge_flagged(self):
        # Via centre outside the body, but its copper overlaps it.
        pcb = _pcb([_qfn()], [_via(50.0, 51.7)])
        assert len(_run(pcb)) == 1

    def test_rotation_is_honored(self):
        # A 4x1mm body rotated 90 degrees spans x +-0.5, y +-2.
        body = FootprintGraphic(
            graphic_type="rect", layer="F.Fab", stroke_width=0.1, start=(-2, -0.5), end=(2, 0.5)
        )
        fp = _qfn(graphics=[body], rotation=90.0)
        assert len(_run(_pcb([fp], [_via(50.0, 51.5, size=0.4)]))) == 1
        assert _run(_pcb([fp], [_via(51.5, 50.0, size=0.4)])) == []

    def test_back_side_footprint_uses_b_fab(self):
        fp = _qfn(layer="B.Cu", graphics=[_fab_rect(1.5, layer="B.Fab")])
        assert len(_run(_pcb([fp], [_via(50.0, 51.1)]))) == 1

    def test_blind_via_not_reaching_component_side_skipped(self):
        via = _via(50.0, 51.1)
        via.layers = ["In1.Cu", "B.Cu"]
        assert _run(_pcb([_qfn()], [via])) == []

    def test_fab_line_loop_with_chamfer(self):
        """KiCad-5 style Fab outline: a closed loop of fp_line segments."""
        pts = [(-0.75, -1.5), (1.5, -1.5), (1.5, 1.5), (-1.5, 1.5), (-1.5, -0.75)]
        lines = [
            FootprintGraphic(
                graphic_type="line",
                layer="F.Fab",
                stroke_width=0.1,
                start=pts[i],
                end=pts[(i + 1) % len(pts)],
            )
            for i in range(len(pts))
        ]
        fp = _qfn(graphics=lines)
        assert len(_run(_pcb([fp], [_via(50.0, 51.1)]))) == 1
        # Inside the chamfer's cut-off corner -> outside the body.
        assert _run(_pcb([fp], [_via(48.6, 48.6, size=0.2)])) == []

    def test_largest_fab_shape_is_the_body(self):
        """A small Fab mark must not stand in for the body outline."""
        tick = FootprintGraphic(
            graphic_type="rect", layer="F.Fab", stroke_width=0.1, start=(-0.2, -0.2), end=(0.2, 0.2)
        )
        fp = _qfn(graphics=[tick, _fab_rect(1.5)])
        assert len(_run(_pcb([fp], [_via(50.0, 51.1)]))) == 1


# ---------------------------------------------------------------------------
# Body-outline fallback
# ---------------------------------------------------------------------------


class TestCourtyardFallback:
    def test_courtyard_used_without_fab(self):
        fp = _qfn(graphics=[_crtyd_rect(2.0)])
        found = _run(_pcb([fp], [_via(50.0, 51.8)]))
        assert len(found) == 1
        assert "courtyard" in found[0].message

    def test_courtyard_fallback_can_be_disabled(self):
        fp = _qfn(graphics=[_crtyd_rect(2.0)])
        assert _run(_pcb([fp], [_via(50.0, 51.8)]), fallback_to_courtyard=False) == []

    def test_fab_preferred_over_courtyard(self):
        fp = _qfn(graphics=[_fab_rect(1.5), _crtyd_rect(2.0)])
        # Inside the courtyard but outside the Fab body -> not flagged.
        assert _run(_pcb([fp], [_via(50.0, 51.85)])) == []

    def test_no_outline_skipped(self):
        fp = _qfn(graphics=[])
        assert _run(_pcb([fp], [_via(50.0, 50.0)])) == []


# ---------------------------------------------------------------------------
# Thermal-pad vias
# ---------------------------------------------------------------------------


class TestThermalPadVias:
    def test_thermal_via_allowed_by_default(self):
        pcb = _pcb([_qfn()], [_via(50.3, 50.3, net=(1, "GND"))])
        assert _run(pcb) == []

    def test_thermal_via_flagged_when_disallowed(self):
        pcb = _pcb([_qfn()], [_via(50.3, 50.3, net=(1, "GND"))])
        found = _run(pcb, allow_thermal_pad_vias=False)
        assert len(found) == 1
        assert found[0].nets == ("GND",)

    def test_other_net_via_in_ep_region_flagged(self):
        pcb = _pcb([_qfn()], [_via(50.3, 50.3, net=(2, "SIG"))])
        assert len(_run(pcb)) == 1

    def test_ep_net_via_outside_ep_flagged(self):
        """A GND via under the body but off the exposed pad is not a thermal via."""
        pcb = _pcb([_qfn()], [_via(50.0, 51.1, net=(1, "GND"))])
        assert len(_run(pcb)) == 1

    def test_exposed_pad_detection(self):
        assert [p.number for p in exposed_pads(_qfn())] == ["9"]


def _split_ep_qfn(*, ep_net: tuple[int, str] = (1, "GND")) -> Footprint:
    """A QFN-8 whose 1.5x1.5 thermal pad is split into four 0.7x0.7 pads."""
    fp = _qfn(ep_net=ep_net)
    fp.pads = [p for p in fp.pads if p.number != "9"]
    for i, (dx, dy) in enumerate(
        [(-0.375, -0.375), (0.375, -0.375), (-0.375, 0.375), (0.375, 0.375)]
    ):
        fp.pads.append(
            Pad(
                number="9" if i == 0 else f"9{chr(ord('a') + i)}",
                type="smd",
                shape="rect",
                position=(dx, dy),
                size=(0.7, 0.7),
                layers=["F.Cu"],
                net_number=ep_net[0],
                net_name=ep_net[1],
            )
        )
    return fp


class TestSplitExposedPad:
    def test_split_pads_individually_under_ratio(self):
        """Guard the premise: no single split pad passes the area ratio."""
        typical = 0.8 * 0.3
        assert 0.7 * 0.7 < 4.0 * typical <= 4 * 0.7 * 0.7

    def test_split_ep_cluster_detected(self):
        assert sorted(p.number for p in exposed_pads(_split_ep_qfn())) == [
            "9",
            "9b",
            "9c",
            "9d",
        ]

    def test_thermal_via_over_split_pad_allowed(self):
        pcb = _pcb([_split_ep_qfn()], [_via(50.375, 50.375, net=(1, "GND"))])
        assert _run(pcb) == []

    def test_other_net_via_over_split_pad_flagged(self):
        pcb = _pcb([_split_ep_qfn()], [_via(50.375, 50.375, net=(2, "SIG"))])
        assert len(_run(pcb)) == 1

    def test_same_net_signal_pins_not_a_cluster(self):
        """Median-sized same-net pins never sum into a phantom exposed pad."""
        fp = _qfn()
        for pad in fp.pads[:6]:
            pad.net_number, pad.net_name = 3, "VDD"
        assert [p.number for p in exposed_pads(fp)] == ["9"]

    def test_intact_ep_does_not_absorb_larger_than_median_pins(self):
        """Pins 1/2 on the EP's net, slightly > median, are not thermal pads."""
        fp = _qfn()
        for pad in fp.pads[:2]:
            pad.net_number, pad.net_name = 1, "GND"
            pad.size = (0.8, 0.4)
        assert [p.number for p in exposed_pads(fp)] == ["9"]
        # A GND via inside pin 1's copper is still reported.
        pin1 = fp.pads[0]
        pcb = _pcb([fp], [_via(50.0 + pin1.position[0], 50.0 + pin1.position[1], net=(1, "GND"))])
        assert len(_run(pcb)) == 1

    def test_unconnected_split_pads_not_clustered(self):
        """Net 0 pads are never grouped together."""
        assert exposed_pads(_split_ep_qfn(ep_net=(0, ""))) == []


# ---------------------------------------------------------------------------
# Selection / configuration
# ---------------------------------------------------------------------------


class TestSelection:
    @pytest.mark.parametrize(
        "name",
        [
            "Package_DFN_QFN:DFN-8-1EP_2x2mm_P0.5mm_EP0.9x1.6mm",
            "Package_SON:WSON-8-1EP_2x2mm_P0.5mm_EP0.9x1.6mm",
            "Package_LGA:LGA-14_3x2.5mm_P0.5mm_LayoutBorder3x4y",
            "package_dfn_qfn:qfn-8-1ep_3x3mm",
            "MyLib:Wson-6_1.5x1.5mm",
            "vendor:lga-12",
            "vendor:usoN-8",
            "MyLib:vson-10_3x3mm",
            "Package_SON:X2SON-8_1.4x1mm_P0.35mm",
            "vendor:Dfn-6",
            "vendor:Qfn-20",
            "Package_SON:WSON-8-1EP_2x2mm",
            "Package_SON:VSON-8_3x3mm",
            "Package_SON:USON-10_2.5x1.0mm",
            "Package_DFN_QFN:TDFN-8-1EP_3x3mm",
            "Package_DFN_QFN:UDFN-6_1.45x1mm",
        ],
    )
    def test_default_pattern_selects(self, name: str):
        pcb = _pcb([_qfn(name=name)], [_via(50.0, 51.1)])
        assert len(_run(pcb)) == 1

    @pytest.mark.parametrize(
        "name",
        [
            "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
            "Package_QFP:LQFP-48_7x7mm_P0.5mm",
            "Package_BGA:BGA-64_9.0x9.0mm_Layout10x10_P0.8mm",
            "Resistor_SMD:R_0603_1608Metric",
            # "son" inside vendor/part names must not select (case-insensitive
            # match is anchored to the SON package token).
            "Crystal:Resonator_SMD_Murata_CSTxExxV-3Pin_3.0x1.1mm",
            "Inductor_SMD:L_Panasonic_PCC-M0530M",
            "Oscillator:Oscillator_SMD_SeikoEpson_SG210-4Pin_2.5x2.0mm",
            "Crystal:Resonator_SMD_muRata_CSTCC2_Series",
            "Sensor:Person_Detector",
            "Inductor_SMD:L_Vulgaris",
            "Transformer_SMD:Transformer_Nolga",
            "TerminalBlock:TerminalBlock_Degson_DG246-3.81-2P",
        ],
    )
    def test_default_pattern_skips(self, name: str):
        pcb = _pcb([_qfn(name=name)], [_via(50.0, 51.1)])
        assert _run(pcb) == []

    @pytest.mark.parametrize(
        "name", ["Package_BGA:BGA-64_9.0x9.0mm", "Package_QFP:LQFP-48_7x7mm_P0.5mm"]
    )
    def test_custom_pattern_opts_in(self, name: str):
        pcb = _pcb([_qfn(name=name)], [_via(50.0, 51.1)])
        assert len(_run(pcb, footprint_pattern=r"QFN|DFN|SON|LGA|QFP|BGA")) == 1

    def test_compiled_pattern_flags_untouched(self):
        """A pre-compiled pattern keeps its own (case-sensitive) flags."""
        pcb = _pcb([_qfn(name="vendor:qfn-8")], [_via(50.0, 51.1)])
        assert _run(pcb, footprint_pattern=re.compile(r"QFN")) == []
        assert len(_run(pcb, footprint_pattern=r"QFN")) == 1

    def test_include_and_exclude_references(self):
        soic = _qfn(reference="U7", name="Package_SO:SOIC-8_3.9x4.9mm_P1.27mm")
        pcb = _pcb([soic], [_via(50.0, 51.1)])
        assert len(_run(pcb, include_references=["U7"])) == 1
        assert _run(_pcb([_qfn()], [_via(50.0, 51.1)]), exclude_references=["U1"]) == []

    def test_pattern_none_checks_only_included(self):
        pcb = _pcb([_qfn()], [_via(50.0, 51.1)])
        assert _run(pcb, footprint_pattern=None) == []
        assert len(_run(pcb, footprint_pattern=None, include_references=["U1"])) == 1

    def test_severity_configurable(self):
        pcb = _pcb([_qfn()], [_via(50.0, 51.1)])
        assert _run(pcb, severity="error")[0].severity == "error"

    def test_invalid_severity_rejected(self):
        with pytest.raises(ValueError):
            ViaUnderBodyRule(severity="fatal")


# ---------------------------------------------------------------------------
# Fixture board / checker / CLI
# ---------------------------------------------------------------------------


class TestFixtureBoard:
    def test_fixture_default(self):
        pcb = PCB.load(str(FIXTURE))
        found = _run(pcb)
        assert [v.items for v in found] == [("Via@110/116.1:F.Cu-B.Cu:d0.3/s0.6", "U1")]

    def test_fixture_thermal_disallowed(self):
        pcb = PCB.load(str(FIXTURE))
        found = _run(pcb, allow_thermal_pad_vias=False)
        assert sorted(v.items for v in found) == [
            ("Via@110.3/115.3:F.Cu-B.Cu:d0.3/s0.6", "U1"),
            ("Via@110/116.1:F.Cu-B.Cu:d0.3/s0.6", "U1"),
        ]

    def test_checker_reports_sheet_absolute_location(self):
        checker = DRCChecker(PCB.load(str(FIXTURE)), manufacturer="jlcpcb", layers=2)
        found = checker.check_via_under_body().filter_by_rule(VIA_UNDER_BODY_RULE_ID)
        assert len(found) == 1
        assert found[0].location == (110.0, 116.1)


class TestCheckerWiring:
    def test_check_all_methods_includes_rule(self):
        assert "check_via_under_body" in DRCChecker.CHECK_ALL_METHODS

    def test_cli_category_registered(self):
        assert "via_under_body" in check_cmd.CHECK_CATEGORIES

    def test_rule_classified_advisory(self):
        """Required: the ``via`` prefix fallback would file it as Manufacturing."""
        assert DRCChecker.category_for_rule(VIA_UNDER_BODY_RULE_ID) == DRCChecker.CATEGORY_ADVISORY


class TestCLI:
    def test_json_output(self, capsys):
        rc = check_cmd.main(
            [str(FIXTURE), "--only", "via_under_body", "--drc-only", "--format", "json"]
        )
        data = json.loads(capsys.readouterr().out)
        assert rc == 0  # warnings never fail the plain gate
        found = [v for v in data["violations"] if v["rule_id"] == VIA_UNDER_BODY_RULE_ID]
        assert len(found) == 1
        assert found[0]["items"] == ["Via@110/116.1:F.Cu-B.Cu:d0.3/s0.6", "U1"]
        assert found[0]["severity"] == "warning"
        assert found[0]["location"] == [110.0, 116.1]

    def test_strict_blocks_on_warning(self, capsys):
        rc = check_cmd.main([str(FIXTURE), "--only", "via_under_body", "--drc-only", "--strict"])
        capsys.readouterr()
        assert rc != 0

    def test_skip(self, capsys):
        check_cmd.main([str(FIXTURE), "--skip", "via_under_body", "--drc-only", "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        assert all(v["rule_id"] != VIA_UNDER_BODY_RULE_ID for v in data["violations"])

    def test_waiver_suppresses_finding(self, capsys, tmp_path: Path):
        board = tmp_path / FIXTURE.name
        board.write_text(FIXTURE.read_text())
        (tmp_path / ".kct_waivers.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "waivers": [
                        {
                            "rule": VIA_UNDER_BODY_RULE_ID,
                            "items": ["Via@110/116.1:F.Cu-B.Cu:d0.3/s0.6", "U1"],
                            "reason": "debug-only via, tented",
                            "issue": "test",
                        }
                    ],
                }
            )
        )
        rc = check_cmd.main([str(board), "--only", "via_under_body", "--drc-only", "--strict"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "WAIVED" in out
