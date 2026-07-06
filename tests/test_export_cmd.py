"""Tests for the kct export CLI command."""

from pathlib import Path

from kicad_tools.cli.export_cmd import _find_pcb_for_export
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
                "--skip-preflight",
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
                "--skip-preflight",
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
        # Sibling schematic is required for the default schematic-sourced
        # BOM path -- AssemblyPackage now fails fast without it.
        (project_dir / "board.kicad_sch").write_text("(kicad_sch)")

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
                "--skip-preflight",
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
        assert args.export_auto_lcsc is True
        assert args.export_no_auto_lcsc is False

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
                "--include-tht",
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
        assert args.export_include_tht is True

    def test_parser_export_no_auto_lcsc_flag(self):
        """--no-auto-lcsc should set export_no_auto_lcsc to True."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["export", "board.kicad_pcb", "--no-auto-lcsc"])
        assert args.export_no_auto_lcsc is True
        # --auto-lcsc default is still True (separate dest)
        assert args.export_auto_lcsc is True

    def test_parser_export_auto_lcsc_flag(self):
        """--auto-lcsc should be explicitly recognized."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["export", "board.kicad_pcb", "--auto-lcsc"])
        assert args.export_auto_lcsc is True
        assert args.export_no_auto_lcsc is False

    def test_dispatch_forwards_no_auto_lcsc(self, tmp_path, monkeypatch):
        """--no-auto-lcsc should be forwarded through dispatch to export_cmd."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        captured_argv = {}

        import kicad_tools.cli.export_cmd as export_mod

        original_main = export_mod.main

        def spy_main(argv=None):
            captured_argv["value"] = argv
            return original_main(argv)

        monkeypatch.setattr("kicad_tools.cli.export_cmd.main", spy_main)

        from kicad_tools.cli import main as cli_main

        rc = cli_main(
            ["export", str(pcb), "--no-auto-lcsc", "--dry-run", "-o", str(tmp_path / "out")]
        )
        assert rc == 0
        assert "--no-auto-lcsc" in captured_argv["value"]

    def test_parser_strict_preflight_flag(self):
        """--strict-preflight should be parsed correctly."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["export", "board.kicad_pcb", "--strict-preflight"])
        assert args.export_strict_preflight is True

    def test_parser_strict_preflight_default_false(self):
        """--strict-preflight should default to False."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["export", "board.kicad_pcb"])
        assert args.export_strict_preflight is False

    def test_dispatch_forwards_strict_preflight(self, tmp_path, monkeypatch):
        """--strict-preflight should be forwarded through dispatch to export_cmd."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        captured_argv = {}

        import kicad_tools.cli.export_cmd as export_mod

        original_main = export_mod.main

        def spy_main(argv=None):
            captured_argv["value"] = argv
            return original_main(argv)

        monkeypatch.setattr("kicad_tools.cli.export_cmd.main", spy_main)

        from kicad_tools.cli import main as cli_main

        rc = cli_main(
            ["export", str(pcb), "--strict-preflight", "--dry-run", "-o", str(tmp_path / "out")]
        )
        assert rc == 0
        assert "--strict-preflight" in captured_argv["value"]

    def test_dispatch_wiring(self, tmp_path, monkeypatch):
        """Verify that 'kct export ...' dispatches to export_cmd.main."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        from kicad_tools.cli import main as cli_main

        # Just test dry-run to avoid needing kicad-cli
        rc = cli_main(["export", str(pcb), "--dry-run", "-o", str(tmp_path / "out")])
        assert rc == 0


class TestExportCmdStrictPreflight:
    """Tests for --strict-preflight flag behavior."""

    def test_strict_preflight_parsed(self, tmp_path):
        """--strict-preflight flag should be parsed by export_cmd."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        # dry-run to avoid needing kicad-cli
        rc = export_main([str(pcb), "--strict-preflight", "--dry-run", "-o", str(tmp_path / "out")])
        assert rc == 0


class TestExportCmdIncludeTHT:
    """Tests for the --include-tht CLI flag."""

    def test_include_tht_flag_accepted(self, tmp_path):
        """--include-tht should be a recognized flag."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        rc = export_main([str(pcb), "--include-tht", "--dry-run", "-o", str(tmp_path / "out")])
        assert rc == 0

    def test_include_tht_sets_pnp_config(self, tmp_path, monkeypatch):
        """--include-tht should set exclude_tht=False on PnPExportConfig."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = project_dir / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        # Sibling schematic is required for the default schematic-sourced
        # BOM path -- AssemblyPackage now fails fast without it.
        (project_dir / "board.kicad_sch").write_text("(kicad_sch)")

        from kicad_tools.export import assembly

        captured_config = {}

        original_init = assembly.AssemblyPackage.__init__

        def spy_init(self, pcb_path, schematic_path=None, manufacturer="jlcpcb", config=None):
            captured_config["pnp_config"] = config.pnp_config if config else None
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
                "--include-tht",
                "--no-report",
                "--no-project-zip",
                "--skip-preflight",
                "-o",
                str(tmp_path / "out"),
            ]
        )
        assert rc == 0
        assert captured_config["pnp_config"] is not None
        assert captured_config["pnp_config"].exclude_tht is False

    def test_without_include_tht_no_pnp_config(self, tmp_path, monkeypatch):
        """Without --include-tht, pnp_config should be None (formatter default applies)."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = project_dir / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        # Sibling schematic is required for the default schematic-sourced
        # BOM path -- AssemblyPackage now fails fast without it.
        (project_dir / "board.kicad_sch").write_text("(kicad_sch)")

        from kicad_tools.export import assembly

        captured_config = {}

        original_init = assembly.AssemblyPackage.__init__

        def spy_init(self, pcb_path, schematic_path=None, manufacturer="jlcpcb", config=None):
            captured_config["pnp_config"] = config.pnp_config if config else None
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
                "--no-report",
                "--no-project-zip",
                "--skip-preflight",
                "-o",
                str(tmp_path / "out"),
            ]
        )
        assert rc == 0
        assert captured_config["pnp_config"] is None


class TestFindPcbForExport:
    """Tests for the _find_pcb_for_export helper function."""

    def test_prefers_routed_file(self, tmp_path):
        """When both routed and unrouted PCB files exist, prefer the routed one."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        unrouted = tmp_path / "board.kicad_pcb"
        unrouted.write_text("(kicad_pcb)")
        routed = output_dir / "board_routed.kicad_pcb"
        routed.write_text("(kicad_pcb)")

        result = _find_pcb_for_export(tmp_path)
        assert result == routed

    def test_falls_back_to_unrouted(self, tmp_path):
        """When only an unrouted PCB file exists, use it."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        result = _find_pcb_for_export(tmp_path)
        assert result == pcb

    def test_returns_none_for_empty_directory(self, tmp_path):
        """Empty directory should return None."""
        result = _find_pcb_for_export(tmp_path)
        assert result is None

    def test_ignores_backup_files(self, tmp_path):
        """Backup files (*-bak.kicad_pcb) should be excluded."""
        bak = tmp_path / "board-bak.kicad_pcb"
        bak.write_text("(kicad_pcb)")

        result = _find_pcb_for_export(tmp_path)
        assert result is None

    def test_only_backup_files_returns_none(self, tmp_path):
        """Directory with only backup files should return None."""
        (tmp_path / "board-bak.kicad_pcb").write_text("(kicad_pcb)")
        (tmp_path / "other-bak.kicad_pcb").write_text("(kicad_pcb)")

        result = _find_pcb_for_export(tmp_path)
        assert result is None

    def test_searches_subdirectories(self, tmp_path):
        """Should find PCB files in nested subdirectories."""
        sub = tmp_path / "output"
        sub.mkdir()
        pcb = sub / "board_routed.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        result = _find_pcb_for_export(tmp_path)
        assert result == pcb


class TestExportCmdDirectoryInput:
    """Tests for directory path input to kct export."""

    def test_directory_with_routed_pcb_dry_run(self, tmp_path):
        """Directory containing a routed PCB should work with --dry-run."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        routed = output_dir / "board_routed.kicad_pcb"
        routed.write_text("(kicad_pcb)")

        rc = export_main([str(tmp_path), "--dry-run", "-o", str(tmp_path / "mfg")])
        assert rc == 0

    def test_directory_with_only_unrouted_pcb_dry_run(self, tmp_path):
        """Directory with only unrouted PCB should work."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        rc = export_main([str(tmp_path), "--dry-run", "-o", str(tmp_path / "mfg")])
        assert rc == 0

    def test_directory_with_no_pcb_returns_error(self, tmp_path, capsys):
        """Empty directory should return exit code 1 with helpful message."""
        rc = export_main([str(tmp_path)])
        assert rc == 1

        captured = capsys.readouterr()
        assert "No .kicad_pcb file found" in captured.err
        assert "Hint:" in captured.err

    def test_explicit_pcb_path_still_works(self, tmp_path):
        """Direct file path should behave as before (regression check)."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        rc = export_main([str(pcb), "--dry-run", "-o", str(tmp_path / "mfg")])
        assert rc == 0

    def test_wrong_file_extension_returns_error(self, tmp_path, capsys):
        """Non-.kicad_pcb file should return exit code 1."""
        wrong = tmp_path / "board.txt"
        wrong.write_text("not a pcb")

        rc = export_main([str(wrong)])
        assert rc == 1

        captured = capsys.readouterr()
        assert "Expected .kicad_pcb file" in captured.err

    def test_directory_prefers_routed_over_unrouted(self, tmp_path, capsys):
        """When both routed and unrouted exist, routed is selected for export."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        (tmp_path / "board.kicad_pcb").write_text("(kicad_pcb)")
        routed = output_dir / "board_routed.kicad_pcb"
        routed.write_text("(kicad_pcb)")

        rc = export_main([str(tmp_path), "--dry-run", "-o", str(tmp_path / "mfg")])
        assert rc == 0

        captured = capsys.readouterr()
        # The dry-run output should reference the output directory,
        # confirming we got past the directory resolution step
        assert "Dry run" in captured.out


class TestExportCmdKeepVersions:
    """Tests for --keep-versions CLI flag."""

    def test_keep_versions_flag_accepted(self, tmp_path):
        """--keep-versions should be a recognized flag."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        rc = export_main([str(pcb), "--keep-versions", "--dry-run", "-o", str(tmp_path / "out")])
        assert rc == 0

    def test_default_is_flat_output(self, tmp_path, monkeypatch):
        """Without --keep-versions, latest_report_only should be True (flat output)."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = project_dir / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        from kicad_tools.export.manufacturing import ManufacturingPackage

        captured_config = {}

        original_init = ManufacturingPackage.__init__

        def spy_init(self, pcb_path, schematic_path=None, manufacturer="jlcpcb", config=None):
            captured_config["latest_report_only"] = config.latest_report_only if config else None
            original_init(self, pcb_path, schematic_path, manufacturer, config)

        def fake_export(self, output_dir=None, *, dry_run=False):
            from kicad_tools.export.manufacturing import ManufacturingResult

            od = Path(output_dir) if output_dir else self.config.output_dir
            return ManufacturingResult(output_dir=od)

        monkeypatch.setattr(ManufacturingPackage, "__init__", spy_init)
        monkeypatch.setattr(ManufacturingPackage, "export", fake_export)

        rc = export_main([str(pcb), "--no-report", "--skip-preflight", "-o", str(tmp_path / "out")])
        assert rc == 0
        assert captured_config["latest_report_only"] is True


class TestExportCmdKeepGerberFiles:
    """Tests for --keep-gerber-files CLI flag."""

    def test_keep_gerber_files_flag_accepted(self, tmp_path):
        """--keep-gerber-files should be a recognized flag."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        rc = export_main(
            [str(pcb), "--keep-gerber-files", "--dry-run", "-o", str(tmp_path / "out")]
        )
        assert rc == 0

    def test_keep_gerber_files_sets_gerber_config(self, tmp_path, monkeypatch):
        """--keep-gerber-files should set clean_after_zip=False on GerberConfig."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = project_dir / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        from kicad_tools.export.manufacturing import ManufacturingPackage

        captured_config = {}

        original_init = ManufacturingPackage.__init__

        def spy_init(self, pcb_path, schematic_path=None, manufacturer="jlcpcb", config=None):
            captured_config["gerber_config"] = config.gerber_config if config else None
            original_init(self, pcb_path, schematic_path, manufacturer, config)

        def fake_export(self, output_dir=None, *, dry_run=False):
            from kicad_tools.export.manufacturing import ManufacturingResult

            od = Path(output_dir) if output_dir else self.config.output_dir
            return ManufacturingResult(output_dir=od)

        monkeypatch.setattr(ManufacturingPackage, "__init__", spy_init)
        monkeypatch.setattr(ManufacturingPackage, "export", fake_export)

        rc = export_main(
            [
                str(pcb),
                "--keep-gerber-files",
                "--no-report",
                "--skip-preflight",
                "-o",
                str(tmp_path / "out"),
            ]
        )
        assert rc == 0
        assert captured_config["gerber_config"] is not None
        assert captured_config["gerber_config"].clean_after_zip is False


class TestExportCmdSchematicAutoDiscovery:
    """Regression tests for issue #2741 -- sibling .kicad_sch discovery.

    The original symptom was 'absolute --pcb path yields 6/8 files instead
    of 8'.  The curator narrowed the real bug to two underlying gaps:

    1. ``find_schematic`` was assumed to be CWD-sensitive (it is not).
       This test confirms an absolute PCB path resolves the sibling
       schematic identically whether the CWD is the board dir or not.

    2. When the schematic genuinely cannot be auto-discovered (e.g. the
       PCB was copied to a directory with no sibling schematic), the old
       code silently wrote a partial package (Gerbers/CPL/Report/etc.) to
       disk and then surfaced a BOM error in stderr.  The fix makes
       ``AssemblyPackage.__init__`` fail fast, so nothing is written.
    """

    def test_absolute_pcb_path_outside_cwd_produces_full_package(self, tmp_path, monkeypatch):
        """Running kct export with an absolute --pcb path from an unrelated
        CWD must still discover the sibling .kicad_sch and produce a full
        package (no missing BOM).
        """
        # Project lives in board_dir; we will run from cwd_dir (different).
        board_dir = tmp_path / "boards" / "stm32_devboard" / "output"
        board_dir.mkdir(parents=True)
        # _routed suffix exercises the SCHEMATIC_STRIP_SUFFIXES path.
        pcb = board_dir / "stm32_devboard_routed.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        sch = board_dir / "stm32_devboard.kicad_sch"
        sch.write_text("(kicad_sch)")

        cwd_dir = tmp_path / "elsewhere"
        cwd_dir.mkdir()
        monkeypatch.chdir(cwd_dir)

        from kicad_tools.export import assembly

        captured_schematic = {}

        original_init = assembly.AssemblyPackage.__init__

        def spy_init(self, pcb_path, schematic_path=None, manufacturer="jlcpcb", config=None):
            original_init(self, pcb_path, schematic_path, manufacturer, config)
            captured_schematic["value"] = self.schematic_path

        def fake_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            bom_path = od / "bom_jlcpcb.csv"
            bom_path.write_text("Comment,Designator,Footprint,LCSC Part #\n")
            cpl_path = od / "cpl_jlcpcb.csv"
            cpl_path.write_text("Designator\n")
            return assembly.AssemblyPackageResult(
                output_dir=od,
                bom_path=bom_path,
                pnp_path=cpl_path,
            )

        monkeypatch.setattr(assembly.AssemblyPackage, "__init__", spy_init)
        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_export)

        out_dir = tmp_path / "out_abs"
        rc = export_main(
            [
                str(pcb.resolve()),  # absolute, outside CWD
                "--no-report",
                "--skip-preflight",
                "-o",
                str(out_dir),
            ]
        )

        assert rc == 0
        # The schematic was auto-discovered via _routed stripping
        assert captured_schematic["value"] == sch
        # Manifest contains the BOM (the original missing artefact)
        assert (out_dir / "bom_jlcpcb.csv").exists()
        assert (out_dir / "manifest.json").exists()

    def test_missing_sibling_schematic_fails_fast(self, tmp_path, capsys):
        """When a .kicad_pcb has no discoverable sibling .kicad_sch and
        the default schematic-sourced BOM is selected, kct export must:

        - return a non-zero exit code,
        - print an error naming the PCB stem and suggesting --sch /
          --bom-source pcb, and
        - leave no partial package on disk (no Gerbers, no CPL, no
          manifest, no project zip, no README).
        """
        orphan_dir = tmp_path / "orphan"
        orphan_dir.mkdir()
        pcb = orphan_dir / "orphan_pcb.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        # No sibling .kicad_sch, no .kicad_pro -- discovery cannot succeed.

        out_dir = tmp_path / "out"
        rc = export_main(
            [
                str(pcb),
                "--no-report",
                "--skip-preflight",
                "-o",
                str(out_dir),
            ]
        )

        assert rc == 1

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        # Error names the PCB stem and the remediation hints
        assert "Schematic file not found" in combined
        assert "orphan_pcb" in combined
        assert ("--sch" in combined) or ("--bom-source pcb" in combined)

        # No partial package on disk: AssemblyPackage construction failed
        # before mkdir, so nothing should have been written.
        if out_dir.exists():
            written = list(out_dir.iterdir())
            assert written == [], (
                f"Expected no partial package on disk, found: {[p.name for p in written]}"
            )

    def test_missing_schematic_with_bom_source_pcb_succeeds(self, tmp_path, monkeypatch):
        """``--bom-source pcb`` is the documented escape hatch for the
        missing-schematic case.  It must not fail in AssemblyPackage.__init__.
        """
        orphan_dir = tmp_path / "orphan"
        orphan_dir.mkdir()
        pcb = orphan_dir / "orphan_pcb.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        from kicad_tools.export import assembly

        def fake_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            return assembly.AssemblyPackageResult(output_dir=od)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_export)

        out_dir = tmp_path / "out"
        rc = export_main(
            [
                str(pcb),
                "--bom-source",
                "pcb",
                "--no-report",
                "--no-project-zip",
                "--skip-preflight",
                "-o",
                str(out_dir),
            ]
        )
        assert rc == 0

    def test_missing_schematic_with_no_bom_succeeds(self, tmp_path, monkeypatch):
        """``--no-bom`` should likewise bypass the schematic requirement."""
        orphan_dir = tmp_path / "orphan"
        orphan_dir.mkdir()
        pcb = orphan_dir / "orphan_pcb.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        from kicad_tools.export import assembly

        def fake_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            return assembly.AssemblyPackageResult(output_dir=od)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_export)

        out_dir = tmp_path / "out"
        rc = export_main(
            [
                str(pcb),
                "--no-bom",
                "--no-report",
                "--no-project-zip",
                "--skip-preflight",
                "-o",
                str(out_dir),
            ]
        )
        assert rc == 0


class TestExportDRCSafetyFloor:
    """CLI-level tests for the connectivity safety floor (#3912)."""

    @staticmethod
    def _patch_assembly(monkeypatch):
        from kicad_tools.export import assembly

        def fake_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            bom_path = od / "bom_jlcpcb.csv"
            bom_path.write_text("Comment,Designator,Footprint,LCSC Part #\n")
            return assembly.AssemblyPackageResult(output_dir=od, bom_path=bom_path)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_export)

    @staticmethod
    def _write_shorting_drc_report(path: Path) -> None:
        import json

        path.write_text(
            json.dumps(
                {
                    "file": "board.kicad_pcb",
                    "manufacturer": "jlcpcb",
                    "summary": {"errors": 1, "warnings": 0, "passed": False},
                    "violations": [
                        {
                            "rule_id": "shorting_items",
                            "severity": "error",
                            "message": "Two items share a net (GND / PHASE_A)",
                            "items": ["Track [GND]", "Track [PHASE_A]"],
                        }
                    ],
                }
            )
        )

    def test_skip_preflight_blocks_on_drc_shorts(self, tmp_path, monkeypatch):
        """`kct export --skip-preflight` on a shorted board must return 1."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = project_dir / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        (project_dir / "board.kicad_sch").write_text("(kicad_sch)")
        self._write_shorting_drc_report(project_dir / "drc_report.json")

        self._patch_assembly(monkeypatch)

        out_dir = tmp_path / "manufacturing"
        rc = export_main(
            [
                str(pcb),
                "--mfr",
                "jlcpcb",
                "-o",
                str(out_dir),
                "--no-report",
                "--skip-preflight",
            ]
        )

        assert rc == 1, "Export must exit non-zero on net shorts"
        # No manifest should have been written -- export aborted before assembly.
        assert not (out_dir / "manifest.json").exists()

    def test_skip_drc_floor_allows_shorted_export(self, tmp_path, monkeypatch):
        """`--skip-drc-floor` bypasses the floor and exits 0 (escape hatch)."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = project_dir / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        (project_dir / "board.kicad_sch").write_text("(kicad_sch)")
        self._write_shorting_drc_report(project_dir / "drc_report.json")

        self._patch_assembly(monkeypatch)

        out_dir = tmp_path / "manufacturing"
        rc = export_main(
            [
                str(pcb),
                "--mfr",
                "jlcpcb",
                "-o",
                str(out_dir),
                "--no-report",
                "--skip-preflight",
                "--skip-drc-floor",
            ]
        )

        assert rc == 0, "Escape hatch should allow export despite shorts"
        assert (out_dir / "manifest.json").exists()

    def test_skip_preflight_clean_board_still_exports(self, tmp_path, monkeypatch):
        """`--skip-preflight` on a board with no DRC report exits 0 (no regression)."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = project_dir / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        (project_dir / "board.kicad_sch").write_text("(kicad_sch)")
        # No drc_report.json -- floor is a no-op.

        self._patch_assembly(monkeypatch)

        out_dir = tmp_path / "manufacturing"
        rc = export_main(
            [
                str(pcb),
                "--mfr",
                "jlcpcb",
                "-o",
                str(out_dir),
                "--no-report",
                "--skip-preflight",
            ]
        )

        assert rc == 0
        assert (out_dir / "manifest.json").exists()

    def test_parser_skip_drc_floor_flag(self):
        """--skip-drc-floor parses to export_skip_drc_floor=True."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["export", "board.kicad_pcb", "--skip-drc-floor"])
        assert args.export_skip_drc_floor is True

    def test_parser_skip_drc_floor_default_false(self):
        """--skip-drc-floor defaults to False."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["export", "board.kicad_pcb"])
        assert args.export_skip_drc_floor is False

    def test_dispatch_forwards_skip_drc_floor(self, tmp_path, monkeypatch):
        """--skip-drc-floor should be forwarded through dispatch to export_cmd."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        captured = {}

        def spy_main(argv=None):
            captured["argv"] = argv
            return 0

        monkeypatch.setattr("kicad_tools.cli.export_cmd.main", spy_main)

        from kicad_tools.cli import main as cli_main

        cli_main(["export", str(pcb), "--skip-drc-floor"])
        assert "--skip-drc-floor" in captured["argv"]
