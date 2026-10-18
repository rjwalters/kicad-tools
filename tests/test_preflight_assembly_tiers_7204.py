"""Assembly-capability preflight checks against PCBA service tiers (Issue #7204).

JLCPCB sells two PCBA services with different limits (Economic, Standard).
``kct export`` preflight now reports:

* ``bottom_side_in_cpl`` -- always WARN when bottom-side parts are placed,
  naming the tier that can do double-sided placement (+ a panelize note when
  the board is below that tier's minimum single-PCB size);
* ``assembly_package_limits`` -- chip package size and IC/BGA pitch against
  the selected tier: WARN when a costlier tier is needed, FAIL only when no
  tier the fab offers can place the part.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.export.preflight import (
    PreflightChecker,
    PreflightConfig,
    _requirements_for_footprint,
)
from kicad_tools.manufacturers import get_profile
from kicad_tools.manufacturers.jlcpcb import (
    JLCPCB_ASSEMBLY,
    JLCPCB_ASSEMBLY_ECONOMIC,
    JLCPCB_ASSEMBLY_STANDARD,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _smd_pads(layer: str, count: int = 2, pitch: float = 1.0, size=(0.6, 0.5)) -> str:
    cu = "F" if layer == "F.Cu" else "B"
    lines = []
    for i in range(count):
        x = (i - (count - 1) / 2) * pitch
        lines.append(
            f'    (pad "{i + 1}" smd rect (at {x:.4f} 0) (size {size[0]} {size[1]}) '
            f'(layers "{cu}.Cu" "{cu}.Paste" "{cu}.Mask") (net 0 ""))'
        )
    return "\n".join(lines)


def _fp(
    ref: str,
    lib_id: str,
    *,
    layer: str = "F.Cu",
    x: float = 10.0,
    attr: str = "smd",
    pads: str | None = None,
) -> str:
    pads = pads if pads is not None else _smd_pads(layer)
    if attr == "through_hole":
        pads = (
            '    (pad "1" thru_hole rect (at 0 0) (size 1.7 1.7) (drill 1.0) '
            '(layers "*.Cu" "*.Mask") (net 0 ""))'
        )
    return f"""  (footprint "{lib_id}"
    (layer "{layer}")
    (at {x} 25)
    (attr {attr})
    (fp_text reference "{ref}" (at 0 -1.5) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
    (fp_text value "V" (at 0 1.5) (layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))
{pads}
  )"""


def _pcb(tmp_path: Path, footprints: list[str], size=(100.0, 80.0)) -> Path:
    w, h = size
    content = f"""(kicad_pcb (version 20231014) (generator "pcbnew")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (gr_line (start 0 0) (end {w} 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start {w} 0) (end {w} {h}) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start {w} {h}) (end 0 {h}) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 0 {h}) (end 0 0) (layer "Edge.Cuts") (width 0.1))
{chr(10).join(footprints)}
)
"""
    path = tmp_path / "board.kicad_pcb"
    path.write_text(content)
    return path


def _run(
    tmp_path: Path,
    footprints: list[str],
    *,
    manufacturer: str = "jlcpcb",
    tier: str = "auto",
    size=(100.0, 80.0),
):
    checker = PreflightChecker(
        pcb_path=_pcb(tmp_path, footprints, size=size),
        manufacturer=manufacturer,
        config=PreflightConfig(skip_drc=True, skip_erc=True, assembly_tier=tier),
    )
    results = checker.run_all()
    by_name = {r.name: r for r in results}
    return checker, results, by_name


# ---------------------------------------------------------------------------
# Profile values (cited from jlcpcb.com/capabilities/pcb-assembly-capabilities)
# ---------------------------------------------------------------------------


class TestJlcpcbTierProfile:
    def test_economic_values(self):
        t = JLCPCB_ASSEMBLY_ECONOMIC
        assert t.tier_name == "Economic PCBA"
        assert t.min_bga_pitch_mm == 0.5
        assert t.min_component_pitch_mm == 0.4
        assert t.min_package == "0402"
        assert t.supports_double_sided is False
        assert t.requires_rails_and_fiducials is False
        assert "0201" not in t.supported_packages

    def test_standard_values(self):
        t = JLCPCB_ASSEMBLY_STANDARD
        assert t.tier_name == "Standard PCBA"
        assert t.min_bga_pitch_mm == 0.3
        assert t.min_component_pitch_mm == 0.35
        assert t.min_package == "01005"
        assert t.supports_double_sided is True
        assert t.min_board_size_mm == (70.0, 70.0)
        assert t.requires_rails_and_fiducials is True
        assert t.min_build_days == 4
        assert {"01005", "0201"} <= set(t.supported_packages)

    @pytest.mark.parametrize("mfr", ["jlcpcb", "jlcpcb-tier1"])
    def test_profile_tiers_cheapest_first_and_superset(self, mfr):
        profile = get_profile(mfr)
        assert [t.tier_name for t in profile.get_assembly_tiers()] == [
            "Economic PCBA",
            "Standard PCBA",
        ]
        # ``assembly`` stays the capability superset so kct mfr stays truthful.
        assert profile.assembly is JLCPCB_ASSEMBLY is JLCPCB_ASSEMBLY_STANDARD

    def test_untiered_profile_falls_back_to_single_assembly(self):
        profile = get_profile("pcbway")
        assert profile.assembly_tiers == []
        assert profile.get_assembly_tiers() == [profile.assembly]

    def test_pcb_only_profile_has_no_tiers(self):
        assert get_profile("oshpark").get_assembly_tiers() == []


# ---------------------------------------------------------------------------
# Lib-id parsing
# ---------------------------------------------------------------------------


class TestRequirementParsing:
    def test_chip_package(self):
        (req,) = _requirements_for_footprint("C1", "Capacitor_SMD:C_0201_0603Metric", ())
        assert req.kind == "package"
        assert req.label == "0201"
        assert req.value == pytest.approx(0.6)

    def test_chip_package_handsolder_suffix(self):
        (req,) = _requirements_for_footprint(
            "R1", "Resistor_SMD:R_0402_1005Metric_Pad0.72x0.64mm_HandSolder", ()
        )
        assert req.label == "0402"

    def test_ic_pitch(self):
        (req,) = _requirements_for_footprint("U1", "Package_QFP:LQFP-48_7x7mm_P0.5mm", ())
        assert (req.kind, req.value, req.measured) == ("ic_pitch", 0.5, False)

    def test_bga_pitch(self):
        (req,) = _requirements_for_footprint(
            "U2", "Package_BGA:BGA-64_5.0x5.0mm_Layout8x8_P0.5mm_Ball0.3mm", ()
        )
        assert (req.kind, req.value) == ("bga_pitch", 0.5)

    def test_connector_pitch_not_judged_as_ic(self):
        assert (
            _requirements_for_footprint(
                "J1", "Connector_FFC-FPC:Hirose_FH12-10S-0.5SH_1x10-1MP_P0.50mm_Horizontal", ()
            )
            == []
        )

    def test_sot23_without_pitch_token_has_no_requirement(self):
        assert _requirements_for_footprint("Q1", "Package_TO_SOT_SMD:SOT-23", ()) == []


# ---------------------------------------------------------------------------
# bottom_side_in_cpl
# ---------------------------------------------------------------------------


R0603 = "Resistor_SMD:R_0603_1608Metric"


class TestBottomSideInCpl:
    def test_top_only_ok(self, tmp_path):
        _, _, r = _run(tmp_path, [_fp("R1", R0603)])
        assert r["bottom_side_in_cpl"].status == "OK"

    def test_bottom_side_warns_naming_standard(self, tmp_path):
        fps = [_fp("R1", R0603)] + [
            _fp(f"C{i}", R0603, layer="B.Cu", x=10 + 3 * i) for i in range(1, 4)
        ]
        checker, results, r = _run(tmp_path, fps)
        res = r["bottom_side_in_cpl"]
        assert res.status == "WARN"
        assert res.message.startswith("3 bottom-side component(s): requires JLCPCB Standard PCBA")
        assert "double-sided" in res.message
        assert "edge rails + fiducials required" in res.message
        assert ">=4-day build" in res.message
        assert "panelize" not in res.message  # 100x80 mm >= 70x70 mm
        assert "C1, C2, C3" in res.details
        assert not checker.has_failures(results)

    def test_small_board_adds_panelize_note(self, tmp_path):
        fps = [_fp("C1", R0603, layer="B.Cu")]
        _, _, r = _run(tmp_path, fps, size=(50.0, 40.0))
        msg = r["bottom_side_in_cpl"].message
        assert "board is 50.0x40.0 mm, below Standard PCBA's 70x70 mm" in msg
        assert "kct panel" in msg

    def test_one_side_below_minimum_adds_note(self, tmp_path):
        _, _, r = _run(tmp_path, [_fp("C1", R0603, layer="B.Cu")], size=(120.0, 60.0))
        assert "panelize" in r["bottom_side_in_cpl"].message

    @pytest.mark.parametrize("tier", ["economic", "standard"])
    def test_stays_warn_for_explicit_tier(self, tmp_path, tier):
        _, _, r = _run(tmp_path, [_fp("C1", R0603, layer="B.Cu")], tier=tier)
        assert r["bottom_side_in_cpl"].status == "WARN"

    def test_dnp_and_pos_excluded_ignored(self, tmp_path):
        fps = [
            _fp("C1", R0603, layer="B.Cu", attr="smd dnp"),
            _fp("C2", R0603, layer="B.Cu", x=15, attr="smd exclude_from_pos_files"),
        ]
        _, _, r = _run(tmp_path, fps)
        assert r["bottom_side_in_cpl"].status == "OK"

    def test_auto_excluded_tht_ignored(self, tmp_path):
        fps = [
            _fp(
                "J1",
                "Connector_PinHeader_2.54mm:PinHeader_1x01_P2.54mm_Vertical",
                layer="B.Cu",
                attr="through_hole",
            )
        ]
        _, _, r = _run(tmp_path, fps)
        assert r["bottom_side_in_cpl"].status == "OK"

    def test_untiered_fab_has_no_tier_wording(self, tmp_path):
        _, _, r = _run(tmp_path, [_fp("C1", R0603, layer="B.Cu")], manufacturer="pcbway")
        res = r["bottom_side_in_cpl"]
        assert res.status == "WARN"
        assert "double-sided assembly" in res.message
        assert "Standard" not in res.message and "Economic" not in res.message


# ---------------------------------------------------------------------------
# assembly_package_limits
# ---------------------------------------------------------------------------


class TestAssemblyPackageLimits:
    def test_economic_fit_ok(self, tmp_path):
        fps = [_fp("R1", R0603), _fp("U1", "Package_QFP:LQFP-48_7x7mm_P0.5mm", x=20)]
        _, _, r = _run(tmp_path, fps)
        res = r["assembly_package_limits"]
        assert res.status == "OK"
        assert "fit JLCPCB Economic PCBA" in res.message

    def test_0201_warns_requires_standard(self, tmp_path):
        _, results, r = _run(tmp_path, [_fp("C1", "Capacitor_SMD:C_0201_0603Metric")])
        res = r["assembly_package_limits"]
        assert res.status == "WARN"
        assert "Cheapest JLCPCB tier that fits: Standard PCBA" in res.message
        assert "C1 package 0201" in res.details
        assert not PreflightChecker.has_failures(results)

    def test_0201_ok_when_standard_selected(self, tmp_path):
        _, _, r = _run(tmp_path, [_fp("C1", "Capacitor_SMD:C_0201_0603Metric")], tier="standard")
        res = r["assembly_package_limits"]
        assert res.status == "OK"
        assert "within JLCPCB Standard PCBA limits" in res.message

    def test_0201_explicit_economic_names_required_tier(self, tmp_path):
        _, _, r = _run(tmp_path, [_fp("C1", "Capacitor_SMD:C_0201_0603Metric")], tier="economic")
        res = r["assembly_package_limits"]
        assert res.status == "WARN"
        assert "beyond JLCPCB Economic PCBA limits; requires Standard PCBA" in res.message

    def test_01005_warns_not_fails(self, tmp_path):
        _, _, r = _run(tmp_path, [_fp("C1", "Capacitor_SMD:C_01005_0402Metric")])
        assert r["assembly_package_limits"].status == "WARN"

    def test_ic_pitch_035_warns(self, tmp_path):
        _, _, r = _run(tmp_path, [_fp("U1", "Package_QFP:LQFP-100_14x14mm_P0.35mm")])
        res = r["assembly_package_limits"]
        assert res.status == "WARN"
        assert "U1 pin pitch 0.35 mm" in res.details

    def test_ic_pitch_below_every_tier_fails(self, tmp_path):
        _, results, r = _run(tmp_path, [_fp("U1", "Package_QFP:LQFP-100_14x14mm_P0.3mm")])
        assert r["assembly_package_limits"].status == "FAIL"
        assert PreflightChecker.has_failures(results)

    def test_bga_pitch_04_warns(self, tmp_path):
        _, _, r = _run(tmp_path, [_fp("U1", "Package_BGA:BGA-100_P0.4mm")])
        assert r["assembly_package_limits"].status == "WARN"

    def test_bga_pitch_025_fails(self, tmp_path):
        _, _, r = _run(tmp_path, [_fp("U1", "Package_BGA:WLCSP-20_P0.25mm")])
        res = r["assembly_package_limits"]
        assert res.status == "FAIL"
        assert "beyond JLCPCB Standard PCBA limits" in res.message
        assert "U1 BGA pitch 0.25 mm" in res.details

    def test_bga_pitch_025_fails_even_with_standard(self, tmp_path):
        _, _, r = _run(tmp_path, [_fp("U1", "Package_BGA:BGA-100_P0.25mm")], tier="standard")
        assert r["assembly_package_limits"].status == "FAIL"

    def test_measured_pitch_caps_at_warn(self, tmp_path):
        pads = _smd_pads("F.Cu", count=8, pitch=0.3, size=(0.15, 0.8))
        _, _, r = _run(tmp_path, [_fp("U1", "MyLib:CustomIC", pads=pads)])
        res = r["assembly_package_limits"]
        assert res.status == "WARN"
        assert "(measured)" in res.details

    def test_measured_pitch_ignores_paste_only_and_few_pads(self, tmp_path):
        pads = _smd_pads("F.Cu", count=3, pitch=0.2, size=(0.15, 0.8))
        _, _, r = _run(tmp_path, [_fp("U1", "MyLib:Tiny3Pad", pads=pads)])
        assert r["assembly_package_limits"].status == "OK"

    def test_dnp_part_not_judged(self, tmp_path):
        fps = [_fp("U1", "Package_BGA:BGA-100_P0.25mm", attr="smd dnp")]
        _, _, r = _run(tmp_path, fps)
        assert r["assembly_package_limits"].status == "OK"

    def test_untiered_fab_uses_single_assembly(self, tmp_path):
        # PCBway: single service, min IC pitch 0.35 mm, no tier wording.
        _, _, r = _run(
            tmp_path,
            [_fp("U1", "Package_QFP:LQFP-100_14x14mm_P0.3mm")],
            manufacturer="pcbway",
        )
        res = r["assembly_package_limits"]
        assert res.status == "FAIL"
        assert "assembly limits" in res.message
        assert "Economic" not in res.message and "Standard" not in res.message

    def test_untiered_fab_ignores_tier_flag(self, tmp_path):
        _, _, r = _run(tmp_path, [_fp("R1", R0603)], manufacturer="pcbway", tier="economic")
        res = r["assembly_package_limits"]
        assert res.status == "OK"
        assert "single assembly service" in res.details

    def test_mil_rounded_limit_admits_half_mm_qfp(self, tmp_path):
        # FlashPCB quotes 20 mil (0.508 mm); a 0.5 mm QFP is not a FAIL.
        _, _, r = _run(
            tmp_path,
            [_fp("U1", "Package_QFP:LQFP-48_7x7mm_P0.5mm")],
            manufacturer="flashpcb",
        )
        assert r["assembly_package_limits"].status == "OK"


class TestSkippedForPcbOnlyFab:
    def test_assembly_none_skips_rows(self, tmp_path):
        fps = [_fp("C1", "Capacitor_SMD:C_0201_0603Metric", layer="B.Cu")]
        _, _, r = _run(tmp_path, fps, manufacturer="oshpark")
        assert "bottom_side_in_cpl" not in r
        assert "assembly_package_limits" not in r


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------


class TestAssemblyTierFlag:
    def test_export_cmd_parser(self):
        from kicad_tools.cli.export_cmd import build_parser

        args = build_parser().parse_args(["board.kicad_pcb", "--assembly-tier", "economic"])
        assert args.assembly_tier == "economic"
        assert build_parser().parse_args(["board.kicad_pcb"]).assembly_tier == "auto"

    def test_export_cmd_rejects_unknown_tier(self):
        from kicad_tools.cli.export_cmd import build_parser

        with pytest.raises(SystemExit):
            build_parser().parse_args(["board.kicad_pcb", "--assembly-tier", "premium"])

    def test_run_export_plumbs_tier_into_preflight_config(self, tmp_path, monkeypatch):
        from kicad_tools.cli import export_cmd
        from kicad_tools.export import manufacturing

        captured = {}

        class _Stop(Exception):
            pass

        def _fake_init(self, pcb_path, schematic_path=None, manufacturer="jlcpcb", config=None):
            captured["config"] = config
            raise _Stop

        monkeypatch.setattr(manufacturing.ManufacturingPackage, "__init__", _fake_init)
        pcb = _pcb(tmp_path, [_fp("R1", R0603)])
        args = export_cmd.build_parser().parse_args([str(pcb), "--assembly-tier", "standard"])
        with pytest.raises(_Stop):
            export_cmd.run_export(args)
        assert captured["config"].preflight.assembly_tier == "standard"

    def test_top_level_kct_export_forwards_flag(self, monkeypatch):
        from kicad_tools.cli import main

        seen = {}

        def _fake_main(argv=None):
            seen["argv"] = argv
            return 0

        monkeypatch.setattr("kicad_tools.cli.export_cmd.main", _fake_main)
        main(["export", "board.kicad_pcb", "--assembly-tier", "standard"])
        argv = seen["argv"]
        assert argv[argv.index("--assembly-tier") + 1] == "standard"


# ---------------------------------------------------------------------------
# Design report: "Bottom-side parts" assembly note
# ---------------------------------------------------------------------------


def _mock_pcb(specs):
    from unittest.mock import MagicMock

    pcb = MagicMock()
    fps = []
    for ref, name, layer, dnp in specs:
        fp = MagicMock()
        fp.name = name
        fp.reference = ref
        fp.layer = layer
        fp.dnp = dnp
        fp.exclude_from_pos_files = False
        fps.append(fp)
    pcb.footprints = fps
    return pcb


class TestReportBottomSideNote:
    def test_bottom_side_only_board_still_gets_notes(self, tmp_path):
        """Bottom-side parts alone must not hit the all-zero early return."""
        from kicad_tools.report.collector import ReportDataCollector

        collector = ReportDataCollector(tmp_path / "dummy.kicad_pcb")
        pcb = _mock_pcb(
            [
                ("R1", R0603, "F.Cu", False),
                ("C1", "Capacitor_SMD:C_0603_1608Metric", "B.Cu", False),
                ("C2", "Capacitor_SMD:C_0603_1608Metric", "B.Cu", False),
                ("C3", "Capacitor_SMD:C_0603_1608Metric", "B.Cu", True),  # DNP
            ]
        )
        notes = collector._extract_assembly_notes(pcb)
        assert notes is not None
        assert notes["bottom_side_count"] == 2
        assert notes["bottom_side_parts"] == ["C1", "C2"]
        assert "2 bottom-side components" in notes["summary"]

    def test_top_only_simple_board_still_none(self, tmp_path):
        from kicad_tools.report.collector import ReportDataCollector

        collector = ReportDataCollector(tmp_path / "dummy.kicad_pcb")
        pcb = _mock_pcb([("R1", R0603, "F.Cu", False)])
        assert collector._extract_assembly_notes(pcb) is None

    def test_template_renders_bottom_side_line(self, tmp_path):
        from kicad_tools.report.generator import ReportGenerator
        from tests.test_report_generator import _full_data

        data = _full_data(
            assembly_notes={
                "fine_pitch_count": 0,
                "fine_pitch_parts": [],
                "thermal_pad_count": 0,
                "polarized_count": 0,
                "bottom_side_count": 2,
                "bottom_side_parts": ["C1", "C2"],
                "summary": "2 bottom-side components",
            },
        )
        content = ReportGenerator().generate(data, tmp_path).read_text(encoding="utf-8")
        assert "- **Bottom-side parts**: 2 (C1, C2) -- double-sided assembly" in content
