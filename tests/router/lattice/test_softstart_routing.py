"""Softstart rev-C lattice ROUTING proof (issue #4271, epic #4267 P4).

The substrate-size proof lives in ``test_softstart_memory.py``; this is the
routing half: the lattice engine negotiates the real 160x100mm 4-layer board
the grid cannot route at pad-exact fidelity (#4242), with the net-class
sidecar threaded (Phase A of #4271) so HV_HICUR copper is emitted AND spaced
at its true 2.6mm width.

The board is a local-only external fixture (``boards/external/softstart``
is a symlink that dangles in CI and fresh worktrees), so the whole module
skips cleanly when it is absent -- exactly like the memory test.

Assertions (pinned to the measured 2026-07-16 P4 verdict; see #4271):

1. ``lattice_builds == 1`` at 287-connection scale (static substrate).
2. Zero cross-net short in the emitted copper (the #3906 invariant checked
   pairwise at the per-class copper gap).
3. Completion >= the measured floor.
4. Net-class honesty: every HV_HICUR connection that routes is emitted at
   its 2.6mm class width.

NOTE: this test runs the REAL whole-netset negotiation (minutes of wall
clock, local-only).  ``max_iterations`` is capped to keep it bounded; the
floor below is derived from the measured run at that cap.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest

from kicad_tools.router.lattice.geometry import seg_seg_dist
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules, NetClassRouting

_REPO = Path(__file__).resolve().parents[3]
_SOFTSTART_DIR = _REPO / "boards/external/softstart/output_revc"
_SOFTSTART = _SOFTSTART_DIR / "softstart_revc.kicad_pcb"
_SIDECAR = _SOFTSTART_DIR / "net_class_map.json"

# Measured floors -- pinned from the 2026-07-17 #4271 P4 measurement
# (deterministic negotiation; measured 267/287 connections and 63/79 nets
# fully connected at max_iterations=2, declines {no-path: 13,
# pad-escape-end: 7}).  Floors sit below the measurement for stability but
# above the epic acceptance floor (>= 40/79 nets), so a regression to the
# pre-#4271 behavior (or below the epic floor) fails loudly.
_CONNECTION_FLOOR = 255
_NET_FLOOR = 55

pytestmark = pytest.mark.skipif(
    not _SOFTSTART.exists(), reason="local-only softstart fixture absent"
)


def _load_connections() -> tuple[list, dict[int, str], dict[str, NetClassRouting]]:
    """The dispatch topology (core.py ``_negotiate_lattice_netset``) with the
    net-class sidecar resolved exactly like the CLI (issue #4149 rekeying)."""
    from kicad_tools.router.io import load_pads_for_analysis
    from kicad_tools.router.net_names import resolve_net_class_map_keys

    text = _SOFTSTART.read_text()
    pads = load_pads_for_analysis(text)

    fields = NetClassRouting.__dataclass_fields__
    raw = json.loads(_SIDECAR.read_text())
    loaded = {
        key: NetClassRouting(**{f: v for f, v in entry.items() if f in fields})
        for key, entry in raw.items()
    }
    board_net_names = sorted({p.net_name for p in pads if p.net > 0})
    resolution = resolve_net_class_map_keys(loaded.keys(), board_net_names)
    class_by_name = {bn: loaded[uk] for bn, uk in resolution.resolved.items()}

    by_net: dict[int, list[Pad]] = defaultdict(list)
    name_by_net: dict[int, str] = {}
    for p in pads:
        if p.net > 0:
            by_net[p.net].append(p)
            name_by_net[p.net] = p.net_name

    conns = []
    for net, ps in by_net.items():
        if len(ps) < 2:
            continue
        anchor = ps[0]
        nc = class_by_name.get(name_by_net[net])
        for seq, other in enumerate(ps[1:]):
            conns.append(((net, seq), anchor, other, nc))
    return conns, name_by_net, class_by_name


def test_softstart_lattice_routing_proof() -> None:
    conns, name_by_net, class_by_name = _load_connections()
    assert len(conns) == 287, "anchor-star topology of the rev-C fixture"

    pf = LatticePathfinder.from_board(
        _SOFTSTART.read_text(),
        DesignRules(),
        layer_stack=LayerStack.four_layer_all_signal(),
    )
    routes, stats = pf.route_netset(conns, max_iterations=2)

    # Measurement visibility (pytest -s): the honest census.
    from collections import Counter

    print(
        f"\n[softstart proof] connections {stats.routed}/{stats.total} "
        f"iterations={stats.iterations} converged={stats.converged} "
        f"declines={dict(Counter(pf.failure_reasons.values()))}"
    )

    # 1. Static substrate at scale.
    assert stats.lattice_builds == 1

    # 3. Completion floor (measured; every shortfall is a decline+reason).
    assert stats.routed >= _CONNECTION_FLOOR, (
        f"connections {stats.routed}/{stats.total} below the measured floor; "
        f"declines: {dict(list(pf.failure_reasons.items())[:10])}..."
    )
    assert len(pf.failure_reasons) == stats.total - stats.routed

    # Net-level completion.
    keys_by_net: dict[int, list] = defaultdict(list)
    for key, *_ in conns:
        keys_by_net[key[0]].append(key)
    full = [net for net, keys in keys_by_net.items() if all(k in routes for k in keys)]
    print(f"[softstart proof] nets fully connected: {len(full)}/{len(keys_by_net)}")
    assert len(full) >= _NET_FLOOR, f"nets fully connected {len(full)} below floor"

    # 4. Net-class honesty: routed HV_HICUR connections carry 2.6mm copper.
    hv_checked = 0
    for key, route in routes.items():
        nc = class_by_name.get(name_by_net[key[0]])
        if nc is not None and nc.name == "HV_HICUR":
            hv_checked += 1
            assert all(abs(s.width - nc.trace_width) < 1e-9 for s in route.segments)

    # 2. Zero cross-net short: pairwise per-class copper gap on same layer.
    flat: list[tuple[int, object, tuple, tuple, float, float]] = []
    rules = pf.rules
    for key, route in routes.items():
        nc = class_by_name.get(name_by_net[key[0]])
        clr = max(getattr(nc, "clearance", 0.0) or 0.0, rules.trace_clearance)
        for seg in route.segments:
            flat.append(
                (route.net, seg.layer, (seg.x1, seg.y1), (seg.x2, seg.y2), seg.width / 2, clr)
            )
    # Bucket by layer to keep the pairwise check tractable.
    by_layer: dict[object, list] = defaultdict(list)
    for item in flat:
        by_layer[item[1]].append(item)
    for items in by_layer.values():
        for i in range(len(items)):
            n1, _l1, p1, q1, h1, c1 = items[i]
            for j in range(i + 1, len(items)):
                n2, _l2, p2, q2, h2, c2 = items[j]
                if n1 == n2:
                    continue
                # Cheap bbox prefilter.
                gap = h1 + h2 + max(c1, c2)
                if (
                    min(p1[0], q1[0]) > max(p2[0], q2[0]) + gap
                    or min(p2[0], q2[0]) > max(p1[0], q1[0]) + gap
                    or min(p1[1], q1[1]) > max(p2[1], q2[1]) + gap
                    or min(p2[1], q2[1]) > max(p1[1], q1[1]) + gap
                ):
                    continue
                d = seg_seg_dist(p1, q1, p2, q2)
                assert d >= gap - 1e-6, f"nets {n1}/{n2} copper {d:.4f}mm apart (< {gap:.4f}mm)"
