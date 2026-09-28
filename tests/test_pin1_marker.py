"""Tests for the pin-1 / polarity silkscreen-marker rule.

Unit tests build in-memory ``PCB(SExp(...))`` fixtures (mirroring
``tests/test_connector_edge_access.py``); fixture/CLI tests load
``tests/fixtures/pin1_marker.kicad_pcb`` and drive ``check_cmd.main``.

Fixture layout (sheet-absolute, all front side):

* U1 QFN-8 at (108, 108): filled silk triangle next to pad 1 -> ok.
* U2 QFN-8 at (116, 108): only a symmetric silk rectangle -> missing.
* U3 QFN-8 at (124, 108): pin-1 silk dot entirely under the body -> obscured.
* D1 LED_0603 at (108, 120): "[" outline with the cathode bar -> ok.
* D2 LED_0603 at (116, 120): only top/bottom lines -> missing.
* R1 R_0603 at (124, 120): no silk at all, not selected -> ok.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli import check_cmd
from kicad_tools.schema.pcb import (
    PCB,
    BoardGraphic,
    Footprint,
    FootprintGraphic,
    FootprintText,
    Pad,
)
from kicad_tools.sexp import SExp
from kicad_tools.validate.checker import DRCChecker
from kicad_tools.validate.rules.pin1_marker import (
    PIN1_MARKER_MISSING_RULE_ID,
    PIN1_MARKER_OBSCURED_RULE_ID,
    Pin1MarkerRule,
)

pytest.importorskip("shapely")

FIXTURE = Path(__file__).parent / "fixtures" / "pin1_marker.kicad_pcb"

QFN_NAME = "Package_DFN_QFN:QFN-8-1EP_3x3mm_P0.65mm_EP1.5x1.5mm"
LED_NAME = "LED_SMD:LED_0603_1608Metric"


# ---------------------------------------------------------------------------
# In-memory helpers
# ---------------------------------------------------------------------------


def _g(kind: str, layer: str = "F.SilkS", width: float = 0.12, **kwargs) -> FootprintGraphic:
    return FootprintGraphic(graphic_type=kind, layer=layer, stroke_width=width, **kwargs)


def _fab_body(half: float = 1.5, layer: str = "F.Fab") -> FootprintGraphic:
    return _g("rect", layer=layer, width=0.1, start=(-half, -half), end=(half, half))


def _triangle(layer: str = "F.SilkS") -> FootprintGraphic:
    return _g("poly", layer=layer, points=[(-2.1, -0.975), (-2.4, -0.775), (-2.4, -1.175)])


def _qfn_pads(cu: str = "F.Cu") -> list[Pad]:
    pads = [
        Pad(
            number=str(i + 1),
            type="smd",
            shape="rect",
            position=(
                -1.45 if i < 4 else 1.45,
                (-0.975 + 0.65 * i) if i < 4 else (0.975 - 0.65 * (i - 4)),
            ),
            size=(0.8, 0.3),
            layers=[cu],
        )
        for i in range(8)
    ]
    pads.append(
        Pad(number="9", type="smd", shape="rect", position=(0, 0), size=(1.5, 1.5), layers=[cu])
    )
    return pads


def _led_pads() -> list[Pad]:
    return [
        Pad(
            number=n,
            type="smd",
            shape="rect",
            position=(x, 0.0),
            size=(0.875, 0.95),
            layers=["F.Cu"],
        )
        for n, x in (("1", -0.7875), ("2", 0.7875))
    ]


def _fp(
    *,
    name: str = QFN_NAME,
    reference: str = "U1",
    graphics: list[FootprintGraphic] | None = None,
    pads: list[Pad] | None = None,
    position: tuple[float, float] = (50.0, 50.0),
    rotation: float = 0.0,
    layer: str = "F.Cu",
    texts: list[FootprintText] | None = None,
) -> Footprint:
    return Footprint(
        name=name,
        layer=layer,
        position=position,
        rotation=rotation,
        reference=reference,
        value="X",
        pads=_qfn_pads() if pads is None else pads,
        texts=texts or [],
        graphics=[_fab_body()] if graphics is None else graphics,
    )


def _pcb(*footprints: Footprint, board_graphics: list[BoardGraphic] | None = None) -> PCB:
    pcb = PCB(SExp(name="kicad_pcb"))
    pcb._footprints.extend(footprints)
    pcb._graphics.extend(board_graphics or [])
    return pcb


def _run(pcb: PCB, **kwargs):
    return Pin1MarkerRule(**kwargs).check(pcb, None).violations


def _ids(violations) -> list[tuple[str, tuple[str, ...]]]:
    return [(v.rule_id, v.items) for v in violations]


# ---------------------------------------------------------------------------
# Marker shapes
# ---------------------------------------------------------------------------


class TestMarkerShapes:
    def test_triangle_marker_ok(self):
        assert _run(_pcb(_fp(graphics=[_fab_body(), _triangle()]))) == []

    def test_no_silk_missing(self):
        found = _run(_pcb(_fp()))
        assert _ids(found) == [(PIN1_MARKER_MISSING_RULE_ID, ("U1",))]
        assert found[0].severity == "warning"
        # Location is pad 1's centre (board-relative here).
        assert found[0].location == pytest.approx((48.55, 49.025))

    def test_dot_marker_ok(self):
        dot = _g("circle", center=(-2.2, -1.5), end=(-2.05, -1.5))
        assert _run(_pcb(_fp(graphics=[_fab_body(), dot]))) == []

    def test_arc_marker_ok(self):
        arc = _g("arc", start=(-2.3, -1.3), mid=(-2.0, -1.6), end=(-1.7, -1.9))
        assert _run(_pcb(_fp(graphics=[_fab_body(), arc]))) == []

    def test_line_bar_marker_ok(self):
        bar = _g("line", start=(-2.2, -1.2), end=(-2.2, -0.75))
        assert _run(_pcb(_fp(graphics=[_fab_body(), bar]))) == []

    def test_user_text_one_ok(self):
        text = FootprintText(
            text_type="user",
            text="1",
            position=(-2.4, -1.0),
            layer="F.SilkS",
            font_size=(0.5, 0.5),
            font_thickness=0.1,
        )
        assert _run(_pcb(_fp(texts=[text]))) == []

    def test_reference_text_does_not_count(self):
        text = FootprintText(
            text_type="reference",
            text="U1",
            position=(-2.4, -1.0),
            layer="F.SilkS",
            font_size=(0.5, 0.5),
            font_thickness=0.1,
        )
        assert _ids(_run(_pcb(_fp(texts=[text])))) == [(PIN1_MARKER_MISSING_RULE_ID, ("U1",))]

    def test_marker_on_fab_only_is_missing(self):
        fab_tri = _triangle(layer="F.Fab")
        found = _run(_pcb(_fp(graphics=[_fab_body(), fab_tri])))
        assert _ids(found) == [(PIN1_MARKER_MISSING_RULE_ID, ("U1",))]

    def test_board_level_silk_marker_ok(self):
        dot = BoardGraphic(
            graphic_type="circle",
            layer="F.SilkS",
            stroke_width=0.12,
            center=(47.8, 48.5),
            end=(47.95, 48.5),
        )
        assert _run(_pcb(_fp(), board_graphics=[dot])) == []
        assert len(_run(_pcb(_fp(), board_graphics=[dot]), include_board_silk=False)) == 1


# ---------------------------------------------------------------------------
# Asymmetry / visibility
# ---------------------------------------------------------------------------


class TestAsymmetryAndVisibility:
    def test_symmetric_outline_is_missing(self):
        ring = _g("rect", start=(-1.61, -1.61), end=(1.61, 1.61))
        found = _run(_pcb(_fp(graphics=[_fab_body(), ring])))
        assert _ids(found) == [(PIN1_MARKER_MISSING_RULE_ID, ("U1",))]

    def test_symmetric_outline_accepted_without_asymmetry(self):
        ring = _g("rect", start=(-1.61, -1.61), end=(1.61, 1.61))
        assert _run(_pcb(_fp(graphics=[_fab_body(), ring])), require_asymmetry=False) == []

    def test_marker_under_body_is_obscured(self):
        dot = _g("circle", center=(-0.9, -0.9), end=(-0.75, -0.9))
        found = _run(_pcb(_fp(graphics=[_fab_body(), dot])))
        assert _ids(found) == [(PIN1_MARKER_OBSCURED_RULE_ID, ("U1",))]
        assert "under the package body" in found[0].message

    def test_marker_on_pad_copper_is_obscured(self):
        # A dot fully on pad 1's copper is clipped away by the fab.
        dot = _g("circle", center=(-1.45, -0.975), end=(-1.4, -0.975), width=0.05)
        found = _run(_pcb(_fp(graphics=[_fab_body(), dot])))
        assert _ids(found) == [(PIN1_MARKER_OBSCURED_RULE_ID, ("U1",))]

    def test_marker_straddling_body_edge_ok(self):
        bar = _g("line", start=(-1.9, -1.2), end=(-1.2, -1.2))
        assert _run(_pcb(_fp(graphics=[_fab_body(), bar]))) == []

    def test_search_radius(self):
        far = _g("circle", center=(-5.0, -1.0), end=(-4.85, -1.0))
        pcb = _pcb(_fp(graphics=[_fab_body(), far]))
        assert len(_run(pcb)) == 1
        assert _run(pcb, search_radius_mm=3.5) == []

    def test_led_cathode_bar(self):
        top = _g("line", start=(1.46, -0.735), end=(-1.485, -0.735))
        bottom = _g("line", start=(-1.485, 0.735), end=(1.46, 0.735))
        bar = _g("line", start=(-1.485, -0.735), end=(-1.485, 0.735))
        body = _g("rect", layer="F.Fab", width=0.1, start=(-0.8, -0.4), end=(0.8, 0.4))
        ok = _fp(name=LED_NAME, reference="D1", pads=_led_pads(), graphics=[body, top, bar, bottom])
        bad = _fp(name=LED_NAME, reference="D2", pads=_led_pads(), graphics=[body, top, bottom])
        assert _ids(_run(_pcb(ok, bad))) == [(PIN1_MARKER_MISSING_RULE_ID, ("D2",))]


# ---------------------------------------------------------------------------
# Multi-segment corner marks (stock KiCad crystal L-marker, #5737 review)
# ---------------------------------------------------------------------------

CRYSTAL_NAME = "Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm"


def _crystal_pads() -> list[Pad]:
    # Stock KiCad 10 Crystal_SMD_3225-4Pin_3.2x2.5mm pad layout.
    return [
        Pad(
            number=n,
            type="smd",
            shape="roundrect",
            position=pos,
            size=(1.4, 1.2),
            layers=["F.Cu"],
        )
        for n, pos in (
            ("1", (-1.1, 0.85)),
            ("2", (1.1, 0.85)),
            ("3", (1.1, -0.85)),
            ("4", (-1.1, -0.85)),
        )
    ]


def _crystal_fab() -> FootprintGraphic:
    return _g(
        "poly",
        layer="F.Fab",
        width=0.1,
        points=[(1.6, -1.25), (1.6, 1.25), (-0.975, 1.25), (-1.6, 0.625), (-1.6, -1.25)],
    )


def _crystal(graphics: list[FootprintGraphic], rotation: float = 0.0) -> Footprint:
    return _fp(
        name=CRYSTAL_NAME,
        reference="Y1",
        pads=_crystal_pads(),
        graphics=[_crystal_fab(), *graphics],
        rotation=rotation,
    )


def _stock_crystal_l() -> list[FootprintGraphic]:
    # The stock silk: two separate fp_lines forming an L at pad 1's corner.
    return [
        _g("line", start=(-2.06, -1.71), end=(-2.06, 1.71)),
        _g("line", start=(-2.06, 1.71), end=(2.06, 1.71)),
    ]


class TestCornerMarks:
    @pytest.mark.parametrize("rotation", [0.0, 90.0, 180.0, 270.0])
    def test_stock_crystal_l_marker_ok(self, rotation: float):
        # Each leg ties between pad 1 and a neighbour (pad 4 / pad 2); the L as
        # a whole has its centroid in pad 1's corner and must be accepted.
        assert _run(_pcb(_crystal(_stock_crystal_l(), rotation=rotation))) == []

    def test_crystal_without_silk_is_missing(self):
        found = _run(_pcb(_crystal([])))
        assert _ids(found) == [(PIN1_MARKER_MISSING_RULE_ID, ("Y1",))]

    def test_crystal_l_at_wrong_pad_is_missing(self):
        # Mirrored L in pad 2's corner points at pad 2, not pad 1.
        mirrored = [
            _g("line", start=(2.06, -1.71), end=(2.06, 1.71)),
            _g("line", start=(2.06, 1.71), end=(-2.06, 1.71)),
        ]
        found = _run(_pcb(_crystal(mirrored)))
        assert _ids(found) == [(PIN1_MARKER_MISSING_RULE_ID, ("Y1",))]

    def test_crystal_single_leg_is_missing(self):
        # One straight leg alone never gets the tie-break.
        found = _run(_pcb(_crystal(_stock_crystal_l()[:1])))
        assert _ids(found) == [(PIN1_MARKER_MISSING_RULE_ID, ("Y1",))]

    def test_crystal_closed_box_is_missing(self):
        box = [
            _g("line", start=(-2.06, -1.71), end=(-2.06, 1.71)),
            _g("line", start=(-2.06, 1.71), end=(2.06, 1.71)),
            _g("line", start=(2.06, 1.71), end=(2.06, -1.71)),
            _g("line", start=(2.06, -1.71), end=(-2.06, -1.71)),
        ]
        found = _run(_pcb(_crystal(box)))
        assert _ids(found) == [(PIN1_MARKER_MISSING_RULE_ID, ("Y1",))]

    def test_crystal_u_outline_is_missing(self):
        # Left + top + bottom: centroid ties between pad 1 and pad 4.
        u_shape = [
            _g("line", start=(2.06, -1.71), end=(-2.06, -1.71)),
            _g("line", start=(-2.06, -1.71), end=(-2.06, 1.71)),
            _g("line", start=(-2.06, 1.71), end=(2.06, 1.71)),
        ]
        found = _run(_pcb(_crystal(u_shape)))
        assert _ids(found) == [(PIN1_MARKER_MISSING_RULE_ID, ("Y1",))]

    def test_crystal_l_tie_break_respects_require_asymmetry_off(self):
        assert _run(_pcb(_crystal(_stock_crystal_l())), require_asymmetry=False) == []


# ---------------------------------------------------------------------------
# Transforms
# ---------------------------------------------------------------------------


class TestTransforms:
    @pytest.mark.parametrize("rotation", [0.0, 90.0, 180.0, 270.0, 30.0])
    def test_rotation_keeps_marker_attached(self, rotation: float):
        fp = _fp(graphics=[_fab_body(), _triangle()], rotation=rotation)
        assert _run(_pcb(fp)) == []

    def test_back_side_uses_b_silk(self):
        fp = _fp(
            layer="B.Cu",
            pads=_qfn_pads("B.Cu"),
            graphics=[_fab_body(layer="B.Fab"), _triangle(layer="B.SilkS")],
        )
        assert _run(_pcb(fp)) == []

    def test_back_side_ignores_front_silk(self):
        fp = _fp(
            layer="B.Cu",
            pads=_qfn_pads("B.Cu"),
            graphics=[_fab_body(layer="B.Fab"), _triangle(layer="F.SilkS")],
        )
        assert len(_run(_pcb(fp))) == 1


# ---------------------------------------------------------------------------
# Selection / configuration
# ---------------------------------------------------------------------------


def _two_pad(name: str, reference: str = "X1") -> Footprint:
    return _fp(name=name, reference=reference, pads=_led_pads(), graphics=[])


class TestSelection:
    @pytest.mark.parametrize(
        "name",
        [
            "Diode_SMD:D_SOD-123",
            "LED_SMD:LED_0805_2012Metric",
            "Capacitor_SMD:CP_Elec_4x5.3",
            "Capacitor_THT:CP_Radial_D5.0mm_P2.00mm",
            "Capacitor_Tantalum_SMD:CP_EIA-3216-18_Kemet-A",
        ],
    )
    def test_two_pin_polarized_selected(self, name: str):
        assert len(_run(_pcb(_two_pad(name)))) == 1

    @pytest.mark.parametrize(
        "name",
        [
            "Resistor_SMD:R_0603_1608Metric",
            "Capacitor_SMD:C_0603_1608Metric",
            "Inductor_SMD:L_0805_2012Metric",
        ],
    )
    def test_two_pin_passives_skipped(self, name: str):
        assert _run(_pcb(_two_pad(name))) == []

    @pytest.mark.parametrize(
        "name",
        [
            "Resistor_SMD:R_Array_Convex_4x0603",
            "MountingHole:MountingHole_3.2mm_M3_Pad_Via",
            "TestPoint:TestPoint_Keystone_5015_Micro-Minature",
            "Button_Switch_SMD:SW_SPST_TL3342",
            "Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12",
            "Connector_Coaxial:U.FL_Hirose_U.FL-R-SMT-1_Vertical",
        ],
    )
    def test_excluded_patterns(self, name: str):
        assert _run(_pcb(_fp(name=name))) == []

    def test_pad_count_heuristic_selects_unknown_ic(self):
        assert len(_run(_pcb(_fp(name="MyLib:CustomIC")))) == 1
        assert _run(_pcb(_fp(name="MyLib:CustomIC")), min_pads=None) == []

    def test_requires_pin1_pad(self):
        pads = [p for p in _qfn_pads() if p.number != "1"]
        assert _run(_pcb(_fp(pads=pads))) == []

    def test_bga_a1(self):
        pads = [
            Pad(
                number=n, type="smd", shape="circle", position=pos, size=(0.4, 0.4), layers=["F.Cu"]
            )
            for n, pos in (
                ("A1", (-0.4, -0.4)),
                ("A2", (0.4, -0.4)),
                ("B1", (-0.4, 0.4)),
                ("B2", (0.4, 0.4)),
            )
        ]
        fp = _fp(name="Package_BGA:BGA-4", pads=pads, graphics=[_fab_body(0.8)])
        found = _run(_pcb(fp))
        assert _ids(found) == [(PIN1_MARKER_MISSING_RULE_ID, ("U1",))]
        assert found[0].location == pytest.approx((49.6, 49.6))

    def test_include_and_exclude_references(self):
        r = _two_pad("Resistor_SMD:R_0603_1608Metric", reference="R9")
        assert len(_run(_pcb(r), include_references=["R9"])) == 1
        assert _run(_pcb(_fp()), exclude_references=["U1"]) == []

    def test_severity_configurable(self):
        assert _run(_pcb(_fp()), severity="info")[0].severity == "info"

    @pytest.mark.parametrize("kwargs", [{"severity": "fatal"}, {"search_radius_mm": 0.0}])
    def test_invalid_config_rejected(self, kwargs):
        with pytest.raises(ValueError):
            Pin1MarkerRule(**kwargs)


# ---------------------------------------------------------------------------
# Fixture board / checker / CLI
# ---------------------------------------------------------------------------

_FIXTURE_EXPECTED = [
    (PIN1_MARKER_MISSING_RULE_ID, ("U2",)),
    (PIN1_MARKER_OBSCURED_RULE_ID, ("U3",)),
    (PIN1_MARKER_MISSING_RULE_ID, ("D2",)),
]


class TestFixtureBoard:
    def test_fixture_findings(self):
        assert _ids(_run(PCB.load(str(FIXTURE)))) == _FIXTURE_EXPECTED

    def test_checker_reports_sheet_absolute_location(self):
        checker = DRCChecker(PCB.load(str(FIXTURE)), manufacturer="jlcpcb", layers=2)
        found = checker.check_pin1_markers().violations
        assert found[0].location == pytest.approx((114.55, 107.025))


class TestCheckerWiring:
    def test_check_all_methods_includes_rule(self):
        assert "check_pin1_markers" in DRCChecker.CHECK_ALL_METHODS

    def test_cli_category_registered(self):
        assert "pin1_marker" in check_cmd.CHECK_CATEGORIES

    @pytest.mark.parametrize("rule_id", [PIN1_MARKER_MISSING_RULE_ID, PIN1_MARKER_OBSCURED_RULE_ID])
    def test_rule_ids_classified_advisory(self, rule_id: str):
        assert DRCChecker.category_for_rule(rule_id) == DRCChecker.CATEGORY_ADVISORY


class TestCLI:
    def test_json_output(self, capsys):
        rc = check_cmd.main(
            [str(FIXTURE), "--only", "pin1_marker", "--drc-only", "--format", "json"]
        )
        data = json.loads(capsys.readouterr().out)
        assert rc == 0  # warnings never fail the plain gate
        got = [(v["rule_id"], tuple(v["items"])) for v in data["violations"]]
        assert got == _FIXTURE_EXPECTED
        assert all(v["severity"] == "warning" for v in data["violations"])

    def test_strict_blocks_on_warning(self, capsys):
        rc = check_cmd.main([str(FIXTURE), "--only", "pin1_marker", "--drc-only", "--strict"])
        capsys.readouterr()
        assert rc != 0

    def test_skip(self, capsys):
        check_cmd.main([str(FIXTURE), "--skip", "pin1_marker", "--drc-only", "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        assert not [v for v in data["violations"] if v["rule_id"].startswith("pin1_marker")]

    def test_waiver_suppresses_finding(self, capsys, tmp_path: Path):
        board = tmp_path / FIXTURE.name
        board.write_text(FIXTURE.read_text())
        (tmp_path / ".kct_waivers.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "waivers": [
                        {"rule": rid, "items": list(items), "reason": "test", "issue": "test"}
                        for rid, items in _FIXTURE_EXPECTED
                    ],
                }
            )
        )
        rc = check_cmd.main([str(board), "--only", "pin1_marker", "--drc-only", "--strict"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "WAIVED" in out
