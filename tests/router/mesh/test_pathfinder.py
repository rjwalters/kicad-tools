"""End-to-end MeshPathfinder acceptance tests on committed fleet boards (#4268).

Proves the P1 vertical slice on real geometry: parse a board -> mesh route one
net -> 45-legal copper (passes the #3907 ``to_sexp`` choke with enforcement
ON) -> DRC-clean under ``kicad-cli pcb drc --refill-zones``.

The issue names softstart rev-C as the motivating board, but softstart is a
local-only external symlink that dangles in fresh worktrees / CI, so the
committed acceptance runs on in-repo fleet boards (charlieplex, stm32).  The
softstart rev-C run is exercised manually and reported in the PR body.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.drc.geometric import run_geometric_drc
from kicad_tools.router.io import (
    _extract_edge_segments,
    load_pads_for_analysis,
    merge_routes_into_pcb,
)
from kicad_tools.router.mesh.pathfinder import MeshPathfinder
from kicad_tools.router.primitives import (
    is_segment_45_enforcement_enabled,
)

pytest.importorskip("kicad_tools.router.router_cpp")

_REPO = Path(__file__).resolve().parents[3]
_CHARLIEPLEX = _REPO / "boards/02-charlieplex-led/output/charlieplex_3x3.kicad_pcb"
_STM32 = _REPO / "boards/04-stm32-devboard/output/stm32_devboard.kicad_pcb"


def _pads_by_net(pads):
    bynet: dict[int, list] = {}
    for p in pads:
        if p.net > 0:
            bynet.setdefault(p.net, []).append(p)
    return bynet


def _two_pad_nets(pads):
    """2-pad, single-layer nets sorted by separation (farthest first)."""
    out = []
    for net, ps in _pads_by_net(pads).items():
        if len(ps) == 2 and ps[0].layer == ps[1].layer:
            d = ((ps[0].x - ps[1].x) ** 2 + (ps[0].y - ps[1].y) ** 2) ** 0.5
            out.append((d, net, ps))
    out.sort(reverse=True)
    return out


def test_mesh_binding_available() -> None:
    import kicad_tools.router.router_cpp as rc

    assert hasattr(rc, "constrained_delaunay")


def test_routes_single_net_emits_45_legal_drc_clean_charlieplex(tmp_path) -> None:
    """Route one real net end-to-end; assert 45-legal AND DRC-clean.

    All file I/O (including kicad-cli ``.kicad_prl`` sidecars) is confined to
    ``tmp_path`` so the committed board tree is never polluted.
    """
    assert is_segment_45_enforcement_enabled(), "the #3907 choke must be ON"
    text = _CHARLIEPLEX.read_text()
    pads = load_pads_for_analysis(text)
    pf = MeshPathfinder.from_board(text)

    routed = None
    for _d, _net, (a, b) in _two_pad_nets(pads):
        r = pf.route(a, b)
        if r is not None and r.segments:
            routed = r
            break
    assert routed is not None, "expected at least one routable net"

    # 45-legality by construction: serialization raises on off-angle copper
    # when enforcement is on (it is, asserted above).
    sexp = routed.to_sexp()
    assert "(segment" in sexp

    out = tmp_path / "charlieplex_routed.kicad_pcb"
    out.write_text(merge_routes_into_pcb(text, sexp))
    res = run_geometric_drc(out)
    if not res.ran:
        pytest.skip(f"kicad-cli DRC unavailable: {res.reason}")
    assert res.error_count == 0, f"DRC errors: {dict(res.by_type)}"


def test_mesh_route_never_shorts_on_dense_board_stm32(tmp_path) -> None:
    """The #3906 guarantee on real geometry: no emitted route adds DRC errors.

    stm32 has dense fine-pitch pads; the clearance gate must EITHER decline a
    net (return None) or emit a route that introduces zero new DRC violations
    -- it must never ship a short.  (An obstacle-blind fit shorted here.)

    File I/O is confined to ``tmp_path`` (the baseline board is copied there so
    kicad-cli's ``.kicad_prl`` sidecar never lands in the committed tree).
    """
    text = _STM32.read_text()
    pads = load_pads_for_analysis(text)
    pf = MeshPathfinder.from_board(text)

    base_pcb = tmp_path / "stm32_base.kicad_pcb"
    base_pcb.write_text(text)
    base = run_geometric_drc(base_pcb)
    if not base.ran:
        pytest.skip(f"kicad-cli DRC unavailable: {base.reason}")

    # Take the single farthest net; if declined, take the next routable one.
    route = None
    for _d, _net, (a, b) in _two_pad_nets(pads):
        r = pf.route(a, b)
        if r is not None and r.segments:
            route = r
            break
    assert route is not None

    out = tmp_path / "stm32_routed.kicad_pcb"
    out.write_text(merge_routes_into_pcb(text, route.to_sexp()))
    res = run_geometric_drc(out)
    assert res.ran
    # No NEW error-severity findings vs the unrouted baseline.
    assert res.error_count <= base.error_count, (
        f"route introduced DRC errors: base={base.error_count} "
        f"routed={res.error_count} types={dict(res.by_type)}"
    )


def test_node_count_is_orders_below_grid() -> None:
    """Criterion 6: mesh substrate node count is far below the grid's 63.5M."""
    import kicad_tools.router.router_cpp as rc

    text = _STM32.read_text()
    pads = load_pads_for_analysis(text)
    segs = _extract_edge_segments(text)
    xs = [c for s in segs for c in (s[0][0], s[1][0])]
    ys = [c for s in segs for c in (s[0][1], s[1][1])]
    outline = [
        (min(xs), min(ys)),
        (max(xs), min(ys)),
        (max(xs), max(ys)),
        (min(xs), max(ys)),
    ]
    steiner = [(p.x, p.y) for p in pads]
    verts, tris = rc.constrained_delaunay(outline, [], steiner)
    assert 0 < len(verts) < 100_000  # << 63.5M grid cells
    assert 0 < len(tris) < 200_000
