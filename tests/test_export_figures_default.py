"""Regression tests: ``kct export`` ships report figures by default (issue #3583).

Committed manufacturing bundles for the demo boards were missing their
``images/`` directory (per-copper-layer renders, front/back assembly
views, schematic sheets) because figure generation degraded *silently*
when ``cairosvg`` was absent from the export environment: the skip
reason only went to the module logger, never to the export result, so
``kct export`` looked successful while shipping a bundle without
visuals.

Two guards:

1. **Loud degradation** -- every figure-skip path must record a warning
   on ``ManufacturingResult.warnings`` (surfaced by the CLI and JSON
   output).
2. **Default shipping** -- when kicad-cli + cairosvg are available (the
   default dev environment now includes cairosvg), a plain export of a
   real fixture board must land PNG figures in ``<bundle>/images/`` and
   list them in ``manifest.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.export.manufacturing import (
    ManufacturingConfig,
    ManufacturingPackage,
    ManufacturingResult,
)
from kicad_tools.export.preflight import PreflightConfig

FIXTURES = Path(__file__).parent / "fixtures" / "projects"


def _figure_deps_available() -> bool:
    """True when both kicad-cli and a working cairosvg are present."""
    try:
        from kicad_tools.cli.runner import find_kicad_cli
        from kicad_tools.mcp.tools.screenshot import _check_cairosvg
    except ImportError:
        return False
    return find_kicad_cli() is not None and _check_cairosvg()


# ---------------------------------------------------------------------------
# Guard 1: silent skip paths must surface on result.warnings
# ---------------------------------------------------------------------------


class TestFigureSkipIsLoud:
    def _package(self, tmp_path: Path) -> tuple[ManufacturingPackage, ManufacturingResult]:
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        pkg = ManufacturingPackage(pcb_path=pcb, manufacturer="jlcpcb")
        result = ManufacturingResult(output_dir=tmp_path / "out")
        return pkg, result

    def test_missing_renderer_deps_record_result_warning(self, tmp_path, monkeypatch):
        """RuntimeError from the figure generator (e.g. cairosvg missing)
        must append a bundle-will-lack-images warning to result.warnings."""
        from kicad_tools.report import figures as figures_mod

        def boom(self, *args, **kwargs):
            raise RuntimeError(
                "cairosvg is required for report figure generation. "
                "Install with: pip install 'kicad-tools[screenshot]'"
            )

        monkeypatch.setattr(figures_mod.ReportFigureGenerator, "generate_all", boom)

        pkg, result = self._package(tmp_path)
        # A schematic exists, so the skip is attributable to the renderer.
        (tmp_path / "board.kicad_sch").write_text("(kicad_sch)")

        out = pkg._generate_figures(tmp_path / "v1", result)

        assert out is None
        assert any("Report figures skipped" in w for w in result.warnings)
        assert any("cairosvg" in w for w in result.warnings)
        assert any("images/" in w for w in result.warnings)

    def test_missing_schematic_records_result_warning(self, tmp_path):
        pkg, result = self._package(tmp_path)
        # No .kicad_sch anywhere next to the PCB.
        out = pkg._generate_figures(tmp_path / "v1", result)

        assert out is None
        assert any("Report figures skipped" in w for w in result.warnings)
        assert any("no schematic found" in w for w in result.warnings)

    def test_stale_images_removed_when_no_figures_staged(self, tmp_path):
        """A figure-less export must not leave behind images/ from a
        previous export (stale renders shipping against fresh gerbers is
        how softstart ended up with pre-repair visuals)."""
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        stale = out_dir / "images"
        stale.mkdir()
        (stale / "layer_F_Cu.png").write_bytes(b"\x89PNG-old")

        staging = tmp_path / "report_staging"
        staging.mkdir()  # no figures/ subdir staged

        result = ManufacturingResult(output_dir=out_dir)
        ManufacturingPackage._promote_report_images(out_dir, staging, result)

        assert not stale.exists(), "stale images/ must be removed"
        assert any("stale images/" in w.lower() for w in result.warnings)

    def test_empty_figure_set_records_result_warning(self, tmp_path, monkeypatch):
        from kicad_tools.report import figures as figures_mod

        monkeypatch.setattr(
            figures_mod.ReportFigureGenerator,
            "generate_all",
            lambda self, *a, **k: [],
        )

        pkg, result = self._package(tmp_path)
        (tmp_path / "board.kicad_sch").write_text("(kicad_sch)")

        out = pkg._generate_figures(tmp_path / "v1", result)

        assert out is None
        assert any("produced no images" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Guard 2: a real export ships images/ in the bundle + manifest by default
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _figure_deps_available(),
    reason="kicad-cli and/or cairosvg not available for figure rendering",
)
class TestExportShipsImagesByDefault:
    @pytest.mark.slow
    def test_bundle_contains_images_and_manifest_entries(self, tmp_path):
        import shutil

        # Copy the tiny fixture project so side-effect files stay in tmp.
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        for name in (
            "test_project.kicad_pcb",
            "test_project.kicad_sch",
            "test_project.kicad_pro",
        ):
            shutil.copy2(FIXTURES / name, project_dir / name)

        out_dir = tmp_path / "manufacturing"
        config = ManufacturingConfig(
            output_dir=out_dir,
            # Keep the test focused on the report/figure pipeline: skip
            # gerbers (extra kicad-cli runs) and network-flavored steps.
            include_gerbers=False,
            include_project_zip=False,
            auto_lcsc=False,
            preflight=PreflightConfig(skip_all=True),
        )
        pkg = ManufacturingPackage(
            pcb_path=project_dir / "test_project.kicad_pcb",
            manufacturer="jlcpcb",
            config=config,
        )
        result = pkg.export(out_dir)

        assert result.success, f"export errors: {result.errors}"

        # The bundle must ship an images/ directory with the PCB figure
        # set by DEFAULT (no extra flags).  The fixture is a 2-layer
        # board, so per-layer renders are layer_F_Cu / layer_B_Cu.
        images_dir = out_dir / "images"
        assert images_dir.is_dir(), f"bundle lacks images/ -- warnings: {result.warnings}"
        shipped = sorted(p.name for p in images_dir.glob("*.png"))
        assert shipped, "images/ exists but contains no PNGs"
        for expected in ("layer_F_Cu.png", "layer_B_Cu.png", "pcb_front.png"):
            assert expected in shipped, f"missing {expected}; shipped: {shipped}"

        # No silent-skip warning may fire when deps are available.
        assert not any("Report figures skipped" in w for w in result.warnings)

        # result.image_paths feeds the manifest.
        assert result.image_paths
        assert all(p.parent == images_dir for p in result.image_paths)

        # report.md must reference the shipped images/ (not build-time figures/).
        report_md = out_dir / "report.md"
        assert report_md.exists()
        md_text = report_md.read_text(encoding="utf-8")
        assert "](images/" in md_text
        assert "](figures/" not in md_text

        # manifest.json must checksum every shipped image.
        manifest = json.loads((out_dir / "manifest.json").read_text())
        for name in shipped:
            assert name in manifest["files"], (
                f"manifest missing image {name}; manifest files: {sorted(manifest['files'])}"
            )
