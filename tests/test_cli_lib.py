"""Tests for kicad_tools lib CLI commands."""

import json

import pytest

from kicad_tools.schema import SymbolLibrary


def _kicad_installed() -> bool:
    """Check if KiCad standard libraries are available."""
    from kicad_tools.schematic.grid import get_symbol_search_paths

    return any(
        path.exists() and any(path.glob("*.kicad_sym")) for path in get_symbol_search_paths()
    )


class TestLibListCommand:
    """Tests for the lib list command."""

    def test_lib_list_no_options(self, capsys):
        """Test lib list with no options shows both symbol and footprint libraries."""
        from kicad_tools.cli import main

        result = main(["lib", "list"])
        assert result == 0

        captured = capsys.readouterr()
        assert "Symbol Libraries" in captured.out
        assert "Footprint Libraries" in captured.out

    def test_lib_list_symbols_only(self, capsys):
        """Test lib list --symbols shows only symbol libraries."""
        from kicad_tools.cli import main

        result = main(["lib", "list", "--symbols"])
        assert result == 0

        captured = capsys.readouterr()
        assert "Symbol Libraries" in captured.out
        assert "Footprint Libraries" not in captured.out

    def test_lib_list_footprints_only(self, capsys):
        """Test lib list --footprints shows only footprint libraries."""
        from kicad_tools.cli import main

        result = main(["lib", "list", "--footprints"])
        assert result == 0

        captured = capsys.readouterr()
        assert "Footprint Libraries" in captured.out
        assert "Symbol Libraries" not in captured.out

    def test_lib_list_json_format(self, capsys):
        """Test lib list with JSON output."""
        from kicad_tools.cli import main

        result = main(["lib", "list", "--format", "json"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "symbols" in data or "footprints" in data


class TestLibSymbolInfoCommand:
    """Tests for the lib symbol-info command."""

    def test_symbol_info_not_found_library(self, capsys):
        """Test symbol-info with non-existent library."""
        from kicad_tools.cli import main

        result = main(["lib", "symbol-info", "NonExistent", "R"])
        assert result == 1

        captured = capsys.readouterr()
        assert "not found" in captured.err.lower()

    def test_symbol_info_not_found_symbol(self, capsys):
        """Test symbol-info with non-existent symbol in valid library."""
        from kicad_tools.cli import main

        # Device library should exist in standard KiCad installation
        result = main(["lib", "symbol-info", "Device", "NonExistentSymbol"])

        # May succeed or fail depending on KiCad installation
        # If library found, symbol should not be found
        if result == 1:
            captured = capsys.readouterr()
            assert "not found" in captured.err.lower() or "not found" in captured.out.lower()

    @pytest.mark.skipif(
        not _kicad_installed(),
        reason="KiCad symbols not installed",
    )
    def test_symbol_info_device_r(self, capsys):
        """Test symbol-info for Device:R (common resistor symbol)."""
        from kicad_tools.cli import main

        result = main(["lib", "symbol-info", "Device", "R"])
        assert result == 0

        captured = capsys.readouterr()
        assert "Symbol: R" in captured.out
        assert "Pin count:" in captured.out

    @pytest.mark.skipif(
        not _kicad_installed(),
        reason="KiCad symbols not installed",
    )
    def test_symbol_info_json_format(self, capsys):
        """Test symbol-info with JSON output."""
        from kicad_tools.cli import main

        result = main(["lib", "symbol-info", "Device", "R", "--format", "json"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["name"] == "R"
        assert "pin_count" in data

    @pytest.mark.skipif(
        not _kicad_installed(),
        reason="KiCad symbols not installed",
    )
    def test_symbol_info_with_pins(self, capsys):
        """Test symbol-info with --pins option."""
        from kicad_tools.cli import main

        result = main(["lib", "symbol-info", "Device", "R", "--pins"])
        assert result == 0

        captured = capsys.readouterr()
        assert "Pins:" in captured.out


class TestLibFootprintInfoCommand:
    """Tests for the lib footprint-info command."""

    def test_footprint_info_not_found_library(self, capsys):
        """Test footprint-info with non-existent library."""
        from kicad_tools.cli import main

        result = main(["lib", "footprint-info", "NonExistent", "C_0402"])
        assert result == 1

        captured = capsys.readouterr()
        assert "not found" in captured.err.lower()

    @pytest.mark.skipif(
        not _kicad_installed(),
        reason="KiCad footprints not installed",
    )
    def test_footprint_info_capacitor(self, capsys):
        """Test footprint-info for standard capacitor footprint."""
        from kicad_tools.cli import main

        result = main(["lib", "footprint-info", "Capacitor_SMD", "C_0402_1005Metric"])
        assert result == 0

        captured = capsys.readouterr()
        assert "Footprint:" in captured.out
        assert "Pads:" in captured.out


KICAD_CLI = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"


def _kicad_cli() -> str | None:
    import os
    import shutil

    return shutil.which("kicad-cli") or (KICAD_CLI if os.path.exists(KICAD_CLI) else None)


def _run(argv, capsys):
    from kicad_tools.cli import main

    rc = main(argv)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


class TestCreateSymbolLib:
    def test_create_and_load(self, tmp_path, capsys):
        target = tmp_path / "sub" / "mine.kicad_sym"
        rc, out, _ = _run(["lib", "create-symbol-lib", str(target)], capsys)
        assert rc == 0
        assert "not yet implemented" not in out.lower()
        assert len(SymbolLibrary.load(str(target)).symbols) == 0

    def test_suffix_appended_and_json(self, tmp_path, capsys):
        rc, out, _ = _run(
            ["lib", "create-symbol-lib", str(tmp_path / "x"), "--format", "json"], capsys
        )
        assert rc == 0
        doc = json.loads(out)
        assert doc["implemented"] is True and doc["success"] is True
        assert doc["symbol_count"] == 0
        assert doc["path"].endswith("x.kicad_sym")
        assert (tmp_path / "x.kicad_sym").exists()

    def test_exists_requires_force(self, tmp_path, capsys):
        target = tmp_path / "a.kicad_sym"
        target.write_text("keep me")
        rc, out, err = _run(["lib", "create-symbol-lib", str(target)], capsys)
        assert rc == 1
        assert "--force" in err
        assert target.read_text() == "keep me"
        rc, _, _ = _run(["lib", "create-symbol-lib", str(target), "--force"], capsys)
        assert rc == 0
        SymbolLibrary.load(str(target))

    def test_exists_json_error(self, tmp_path, capsys):
        target = tmp_path / "a.kicad_sym"
        target.write_text("x")
        rc, out, _ = _run(["lib", "create-symbol-lib", str(target), "--format", "json"], capsys)
        assert rc == 1
        doc = json.loads(out)
        assert doc["success"] is False and "error" in doc

    def test_kicad_cli_accepts(self, tmp_path, capsys):
        import subprocess

        cli = _kicad_cli()
        if cli is None:
            pytest.skip("kicad-cli not available")
        target = tmp_path / "a.kicad_sym"
        assert _run(["lib", "create-symbol-lib", str(target)], capsys)[0] == 0
        res = subprocess.run(
            [cli, "sym", "export", "svg", "-o", str(tmp_path / "svg"), str(target)],
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, res.stderr


class TestCreateFootprintLib:
    def test_create_and_load(self, tmp_path, capsys):
        from kicad_tools.pcb.footprints import FootprintLibrary

        rc, out, _ = _run(["lib", "create-footprint-lib", str(tmp_path / "My")], capsys)
        assert rc == 0
        assert (tmp_path / "My.pretty").is_dir()
        assert FootprintLibrary.load(tmp_path / "My.pretty").footprints == []

    def test_idempotent_on_empty_dir_json(self, tmp_path, capsys):
        (tmp_path / "E.pretty").mkdir()
        rc, out, _ = _run(
            ["lib", "create-footprint-lib", str(tmp_path / "E.pretty"), "--format", "json"], capsys
        )
        assert rc == 0
        doc = json.loads(out)
        assert doc["footprint_count"] == 0 and doc["success"] is True

    def test_nonempty_dir_refused_and_preserved(self, tmp_path, capsys):
        d = tmp_path / "N.pretty"
        d.mkdir()
        (d / "notes.txt").write_text("keep")
        for extra in ([], ["--force"]):
            rc, _, err = _run(["lib", "create-footprint-lib", str(d), *extra], capsys)
            assert rc == 1
            assert (d / "notes.txt").read_text() == "keep"

    def test_force_reuses_dir_with_footprints(self, tmp_path, capsys):
        lib = tmp_path / "F.pretty"
        assert (
            _run(["lib", "generate-footprint", str(lib), "chip", "--size", "0402"], capsys)[0] == 0
        )
        assert _run(["lib", "create-footprint-lib", str(lib)], capsys)[0] == 1
        rc, out, _ = _run(
            ["lib", "create-footprint-lib", str(lib), "--force", "--format", "json"], capsys
        )
        assert rc == 0
        assert json.loads(out)["footprint_count"] == 1

    def test_existing_file_refused(self, tmp_path, capsys):
        f = tmp_path / "F.pretty"
        f.write_text("x")
        rc, _, _ = _run(["lib", "create-footprint-lib", str(f)], capsys)
        assert rc == 1


class TestGenerateFootprint:
    def test_generate_roundtrip(self, tmp_path, capsys):
        from kicad_tools.pcb.footprints import FootprintLibrary

        lib = tmp_path / "G.pretty"
        rc, out, _ = _run(
            ["lib", "generate-footprint", str(lib), "soic", "--pins", "8", "--format", "json"],
            capsys,
        )
        assert rc == 0
        doc = json.loads(out)
        assert doc["pad_count"] == 8 and doc["type"] == "soic" and doc["success"] is True
        loaded = FootprintLibrary.load(lib)
        assert len(loaded.footprints) == 1
        assert loaded.footprints[0].name == doc["name"]
        assert len(loaded.footprints[0].pads) == 8

    @pytest.mark.parametrize(
        "argv,pads",
        [
            (["qfp", "--pins", "32"], 32),
            (["qfn", "--pins", "16", "--body-size", "3.0", "--exposed-pad", "1.7"], 17),
            (["chip", "--size", "0603", "--prefix", "R"], 2),
            (["sot", "--variant", "SOT-23"], 3),
        ],
    )
    def test_other_types(self, tmp_path, capsys, argv, pads):
        from kicad_tools.pcb.footprints import FootprintLibrary

        lib = tmp_path / "T.pretty"
        rc, _, err = _run(["lib", "generate-footprint", str(lib), *argv], capsys)
        assert rc == 0, err
        (fp,) = FootprintLibrary.load(lib).footprints
        assert len(fp.pads) == pads

    def test_dfn_not_offered(self, tmp_path):
        from kicad_tools.cli import main

        with pytest.raises(SystemExit) as e:
            main(["lib", "generate-footprint", str(tmp_path / "x.pretty"), "dfn"])
        assert e.value.code == 2

    def test_missing_pins_exit_1(self, tmp_path, capsys):
        rc, _, err = _run(["lib", "generate-footprint", str(tmp_path / "x.pretty"), "soic"], capsys)
        assert rc == 1
        assert "--pins" in err
        assert not (tmp_path / "x.pretty").exists()

    def test_missing_pins_json(self, tmp_path, capsys):
        rc, out, _ = _run(
            ["lib", "generate-footprint", str(tmp_path / "x.pretty"), "qfp", "--format", "json"],
            capsys,
        )
        assert rc == 1
        doc = json.loads(out)
        assert doc["success"] is False and "--pins" in doc["error"]

    def test_overwrite_requires_force(self, tmp_path, capsys):
        lib = tmp_path / "O.pretty"
        argv = ["lib", "generate-footprint", str(lib), "chip", "--size", "0402"]
        assert _run(argv, capsys)[0] == 0
        (path,) = lib.glob("*.kicad_mod")
        path.write_text("sentinel")
        rc, _, err = _run(argv, capsys)
        assert rc == 1 and "--force" in err
        assert path.read_text() == "sentinel"
        assert _run([*argv, "--force"], capsys)[0] == 0
        assert path.read_text() != "sentinel"

    def test_parity_with_footprint_generate(self, tmp_path, capsys):
        from kicad_tools.cli.footprint_generate import main as fp_main

        lib = tmp_path / "P.pretty"
        assert _run(["lib", "generate-footprint", str(lib), "soic", "--pins", "8"], capsys)[0] == 0
        (path,) = lib.glob("*.kicad_mod")
        assert fp_main(["soic", "--pins", "8"]) == 0
        assert capsys.readouterr().out.strip() == path.read_text().strip()

    def test_kicad_cli_accepts(self, tmp_path, capsys):
        import subprocess

        cli = _kicad_cli()
        if cli is None:
            pytest.skip("kicad-cli not available")
        lib = tmp_path / "K.pretty"
        assert _run(["lib", "generate-footprint", str(lib), "soic", "--pins", "8"], capsys)[0] == 0
        out = tmp_path / "svg"
        out.mkdir()
        res = subprocess.run(
            [cli, "fp", "export", "svg", "-o", str(out), str(lib)], capture_output=True, text=True
        )
        assert res.returncode == 0, res.stderr
        assert list(out.glob("*.svg"))


class TestLibExportCommand:
    """Tests for the lib export command."""

    def test_export_not_found(self, capsys):
        """Test export with non-existent file."""
        from kicad_tools.cli import main

        result = main(["lib", "export", "nonexistent.kicad_sym"])
        assert result == 1

        captured = capsys.readouterr()
        assert "not found" in captured.err.lower()

    def test_export_unsupported_type(self, tmp_path, capsys):
        """Test export with unsupported file type."""
        from kicad_tools.cli import main

        # Create a dummy file
        dummy = tmp_path / "test.txt"
        dummy.write_text("test")

        result = main(["lib", "export", str(dummy)])
        assert result == 1

        captured = capsys.readouterr()
        assert "unsupported" in captured.err.lower()
