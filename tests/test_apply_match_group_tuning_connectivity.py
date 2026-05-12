"""Connectivity-invariant test for ``apply_match_group_tuning`` (AC7).

Issue #2723.  This module verifies the AC7 contract: a deliberately-faulty
serpentine insertion (e.g. monkey-patched to corrupt a route by dropping a
segment between pads) causes
:func:`_enforce_connectivity_invariant_or_exit` to flag the regression --
mirroring the Phase 3I diff-pair pattern at ``route_cmd.py:5223``.

The test intentionally constructs a corrupting tuner and asserts that the
post-phase invariant detects the regression and either reverts the routes
(default mode) OR raises :class:`ConnectivityRegressionError` (strict mode).
"""

from __future__ import annotations

import pytest

from kicad_tools.router.connectivity_invariant import (
    ConnectivityRegressionError,
    enforce_connectivity_invariant,
    snapshot_connectivity,
)
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.match_group_length import MatchGroup, MatchGroupSource
from kicad_tools.router.primitives import Pad, Route, Segment


def _pad(x: float, y: float, ref: str, net: int, net_name: str) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=0.5,
        height=0.5,
        net=net,
        net_name=net_name,
        layer=Layer.F_CU,
        ref=ref,
        pin="1",
    )


def _two_pad_route(net: int, name: str, x1: float, y: float, x2: float) -> Route:
    return Route(
        net=net,
        net_name=name,
        segments=[
            Segment(
                x1=x1,
                y1=y,
                x2=x2,
                y2=y,
                width=0.2,
                layer=Layer.F_CU,
                net=net,
                net_name=name,
            )
        ],
    )


def _make_4_net_routed_autorouter() -> Autorouter:
    """An autorouter with 4 nets, each a 2-pad straight-segment net.

    All 4 nets are multi-pad so the connectivity snapshot tracks them.
    Lengths: net 1 = 22mm, net 2 = 20mm, net 3 = 18mm, net 4 = 18mm.
    """
    ar = Autorouter(width=80.0, height=80.0)
    ar.net_names = {1: "DQ0", 2: "DQ1", 3: "DQ2", 4: "DQ3"}

    # Pads: each net has two pads at the segment endpoints.
    ar.pads = {}
    ar.nets = {1: [], 2: [], 3: [], 4: []}
    layout = [
        (1, "DQ0", 0.0, 22.0, 0.0),
        (2, "DQ1", 0.0, 20.0, 10.0),
        (3, "DQ2", 0.0, 18.0, 20.0),
        (4, "DQ3", 0.0, 18.0, 30.0),
    ]
    for nid, name, x1, x2, y in layout:
        pad_a_key = (f"R{nid}", "1")
        pad_b_key = (f"R{nid}", "2")
        ar.pads[pad_a_key] = _pad(x1, y, f"R{nid}", nid, name)
        ar.pads[pad_b_key] = _pad(x2, y, f"R{nid}", nid, name)
        ar.nets[nid] = [pad_a_key, pad_b_key]

    ar.routes = [
        _two_pad_route(1, "DQ0", 0.0, 0.0, 22.0),
        _two_pad_route(2, "DQ1", 0.0, 10.0, 20.0),
        _two_pad_route(3, "DQ2", 0.0, 20.0, 18.0),
        _two_pad_route(4, "DQ3", 0.0, 30.0, 18.0),
    ]
    return ar


def test_baseline_no_regression():
    """Sanity: tuning the well-formed group does NOT flag connectivity loss."""
    ar = _make_4_net_routed_autorouter()
    group = MatchGroup(
        name="DDR_DATA_BYTE_0",
        net_ids=[1, 2, 3, 4],
        tolerance=0.1,
        reference_net_id=1,
        source=MatchGroupSource.LEGACY_API,
    )

    snapshot = snapshot_connectivity(ar)
    ar.apply_match_group_tuning(detected_groups=[group], verbose=False)
    result = enforce_connectivity_invariant(
        ar, snapshot, phase="length_match_groups", strict=False, quiet=True
    )
    assert not result.regressed_nets, (
        f"Baseline tuning should not regress connectivity, but got {result.regressed_nets}"
    )


def test_corrupted_route_triggers_invariant_strict_mode():
    """A corrupting tuner that drops segments mid-pad causes the
    connectivity invariant to raise in strict mode (AC7)."""
    ar = _make_4_net_routed_autorouter()
    snapshot = snapshot_connectivity(ar)

    # Mutate the routes directly to simulate a faulty tuner that breaks
    # the pad-to-pad chain.  We replace net 2's single segment with a
    # half-segment that no longer touches the second pad.  This is the
    # "deliberately-faulty serpentine insertion" hostile fixture
    # described in AC7 -- equivalent to a corrupting monkey-patch on
    # tune_match_group_v2.
    for i, r in enumerate(ar.routes):
        if r.net == 2:
            ar.routes[i] = Route(
                net=2,
                net_name="DQ1",
                segments=[
                    Segment(
                        x1=0.0,
                        y1=10.0,
                        x2=5.0,  # was 20.0 -- now ends mid-air
                        y2=10.0,
                        width=0.2,
                        layer=Layer.F_CU,
                        net=2,
                        net_name="DQ1",
                    )
                ],
            )
            break

    with pytest.raises(ConnectivityRegressionError):
        enforce_connectivity_invariant(
            ar, snapshot, phase="length_match_groups", strict=True, quiet=True
        )


def test_corrupted_route_reverts_in_default_mode():
    """A corrupting tuner is detected and the regressed net is reverted
    in default (non-strict) mode."""
    ar = _make_4_net_routed_autorouter()
    snapshot = snapshot_connectivity(ar)

    # Same hostile mutation as the strict test.
    for i, r in enumerate(ar.routes):
        if r.net == 2:
            ar.routes[i] = Route(
                net=2,
                net_name="DQ1",
                segments=[
                    Segment(
                        x1=0.0,
                        y1=10.0,
                        x2=5.0,
                        y2=10.0,
                        width=0.2,
                        layer=Layer.F_CU,
                        net=2,
                        net_name="DQ1",
                    )
                ],
            )
            break

    result = enforce_connectivity_invariant(
        ar, snapshot, phase="length_match_groups", strict=False, quiet=True
    )
    assert 2 in result.regressed_nets, (
        "Net 2 was deliberately broken; it should appear in regressed_nets"
    )
    # After revert, net 2's route should once again span both pads.
    net_2_routes = [r for r in ar.routes if r.net == 2]
    assert len(net_2_routes) == 1
    seg = net_2_routes[0].segments[0]
    # Pad at x=20.0 must be on the segment.
    assert seg.x2 == 20.0 or seg.x1 == 20.0, (
        f"Net 2 was not reverted; segment endpoints {seg.x1},{seg.x2}"
    )
