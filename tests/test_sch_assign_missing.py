"""Tests for ``sch assign-footprints --assign-missing`` (issue #3866).

These exercise the disk-free heuristic auto-assignment path end-to-end:

* a footprint-less standard passive is auto-assigned from value+package, with
  NO installed KiCad library; and
* a footprint-less unknown IC FAILS LOUD (non-zero exit) rather than being
  silently skipped.

They also assert the pre-flight gate (``check_missing_footprints`` /
``run_preflight``) trips on missing footprints.
"""

from __future__ import annotations

import json

from kicad_tools.cli import sch_assign_footprints, sch_suggest_footprint
from kicad_tools.cli.sch_assign_footprints import run_assign_footprints
from kicad_tools.footprints.library_path import LibraryPaths

# A two-symbol schematic with NO footprints set:
#   R1 -- a 0402 resistor (chip size embedded in the value field), and
#   U1 -- an 8-pin op-amp (an IC the heuristic cannot resolve).
_SCH = """(kicad_sch
    (version 20231120)
    (generator "kicadtools_test")
    (uuid "00000000-0000-0000-0000-0000000000ab")
    (paper "A4")
    (lib_symbols
        (symbol "Device:R"
            (symbol "R_1_1"
                (pin passive line (at 0 3.81 270) (length 1.27) (name "~") (number "1"))
                (pin passive line (at 0 -3.81 90) (length 1.27) (name "~") (number "2"))
            )
        )
        (symbol "Amplifier_Operational:LM358"
            (symbol "LM358_1_1"
                (pin input line (at 0 0 0) (length 1) (name "-") (number "1"))
                (pin input line (at 0 1 0) (length 1) (name "+") (number "2"))
                (pin output line (at 0 2 0) (length 1) (name "O") (number "3"))
                (pin power_in line (at 0 3 0) (length 1) (name "V-") (number "4"))
                (pin input line (at 0 4 0) (length 1) (name "+") (number "5"))
                (pin input line (at 0 5 0) (length 1) (name "-") (number "6"))
                (pin output line (at 0 6 0) (length 1) (name "O") (number "7"))
                (pin power_in line (at 0 7 0) (length 1) (name "V+") (number "8"))
            )
        )
    )
    (symbol
        (lib_id "Device:R")
        (at 50 50 0)
        (unit 1)
        (in_bom yes)
        (on_board yes)
        (dnp no)
        (uuid "11111111-1111-1111-1111-111111111111")
        (property "Reference" "R1" (at 0 0 0))
        (property "Value" "10k 0402" (at 0 0 0))
        (property "Footprint" "" (at 0 0 0))
        (pin "1" (uuid "11111111-1111-1111-1111-000000000001"))
        (pin "2" (uuid "11111111-1111-1111-1111-000000000002"))
        (instances (project "test" (path "/" (reference "R1") (unit 1))))
    )
    (symbol
        (lib_id "Amplifier_Operational:LM358")
        (at 100 50 0)
        (unit 1)
        (in_bom yes)
        (on_board yes)
        (dnp no)
        (uuid "22222222-2222-2222-2222-222222222222")
        (property "Reference" "U1" (at 0 0 0))
        (property "Value" "LM358" (at 0 0 0))
        (property "Footprint" "" (at 0 0 0))
        (pin "1" (uuid "22222222-2222-2222-2222-000000000001"))
        (pin "2" (uuid "22222222-2222-2222-2222-000000000002"))
        (pin "3" (uuid "22222222-2222-2222-2222-000000000003"))
        (pin "4" (uuid "22222222-2222-2222-2222-000000000004"))
        (pin "5" (uuid "22222222-2222-2222-2222-000000000005"))
        (pin "6" (uuid "22222222-2222-2222-2222-000000000006"))
        (pin "7" (uuid "22222222-2222-2222-2222-000000000007"))
        (pin "8" (uuid "22222222-2222-2222-2222-000000000008"))
        (instances (project "test" (path "/" (reference "U1") (unit 1))))
    )
    (sheet_instances (path "/" (page "1")))
)
"""

# A single footprint-less passive (no unknown IC), used to prove the happy
# path writes a real assignment and exits 0.
_SCH_PASSIVE_ONLY = """(kicad_sch
    (version 20231120)
    (generator "kicadtools_test")
    (uuid "00000000-0000-0000-0000-0000000000ac")
    (paper "A4")
    (lib_symbols
        (symbol "Device:C"
            (symbol "C_1_1"
                (pin passive line (at 0 3.81 270) (length 1.27) (name "~") (number "1"))
                (pin passive line (at 0 -3.81 90) (length 1.27) (name "~") (number "2"))
            )
        )
    )
    (symbol
        (lib_id "Device:C")
        (at 50 50 0)
        (unit 1)
        (in_bom yes)
        (on_board yes)
        (dnp no)
        (uuid "33333333-3333-3333-3333-333333333333")
        (property "Reference" "C1" (at 0 0 0))
        (property "Value" "100nF 0603" (at 0 0 0))
        (property "Footprint" "" (at 0 0 0))
        (pin "1" (uuid "33333333-3333-3333-3333-000000000001"))
        (pin "2" (uuid "33333333-3333-3333-3333-000000000002"))
        (instances (project "test" (path "/" (reference "C1") (unit 1))))
    )
    (sheet_instances (path "/" (page "1")))
)
"""


def _no_global_libs(*_args, **_kwargs):
    return LibraryPaths(footprints_path=None, source="auto")


def _patch_no_libs(monkeypatch):
    monkeypatch.setattr(sch_assign_footprints, "detect_kicad_library_path", _no_global_libs)
    monkeypatch.setattr(sch_suggest_footprint, "detect_kicad_library_path", _no_global_libs)


def _write(tmp_path, content, name="proj.kicad_sch"):
    sch = tmp_path / name
    sch.write_text(content, encoding="utf-8")
    return sch


def test_passive_auto_assigned_with_no_library(tmp_path, monkeypatch, capsys):
    """A footprint-less 0603 cap is auto-assigned by heuristic, no library needed."""
    _patch_no_libs(monkeypatch)
    sch = _write(tmp_path, _SCH_PASSIVE_ONLY)

    rc = run_assign_footprints(
        sch, dry_run=True, output_format="json", assign_missing=True, backup=False
    )
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assigned = {r["reference"]: r for r in data["assigned"]}
    assert "C1" in assigned
    assert assigned["C1"]["footprint"] == "Capacitor_SMD:C_0603_1608Metric"
    assert assigned["C1"]["assigned_by"] == "heuristic"


def test_passive_written_to_disk_with_no_library(tmp_path, monkeypatch, capsys):
    """End-to-end write: the heuristic footprint lands in the schematic file."""
    _patch_no_libs(monkeypatch)
    sch = _write(tmp_path, _SCH_PASSIVE_ONLY)

    rc = run_assign_footprints(
        sch, dry_run=False, output_format="text", assign_missing=True, backup=False
    )
    assert rc == 0
    written = sch.read_text(encoding="utf-8")
    assert '"Capacitor_SMD:C_0603_1608Metric"' in written


def test_unknown_ic_fails_loud(tmp_path, monkeypatch, capsys):
    """A footprint-less unknown IC causes a non-zero exit even when the
    passive in the same schematic IS auto-assigned (fail-loud)."""
    _patch_no_libs(monkeypatch)
    sch = _write(tmp_path, _SCH)

    rc = run_assign_footprints(
        sch, dry_run=True, output_format="json", assign_missing=True, backup=False
    )
    captured = capsys.readouterr()
    out, err = captured.out, captured.err
    # Non-zero: U1 (the op-amp) could not be resolved.
    assert rc == 1
    data = json.loads(out)
    assigned = {r["reference"] for r in data["assigned"]}
    # The passive still gets assigned...
    assert "R1" in assigned
    # ...but the IC is NOT auto-assigned -- it surfaces as unresolved.
    assert "U1" not in assigned
    unresolved = {r["reference"] for r in data["ambiguous"]} | {
        r["reference"] for r in data["no_candidates"]
    }
    assert "U1" in unresolved
    # The stderr message names the actionable manual-fix path.
    assert "U1" in err
    assert "by hand" in err


def test_unknown_ic_still_writes_resolved_passive(tmp_path, monkeypatch, capsys):
    """Fail-loud must not throw away the work it COULD do: the passive is
    still written even though the run exits non-zero for the unknown IC."""
    _patch_no_libs(monkeypatch)
    sch = _write(tmp_path, _SCH)

    rc = run_assign_footprints(
        sch, dry_run=False, output_format="text", assign_missing=True, backup=False
    )
    assert rc == 1
    written = sch.read_text(encoding="utf-8")
    assert '"Resistor_SMD:R_0402_1005Metric"' in written
    # Exactly one footprint was written -- U1 was NOT auto-assigned, so no
    # IC footprint string appears for it.
    assert written.count("Resistor_SMD:R_0402_1005Metric") == 1
    assert "Package_" not in written  # no IC footprint guessed for U1


def test_preflight_gate_trips_on_missing_footprint(tmp_path):
    """The pre-flight gate must FAIL (error severity, non-passing) when a
    schematic symbol is missing a footprint (issue #3866)."""
    from kicad_tools.cli.sch_preflight import run_preflight
    from kicad_tools.cli.sch_validate import check_missing_footprints

    sch = _write(tmp_path, _SCH)

    issues = check_missing_footprints(str(sch))
    missing = [
        i
        for i in issues
        if i.category == "footprint" and i.message.startswith("Missing footprint:")
    ]
    # Both R1 and U1 are footprint-less here.
    assert len(missing) == 2
    # They are ERRORS now, not warnings -- so the gate fails loud.
    assert all(i.severity == "error" for i in missing)
    # The message names the actionable fix.
    assert all("assign-footprints" in i.message for i in missing)

    # run_preflight aggregates these as errors -> result does not pass.
    result = run_preflight(str(sch))
    assert result.error_count >= 2
    assert result.passed is False


def test_without_assign_missing_no_library_errors(tmp_path, monkeypatch, capsys):
    """Without --assign-missing and no library, the old hard error stands."""
    _patch_no_libs(monkeypatch)
    sch = _write(tmp_path, _SCH_PASSIVE_ONLY)

    rc = run_assign_footprints(sch, dry_run=True, output_format="text", assign_missing=False)
    err = capsys.readouterr().err
    assert rc == 1
    assert "No KiCad footprint library found" in err
    # The new hint points at --assign-missing.
    assert "--assign-missing" in err


# A schematic mixing a synthesized ``kicad_tools_pwr:`` power-flag symbol
# (reference ``#PWR01``, footprint ``""`` by design) with a real
# footprint-less passive. The power flag must be SKIPPED by the
# missing-footprint gate (it is virtual, never placed on the PCB), while
# the passive must still trip it. Regression for the false positive the
# Judge found on boards 01-voltage-divider / 04-stm32-devboard (#3866):
# the skip rule recognized only the stock ``power:`` library and missed
# the ``kicad_tools_pwr:`` library that generators synthesize.
_SCH_PWR_FLAG = """(kicad_sch
    (version 20231120)
    (generator "kicadtools_test")
    (uuid "00000000-0000-0000-0000-0000000000ad")
    (paper "A4")
    (lib_symbols
        (symbol "kicad_tools_pwr:VIN"
            (power)
            (symbol "VIN_1_1"
                (pin power_in line (at 0 0 90) (length 0) (name "VIN") (number "1"))
            )
        )
        (symbol "Device:R"
            (symbol "R_1_1"
                (pin passive line (at 0 3.81 270) (length 1.27) (name "~") (number "1"))
                (pin passive line (at 0 -3.81 90) (length 1.27) (name "~") (number "2"))
            )
        )
    )
    (symbol
        (lib_id "kicad_tools_pwr:VIN")
        (at 50 40 0)
        (unit 1)
        (in_bom no)
        (on_board no)
        (dnp no)
        (uuid "44444444-4444-4444-4444-444444444444")
        (property "Reference" "#PWR01" (at 0 0 0))
        (property "Value" "VIN" (at 0 0 0))
        (property "Footprint" "" (at 0 0 0))
        (pin "1" (uuid "44444444-4444-4444-4444-000000000001"))
        (instances (project "test" (path "/" (reference "#PWR01") (unit 1))))
    )
    (symbol
        (lib_id "Device:R")
        (at 50 60 0)
        (unit 1)
        (in_bom yes)
        (on_board yes)
        (dnp no)
        (uuid "55555555-5555-5555-5555-555555555555")
        (property "Reference" "R1" (at 0 0 0))
        (property "Value" "10k 0402" (at 0 0 0))
        (property "Footprint" "" (at 0 0 0))
        (pin "1" (uuid "55555555-5555-5555-5555-000000000001"))
        (pin "2" (uuid "55555555-5555-5555-5555-000000000002"))
        (instances (project "test" (path "/" (reference "R1") (unit 1))))
    )
    (sheet_instances (path "/" (page "1")))
)
"""

# A schematic containing ONLY a footprint-less ``kicad_tools_pwr:``
# power-flag symbol -- mirrors boards 01/04, which failed pre-flight on a
# single such symbol before the fix. The gate must be clean (no errors).
_SCH_PWR_FLAG_ONLY = """(kicad_sch
    (version 20231120)
    (generator "kicadtools_test")
    (uuid "00000000-0000-0000-0000-0000000000ae")
    (paper "A4")
    (lib_symbols
        (symbol "kicad_tools_pwr:VIN"
            (power)
            (symbol "VIN_1_1"
                (pin power_in line (at 0 0 90) (length 0) (name "VIN") (number "1"))
            )
        )
    )
    (symbol
        (lib_id "kicad_tools_pwr:VIN")
        (at 50 40 0)
        (unit 1)
        (in_bom no)
        (on_board no)
        (dnp no)
        (uuid "66666666-6666-6666-6666-666666666666")
        (property "Reference" "#PWR01" (at 0 0 0))
        (property "Value" "VIN" (at 0 0 0))
        (property "Footprint" "" (at 0 0 0))
        (pin "1" (uuid "66666666-6666-6666-6666-000000000001"))
        (instances (project "test" (path "/" (reference "#PWR01") (unit 1))))
    )
    (sheet_instances (path "/" (page "1")))
)
"""


def test_pwr_flag_lib_skipped_by_missing_footprint_gate(tmp_path):
    """A synthesized ``kicad_tools_pwr:`` power flag (footprint ``""`` by
    design) must NOT trip the missing-footprint gate, while a real
    footprint-less passive in the same schematic still MUST (#3866)."""
    from kicad_tools.cli.sch_validate import check_missing_footprints

    sch = _write(tmp_path, _SCH_PWR_FLAG)

    issues = check_missing_footprints(str(sch))
    missing = [
        i
        for i in issues
        if i.category == "footprint" and i.message.startswith("Missing footprint:")
    ]
    refs = [i.message for i in missing]
    # The power flag (#PWR01 / kicad_tools_pwr:VIN) is excluded...
    assert not any("#PWR01" in m for m in refs), refs
    # ...but the genuine passive R1 still trips the gate.
    assert len(missing) == 1
    assert "R1" in missing[0].message
    assert missing[0].severity == "error"


def test_pwr_flag_only_schematic_passes_export_preflight(tmp_path):
    """The export preflight footprint gate must report OK (not FAIL) on a
    schematic whose only footprint-less symbol is a ``kicad_tools_pwr:``
    power flag -- the exact false positive seen on boards 01/04."""
    from kicad_tools.export.preflight import PreflightChecker

    sch = _write(tmp_path, _SCH_PWR_FLAG_ONLY)
    # A trivial empty PCB so the checker can construct; only the schematic
    # footprint gate is exercised here.
    pcb = tmp_path / "proj.kicad_pcb"
    pcb.write_text(
        "(kicad_pcb (version 20240108) (generator pcbnew) (general) (layers) (setup))\n",
        encoding="utf-8",
    )

    checker = PreflightChecker(pcb_path=str(pcb), schematic_path=str(sch))
    result = checker._check_schematic_footprints()
    assert result.status == "OK", result.message


def test_pwr_flag_only_schematic_passes_sch_validate_gate(tmp_path):
    """``check_missing_footprints`` must return zero footprint errors for a
    power-flag-only schematic (boards 01/04 regression)."""
    from kicad_tools.cli.sch_validate import check_missing_footprints

    sch = _write(tmp_path, _SCH_PWR_FLAG_ONLY)
    issues = check_missing_footprints(str(sch))
    errors = [i for i in issues if i.severity == "error" and i.category == "footprint"]
    assert errors == []
