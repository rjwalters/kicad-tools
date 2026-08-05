"""Tests for the connector mating / edge-access rule (Issue #4613).

Covers the two-tier design in ``validate/rules/connector_access.py``:

* Tier A -- default-on ``connector_edge_access`` warning for rigid-plug
  panel-entry families (audio / USB / barrel jack / RJ / card edge) too
  far from every ``Edge.Cuts`` segment for a plug to reach.
* Tier B -- ``connector_edge_distance`` info inventory rows for every
  ``Connector_*`` footprint, gated by ``emit_inventory``.

Unit tests build in-memory synthetic ``PCB(SExp(...))`` fixtures
(mirroring ``tests/test_validate_courtyard_overlap.py``); CLI tests write
a minimal ``.kicad_pcb`` and drive ``check_cmd.main``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli import check_cmd
from kicad_tools.schema.pcb import (
    PCB,
    Footprint,
    FootprintGraphic,
    GraphicLine,
    Pad,
)
from kicad_tools.sexp import SExp
from kicad_tools.validate.checker import DRCChecker
from kicad_tools.validate.rules.connector_access import (
    CONNECTOR_EDGE_ACCESS_MAX_MM,
    CONNECTOR_EDGE_ACCESS_RULE_ID,
    CONNECTOR_EDGE_DISTANCE_RULE_ID,
    ConnectorEdgeAccessRule,
)
from kicad_tools.validate.rules.waivers import apply_waivers, waivers_from_dict

pytest.importorskip("shapely")


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

# The shipped-incident footprint id (motivating case for the rule).
INCIDENT_JACK = "Connector_Audio:Jack_3.5mm_CUI_SJ-3523-SMT_Horizontal"


def _crtyd_rect(
    *,
    start: tuple[float, float],
    end: tuple[float, float],
    layer: str = "F.CrtYd",
) -> FootprintGraphic:
    return FootprintGraphic(
        graphic_type="rect",
        layer=layer,
        stroke_width=0.05,
        start=start,
        end=end,
    )


def _make_footprint(
    *,
    name: str,
    reference: str = "J1",
    position: tuple[float, float] = (0.0, 0.0),
    rotation: float = 0.0,
    layer: str = "F.Cu",
    pads: list[Pad] | None = None,
    graphics: list[FootprintGraphic] | None = None,
) -> Footprint:
    return Footprint(
        name=name,
        layer=layer,
        position=position,
        rotation=rotation,
        reference=reference,
        value="TEST",
        pads=pads or [],
        texts=[],
        graphics=graphics or [],
    )


def _smd_pad(number: str, position: tuple[float, float], size=(1.0, 1.0)) -> Pad:
    return Pad(
        number=number,
        type="smd",
        shape="rect",
        position=position,
        size=size,
        layers=["F.Cu"],
    )


def _pcb_with_outline(
    *footprints: Footprint,
    outline: tuple[float, float, float, float] | None = (0.0, 0.0, 100.0, 100.0),
    extra_edge_segments: list[tuple[tuple[float, float], tuple[float, float]]] | None = None,
) -> PCB:
    """Build an in-memory PCB with a rectangular Edge.Cuts outline.

    ``outline`` is (x_min, y_min, x_max, y_max); ``None`` omits the
    outline entirely.  ``extra_edge_segments`` adds internal-cutout
    Edge.Cuts segments.
    """
    pcb = PCB(SExp(name="kicad_pcb"))
    if outline is not None:
        x0, y0, x1, y1 = outline
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        for i in range(4):
            pcb._graphic_lines.append(
                GraphicLine(
                    start=corners[i],
                    end=corners[(i + 1) % 4],
                    layer="Edge.Cuts",
                    width=0.1,
                )
            )
    for seg_start, seg_end in extra_edge_segments or []:
        pcb._graphic_lines.append(
            GraphicLine(start=seg_start, end=seg_end, layer="Edge.Cuts", width=0.1)
        )
    for fp in footprints:
        pcb._footprints.append(fp)
    return pcb


def _run(pcb: PCB, *, emit_inventory: bool = False):
    return ConnectorEdgeAccessRule(emit_inventory=emit_inventory).check(pcb, None)


def _warnings(results):
    return results.filter_by_rule(CONNECTOR_EDGE_ACCESS_RULE_ID)


def _infos(results):
    return results.filter_by_rule(CONNECTOR_EDGE_DISTANCE_RULE_ID)


# ---------------------------------------------------------------------------
# Tier A: the incident geometry and the flush/overhang counterpart
# ---------------------------------------------------------------------------


class TestIncidentGeometry:
    """The shipped defect: 65x75mm outline, jack courtyard 10.66mm inside."""

    def _incident_pcb(self) -> PCB:
        # Board outline X 116.50..181.50, Y 77.00..152.00 (65 x 75 mm).
        # J3 at (151.123, 132.340) rot 0; courtyard X 145.52..156.72,
        # Y 123.34..141.34 (11.20 x 18.00 mm) -> 10.66mm from the nearest
        # (bottom, y=152) edge.
        jack = _make_footprint(
            name=INCIDENT_JACK,
            reference="J3",
            position=(151.123, 132.340),
            graphics=[_crtyd_rect(start=(-5.603, -9.0), end=(5.597, 9.0))],
        )
        return _pcb_with_outline(jack, outline=(116.5, 77.0, 181.5, 152.0))

    def test_incident_jack_triggers_warning(self):
        results = _run(self._incident_pcb())
        warnings = _warnings(results)
        assert len(warnings) == 1
        v = warnings[0]
        assert v.severity == "warning"
        assert v.items == ("J3",)
        assert v.actual_value == pytest.approx(10.66, abs=0.01)
        assert v.required_value == CONNECTOR_EDGE_ACCESS_MAX_MM
        assert "J3" in v.message
        assert "10.66" in v.message

    def test_incident_warning_is_not_an_error(self):
        """Warning severity: plain exit gate (is_error) is unaffected."""
        results = _run(self._incident_pcb())
        assert results.error_count == 0
        assert results.warning_count == 1

    def test_same_jack_flush_at_edge_no_warning(self):
        """The same footprint with its courtyard touching the edge passes."""
        jack = _make_footprint(
            name=INCIDENT_JACK,
            reference="J3",
            # Courtyard spans y in [position-9, position+9]; put the lower
            # edge exactly on the outline (y=152).
            position=(151.123, 143.0),
            graphics=[_crtyd_rect(start=(-5.603, -9.0), end=(5.597, 9.0))],
        )
        pcb = _pcb_with_outline(jack, outline=(116.5, 77.0, 181.5, 152.0))
        assert _warnings(_run(pcb)) == []

    def test_same_jack_overhanging_edge_no_warning(self):
        """A courtyard straddling the outline measures 0.0mm (no warning)."""
        jack = _make_footprint(
            name=INCIDENT_JACK,
            reference="J3",
            position=(151.123, 150.0),  # courtyard y 141..159 straddles y=152
            graphics=[_crtyd_rect(start=(-5.603, -9.0), end=(5.597, 9.0))],
        )
        pcb = _pcb_with_outline(jack, outline=(116.5, 77.0, 181.5, 152.0))
        results = _run(pcb, emit_inventory=True)
        assert _warnings(results) == []
        (info,) = _infos(results)
        assert info.actual_value == pytest.approx(0.0, abs=1e-9)

    def test_just_inside_threshold_no_warning(self):
        """A connector exactly at the 3.0mm threshold does not warn."""
        jack = _make_footprint(
            name=INCIDENT_JACK,
            reference="J3",
            # Lower courtyard edge at y = 149.0 -> 3.0mm from y=152.
            position=(151.123, 140.0),
            graphics=[_crtyd_rect(start=(-5.603, -9.0), end=(5.597, 9.0))],
        )
        pcb = _pcb_with_outline(jack, outline=(116.5, 77.0, 181.5, 152.0))
        assert _warnings(_run(pcb)) == []


# ---------------------------------------------------------------------------
# Tier A: family classification (false-positive guards)
# ---------------------------------------------------------------------------

_MID_BOARD = (50.0, 50.0)
_CENTERED_CRTYD = [_crtyd_rect(start=(-2.0, -2.0), end=(2.0, 2.0))]


class TestFamilyClassification:
    def _mid_board_fp(self, name: str, **kwargs) -> Footprint:
        return _make_footprint(
            name=name,
            position=_MID_BOARD,
            graphics=list(_CENTERED_CRTYD),
            **kwargs,
        )

    @pytest.mark.parametrize(
        "name",
        [
            # Flexible-cable / board-stacking families: deliberately excluded.
            "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
            "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Horizontal",
            "Connector_PinSocket_2.54mm:PinSocket_1x04_P2.54mm_Vertical",
            "Connector_JST:JST_XH_B2B-XH-A_1x02_P2.50mm_Vertical",
            "Connector_JST:JST_PH_S2B-PH-K_1x02_P2.00mm_Horizontal",
            "Connector_Molex:Molex_PicoBlade_53398-0271_1x02-1MP_P1.25mm_Vertical",
            "Connector_FFC-FPC:TE_1-84952-4_1x04-1MP_P1.0mm_Horizontal",
            "Connector_IDC:IDC-Header_2x05_P2.54mm_Vertical",
            "Connector_Wire:SolderWirePad_1x01_SMD_1x2mm",
            "Connector_Generic:Conn_01x02",
            # Tier-A libraries but _Vertical (mates upward): excluded.
            "Connector_Audio:Jack_3.5mm_QingPu_WQP-PJ398SM_Vertical_CircularHoles",
            "Connector_USB:USB_A_Receptacle_Vertical",
            "Connector_RJ:RJ45_Amphenol_RJHSE538X_Vertical",
            # Tier-A suffix families WITHOUT the _Horizontal signal: excluded
            # (precision-first -- see the curator design).
            "Connector_USB:USB_C_Receptacle_GCT_USB4105",
            # Non-Tier-A connector libraries: inventory only, no warning.
            "Connector_Video:HDMI_A_Receptacle",
            "Connector_PCIE:PCIE_Mini_Edge",
        ],
    )
    def test_mid_board_non_tier_a_no_warning(self, name: str):
        pcb = _pcb_with_outline(self._mid_board_fp(name))
        assert _warnings(_run(pcb)) == []

    @pytest.mark.parametrize(
        "name",
        [
            # _Horizontal-suffix families.
            INCIDENT_JACK,
            "Connector_USB:USB_Micro-B_Molex-105017-0001_Horizontal",
            "Connector_RJ:RJ45_Amphenol_54602-x08_Horizontal",
            # Always-horizontal families (with and without the suffix).
            "Connector_BarrelJack:BarrelJack_Horizontal",
            "Connector_BarrelJack:BarrelJack_CUI_PJ-063AH",
            "Connector_Card:microSD_HC_Hirose_DM3D-SF",
        ],
    )
    def test_mid_board_tier_a_warns(self, name: str):
        pcb = _pcb_with_outline(self._mid_board_fp(name))
        warnings = _warnings(_run(pcb))
        assert len(warnings) == 1
        assert warnings[0].items == ("J1",)

    def test_non_connector_footprint_ignored_entirely(self):
        fp = self._mid_board_fp("Resistor_SMD:R_0402_1005Metric")
        results = _run(_pcb_with_outline(fp), emit_inventory=True)
        assert _warnings(results) == []
        assert _infos(results) == []


# ---------------------------------------------------------------------------
# Rotation correctness (KiCad negated-rotation transform, #3739)
# ---------------------------------------------------------------------------


class TestRotationCorrectness:
    """An asymmetric courtyard arm distinguishes the transform's sign.

    Local courtyard: a bar from (6, -1) to (46, 1) -- an arm reaching
    local +x.  Footprint at (50, 90) on a 100x100 board.  Under KiCad's
    negated-rotation convention:

    * rot 0   -> arm to world +x: nearest edge 100-96 = 4mm  -> warns
    * rot 90  -> arm to world -y: nearest edge 100-84 = 16mm -> warns
    * rot 180 -> arm to world -x: nearest edge  4-0  = 4mm  -> warns
    * rot 270 -> arm to world +y: overhangs y=100  -> 0mm    -> passes

    A hand-rolled non-negated transform would swap the 90/270 outcomes.
    """

    _ARM = [_crtyd_rect(start=(6.0, -1.0), end=(46.0, 1.0))]

    def _fp(self, rotation: float) -> Footprint:
        return _make_footprint(
            name="Connector_BarrelJack:BarrelJack_Horizontal",
            position=(50.0, 90.0),
            rotation=rotation,
            graphics=list(self._ARM),
        )

    @pytest.mark.parametrize(
        ("rotation", "expected_distance"),
        [(0.0, 4.0), (90.0, 16.0), (180.0, 4.0)],
    )
    def test_rotated_arm_warns_with_expected_distance(
        self, rotation: float, expected_distance: float
    ):
        results = _run(_pcb_with_outline(self._fp(rotation)))
        warnings = _warnings(results)
        assert len(warnings) == 1
        assert warnings[0].actual_value == pytest.approx(expected_distance, abs=1e-6)

    def test_rotation_270_overhangs_edge_no_warning(self):
        results = _run(_pcb_with_outline(self._fp(270.0)))
        assert _warnings(results) == []


# ---------------------------------------------------------------------------
# Geometry fallbacks and edge cases
# ---------------------------------------------------------------------------


class TestGeometryFallbacks:
    def test_no_courtyard_falls_back_to_pad_bbox(self):
        """No CrtYd geometry: the pad bounding box is measured (no crash,
        no silent skip)."""
        fp = _make_footprint(
            name="Connector_BarrelJack:BarrelJack_Horizontal",
            position=_MID_BOARD,
            pads=[_smd_pad("1", (-2.0, 0.0)), _smd_pad("2", (2.0, 0.0))],
        )
        results = _run(_pcb_with_outline(fp), emit_inventory=True)
        warnings = _warnings(results)
        assert len(warnings) == 1
        # Pad bbox spans x 47.5..52.5 (centers +/-2, half-size 0.5),
        # y 49.5..50.5 -> nearest edge 47.5mm.
        assert warnings[0].actual_value == pytest.approx(47.5, abs=1e-6)

    def test_no_courtyard_no_pads_uses_origin_point(self):
        """Courtyard-less, pad-less footprint: origin point, no crash."""
        fp = _make_footprint(
            name="Connector_BarrelJack:BarrelJack_Horizontal",
            position=(50.0, 99.0),
        )
        results = _run(_pcb_with_outline(fp))
        assert _warnings(results) == []  # 1mm from the y=100 edge

    def test_back_side_courtyard_resolves(self):
        """A B.Cu connector with only a B.CrtYd is measured correctly."""
        fp = _make_footprint(
            name="Connector_BarrelJack:BarrelJack_Horizontal",
            position=_MID_BOARD,
            layer="B.Cu",
            graphics=[_crtyd_rect(start=(-2.0, -2.0), end=(2.0, 2.0), layer="B.CrtYd")],
        )
        results = _run(_pcb_with_outline(fp))
        warnings = _warnings(results)
        assert len(warnings) == 1
        assert warnings[0].actual_value == pytest.approx(48.0, abs=1e-6)

    def test_no_board_outline_returns_empty(self):
        """No Edge.Cuts: empty results (mirrors EdgeClearanceRule)."""
        fp = _make_footprint(
            name="Connector_BarrelJack:BarrelJack_Horizontal",
            position=_MID_BOARD,
            graphics=list(_CENTERED_CRTYD),
        )
        pcb = _pcb_with_outline(fp, outline=None)
        results = _run(pcb, emit_inventory=True)
        assert results.violations == []

    def test_internal_cutout_provides_edge_access(self):
        """Internal cutouts are Edge.Cuts too: a connector mating through
        a panel cutout passes."""
        fp = _make_footprint(
            name="Connector_BarrelJack:BarrelJack_Horizontal",
            position=_MID_BOARD,
            graphics=list(_CENTERED_CRTYD),
        )
        # A cutout rectangle right next to the connector (x 53..60).
        cutout = [
            ((53.0, 45.0), (60.0, 45.0)),
            ((60.0, 45.0), (60.0, 55.0)),
            ((60.0, 55.0), (53.0, 55.0)),
            ((53.0, 55.0), (53.0, 45.0)),
        ]
        pcb = _pcb_with_outline(fp, extra_edge_segments=cutout)
        assert _warnings(_run(pcb)) == []

    def test_multi_segment_polyline_outline(self):
        """An L-shaped (6-segment) outline works, not just a rect."""
        fp = _make_footprint(
            name="Connector_BarrelJack:BarrelJack_Horizontal",
            position=(25.0, 75.0),
            graphics=list(_CENTERED_CRTYD),
        )
        pcb = PCB(SExp(name="kicad_pcb"))
        # L-shape: 100x100 with the top-right 50x50 quadrant removed.
        corners = [(0, 0), (50, 0), (50, 50), (100, 50), (100, 100), (0, 100)]
        for i in range(len(corners)):
            pcb._graphic_lines.append(
                GraphicLine(
                    start=corners[i],
                    end=corners[(i + 1) % len(corners)],
                    layer="Edge.Cuts",
                    width=0.1,
                )
            )
        pcb._footprints.append(fp)
        results = _run(pcb)
        warnings = _warnings(results)
        assert len(warnings) == 1
        # Courtyard x 23..27, y 73..77 -> nearest edge is x=0 at 23mm.
        assert warnings[0].actual_value == pytest.approx(23.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Tier B: inventory gating
# ---------------------------------------------------------------------------


class TestInventory:
    def _two_connector_pcb(self) -> PCB:
        jack = _make_footprint(
            name=INCIDENT_JACK,
            reference="J1",
            position=_MID_BOARD,
            graphics=list(_CENTERED_CRTYD),
        )
        header = _make_footprint(
            name="Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
            reference="J2",
            position=(20.0, 20.0),
            graphics=[_crtyd_rect(start=(-1.0, -1.0), end=(1.0, 4.0))],
        )
        return _pcb_with_outline(jack, header)

    def test_inventory_rows_for_every_connector(self):
        """Every Connector_* footprint gets an info row -- including the
        pin header the warning tier deliberately ignores."""
        results = _run(self._two_connector_pcb(), emit_inventory=True)
        infos = _infos(results)
        assert {v.items for v in infos} == {("J1",), ("J2",)}
        for v in infos:
            assert v.severity == "info"
            assert v.actual_value is not None and v.actual_value > 0

    def test_inventory_distance_value(self):
        results = _run(self._two_connector_pcb(), emit_inventory=True)
        by_ref = {v.items[0]: v for v in _infos(results)}
        # J1 courtyard 48mm from every edge; J2 courtyard nearest edge 19mm.
        assert by_ref["J1"].actual_value == pytest.approx(48.0, abs=1e-6)
        assert by_ref["J2"].actual_value == pytest.approx(19.0, abs=1e-6)

    def test_inventory_off_by_default(self):
        results = _run(self._two_connector_pcb(), emit_inventory=False)
        assert _infos(results) == []
        # The Tier-A warning still fires regardless of the inventory gate.
        assert len(_warnings(results)) == 1


# ---------------------------------------------------------------------------
# Waivers (.kct_waivers.json, Issue #4417)
# ---------------------------------------------------------------------------


class TestWaivers:
    def test_waiver_by_reference_suppresses_finding(self):
        jack = _make_footprint(
            name=INCIDENT_JACK,
            reference="J3",
            position=_MID_BOARD,
            graphics=list(_CENTERED_CRTYD),
        )
        results = _run(_pcb_with_outline(jack))
        assert results.warning_count == 1

        waivers = waivers_from_dict(
            {
                "version": 2,
                "waivers": [
                    {
                        "rule": CONNECTOR_EDGE_ACCESS_RULE_ID,
                        "items": ["J3"],
                        "reason": "internal mezzanine jack, mates through enclosure",
                        "issue": "test#4613",
                    }
                ],
            }
        )
        apply_waivers(results, waivers)
        assert results.warning_count == 0
        waived = [v for v in results.violations if v.is_waived]
        assert len(waived) == 1
        assert waived[0].rule_id == CONNECTOR_EDGE_ACCESS_RULE_ID
        assert waived[0].items == ("J3",)


# ---------------------------------------------------------------------------
# Checker wiring / categories
# ---------------------------------------------------------------------------


class TestCheckerWiring:
    def test_check_all_methods_includes_connector_access(self):
        assert "check_connector_access" in DRCChecker.CHECK_ALL_METHODS

    def test_cli_category_registered(self):
        assert "connector_access" in check_cmd.CHECK_CATEGORIES

    def test_both_rule_ids_classified_advisory(self):
        """Required: category_for_rule defaults unknown ids to the
        fab-blocking Manufacturing bucket."""
        assert (
            DRCChecker.category_for_rule(CONNECTOR_EDGE_ACCESS_RULE_ID)
            == DRCChecker.CATEGORY_ADVISORY
        )
        assert (
            DRCChecker.category_for_rule(CONNECTOR_EDGE_DISTANCE_RULE_ID)
            == DRCChecker.CATEGORY_ADVISORY
        )

    def test_checker_gates_inventory_on_measurement_flags(self):
        """The checker emits the inventory only under the measurement /
        verbose gate (same gate as the skew rules)."""
        pcb = PCB.create(width=100.0, height=100.0, layers=2)
        jack = _make_footprint(
            name=INCIDENT_JACK,
            reference="J1",
            position=_MID_BOARD,
            graphics=list(_CENTERED_CRTYD),
        )
        pcb._footprints.append(jack)

        quiet = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = quiet.check_connector_access()
        assert results.filter_by_rule(CONNECTOR_EDGE_DISTANCE_RULE_ID) == []
        assert len(results.filter_by_rule(CONNECTOR_EDGE_ACCESS_RULE_ID)) == 1

        measuring = DRCChecker(pcb, manufacturer="jlcpcb", layers=2, emit_measurements=True)
        results = measuring.check_connector_access()
        assert len(results.filter_by_rule(CONNECTOR_EDGE_DISTANCE_RULE_ID)) == 1


# ---------------------------------------------------------------------------
# CLI-level behaviour (--only / --skip / JSON / exit codes)
# ---------------------------------------------------------------------------

# A minimal board file: 50x50mm outline with a mid-board horizontal audio
# jack (courtyard 4x4mm at center -> 23mm from every edge) and a mid-board
# vertical pin header (must NOT warn).
CLI_TEST_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (gr_rect (start 100 100) (end 150 150)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "Connector_Audio:Jack_3.5mm_CUI_SJ-3523-SMT_Horizontal"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000100")
    (at 125 125)
    (property "Reference" "J1" (at 0 -3.5 0) (layer "F.SilkS")
      (uuid "00000000-0000-0000-0000-000000000101"))
    (fp_rect (start -2 -2) (end 2 2)
      (stroke (width 0.05) (type default))
      (fill none)
      (layer "F.CrtYd")
    )
    (pad "1" smd rect (at -1 0) (size 1.0 1.0) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
  )
  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000200")
    (at 135 135)
    (property "Reference" "J2" (at 0 -2.5 0) (layer "F.SilkS")
      (uuid "00000000-0000-0000-0000-000000000201"))
    (fp_rect (start -1.3 -1.3) (end 1.3 3.8)
      (stroke (width 0.05) (type default))
      (fill none)
      (layer "F.CrtYd")
    )
    (pad "1" thru_hole rect (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net 0 ""))
    (pad "2" thru_hole oval (at 0 2.54) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net 0 ""))
  )
)
"""


@pytest.fixture
def connector_pcb(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "connector_access.kicad_pcb"
    pcb_file.write_text(CLI_TEST_PCB)
    return pcb_file


class TestCLI:
    def test_only_connector_access_warns_and_exits_zero(self, connector_pcb: Path, capsys):
        rc = check_cmd.main([str(connector_pcb), "--only", "connector_access", "--drc-only"])
        captured = capsys.readouterr()
        assert rc == 0  # warnings never fail the plain gate
        assert CONNECTOR_EDGE_ACCESS_RULE_ID in captured.out
        assert "J1" in captured.out
        # The vertical pin header must not be flagged by the warning tier.
        assert captured.out.count(CONNECTOR_EDGE_ACCESS_RULE_ID + ":") <= 1

    def test_strict_blocks_on_warning(self, connector_pcb: Path, capsys):
        rc = check_cmd.main(
            [str(connector_pcb), "--only", "connector_access", "--drc-only", "--strict"]
        )
        capsys.readouterr()
        assert rc != 0

    def test_skip_connector_access(self, connector_pcb: Path, capsys):
        rc = check_cmd.main([str(connector_pcb), "--skip", "connector_access", "--drc-only"])
        captured = capsys.readouterr()
        assert rc == 0
        assert CONNECTOR_EDGE_ACCESS_RULE_ID not in captured.out

    def test_json_includes_warning_and_inventory(self, connector_pcb: Path, capsys):
        """JSON output carries both tiers with no emitter changes: the
        warning finding plus one connector_edge_distance row per
        Connector_* footprint (the CLI always builds the checker with
        emit_measurements=True)."""
        rc = check_cmd.main(
            [
                str(connector_pcb),
                "--only",
                "connector_access",
                "--drc-only",
                "--format",
                "json",
            ]
        )
        captured = capsys.readouterr()
        assert rc == 0
        data = json.loads(captured.out)
        by_rule: dict[str, list] = {}
        for v in data["violations"]:
            by_rule.setdefault(v["rule_id"], []).append(v)

        warnings = by_rule.get(CONNECTOR_EDGE_ACCESS_RULE_ID, [])
        assert len(warnings) == 1
        assert warnings[0]["items"] == ["J1"]
        assert warnings[0]["severity"] == "warning"
        assert warnings[0]["actual_value"] == pytest.approx(23.0, abs=1e-6)
        assert warnings[0]["required_value"] == CONNECTOR_EDGE_ACCESS_MAX_MM

        inventory = by_rule.get(CONNECTOR_EDGE_DISTANCE_RULE_ID, [])
        assert {tuple(v["items"]) for v in inventory} == {("J1",), ("J2",)}
        for v in inventory:
            assert v["severity"] == "info"
            assert v["actual_value"] is not None

    def test_waiver_sidecar_suppresses_cli_warning(
        self, connector_pcb: Path, capsys, tmp_path: Path
    ):
        (tmp_path / ".kct_waivers.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "waivers": [
                        {
                            "rule": CONNECTOR_EDGE_ACCESS_RULE_ID,
                            "items": ["J1"],
                            "reason": "deliberate internal jack for factory test rig",
                            "issue": "test#4613",
                        }
                    ],
                }
            )
        )
        rc = check_cmd.main(
            [str(connector_pcb), "--only", "connector_access", "--drc-only", "--strict"]
        )
        captured = capsys.readouterr()
        # Waived: even --strict passes, and the finding renders as WAIVED.
        assert rc == 0
        assert "WAIVED" in captured.out
