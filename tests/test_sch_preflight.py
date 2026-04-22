"""Tests for the sch preflight pre-layout validation command."""

import contextlib
import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures: schematic snippets used by the preflight checks
# ---------------------------------------------------------------------------

# A schematic with a symbol whose footprint is missing the library prefix.
_SCH_BAD_FOOTPRINT_NO_PREFIX = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "R_0402_1005Metric" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000003"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000004"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "R1")
          (unit 1)
        )
      )
    )
  )
)
"""

# A schematic with an empty library prefix (":" with no library name).
_SCH_EMPTY_LIB_PREFIX = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" ":R_0402" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000003"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000004"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "R1")
          (unit 1)
        )
      )
    )
  )
)
"""

# A schematic with a generic value ("R") that should be flagged.
_SCH_GENERIC_VALUE = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "R" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0402_1005Metric" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000003"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000004"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "R1")
          (unit 1)
        )
      )
    )
  )
)
"""

# A schematic with power symbols but no PWR_FLAG.
_SCH_NO_PWR_FLAG = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "power:VCC")
    (at 100 50 0)
    (uuid "00000000-0000-0000-0000-000000000010")
    (property "Reference" "#PWR01" (at 100 45 0) (effects (font (size 1.27 1.27))))
    (property "Value" "VCC" (at 100 55 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 100 50 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 50 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000011"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "#PWR01")
          (unit 1)
        )
      )
    )
  )
  (symbol
    (lib_id "power:GND")
    (at 100 150 0)
    (uuid "00000000-0000-0000-0000-000000000020")
    (property "Reference" "#PWR02" (at 100 155 0) (effects (font (size 1.27 1.27))))
    (property "Value" "GND" (at 100 145 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 100 150 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 150 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000021"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "#PWR02")
          (unit 1)
        )
      )
    )
  )
)
"""

# A schematic with power symbols AND a PWR_FLAG -- should pass.
_SCH_WITH_PWR_FLAG = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "power:VCC")
    (at 100 50 0)
    (uuid "00000000-0000-0000-0000-000000000010")
    (property "Reference" "#PWR01" (at 100 45 0) (effects (font (size 1.27 1.27))))
    (property "Value" "VCC" (at 100 55 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 100 50 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 50 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000011"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "#PWR01")
          (unit 1)
        )
      )
    )
  )
  (symbol
    (lib_id "power:PWR_FLAG")
    (at 110 50 0)
    (uuid "00000000-0000-0000-0000-000000000030")
    (property "Reference" "#FLG01" (at 110 45 0) (effects (font (size 1.27 1.27))))
    (property "Value" "PWR_FLAG" (at 110 55 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 110 50 0) (effects (hide yes)))
    (property "Datasheet" "" (at 110 50 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000031"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "#FLG01")
          (unit 1)
        )
      )
    )
  )
)
"""

# A schematic with a single-occurrence label (single-pin net).
_SCH_SINGLE_PIN_NET = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (label "LONELY_NET"
    (at 90 100 0)
    (effects (font (size 1.27 1.27)))
    (uuid "00000000-0000-0000-0000-000000000006")
  )
)
"""

# An empty schematic (no components) -- should pass cleanly.
_SCH_EMPTY = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
)
"""


# ---------------------------------------------------------------------------
# Helper to write a schematic file from a string
# ---------------------------------------------------------------------------


@pytest.fixture
def _write_sch(tmp_path: Path):
    """Return a helper that writes schematic content to a temp file and returns its path."""

    def _inner(content: str, name: str = "test.kicad_sch") -> Path:
        p = tmp_path / name
        p.write_text(content)
        return p

    return _inner


# ---------------------------------------------------------------------------
# Individual check tests
# ---------------------------------------------------------------------------


class TestCheckFootprintLibraryResolution:
    """Tests for check_footprint_library_resolution."""

    def test_missing_library_prefix(self, _write_sch):
        from kicad_tools.cli.sch_preflight import check_footprint_library_resolution

        sch = _write_sch(_SCH_BAD_FOOTPRINT_NO_PREFIX)
        issues = check_footprint_library_resolution(str(sch))
        assert len(issues) >= 1
        assert any("missing library prefix" in i.message for i in issues)
        assert all(i.severity == "warning" for i in issues)

    def test_empty_library_prefix(self, _write_sch):
        from kicad_tools.cli.sch_preflight import check_footprint_library_resolution

        sch = _write_sch(_SCH_EMPTY_LIB_PREFIX)
        issues = check_footprint_library_resolution(str(sch))
        assert len(issues) >= 1
        assert any("empty library or footprint name" in i.message for i in issues)
        assert any(i.severity == "error" for i in issues)

    def test_valid_footprint_passes(self, minimal_schematic: Path):
        from kicad_tools.cli.sch_preflight import check_footprint_library_resolution

        issues = check_footprint_library_resolution(str(minimal_schematic))
        fp_issues = [i for i in issues if i.category == "footprint_resolution"]
        assert len(fp_issues) == 0


class TestCheckGenericValues:
    """Tests for check_generic_values."""

    def test_generic_value_flagged(self, _write_sch):
        from kicad_tools.cli.sch_preflight import check_generic_values

        sch = _write_sch(_SCH_GENERIC_VALUE)
        issues = check_generic_values(str(sch))
        assert len(issues) >= 1
        assert any("generic value" in i.message.lower() for i in issues)
        assert all(i.severity == "warning" for i in issues)

    def test_real_value_passes(self, minimal_schematic: Path):
        from kicad_tools.cli.sch_preflight import check_generic_values

        # minimal_schematic has value "10k" which is not generic
        issues = check_generic_values(str(minimal_schematic))
        assert len(issues) == 0


class TestCheckPowerFlags:
    """Tests for check_power_flags."""

    def test_no_pwr_flag_warns(self, _write_sch):
        from kicad_tools.cli.sch_preflight import check_power_flags

        sch = _write_sch(_SCH_NO_PWR_FLAG)
        issues = check_power_flags(str(sch))
        assert len(issues) == 1
        assert "PWR_FLAG" in issues[0].message
        assert issues[0].severity == "warning"

    def test_with_pwr_flag_passes(self, _write_sch):
        from kicad_tools.cli.sch_preflight import check_power_flags

        sch = _write_sch(_SCH_WITH_PWR_FLAG)
        issues = check_power_flags(str(sch))
        pf_issues = [i for i in issues if i.category == "power_flag"]
        assert len(pf_issues) == 0

    def test_no_power_symbols_passes(self, _write_sch):
        from kicad_tools.cli.sch_preflight import check_power_flags

        sch = _write_sch(_SCH_EMPTY)
        issues = check_power_flags(str(sch))
        assert len(issues) == 0


class TestCheckSinglePinNets:
    """Tests for check_single_pin_nets."""

    def test_single_pin_net_detected(self, _write_sch):
        from kicad_tools.cli.sch_preflight import check_single_pin_nets

        sch = _write_sch(_SCH_SINGLE_PIN_NET)
        issues = check_single_pin_nets(str(sch))
        assert len(issues) >= 1
        assert any("LONELY_NET" in i.message for i in issues)
        assert all(i.severity == "warning" for i in issues)

    def test_empty_schematic_passes(self, _write_sch):
        from kicad_tools.cli.sch_preflight import check_single_pin_nets

        sch = _write_sch(_SCH_EMPTY)
        issues = check_single_pin_nets(str(sch))
        assert len(issues) == 0


# ---------------------------------------------------------------------------
# Integration: run_preflight
# ---------------------------------------------------------------------------


class TestRunPreflight:
    """Tests for the run_preflight aggregator."""

    def test_all_checks_run(self, minimal_schematic: Path):
        from kicad_tools.cli.sch_preflight import run_preflight

        result = run_preflight(str(minimal_schematic))
        # Should run all 8 check categories
        assert "footprints" in result.checks_run
        assert "values" in result.checks_run
        assert "hierarchy" in result.checks_run
        assert "footprint_resolution" in result.checks_run
        assert "pin_pad_count" in result.checks_run
        assert "single_pin_nets" in result.checks_run
        assert "generic_values" in result.checks_run
        assert "power_flags" in result.checks_run

    def test_clean_schematic_passes(self, minimal_schematic: Path):
        from kicad_tools.cli.sch_preflight import run_preflight

        result = run_preflight(str(minimal_schematic))
        # The minimal schematic has valid footprint, real value, no power issues
        assert result.passed is True

    def test_empty_schematic_passes(self, _write_sch):
        from kicad_tools.cli.sch_preflight import run_preflight

        sch = _write_sch(_SCH_EMPTY)
        result = run_preflight(str(sch))
        assert result.passed is True
        assert result.error_count == 0


# ---------------------------------------------------------------------------
# CLI entry point: main()
# ---------------------------------------------------------------------------


class TestPreflightCLI:
    """Tests for the CLI entry point (main)."""

    def test_file_not_found_exits_1(self, capsys):
        from kicad_tools.cli.sch_preflight import main

        with pytest.raises(SystemExit) as exc_info:
            main(["nonexistent.kicad_sch"])
        assert exc_info.value.code == 1

    def test_text_output(self, minimal_schematic: Path, capsys):
        from kicad_tools.cli.sch_preflight import main

        # Should not raise (clean schematic)
        main([str(minimal_schematic)])
        captured = capsys.readouterr()
        assert "Validation:" in captured.out
        assert "Checks run:" in captured.out

    def test_json_output(self, minimal_schematic: Path, capsys):
        from kicad_tools.cli.sch_preflight import main

        main([str(minimal_schematic), "--format", "json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "schematic" in data
        assert "passed" in data
        assert "checks_run" in data
        assert "issues" in data
        assert isinstance(data["checks_run"], list)
        assert len(data["checks_run"]) == 8

    def test_strict_flag_exits_on_warnings(self, _write_sch, capsys):
        from kicad_tools.cli.sch_preflight import main

        # This schematic triggers a generic-value warning
        sch = _write_sch(_SCH_GENERIC_VALUE)
        with pytest.raises(SystemExit) as exc_info:
            main([str(sch), "--strict"])
        assert exc_info.value.code == 1

    def test_no_strict_passes_with_warnings(self, _write_sch, capsys):
        from kicad_tools.cli.sch_preflight import main

        sch = _write_sch(_SCH_GENERIC_VALUE)
        # Without --strict, warnings do not cause exit(1)
        main([str(sch)])
        captured = capsys.readouterr()
        assert "warning" in captured.out.lower() or "Warning" in captured.out or len(captured.out) > 0

    def test_quiet_json_omits_warnings(self, _write_sch, capsys):
        from kicad_tools.cli.sch_preflight import main

        sch = _write_sch(_SCH_GENERIC_VALUE)
        main([str(sch), "--format", "json", "--quiet"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # With --quiet, only errors should appear in the issues list
        for issue in data["issues"]:
            assert issue["severity"] == "error"

    def test_error_causes_exit_1(self, _write_sch, capsys):
        from kicad_tools.cli.sch_preflight import main

        # Empty library prefix produces an error
        sch = _write_sch(_SCH_EMPTY_LIB_PREFIX)
        with pytest.raises(SystemExit) as exc_info:
            main([str(sch)])
        assert exc_info.value.code == 1
