"""Issue #9215: ``kct route`` resolves the same-net TRACE slit-band edge.

PR #9212 (issue #9208) added ``DesignRules.same_net_trace_gap_band_edge_mm``
and the C++ via-annulus-vs-own-net-trace band refusal, but only board 06's
in-process recipe resolved the edge -- every ``kct route`` invocation left
the trace-population guard off while its pad twin
(``_mfr_pad_gap_band_edge``, #8974) resolved on the same path.  A scan of
the committed ``kct route`` artifacts found the population live (board 03:
1, board 04: 10, board 05: 1 same-net via-vs-own-trace ``physical_copper_gap``
errors graded in the fab trace band), so the edge is now resolved at the
same single point and threaded through the same ``DesignRules(...)`` sites.

The resolution contract is the pad sibling's (#8974): the
``trace_spacing`` detection edge of the stackup's dfm entry
(``_resolve_mfr_trace_gap_edge``); ``None`` (no ``--manufacturer``, no dfm
table, no stackup entry) keeps the guard OFF rather than guessing an edge.
"""

from __future__ import annotations

import argparse

import pytest

from kicad_tools.cli.stitch_cmd import _resolve_mfr_trace_gap_edge  # noqa: E402

#: jlcpcb ``2layer_1oz`` ``trace_spacing`` detection edge (the same dfm table
#: path ``kct check`` grades the trace population under).
EDGE_TRACE_2L_1OZ = 0.127
#: jlcpcb ``4layer_1oz`` ``trace_spacing`` detection edge (board 06's band).
EDGE_TRACE_4L_1OZ = 0.1016


def _route_args(**over):
    """A minimal ``kct route`` args namespace for the resolution point.

    Mirrors ``tests/test_route_side_same_net_pad_gap.py``: ``layers="2"``
    keeps ``_effective_layer_count_for_rules`` off the board file, and the
    stackup probe swallows a missing board, so ``pcb_path`` never has to
    exist.
    """
    ns = argparse.Namespace(
        manufacturer=None,
        copper=None,
        layers="2",
        trace_width=None,
        min_trace=None,
    )
    for key, value in over.items():
        setattr(ns, key, value)
    return ns


def _apply(args):
    from kicad_tools.cli.route_cmd import _apply_manufacturer_trace_width_floor

    assert _apply_manufacturer_trace_width_floor(args, "unused.kicad_pcb", quiet=True) == 0
    return args


def test_trace_edge_is_none_without_a_manufacturer():
    # Same fail-closed contract as the pad sibling (#8974 review finding 1):
    # with no manufacturer there is nothing to resolve from, and the guard
    # must not guess a fab.
    args = _apply(_route_args())
    assert args._mfr_trace_gap_band_edge is None


def test_trace_edge_engages_with_a_manufacturer():
    # jlcpcb 2layer_1oz resolves a real trace-population edge from the
    # profile table -- the guard engages on the CLI path.
    args = _apply(_route_args(manufacturer="jlcpcb"))
    assert args._mfr_trace_gap_band_edge == pytest.approx(EDGE_TRACE_2L_1OZ)


def test_trace_edge_4l_matches_board06_resolution():
    # The 4-layer edge equals what board 06's recipe resolves in-process
    # (0.1016), so the CLI path and the recipe path agree on the band.
    assert _resolve_mfr_trace_gap_edge("jlcpcb", 4, 1.0) == pytest.approx(EDGE_TRACE_4L_1OZ)
    args = _apply(_route_args(manufacturer="jlcpcb", layers="4"))
    assert args._mfr_trace_gap_band_edge == pytest.approx(EDGE_TRACE_4L_1OZ)


def test_trace_edge_fails_closed_on_a_tableless_profile():
    # jlcpcb-tier1 has no dfm threshold table: the pad sibling resolves None
    # there today and the trace sibling must match, not fall back to the
    # base tier's table.
    assert _resolve_mfr_trace_gap_edge("jlcpcb-tier1", 2, 1.0) is None
    args = _apply(_route_args(manufacturer="jlcpcb-tier1"))
    assert args._mfr_trace_gap_band_edge is None
    assert args._mfr_pad_gap_band_edge is None
