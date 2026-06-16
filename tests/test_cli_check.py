"""Tests for kct check CLI command (pure Python DRC)."""

import json
import os
import shutil
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_00_DIR = REPO_ROOT / "boards" / "00-simple-led" / "output"
BOARD_00_SCH = BOARD_00_DIR / "simple_led.kicad_sch"
BOARD_00_PCB = BOARD_00_DIR / "simple_led_routed.kicad_pcb"
BOARD_00_MANIFEST = BOARD_00_DIR / "manufacturing" / "manifest.json"


class TestCheckCommand:
    """Tests for the check CLI command."""

    def test_check_file_not_found(self, capsys):
        """Test check command with missing file."""
        from kicad_tools.cli.check_cmd import main

        result = main(["nonexistent.kicad_pcb"])
        assert result == 1

        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "Error" in captured.err

    def test_check_wrong_extension(self, capsys, tmp_path: Path):
        """Test check command with wrong file extension."""
        from kicad_tools.cli.check_cmd import main

        wrong_file = tmp_path / "test.txt"
        wrong_file.write_text("not a pcb")

        result = main([str(wrong_file)])
        assert result == 1

        captured = capsys.readouterr()
        assert ".kicad_pcb" in captured.err

    def test_check_basic_table_output(self, drc_clean_pcb: Path, capsys):
        """Test check command with table output format."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb)])
        assert result == 0

        captured = capsys.readouterr()
        assert "PURE PYTHON DRC CHECK" in captured.out
        assert "DRC PASSED" in captured.out or "Results:" in captured.out

    def test_check_json_output(self, drc_clean_pcb: Path, capsys):
        """Test check command with JSON output format."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb), "--format", "json"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # Verify JSON structure
        assert "file" in data
        assert "manufacturer" in data
        assert "layers" in data
        assert "summary" in data
        assert "violations" in data
        assert "passed" in data["summary"]
        assert data["summary"]["passed"] is True  # No violations with clean PCB

    def test_check_summary_output(self, drc_clean_pcb: Path, capsys):
        """Test check command with summary output format."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb), "--format", "summary"])
        assert result == 0

        captured = capsys.readouterr()
        assert "DRC" in captured.out

    def test_check_manufacturer_option(self, drc_clean_pcb: Path, capsys):
        """Test check command with manufacturer option."""
        from kicad_tools.cli.check_cmd import main

        # Test with different manufacturers
        for mfr in ["jlcpcb", "seeed", "pcbway", "oshpark"]:
            result = main([str(drc_clean_pcb), "--mfr", mfr])
            assert result == 0, f"Failed for manufacturer {mfr}"

    def test_check_layers_option(self, drc_clean_pcb: Path, capsys):
        """Test check command with layers option."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb), "--layers", "4"])
        assert result == 0

        captured = capsys.readouterr()
        assert "4" in captured.out  # Layer count should appear in output

    def test_check_only_filter(self, drc_clean_pcb: Path, capsys):
        """Test check command with --only filter."""
        from kicad_tools.cli.check_cmd import main

        # Run only clearance checks
        result = main([str(drc_clean_pcb), "--only", "clearance"])
        assert result == 0

        # Run multiple categories
        result = main([str(drc_clean_pcb), "--only", "clearance,dimensions"])
        assert result == 0

    def test_check_skip_filter(self, drc_clean_pcb: Path, capsys):
        """Test check command with --skip filter."""
        from kicad_tools.cli.check_cmd import main

        # Skip silkscreen checks
        result = main([str(drc_clean_pcb), "--skip", "silkscreen"])
        assert result == 0

        # Skip multiple categories
        result = main([str(drc_clean_pcb), "--skip", "silkscreen,edge"])
        assert result == 0

    def test_check_invalid_filter_category(self, minimal_pcb: Path, capsys):
        """Test check command with invalid filter category."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(minimal_pcb), "--only", "invalid_category"])
        assert result == 1

        captured = capsys.readouterr()
        assert "Unknown check category" in captured.err

    def test_check_errors_only_flag(self, drc_clean_pcb: Path, capsys):
        """Test check command with --errors-only flag."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb), "--errors-only"])
        assert result == 0  # No errors with clean PCB

    def test_check_verbose_flag(self, drc_clean_pcb: Path, capsys):
        """Test check command with --verbose flag."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb), "--verbose"])
        assert result == 0

    def test_check_copper_weight_option(self, drc_clean_pcb: Path, capsys):
        """Test check command with copper weight option."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb), "--copper", "2.0"])
        assert result == 0

    def test_check_help_text(self, capsys):
        """Test check command help text."""
        from kicad_tools.cli.check_cmd import main

        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "Pure Python DRC" in captured.out or "kct check" in captured.out


class TestCheckOutputFlag:
    """Tests for the --output flag that writes JSON report to file."""

    def test_output_writes_json_file(self, drc_clean_pcb: Path, tmp_path: Path):
        """Test that --output writes a valid JSON report file."""
        from kicad_tools.cli.check_cmd import main

        output_file = tmp_path / "drc_report.json"
        result = main([str(drc_clean_pcb), "--output", str(output_file)])
        assert result == 0

        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert "file" in data
        assert "summary" in data
        assert "violations" in data
        assert data["summary"]["passed"] is True

    def test_output_with_table_format(self, drc_clean_pcb: Path, tmp_path: Path, capsys):
        """Test that --output writes JSON file even with table format (default)."""
        from kicad_tools.cli.check_cmd import main

        output_file = tmp_path / "drc_report.json"
        result = main([str(drc_clean_pcb), "--output", str(output_file)])
        assert result == 0

        # Table output should still go to stdout
        captured = capsys.readouterr()
        assert "PURE PYTHON DRC CHECK" in captured.out

        # JSON file should also be written
        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert data["summary"]["passed"] is True

    def test_output_creates_parent_directories(self, drc_clean_pcb: Path, tmp_path: Path):
        """Test that --output creates parent directories if needed."""
        from kicad_tools.cli.check_cmd import main

        output_file = tmp_path / "subdir" / "nested" / "drc_report.json"
        result = main([str(drc_clean_pcb), "--output", str(output_file)])
        assert result == 0

        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert "summary" in data

    def test_output_without_flag_no_file(self, drc_clean_pcb: Path, tmp_path: Path, capsys):
        """Test that no file is written when --output is not specified."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb)])
        assert result == 0

        # No drc_report.json should exist in the PCB directory
        report = drc_clean_pcb.parent / "drc_report.json"
        assert not report.exists()

    def test_output_with_violations(self, minimal_pcb: Path, tmp_path: Path):
        """Test that --output captures violations in the report file."""
        from kicad_tools.cli.check_cmd import main

        output_file = tmp_path / "drc_report.json"
        result = main([str(minimal_pcb), "--output", str(output_file)])
        assert result == 2  # Violations found

        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert data["summary"]["passed"] is False
        assert data["summary"]["errors"] > 0
        assert len(data["violations"]) > 0


class TestCheckCommandIntegration:
    """Integration tests for check command via main CLI."""

    def test_check_via_main_cli(self, drc_clean_pcb: Path, capsys):
        """Test check command through the main CLI dispatcher."""
        from kicad_tools.cli import main

        result = main(["check", str(drc_clean_pcb)])
        assert result == 0

        captured = capsys.readouterr()
        assert "DRC" in captured.out

    def test_check_via_main_cli_with_options(self, drc_clean_pcb: Path, capsys):
        """Test check command through main CLI with options."""
        from kicad_tools.cli import main

        result = main(
            [
                "check",
                str(drc_clean_pcb),
                "--mfr",
                "seeed",
                "--layers",
                "4",
                "--format",
                "json",
            ]
        )
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["manufacturer"] == "seeed"
        assert data["layers"] == 4


class TestCheckExitCodes:
    """Tests for check command exit codes."""

    def test_exit_code_0_no_violations(self, drc_clean_pcb: Path):
        """Test exit code 0 when no violations found."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb)])
        assert result == 0

    def test_exit_code_0_warnings_only_no_strict(self, drc_clean_pcb: Path):
        """Test exit code 0 with warnings when not in strict mode."""
        from kicad_tools.cli.check_cmd import main

        # With clean PCB, no warnings to test
        # But this confirms the code path works
        result = main([str(drc_clean_pcb)])
        assert result == 0

    def test_exit_code_with_strict_flag(self, drc_clean_pcb: Path):
        """Test that --strict flag is preserved under --drc-only.

        Note (issue #3750): in the default meta-check mode, ``--strict``
        also rolls ``NOT RUN`` sub-checks up to ``FAILED`` (so a fresh
        in-tmp PCB with no sibling schematic exits 2 under ``--strict``).
        The legacy "warnings only matter under --strict, no errors -> 0"
        contract now lives behind ``--drc-only``.
        """
        from kicad_tools.cli.check_cmd import main

        # With clean PCB returning no violations and DRC-only mode,
        # --strict has no effect (no warnings to escalate).
        result = main([str(drc_clean_pcb), "--strict", "--drc-only"])
        assert result == 0

    def test_exit_code_2_with_violations(self, minimal_pcb: Path):
        """Test exit code 2 when DRC violations are found.

        Exit code 2 means the check ran successfully but found errors.
        Exit code 1 is reserved for tool-level failures (file not found, etc.).
        """
        from kicad_tools.cli.check_cmd import main

        # minimal_pcb has a trace overlapping a pad, causing a clearance violation
        result = main([str(minimal_pcb)])
        assert result == 2  # Errors found (tool ran OK, board has issues)

    def test_exit_code_1_for_tool_error(self, capsys):
        """Test exit code 1 for tool-level errors (file not found)."""
        from kicad_tools.cli.check_cmd import main

        result = main(["nonexistent_board.kicad_pcb"])
        assert result == 1  # Tool error


class TestCheckJsonSchema:
    """Tests for check command JSON output schema."""

    def test_json_schema_complete(self, drc_clean_pcb: Path, capsys):
        """Test that JSON output contains all required fields."""
        from kicad_tools.cli.check_cmd import main

        main([str(drc_clean_pcb), "--format", "json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # Required top-level fields
        assert "file" in data
        assert "manufacturer" in data
        assert "layers" in data
        assert "summary" in data
        assert "violations" in data

        # Required summary fields
        assert "errors" in data["summary"]
        assert "warnings" in data["summary"]
        assert "rules_checked" in data["summary"]
        assert "passed" in data["summary"]

        # violations should be a list
        assert isinstance(data["violations"], list)

    def test_json_output_is_ci_friendly(self, drc_clean_pcb: Path, capsys):
        """Test that JSON output can be parsed by CI tools."""
        from kicad_tools.cli.check_cmd import main

        main([str(drc_clean_pcb), "--format", "json"])
        captured = capsys.readouterr()

        # Should be parseable without errors
        data = json.loads(captured.out)

        # CI-friendly check: summary.passed is a boolean
        assert isinstance(data["summary"]["passed"], bool)

        # CI-friendly check: counts are integers
        assert isinstance(data["summary"]["errors"], int)
        assert isinstance(data["summary"]["warnings"], int)


class TestCheckLayerAutoDetection:
    """Tests for automatic copper layer count detection."""

    def test_auto_detect_2_layer_board(self, drc_clean_pcb: Path, capsys):
        """Test that a 2-layer board auto-detects 2 layers (no regression)."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb), "--format", "json"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["layers"] == 2

    def test_auto_detect_4_layer_board(self, four_layer_pcb: Path, capsys):
        """Test that a 4-layer board auto-detects 4 layers without --layers flag."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(four_layer_pcb), "--format", "json"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["layers"] == 4

    def test_auto_detect_6_layer_board(self, six_layer_pcb: Path, capsys):
        """Test that a 6-layer board auto-detects 6 layers."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(six_layer_pcb), "--format", "json"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["layers"] == 6

    def test_explicit_layers_overrides_detection(self, four_layer_pcb: Path, capsys):
        """Test that --layers flag overrides auto-detection."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(four_layer_pcb), "--layers", "2", "--format", "json"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["layers"] == 2

    def test_auto_detect_4_layer_table_output(self, four_layer_pcb: Path, capsys):
        """Test that table output shows auto-detected layer count."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(four_layer_pcb)])
        assert result == 0

        captured = capsys.readouterr()
        assert "Layers: 4" in captured.out

    def test_help_text_mentions_auto_detection(self, capsys):
        """Test that --layers help text indicates auto-detection."""
        from kicad_tools.cli.check_cmd import main

        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "auto-detect" in captured.out.lower()


def _swap_d1_pad_nets_in_pcb(src_pcb: Path, dest_pcb: Path) -> Path:
    """Copy ``src_pcb`` to ``dest_pcb`` with D1's pad nets swapped.

    Mirrors the helper in ``tests/test_board_00_lvs.py`` so the LVS
    sub-check can be exercised against a deliberately mismatched PCB
    without re-running the router.
    """
    from kicad_tools.lvs import _ref_of
    from kicad_tools.sexp import SExp, parse_file

    doc = parse_file(src_pcb)
    for fp in doc.find_all("footprint"):
        if _ref_of(fp) != "D1":
            continue
        pad1 = pad2 = None
        for pad in fp.find_all("pad"):
            num = pad.get_string(0)
            if num == "1":
                pad1 = pad
            elif num == "2":
                pad2 = pad
        assert pad1 is not None and pad2 is not None
        net1 = pad1.find("net")
        net2 = pad2.find("net")
        assert net1 is not None and net2 is not None
        new_net_for_1 = SExp.list("net", net2.get_int(0), net2.get_string(1) or "")
        new_net_for_2 = SExp.list("net", net1.get_int(0), net1.get_string(1) or "")
        for pad, old_net, new_net in (
            (pad1, net1, new_net_for_1),
            (pad2, net2, new_net_for_2),
        ):
            for i, child in enumerate(pad.children):
                if child is old_net:
                    pad.children[i] = new_net
                    break
        break
    dest_pcb.write_text(doc.to_string() + "\n")
    return dest_pcb


def _stage_board00_copy(dest_dir: Path, with_manifest: bool = True) -> tuple[Path, Path]:
    """Copy board 00's committed PCB + schematic into a tmp workspace.

    Mirrors the on-disk layout the meta sub-checks expect
    (``<dir>/<pcb>``, ``<dir>/<basename>.kicad_sch``,
    ``<dir>/manufacturing/manifest.json``) so the helpers can be driven
    without touching the real board fixture.  Returns
    ``(pcb_path, manifest_path)``; ``manifest_path`` is the destination
    even when ``with_manifest`` is False so callers can still introspect.
    """
    if not BOARD_00_SCH.exists() or not BOARD_00_PCB.exists():
        pytest.skip("board 00 artifacts missing; run generate_design.py")

    pcb_dest = dest_dir / "simple_led_routed.kicad_pcb"
    sch_dest = dest_dir / "simple_led.kicad_sch"
    manifest_dest = dest_dir / "manufacturing" / "manifest.json"

    shutil.copy(BOARD_00_PCB, pcb_dest)
    shutil.copy(BOARD_00_SCH, sch_dest)

    if with_manifest:
        manifest_dest.parent.mkdir(parents=True, exist_ok=True)
        if BOARD_00_MANIFEST.exists():
            shutil.copy(BOARD_00_MANIFEST, manifest_dest)
        else:
            manifest_dest.write_text('{"version": "1.0"}\n')
        # Ensure the manifest is at least as new as the PCB so the
        # freshness gate passes (mirrors a real ``kct export`` run).
        now = time.time()
        os.utime(manifest_dest, (now + 1.0, now + 1.0))
        os.utime(pcb_dest, (now, now))
        os.utime(sch_dest, (now, now))

    return pcb_dest, manifest_dest


class TestCheckMetaCheck:
    """Per-sub-check meta output for the default ``kct check`` path (#3750).

    The default (no ``--drc-only``) invocation must print one status line
    for each of DRC / ERC / LVS / Manifest, plus an ``Overall:`` rollup,
    and fold the rollup into the exit code per the spec.
    """

    def test_meta_check_all_passed_board_00(self, tmp_path: Path, capsys):
        """Board 00 with fresh manifest should report all PASSED + exit 0."""
        from kicad_tools.cli.check_cmd import main

        pcb, _manifest = _stage_board00_copy(tmp_path)
        result = main([str(pcb)])
        assert result == 0
        captured = capsys.readouterr()
        assert "DRC:" in captured.out and "PASSED" in captured.out
        assert "ERC:" in captured.out
        assert "LVS:" in captured.out
        assert "Manifest:" in captured.out
        assert "Overall:" in captured.out
        # Overall must be PASSED.  Grab the Overall line specifically so a
        # PASSED token elsewhere doesn't false-pass the assertion.
        overall_line = next(
            line for line in captured.out.splitlines() if line.startswith("Overall:")
        )
        assert "PASSED" in overall_line

    def test_meta_check_no_schematic_marks_erc_lvs_not_run(self, drc_clean_pcb: Path, capsys):
        """Tmp PCB with no sibling schematic -> ERC/LVS NOT RUN, Overall INCOMPLETE."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb)])
        # Default (non-strict) mode: NOT RUN sub-checks roll up to
        # INCOMPLETE, which the exit-code policy treats as a soft 0 so a
        # bare ``kct check <pcb>`` is still backward-compatible with
        # recipes that don't ship a schematic.
        assert result == 0
        captured = capsys.readouterr()
        assert "ERC:" in captured.out and "NOT RUN" in captured.out
        assert "LVS:" in captured.out
        # Manifest is also NOT RUN (no manufacturing/ sibling in tmp_path).
        assert "Manifest:" in captured.out
        overall_line = next(
            line for line in captured.out.splitlines() if line.startswith("Overall:")
        )
        assert "INCOMPLETE" in overall_line

    def test_meta_check_strict_fails_on_not_run(self, drc_clean_pcb: Path):
        """Under --strict, NOT RUN sub-checks roll up to FAILED -> exit 2."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb), "--strict"])
        assert result == 2

    def test_meta_check_lvs_mismatch_fails_overall(self, tmp_path: Path, capsys):
        """A swapped-pad PCB triggers LVS FAILED -> Overall FAILED -> exit 2."""
        from kicad_tools.cli.check_cmd import main

        pcb, _manifest = _stage_board00_copy(tmp_path)
        _swap_d1_pad_nets_in_pcb(pcb, pcb)

        result = main([str(pcb)])
        assert result == 2
        captured = capsys.readouterr()
        # LVS row must say FAILED; Overall must say FAILED.
        lvs_line = next(line for line in captured.out.splitlines() if line.startswith("LVS:"))
        assert "FAILED" in lvs_line
        assert "D1" in lvs_line  # the mismatch detail should name the offending ref
        overall_line = next(
            line for line in captured.out.splitlines() if line.startswith("Overall:")
        )
        assert "FAILED" in overall_line

    def test_meta_check_stale_manifest_fails_overall(self, tmp_path: Path, capsys):
        """If routed PCB is significantly newer than manifest, Manifest -> FAILED."""
        from kicad_tools.cli.check_cmd import main

        pcb, manifest = _stage_board00_copy(tmp_path)
        # Push the PCB mtime well past the manifest's freshness tolerance
        # (5s in check_cmd._manifest_subcheck).  Use 60s to be unambiguous.
        manifest_mtime = manifest.stat().st_mtime
        new_pcb_mtime = manifest_mtime + 60.0
        os.utime(pcb, (new_pcb_mtime, new_pcb_mtime))

        result = main([str(pcb)])
        assert result == 2
        captured = capsys.readouterr()
        manifest_line = next(
            line for line in captured.out.splitlines() if line.startswith("Manifest:")
        )
        assert "STALE" in manifest_line
        overall_line = next(
            line for line in captured.out.splitlines() if line.startswith("Overall:")
        )
        assert "FAILED" in overall_line

    def test_drc_only_preserves_legacy_output(self, drc_clean_pcb: Path, capsys):
        """--drc-only must skip the meta stanza and use the legacy exit rule."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb), "--drc-only"])
        assert result == 0
        captured = capsys.readouterr()
        # The meta stanza is suppressed: no Overall:, no DRC: status line,
        # no ERC: or LVS: lines.  (The DRC table heading is fine -- it's
        # the legacy "PURE PYTHON DRC CHECK" banner.)
        assert "Overall:" not in captured.out
        assert "ERC:" not in captured.out
        assert "LVS:" not in captured.out
        assert "Manifest:" not in captured.out

    def test_meta_check_json_envelope(self, tmp_path: Path, capsys):
        """JSON output gains meta_checks field; legacy fields are unchanged."""
        from kicad_tools.cli.check_cmd import main

        pcb, _manifest = _stage_board00_copy(tmp_path)
        result = main([str(pcb), "--format", "json"])
        assert result == 0

        captured = capsys.readouterr()
        # Strip any leading warning lines that go to stderr; stdout is JSON.
        data = json.loads(captured.out)

        # Legacy fields must still be present and untouched.
        assert "summary" in data
        assert "passed" in data["summary"]
        assert "errors" in data["summary"]
        assert "rules_checked" in data["summary"]
        assert isinstance(data["violations"], list)

        # New: meta_checks envelope.
        assert "meta_checks" in data
        meta = data["meta_checks"]
        assert set(meta.keys()) == {"drc", "erc", "lvs", "manifest", "overall"}
        for name in ("drc", "erc", "lvs", "manifest"):
            assert "status" in meta[name]
            assert "detail" in meta[name]
        assert meta["overall"] == "PASSED"

    def test_meta_checks_omitted_under_drc_only_in_json(self, drc_clean_pcb: Path, capsys):
        """--drc-only must NOT add meta_checks to the JSON envelope."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb), "--format", "json", "--drc-only"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # Legacy fields unchanged...
        assert data["summary"]["passed"] is True
        # ...and meta_checks omitted (OMIT-when-absent convention).
        assert "meta_checks" not in data
