"""Tests for the ``sch assign-footprints`` bulk-assign CLI.

Gating strategy mirrors ``test_sch_suggest_footprint.py``: tests that need
real ``.kicad_mod`` files from the standard KiCad library are gated behind
``LibraryPaths.found``; tests that build a self-contained project (its own
``fp-lib-table`` + ``.pretty`` directory in ``tmp_path``) run everywhere.

The ambiguity policy under test is the one documented in
:mod:`kicad_tools.cli.sch_assign_footprints`: top candidate's
``(filter_match, keyword_match, hand_solder)`` tier must strictly out-rank
the runner-up's.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kicad_tools.cli import sch_assign_footprints, sch_suggest_footprint
from kicad_tools.cli.sch_assign_footprints import (
    _is_unambiguous,
    run_assign_footprints,
)
from kicad_tools.cli.sch_footprint_common import iter_missing_footprint_symbols
from kicad_tools.cli.sch_validate import check_missing_footprints
from kicad_tools.footprints.library_path import (
    LibraryPaths,
    detect_kicad_library_path,
)

FIXTURE = Path(__file__).parent / "fixtures" / "missing_footprint.kicad_sch"

_LIBS_AVAILABLE = detect_kicad_library_path().found

requires_kicad_libs = pytest.mark.skipif(
    not _LIBS_AVAILABLE,
    reason="KiCad footprint libraries not installed in this environment",
)

# ---------------------------------------------------------------------------
# Self-contained project fixtures (no KiCad libs needed)
# ---------------------------------------------------------------------------

# Minimal 2-pad footprint definition.
_MINI_FP_2 = """(footprint "OnlyChoice"
    (version 20240108)
    (generator "kicadtools_test")
    (layer "F.Cu")
    (attr smd)
    (pad "1" smd roundrect (at -1 0) (size 1 1) (layers "F.Cu"))
    (pad "2" smd roundrect (at 1 0) (size 1 1) (layers "F.Cu"))
)
"""

_MINI_FP_2_ALT = """(footprint "SecondChoice"
    (version 20240108)
    (generator "kicadtools_test")
    (layer "F.Cu")
    (attr smd)
    (pad "1" smd roundrect (at -1 0) (size 1 1) (layers "F.Cu"))
    (pad "2" smd roundrect (at 1 0) (size 1 1) (layers "F.Cu"))
)
"""

# A 5-pin footprint to match U7 in the fixture (so we can test "exactly one
# project candidate" winning the auto-assign even when global libraries are
# disabled).
_MINI_FP_5 = """(footprint "Only5PinHere"
    (version 20240108)
    (generator "kicadtools_test")
    (layer "F.Cu")
    (attr smd)
    (pad "1" smd roundrect (at -1 0) (size 1 1) (layers "F.Cu"))
    (pad "2" smd roundrect (at 0 0) (size 1 1) (layers "F.Cu"))
    (pad "3" smd roundrect (at 1 0) (size 1 1) (layers "F.Cu"))
    (pad "4" smd roundrect (at -1 1) (size 1 1) (layers "F.Cu"))
    (pad "5" smd roundrect (at 1 1) (size 1 1) (layers "F.Cu"))
)
"""

# A 3-symbol schematic: one already-assigned (R1), one with a single matching
# candidate (R2, will be assigned), one ambiguous (R3, two equally-ranked
# matches in the project library). The reason we test "ambiguous" here with a
# project library is that we get full control over how many candidates exist.
_THREE_SYMBOL_SCH = """(kicad_sch
    (version 20231120)
    (generator "kicadtools_test")
    (uuid "00000000-0000-0000-0000-0000000000aa")
    (paper "A4")
    (lib_symbols
        (symbol "Device:R"
            (pin_numbers hide)
            (pin_names hide)
            (symbol "R_0_1"
                (rectangle
                    (start -1.016 -2.54)
                    (end 1.016 2.54)
                    (stroke (width 0.254))
                    (fill (type none))
                )
            )
            (symbol "R_1_1"
                (pin passive line
                    (at 0 3.81 270)
                    (length 1.27)
                    (name "~")
                    (number "1")
                )
                (pin passive line
                    (at 0 -3.81 90)
                    (length 1.27)
                    (name "~")
                    (number "2")
                )
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
        (property "Value" "10k" (at 0 0 0))
        (property "Footprint" "Existing:R_0805_Already_Set" (at 0 0 0))
        (property "Datasheet" "~" (at 0 0 0))
        (pin "1" (uuid "11111111-1111-1111-1111-000000000001"))
        (pin "2" (uuid "11111111-1111-1111-1111-000000000002"))
        (instances
            (project "test"
                (path "/" (reference "R1") (unit 1))
            )
        )
    )
    (symbol
        (lib_id "Device:R")
        (at 100 50 0)
        (unit 1)
        (in_bom yes)
        (on_board yes)
        (dnp no)
        (uuid "22222222-2222-2222-2222-222222222222")
        (property "Reference" "R2" (at 0 0 0))
        (property "Value" "1k" (at 0 0 0))
        (property "Footprint" "" (at 0 0 0))
        (property "Datasheet" "~" (at 0 0 0))
        (pin "1" (uuid "22222222-2222-2222-2222-000000000001"))
        (pin "2" (uuid "22222222-2222-2222-2222-000000000002"))
        (instances
            (project "test"
                (path "/" (reference "R2") (unit 1))
            )
        )
    )
    (symbol
        (lib_id "Device:R")
        (at 150 50 0)
        (unit 1)
        (in_bom yes)
        (on_board yes)
        (dnp no)
        (uuid "33333333-3333-3333-3333-333333333333")
        (property "Reference" "R3" (at 0 0 0))
        (property "Value" "100" (at 0 0 0))
        (property "Footprint" "" (at 0 0 0))
        (property "Datasheet" "~" (at 0 0 0))
        (pin "1" (uuid "33333333-3333-3333-3333-000000000001"))
        (pin "2" (uuid "33333333-3333-3333-3333-000000000002"))
        (instances
            (project "test"
                (path "/" (reference "R3") (unit 1))
            )
        )
    )
    (sheet_instances
        (path "/" (page "1"))
    )
)
"""

_R_ONLY_LIB_TABLE = (
    '(fp_lib_table\n'
    '  (version 7)\n'
    '  (lib (name "OnlyLib") (type "KiCad") (uri "${KIPRJMOD}/OnlyLib.pretty")'
    '       (options "") (descr "Test"))\n'
    ')'
)


def _make_project(
    tmp_path: Path,
    *,
    add_second_candidate: bool = False,
) -> Path:
    """Build a self-contained test project under *tmp_path*.

    When *add_second_candidate* is True, a second 2-pad footprint is added to
    the project library so that R2 / R3 see two equally-ranked candidates
    (ambiguous, by policy).
    """
    (tmp_path / "proj.kicad_pro").write_text("{}", encoding="utf-8")
    (tmp_path / "fp-lib-table").write_text(_R_ONLY_LIB_TABLE, encoding="utf-8")

    lib_dir = tmp_path / "OnlyLib.pretty"
    lib_dir.mkdir()
    (lib_dir / "OnlyChoice.kicad_mod").write_text(_MINI_FP_2, encoding="utf-8")
    if add_second_candidate:
        (lib_dir / "SecondChoice.kicad_mod").write_text(
            _MINI_FP_2_ALT, encoding="utf-8"
        )

    sch = tmp_path / "proj.kicad_sch"
    sch.write_text(_THREE_SYMBOL_SCH, encoding="utf-8")
    return sch


def _no_global_libs(*_args, **_kwargs):
    """Helper monkeypatch target: make `detect_kicad_library_path` return empty."""
    return LibraryPaths(footprints_path=None, source="auto")


# ---------------------------------------------------------------------------
# Ambiguity-policy unit tests (pure, no IO)
# ---------------------------------------------------------------------------


def test_is_unambiguous_empty_list():
    """Empty candidate list -> not unambiguous (also: nothing to assign)."""
    assert _is_unambiguous([], [], None) is False


def test_is_unambiguous_single_candidate():
    """Single candidate is trivially unambiguous (nothing to be ambiguous with)."""
    cand = [{"library": "L", "footprint": "F1", "pads": 2, "origin": "global"}]
    assert _is_unambiguous(cand, [], None) is True


def test_is_unambiguous_top_beats_runner_up_on_filter():
    """Filter-match tier beats non-match: clearly unambiguous."""
    cands = [
        {"library": "L", "footprint": "MATCH_X", "pads": 2, "origin": "global"},
        {"library": "L", "footprint": "OTHER", "pads": 2, "origin": "global"},
    ]
    assert _is_unambiguous(cands, ["MATCH*"], None) is True


def test_is_unambiguous_two_equally_ranked_candidates():
    """Two filter-matching candidates -> ambiguous (alphabetical tiebreak only)."""
    cands = [
        {"library": "L", "footprint": "MATCH_A", "pads": 2, "origin": "global"},
        {"library": "L", "footprint": "MATCH_B", "pads": 2, "origin": "global"},
    ]
    assert _is_unambiguous(cands, ["MATCH*"], None) is False


def test_is_unambiguous_handsolder_breaks_tie():
    """Non-handsolder strictly out-ranks handsolder -> unambiguous."""
    cands = [
        {"library": "L", "footprint": "X_Plain", "pads": 2, "origin": "global"},
        {"library": "L", "footprint": "X_HandSoldering", "pads": 2, "origin": "global"},
    ]
    assert _is_unambiguous(cands, [], None) is True


# ---------------------------------------------------------------------------
# Project-fixture tests (do NOT need KiCad libs)
# ---------------------------------------------------------------------------


def test_dry_run_unambiguous_assignment_reports_mapping(
    tmp_path, monkeypatch, capsys
):
    """``--dry-run --format json`` proposes an unambiguous assignment for R2/R3.

    Only ``OnlyChoice`` exists in the project library, so both empty-footprint
    symbols (R2, R3) get the same single-candidate match. R1 is pre-assigned
    and must be skipped.
    """
    monkeypatch.setattr(
        sch_assign_footprints, "detect_kicad_library_path", _no_global_libs
    )
    # find_footprint_candidates pulls from this module too:
    monkeypatch.setattr(
        sch_suggest_footprint, "detect_kicad_library_path", _no_global_libs
    )

    sch = _make_project(tmp_path)
    rc = run_assign_footprints(sch, dry_run=True, output_format="json")
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    refs = {r["reference"]: r for r in data["assigned"]}
    assert "R2" in refs and "R3" in refs
    assert refs["R2"]["footprint"] == "OnlyLib:OnlyChoice"
    # R1 had a footprint already -> not in the scanned set (default), so it
    # cannot show up as assigned / ambiguous / no_candidates.
    assert all(r["reference"] != "R1" for r in data["assigned"])
    assert all(r["reference"] != "R1" for r in data["ambiguous"])
    assert all(r["reference"] != "R1" for r in data["no_candidates"])
    # JSON contract.
    assert {"assigned", "ambiguous", "no_candidates", "dry_run"}.issubset(data)
    assert data["dry_run"] is True


def test_assignment_writes_mapping_and_creates_backup(
    tmp_path, monkeypatch, capsys
):
    """Full write path: file actually mutates, ``.bak`` exists."""
    monkeypatch.setattr(
        sch_assign_footprints, "detect_kicad_library_path", _no_global_libs
    )
    monkeypatch.setattr(
        sch_suggest_footprint, "detect_kicad_library_path", _no_global_libs
    )

    sch = _make_project(tmp_path)
    rc = run_assign_footprints(sch, dry_run=False, output_format="text")
    assert rc == 0

    written = sch.read_text(encoding="utf-8")
    assert '"OnlyLib:OnlyChoice"' in written
    # The pre-existing footprint on R1 must be untouched.
    assert '"Existing:R_0805_Already_Set"' in written
    # Backup file exists (pattern: <stem>_backup_<timestamp>.kicad_sch).
    backups = list(sch.parent.glob(f"{sch.stem}_backup_*{sch.suffix}"))
    assert backups, f"no backup file found in {sch.parent}"


def test_no_force_does_not_overwrite_existing_footprint(
    tmp_path, monkeypatch, capsys
):
    """Without ``--force``, R1's pre-assigned footprint is left alone."""
    monkeypatch.setattr(
        sch_assign_footprints, "detect_kicad_library_path", _no_global_libs
    )
    monkeypatch.setattr(
        sch_suggest_footprint, "detect_kicad_library_path", _no_global_libs
    )

    sch = _make_project(tmp_path)
    run_assign_footprints(sch, dry_run=True, output_format="json")
    out = capsys.readouterr().out
    data = json.loads(out)
    seen_refs = (
        [r["reference"] for r in data["assigned"]]
        + [r["reference"] for r in data["ambiguous"]]
        + [r["reference"] for r in data["no_candidates"]]
    )
    assert "R1" not in seen_refs, "R1 already had a footprint and must be skipped"


def test_force_reconsiders_already_assigned_symbol(
    tmp_path, monkeypatch, capsys
):
    """``--force`` flips R1 back into the scan -> it gets the same OnlyChoice."""
    monkeypatch.setattr(
        sch_assign_footprints, "detect_kicad_library_path", _no_global_libs
    )
    monkeypatch.setattr(
        sch_suggest_footprint, "detect_kicad_library_path", _no_global_libs
    )

    sch = _make_project(tmp_path)
    run_assign_footprints(sch, dry_run=True, output_format="json", force=True)
    out = capsys.readouterr().out
    data = json.loads(out)
    seen_refs = (
        [r["reference"] for r in data["assigned"]]
        + [r["reference"] for r in data["ambiguous"]]
    )
    # R1 should now be considered (assigned in this case — single candidate).
    assert "R1" in seen_refs


def test_two_equally_ranked_candidates_are_ambiguous(
    tmp_path, monkeypatch, capsys
):
    """With two project candidates and no filters, R2 / R3 are ambiguous."""
    monkeypatch.setattr(
        sch_assign_footprints, "detect_kicad_library_path", _no_global_libs
    )
    monkeypatch.setattr(
        sch_suggest_footprint, "detect_kicad_library_path", _no_global_libs
    )

    sch = _make_project(tmp_path, add_second_candidate=True)
    rc = run_assign_footprints(sch, dry_run=True, output_format="json")
    out = capsys.readouterr().out
    # No assignments succeed -> nonzero by policy (symbols existed but
    # nothing was assignable).
    assert rc == 1
    data = json.loads(out)
    ambiguous_refs = {r["reference"] for r in data["ambiguous"]}
    assert "R2" in ambiguous_refs and "R3" in ambiguous_refs
    assert data["assigned"] == []


def test_no_library_returns_nonzero(tmp_path, monkeypatch, capsys):
    """No global libs AND no project table -> actionable error + exit 1."""
    monkeypatch.setattr(
        sch_assign_footprints, "detect_kicad_library_path", _no_global_libs
    )

    # No fp-lib-table; just the schematic in a plain directory.
    sch = tmp_path / "proj.kicad_sch"
    sch.write_text(_THREE_SYMBOL_SCH, encoding="utf-8")

    rc = run_assign_footprints(sch)
    err = capsys.readouterr().err
    assert rc == 1
    assert "No KiCad footprint library found" in err
    assert "KICAD_FOOTPRINT_DIR" in err


def test_missing_schematic_file_returns_nonzero(capsys):
    rc = run_assign_footprints(Path("/nonexistent/board.kicad_sch"))
    err = capsys.readouterr().err
    assert rc == 1
    assert "not found" in err.lower()


def test_text_output_lists_assigned_and_ambiguous(
    tmp_path, monkeypatch, capsys
):
    """Default text format renders each bucket so a CLI user can read it."""
    monkeypatch.setattr(
        sch_assign_footprints, "detect_kicad_library_path", _no_global_libs
    )
    monkeypatch.setattr(
        sch_suggest_footprint, "detect_kicad_library_path", _no_global_libs
    )

    sch = _make_project(tmp_path, add_second_candidate=True)
    run_assign_footprints(sch, dry_run=True, output_format="text")
    out = capsys.readouterr().out
    assert "assign-footprints" in out
    assert "Ambiguous (" in out
    # The header line carries the bucket counts.
    assert "ambiguous:" in out


def test_no_missing_symbols_returns_zero(tmp_path, monkeypatch, capsys):
    """A schematic with every footprint already set is a success (0), not a 1."""
    monkeypatch.setattr(
        sch_assign_footprints, "detect_kicad_library_path", _no_global_libs
    )
    monkeypatch.setattr(
        sch_suggest_footprint, "detect_kicad_library_path", _no_global_libs
    )

    # Every symbol has a non-empty Footprint property.
    pre_assigned_sch = _THREE_SYMBOL_SCH.replace(
        '(property "Footprint" "" ',
        '(property "Footprint" "Existing:Pre" ',
    )
    sch = tmp_path / "proj.kicad_sch"
    sch.write_text(pre_assigned_sch, encoding="utf-8")
    # Project table needed so we don't trip the no-library error path.
    (tmp_path / "proj.kicad_pro").write_text("{}", encoding="utf-8")
    (tmp_path / "fp-lib-table").write_text(_R_ONLY_LIB_TABLE, encoding="utf-8")
    lib_dir = tmp_path / "OnlyLib.pretty"
    lib_dir.mkdir()
    (lib_dir / "OnlyChoice.kicad_mod").write_text(_MINI_FP_2, encoding="utf-8")

    rc = run_assign_footprints(sch, dry_run=True, output_format="json")
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["scanned"] == 0
    assert data["assigned"] == []


def test_iter_missing_matches_preflight_check(tmp_path, monkeypatch):
    """Invariant: ``iter_missing_footprint_symbols`` agrees with
    ``check_missing_footprints`` on which symbols are missing footprints.

    Guards against the two enumerations drifting on power/dnp/empty rules.
    """
    sch = _make_project(tmp_path)

    iter_refs = {
        sym.reference
        for _node, sym, _sch in iter_missing_footprint_symbols(sch)
    }
    preflight_refs: set[str] = set()
    for issue in check_missing_footprints(str(sch)):
        if issue.category == "footprint" and issue.severity == "warning":
            # The validator message format is "Missing footprint: REF (VALUE)".
            msg = issue.message
            if msg.startswith("Missing footprint:"):
                tail = msg[len("Missing footprint:"):].strip()
                ref = tail.split(" ", 1)[0]
                preflight_refs.add(ref)

    assert iter_refs == preflight_refs


# ---------------------------------------------------------------------------
# Pin-count validation regression (project-only, deterministic)
# ---------------------------------------------------------------------------


def test_no_validate_flag_propagates_to_set_footprint(
    tmp_path, monkeypatch, capsys
):
    """``validate=False`` must thread through to :func:`run_set_footprint`.

    Regression for the assign-write split: the new path must respect the
    ``--no-validate`` flag the user passed. We verify by intercepting the
    ``run_set_footprint`` call and inspecting the kwarg.
    """
    monkeypatch.setattr(
        sch_assign_footprints, "detect_kicad_library_path", _no_global_libs
    )
    monkeypatch.setattr(
        sch_suggest_footprint, "detect_kicad_library_path", _no_global_libs
    )

    captured: dict[str, Any] = {}

    def _capture_set_footprint(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(
        sch_assign_footprints, "run_set_footprint", _capture_set_footprint
    )

    sch = _make_project(tmp_path)
    rc = run_assign_footprints(
        sch, dry_run=False, output_format="json", validate=False, backup=False
    )
    assert rc == 0
    assert captured.get("validate") is False
    assert captured.get("backup") is False
    # The mapping handed down must contain the resolved Library:Footprint string.
    assert captured.get("mapping") == {
        "R2": "OnlyLib:OnlyChoice",
        "R3": "OnlyLib:OnlyChoice",
    }


@requires_kicad_libs
def test_pin_count_validation_still_fires_through_assign_path(
    tmp_path, monkeypatch, capsys
):
    """End-to-end: when the assign path produces a wrong-pad-count mapping,
    ``run_set_footprint``'s validator must still emit its warning.

    This guards against the assign path silently bypassing validation.
    We force-produce a bad mapping by monkey-patching
    ``find_footprint_candidates`` to return a 5-pad candidate for the
    2-pin Device:R symbols.
    """
    sch = tmp_path / "proj.kicad_sch"
    sch.write_text(_THREE_SYMBOL_SCH, encoding="utf-8")

    # Pretend a real Resistor_SMD footprint with 5 pads exists (any name that
    # _footprint_pad_count can resolve in the installed library will do).
    def _bad_candidate(*_args, **_kwargs):
        # Resistor_SMD:R_Array_Convex_4x0603 has 8 pads in standard KiCad;
        # any non-2-pad real footprint trips the validation against R2/R3.
        return [
            {
                "library": "Resistor_SMD",
                "footprint": "R_Array_Convex_4x0603",
                "pads": 8,
                "origin": "global",
            }
        ]

    monkeypatch.setattr(
        sch_assign_footprints, "find_footprint_candidates", _bad_candidate
    )

    run_assign_footprints(
        sch, dry_run=False, output_format="text", validate=True, no_project_lib=True
    )
    err = capsys.readouterr().err
    assert "pin-count mismatch" in err


# ---------------------------------------------------------------------------
# Library-gated tests against the real KiCad library
# ---------------------------------------------------------------------------


@requires_kicad_libs
def test_real_library_classifies_u7_as_ambiguous(capsys):
    """U7 from the real fixture has 3+ equally-ranked SOT alternatives.

    The 74LVC1G17 ``ki_fp_filters`` deliberately list multiple physically
    distinct 5-pin packages (SOT-23-5, SOT-553, Texas R-PDSO-G5-DCK, ...).
    A conservative auto-assigner correctly refuses to pick for the user.
    """
    rc = run_assign_footprints(FIXTURE, dry_run=True, output_format="json")
    out = capsys.readouterr().out
    # Both R1 and U7 are ambiguous (R1: no filters, many 2-pad parts;
    # U7: multiple SOT-x family hits) -> nothing to assign -> rc 1.
    assert rc == 1
    data = json.loads(out)
    ambiguous_refs = {r["reference"] for r in data["ambiguous"]}
    assert "U7" in ambiguous_refs
    # SOT-23-5 must show up in U7's top candidates -- the policy is
    # conservative, not blind.
    u7 = next(r for r in data["ambiguous"] if r["reference"] == "U7")
    cand_names = [c["footprint"] for c in u7["candidates"]]
    assert "SOT-23-5" in cand_names


@requires_kicad_libs
def test_real_library_dry_run_emits_bucket_keys(capsys):
    """JSON contract: ``assigned``/``ambiguous``/``no_candidates`` always present."""
    run_assign_footprints(FIXTURE, dry_run=True, output_format="json")
    out = capsys.readouterr().out
    data = json.loads(out)
    assert isinstance(data["assigned"], list)
    assert isinstance(data["ambiguous"], list)
    assert isinstance(data["no_candidates"], list)
    assert data["scanned"] == 3  # R1, U7, NT2
