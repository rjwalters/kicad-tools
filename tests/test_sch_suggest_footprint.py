"""Tests for the ``sch suggest-footprint`` command.

The tests that need real ``.kicad_mod`` files are gated behind
``LibraryPaths.found`` so the suite stays green on KiCad-less CI runners.
The no-library degradation tests must run everywhere and are NOT gated.
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path

import pytest

from kicad_tools.cli import sch_suggest_footprint
from kicad_tools.cli.sch_suggest_footprint import (
    _get_fp_filters,
    _matches_fp_filters,
    run_suggest_footprint,
)
from kicad_tools.footprints.library_path import (
    LibraryPaths,
    detect_kicad_library_path,
)
from kicad_tools.schema import Schematic

# Canonical ki_fp_filters value for the 74LVC1G17 (from KiCad's 74xGxx.kicad_sym),
# embedded into the U7 lib_symbol of the test fixture.
_EXPECTED_FP_FILTERS = (
    "SOT?23* SOT?553* Texas?R-PDSO-G5?DCK* Texas?R-PDSO-N5?DRL* Texas?X2SON*0.8x0.8mm*P0.48mm*"
)

FIXTURE = Path(__file__).parent / "fixtures" / "missing_footprint.kicad_sch"

_LIBS_AVAILABLE = detect_kicad_library_path().found

requires_kicad_libs = pytest.mark.skipif(
    not _LIBS_AVAILABLE,
    reason="KiCad footprint libraries not installed in this environment",
)


# ---------------------------------------------------------------------------
# Tests that need real library files (gated)
# ---------------------------------------------------------------------------


@requires_kicad_libs
def test_suggest_sot23_for_5pin_buffer(capsys):
    """AC #1: U7 (74LVC1G17, 5 pins) + --package SOT-23 yields SOT-23-5."""
    rc = run_suggest_footprint(FIXTURE, ref="U7", package="SOT-23", limit=20)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Package_TO_SOT_SMD:SOT-23-5" in out
    assert "TSOT-23-5" in out
    # Every suggestion must have 5 pads (the symbol pin count).
    for line in out.splitlines():
        if "pads)" in line:
            assert "(5 pads)" in line


@requires_kicad_libs
def test_suggest_ranks_non_handsolder_first(capsys):
    """Plain SOT-23-5 should rank before its _HandSoldering variant."""
    run_suggest_footprint(FIXTURE, ref="U7", package="SOT-23", limit=20)
    out = capsys.readouterr().out
    plain = out.find("Package_TO_SOT_SMD:SOT-23-5 ")
    hand = out.find("SOT-23-5_HandSoldering")
    assert plain != -1 and hand != -1
    assert plain < hand


@requires_kicad_libs
def test_suggest_r0603_for_resistor(capsys):
    """AC #2: R1 (2 pins) + --package R_0603 yields R_0603 footprints."""
    rc = run_suggest_footprint(FIXTURE, ref="R1", package="R_0603", limit=10)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Resistor_SMD:R_0603_1608Metric" in out


@requires_kicad_libs
def test_suggest_json_format(capsys):
    rc = run_suggest_footprint(FIXTURE, ref="U7", package="SOT-23", output_format="json", limit=5)
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["reference"] == "U7"
    assert data["pin_count"] == 5
    assert data["package_keyword"] == "SOT-23"
    assert len(data["candidates"]) >= 1
    for c in data["candidates"]:
        assert c["pads"] == 5


@requires_kicad_libs
def test_suggest_no_candidates_when_padcount_unmatched(capsys):
    """A 5-pin part with a 2-pad-only package keyword yields no candidates."""
    rc = run_suggest_footprint(FIXTURE, ref="U7", package="R_0603", limit=10)
    err = capsys.readouterr().err
    assert rc == 1
    assert "No matching footprints" in err


# ---------------------------------------------------------------------------
# Tests that run everywhere (NOT gated)
# ---------------------------------------------------------------------------


def test_no_library_graceful_degradation(monkeypatch, capsys):
    """AC #3: no library -> actionable message + non-zero exit, no traceback."""

    def _no_libs(*_args, **_kwargs):
        return LibraryPaths(footprints_path=None, source="auto")

    monkeypatch.setattr(sch_suggest_footprint, "detect_kicad_library_path", _no_libs)

    rc = run_suggest_footprint(FIXTURE, ref="U7", package="SOT-23")
    err = capsys.readouterr().err
    assert rc == 1
    assert "KICAD_FOOTPRINT_DIR" in err
    assert "No KiCad footprint library found" in err


# ---------------------------------------------------------------------------
# ki_fp_filters parse + glob match (KiCad-INDEPENDENT, NOT gated)
# ---------------------------------------------------------------------------


def test_fp_filters_parse_from_fixture():
    """AC: ki_fp_filters parses from the embedded lib_symbol's properties dict."""
    sch = Schematic.load(FIXTURE)
    lib_sym = sch.get_lib_symbol_resolved("74xGxx:74LVC1G17")
    assert lib_sym is not None
    # No new schema field: reuse LibrarySymbol.properties.
    assert lib_sym.properties.get("ki_fp_filters") == _EXPECTED_FP_FILTERS


def test_get_fp_filters_splits_space_separated_patterns():
    """AC: _get_fp_filters returns the space-split list of glob patterns."""
    sch = Schematic.load(FIXTURE)
    sym = sch.get_symbol("U7")
    filters = _get_fp_filters(sch, sym)
    assert filters == [
        "SOT?23*",
        "SOT?553*",
        "Texas?R-PDSO-G5?DCK*",
        "Texas?R-PDSO-N5?DRL*",
        "Texas?X2SON*0.8x0.8mm*P0.48mm*",
    ]


def test_fp_filters_glob_matches_sot23_variants():
    """AC: fnmatch.fnmatchcase glob semantics against the footprint stem.

    Patterns are deliberately broad (SOT-23-5, SOT-23-6, and SOT-23 all match
    ``SOT?23*``), which is why pin-count must AND-combine to disambiguate.
    """
    sch = Schematic.load(FIXTURE)
    sym = sch.get_symbol("U7")
    filters = _get_fp_filters(sch, sym)

    # Direct fnmatch.fnmatchcase semantics (case-sensitive, ? = single char).
    assert fnmatch.fnmatchcase("SOT-23-5", "SOT?23*")
    assert not fnmatch.fnmatchcase("sot-23-5", "SOT?23*")  # case-sensitive

    # The helper applies any-of-patterns matching.
    assert _matches_fp_filters("SOT-23-5", filters)
    assert _matches_fp_filters("SOT-23-6", filters)
    assert _matches_fp_filters("SOT-23", filters)
    assert _matches_fp_filters("SOT-553", filters)
    assert _matches_fp_filters("Texas_X2SON-5_0.8x0.8mm_P0.48mm", filters)
    assert not _matches_fp_filters("QFN-16", filters)


def test_get_fp_filters_empty_when_no_property():
    """AC: symbol without ki_fp_filters -> empty list (Phase 1 fallback)."""
    sch = Schematic.load(FIXTURE)
    r1 = sch.get_symbol("R1")  # Device:R has no ki_fp_filters in the fixture.
    assert _get_fp_filters(sch, r1) == []


def test_matches_fp_filters_empty_filter_list():
    """Empty/None filters match nothing (callers treat as 'do not constrain')."""
    assert not _matches_fp_filters("SOT-23-5", [])
    assert not _matches_fp_filters("SOT-23-5", None)


# ---------------------------------------------------------------------------
# ki_fp_filters ref-only ranking (library-gated)
# ---------------------------------------------------------------------------


@requires_kicad_libs
def test_suggest_ref_only_infers_sot23_5(capsys):
    """AC #1: U7 with NO --package infers SOT-23-5 via ki_fp_filters + pins."""
    rc = run_suggest_footprint(FIXTURE, ref="U7", limit=20)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Package_TO_SOT_SMD:SOT-23-5" in out
    # All candidates satisfy the pin-count AND-combined filter (5 pads).
    for line in out.splitlines():
        if "pads)" in line:
            assert "(5 pads)" in line


@requires_kicad_libs
def test_suggest_ref_only_ranks_sot23_5_first(capsys):
    """AC: filter-matching candidate ranks first for ref-only suggestion."""
    rc = run_suggest_footprint(FIXTURE, ref="U7", output_format="json", limit=20)
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["candidates"], "expected at least one candidate"
    # The top-ranked candidate matches the symbol's ki_fp_filters.
    top = data["candidates"][0]
    assert _matches_fp_filters(top["footprint"], data["fp_filters"])
    assert top["pads"] == 5
    # SOT-23-5 should be present and the non-hand-solder variant ranks ahead.
    names = [c["footprint"] for c in data["candidates"]]
    assert "SOT-23-5" in names


@requires_kicad_libs
def test_suggest_ref_only_json_includes_fp_filters(capsys):
    """AC: JSON output surfaces the resolved fp_filters list."""
    rc = run_suggest_footprint(FIXTURE, ref="U7", output_format="json", limit=5)
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["fp_filters"] == [
        "SOT?23*",
        "SOT?553*",
        "Texas?R-PDSO-G5?DCK*",
        "Texas?R-PDSO-N5?DRL*",
        "Texas?X2SON*0.8x0.8mm*P0.48mm*",
    ]


@requires_kicad_libs
def test_explicit_package_overrides_inferred_filters(capsys):
    """AC: --package overrides inferred filters; fp_filters omitted in JSON."""
    rc = run_suggest_footprint(FIXTURE, ref="U7", package="SOT-23", output_format="json", limit=20)
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    # Explicit --package suppresses the inferred ki_fp_filters.
    assert data["fp_filters"] == []
    assert data["package_keyword"] == "SOT-23"


@requires_kicad_libs
def test_suggest_ref_only_fallback_for_no_filter_symbol(capsys):
    """AC #3: R1 (no ki_fp_filters) still behaves as Phase 1 (no crash)."""
    rc = run_suggest_footprint(FIXTURE, ref="R1", output_format="json", limit=10)
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    # Fallback: no inferred filters, suggestions driven by pin count + value.
    assert data["fp_filters"] == []
    assert data["pin_count"] == 2
    # All candidates must be 2-pad (Phase 1 pin-count behavior preserved).
    for c in data["candidates"]:
        assert c["pads"] == 2


def test_symbol_not_found(capsys):
    rc = run_suggest_footprint(FIXTURE, ref="ZZ99", package="SOT-23")
    err = capsys.readouterr().err
    assert rc == 1
    assert "not found" in err


def test_missing_schematic_file(capsys):
    rc = run_suggest_footprint(Path("/nonexistent/board.kicad_sch"), ref="U7")
    err = capsys.readouterr().err
    assert rc == 1
    assert "not found" in err.lower()
