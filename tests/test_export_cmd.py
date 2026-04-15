"""Tests for the kct export CLI command."""

from pathlib import Path

from kicad_tools.cli.export_cmd import main as export_main


class TestExportCmdParsing:
    """Tests for CLI argument parsing and error handling."""

    def test_missing_pcb_file_returns_error(self, tmp_path):
        """Non-existent PCB file should produce exit code 1."""
        fake_pcb = str(tmp_path / "nonexistent.kicad_pcb")
        rc = export_main([fake_pcb])
        assert rc == 1

    def test_dry_run_no_files_created(self, tmp_path):
        """--dry-run should not create the output directory."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        out_dir = tmp_path / "output"
        rc = export_main([str(pcb), "--dry-run", "-o", str(out_dir)])

        assert rc == 0
        assert not out_dir.exists()

    def test_dry_run_output_lists_files(self, tmp_path, capsys):
        """--dry-run should print the files that would be generated."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        rc = export_main([str(pcb), "--dry-run", "-o", str(tmp_path / "out")])
        assert rc == 0

        captured = capsys.readouterr()
        assert "Dry run" in captured.out
        assert "bom_jlcpcb.csv" in captured.out
        assert "cpl_jlcpcb.csv" in captured.out
        assert "manifest.json" in captured.out
        assert "kicad_project.zip" in captured.out


class TestExportCmdIntegration:
    """Integration tests that mock the assembly pipeline."""

    def test_full_export_with_mocked_assembly(self, tmp_path, monkeypatch):
        """Test full export pipeline with mocked assembly."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = project_dir / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        (project_dir / "board.kicad_sch").write_text("(kicad_sch)")
        (project_dir / "board.kicad_pro").write_text("{}")

        from kicad_tools.export import assembly

        def fake_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            bom_path = od / "bom_jlcpcb.csv"
            bom_path.write_text("Comment,Designator,Footprint,LCSC Part #\n")
            cpl_path = od / "cpl_jlcpcb.csv"
            cpl_path.write_text("Designator,Val,Package,Mid X,Mid Y,Rotation,Layer\n")
            return assembly.AssemblyPackageResult(
                output_dir=od,
                bom_path=bom_path,
                pnp_path=cpl_path,
            )

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_export)

        out_dir = tmp_path / "manufacturing"

        rc = export_main(
            [
                str(pcb),
                "--mfr",
                "jlcpcb",
                "-o",
                str(out_dir),
                "--no-report",
            ]
        )

        assert rc == 0
        assert out_dir.exists()

        # Check generated files
        assert (out_dir / "bom_jlcpcb.csv").exists()
        assert (out_dir / "cpl_jlcpcb.csv").exists()
        assert (out_dir / "kicad_project.zip").exists()
        assert (out_dir / "manifest.json").exists()

    def test_no_bom_flag(self, tmp_path, monkeypatch):
        """--no-bom should skip BOM generation."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = project_dir / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        from kicad_tools.export import assembly

        def fake_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            result = assembly.AssemblyPackageResult(output_dir=od)
            # BOM should not be generated since include_bom is False
            assert not self.config.include_bom
            return result

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_export)

        rc = export_main(
            [
                str(pcb),
                "--no-bom",
                "--no-report",
                "--no-project-zip",
                "-o",
                str(tmp_path / "out"),
            ]
        )
        assert rc == 0

    def test_manufacturer_pcbway(self, tmp_path, monkeypatch):
        """--mfr pcbway should be passed through correctly."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = project_dir / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        from kicad_tools.export import assembly

        captured_manufacturer = {}

        original_init = assembly.AssemblyPackage.__init__

        def spy_init(self, pcb_path, schematic_path=None, manufacturer="jlcpcb", config=None):
            captured_manufacturer["value"] = manufacturer
            original_init(self, pcb_path, schematic_path, manufacturer, config)

        def fake_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            return assembly.AssemblyPackageResult(output_dir=od)

        monkeypatch.setattr(assembly.AssemblyPackage, "__init__", spy_init)
        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_export)

        rc = export_main(
            [
                str(pcb),
                "--mfr",
                "pcbway",
                "--no-report",
                "--no-project-zip",
                "-o",
                str(tmp_path / "out"),
            ]
        )
        assert rc == 0
        assert captured_manufacturer["value"] == "pcbway"


class TestExportCmdFromMainParser:
    """Tests that the export command is properly wired into the main kct parser."""

    def test_parser_has_export_command(self):
        """The main parser should recognize 'export' as a subcommand."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["export", "board.kicad_pcb"])
        assert args.command == "export"
        assert args.export_pcb == "board.kicad_pcb"

    def test_parser_export_defaults(self):
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["export", "board.kicad_pcb"])
        assert args.export_mfr == "jlcpcb"
        assert args.export_output is None
        assert args.export_dry_run is False
        assert args.export_no_report is False

    def test_parser_export_all_flags(self):
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "export",
                "board.kicad_pcb",
                "--mfr",
                "pcbway",
                "-o",
                "/tmp/out",
                "--sch",
                "board.kicad_sch",
                "--dry-run",
                "--no-report",
                "--no-gerbers",
                "--no-bom",
                "--no-cpl",
                "--no-project-zip",
            ]
        )
        assert args.export_mfr == "pcbway"
        assert args.export_output == "/tmp/out"
        assert args.export_sch == "board.kicad_sch"
        assert args.export_dry_run is True
        assert args.export_no_report is True
        assert args.export_no_gerbers is True
        assert args.export_no_bom is True
        assert args.export_no_cpl is True
        assert args.export_no_project_zip is True

    def test_dispatch_wiring(self, tmp_path, monkeypatch):
        """Verify that 'kct export ...' dispatches to export_cmd.main."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        from kicad_tools.cli import main as cli_main

        # Just test dry-run to avoid needing kicad-cli
        rc = cli_main(["export", str(pcb), "--dry-run", "-o", str(tmp_path / "out")])
        assert rc == 0
