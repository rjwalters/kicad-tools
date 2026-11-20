"""The #9619 pinned KiCad clearance-band probe, black-box on both engines.

The operator ruling on Issue #9619 splits the clearance slack in two: the
*checker* (``validate/rules/base.py``'s
``KICAD_CLEARANCE_BAND_MM``, applied by ``drc_clear`` at the
copper-clearance verdict sites) takes the largest shortfall pinned KiCad
10.0.6 accepts, so a verdict at kct's boundary is one ``kicad-cli pcb
drc`` agrees on; everything that *writes* copper -- the router (see the
note next to ``router/clearance_kernel.py``'s constant), the via-moving
commands and the placement optimiser's incremental DRC
(``WRITER_CLEARANCE_MARGIN_MM``) -- keeps its own 100 nm margin inside
that band.  Unprobed *non-clearance* checks (track width, drill,
annular, floors, silk, via-in-pad) deliberately keep the 1e-4
``DRC_TOLERANCE``: the #9619 review probed kicad-cli flagging a 100 nm
track-width or drill shortfall outright, so the 500 nm band is
clearance-only evidence.  This file pins the checker's clearance half of
the split to the real KiCad behaviour.

Probe method (2026-11-19, ``kicad/kicad:10.0`` image at the CI-pinned
digest, kicad-cli 10.0.6, ``KCT_KICAD_DOCKER=1``): two nets under a
``Default`` netclass clearance of 0.127 mm, gap swept at 10 nm steps and
refined at 1 nm, on three pair shapes --

* **parallel tracks** (axis-aligned): a 500 nm shortfall is accepted and a
  501 nm shortfall is flagged, so KiCad's cut-off is exactly 500 nm and the
  checker constant is ``5e-4``;
* **45-degree pair**: exact-500 nm is unconstructible (the perpendicular
  offset of two 45-degree tracks is ``(p + q) / sqrt(2)`` for integer-nm
  offsets, hence irrational), so the boundary is pinned by the closest
  constructible pair, 1 nm of offset apart: true shortfalls 499.10 nm
  (accepted) and 500.51 nm (flagged);
* **pad-to-track** (circular SMD pad vs a track): flips at the same edge as
  the parallel pair (500 nm accepted, 501 nm flagged), so the band is not a
  parallel-tracks-only artefact.

The #9724 leg (2026-11-20, same image digest and kicad-cli) probed
``copper_edge_clearance`` the same way -- one 0.2 mm track parallel to a
straight rectangular-board edge under a 0.5 mm
``min_copper_edge_clearance`` -- and found the *same* 500 nm cut-off:
500 nm accepted, 501 nm flagged (0/400/500 clear, 501/510/600 flagged in
one run).  ``edge.py``'s copper-to-edge comparisons (traces, SMD pads,
zones) therefore ride ``drc_clear`` too; its hole-minimum branches (vias,
through-hole pads) keep the plain 1e-4 epsilon -- the copper-edge probe
cannot establish a tolerance for a separate hole minimum.

The rule-level tests below run everywhere and pin the checker's verdicts on
those exact boards.  The black-box tests additionally re-run
``kicad-cli pcb drc`` on the same boards -- the pinned 10.0.6 binary is
*required* (a missing or wrong-version one fails the test; CI installs it
natively, ``KCT_KICAD_DOCKER=1`` routes it through the image) -- and fail if
the two engines disagree, so a future KiCad that changes its tolerance breaks
this test instead of silently diverging from kct.
"""

from __future__ import annotations

import copy
import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string
from kicad_tools.validate.rules.base import (
    DRC_FLOAT_GUARD_MM,
    KICAD_CLEARANCE_BAND_MM,
)
from kicad_tools.validate.rules.clearance import ClearanceRule
from kicad_tools.validate.rules.edge import EdgeClearanceRule

MIN_CLEARANCE = 0.127
TRACK_WIDTH = 0.2
MIN_EDGE_CLEARANCE = 0.5

_HEADER = """(kicad_pcb (version 20241229) (generator "probe_9619")
\t(general (thickness 1.6) (legacy_teardrops no))
\t(paper "A4")
\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
\t(net 0 "") (net 1 "N1") (net 2 "N2")
"""

#: The sibling project: both nets in ``Default``, clearance 0.127 mm.
_PROJECT = {
    "meta": {"filename": "probe.kicad_pro", "version": 3},
    "net_settings": {
        "meta": {"version": 4},
        "classes": [
            {
                "name": "Default",
                "clearance": MIN_CLEARANCE,
                "track_width": TRACK_WIDTH,
                "via_diameter": 0.6,
                "via_drill": 0.3,
                "microvia_diameter": 0.3,
                "microvia_drill": 0.1,
                "diff_pair_width": TRACK_WIDTH,
                "diff_pair_gap": 0.25,
                "diff_pair_via_gap": 0.25,
                "wire_width": 6,
                "bus_width": 12,
                "line_style": 0,
                "schematic_color": "rgba(0, 0, 0, 0.000)",
                "pcb_color": "rgba(0, 0, 0, 0.000)",
                "priority": 2147483647,
            }
        ],
        "netclass_assignments": None,
        "netclass_patterns": [],
    },
}


def _fmt(nm: int) -> str:
    """An integer-nanometre coordinate as exact 6-decimal mm text."""
    sign = "-" if nm < 0 else ""
    nm = abs(nm)
    return f"{sign}{nm // 1_000_000}.{nm % 1_000_000:06d}"


def parallel_board(shortfall_nm: int) -> str:
    """Two horizontal 0.2 mm tracks, edge-to-edge ``0.127 - shortfall``."""
    y_nm = 20_000_000 + 200_000 + 127_000 - shortfall_nm
    return (
        _HEADER
        + '\t(segment (start 20 20) (end 60 20) (width 0.2) (layer "F.Cu") (net 1))\n'
        + f"\t(segment (start 20 {_fmt(y_nm)}) (end 60 {_fmt(y_nm)}) (width 0.2) "
        + '(layer "F.Cu") (net 2))\n'
        + ")\n"
    )


def diagonal_board(offset_nm: int) -> str:
    """Two parallel 45-degree 0.2 mm tracks, offset by ``(a, -a)``.

    ``a`` is integer nanometres, so the perpendicular centreline distance is
    ``a * sqrt(2)`` and the true edge-to-edge shortfall is
    ``0.127 + 0.2 - a * sqrt(2)`` -- irrational, hence the 1 nm-offset pair
    that brackets the 500 nm cut-off (499.10 / 500.51 nm).
    """
    a = offset_nm
    return (
        _HEADER
        + '\t(segment (start 20 20) (end 50 50) (width 0.2) (layer "F.Cu") (net 1))\n'
        + f"\t(segment (start {_fmt(20_000_000 + a)} {_fmt(20_000_000 - a)}) "
        + f"(end {_fmt(50_000_000 + a)} {_fmt(50_000_000 - a)}) (width 0.2) "
        + '(layer "F.Cu") (net 2))\n'
        + ")\n"
    )


def diagonal_true_shortfall_nm(offset_nm: int) -> float:
    """The exact edge-to-edge shortfall of :func:`diagonal_board` (nm)."""
    return (MIN_CLEARANCE + TRACK_WIDTH - offset_nm * 1e-6 * math.sqrt(2.0)) * 1e6


def pad_track_board(shortfall_nm: int) -> str:
    """A 1.0 mm circular SMD pad vs a horizontal 0.2 mm track below it."""
    y_nm = 25_000_000 + 500_000 + 100_000 + 127_000 - shortfall_nm
    return (
        _HEADER
        + '\t(footprint "probe:PAD" (layer "F.Cu") (at 30 25)\n'
        + "\t\t(attr smd exclude_from_pos_files exclude_from_bom allow_missing_courtyard)\n"
        + '\t\t(property "Reference" "REF1" (at 0 -1) (layer "F.SilkS") hide)\n'
        + '\t\t(pad "1" smd circle (at 0 0) (size 1.0 1.0) (layers "F.Cu") (net 2 "N2"))\n'
        + "\t)\n"
        + f"\t(segment (start 20 {_fmt(y_nm)}) (end 60 {_fmt(y_nm)}) (width 0.2) "
        + '(layer "F.Cu") (net 1))\n'
        + ")\n"
    )


#: The pinned probe points.  ``expect`` is the *violation* expectation both
#: engines must agree on.  The ruling's endpoints -- exactly at the accepted
#: limit and 10 nm beyond -- are the 500/510 axis and pad rows; the 501 rows
#: pin the 1 nm refinement that resolved the 10 nm cut-off; the diagonal rows
#: bracket the same edge within diagonal constructibility.
CASES = [
    pytest.param(parallel_board, 500, True, id="parallel-500nm-inside-band"),
    pytest.param(parallel_board, 501, False, id="parallel-501nm-beyond-band"),
    pytest.param(parallel_board, 510, False, id="parallel-510nm-beyond-band"),
    pytest.param(diagonal_board, 230_871, True, id="diagonal-499nm-inside-band"),
    pytest.param(diagonal_board, 230_870, False, id="diagonal-501nm-beyond-band"),
    pytest.param(pad_track_board, 500, True, id="pad-track-500nm-inside-band"),
    pytest.param(pad_track_board, 501, False, id="pad-track-501nm-beyond-band"),
    pytest.param(pad_track_board, 510, False, id="pad-track-510nm-beyond-band"),
]


#: The copper-to-edge project: the #9724 probe board's `.kicad_pro`.  The
#: netclass block is ``_PROJECT``'s; the edge minimum lives in
#: ``board.design_settings.rules.min_copper_edge_clearance`` (the key
#: ``kicad-cli pcb drc`` reads for its ``copper_edge_clearance`` check).
_EDGE_PROJECT = {
    **copy.deepcopy(_PROJECT),
    "board": {
        "design_settings": {
            "rules": {
                "min_clearance": MIN_CLEARANCE,
                "min_track_width": TRACK_WIDTH,
                "min_copper_edge_clearance": MIN_EDGE_CLEARANCE,
            },
        },
    },
}


def edge_board(shortfall_nm: int) -> str:
    """One horizontal 0.2 mm track above a straight rectangular-board edge.

    The board is a 100 x 100 mm rectangle (``gr_rect`` on Edge.Cuts, four
    straight edges -- no arc approximation can touch the measured cut-off);
    the track runs parallel to the bottom edge with its copper envelope
    ``0.5 mm - shortfall`` above it (integer-nm coordinates throughout).
    """
    y_nm = 10_000_000 + 100_000 + 500_000 - shortfall_nm
    return (
        '(kicad_pcb (version 20241229) (generator "probe_9724")\n'
        "\t(general (thickness 1.6) (legacy_teardrops no))\n"
        '\t(paper "A4")\n'
        '\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        '\t(net 0 "") (net 1 "N1")\n'
        "\t(gr_rect (start 10 10) (end 110 110) (stroke (width 0.05) (type solid))\n"
        '\t\t(layer "Edge.Cuts") (fill none))\n'
        + f"\t(segment (start 50 {_fmt(y_nm)}) (end 70 {_fmt(y_nm)}) (width 0.2) "
        + '(layer "F.Cu") (net 1))\n'
        + ")\n"
    )


#: The #9724 probe points on the copper-to-edge leg.  ``expect`` is the
#: *violation* expectation both engines must agree on: 0 nm is the control
#: exactly at the minimum, 400 nm sits inside the band but beyond the old
#: 1e-4 epsilon (the false positive the move to ``drc_clear`` removed), the
#: 500/501 pair is the 1 nm refinement of the cut-off, and 600 nm is the
#: clearly-violating control.
EDGE_CASES = [
    pytest.param(0, True, id="edge-0nm-at-minimum"),
    pytest.param(400, True, id="edge-400nm-inside-band"),
    pytest.param(500, True, id="edge-500nm-inside-band"),
    pytest.param(501, False, id="edge-501nm-beyond-band"),
    pytest.param(600, False, id="edge-600nm-beyond-band"),
]


class _EdgeRules:
    """Minimal rule source: one copper-to-edge floor, nothing else."""

    min_copper_to_edge_mm = MIN_EDGE_CLEARANCE
    min_hole_to_edge_mm = 0.7  # distinct minimum, unused by the trace branch


def _edge_rule_violation_count(board: str) -> int:
    return EdgeClearanceRule().check(PCB(parse_string(board)), _EdgeRules()).error_count


class _Rules:
    """Minimal rule source: one general clearance floor, nothing else."""

    min_clearance_mm = MIN_CLEARANCE
    min_smd_pad_clearance_mm = None
    min_pth_hole_to_track_mm = None
    min_inner_pth_hole_to_copper_mm = None


def _rule_violation_count(board: str) -> int:
    return ClearanceRule().check(PCB(parse_string(board)), _Rules()).error_count


def test_checker_band_is_the_probed_500nm() -> None:
    """The constant equals the largest shortfall pinned KiCad accepts.

    Probe (10 nm steps refined at 1 nm, see the module docstring): 500 nm
    accepted, 501 nm flagged -- the cut-off is exactly 500 nm, so the band is
    5e-4 mm.  Not 499 nm (that would flag a 500 nm gap kicad-cli accepts),
    and never beyond KiCad's figure (#9619 ruling).
    """
    assert KICAD_CLEARANCE_BAND_MM == 5e-4


def test_band_guard_is_negligible_against_iu() -> None:
    """The float guard stays six orders below KiCad's integer-nm IU and six
    above the board-coordinate dust, so it cannot move any verdict a
    nanometre-resolution check could see."""
    assert DRC_FLOAT_GUARD_MM < 1e-6  # well under KiCad's 1 nm IU
    assert DRC_FLOAT_GUARD_MM > 1e-12  # well above board-coordinate dust
    assert KICAD_CLEARANCE_BAND_MM + DRC_FLOAT_GUARD_MM < 1.6e-3  # band stays sub-marginal


def test_diagonal_case_construction_is_at_the_band_edge() -> None:
    """The diagonal fixture really brackets the 500 nm cut-off: the accepted
    offset sits ~0.9 nm inside the band, the flagged one ~0.5 nm beyond it."""
    assert diagonal_true_shortfall_nm(230_871) == pytest.approx(499.10, abs=0.01)
    assert diagonal_true_shortfall_nm(230_870) == pytest.approx(500.51, abs=0.01)


@pytest.mark.parametrize(("build", "value", "expect_clear"), CASES)
def test_rule_verdict_matches_the_pinned_probe(build, value: int, expect_clear: bool) -> None:
    """``ClearanceRule`` reproduces the pinned kicad-cli verdict on every
    probe board -- the exactly-at-the-band cases included, where board-file
    float dust (``20.3265`` is not binary-exact) must be absorbed by
    :data:`DRC_FLOAT_GUARD_MM` rather than decide the verdict."""
    errors = _rule_violation_count(build(value))
    assert errors == (0 if expect_clear else 1), (
        "the checker must agree with pinned kicad-cli 10.0.6 at the band edge "
        f"(case {build.__name__}({value}): expected "
        f"{'no finding' if expect_clear else 'a finding'}, got {errors})"
    )


def _pinned_kicad_cli() -> str:
    """The kicad-cli to probe with; a hard failure when it cannot be trusted.

    Under ``KCT_KICAD_DOCKER=1`` the session fixture routes ``find_kicad_cli``
    to the pinned image; CI installs the same release natively.  Any other
    answer fails the test instead of skipping it: the probe is only
    meaningful against the version the constant was measured on, and a
    silent skip would disarm the tripwire (operator rule: no added skips,
    ``pytest.skip`` included) -- a CI image or version-string change that
    breaks resolution must go red, not quietly unwatched.
    """
    from kicad_tools.cli.kicad_toolchain import PINNED_KICAD_VERSION
    from kicad_tools.cli.runner import find_kicad_cli

    binary = find_kicad_cli() or (shutil.which("kicad-cli"))
    if binary is None:
        pytest.fail(
            f"pinned kicad-cli {PINNED_KICAD_VERSION} not found: the probe must run "
            "against it (CI's Test job installs it natively; locally set "
            "KCT_KICAD_DOCKER=1 to route through the pinned image)"
        )
    try:
        version = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.fail(f"pinned kicad-cli at {binary} is unusable: {exc!r}")
    if version.returncode != 0 or PINNED_KICAD_VERSION not in version.stdout:
        pytest.fail(
            f"kicad-cli at {binary} is not the pinned {PINNED_KICAD_VERSION} "
            f"(rc={version.returncode}, stdout={version.stdout.strip()!r}); set "
            "KCT_KICAD_DOCKER=1 to route through the pinned image"
        )
    return binary


@pytest.mark.pr_required
@pytest.mark.parametrize(("build", "value", "expect_clear"), CASES)
def test_pinned_kicad_cli_agrees_with_the_checker(
    build, value: int, expect_clear: bool, tmp_path: Path
) -> None:
    """Black-box: ``kicad-cli pcb drc`` on the same board returns the pinned
    verdict *and* the same verdict the checker just returned.

    A missing or wrong-version kicad-cli is a hard failure (see
    :func:`_pinned_kicad_cli`), never a skip.  If a future KiCad changes its
    acceptance band, the 500 nm rows flip here while the checker rows above
    do not -- this test failing is the designed tripwire."""
    _pinned_kicad_cli()  # hard-fails unless the pinned binary answers
    from kicad_tools.cli.runner import run_drc

    board = build(value)
    errors = _rule_violation_count(board)
    stem = tmp_path / f"probe_{build.__name__}_{value}"
    pcb = stem.with_suffix(".kicad_pcb")
    pcb.write_text(board)
    stem.with_suffix(".kicad_pro").write_text(json.dumps(_PROJECT))
    report = stem.with_suffix(".rpt.json")
    result = run_drc(pcb, output_path=report, format="json", schematic_parity=False)
    assert result.success, f"kicad-cli run failed: {result.stderr}"
    kicad_clearance = sum(
        1
        for violation in json.loads(report.read_text()).get("violations", [])
        if violation.get("type") == "clearance"
    )
    expected = 0 if expect_clear else 1
    assert kicad_clearance == expected == errors, (
        f"pinned kicad-cli reported {kicad_clearance} clearance violations and "
        f"kct reported {errors}; the probed expectation is {expected} "
        f"(case {build.__name__}({value}))"
    )


@pytest.mark.parametrize(("shortfall_nm", "expect_clear"), EDGE_CASES)
def test_edge_rule_verdict_matches_the_pinned_probe(shortfall_nm: int, expect_clear: bool) -> None:
    """``EdgeClearanceRule``'s trace verdict reproduces the pinned kicad-cli
    verdict on every #9724 edge probe board.

    Probe (same method as the module docstring's clearance leg, on the
    copper-to-edge rule: straight rectangular edge, 0.5 mm
    ``min_copper_edge_clearance``, 10 nm sweep refined at 1 nm): 500 nm
    accepted, 501 nm flagged -- the same cut-off as the copper-clearance
    band, which is why ``edge.py``'s copper-to-edge comparisons moved onto
    ``drc_clear`` (#9724).  The 400 nm row is the false positive that move
    removed: inside KiCad's band but beyond the old 1e-4 epsilon."""
    errors = _edge_rule_violation_count(edge_board(shortfall_nm))
    assert errors == (0 if expect_clear else 1), (
        "the edge checker must agree with pinned kicad-cli 10.0.6 at the band "
        f"edge (edge_board({shortfall_nm} nm shortfall): expected "
        f"{'no finding' if expect_clear else 'a finding'}, got {errors})"
    )


@pytest.mark.pr_required
@pytest.mark.parametrize(("shortfall_nm", "expect_clear"), EDGE_CASES)
def test_pinned_kicad_cli_agrees_on_copper_edge_clearance(
    shortfall_nm: int, expect_clear: bool, tmp_path: Path
) -> None:
    """Black-box: ``kicad-cli pcb drc`` on the #9724 edge board returns the
    pinned ``copper_edge_clearance`` verdict *and* the same verdict
    ``EdgeClearanceRule`` just returned.

    Same tripwire contract as the clearance leg above: a future KiCad that
    changes the copper-to-edge acceptance band flips these rows while the
    rule rows do not, so the disagreement goes red instead of silent."""
    _pinned_kicad_cli()  # hard-fails unless the pinned binary answers
    from kicad_tools.cli.runner import run_drc

    board = edge_board(shortfall_nm)
    errors = _edge_rule_violation_count(board)
    stem = tmp_path / f"probe_edge_{shortfall_nm}"
    pcb = stem.with_suffix(".kicad_pcb")
    pcb.write_text(board)
    stem.with_suffix(".kicad_pro").write_text(json.dumps(_EDGE_PROJECT))
    report = stem.with_suffix(".rpt.json")
    result = run_drc(pcb, output_path=report, format="json", schematic_parity=False)
    assert result.success, f"kicad-cli run failed: {result.stderr}"
    kicad_edge = sum(
        1
        for violation in json.loads(report.read_text()).get("violations", [])
        if violation.get("type") == "copper_edge_clearance"
    )
    expected = 0 if expect_clear else 1
    assert kicad_edge == expected == errors, (
        f"pinned kicad-cli reported {kicad_edge} copper_edge_clearance violations "
        f"and kct reported {errors}; the probed expectation is {expected} "
        f"(edge_board({shortfall_nm} nm shortfall))"
    )
