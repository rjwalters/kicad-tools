"""Softstart rev-C lattice ROUTING proof (issue #4271, epic #4267 P4).

The substrate-size proof lives in ``test_softstart_memory.py``; this is the
routing half: the lattice engine negotiates the real 160x100mm 4-layer board
the grid cannot route at pad-exact fidelity (#4242), with the net-class
sidecar threaded (Phase A of #4271) so HV_HICUR copper is emitted AND spaced
at its true 2.6mm width.

The board is a local-only external fixture (``boards/external/softstart``
is a symlink that dangles in CI and fresh worktrees), so the whole module
skips cleanly when it is absent -- exactly like the memory test.  The
artifact is additionally pinned by content hash (``softstart_fixture``,
issue #4670): a drifted fixture skips with an explicit message instead of
failing the topology assert.

Assertions (re-pinned 2026-08-07 against softstart commit 7800b04, after
the NRST star-break rework -- softstart PR #26, R30/R31 -- grew the
anchor-star topology 287 -> 295 connections; issue #4670.  Original P4
verdict 2026-07-16, #4271):

1. ``lattice_builds == 1`` at 295-connection scale (static substrate).
2. Zero cross-net short in the emitted copper (the #3906 invariant checked
   pairwise at the per-class copper gap).
3. Completion >= the measured floor.
4. Net-class honesty: every HV_HICUR connection that routes carries its
   2.6mm class width on the widened lattice body; any narrower segment is a
   legal pad-escape neck at the DRU floor (the #4293 taper).

NOTE: this test runs the REAL whole-netset negotiation (minutes of wall
clock, local-only).  ``max_iterations`` is capped to keep it bounded; the
floor below is derived from the measured run at that cap.
"""

from __future__ import annotations

import json
from collections import defaultdict

import pytest

from kicad_tools.router.lattice.geometry import seg_seg_dist
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules, NetClassRouting

from .softstart_fixture import (
    SIDECAR as _SIDECAR,
)
from .softstart_fixture import (
    SOFTSTART_BOARD as _SOFTSTART,
)
from .softstart_fixture import (
    fixture_skip_reason,
)

# Measured floors -- re-pinned 2026-08-07 (#4670) against the 295-connection
# fixture (softstart commit 7800b04, post-PR-#26 NRST star-break rework):
# deterministic negotiation measured 288/295 connections and 78/84 nets
# fully connected at max_iterations=2, declines {pad-escape-end: 1,
# no-path: 6}.  History: the 2026-07-17 #4293 P4 re-measurement on the
# 287-connection board was 273/287 and 66/79 (floors 265/60), itself RAISED
# from the #4271 floors (255/55) when the oversize neck-down escape (#4293)
# converted 6 of the 7 pad-escape-end declines.  Floors sit below the
# current measurement for stability (same margins as the #4293 pin: 8
# connections, 6 nets) but above the historical reality, so a regression to
# the pre-#4293 or the epic floor (>= 40 nets) fails loudly.
_CONNECTION_FLOOR = 280
_NET_FLOOR = 72

# Skip when the local-only fixture is absent (CI, fresh worktrees) OR when it
# has drifted from the pinned content hash -- see softstart_fixture (#4670).
_SKIP_REASON = fixture_skip_reason()
pytestmark = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")


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
    assert len(conns) == 295, "anchor-star topology of the rev-C fixture (pinned, #4670)"

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

    # 4. Net-class honesty WITH the #4293 taper: routed HV_HICUR connections
    # carry 2.6mm copper on the widened lattice body, and any narrower segment
    # is a legal pad-escape neck (== the DRU neck floor, never an arbitrary
    # width).  Every routed HV connection must show the full class width
    # somewhere (the body is never all-neck).
    neck_w = pf._neck_width(class_by_name.get("HV_HICUR"))
    hv_checked = 0
    for key, route in routes.items():
        nc = class_by_name.get(name_by_net[key[0]])
        if nc is not None and nc.name == "HV_HICUR":
            hv_checked += 1
            widths = {round(s.width, 6) for s in route.segments}
            assert widths <= {round(nc.trace_width, 6), round(neck_w, 6)}, (
                f"HV net {key} has off-class widths {widths} "
                f"(expected only {nc.trace_width}mm body or {neck_w}mm neck)"
            )
            assert any(abs(s.width - nc.trace_width) < 1e-9 for s in route.segments), (
                f"HV net {key} widened body missing (all-neck emission)"
            )

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
