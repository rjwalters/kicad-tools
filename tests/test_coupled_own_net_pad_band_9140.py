"""Coupled pose / trunk / stub-tail legs honour the own-net pad slit band (#9140).

The coupled diff-pair engine's pad gate (``_span_pad_clear``) used to skip the
span's OWN-net pads, so a leg could skirt its own pad inside the fab
``pad_spacing`` band (board 06 LVDS: 0.1275 mm < 0.15 mm).  The pose/trunk
``span_check`` hook, the synthesized stub-tail gate list and the assembled
segment gate all converge on ``_span_pad_clear``, so one rule covers them.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Segment
from kicad_tools.router.rules import DesignRules
from kicad_tools.router.via_clearance import same_net_segment_pad_gap_acceptable

EDGE = 0.15
NET = 7
HALF = 0.1  # 0.2 mm trace
PAD_HALF = 0.2


def _dpr(edge=EDGE):
    rules = DesignRules()
    rules.grid_resolution = 0.05
    rules.same_net_pad_gap_band_edge_mm = edge
    return Autorouter(width=20.0, height=10.0, rules=rules)._diffpair


def _pad(net=NET, layer=Layer.F_CU, **kw):
    return Pad(
        x=5.0,
        y=5.0,
        width=0.4,
        height=0.4,
        layer=layer,
        net=net,
        net_name="N",
        ref="R1",
        pin="1",
        **kw,
    )


def _span_at_gap(dpr, gap, layer_idx=0, net=NET):
    """Horizontal 0.2 mm span passing ``gap`` mm (copper edge) above the pad."""
    y = 5.0 - PAD_HALF - HALF - gap
    return dpr._span_pad_clear(3.0, y, 7.0, y, layer_idx, net, 0.2)


@pytest.mark.parametrize(
    ("gap", "ok"),
    [
        (0.1275, False),  # the board 06 LVDS finding
        (0.05, False),
        (0.0, True),  # touches: one copper union
        (-0.05, True),  # overlaps the pad
        (EDGE, True),  # at the detection edge
        (0.2, True),
    ],
)
def test_span_two_region_rule_on_own_net_pad(gap, ok):
    dpr = _dpr()
    dpr.autorouter.grid.add_pad(_pad())
    assert _span_at_gap(dpr, gap) is ok


def test_matches_via_clearance_predicate():
    """Python parity with the shared predicate the other routers use."""
    dpr = _dpr()
    dpr.autorouter.grid.add_pad(_pad())
    bbox = (5.0 - PAD_HALF, 5.0 - PAD_HALF, 5.0 + PAD_HALF, 5.0 + PAD_HALF)
    for gap in (0.1275, 0.05, 0.0, -0.05, 0.16, 0.3):
        y = 5.0 - PAD_HALF - HALF - gap
        expected = same_net_segment_pad_gap_acceptable((3.0, y), (7.0, y), HALF, [bbox], EDGE)
        assert _span_at_gap(dpr, gap) is expected, gap


def test_assembled_segment_gate_also_rejects():
    dpr = _dpr()
    dpr.autorouter.grid.add_pad(_pad())
    y = 5.0 - PAD_HALF - HALF - 0.1275
    seg = Segment(x1=3.0, y1=y, x2=7.0, y2=y, width=0.2, layer=Layer.F_CU, net=NET, net_name="N")
    assert dpr._segment_pad_clear(seg) is False


def test_rotated_roundrect_pad_uses_true_outline():
    """A 45-degree rotated roundrect is measured as copper, not its bbox."""
    dpr = _dpr()
    dpr.autorouter.grid.add_pad(_pad(rotation=45.0, shape="roundrect", roundrect_rratio=0.25))
    # Sweep a vertical-offset family: some positions must be rejected (inside
    # the band) and the far/touching ones accepted -- never all-or-nothing.
    results = [
        dpr._span_pad_clear(3.0, y, 7.0, y, 0, NET, 0.2) for y in (3.5, 4.0, 4.2, 4.4, 4.6, 5.0)
    ]
    assert True in results and False in results
    assert results[-1] is True  # through the pad centre: union
    assert results[0] is True  # far away


def test_pad_on_opposite_layer_ignored():
    dpr = _dpr()
    dpr.autorouter.grid.add_pad(_pad(layer=Layer.B_CU))
    assert _span_at_gap(dpr, 0.1275, layer_idx=0) is True


def test_foreign_net_pad_unchanged_by_band_rule():
    """A foreign pad at a gap > its clearance is still accepted (no band rule)."""
    dpr = _dpr()
    dpr.autorouter.grid.add_pad(_pad(net=99))
    # 0.1275 gap < 0.2 clearance: rejected by the existing FOREIGN gate...
    assert _span_at_gap(dpr, 0.1275) is False
    # ...and a clearly clear span is accepted.
    assert _span_at_gap(dpr, 0.5) is True


@pytest.mark.parametrize("edge", [None, 0.0])
def test_guard_off_without_resolved_edge(edge):
    dpr = _dpr(edge=edge)
    dpr.autorouter.grid.add_pad(_pad())
    assert _span_at_gap(dpr, 0.1275) is True
