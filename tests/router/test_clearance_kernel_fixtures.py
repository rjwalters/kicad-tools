"""The clearance kernel against the named Phase 1a fixtures (Epic #5509, 1b).

Each fixture in ``tests/fixtures/conformance/`` reproduces a **specific,
already-observed** disagreement between ``kicad-cli pcb drc`` and one of
today's in-tree clearance consumers.  Parity (``test_clearance_kernel_parity``)
only proves the two kernel ports agree with *each other*; this module is the
first consumer-independent evidence that they agree with **KiCad** -- the whole
point of Epic #5509.

Three properties are asserted per fixture:

1. the kernel's ``copper_gap`` reproduces the gap the fixture was built at;
2. ``clear(..., required_mm)`` matches kicad-cli's recorded verdict for that
   pair (``PairIntent.expect_violation``);
3. for #5398 specifically, the verdict is identical in **both insertion
   orders** -- which it must be, because the kernel has no notion of "which
   existed first".  That absence is precisely what makes it a fix for #5398,
   where the same pair is accepted or rejected depending on whether the via or
   the segment was committed first.

The kernel assertions run with or without kicad-cli installed: the committed
fixture carries the expected kicad-cli verdict.  A separate re-verification
leg actually re-runs ``kicad-cli pcb drc`` and is skipped when the binary is
absent (the ``tests/test_kicad_cli_roundtrip.py:49`` idiom, applied to that
test only rather than module-wide).

Scope: Phase 1b PR A covers the three segment/via fixtures.  The fourth,
``roundrect-corner-gap``, needs ``KPad`` and the ``pad_outline`` port and is
covered by the follow-up PR -- it is listed here (xfail-free, explicitly
skipped) rather than silently omitted.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router import clearance_kernel as ck
from kicad_tools.router.cpp_backend import is_cpp_available
from tests.conformance.adapters import BOARD_EDGE, KIND_CLEARANCE, Verdict
from tests.conformance.board import FIXTURES_DIR
from tests.conformance.fixtures import (
    HOLE_TO_HOLE_MM,
    build_named_fixture,
    fixture_stem,
)
from tests.conformance.generator import PairKind
from tests.conformance.oracle import kicad_cli_available, run_oracle

requires_cpp = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not available",
)
requires_kicad_cli = pytest.mark.skipif(not kicad_cli_available(), reason="kicad-cli not installed")

# The fixtures this PR's kernel can model end-to-end (segments and vias only).
SEG_VIA_FIXTURES = (
    "issue5398-seg-via-0p18-order",
    "issue5410-dqs-n-halo-vs-legal-via",
    "search-vs-commit-seg-via-max",
)

# F.Cu / In1.Cu / In2.Cu / B.Cu -> kernel layer index.
_LAYER_INDEX = {"F.Cu": 0, "In1.Cu": 1, "In2.Cu": 2, "B.Cu": 3}


def _shapes_by_net(name: str) -> dict[str, ck.KShape]:
    """Every copper object of a named fixture, keyed by its (unique) net."""
    case = build_named_fixture(name)
    shapes: dict[str, ck.KShape] = {}
    for seg in case.segments:
        shapes[seg.net] = ck.KSegment(
            x1=seg.start[0],
            y1=seg.start[1],
            x2=seg.end[0],
            y2=seg.end[1],
            width=seg.width,
            layer=_LAYER_INDEX[seg.layer],
        )
    for via in case.vias:
        shapes[via.net] = ck.KVia(x=via.x, y=via.y, diameter=via.diameter, drill=via.drill)
    assert not case.pads, f"{name} has pads; KPad arrives in the follow-up PR"
    return shapes


# ---------------------------------------------------------------------------
# Gap reproduction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", SEG_VIA_FIXTURES)
def test_kernel_reproduces_the_fixture_gap(name: str) -> None:
    """The kernel measures the gap each fixture was analytically placed at."""
    case = build_named_fixture(name)
    shapes = _shapes_by_net(name)

    for pair in case.pairs:
        gap = ck.copper_gap(shapes[pair.net_a], shapes[pair.net_b])
        assert gap == pytest.approx(pair.target_gap_mm, abs=1e-6), (
            f"{name}: {pair.net_a} <-> {pair.net_b} measured {gap:.6f} mm, "
            f"fixture was placed at {pair.target_gap_mm:.6f} mm"
        )


@pytest.mark.parametrize("name", SEG_VIA_FIXTURES)
def test_kernel_matches_the_recorded_kicad_cli_verdict(name: str) -> None:
    """``clear()`` agrees with what kicad-cli says about each fixture pair.

    ``PairIntent.expect_violation`` is the committed kicad-cli expectation --
    ``tests/conformance/test_named_fixtures.py`` is the hard gate that keeps it
    honest against a live kicad-cli run.
    """
    case = build_named_fixture(name)
    shapes = _shapes_by_net(name)

    for pair in case.pairs:
        verdict_clear = ck.clear(shapes[pair.net_a], shapes[pair.net_b], pair.required_mm)
        assert verdict_clear is not pair.expect_violation, (
            f"{name}: kernel says clear={verdict_clear} for {pair.net_a} <-> "
            f"{pair.net_b} at {pair.required_mm} mm, but kicad-cli "
            f"{'flags' if pair.expect_violation else 'accepts'} it"
        )


# ---------------------------------------------------------------------------
# #5398 -- order cannot matter
# ---------------------------------------------------------------------------


def test_issue5398_is_rejected_in_both_insertion_orders() -> None:
    """The 0.18 mm segment/via pair is flagged whichever side is "first".

    This is #5398's whole defect, stated as a property of the kernel: today's
    grid gates compare the pair against ``trace_clearance`` (0.15, accept) when
    the via was committed first and ``via_clearance`` (0.20, reject) when the
    segment was -- so a board can route clean and DRC dirty.  The kernel takes
    ``required_mm`` as an argument and has no insertion order, so both
    evaluations are literally the same call with its arguments swapped.
    """
    case = build_named_fixture("issue5398-seg-via-0p18-order")
    shapes = _shapes_by_net("issue5398-seg-via-0p18-order")
    (pair,) = case.pairs
    assert pair.kind == PairKind.SEG_VIA
    seg, via = shapes[pair.net_a], shapes[pair.net_b]

    # kicad-cli applies the project Default class (0.20 mm) to a 0.18 mm gap.
    assert pair.required_mm == pytest.approx(0.20)
    assert pair.expect_violation is True

    via_first = ck.clear(via, seg, pair.required_mm)
    segment_first = ck.clear(seg, via, pair.required_mm)
    assert via_first is False, "kernel accepted #5398's pair with the via first"
    assert segment_first is False, "kernel accepted #5398's pair with the segment first"
    assert via_first == segment_first


@requires_cpp
def test_issue5398_both_orders_agree_in_the_cpp_kernel_too() -> None:
    """Same property, asserted against the compiled kernel."""
    from kicad_tools.router import router_cpp

    case = build_named_fixture("issue5398-seg-via-0p18-order")
    (pair,) = case.pairs
    segment = case.segments[0]
    via = case.vias[0]

    cpp_seg = router_cpp.KSegment(
        segment.start[0],
        segment.start[1],
        segment.end[0],
        segment.end[1],
        segment.width,
        _LAYER_INDEX[segment.layer],
    )
    cpp_via = router_cpp.KVia(via.x, via.y, via.diameter, via.drill)

    assert router_cpp.clear(cpp_seg, cpp_via, pair.required_mm) is False
    assert router_cpp.clear(cpp_via, cpp_seg, pair.required_mm) is False
    assert router_cpp.copper_gap(cpp_seg, cpp_via) == pytest.approx(
        router_cpp.copper_gap(cpp_via, cpp_seg), abs=1e-7
    )


# ---------------------------------------------------------------------------
# #5410 -- a legal via the grid halo refused
# ---------------------------------------------------------------------------


def test_issue5410_dq3_via_is_clear_on_copper_and_on_drill() -> None:
    """DQ3's via is legal against DQS_N on both counts, and the kernel says so.

    #5410's numbers verbatim: 0.213 mm copper against a 0.20 mm requirement and
    0.513 mm drill-to-drill against a 0.50 mm floor.  The router refused it
    because ``_mark_via`` paints a six-cell Chebyshev *square* halo on a
    0.127 mm grid; the kernel measures the actual geometry.
    """
    shapes = _shapes_by_net("issue5410-dqs-n-halo-vs-legal-via")
    dqs_n, dq3 = shapes["DQS_N"], shapes["DQ3"]

    copper = ck.copper_gap(dqs_n, dq3)
    drill = ck.hole_gap(dqs_n, dq3)
    assert copper == pytest.approx(0.213, abs=5e-4), f"copper gap {copper:.6f} mm"
    assert drill == pytest.approx(0.513, abs=5e-4), f"drill gap {drill:.6f} mm"

    assert ck.clear(dqs_n, dq3, 0.20) is True
    assert drill >= HOLE_TO_HOLE_MM - ck.CLEARANCE_EPSILON_MM


# ---------------------------------------------------------------------------
# search-vs-commit -- one pair, one requirement, one answer
# ---------------------------------------------------------------------------


def test_search_vs_commit_pair_is_clear_at_0p15_and_not_at_0p20() -> None:
    """0.18 mm clears the project's 0.15 mm rule; only the requirement varies.

    The router disagrees with *itself* on this pair today: ``RouteHaloGeometry``
    raises the requirement to ``max(required, via_clearance)`` = 0.20 and
    refuses the candidate during search, while the commit validators compare
    the same pair against 0.15 and accept it.  The kernel cannot hold both
    opinions -- it is handed one ``required_mm`` and answers once.
    """
    case = build_named_fixture("search-vs-commit-seg-via-max")
    shapes = _shapes_by_net("search-vs-commit-seg-via-max")
    (pair,) = case.pairs
    seg, via = shapes[pair.net_a], shapes[pair.net_b]

    assert ck.copper_gap(seg, via) == pytest.approx(0.18, abs=1e-9)
    # kicad-cli's requirement for this board is 0.15 -> clean, as recorded.
    assert pair.required_mm == pytest.approx(0.15)
    assert ck.clear(seg, via, 0.15) is True
    # Handed the router's search-side 0.20 instead, the same pair fails --
    # which is the epic's thesis: the disagreement lives in the *resolver*,
    # not in the geometry.
    assert ck.clear(seg, via, 0.20) is False


# ---------------------------------------------------------------------------
# Copper-to-edge on the fixture boards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", SEG_VIA_FIXTURES)
def test_fixture_copper_clears_the_board_outline(name: str) -> None:
    """No fixture accidentally places copper near its own board edge.

    Each fixture is meant to probe exactly one pair; copper that also sat close
    to the outline would add a second, uncontrolled finding to kicad-cli's
    report and make the fixture measure two things at once.
    """
    case = build_named_fixture(name)
    shapes = _shapes_by_net(name)
    edge = ck.KEdge(
        points=(
            (0.0, 0.0),
            (case.width, 0.0),
            (case.width, case.height),
            (0.0, case.height),
            (0.0, 0.0),
        )
    )
    for net, shape in shapes.items():
        gap = ck.copper_gap(shape, edge)
        assert not math.isinf(gap)
        assert gap >= case.rules.min_copper_to_edge, net


# ---------------------------------------------------------------------------
# kicad-cli re-verification leg (skipped when the binary is absent)
# ---------------------------------------------------------------------------


@requires_kicad_cli
@pytest.mark.parametrize("name", SEG_VIA_FIXTURES)
def test_kernel_agrees_with_a_live_kicad_cli_run(name: str, tmp_path) -> None:
    """Re-run kicad-cli and compare its clearance findings to the kernel's.

    The kernel assertions above stand on the fixture's *recorded* expectation.
    This one re-derives it: the set of net pairs the kernel flags at the
    project requirement must equal the set kicad-cli reports, for the same
    board, with zones left as-is.
    """
    case = build_named_fixture(name)
    shapes = _shapes_by_net(name)
    board = FIXTURES_DIR / f"{fixture_stem(case)}.kicad_pcb"

    required = case.rules.project_clearance
    nets = sorted(shapes)
    kernel_findings = {
        Verdict.pair(KIND_CLEARANCE, a, b)
        for i, a in enumerate(nets)
        for b in nets[i + 1 :]
        if not ck.clear(shapes[a], shapes[b], required)
    }

    oracle = run_oracle(board, refill=False, work_dir=tmp_path)
    cli_findings = {v for v in oracle.without_zones() if v.kind == KIND_CLEARANCE}
    cli_findings = {v for v in cli_findings if BOARD_EDGE not in v.nets}

    assert kernel_findings == cli_findings, (
        f"{name}: kernel {sorted(v.describe() for v in kernel_findings)} vs "
        f"kicad-cli {sorted(v.describe() for v in cli_findings)}"
    )


# ---------------------------------------------------------------------------
# Fourth fixture -- explicitly deferred, not silently omitted
# ---------------------------------------------------------------------------


def test_roundrect_corner_fixture_needs_the_pad_port() -> None:
    """``roundrect-corner-gap`` is covered once ``KPad`` / ``pad_outline`` land.

    Phase 1b was split by shape class (the issue's own "Sizing and split"
    guidance): this PR ships ``KSegment`` / ``KVia`` / ``KEdge``, and the pad
    outline port of ``validate/rules/clearance.py:_pad_polygon`` plus
    ``KZonePoly`` follow.  The fixture is named here so the gap is visible in
    the test report rather than inferred from an absence.
    """
    case = build_named_fixture("roundrect-corner-gap")
    assert case.pads, "fixture no longer has pads -- revisit this deferral"
    pytest.skip("roundrect-corner-gap needs KPad/pad_outline (clearance kernel PR B)")
