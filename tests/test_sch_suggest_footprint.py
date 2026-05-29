"""Tests for the ``sch suggest-footprint`` command.

The tests that need real ``.kicad_mod`` files are gated behind
``LibraryPaths.found`` so the suite stays green on KiCad-less CI runners.
The no-library degradation tests must run everywhere and are NOT gated.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli import sch_suggest_footprint
from kicad_tools.cli.sch_suggest_footprint import run_suggest_footprint
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
