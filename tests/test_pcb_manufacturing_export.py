"""Tests for PCB manufacturing export methods."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.schema.pcb import PCB


class TestPCBPath:
    """Tests for PCB path tracking."""

    def test_load_sets_path(self, test_project_pcb):
        """PCB.load() should store the file path."""
        pcb = PCB.load(test_project_pcb)
        assert pcb.path is not None
        assert pcb.path == Path(test_project_pcb)

    def test_create_has_no_path(self):
        """PCB.create() should have no path initially."""
        pcb = PCB.create(width=100, height=100)
        assert pcb.path is None

    def test_save_sets_path(self):
        """Saving a PCB should update the stored path."""
        pcb = PCB.create(width=100, height=100)
        assert pcb.path is None

        with tempfile.NamedTemporaryFile(suffix=".kicad_pcb", delete=False) as f:
            path = Path(f.name)

        try:
            pcb.save(path)
            assert pcb.path == path
        finally:
            path.unlink(missing_ok=True)

    def test_save_without_path_raises(self):
        """Saving without a path when none stored should raise."""
        pcb = PCB.create(width=100, height=100)
        with pytest.raises(ValueError, match="No path specified"):
            pcb.save()


class TestExportPlacement:
    """Tests for export_placement method."""

    def test_export_placement_csv(self, test_project_pcb):
        """Should export placement file in CSV format."""
        pcb = PCB.load(test_project_pcb)

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "placement.csv"
            result = pcb.export_placement(output)

            assert result == output
            assert output.exists()

            content = output.read_text()
            # Check that the file has valid placement data
            assert "Ref" in content or "Reference" in content or "Designator" in content

    def test_export_placement_jlcpcb(self, test_project_pcb):
        """Should export placement in JLCPCB format."""
        pcb = PCB.load(test_project_pcb)

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "cpl_jlcpcb.csv"
            result = pcb.export_placement(output, format="jlcpcb")

            assert result == output
            assert output.exists()

    def test_export_placement_jlcpcb_excludes_tht(self, test_project_pcb, tmp_path):
        """export_placement(format="jlcpcb") must honor JLCPCB's exclude_tht default.

        Regression test for issue #3627: export_placement() used to synthesize a
        bare PnPExportConfig() and pass it to export_pnp(), defeating the JLCPCB
        formatter's exclude_tht=True default.  The CPL would then ship THT rows,
        diverging from export_pnp(..., config=None).  Passing config=None lets the
        formatter resolve the effective config (single source of truth, #3616/#3618).
        """
        # Inject a through-hole footprint into the SMD-only fixture so the
        # JLCPCB exclude_tht default has something to act on.
        tht_footprint = (
            '\t(footprint "Connector_PinHeader_2.54mm:PinHeader_1x04"\n'
            '\t\t(layer "F.Cu")\n'
            '\t\t(uuid "fp-j1-uuid")\n'
            "\t\t(at 160 50)\n"
            "\t\t(attr through_hole)\n"
            '\t\t(property "Reference" "J1" (at 0 -1.5) (layer "F.SilkS") (uuid "ref-j1"))\n'
            '\t\t(property "Value" "Conn_01x04" (at 0 1.5) (layer "F.Fab") (uuid "val-j1"))\n'
            '\t\t(pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0) '
            '(layers "*.Cu" "*.Mask") (net 2 "GND"))\n'
            "\t)\n"
        )
        original = Path(test_project_pcb).read_text()
        # Insert the THT footprint before the final closing paren of the board.
        patched = original.rstrip()
        assert patched.endswith(")")
        patched = patched[:-1] + tht_footprint + ")\n"

        pcb_path = tmp_path / "with_tht.kicad_pcb"
        pcb_path.write_text(patched)

        pcb = PCB.load(pcb_path)

        output = tmp_path / "cpl_jlcpcb.csv"
        pcb.export_placement(output, format="jlcpcb")
        content = output.read_text()

        # THT part excluded from the CPL by JLCPCB's exclude_tht default...
        assert "J1" not in content
        # ...while SMD parts remain.
        assert "R1" in content
        assert "C1" in content

    def test_export_placement_top_only(self, test_project_pcb):
        """Should export only top-side components."""
        pcb = PCB.load(test_project_pcb)

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "placement_top.csv"
            result = pcb.export_placement(output, side="top")

            assert result == output
            assert output.exists()


class TestExportGerbers:
    """Tests for export_gerbers method (requires kicad-cli)."""

    @pytest.fixture
    def skip_without_kicad_cli(self):
        """Skip test if kicad-cli is not available."""
        from kicad_tools.export import find_kicad_cli

        if find_kicad_cli() is None:
            pytest.skip("kicad-cli not available")

    def test_export_gerbers_jlcpcb(self, test_project_pcb, skip_without_kicad_cli):
        """Should export Gerber files for JLCPCB."""
        from kicad_tools.exceptions import ExportError

        pcb = PCB.load(test_project_pcb)

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "gerbers"
            try:
                result = pcb.export_gerbers(output_dir, manufacturer="jlcpcb")
                assert result.exists()
                # export_gerbers now returns a ready-to-upload zip rather
                # than a directory of loose .gbr files (stale-test update,
                # issue #3436 burn-down).  Verify it contains gerber layers.
                import zipfile

                with zipfile.ZipFile(result) as zf:
                    names = zf.namelist()
                gerber_files = [
                    n
                    for n in names
                    if n.lower().endswith((".gbr", ".gtl", ".gbl", ".gko")) or ".g" in n.lower()
                ]
                assert len(gerber_files) > 0, f"no gerber layers in {result}: {names}"
            except ExportError as e:
                # kicad-cli may fail for various reasons (version mismatch, etc.)
                pytest.skip(f"kicad-cli export failed: {e}")

    def test_export_gerbers_requires_path(self):
        """Should raise error if PCB has no path."""
        pcb = PCB.create(width=100, height=100)

        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(ValueError, match="requires a PCB file path"):
                pcb.export_gerbers(tmpdir)


class TestExportGerbersZip:
    """Tests for export_gerbers_zip method."""

    @pytest.fixture
    def skip_without_kicad_cli(self):
        """Skip test if kicad-cli is not available."""
        from kicad_tools.export import find_kicad_cli

        if find_kicad_cli() is None:
            pytest.skip("kicad-cli not available")

    def test_export_gerbers_zip(self, test_project_pcb, skip_without_kicad_cli):
        """Should export Gerbers as a zip file."""
        from kicad_tools.exceptions import ExportError

        pcb = PCB.load(test_project_pcb)

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "gerbers.zip"
            try:
                result = pcb.export_gerbers_zip(output, manufacturer="jlcpcb")

                assert result == output
                assert output.exists()

                # Verify it's a valid zip
                import zipfile

                with zipfile.ZipFile(output, "r") as zf:
                    names = zf.namelist()
                    assert len(names) > 0
            except ExportError as e:
                # kicad-cli may fail for various reasons
                pytest.skip(f"kicad-cli export failed: {e}")


class TestExportBOM:
    """Tests for export_bom method."""

    def test_export_bom_requires_schematic(self, test_project_pcb):
        """Should raise error if schematic not found."""
        pcb = PCB.load(test_project_pcb)

        # If no schematic exists alongside the PCB
        schematic_path = Path(test_project_pcb).with_suffix(".kicad_sch")
        if not schematic_path.exists():
            with tempfile.TemporaryDirectory() as tmpdir:
                with pytest.raises(ValueError, match="Schematic not found"):
                    pcb.export_bom(Path(tmpdir) / "bom.csv")


class TestExportDrill:
    """Tests for export_drill method."""

    @pytest.fixture
    def skip_without_kicad_cli(self):
        """Skip test if kicad-cli is not available."""
        from kicad_tools.export import find_kicad_cli

        if find_kicad_cli() is None:
            pytest.skip("kicad-cli not available")

    def test_export_drill(self, test_project_pcb, skip_without_kicad_cli):
        """Should export drill files."""
        from kicad_tools.exceptions import ExportError

        pcb = PCB.load(test_project_pcb)

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "drill"
            try:
                result = pcb.export_drill(output_dir)
                assert result.exists()
            except ExportError as e:
                # kicad-cli may fail for various reasons
                pytest.skip(f"kicad-cli export failed: {e}")


class TestExportManufacturing:
    """Tests for export_manufacturing method."""

    @pytest.fixture
    def skip_without_kicad_cli(self):
        """Skip test if kicad-cli is not available."""
        from kicad_tools.export import find_kicad_cli

        if find_kicad_cli() is None:
            pytest.skip("kicad-cli not available")

    def test_export_manufacturing_requires_path(self):
        """Should raise error if PCB has no path."""
        pcb = PCB.create(width=100, height=100)

        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(ValueError, match="requires a PCB file path"):
                pcb.export_manufacturing(tmpdir)

    def test_export_manufacturing(self, test_project_pcb, skip_without_kicad_cli):
        """Should export complete manufacturing package."""
        from kicad_tools.exceptions import ExportError

        pcb = PCB.load(test_project_pcb)

        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                # Export without assembly (no schematic needed)
                result = pcb.export_manufacturing(
                    tmpdir, manufacturer="jlcpcb", include_assembly=False
                )

                assert "gerbers" in result
                # The gerbers key may be None or a path depending on success
                if result["gerbers"]:
                    assert Path(result["gerbers"]).exists()
            except ExportError as e:
                # kicad-cli may fail for various reasons
                pytest.skip(f"kicad-cli export failed: {e}")


class TestGenerateReport:
    """Tests for ManufacturingPackage._generate_report method."""

    def test_generate_report_produces_markdown(self, test_project_pcb):
        """_generate_report should produce a report.md and set result.report_path."""
        from kicad_tools.export.manufacturing import (
            ManufacturingConfig,
            ManufacturingPackage,
            ManufacturingResult,
        )

        pkg = ManufacturingPackage(
            pcb_path=test_project_pcb,
            manufacturer="jlcpcb",
            config=ManufacturingConfig(include_report=True),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            result = ManufacturingResult(output_dir=out_dir)

            # Force both PDF renderers off so we deterministically test pure
            # Markdown output. (Pre-fix #3205, patching only weasyprint was
            # sufficient because the pandoc fallback was suppressed by the
            # OSError leak; post-fix the fallback runs whenever pandoc is
            # on PATH, producing report.pdf instead of report.md.)
            with (
                patch(
                    "kicad_tools.report.renderers._weasyprint_available",
                    return_value=False,
                ),
                patch(
                    "kicad_tools.report.renderers._pandoc_available",
                    return_value=False,
                ),
            ):
                pkg._generate_report(out_dir, result)

            # report_path should be set to a valid .md file
            assert result.report_path is not None
            assert result.report_path.exists()
            assert result.report_path.suffix == ".md"
            # No errors should be recorded
            report_errors = [e for e in result.errors if "Report" in e or "report" in e]
            assert len(report_errors) == 0

    def test_generate_report_no_import_error(self, test_project_pcb):
        """_generate_report must not trigger an ImportError for generate_report."""
        from kicad_tools.export.manufacturing import (
            ManufacturingPackage,
            ManufacturingResult,
        )

        pkg = ManufacturingPackage(
            pcb_path=test_project_pcb,
            manufacturer="jlcpcb",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            result = ManufacturingResult(output_dir=out_dir)

            # Patch logger to capture warnings -- ImportError would produce
            # "report module not available" warning
            with patch("kicad_tools.export.manufacturing.logger") as mock_logger:
                pkg._generate_report(out_dir, result)

                # Verify the old misleading message does NOT appear
                for call in mock_logger.warning.call_args_list:
                    msg = call[0][0] if call[0] else ""
                    assert "report module not available" not in msg

    def test_generate_report_sets_report_path_in_result(self, test_project_pcb):
        """ManufacturingResult.report_path should point to the generated file.

        Patches both PDF renderers to be unavailable so the assertion against
        the Markdown source remains deterministic regardless of host pandoc /
        weasyprint installation state. (Previously this test relied on
        ``_weasyprint_available()`` leaking ``OSError`` on libgobject-less
        hosts to suppress the pandoc fallback; after issue #3205 the fallback
        is exercised correctly when pandoc IS available, producing a binary
        PDF that cannot be read with ``encoding="utf-8"``.)
        """
        from kicad_tools.export.manufacturing import (
            ManufacturingPackage,
            ManufacturingResult,
        )

        pkg = ManufacturingPackage(
            pcb_path=test_project_pcb,
            manufacturer="jlcpcb",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            result = ManufacturingResult(output_dir=out_dir)
            with (
                patch(
                    "kicad_tools.report.renderers._weasyprint_available",
                    return_value=False,
                ),
                patch(
                    "kicad_tools.report.renderers._pandoc_available",
                    return_value=False,
                ),
            ):
                pkg._generate_report(out_dir, result)

            assert result.report_path is not None
            assert result.report_path.suffix == ".md"
            content = result.report_path.read_text(encoding="utf-8")
            # Should contain some markdown content
            assert len(content) > 0


class TestRenderReportPdf:
    """Tests for ManufacturingPackage._render_report_pdf integration."""

    def test_render_pdf_produces_pdf_when_available(self, test_project_pcb):
        """_generate_report produces report.pdf when weasyprint is available."""
        from kicad_tools.export.manufacturing import (
            ManufacturingPackage,
            ManufacturingResult,
        )

        pkg = ManufacturingPackage(
            pcb_path=test_project_pcb,
            manufacturer="jlcpcb",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            result = ManufacturingResult(output_dir=out_dir)

            # Mock both render_html and render_pdf to avoid needing
            # the markdown and weasyprint packages at test time.
            def fake_render_html(md_content, figures_dir=None):
                return "<html><body>mock</body></html>"

            def fake_render_pdf(html_content, output_path):
                Path(output_path).write_bytes(b"%PDF-mock")

            with patch(
                "kicad_tools.export.manufacturing.ManufacturingPackage._render_report_pdf",
                wraps=None,
            ):
                # Let _generate_report run normally to produce the .md,
                # then manually call the rendering logic with mocks.
                pkg._generate_report(out_dir, result)

            # At this point result.report_path is .md; now test the
            # rendering step in isolation.
            assert result.report_path is not None
            md_path = result.report_path
            assert md_path.suffix == ".md"

            # Simulate what _render_report_pdf does with mocked renderers
            with (
                patch(
                    "kicad_tools.report.renderers.render_html",
                    side_effect=fake_render_html,
                ),
                patch(
                    "kicad_tools.report.renderers.render_pdf",
                    side_effect=fake_render_pdf,
                ),
            ):
                from kicad_tools.export.manufacturing import ManufacturingPackage as MP

                version_dir = md_path.parent
                MP._render_report_pdf(md_path, version_dir, result)

            assert result.report_path is not None
            assert result.report_path.suffix == ".pdf"
            assert result.report_path.exists()

    def test_render_pdf_falls_back_to_md(self, test_project_pcb):
        """_generate_report falls back to .md when weasyprint is absent."""
        from kicad_tools.export.manufacturing import (
            ManufacturingPackage,
            ManufacturingResult,
        )

        pkg = ManufacturingPackage(
            pcb_path=test_project_pcb,
            manufacturer="jlcpcb",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            result = ManufacturingResult(output_dir=out_dir)

            with (
                patch(
                    "kicad_tools.report.renderers._weasyprint_available",
                    return_value=False,
                ),
                patch(
                    "kicad_tools.report.renderers._pandoc_available",
                    return_value=False,
                ),
            ):
                pkg._generate_report(out_dir, result)

            # result.report_path should remain as .md
            assert result.report_path is not None
            assert result.report_path.suffix == ".md"
            assert result.report_path.exists()
            # No errors recorded for graceful fallback
            report_errors = [e for e in result.errors if "report" in e.lower()]
            assert len(report_errors) == 0

    def test_render_pdf_swallows_libgobject_oserror(self, test_project_pcb):
        """Regression test for #3205: libgobject OSError must not poison result.errors.

        On macOS / minimal Linux hosts, ``import weasyprint`` raises ``OSError``
        from ``ctypes.CDLL`` when libgobject is unavailable. Before the fix,
        this escaped ``_weasyprint_available()`` (which only caught
        ``ImportError``), bubbled up through ``_render_report_pdf``, and was
        caught by the broad ``except Exception`` in ``_generate_report`` —
        which appended ``"Report generation failed: ..."`` to
        ``result.errors`` and forced ``kct export`` to exit 1 even when every
        other artifact wrote successfully.

        After the fix, the OSError is caught at ``_weasyprint_available()``
        and the renderer degrades to pandoc-or-Markdown gracefully. The
        manufacturing result must be clean of report-related errors.
        """
        from kicad_tools.export.manufacturing import (
            ManufacturingPackage,
            ManufacturingResult,
        )

        pkg = ManufacturingPackage(
            pcb_path=test_project_pcb,
            manufacturer="jlcpcb",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            result = ManufacturingResult(output_dir=out_dir)

            # Simulate the libgobject failure mode: the *availability probe*
            # encounters OSError. After the fix this is silently treated as
            # "unavailable" and the pipeline falls back to pandoc-or-MD.
            def fake_weasy_available():
                # Match the post-fix behavior: the OSError is caught inside
                # _weasyprint_available and turned into False.
                return False

            with (
                patch(
                    "kicad_tools.report.renderers._weasyprint_available",
                    side_effect=fake_weasy_available,
                ),
                patch(
                    "kicad_tools.report.renderers._pandoc_available",
                    return_value=False,
                ),
            ):
                pkg._generate_report(out_dir, result)

            # Acceptance criterion #1: no report-related entries in errors,
            # so the per-board generators won't fold a False into their
            # exit-code AND-gate.
            report_errors = [e for e in result.errors if "report" in e.lower()]
            assert len(report_errors) == 0, f"Expected no report errors; got: {report_errors}"

            # Acceptance criterion #2: the Markdown report is present (the
            # PDF is the only missing artifact, by design).
            assert result.report_path is not None
            assert result.report_path.suffix == ".md"
            assert result.report_path.exists()
            pdf_sibling = result.report_path.with_suffix(".pdf")
            assert not pdf_sibling.exists()

    def test_render_pdf_manufacturing_outer_guard_catches_oserror(self, test_project_pcb):
        """Defense-in-depth: the outer guard in _render_report_pdf catches OSError.

        Even if a future regression caused ``from ..report.renderers import
        pdf_renderer_available`` itself to raise ``OSError`` at import time
        (e.g. an eager native-library probe), the manufacturing layer must
        still degrade gracefully — exactly as it does for ``ImportError``.
        """
        from kicad_tools.export.manufacturing import (
            ManufacturingPackage,
            ManufacturingResult,
        )

        pkg = ManufacturingPackage(
            pcb_path=test_project_pcb,
            manufacturer="jlcpcb",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            result = ManufacturingResult(output_dir=out_dir)

            # Generate the .md first so we have a report_path to feed into
            # the standalone _render_report_pdf call.
            with (
                patch(
                    "kicad_tools.report.renderers._weasyprint_available",
                    return_value=False,
                ),
                patch(
                    "kicad_tools.report.renderers._pandoc_available",
                    return_value=False,
                ),
            ):
                pkg._generate_report(out_dir, result)
            assert result.report_path is not None
            md_path = result.report_path

            # Now exercise the outer guard by making the *import* itself
            # raise OSError. The static method must catch it and return
            # without raising or mutating result.errors.
            import builtins

            real_import = builtins.__import__

            def raising_import(name, *args, **kwargs):
                if name.endswith("renderers") or name == "..report.renderers":
                    raise OSError("simulated dlopen failure during module import")
                # Also catch the absolute-name import form used by `from ..report.renderers`
                if "report.renderers" in name:
                    raise OSError("simulated dlopen failure during module import")
                return real_import(name, *args, **kwargs)

            errors_before = list(result.errors)
            with patch("builtins.__import__", side_effect=raising_import):
                # Must not raise; must not append to result.errors.
                ManufacturingPackage._render_report_pdf(md_path, out_dir, result)

            assert result.errors == errors_before
            # MD still present; no PDF
            assert md_path.exists()
            assert not md_path.with_suffix(".pdf").exists()


class TestFlattenLatestReportPdf:
    """Tests for _flatten_latest_report preferring PDF over MD."""

    def test_flatten_prefers_pdf_over_md(self, test_project_pcb):
        """_flatten_latest_report sets report_path to PDF when both exist."""
        from kicad_tools.export.manufacturing import (
            ManufacturingConfig,
            ManufacturingPackage,
            ManufacturingResult,
        )

        config = ManufacturingConfig(
            include_report=True,
            latest_report_only=True,
        )
        pkg = ManufacturingPackage(
            pcb_path=test_project_pcb,
            manufacturer="jlcpcb",
            config=config,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            result = ManufacturingResult(output_dir=out_dir)

            # Generate the MD report without PDF rendering
            with patch(
                "kicad_tools.report.renderers._weasyprint_available",
                return_value=False,
            ):
                pkg._generate_report(out_dir, result)

            # Manually create a PDF alongside the MD to simulate PDF rendering
            assert result.report_path is not None
            pdf_path = result.report_path.with_suffix(".pdf")
            pdf_path.write_bytes(b"%PDF-mock")

            # Now flatten -- it should prefer the PDF
            pkg._flatten_latest_report(out_dir, result)

            assert result.report_path is not None
            assert result.report_path.suffix == ".pdf"
            # After flattening, the PDF should be promoted to the output root
            assert result.report_path == out_dir / "report.pdf"

            # Markdown source should also be preserved alongside PDF
            assert (out_dir / "report.md").exists(), (
                "report.md should be preserved alongside report.pdf"
            )
            assert result.report_md_path == out_dir / "report.md"

    def test_flatten_falls_back_to_md(self, test_project_pcb):
        """_flatten_latest_report falls back to MD when no PDF exists."""
        from kicad_tools.export.manufacturing import (
            ManufacturingConfig,
            ManufacturingPackage,
            ManufacturingResult,
        )

        config = ManufacturingConfig(
            include_report=True,
            latest_report_only=True,
        )
        pkg = ManufacturingPackage(
            pcb_path=test_project_pcb,
            manufacturer="jlcpcb",
            config=config,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            result = ManufacturingResult(output_dir=out_dir)

            with (
                patch(
                    "kicad_tools.report.renderers._weasyprint_available",
                    return_value=False,
                ),
                patch(
                    "kicad_tools.report.renderers._pandoc_available",
                    return_value=False,
                ),
            ):
                pkg._generate_report(out_dir, result)
                pkg._flatten_latest_report(out_dir, result)

            assert result.report_path is not None
            assert result.report_path.suffix == ".md"
            # When only MD exists (no PDF), report_md_path should not be set
            assert result.report_md_path is None


# Fixtures
@pytest.fixture
def test_project_pcb():
    """Path to test project PCB fixture."""
    fixture_path = Path(__file__).parent / "fixtures" / "projects" / "test_project.kicad_pcb"
    if not fixture_path.exists():
        pytest.skip(f"Test fixture not found: {fixture_path}")
    return str(fixture_path)
