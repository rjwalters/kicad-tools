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
        """Test check command with table output format.

        Note (issue #3750): tmp PCBs have no sibling schematic / manifest,
        so the meta-check rollup is INCOMPLETE.  Pass ``--allow-incomplete``
        to assert the legacy "DRC clean -> exit 0" contract under the new
        default where INCOMPLETE exits non-zero.
        """
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb), "--allow-incomplete"])
        assert result == 0

        captured = capsys.readouterr()
        assert "PURE PYTHON DRC CHECK" in captured.out
        assert "DRC PASSED" in captured.out or "Results:" in captured.out

    def test_check_json_output(self, drc_clean_pcb: Path, capsys):
        """Test check command with JSON output format."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb), "--format", "json", "--allow-incomplete"])
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

        result = main([str(drc_clean_pcb), "--format", "summary", "--allow-incomplete"])
        assert result == 0

        captured = capsys.readouterr()
        assert "DRC" in captured.out

    def test_check_manufacturer_option(self, drc_clean_pcb: Path, capsys):
        """Test check command with manufacturer option."""
        from kicad_tools.cli.check_cmd import main

        # Test with different manufacturers
        for mfr in ["jlcpcb", "seeed", "pcbway", "oshpark"]:
            result = main([str(drc_clean_pcb), "--mfr", mfr, "--allow-incomplete"])
            assert result == 0, f"Failed for manufacturer {mfr}"

    def test_check_layers_option(self, drc_clean_pcb: Path, capsys):
        """Test check command with layers option."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb), "--layers", "4", "--allow-incomplete"])
        assert result == 0

        captured = capsys.readouterr()
        assert "4" in captured.out  # Layer count should appear in output

    def test_check_only_filter(self, drc_clean_pcb: Path, capsys):
        """Test check command with --only filter."""
        from kicad_tools.cli.check_cmd import main

        # Run only clearance checks
        result = main([str(drc_clean_pcb), "--only", "clearance", "--allow-incomplete"])
        assert result == 0

        # Run multiple categories
        result = main([str(drc_clean_pcb), "--only", "clearance,dimensions", "--allow-incomplete"])
        assert result == 0

    def test_check_skip_filter(self, drc_clean_pcb: Path, capsys):
        """Test check command with --skip filter."""
        from kicad_tools.cli.check_cmd import main

        # Skip silkscreen checks
        result = main([str(drc_clean_pcb), "--skip", "silkscreen", "--allow-incomplete"])
        assert result == 0

        # Skip multiple categories
        result = main([str(drc_clean_pcb), "--skip", "silkscreen,edge", "--allow-incomplete"])
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

        result = main([str(drc_clean_pcb), "--errors-only", "--allow-incomplete"])
        assert result == 0  # No errors with clean PCB

    def test_check_verbose_flag(self, drc_clean_pcb: Path, capsys):
        """Test check command with --verbose flag."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb), "--verbose", "--allow-incomplete"])
        assert result == 0

    def test_check_copper_weight_option(self, drc_clean_pcb: Path, capsys):
        """Test check command with copper weight option."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb), "--copper", "2.0", "--allow-incomplete"])
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
        result = main([str(drc_clean_pcb), "--output", str(output_file), "--allow-incomplete"])
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
        result = main([str(drc_clean_pcb), "--output", str(output_file), "--allow-incomplete"])
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
        result = main([str(drc_clean_pcb), "--output", str(output_file), "--allow-incomplete"])
        assert result == 0

        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert "summary" in data

    def test_output_without_flag_no_file(self, drc_clean_pcb: Path, tmp_path: Path, capsys):
        """Test that no file is written when --output is not specified."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb), "--allow-incomplete"])
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

        result = main(["check", str(drc_clean_pcb), "--allow-incomplete"])
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
                "--allow-incomplete",
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
        """Test exit code 0 when no violations found (with --allow-incomplete).

        Issue #3750: tmp PCBs have no sibling schematic / manifest, so
        the meta rollup is INCOMPLETE.  The opt-in flag preserves the
        "DRC clean -> 0" semantics for this test.
        """
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb), "--allow-incomplete"])
        assert result == 0

    def test_exit_code_0_warnings_only_no_strict(self, drc_clean_pcb: Path):
        """Test exit code 0 with warnings when not in strict mode."""
        from kicad_tools.cli.check_cmd import main

        # With clean PCB, no warnings to test
        # But this confirms the code path works
        result = main([str(drc_clean_pcb), "--allow-incomplete"])
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

        result = main([str(drc_clean_pcb), "--format", "json", "--allow-incomplete"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["layers"] == 2

    def test_auto_detect_4_layer_board(self, four_layer_pcb: Path, capsys):
        """Test that a 4-layer board auto-detects 4 layers without --layers flag."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(four_layer_pcb), "--format", "json", "--allow-incomplete"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["layers"] == 4

    def test_auto_detect_6_layer_board(self, six_layer_pcb: Path, capsys):
        """Test that a 6-layer board auto-detects 6 layers."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(six_layer_pcb), "--format", "json", "--allow-incomplete"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["layers"] == 6

    def test_explicit_layers_overrides_detection(self, four_layer_pcb: Path, capsys):
        """Test that --layers flag overrides auto-detection."""
        from kicad_tools.cli.check_cmd import main

        result = main(
            [str(four_layer_pcb), "--layers", "2", "--format", "json", "--allow-incomplete"]
        )
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["layers"] == 2

    def test_auto_detect_4_layer_table_output(self, four_layer_pcb: Path, capsys):
        """Test that table output shows auto-detected layer count."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(four_layer_pcb), "--allow-incomplete"])
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
        """Tmp PCB with no sibling schematic -> ERC/LVS NOT RUN, Overall INCOMPLETE -> exit 2.

        Issue #3750 AC #3: exit code is 0 only when every sub-check is
        PASSED.  A board that cannot be fully verified (no schematic =
        no ERC, no LVS) must exit non-zero so CI consumers that read
        the exit code do not silently accept a partially verified board.
        Boards that legitimately lack a sub-check input can opt in to
        ``--allow-incomplete`` to flip the exit back to 0.
        """
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb)])
        # Default mode (no --allow-incomplete): INCOMPLETE -> exit 2.
        assert result == 2
        captured = capsys.readouterr()
        assert "ERC:" in captured.out and "NOT RUN" in captured.out
        assert "LVS:" in captured.out
        # Manifest is also NOT RUN (no manufacturing/ sibling in tmp_path).
        assert "Manifest:" in captured.out
        overall_line = next(
            line for line in captured.out.splitlines() if line.startswith("Overall:")
        )
        assert "INCOMPLETE" in overall_line

    def test_meta_check_allow_incomplete_passes(self, drc_clean_pcb: Path, capsys):
        """--allow-incomplete lets NOT RUN sub-checks exit 0 (issue #3750).

        Counterpart to ``test_meta_check_no_schematic_marks_erc_lvs_not_run``:
        a board with no schematic still rolls up to ``Overall: INCOMPLETE``,
        but the opt-in flag suppresses the non-zero exit code so recipes
        that legitimately lack a sub-check input can keep passing.
        """
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb), "--allow-incomplete"])
        assert result == 0
        captured = capsys.readouterr()
        # The human stanza still reports the truthful INCOMPLETE rollup --
        # only the exit code changes.
        overall_line = next(
            line for line in captured.out.splitlines() if line.startswith("Overall:")
        )
        assert "INCOMPLETE" in overall_line

    def test_meta_check_strict_does_not_rescue_incomplete(self, drc_clean_pcb: Path, capsys):
        """--strict does not affect the NOT RUN -> INCOMPLETE rollup.

        ``--strict`` only escalates DRC / ERC warnings.  NOT RUN rollup is
        controlled by ``--allow-incomplete`` (default: exit 2).  Verify
        that running --strict on a tmp PCB with no schematic still exits
        2 (the new default already exits 2 on INCOMPLETE; --strict simply
        does not rescue or differ here).
        """
        from kicad_tools.cli.check_cmd import main

        result = main([str(drc_clean_pcb), "--strict"])
        assert result == 2
        captured = capsys.readouterr()
        # Rollup should still report the truthful INCOMPLETE -- --strict
        # no longer collapses NOT RUN into FAILED at the rollup level.
        overall_line = next(
            line for line in captured.out.splitlines() if line.startswith("Overall:")
        )
        assert "INCOMPLETE" in overall_line

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


# ---------------------------------------------------------------------------
# Issue #4096: stale-zone-fill advisory + opt-in --refill-zones flag
# ---------------------------------------------------------------------------


def _make_zone_clearance_violation(rule_id: str):
    """Build a synthetic ERROR DRCViolation carrying a zone-clearance rule_id."""
    from kicad_tools.core.types import Severity
    from kicad_tools.validate.models import DRCViolation

    return DRCViolation(
        severity=Severity.ERROR,
        message=f"synthetic {rule_id} finding",
        rule_id=rule_id,
    )


class TestStaleZoneFillWarning:
    """Unit coverage for the _warn_stale_zone_fills advisory (issue #4096)."""

    def test_warns_on_segment_zone_clearance(self, tmp_path: Path, capsys):
        from kicad_tools.cli.check_cmd import _warn_stale_zone_fills

        pcb = tmp_path / "board.kicad_pcb"
        violations = [_make_zone_clearance_violation("clearance_segment_zone")]

        _warn_stale_zone_fills(violations, pcb)

        err = capsys.readouterr().err
        assert "clearance_segment_zone" in err
        assert "STALE" in err
        assert "kicad-cli pcb drc --refill-zones --save-board" in err
        assert str(pcb) in err

    def test_warns_on_via_and_pad_zone_clearance(self, tmp_path: Path, capsys):
        from kicad_tools.cli.check_cmd import _warn_stale_zone_fills

        pcb = tmp_path / "board.kicad_pcb"
        violations = [
            _make_zone_clearance_violation("clearance_via_zone"),
            _make_zone_clearance_violation("clearance_pad_zone"),
        ]

        _warn_stale_zone_fills(violations, pcb)

        err = capsys.readouterr().err
        assert "clearance_via_zone" in err
        assert "clearance_pad_zone" in err

    def test_no_warning_without_zone_clearance_findings(self, tmp_path: Path, capsys):
        """A board with only non-zone findings must not emit the advisory."""
        from kicad_tools.cli.check_cmd import _warn_stale_zone_fills
        from kicad_tools.core.types import Severity
        from kicad_tools.validate.models import DRCViolation

        pcb = tmp_path / "board.kicad_pcb"
        violations = [
            DRCViolation(
                severity=Severity.ERROR,
                message="unrelated clearance",
                rule_id="clearance_trace_trace",
            )
        ]

        _warn_stale_zone_fills(violations, pcb)

        assert "refill-zones" not in capsys.readouterr().err

    def test_no_warning_on_empty_violations(self, tmp_path: Path, capsys):
        from kicad_tools.cli.check_cmd import _warn_stale_zone_fills

        _warn_stale_zone_fills([], tmp_path / "board.kicad_pcb")

        assert capsys.readouterr().err == ""

    def test_warning_fires_through_main_on_zone_clearance_finding(
        self, drc_clean_pcb: Path, monkeypatch, capsys
    ):
        """End-to-end: a clearance_*_zone finding triggers the advisory via main().

        Injects a synthetic ``clearance_segment_zone`` error into the DRC
        results so the assertion pins the *wiring* (main -> advisory) rather
        than a specific committed board's fill staleness, which drifts as
        artifacts are refilled.  --drc-only confirms the advisory is
        independent of the meta-check rollup.
        """
        import kicad_tools.cli.check_cmd as check_cmd
        from kicad_tools.cli.check_cmd import main
        from kicad_tools.validate.violations import DRCResults

        real_run = check_cmd.run_selected_checks

        def _inject(*args, **kwargs) -> DRCResults:
            results = real_run(*args, **kwargs)
            results.add(_make_zone_clearance_violation("clearance_segment_zone"))
            return results

        monkeypatch.setattr(check_cmd, "run_selected_checks", _inject)

        main([str(drc_clean_pcb), "--drc-only"])

        err = capsys.readouterr().err
        assert "kicad-cli pcb drc --refill-zones --save-board" in err
        assert "STALE" in err

    def test_no_warning_through_main_on_clean_board(self, drc_clean_pcb: Path, capsys):
        """A clean board (no zone-clearance findings) emits no advisory."""
        from kicad_tools.cli.check_cmd import main

        main([str(drc_clean_pcb), "--drc-only"])

        assert "refill-zones" not in capsys.readouterr().err


class TestRefillZonesFlag:
    """Coverage for the opt-in --refill-zones flag (issue #4096)."""

    def test_flag_invokes_kicad_cli_when_available(self, drc_clean_pcb: Path, monkeypatch, capsys):
        """--refill-zones shells out to run_refill_zones before loading the PCB."""
        import kicad_tools.cli.runner as runner
        from kicad_tools.cli.runner import KiCadCLIResult

        calls: list[Path] = []

        def _spy(pcb_path, kicad_cli=None):
            calls.append(Path(pcb_path))
            return KiCadCLIResult(success=True, output_path=Path(pcb_path))

        monkeypatch.setattr(runner, "run_refill_zones", _spy)

        result = main_(drc_clean_pcb)

        assert result == 0
        assert len(calls) == 1
        assert calls[0] == drc_clean_pcb.resolve()
        assert "refilled zones in place" in capsys.readouterr().err

    def test_flag_degrades_gracefully_when_kicad_cli_absent(
        self, drc_clean_pcb: Path, monkeypatch, capsys
    ):
        """A missing kicad-cli warns and continues rather than crashing."""
        import kicad_tools.cli.runner as runner
        from kicad_tools.cli.runner import KiCadCLIResult

        def _absent(pcb_path, kicad_cli=None):
            return KiCadCLIResult(success=False, stderr="kicad-cli not found.")

        monkeypatch.setattr(runner, "run_refill_zones", _absent)

        # Must not raise, and the check itself still runs to a clean exit.
        result = main_(drc_clean_pcb)

        assert result == 0
        err = capsys.readouterr().err
        assert "--refill-zones requested but the refill did not run" in err
        assert "kicad-cli not found" in err

    def test_no_refill_by_default(self, drc_clean_pcb: Path, monkeypatch):
        """Without --refill-zones the refill helper is never called."""
        import kicad_tools.cli.runner as runner
        from kicad_tools.cli.check_cmd import main

        def _boom(pcb_path, kicad_cli=None):
            raise AssertionError("run_refill_zones must not run by default")

        monkeypatch.setattr(runner, "run_refill_zones", _boom)

        result = main([str(drc_clean_pcb), "--allow-incomplete"])
        assert result == 0

    def test_help_text_documents_board_mutation(self, capsys):
        """The flag help must warn that it mutates the board file."""
        from kicad_tools.cli.check_cmd import main

        with pytest.raises(SystemExit):
            main(["--help"])

        out = capsys.readouterr().out
        assert "--refill-zones" in out
        # The MUTATES side effect must be surfaced explicitly.
        assert "MUTATE" in out.upper()


class TestRunRefillZonesHelper:
    """Direct coverage for runner.run_refill_zones (issue #4096)."""

    def test_returns_failure_when_kicad_cli_missing(self, tmp_path: Path, monkeypatch):
        """No kicad-cli on PATH → success=False with an explanatory message."""
        import kicad_tools.cli.runner as runner

        monkeypatch.setattr(runner, "find_kicad_cli", lambda: None)

        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        result = runner.run_refill_zones(pcb)

        assert result.success is False
        assert "kicad-cli not found" in result.stderr

    def test_builds_refill_save_board_command(self, tmp_path: Path, monkeypatch):
        """Invokes `kicad-cli pcb drc --refill-zones --save-board <pcb>`."""
        import kicad_tools.cli.runner as runner
        from kicad_tools.cli.runner import KiCadCLIResult

        fake_cli = tmp_path / "kicad-cli"
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        captured_cmd: list[list[str]] = []

        class _Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        def _fake_run(cmd, capture_output=True, text=True, **kwargs):
            captured_cmd.append(cmd)
            # Write a non-empty report so the helper reports success.
            out_idx = cmd.index("--output") + 1
            Path(cmd[out_idx]).write_text("{}")
            return _Completed()

        monkeypatch.setattr(runner, "find_kicad_cli", lambda: fake_cli)
        monkeypatch.setattr(runner, "_kicad_drc_supports_refill", lambda _cli: True)
        # Skip net-table repair (no real board content to restore).
        monkeypatch.setattr(runner, "_snapshot_net_declarations", lambda _p: [])
        monkeypatch.setattr(runner, "_snapshot_element_nets", lambda _p: {})
        monkeypatch.setattr(runner, "_restore_net_declarations", lambda *a, **k: None)
        monkeypatch.setattr(runner.subprocess, "run", _fake_run)

        result: KiCadCLIResult = runner.run_refill_zones(pcb)

        assert result.success is True
        assert result.output_path == pcb
        assert len(captured_cmd) == 1
        cmd = captured_cmd[0]
        assert cmd[1:3] == ["pcb", "drc"]
        assert "--refill-zones" in cmd
        assert "--save-board" in cmd
        assert str(pcb) == cmd[-1]


def main_(pcb: Path) -> int:
    """Invoke `kct check --refill-zones` with the fast/complete-friendly flags."""
    from kicad_tools.cli.check_cmd import main

    return main([str(pcb), "--refill-zones", "--allow-incomplete"])


class TestDrillClearanceNetRelationship:
    """Issue #4102: net-relationship enrichment for dimension_drill_clearance.

    The findings already carry both endpoints' nets (``nets=(net1, net2)``,
    also serialized in ``--format json`` via ``DRCViolation.to_dict``).  These
    tests exercise the report *formatting* only: net names shown
    unconditionally with a same-net / different-net qualifier on the finding
    line, and a same-net / different-net sub-count in the ``BY RULE:`` summary.
    """

    @staticmethod
    def _violation(nets, severity="error"):
        from kicad_tools.validate import DRCViolation

        return DRCViolation(
            rule_id="dimension_drill_clearance",
            severity=severity,
            message="Hole-to-hole clearance 0.113mm < minimum 0.500mm",
            location=(10.0, 20.0),
            layer=None,
            actual_value=0.113,
            required_value=0.500,
            items=("V1", "R1-2:GND"),
            nets=tuple(nets),
        )

    def test_net_relationship_helper(self):
        from kicad_tools.cli.check_cmd import _net_relationship

        assert _net_relationship(("HALL_A", "GND")) == "different-net"
        assert _net_relationship(("HALL_A", "HALL_A")) == "same-net"
        # Two *distinct named* nets that happen to look like placeholders --
        # NOT a floating-pad scenario (floating pads never get distinct
        # per-pad numbers; they all resolve to the single "net:0" sentinel).
        assert _net_relationship(("net:3", "net:7")) == "different-net"
        # Not a pair -> nothing to compare.
        assert _net_relationship(()) is None
        assert _net_relationship(("only-one",)) is None

    def test_net_relationship_floating_pins(self):
        """Issue #4127: floating/unconnected pins never read as same-net.

        Every genuinely unconnected pad/via resolves to the *same* ``net:0``
        placeholder (net 0 is a single canonical no-net sentinel), so a naive
        string equality would mislabel two distinct floating pins as same-net.
        Any pair involving a floating endpoint must classify as different-net.
        """
        from kicad_tools.cli.check_cmd import _net_relationship

        # Core regression: two distinct floating pins, both resolved to net:0,
        # must NOT read as same-net.
        assert _net_relationship(("net:0", "net:0")) == "different-net"
        # Floating vs named (already correct today; keep it correct).
        assert _net_relationship(("net:0", "GND")) == "different-net"
        assert _net_relationship(("GND", "net:0")) == "different-net"
        # Empty-string spelling of the no-net sentinel is also floating.
        assert _net_relationship(("", "")) == "different-net"
        assert _net_relationship(("", "GND")) == "different-net"
        assert _net_relationship(("GND", "")) == "different-net"
        # Named/named is unchanged -- the fix does not over-broaden.
        assert _net_relationship(("GND", "GND")) == "same-net"

    def test_different_net_finding_shows_label_and_nets_default(self, capsys):
        """Default (non-verbose) output labels the pair and prints both nets."""
        from kicad_tools.cli.check_cmd import _print_violation

        _print_violation(self._violation(("HALL_A", "GND")), verbose=False)
        out = capsys.readouterr().out
        assert "dimension_drill_clearance (different-net)" in out
        assert "Nets: HALL_A / GND" in out

    def test_same_net_finding_shows_label_and_nets_default(self, capsys):
        """Same-net pairs are labeled same-net; still shown without --verbose."""
        from kicad_tools.cli.check_cmd import _print_violation

        _print_violation(self._violation(("HALL_A", "HALL_A")), verbose=False)
        out = capsys.readouterr().out
        assert "dimension_drill_clearance (same-net)" in out
        assert "Nets: HALL_A / HALL_A" in out

    def test_verbose_keeps_label_and_nets(self, capsys):
        """--verbose keeps the qualifier and the Nets line (no duplicate)."""
        from kicad_tools.cli.check_cmd import _print_violation

        _print_violation(self._violation(("HALL_A", "GND")), verbose=True)
        out = capsys.readouterr().out
        assert "dimension_drill_clearance (different-net)" in out
        # Net endpoints rendered exactly once, in the net-relationship form.
        assert out.count("Nets:") == 1
        assert "Nets: HALL_A / GND" in out

    def test_other_rules_unaffected(self, capsys):
        """Non-drill rules keep their plain header and verbose-only Nets line."""
        from kicad_tools.cli.check_cmd import _print_violation
        from kicad_tools.validate import DRCViolation

        v = DRCViolation(
            rule_id="clearance_trace_trace",
            severity="error",
            message="clearance too small",
            nets=("NET_A", "NET_B"),
        )
        _print_violation(v, verbose=False)
        out = capsys.readouterr().out
        assert "clearance_trace_trace" in out
        assert "(different-net)" not in out
        assert "Nets:" not in out  # gated on --verbose for non-drill rules

    def test_by_rule_summary_includes_breakdown(self, capsys):
        """BY RULE line carries the different-net / same-net sub-count."""
        from pathlib import Path

        from kicad_tools.cli.check_cmd import output_table
        from kicad_tools.validate import DRCResults

        results = DRCResults()
        # 2 different-net + 1 same-net.
        violations = [
            self._violation(("HALL_A", "GND")),
            self._violation(("SIG", "GND")),
            self._violation(("GND", "GND")),
        ]
        for v in violations:
            results.add(v)
        results.rules_checked = 1

        output_table(violations, results, Path("board.kicad_pcb"), "jlcpcb", 2, False)
        out = capsys.readouterr().out
        assert "dimension_drill_clearance: 3 errors (2 different-net, 1 same-net)" in out

    def test_by_rule_summary_floating_pair_counts_as_different(self, capsys):
        """Issue #4127: floating/floating pairs count as different-net in BY RULE.

        The ``diff_n + same_n == errors`` invariant must hold after the fix:
        1 floating/floating + 1 named-different + 1 named-same ->
        (2 different-net, 1 same-net).
        """
        from pathlib import Path

        from kicad_tools.cli.check_cmd import output_table
        from kicad_tools.validate import DRCResults

        results = DRCResults()
        violations = [
            self._violation(("net:0", "net:0")),  # floating/floating
            self._violation(("HALL_A", "GND")),  # named-different
            self._violation(("GND", "GND")),  # named-same
        ]
        for v in violations:
            results.add(v)
        results.rules_checked = 1

        output_table(violations, results, Path("board.kicad_pcb"), "jlcpcb", 2, False)
        out = capsys.readouterr().out
        assert "dimension_drill_clearance: 3 errors (2 different-net, 1 same-net)" in out


class TestClearanceSegmentViaNetRelationship:
    """Issue #4318: same-net / different-net split for copper-copper clearance.

    ``clearance_segment_via`` (and the sibling ``clearance_segment_segment`` /
    ``clearance_via_via`` / ``clearance_pad_via`` rules) already carry
    ``nets=(net_a, net_b)`` (set in ``_create_violation``,
    ``validate/rules/clearance.py``), also serialized in ``--format json``.
    Adding them to ``_NET_RELATIONSHIP_RULE_IDS`` gives them the same
    presentational split ``dimension_drill_clearance`` gets: a per-finding
    qualifier + unconditional ``Nets:`` line and a ``BY RULE`` sub-count.  An
    agent can then triage a genuine different-net short ahead of a lower-risk
    same-net coincidence.  Presentational only; severity is unchanged.
    """

    @staticmethod
    def _violation(nets, rule_id="clearance_segment_via", severity="error"):
        from kicad_tools.validate import DRCViolation

        return DRCViolation(
            rule_id=rule_id,
            severity=severity,
            message="Segment to via clearance 0.000mm < minimum 0.200mm",
            location=(10.0, 20.0),
            layer="F.Cu",
            actual_value=0.0,
            required_value=0.200,
            items=("seg-1", "via-1"),
            nets=tuple(nets),
        )

    def test_rule_ids_registered(self):
        from kicad_tools.cli.check_cmd import _NET_RELATIONSHIP_RULE_IDS

        for rid in (
            "clearance_segment_via",
            "clearance_segment_segment",
            "clearance_via_via",
            "clearance_pad_via",
        ):
            assert rid in _NET_RELATIONSHIP_RULE_IDS

    def test_different_net_header_and_nets_default(self, capsys):
        from kicad_tools.cli.check_cmd import _print_violation

        _print_violation(self._violation(("SIG1", "SIG2")), verbose=False)
        out = capsys.readouterr().out
        assert "clearance_segment_via (different-net)" in out
        assert "Nets: SIG1 / SIG2" in out

    def test_same_net_header_and_nets_default(self, capsys):
        from kicad_tools.cli.check_cmd import _print_violation

        _print_violation(self._violation(("SIG1", "SIG1")), verbose=False)
        out = capsys.readouterr().out
        assert "clearance_segment_via (same-net)" in out
        assert "Nets: SIG1 / SIG1" in out

    def test_floating_endpoint_is_different_net(self, capsys):
        from kicad_tools.cli.check_cmd import _print_violation

        # A floating (net:0) endpoint in a segment-via pair classifies as
        # different-net (#4127) -- never same-net.
        _print_violation(self._violation(("net:0", "SIG1")), verbose=False)
        out = capsys.readouterr().out
        assert "clearance_segment_via (different-net)" in out

    def test_by_rule_summary_split(self, capsys):
        from pathlib import Path

        from kicad_tools.cli.check_cmd import output_table
        from kicad_tools.validate import DRCResults

        results = DRCResults()
        violations = [
            self._violation(("SIG1", "SIG2")),  # different-net
            self._violation(("SIG3", "SIG4")),  # different-net
            self._violation(("SIG1", "SIG1")),  # same-net
        ]
        for v in violations:
            results.add(v)
        results.rules_checked = 1

        output_table(violations, results, Path("board.kicad_pcb"), "jlcpcb", 2, False)
        out = capsys.readouterr().out
        assert "clearance_segment_via: 3 errors (2 different-net, 1 same-net)" in out

    def test_json_still_carries_nets(self):
        # AC T3: --format json must keep the nets pair (no schema regression).
        v = self._violation(("SIG1", "SIG2"))
        d = v.to_dict()
        assert d["nets"] == ["SIG1", "SIG2"]

    def test_sibling_rules_also_split(self, capsys):
        from kicad_tools.cli.check_cmd import _print_violation

        for rid in ("clearance_segment_segment", "clearance_via_via", "clearance_pad_via"):
            _print_violation(self._violation(("A", "B"), rule_id=rid), verbose=False)
            out = capsys.readouterr().out
            assert f"{rid} (different-net)" in out
