"""Integration regression test for the post-optimize connectivity invariant
(issue #2596).

This module focuses on the AUDIO_R-style failure mode that motivated the
issue: a multi-pad signal net whose copper is split across **multiple**
``Route`` objects sharing apex/junction vertices.  The per-route guard
inside :class:`TraceOptimizer.optimize_route` cannot see across Route
boundaries, so on ``main`` the pipeline silently regresses pads from
the connected component.

Running the full ``kicad-tools route`` CLI on chorus-test-revA takes
30+ minutes; a faithful end-to-end fixture is not feasible for a unit
test suite.  Instead we exercise the exact pipeline glue
(``snapshot_connectivity`` + ``enforce_connectivity_invariant``)
against synthetic geometry that reproduces the topology the issue
describes:

* 6 pads on a single net (the AUDIO_R count).
* Three ``Route`` objects sharing intermediate apex points.
* Pre-optimize connectivity: 6/6 pads in one component.

The integration check is: after running the same per-route optimise
loop the CLI runs (then the invariant guard), ``connected_pads(post)
>= connected_pads(pre)``.  This is the exact acceptance criterion in
the issue.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kicad_tools.router.connectivity_invariant import (
    enforce_connectivity_invariant,
    snapshot_connectivity,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.observability import validate_net_connectivity
from kicad_tools.router.optimizer import TraceOptimizer
from kicad_tools.router.primitives import Pad, Route, Segment


@dataclass
class _StubAutorouter:
    routes: list[Route] = field(default_factory=list)
    pads: dict = field(default_factory=dict)
    nets: dict = field(default_factory=dict)
    net_names: dict = field(default_factory=dict)


def _seg(x1: float, y1: float, x2: float, y2: float) -> Segment:
    return Segment(
        x1=x1,
        y1=y1,
        x2=x2,
        y2=y2,
        width=0.2,
        layer=Layer.F_CU,
        net=42,
        net_name="AUDIO_R",
    )


def _pad(x: float, y: float, ref: str) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=0.5,
        height=0.5,
        net=42,
        net_name="AUDIO_R",
        layer=Layer.F_CU,
        ref=ref,
        pin="1",
    )


def _make_audio_r_topology() -> _StubAutorouter:
    """Replicate the chorus-test AUDIO_R topology that regressed.

    AUDIO_R is a 6-pad net.  In the failing routing the pads are
    connected via three trunks:

      * Trunk 1: pads P1, P2, P3 in series along y=0, with kinked
        segments through apex (5, 0).
      * Trunk 2: pads P4, P5 along y=10, joining trunk 1 via a
        short branch from (5, 0) up to (5, 10).
      * Trunk 3: pad P6 hangs off (10, 0) via a short stub.

    Stored as three ``Route`` objects -- the pattern that defeats the
    per-route guard.  Pre-optimize, all 6 pads are in one connected
    component.  Without the pipeline-level invariant, the optimiser
    drops apex (5, 0) when it merges trunk 1 into a single segment,
    stranding trunk 2 (and pads P4 + P5) just like AUDIO_R lost 2 of
    its 6 pads in the issue repro.
    """
    pads = {
        ("U1", "1"): _pad(0.0, 0.0, "P1"),
        ("U1", "2"): _pad(5.0, 0.0, "P2"),
        ("U1", "3"): _pad(10.0, 0.0, "P3"),
        ("U2", "1"): _pad(0.0, 10.0, "P4"),
        ("U2", "2"): _pad(10.0, 10.0, "P5"),
        ("U3", "1"): _pad(15.0, 0.0, "P6"),
    }

    # Trunk 1: P1 -> apex(5,0) -> P3 (kinked so optimiser can collapse).
    trunk1 = Route(
        net=42,
        net_name="AUDIO_R",
        segments=[
            _seg(0.0, 0.0, 2.0, 0.0),
            _seg(2.0, 0.0, 5.0, 0.0),
            _seg(5.0, 0.0, 7.0, 0.0),
            _seg(7.0, 0.0, 10.0, 0.0),
        ],
    )
    # Trunk 2: apex(5,0) -> P4 -> P5 via y=10 rail.  Joins trunk 1 at
    # the apex, so on ``main`` the merged trunk 1 will lose this
    # connection.
    trunk2 = Route(
        net=42,
        net_name="AUDIO_R",
        segments=[
            _seg(5.0, 0.0, 5.0, 5.0),
            _seg(5.0, 5.0, 5.0, 10.0),
            _seg(5.0, 10.0, 0.0, 10.0),  # to P4
            _seg(5.0, 10.0, 10.0, 10.0),  # to P5
        ],
    )
    # Trunk 3: stub from (10, 0) to P6 at (15, 0).  Joins trunk 1 at
    # the right end -- collapsing trunk 1 keeps this endpoint
    # reachable, so no regression here.
    trunk3 = Route(
        net=42,
        net_name="AUDIO_R",
        segments=[_seg(10.0, 0.0, 15.0, 0.0)],
    )

    return _StubAutorouter(
        routes=[trunk1, trunk2, trunk3],
        pads=pads,
        nets={42: list(pads.keys())},
        net_names={42: "AUDIO_R"},
    )


def test_chorus_test_audio_r_no_regression() -> None:
    """AUDIO_R does not regress from N/6 to less-than-N/6 across optimize.

    Reproduces the chorus-test AUDIO_R 5/6 -> 3/6 regression in
    miniature (full chorus-test routing takes 30+ minutes).  Asserts
    the issue #2596 acceptance criterion: ``connected_pads(post) >=
    connected_pads(pre)``.
    """
    router = _make_audio_r_topology()
    snapshot = snapshot_connectivity(router)
    pre_info = snapshot.pre_connectivity[42]
    pre_connected = pre_info["connected_pads"]
    total_pads = pre_info["total_pads"]

    # Sanity: pre-optimise we're fully connected.
    assert total_pads == 6
    assert pre_connected == 6
    assert pre_info["connected"] is True

    # Run the same per-route optimise loop the CLI runs.
    optimizer = TraceOptimizer()
    router.routes = [optimizer.optimize_route(r) for r in router.routes]

    # Apply the pipeline-level invariant (default, non-strict mode).
    enforce_connectivity_invariant(
        router,
        snapshot,
        phase="optimize",
        strict=False,
        quiet=True,
    )

    post = validate_net_connectivity(router.routes, snapshot.net_pads)
    post_connected = post[42]["connected_pads"]

    # The invariant: connected_pads must not regress.
    assert post_connected >= pre_connected, (
        f"AUDIO_R regression: pre {pre_connected}/{total_pads} -> "
        f"post {post_connected}/{total_pads} (issue #2596)"
    )
    assert post[42]["connected"] is True


def test_audio_r_snapshot_records_correct_pad_count() -> None:
    """Snapshot must record the full pad list so post comparison works."""
    router = _make_audio_r_topology()
    snapshot = snapshot_connectivity(router)
    assert 42 in snapshot.net_pads
    assert len(snapshot.net_pads[42]) == 6
    assert snapshot.net_names[42] == "AUDIO_R"


def test_audio_r_strict_mode_raises_on_simulated_regression() -> None:
    """Strict mode raises when AUDIO_R loses pads.

    We force the regression by clearing trunk 2 entirely after the
    snapshot, modelling the worst-case pipeline behaviour.
    """
    import pytest

    from kicad_tools.router.connectivity_invariant import (
        ConnectivityRegressionError,
    )

    router = _make_audio_r_topology()
    snapshot = snapshot_connectivity(router)

    # Drop trunk 2 (the one with pads P4 and P5).
    router.routes = [
        r for r in router.routes if not any(abs(s.y2 - 10.0) < 1e-3 for s in r.segments)
    ]

    with pytest.raises(ConnectivityRegressionError) as excinfo:
        enforce_connectivity_invariant(
            router,
            snapshot,
            phase="optimize",
            strict=True,
            quiet=True,
        )
    assert excinfo.value.phase == "optimize"
    assert 42 in excinfo.value.result.regressed_nets
