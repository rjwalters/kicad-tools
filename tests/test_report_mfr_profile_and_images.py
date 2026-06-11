"""Tests for issue #3497: manufacturing report DRC profile threading and
per-layer/assembly report images.

Covers:

- ``get_fab_family``: capability tiers resolve to the parent fab family
  for export-format selection (BOM/CPL/Gerber/LCSC), while DRC keeps the
  full profile ID.
- Profile threading: the export target's ``--mfr`` flows unchanged into
  the report data collector -> ManufacturingAudit -> DRCChecker chain, so
  the report's DRC section runs against the same profile as
  ``kct check --mfr <profile>``.
- pad_grid policy threading: the audit opts into the ``kct check`` CLI's
  auto-derive tolerance (issue #3061) so audit/report warning counts
  cannot drift from the CLI gate.
- Image emission: per-copper-layer figure entries, template rendering of
  the Copper Layers section, and promotion of report figures into the
  manufacturing package's ``images/`` directory with rewritten markdown
  refs and manifest inclusion.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.manufacturers import get_fab_family

# ---------------------------------------------------------------------------
# get_fab_family
# ---------------------------------------------------------------------------


class TestGetFabFamily:
    def test_tier1_maps_to_jlcpcb(self):
        assert get_fab_family("jlcpcb-tier1") == "jlcpcb"

    def test_tier1_aliases_map_to_jlcpcb(self):
        assert get_fab_family("jlcpcb_tier1") == "jlcpcb"
        assert get_fab_family("jlcpcb-capabilityplus") == "jlcpcb"
        assert get_fab_family("JLCPCB-Tier1") == "jlcpcb"

    def test_base_profiles_map_to_themselves(self):
        for mfr in ("jlcpcb", "pcbway", "oshpark", "seeed", "flashpcb"):
            assert get_fab_family(mfr) == mfr

    def test_plain_aliases_resolve_to_canonical(self):
        assert get_fab_family("jlc") == "jlcpcb"
        assert get_fab_family("lcsc") == "jlcpcb"
        assert get_fab_family("osh") == "oshpark"

    def test_unknown_id_passes_through_normalized(self):
        assert get_fab_family("Generic") == "generic"


# ---------------------------------------------------------------------------
# AssemblyPackage: tier profile uses family formats, keeps profile ID
# ---------------------------------------------------------------------------


class TestAssemblyPackageFabFamily:
    def _make_pkg(self, tmp_path, manufacturer):
        from kicad_tools.export.assembly import AssemblyConfig, AssemblyPackage

        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        return AssemblyPackage(
            pcb_path=pcb,
            manufacturer=manufacturer,
            config=AssemblyConfig(bom_source="pcb"),
        )

    def test_tier1_keeps_profile_but_resolves_family(self, tmp_path):
        pkg = self._make_pkg(tmp_path, "jlcpcb-tier1")
        assert pkg.manufacturer == "jlcpcb-tier1"
        assert pkg.fab_family == "jlcpcb"

    def test_tier1_bom_formatter_resolves(self, tmp_path):
        """The BOM formatter lookup must not raise for a tier profile."""
        from kicad_tools.export.bom_formats import get_bom_formatter

        pkg = self._make_pkg(tmp_path, "jlcpcb-tier1")
        formatter = get_bom_formatter(pkg.fab_family)
        assert formatter.manufacturer_id == "jlcpcb"

    def test_tier1_pnp_formatter_resolves(self, tmp_path):
        from kicad_tools.export.pnp import get_pnp_formatter

        pkg = self._make_pkg(tmp_path, "jlcpcb-tier1")
        formatter = get_pnp_formatter(pkg.fab_family)
        assert formatter.manufacturer_id == "jlcpcb"

    def test_tier1_gerber_preset_resolves(self, tmp_path):
        from kicad_tools.export.gerber import MANUFACTURER_PRESETS

        pkg = self._make_pkg(tmp_path, "jlcpcb-tier1")
        assert pkg.fab_family in MANUFACTURER_PRESETS

    def test_base_jlcpcb_unchanged(self, tmp_path):
        pkg = self._make_pkg(tmp_path, "jlcpcb")
        assert pkg.manufacturer == "jlcpcb"
        assert pkg.fab_family == "jlcpcb"


# ---------------------------------------------------------------------------
# Profile threading: collector -> audit -> DRCChecker
# ---------------------------------------------------------------------------


class TestProfileThreading:
    def test_collector_threads_manufacturer_into_audit(self, tmp_path):
        """ReportDataCollector must run ManufacturingAudit at the same
        manufacturer profile that was passed in (issue #3497)."""
        from kicad_tools.report.collector import ReportDataCollector

        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text("(kicad_pcb)")

        collector = ReportDataCollector(
            pcb_path=pcb_path,
            manufacturer="jlcpcb-tier1",
            skip_erc=True,
        )

        captured = {}

        class FakeAudit:
            def __init__(self, path, manufacturer, quantity, skip_erc):
                captured["manufacturer"] = manufacturer

            def run(self):
                raise RuntimeError("stop after capture")

        with patch("kicad_tools.audit.auditor.ManufacturingAudit", FakeAudit):
            with patch("kicad_tools.schema.pcb.PCB.load", return_value=MagicMock()):
                # Sub-collectors are irrelevant here; we only care that the
                # audit was constructed with the right profile.
                with patch.object(ReportDataCollector, "_safe_collect", MagicMock()):
                    collector.collect_all(tmp_path / "data")

        assert captured["manufacturer"] == "jlcpcb-tier1"

    def test_audit_drc_threads_manufacturer_into_checker(self, tmp_path):
        """ManufacturingAudit._check_drc must construct DRCChecker with the
        audit's manufacturer profile and use the kct-check pad_grid policy."""
        from kicad_tools.audit import auditor as auditor_mod

        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text("(kicad_pcb)")

        audit = auditor_mod.ManufacturingAudit(
            pcb_path,
            manufacturer="jlcpcb-tier1",
            skip_erc=True,
        )

        checker_instance = MagicMock()
        checker_instance.check_all.return_value = MagicMock(violations=[])
        checker_cls = MagicMock(return_value=checker_instance)
        checker_cls.is_advisory_rule = MagicMock(return_value=False)

        with patch("kicad_tools.validate.DRCChecker", checker_cls):
            audit._check_drc(MagicMock())

        # Constructed with the full tier profile, not a downgraded family
        _, kwargs = checker_cls.call_args
        assert kwargs["manufacturer"] == "jlcpcb-tier1"

        # check_all called with the kct-check pad_grid auto-derive policy
        _, ca_kwargs = checker_instance.check_all.call_args
        assert ca_kwargs.get("pad_grid_auto_derive") is True

    def test_check_all_forwards_pad_grid_auto_derive(self):
        """DRCChecker.check_all must forward pad_grid_auto_derive to the
        pad_grid rule and leave other checks untouched."""
        from kicad_tools.validate import DRCChecker
        from kicad_tools.validate.violations import DRCResults

        checker = DRCChecker(MagicMock(), manufacturer="jlcpcb", layers=4)

        mocks = {}
        for name in DRCChecker.CHECK_ALL_METHODS:
            m = MagicMock(return_value=DRCResults())
            setattr(checker, name, m)
            mocks[name] = m

        checker.check_all(pad_grid_auto_derive=True)

        mocks["check_pad_grid_alignment"].assert_called_once_with(auto_derive_threshold=True)
        # All other methods called with no arguments
        for name, m in mocks.items():
            if name != "check_pad_grid_alignment":
                m.assert_called_once_with()

    def test_check_all_default_preserves_fixed_tolerance(self):
        """Default check_all keeps the PR #3057 fixed-tolerance behaviour."""
        from kicad_tools.validate import DRCChecker
        from kicad_tools.validate.violations import DRCResults

        checker = DRCChecker(MagicMock(), manufacturer="jlcpcb", layers=4)
        for name in DRCChecker.CHECK_ALL_METHODS:
            setattr(checker, name, MagicMock(return_value=DRCResults()))

        checker.check_all()

        checker.check_pad_grid_alignment.assert_called_once_with(auto_derive_threshold=False)

    def test_manufacturing_package_threads_manufacturer_to_collector(self, tmp_path):
        """ManufacturingPackage._generate_report must pass its (full)
        manufacturer profile to ReportDataCollector."""
        from kicad_tools.export.manufacturing import (
            ManufacturingPackage,
            ManufacturingResult,
        )

        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        pkg = ManufacturingPackage(pcb_path=pcb, manufacturer="jlcpcb-tier1")
        result = ManufacturingResult(output_dir=tmp_path / "out")

        captured = {}

        class FakeCollector:
            def __init__(self, pcb_path, manufacturer):
                captured["manufacturer"] = manufacturer

            def collect_all(self, data_dir):
                raise RuntimeError("stop after capture")

        with patch("kicad_tools.report.collector.ReportDataCollector", FakeCollector):
            pkg._generate_report(tmp_path / "out", result)

        assert captured["manufacturer"] == "jlcpcb-tier1"


# ---------------------------------------------------------------------------
# Per-layer figure emission
# ---------------------------------------------------------------------------


class TestPerLayerFigures:
    def test_layer_figure_filename(self):
        from kicad_tools.report.figures import _layer_figure_filename

        assert _layer_figure_filename("F.Cu") == "layer_F_Cu.png"
        assert _layer_figure_filename("In1.Cu") == "layer_In1_Cu.png"
        assert _layer_figure_filename("B.Cu") == "layer_B_Cu.png"

    @patch("kicad_tools.report.figures.screenshot_board")
    @patch(
        "kicad_tools.report.figures._copper_layer_names",
        return_value=["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
    )
    def test_four_layer_board_emits_four_layer_figures(self, mock_layers, mock_board, tmp_path):
        from kicad_tools.report.figures import ReportFigureGenerator

        mock_board.return_value = {"success": True}

        gen = ReportFigureGenerator()
        entries = gen._generate_per_layer_figures(tmp_path / "board.kicad_pcb", tmp_path)

        assert [e.filename for e in entries] == [
            "layer_F_Cu.png",
            "layer_In1_Cu.png",
            "layer_In2_Cu.png",
            "layer_B_Cu.png",
        ]
        assert all(e.figure_type == "pcb_layer" for e in entries)
        assert [e.caption for e in entries] == [
            "Copper Layer F.Cu",
            "Copper Layer In1.Cu",
            "Copper Layer In2.Cu",
            "Copper Layer B.Cu",
        ]

        # Each layer rendered together with the board outline
        rendered_layer_args = [call.kwargs["layers"] for call in mock_board.call_args_list]
        assert rendered_layer_args == [
            "F.Cu,Edge.Cuts",
            "In1.Cu,Edge.Cuts",
            "In2.Cu,Edge.Cuts",
            "B.Cu,Edge.Cuts",
        ]

    def test_copper_layer_names_falls_back_on_parse_failure(self, tmp_path):
        from kicad_tools.report.figures import _copper_layer_names

        bad_pcb = tmp_path / "bad.kicad_pcb"
        bad_pcb.write_text("not a pcb")
        assert _copper_layer_names(bad_pcb) == ["F.Cu", "B.Cu"]

    def test_entries_to_layer_figures(self):
        from kicad_tools.cli.report_cmd import _entries_to_layer_figures
        from kicad_tools.report.figures import FigureEntry

        entries = [
            FigureEntry("pcb_front.png", "PCB Front", "pcb_front"),
            FigureEntry("layer_F_Cu.png", "Copper Layer F.Cu", "pcb_layer"),
            FigureEntry("layer_B_Cu.png", "Copper Layer B.Cu", "pcb_layer"),
        ]
        layers = _entries_to_layer_figures(entries)
        assert layers == [
            {"name": "F.Cu", "figure_path": "figures/layer_F_Cu.png"},
            {"name": "B.Cu", "figure_path": "figures/layer_B_Cu.png"},
        ]

    def test_entries_to_layer_figures_empty(self):
        from kicad_tools.cli.report_cmd import _entries_to_layer_figures

        assert _entries_to_layer_figures([]) is None


# ---------------------------------------------------------------------------
# Template rendering of the Copper Layers section
# ---------------------------------------------------------------------------


class TestCopperLayersTemplate:
    def test_template_renders_layer_images(self, tmp_path):
        pytest.importorskip("jinja2")
        from kicad_tools.report.generator import ReportGenerator
        from kicad_tools.report.models import ReportData

        data = ReportData(
            project_name="testboard",
            revision="1",
            date="2026-06-11",
            manufacturer="jlcpcb-tier1",
            pcb_figures={"assembly": "figures/assembly.png"},
            pcb_layer_figures=[
                {"name": "F.Cu", "figure_path": "figures/layer_F_Cu.png"},
                {"name": "In1.Cu", "figure_path": "figures/layer_In1_Cu.png"},
            ],
        )

        gen = ReportGenerator()
        report_path = gen.generate(data, tmp_path)
        md = report_path.read_text()

        assert "## Copper Layers" in md
        assert "![F.Cu](figures/layer_F_Cu.png)" in md
        assert "![In1.Cu](figures/layer_In1_Cu.png)" in md
        assert "![Assembly](figures/assembly.png)" in md

    def test_template_omits_section_without_layer_figures(self, tmp_path):
        pytest.importorskip("jinja2")
        from kicad_tools.report.generator import ReportGenerator
        from kicad_tools.report.models import ReportData

        data = ReportData(
            project_name="testboard",
            revision="1",
            date="2026-06-11",
            manufacturer="jlcpcb",
        )
        gen = ReportGenerator()
        report_path = gen.generate(data, tmp_path)
        assert "## Copper Layers" not in report_path.read_text()


# ---------------------------------------------------------------------------
# Image promotion into the manufacturing package
# ---------------------------------------------------------------------------


class TestImagePromotion:
    def _make_staging(self, tmp_path):
        """Create an out_dir with a promoted report.md and a staged report
        directory containing figures."""
        out_dir = tmp_path / "manufacturing"
        out_dir.mkdir()
        staging = out_dir / "report"
        figures = staging / "figures"
        figures.mkdir(parents=True)

        for name in ("layer_F_Cu.png", "layer_B_Cu.png", "assembly.png"):
            (figures / name).write_bytes(b"\x89PNG fake " + name.encode())

        (out_dir / "report.md").write_text(
            "# Report\n"
            "![F.Cu](figures/layer_F_Cu.png)\n"
            "![B.Cu](figures/layer_B_Cu.png)\n"
            "![Assembly](figures/assembly.png)\n"
        )
        return out_dir, staging

    def test_promote_report_images(self, tmp_path):
        from kicad_tools.export.manufacturing import (
            ManufacturingPackage,
            ManufacturingResult,
        )

        out_dir, staging = self._make_staging(tmp_path)
        result = ManufacturingResult(output_dir=out_dir)

        ManufacturingPackage._promote_report_images(out_dir, staging, result)

        images_dir = out_dir / "images"
        assert images_dir.is_dir()
        assert sorted(p.name for p in images_dir.iterdir()) == [
            "assembly.png",
            "layer_B_Cu.png",
            "layer_F_Cu.png",
        ]

        # Markdown refs rewritten figures/ -> images/
        md = (out_dir / "report.md").read_text()
        assert "](images/layer_F_Cu.png)" in md
        assert "](figures/" not in md

        # Image paths recorded for the manifest
        assert sorted(p.name for p in result.image_paths) == [
            "assembly.png",
            "layer_B_Cu.png",
            "layer_F_Cu.png",
        ]
        # all_files (the manifest source) includes the images
        all_names = {p.name for p in result.all_files}
        assert {"assembly.png", "layer_B_Cu.png", "layer_F_Cu.png"} <= all_names

    def test_promote_noop_without_figures(self, tmp_path):
        from kicad_tools.export.manufacturing import (
            ManufacturingPackage,
            ManufacturingResult,
        )

        out_dir = tmp_path / "manufacturing"
        staging = out_dir / "report"
        staging.mkdir(parents=True)
        (out_dir / "report.md").write_text("# Report\n")

        result = ManufacturingResult(output_dir=out_dir)
        ManufacturingPackage._promote_report_images(out_dir, staging, result)

        assert not (out_dir / "images").exists()
        assert result.image_paths == []

    def test_manifest_includes_image_checksums(self, tmp_path):
        from kicad_tools.export.manufacturing import (
            ManufacturingPackage,
            ManufacturingResult,
            _build_manifest,
            _sha256_file,
        )

        out_dir, staging = self._make_staging(tmp_path)
        result = ManufacturingResult(output_dir=out_dir)
        ManufacturingPackage._promote_report_images(out_dir, staging, result)

        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        manifest = _build_manifest(result, pcb, "jlcpcb-tier1")

        assert manifest["manufacturer"] == "jlcpcb-tier1"
        for name in ("layer_F_Cu.png", "layer_B_Cu.png", "assembly.png"):
            assert name in manifest["files"]
            assert manifest["files"][name]["sha256"] == _sha256_file(out_dir / "images" / name)
