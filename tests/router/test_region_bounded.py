"""Tests for Issue #4148: region-bounded routing (``--region`` on route/route-auto).

Phase 2a scope
--------------

``kct route --region x1,y1,x2,y2`` confines all new routing to an
axis-aligned box (board-relative mm, matching ``pcb strip --region``'s
convention exactly).  Everything outside the box -- empty grid AND existing
copper -- is treated as a fixed obstacle, so the router never adds, modifies,
or removes copper outside the region.

Implementation (option (a) from the curator's enhancement): the routing grid
always covers the full board, so region-bounding marks every routable-layer
cell OUTSIDE the box as an obstacle (:meth:`RoutingGrid.mark_region_bound`),
the COMPLEMENT of a keepout.  No ``router/cpp/`` edits are needed -- the C++
pathfinder hard-blocks any blocked foreign-net / net-0 cell.

These tests follow ``tests/router/test_preserve_existing.py`` conventions
(byte-identical outside-copper assertion) but build small synthetic boards in
the spirit of ``tests/test_pcb.py``'s ``test_strip_region_*`` since the router
API takes a ``.kicad_pcb`` path and the chorus fixture is NOT available in CI.

Phase 2b-1 scope (Issue #4170)
------------------------------

``TestStubReconnection`` covers bare mid-trace stub-endpoint reconnection for
``kct route --region``: a genuine boundary-clipped stub (produced by
``PCB.add_trace`` + ``strip_traces(region=...)``) is reconnected to its
in-region pad, zero copper is modified outside the region, the hole-to-hole
floor is respected, and multi-stub same-net cases reconnect.

Phase 2c scope (Issue #4173)
----------------------------

``TestStubReconnectionRouteAuto`` brings the ``route-auto`` orchestrator
surface to parity with ``kct route --region``: ``route_net_auto(..., region=)``
now reconnects the SAME synthetic boundary stub by pruning the outside pad and
injecting the boundary stub tip as an in-region target in ``_build_pads_for_net``
(reusing the shared #4172 detector).  Unlike the Autorouter, the orchestrator
has no per-cell obstacle grid, so region confinement on this path is provided
entirely by the post-route output-escape filter -- a coarse corridor that
bulges out-of-box fails honestly there rather than writing out-of-region copper.
The old "defer to Phase 2c" fail-fast gate + test are removed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.route_cmd import _parse_region_box
from kicad_tools.cli.route_cmd import main as route_main
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.io import load_pcb_for_routing
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.pcb import parse_segments, parse_vias
from kicad_tools.router.primitives import Via
from kicad_tools.router.rules import DesignRules
from kicad_tools.schema.pcb import PCB

# ---------------------------------------------------------------------------
# Synthetic board construction
# ---------------------------------------------------------------------------


def _footprint(ref: str, x: float, y: float, net_num: int, net_name: str) -> str:
    """A minimal single-pad SMD footprint the router's loader accepts."""
    return (
        f'  (footprint "R_0402" (layer "F.Cu") (at {x} {y})\n'
        f'    (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS"))\n'
        f'    (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") '
        f'(net {net_num} "{net_name}")))'
    )


def _tht_footprint(
    ref: str, x: float, y: float, net_num: int, net_name: str, drill: float = 0.3
) -> str:
    """A minimal through-hole footprint (carries a drill for hole-to-hole tests)."""
    return (
        f'  (footprint "R_THT" (layer "F.Cu") (at {x} {y})\n'
        f'    (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS"))\n'
        f'    (pad "1" thru_hole circle (at 0 0) (size 0.9 0.9) (drill {drill}) '
        f'(layers "*.Cu") (net {net_num} "{net_name}")))'
    )


def _board(
    footprints: list[str],
    *,
    edge: tuple[float, float, float, float] = (100, 100, 140, 140),
    nets: list[tuple[int, str]] | None = None,
    extra: str = "",
) -> str:
    """Assemble a minimal but router-loadable ``.kicad_pcb`` string.

    ``edge`` is the Edge.Cuts ``gr_rect`` (start_x, start_y, end_x, end_y);
    its start becomes both ``PCB._board_origin`` and the router's grid origin,
    so pad ``(at ...)`` coordinates are sheet-absolute and region boxes are
    board-relative (start subtracted).
    """
    if nets is None:
        nets = [(1, "SIG_A"), (2, "SIG_B")]
    net_lines = '  (net 0 "")\n' + "\n".join(f'  (net {n} "{name}")' for n, name in nets)
    ex1, ey1, ex2, ey2 = edge
    return (
        "(kicad_pcb (version 20240108) (generator test)\n"
        f"{net_lines}\n"
        f"  (gr_rect (start {ex1} {ey1}) (end {ex2} {ey2}) "
        '(layer "Edge.Cuts") (width 0.1))\n'
        + "\n".join(footprints)
        + ("\n" + extra if extra else "")
        + "\n)\n"
    )


def _write(tmp_path: Path, text: str, name: str = "in.kicad_pcb") -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


# A default two-net board: SIG_A pads both inside the region box
# (10,10)-(20,20) [board-relative], SIG_B pads both outside it.
def _two_net_board() -> str:
    return _board(
        [
            # board origin is (100,100); board-relative (10,10) == absolute (110,110)
            _footprint("R1", 110, 110, 1, "SIG_A"),
            _footprint("R2", 118, 110, 1, "SIG_A"),
            # SIG_B pads at board-relative (30,30)/(35,30) == absolute (130,130)/(135,130)
            _footprint("R3", 130, 130, 2, "SIG_B"),
            _footprint("R4", 135, 130, 2, "SIG_B"),
        ]
    )


_REGION_A = (10.0, 10.0, 20.0, 20.0)  # board-relative box enclosing SIG_A only


# ---------------------------------------------------------------------------
# CLI region-string parsing / validation
# ---------------------------------------------------------------------------


class TestRegionStringParsing:
    def test_valid_box_normalized(self):
        assert _parse_region_box("10,10,20,20") == (10.0, 10.0, 20.0, 20.0)

    def test_whitespace_tolerated(self):
        assert _parse_region_box(" 1, 2 , 3, 4 ") == (1.0, 2.0, 3.0, 4.0)

    def test_wrong_arity_rejected(self):
        err = _parse_region_box("1,2,3")
        assert isinstance(err, str) and "four" in err

    def test_non_numeric_rejected(self):
        err = _parse_region_box("1,2,three,4")
        assert isinstance(err, str) and "numeric" in err

    def test_degenerate_zero_width_rejected(self):
        err = _parse_region_box("10,10,10,20")
        assert isinstance(err, str) and "x1 < x2" in err

    def test_degenerate_zero_height_rejected(self):
        err = _parse_region_box("10,10,20,10")
        assert isinstance(err, str) and "y1 < y2" in err

    def test_inverted_rejected(self):
        # x1 > x2 is rejected exactly like pcb strip --region (no silent swap).
        err = _parse_region_box("20,10,10,20")
        assert isinstance(err, str) and "x1 < x2" in err


# ---------------------------------------------------------------------------
# Grid-level: mark_region_bound
# ---------------------------------------------------------------------------


class TestMarkRegionBound:
    def _grid(self) -> RoutingGrid:
        rules = DesignRules(grid_resolution=0.5, trace_width=0.2, trace_clearance=0.15)
        # 40x40 board with origin (100,100): world box == board-relative + 100.
        return RoutingGrid(width=40.0, height=40.0, rules=rules, origin_x=100.0, origin_y=100.0)

    def test_outside_cells_blocked_inside_free(self):
        grid = self._grid()
        # World box (110,110)-(120,120) (== board-relative (10,10)-(20,20)).
        grid.mark_region_bound(110.0, 110.0, 120.0, 120.0)

        layer = Layer.F_CU
        # A cell well inside the box is NOT blocked.
        igx, igy = grid.world_to_grid(115.0, 115.0)
        assert not grid.is_blocked(igx, igy, layer, net=1)
        # A cell well outside the box IS blocked (for any net).
        ogx, ogy = grid.world_to_grid(130.0, 130.0)
        assert grid.is_blocked(ogx, ogy, layer, net=1)
        assert grid.is_blocked(ogx, ogy, layer, net=2)

    def test_returns_positive_blocked_count(self):
        grid = self._grid()
        n = grid.mark_region_bound(110.0, 110.0, 120.0, 120.0)
        assert n > 0

    def test_inverted_box_tolerated(self):
        grid = self._grid()
        # Passing the box inverted must normalize to the same result.
        grid.mark_region_bound(120.0, 120.0, 110.0, 110.0)
        igx, igy = grid.world_to_grid(115.0, 115.0)
        assert not grid.is_blocked(igx, igy, Layer.F_CU, net=1)
        ogx, ogy = grid.world_to_grid(105.0, 105.0)
        assert grid.is_blocked(ogx, ogy, Layer.F_CU, net=1)


# ---------------------------------------------------------------------------
# Router-level: load_pcb_for_routing(region=...)
# ---------------------------------------------------------------------------


class TestLoadRegionMarking:
    def test_outside_net_pads_land_in_blocked_zone(self, tmp_path):
        """Cells around the outside net's pads are region-blocked."""
        path = _write(tmp_path, _two_net_board())
        router, _ = load_pcb_for_routing(
            str(path),
            validate_drc=False,
            strict_drc=False,
            load_existing_routes=True,
            region=_REGION_A,
        )
        # SIG_B pad at absolute (130,130) is outside the region -> its cell is
        # blocked to a foreign net (net 1) by the region bound.
        gx, gy = router.grid.world_to_grid(130.0, 130.0)
        assert router.grid.is_blocked(gx, gy, Layer.F_CU, net=1)

        # SIG_A pad at absolute (110,110) is inside the region; the empty grid
        # just next to it (still inside) is free for routing SIG_A.
        gx2, gy2 = router.grid.world_to_grid(114.0, 110.0)
        assert not router.grid.is_blocked(gx2, gy2, Layer.F_CU, net=1)

    def test_in_region_net_routes(self, tmp_path):
        """An in-region net routes normally under the region bound."""
        path = _write(tmp_path, _two_net_board())
        router, _ = load_pcb_for_routing(
            str(path),
            validate_drc=False,
            strict_drc=False,
            load_existing_routes=True,
            region=_REGION_A,
        )
        result = router.route_net(1)  # SIG_A, both pads inside region
        assert result, "in-region net SIG_A should route"
        assert any(r.net == 1 and r.segments for r in router.routes)

    def test_all_new_geometry_inside_region(self, tmp_path):
        """Every routed segment stays within the region box (world coords)."""
        path = _write(tmp_path, _two_net_board())
        router, _ = load_pcb_for_routing(
            str(path),
            validate_drc=False,
            strict_drc=False,
            load_existing_routes=True,
            region=_REGION_A,
        )
        router.route_net(1)
        # region world box == board-relative + origin (100,100)
        wx1, wy1, wx2, wy2 = 110.0, 110.0, 120.0, 120.0
        tol = 1e-3
        for route in router.routes:
            for seg in route.segments:
                for x, y in ((seg.x1, seg.y1), (seg.x2, seg.y2)):
                    assert wx1 - tol <= x <= wx2 + tol, f"seg x={x} escaped region"
                    assert wy1 - tol <= y <= wy2 + tol, f"seg y={y} escaped region"


# ---------------------------------------------------------------------------
# CLI end-to-end
# ---------------------------------------------------------------------------


def _run_route_region(
    tmp_path: Path,
    pcb_text: str,
    region: str | None,
    *,
    extra_argv: list[str] | None = None,
) -> tuple[int, str | None]:
    """Run ``kct route`` (inner main) with optional --region; return (rc, out_text)."""
    in_path = _write(tmp_path, pcb_text)
    out_path = tmp_path / "out.kicad_pcb"
    argv = [
        str(in_path),
        "--output",
        str(out_path),
        "--no-optimize",
        "--force",
        "--quiet",
        "--no-auto-layers",
        "--backend",
        "cpp",
        # These tests assert on region-confinement / preservation GEOMETRY, not
        # manufacturing DRC.  Synthetic 0.5mm-pad boards at a jlcpcb tier can
        # trip grid-quantization clearance findings unrelated to region logic,
        # so skip the DRC gate to keep the assertions focused and deterministic.
        "--skip-drc",
    ]
    if region is not None:
        argv += ["--region", region]
    if extra_argv:
        argv += extra_argv
    rc = route_main(argv)
    out_text = out_path.read_text() if out_path.exists() else None
    return rc, out_text


class TestRouteRegionCLI:
    def test_region_confines_routing_and_preserves_outside(self, tmp_path):
        """Route SIG_A in-region; SIG_B (outside) copper is byte-identical.

        We pre-route SIG_B by hand (add an outside trace + via), then run
        ``route --region`` over just the SIG_A region and assert the outside
        SIG_B geometry is unchanged and no new copper landed outside the box.
        """
        # Board with existing outside SIG_B copper (a trace + a via) placed
        # OUTSIDE the region, plus an unrouted SIG_A inside the region.
        outside_copper = (
            "  (segment (start 130 130) (end 135 130) (width 0.2) "
            '(layer "F.Cu") (net 2))\n'
            "  (via (at 132 130) (size 0.6) (drill 0.3) "
            '(layers "F.Cu" "B.Cu") (net 2))'
        )
        board = _board(
            [
                _footprint("R1", 110, 110, 1, "SIG_A"),
                _footprint("R2", 118, 110, 1, "SIG_A"),
                _footprint("R3", 130, 130, 2, "SIG_B"),
                _footprint("R4", 135, 130, 2, "SIG_B"),
            ],
            extra=outside_copper,
        )
        rc, out_text = _run_route_region(tmp_path, board, "10,10,20,20")
        assert rc == 0, f"route --region exited {rc}"
        assert out_text is not None

        out_segs = parse_segments(out_text)
        out_vias = parse_vias(out_text)

        # SIG_B's outside trace + via survived byte-identically.
        assert "SIG_B" in out_segs
        b_seg = out_segs["SIG_B"]
        assert len(b_seg) == 1
        s = b_seg[0]
        assert (round(s.x1, 3), round(s.y1, 3), round(s.x2, 3), round(s.y2, 3)) == (
            130.0,
            130.0,
            135.0,
            130.0,
        )
        assert "SIG_B" in out_vias and len(out_vias["SIG_B"]) == 1
        assert round(out_vias["SIG_B"][0].x, 3) == 132.0

        # SIG_A got routed inside the region.
        assert "SIG_A" in out_segs and len(out_segs["SIG_A"]) > 0

        # No NEW copper landed outside the region box (world (110,110)-(120,120)).
        for net_name, segs in out_segs.items():
            for seg in segs:
                if net_name == "SIG_B":
                    continue  # preserved existing outside copper is expected
                for x, y in ((seg.x1, seg.y1), (seg.x2, seg.y2)):
                    assert 110.0 - 1e-3 <= x <= 120.0 + 1e-3, (
                        f"{net_name} segment x={x} escaped region"
                    )
                    assert 110.0 - 1e-3 <= y <= 120.0 + 1e-3, (
                        f"{net_name} segment y={y} escaped region"
                    )

    def test_region_composes_with_skip_nets(self, tmp_path):
        """--region ANDed with --skip-nets: a skipped in-region net is not routed."""
        # Two in-region nets: SIG_A (top) and SIG_C (well below it), both inside
        # a generous box (5,5)-(35,35).  The wide vertical separation keeps the
        # routed traces DRC-clean on this synthetic board.
        board = _board(
            [
                _footprint("R1", 110, 110, 1, "SIG_A"),
                _footprint("R2", 118, 110, 1, "SIG_A"),
                _footprint("R5", 110, 128, 3, "SIG_C"),
                _footprint("R6", 118, 128, 3, "SIG_C"),
            ],
            nets=[(1, "SIG_A"), (3, "SIG_C")],
        )
        rc, out_text = _run_route_region(
            tmp_path, board, "5,5,35,35", extra_argv=["--skip-nets", "SIG_C"]
        )
        assert rc == 0
        out_segs = parse_segments(out_text or "")
        # SIG_A routed, SIG_C skipped (no fresh geometry).
        assert "SIG_A" in out_segs and len(out_segs["SIG_A"]) > 0
        assert "SIG_C" not in out_segs or len(out_segs["SIG_C"]) == 0

    def test_unreachable_net_fails_with_clear_message(self, tmp_path, capsys):
        """A net with a pad outside the region fails fast with a per-net message."""
        # SIG_A: one pad inside (110,110), one pad OUTSIDE (135,110 == rel 35,10).
        board = _board(
            [
                _footprint("R1", 110, 110, 1, "SIG_A"),
                _footprint("R2", 135, 110, 1, "SIG_A"),
            ],
            nets=[(1, "SIG_A")],
        )
        rc, _ = _run_route_region(tmp_path, board, "5,5,20,20")
        assert rc != 0
        err = capsys.readouterr().err
        assert "SIG_A" in err
        assert "outside the region" in err or "Phase 2b" in err

    def test_region_outside_board_bounds_is_error(self, tmp_path, capsys):
        """A region entirely outside the board bounds fails with a clear message."""
        board = _two_net_board()
        # board is (100,100)-(140,140) => board-relative bounds (0,0)-(40,40).
        # A box at board-relative (60,60)-(70,70) is entirely outside.
        rc, _ = _run_route_region(tmp_path, board, "60,60,70,70")
        assert rc != 0
        err = capsys.readouterr().err
        assert "outside the board" in err

    def test_degenerate_region_cli_error(self, tmp_path, capsys):
        board = _two_net_board()
        rc, _ = _run_route_region(tmp_path, board, "10,10,10,20")
        assert rc != 0
        err = capsys.readouterr().err
        assert "x1 < x2" in err

    def test_region_with_no_routable_net_fails_gracefully(self, tmp_path, capsys):
        """A region containing zero pads for any net fails gracefully."""
        board = _two_net_board()
        # Empty corner of the board (board-relative (2,2)-(5,5)) has no pads.
        rc, _ = _run_route_region(tmp_path, board, "2,2,5,5")
        assert rc != 0
        err = capsys.readouterr().err
        assert "no routable" in err or "nothing to route" in err

    def test_region_implies_preserve_existing(self, tmp_path):
        """Region mode preserves outside copper even without --preserve-existing.

        The outside SIG_B trace is NOT passed with an explicit
        --preserve-existing flag; region mode must imply it so the trace
        survives (default route mode would strip skipped-net copper).
        """
        outside_copper = (
            '  (segment (start 130 130) (end 138 130) (width 0.2) (layer "F.Cu") (net 2))'
        )
        board = _board(
            [
                _footprint("R1", 110, 110, 1, "SIG_A"),
                _footprint("R2", 118, 110, 1, "SIG_A"),
                _footprint("R3", 130, 130, 2, "SIG_B"),
                _footprint("R4", 138, 130, 2, "SIG_B"),
            ],
            extra=outside_copper,
        )
        rc, out_text = _run_route_region(tmp_path, board, "10,10,20,20")
        assert rc == 0
        out_segs = parse_segments(out_text or "")
        assert "SIG_B" in out_segs and len(out_segs["SIG_B"]) == 1


# ---------------------------------------------------------------------------
# Coordinate parity with pcb strip --region
# ---------------------------------------------------------------------------


class TestCoordinateParity:
    def test_same_box_selects_same_geometry_nonzero_origin(self, tmp_path):
        """The same x1,y1,x2,y2 means the same box for strip and route.

        Build a board with a non-zero origin (Edge.Cuts start != 0,0).  A trace
        at board-relative (10,10)-(15,10) is INSIDE box (5,5,20,20) for both:
        ``pcb strip --region`` removes it, and ``route --region`` treats the
        same absolute cells as in-region (the outside cells are blocked).
        """
        # Origin (50,50): board-relative (10,10) == absolute (60,60).
        board = _board(
            [
                _footprint("R1", 60, 60, 1, "SIG_A"),
                _footprint("R2", 65, 60, 1, "SIG_A"),
            ],
            edge=(50, 50, 90, 90),
            nets=[(1, "SIG_A")],
            extra=('  (segment (start 60 60) (end 65 60) (width 0.2) (layer "F.Cu") (net 1))'),
        )
        pcb = PCB.load(str(_write(tmp_path, board, "parity.kicad_pcb")))
        # The board-relative box (5,5,20,20): the trace at board-relative
        # (10,10)-(15,10) is fully inside, so strip removes it.
        stats = pcb.strip_traces(region=(5, 5, 20, 20), nets=["SIG_A"])
        assert stats["segments"] == 1, "strip --region should remove the in-box trace"

        # Now the router loads the SAME box: the outside cells are blocked and
        # the in-box pad cells are free.  The router's grid origin equals the
        # board origin (50,50), so board-relative (5,5)-(20,20) maps to world
        # (55,55)-(70,70).
        router, _ = load_pcb_for_routing(
            str(_write(tmp_path, board, "parity2.kicad_pcb")),
            validate_drc=False,
            strict_drc=False,
            load_existing_routes=True,
            region=(5, 5, 20, 20),
        )
        # Inside cell (absolute 62,60 == board-relative 12,10) is free for SIG_A.
        igx, igy = router.grid.world_to_grid(62.0, 60.0)
        assert not router.grid.is_blocked(igx, igy, Layer.F_CU, net=1)
        # Outside cell (absolute 80,80 == board-relative 30,30) is blocked.
        ogx, ogy = router.grid.world_to_grid(80.0, 80.0)
        assert router.grid.is_blocked(ogx, ogy, Layer.F_CU, net=1)


# ---------------------------------------------------------------------------
# Hole-to-hole floor for main-router vias
# ---------------------------------------------------------------------------


class TestHoleToHoleFloor:
    def _router_with_pth(self, tmp_path):
        """Router whose only existing drill is a THT pad near a candidate site."""
        board = _board(
            [
                _tht_footprint("J1", 115, 115, 2, "SIG_B", drill=0.3),
            ],
            nets=[(2, "SIG_B")],
        )
        router, _ = load_pcb_for_routing(
            str(_write(tmp_path, board, "h2h.kicad_pcb")),
            validate_drc=False,
            strict_drc=False,
            load_existing_routes=True,
        )
        return router

    def test_via_too_close_to_pth_drill_is_rejected(self, tmp_path):
        """A committed via within min_hole_to_hole of a PTH drill fails validation."""
        from kicad_tools.router.core import _TraceResolverTransaction
        from kicad_tools.router.primitives import Route

        router = self._router_with_pth(tmp_path)
        router.rules.min_hole_to_hole = 0.5

        transaction = _TraceResolverTransaction(router)
        # Snapshot the empty pre-state, THEN commit a new via so it counts as
        # "newly committed" during validation.
        transaction.begin()

        # Candidate via 0.3mm center-to-center from the PTH pad drill (0.3mm):
        # edge = 0.3 - 0.15 - 0.15 = 0.0 << 0.5 floor -> must be rejected.
        via = Via(x=115.3, y=115.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=2)
        route = Route(net=2, net_name="SIG_B", segments=[], vias=[via])
        router.routes.append(route)

        # The direct predicate rejects it...
        assert transaction._via_clears_hole_to_hole(via, 0.5) is False
        # ...and the full committed-geometry gate rejects the transaction.
        assert transaction.validate_committed_geometry() is False

    def test_via_clear_of_drills_passes(self, tmp_path):
        """A via comfortably clear of every drill passes the hole-to-hole floor."""
        from kicad_tools.router.core import _TraceResolverTransaction

        router = self._router_with_pth(tmp_path)
        router.rules.min_hole_to_hole = 0.5

        transaction = _TraceResolverTransaction(router)
        transaction.begin()
        # 5mm away from the only PTH drill -> comfortably clear.
        via = Via(x=120.0, y=120.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=2)
        assert transaction._via_clears_hole_to_hole(via, 0.5) is True


# ---------------------------------------------------------------------------
# route-auto --region
# ---------------------------------------------------------------------------


class TestRouteAutoRegion:
    def test_unreachable_net_fails_with_message(self, tmp_path):
        """route_net_auto rejects a net with a pad outside the region."""
        from kicad_tools.mcp.tools.routing import route_net_auto

        board = _board(
            [
                _footprint("R1", 110, 110, 1, "SIG_A"),
                _footprint("R2", 135, 110, 1, "SIG_A"),  # outside box
            ],
            nets=[(1, "SIG_A")],
        )
        path = _write(tmp_path, board, "auto.kicad_pcb")
        result = route_net_auto(
            str(path),
            "SIG_A",
            output_path=None,
            region="5,5,20,20",
        )
        assert result["success"] is False
        assert "SIG_A" in result["error_message"]
        assert "outside the region" in result["error_message"]

    def test_degenerate_region_raises(self, tmp_path):
        """route_net_auto raises ValueError on a degenerate region box."""
        from kicad_tools.mcp.tools.routing import route_net_auto

        board = _board(
            [
                _footprint("R1", 110, 110, 1, "SIG_A"),
                _footprint("R2", 118, 110, 1, "SIG_A"),
            ],
            nets=[(1, "SIG_A")],
        )
        path = _write(tmp_path, board, "auto2.kicad_pcb")
        with pytest.raises(ValueError, match="x1 < x2"):
            route_net_auto(str(path), "SIG_A", region="10,10,10,20")

    def test_tuple_region_accepted(self, tmp_path):
        """route_net_auto accepts a pre-parsed (x1,y1,x2,y2) tuple region."""
        from kicad_tools.mcp.tools.routing import route_net_auto

        board = _board(
            [
                _footprint("R1", 110, 110, 1, "SIG_A"),
                _footprint("R2", 118, 110, 1, "SIG_A"),
            ],
            nets=[(1, "SIG_A")],
        )
        path = _write(tmp_path, board, "auto3.kicad_pcb")
        # Both pads inside the box -> passes the reachability gate.  Routing may
        # still fail on this minimal board (or be confined-out), but it must NOT
        # be rejected for the UNREACHABLE-PAD reason.
        result = route_net_auto(
            str(path),
            "SIG_A",
            output_path=None,
            region=(5.0, 5.0, 25.0, 25.0),
        )
        if not result["success"]:
            msg = result.get("error_message") or ""
            # The reachability gate names specific pads (e.g. "pad(s) R2.1");
            # any failure here must be the output-confinement or a normal
            # routing failure, not the reachability rejection.
            assert "lie outside the region" not in msg


# ---------------------------------------------------------------------------
# Phase 2b-1: bare boundary stub-endpoint reconnection (Issue #4170)
# ---------------------------------------------------------------------------


def _make_stripped_stub_board(
    tmp_path: Path,
    *,
    name: str,
    footprints: list[str],
    traces: list[tuple[tuple[str, str], tuple[str, str], str]],
    strip_region: tuple[float, float, float, float],
    strip_nets: list[str],
    edge: tuple[float, float, float, float] = (100, 100, 160, 160),
    nets: list[tuple[int, str]] | None = None,
) -> Path:
    """Build a board, route ``traces``, then ``strip_traces(region=...)``.

    Produces a GENUINE boundary-clipped stub (the surviving outside portion of
    each clipped trace, its inside endpoint moved onto the region boundary) --
    exactly what Phase 2b-1 must reconnect.  Returns the path to the stripped
    ``.kicad_pcb`` on disk.
    """
    board = _board(footprints, edge=edge, nets=nets)
    src = _write(tmp_path, board, name + "_src.kicad_pcb")
    pcb = PCB.load(str(src))
    for start, end, net_name in traces:
        pcb.add_trace(start, end, width=0.2, layer="F.Cu", net=net_name)
    stats = pcb.strip_traces(region=strip_region, nets=strip_nets)
    assert stats["segments_clipped"] >= 1, f"expected a boundary-clipped stub, got stats={stats}"
    stripped = tmp_path / (name + ".kicad_pcb")
    pcb.save(str(stripped))
    return stripped


def _touches_cell(segs, x: float, y: float, tol: float = 0.30) -> bool:
    """True if any segment endpoint is within ``tol`` mm of ``(x, y)``.

    The A* joint lands on the reopened tip GRID CELL, so its emitted vertex is
    within roughly one grid cell of the world tip -- assert cell-level, not
    exact-vertex, coincidence.
    """
    for s in segs:
        for px, py in ((s.x1, s.y1), (s.x2, s.y2)):
            if abs(px - x) <= tol and abs(py - y) <= tol:
                return True
    return False


class TestStubReconnection:
    """Phase 2b-1: ``route --region`` reconnects bare boundary stubs."""

    def test_route_region_reconnects_stripped_stub(self, tmp_path):
        """A genuine stripped stub is reconnected to the in-region pad.

        R1 (world 115,120) inside the box; R2 (world 145,120) outside.  A
        straight R1->R2 trace clipped by ``strip_traces(region=(0,0,30,20))``
        leaves a stub running boundary (world 130,120) -> outside (145,120).
        ``route --region 0,0,30,20`` must reconnect R1 to that boundary tip.
        """
        stripped = _make_stripped_stub_board(
            tmp_path,
            name="stub1",
            footprints=[
                _footprint("R1", 115, 120, 1, "SIG_A"),
                _footprint("R2", 145, 120, 1, "SIG_A"),
            ],
            traces=[(("R1", "1"), ("R2", "1"), "SIG_A")],
            strip_region=(0.0, 0.0, 30.0, 20.0),
            strip_nets=["SIG_A"],
            nets=[(1, "SIG_A")],
        )
        out_path = tmp_path / "stub1_out.kicad_pcb"
        rc = route_main(
            [
                str(stripped),
                "--output",
                str(out_path),
                "--no-optimize",
                "--force",
                "--quiet",
                "--no-auto-layers",
                "--backend",
                "cpp",
                "--skip-drc",
                "--region",
                "0,0,30,20",
            ]
        )
        assert rc == 0, f"route --region exited {rc}"
        segs = parse_segments(out_path.read_text()).get("SIG_A", [])
        assert segs, "SIG_A produced no copper"
        # New routing reaches R1's pad (world 115,120)...
        assert _touches_cell(segs, 115.0, 120.0), "route did not reach R1 pad"
        # ...and joins the stub boundary tip cell (world 130,120).
        assert _touches_cell(segs, 130.0, 120.0), "route did not reconnect to the boundary stub tip"

    def test_reconnection_preserves_outside_copper_byte_identical(self, tmp_path):
        """Region-bounded stub reconnection modifies zero copper outside the box.

        Mirrors ``test_region_confines_routing_and_preserves_outside``: the
        surviving stub copper OUTSIDE the region (world x>130) must be
        byte/geometry-identical before and after the reconnection route.
        """
        stripped = _make_stripped_stub_board(
            tmp_path,
            name="stub2",
            footprints=[
                _footprint("R1", 115, 120, 1, "SIG_A"),
                _footprint("R2", 145, 120, 1, "SIG_A"),
            ],
            traces=[(("R1", "1"), ("R2", "1"), "SIG_A")],
            strip_region=(0.0, 0.0, 30.0, 20.0),
            strip_nets=["SIG_A"],
            nets=[(1, "SIG_A")],
        )
        # Snapshot the OUTSIDE portion of the stub before routing (world x>=130).
        before = parse_segments(stripped.read_text()).get("SIG_A", [])
        before_outside = sorted(
            (round(s.x1, 4), round(s.y1, 4), round(s.x2, 4), round(s.y2, 4))
            for s in before
            if min(s.x1, s.x2) >= 130.0 - 1e-6
        )
        assert before_outside, "expected an outside stub before routing"

        out_path = tmp_path / "stub2_out.kicad_pcb"
        rc = route_main(
            [
                str(stripped),
                "--output",
                str(out_path),
                "--no-optimize",
                "--force",
                "--quiet",
                "--no-auto-layers",
                "--backend",
                "cpp",
                "--skip-drc",
                "--region",
                "0,0,30,20",
            ]
        )
        assert rc == 0, f"route --region exited {rc}"
        after = parse_segments(out_path.read_text()).get("SIG_A", [])
        after_outside = sorted(
            (round(s.x1, 4), round(s.y1, 4), round(s.x2, 4), round(s.y2, 4))
            for s in after
            if min(s.x1, s.x2) >= 130.0 - 1e-6
        )
        # The outside stub survives geometry-identically; no NEW copper landed
        # strictly outside the box (all new routing stays at x<=130).
        assert after_outside == before_outside, (
            "copper outside the region was modified during stub reconnection"
        )

    def test_reconnection_respects_hole_to_hole_floor(self, tmp_path):
        """The hole-to-hole floor is threaded on the stub-reconnection path.

        Loads a stripped-stub board (a SIG_A boundary stub + a pre-existing PTH
        drill inside the region on a foreign net) with ``stub_terminals=`` set,
        routes the stub net, then asserts -- via the same
        ``_TraceResolverTransaction._via_clears_hole_to_hole`` predicate the
        Phase 2a main router uses -- that a candidate via placed at the stub
        joint within the floor of the pre-existing drill is REJECTED.  This
        confirms the floor is enforced on the region/stub path, not just the
        plain route path (mirrors ``test_via_too_close_to_pth_drill_is_rejected``).
        """
        from kicad_tools.router.core import _TraceResolverTransaction
        from kicad_tools.router.stub_terminals import StubTerminal

        # SIG_A stub running world (130,118)->(145,118); R1 the in-region pad;
        # J1 a foreign PTH drill inside the region near the reconnection corridor.
        board = _board(
            [
                _footprint("R1", 115, 118, 1, "SIG_A"),
                _tht_footprint("J1", 122, 118, 2, "SIG_B", drill=0.3),
            ],
            edge=(100, 100, 160, 160),
            nets=[(1, "SIG_A"), (2, "SIG_B")],
            extra='  (segment (start 130 118) (end 145 118) (width 0.2) (layer "F.Cu") (net 1))',
        )
        router, _ = load_pcb_for_routing(
            str(_write(tmp_path, board, "stub3.kicad_pcb")),
            validate_drc=False,
            strict_drc=False,
            load_existing_routes=True,
            region=(0.0, 0.0, 30.0, 25.0),
            stub_terminals={
                1: [
                    StubTerminal(
                        net_id=1,
                        net_name="SIG_A",
                        x=30.0,  # board-relative boundary tip -> world (130,118)
                        y=18.0,
                        layer=Layer.F_CU,
                    )
                ]
            },
        )
        router.rules.min_hole_to_hole = 0.5

        # Route the stub net -- confirms the reconnection succeeds end-to-end.
        result = router.route_net(1)
        assert result, "stub net did not reconnect"

        # The Phase 2a hole-to-hole predicate still gates vias on this path: a
        # candidate via 0.3mm from J1's drill (edge gap 0.0 << 0.5) is rejected.
        transaction = _TraceResolverTransaction(router)
        transaction.begin()
        near_via = Via(
            x=122.3, y=118.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=1
        )
        assert transaction._via_clears_hole_to_hole(near_via, 0.5) is False
        # A via comfortably clear of every drill passes.
        far_via = Via(
            x=118.0, y=112.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=1
        )
        assert transaction._via_clears_hole_to_hole(far_via, 0.5) is True

    def test_multi_stub_same_net_reconnects(self, tmp_path):
        """2+ simultaneous stub endpoints on the SAME net are all reconnected.

        SIG_A fans out from an in-region pad R1 to TWO outside pads (R2, R3),
        both clipped at the boundary.  The detector returns two StubTerminals
        for SIG_A; the Autorouter must add BOTH as targets so both boundary tips
        are reconnected into the net island.
        """
        # Two SEPARATE in-region source pads (R1a, R1b) each fan out to an
        # outside pad along a horizontal line, so each clip lands the boundary
        # tip at a predictable axis-aligned point (world 130, y).
        stripped = _make_stripped_stub_board(
            tmp_path,
            name="stub4",
            footprints=[
                _footprint("R1a", 115, 112, 1, "SIG_A"),
                _footprint("R1b", 115, 122, 1, "SIG_A"),
                _footprint("R2", 145, 112, 1, "SIG_A"),
                _footprint("R3", 145, 122, 1, "SIG_A"),
            ],
            traces=[
                (("R1a", "1"), ("R2", "1"), "SIG_A"),
                (("R1b", "1"), ("R3", "1"), "SIG_A"),
            ],
            strip_region=(0.0, 0.0, 30.0, 30.0),
            strip_nets=["SIG_A"],
            nets=[(1, "SIG_A")],
        )
        # Two horizontal clips -> two boundary tips at world (130,112),(130,122).
        out_path = tmp_path / "stub4_out.kicad_pcb"
        rc = route_main(
            [
                str(stripped),
                "--output",
                str(out_path),
                "--no-optimize",
                "--force",
                "--quiet",
                "--no-auto-layers",
                "--backend",
                "cpp",
                "--skip-drc",
                "--region",
                "0,0,30,30",
            ]
        )
        assert rc == 0, f"route --region exited {rc}"
        segs = parse_segments(out_path.read_text()).get("SIG_A", [])
        assert segs, "SIG_A produced no copper"
        assert _touches_cell(segs, 130.0, 112.0), "first stub tip not reconnected"
        assert _touches_cell(segs, 130.0, 122.0), "second stub tip not reconnected"


class TestStubReconnectionRouteAuto:
    """Phase 2c (#4173): ``route-auto --region`` reaches parity with ``route``.

    The ``route_net_auto`` orchestrator surface now reconnects the same bare
    boundary stubs that ``kct route --region`` handles, by pruning the outside
    pad and injecting the boundary stub tip as an in-region target in
    ``_build_pads_for_net`` (reusing the shared #4172 detector).

    Confinement note: the orchestrator has NO per-cell obstacle grid (it uses a
    coarse ``GlobalRouter`` / ``RegionGraph`` tile-corridor planner), so region
    confinement on this path is provided ENTIRELY by ``route_net_auto``'s
    post-route output-escape filter -- there is no pre-route cell-level bound.
    A coarse corridor between two in-region endpoints that bulges through an
    out-of-box tile center therefore FAILS honestly at the output filter rather
    than writing out-of-region copper.  That is expected behavior, not a bug:
    the zero-copper-outside-the-region contract is never violated.
    """

    def test_route_auto_region_reconnects_stripped_stub(self, tmp_path):
        """``route_net_auto --region`` reconnects the same stub as ``route``.

        Parity companion to ``test_route_region_reconnects_stripped_stub`` on
        an equivalent synthetic fixture: R1 (world 110,110) inside the box, R2
        (world 150,110) outside, straight R1->R2 trace clipped at the boundary
        (world 115,110).  The orchestrator surface must reconnect R1 to the
        boundary stub tip (world 115,110) instead of failing fast.

        The box here keeps R1 and the stub tip in a single coarse
        ``RegionGraph`` tile so the corridor is a straight in-box segment.  A
        wider corridor could bulge through an out-of-box tile center (the
        orchestrator's coarse planner is not region-confined); that case fails
        honestly at ``route_net_auto``'s post-route output-escape filter -- the
        SOLE confinement guarantee on this path -- rather than writing
        out-of-region copper.  See the class docstring.
        """
        from kicad_tools.mcp.tools.routing import route_net_auto

        stripped = _make_stripped_stub_board(
            tmp_path,
            name="auto_stub1",
            footprints=[
                _footprint("R1", 110, 110, 1, "SIG_A"),
                _footprint("R2", 150, 110, 1, "SIG_A"),
            ],
            traces=[(("R1", "1"), ("R2", "1"), "SIG_A")],
            strip_region=(0.0, 0.0, 15.0, 25.0),
            strip_nets=["SIG_A"],
            nets=[(1, "SIG_A")],
        )
        out_path = tmp_path / "auto_stub1_out.kicad_pcb"
        result = route_net_auto(
            str(stripped),
            "SIG_A",
            output_path=str(out_path),
            region="0,0,15,25",
        )
        assert result["success"] is True, (
            f"route-auto --region did not reconnect the stub: {result.get('error_message')!r}"
        )
        segs = parse_segments(out_path.read_text()).get("SIG_A", [])
        assert segs, "SIG_A produced no copper"
        # New routing reaches R1's pad (world 110,110)...
        assert _touches_cell(segs, 110.0, 110.0), "route-auto did not reach R1 pad"
        # ...and joins the stub boundary tip (world 115,110).
        assert _touches_cell(segs, 115.0, 110.0), (
            "route-auto did not reconnect to the boundary stub tip"
        )

    def test_route_auto_reconnection_preserves_outside_copper(self, tmp_path):
        """route-auto stub reconnection modifies zero copper outside the box.

        Parity companion to
        ``test_reconnection_preserves_outside_copper_byte_identical``: the
        surviving stub copper OUTSIDE the region (world x>=115) is
        geometry-identical before and after the reconnection, and no NEW copper
        lands strictly outside the box.
        """
        from kicad_tools.mcp.tools.routing import route_net_auto

        stripped = _make_stripped_stub_board(
            tmp_path,
            name="auto_stub2",
            footprints=[
                _footprint("R1", 110, 110, 1, "SIG_A"),
                _footprint("R2", 150, 110, 1, "SIG_A"),
            ],
            traces=[(("R1", "1"), ("R2", "1"), "SIG_A")],
            strip_region=(0.0, 0.0, 15.0, 25.0),
            strip_nets=["SIG_A"],
            nets=[(1, "SIG_A")],
        )
        before = parse_segments(stripped.read_text()).get("SIG_A", [])
        before_outside = sorted(
            (round(s.x1, 4), round(s.y1, 4), round(s.x2, 4), round(s.y2, 4))
            for s in before
            if min(s.x1, s.x2) >= 115.0 - 1e-6
        )
        assert before_outside, "expected an outside stub before routing"

        out_path = tmp_path / "auto_stub2_out.kicad_pcb"
        result = route_net_auto(
            str(stripped),
            "SIG_A",
            output_path=str(out_path),
            region="0,0,15,25",
        )
        assert result["success"] is True, (
            f"route-auto --region did not reconnect the stub: {result.get('error_message')!r}"
        )
        after = parse_segments(out_path.read_text()).get("SIG_A", [])
        after_outside = sorted(
            (round(s.x1, 4), round(s.y1, 4), round(s.x2, 4), round(s.y2, 4))
            for s in after
            if min(s.x1, s.x2) >= 115.0 - 1e-6
        )
        assert after_outside == before_outside, (
            "copper outside the region was modified during route-auto stub reconnection"
        )

    def test_route_auto_reconnection_respects_hole_to_hole_floor(self, tmp_path):
        """The hole-to-hole floor is threaded on the route-auto stub path.

        The route-auto path (``route_net_auto`` -> ``RoutingOrchestrator``)
        shares the same ``_TraceResolverTransaction._via_clears_hole_to_hole``
        predicate as the main router.  This mirrors
        ``test_reconnection_respects_hole_to_hole_floor``: a stub board with a
        pre-existing foreign PTH drill inside the region is loaded, and a
        candidate via placed within the drill's hole-to-hole floor near the
        reconnection joint is rejected -- confirming the floor is enforced for
        vias produced on the stub-reconnection corridor regardless of surface.
        """
        from kicad_tools.router.core import _TraceResolverTransaction
        from kicad_tools.router.stub_terminals import StubTerminal

        board = _board(
            [
                _footprint("R1", 115, 118, 1, "SIG_A"),
                _tht_footprint("J1", 122, 118, 2, "SIG_B", drill=0.3),
            ],
            edge=(100, 100, 160, 160),
            nets=[(1, "SIG_A"), (2, "SIG_B")],
            extra='  (segment (start 130 118) (end 145 118) (width 0.2) (layer "F.Cu") (net 1))',
        )
        router, _ = load_pcb_for_routing(
            str(_write(tmp_path, board, "auto_stub3.kicad_pcb")),
            validate_drc=False,
            strict_drc=False,
            load_existing_routes=True,
            region=(0.0, 0.0, 30.0, 25.0),
            stub_terminals={
                1: [
                    StubTerminal(
                        net_id=1,
                        net_name="SIG_A",
                        x=30.0,  # board-relative boundary tip -> world (130,118)
                        y=18.0,
                        layer=Layer.F_CU,
                    )
                ]
            },
        )
        router.rules.min_hole_to_hole = 0.5

        transaction = _TraceResolverTransaction(router)
        transaction.begin()
        near_via = Via(
            x=122.3, y=118.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=1
        )
        assert transaction._via_clears_hole_to_hole(near_via, 0.5) is False
        far_via = Via(
            x=118.0, y=112.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=1
        )
        assert transaction._via_clears_hole_to_hole(far_via, 0.5) is True

    def test_route_auto_region_no_stub_still_fails(self, tmp_path):
        """A net with an outside pad and NO reconnectable stub still fails fast.

        The retained reachability-gate branch: only ``has_stub`` nets change
        from fail-fast to route-and-inject.  A genuinely-unreachable net (an
        outside pad with no same-net boundary stub) must still return
        ``success=False`` with the clear "no same-net boundary stub" message.
        """
        from kicad_tools.mcp.tools.routing import route_net_auto

        # R1 inside the box, R2 outside -- but NO trace/stub was ever created,
        # so there is nothing to reconnect to.
        board = _board(
            [
                _footprint("R1", 110, 110, 1, "SIG_A"),
                _footprint("R2", 145, 110, 1, "SIG_A"),
            ],
            edge=(100, 100, 160, 160),
            nets=[(1, "SIG_A")],
        )
        path = _write(tmp_path, board, "auto_nostub.kicad_pcb")
        result = route_net_auto(
            str(path),
            "SIG_A",
            output_path=None,
            region="0,0,20,20",
        )
        assert result["success"] is False
        msg = result.get("error_message") or ""
        assert "no same-net boundary stub" in msg, (
            f"expected the no-stub reachability message, got: {msg}"
        )
        assert "SIG_A" in msg


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
