"""Pairwise-matrix per-round cost work (issue #7881).

The hot lattice predicates read the pair requirement through a per-net
partner row (``LatticePairwise.row``) and ``node_clear`` gained the exact
bbox lower-bound skip ``seg_clear`` already had (#6182).  Neither may change
a verdict: these tests compare the optimized predicates against a brute-force
reference that calls ``required`` per obstacle and measures every kernel gap.
"""

from __future__ import annotations

import random
from collections.abc import Iterator, Mapping

from kicad_tools.router.lattice import kernel_adapter as ka
from kicad_tools.router.lattice.obstacles import CommittedCopper, LatticeObstacleModel
from kicad_tools.router.lattice.pairwise import LatticePairwise
from kicad_tools.router.lattice.quadtree import OctilinearLattice
from kicad_tools.router.primitives import Pad

HALF = 0.1
CLR = 0.2
VIA_R = 0.3
REQ = {(1, 2): 3.2, (1, 3): 1.5, (2, 4): 0.9}


def _pairwise() -> LatticePairwise:
    return LatticePairwise(
        required_by_pair=REQ,
        zones=(),
        max_by_net={1: 3.2, 2: 3.2, 3: 1.5, 4: 0.9},
    )


# A #4506 attach zone covering nets 1 and 2 over part of the board, so the
# equivalence runs also exercise the exemption branches.
ZONE = (8.0, 8.0, 16.0, 16.0, frozenset({1, 2}), None)


class _RequiredRow(Mapping[int, float]):
    """A partner row that answers every lookup through ``required`` (pre-#7881)."""

    def __init__(self, pw: LatticePairwise, net: int) -> None:
        self._pw, self._net = pw, net

    def __getitem__(self, other: int) -> float:
        return self._pw.required(self._net, other)

    def __iter__(self) -> Iterator[int]:
        return iter(())

    def __len__(self) -> int:
        return 0


class _PerPairPairwise(LatticePairwise):
    """Reference: the pre-#7881 per-obstacle ``required(net, other)`` lookup."""

    def row(self, net: int) -> Mapping[int, float]:
        return _RequiredRow(self, net)


def _populated(cls: type[LatticePairwise] = LatticePairwise) -> LatticePairwise:
    return cls(
        required_by_pair=REQ,
        zones=(ZONE,),
        max_by_net={1: 3.2, 2: 3.2, 3: 1.5, 4: 0.9},
    )


def _copper(pw: LatticePairwise | None, seed: int) -> CommittedCopper:
    rng = random.Random(seed)
    cc = CommittedCopper(
        2,
        trace_half=HALF,
        clearance=CLR,
        via_radius=VIA_R,
        via_via_gap=0.8,
        same_net_via_gap=0.7,
        pairwise=pw,
    )
    for _ in range(120):
        a = (rng.uniform(0, 30), rng.uniform(0, 30))
        b = (a[0] + rng.uniform(-4, 4), a[1] + rng.uniform(-4, 4))
        cc.add_run(rng.randrange(2), [a, b], rng.randrange(1, 6), HALF, CLR)
    for _ in range(15):
        cc.add_via((rng.uniform(0, 30), rng.uniform(0, 30)), rng.randrange(1, 6))
    return cc


def _ref_node_clear(cc: CommittedCopper, point, layer, net) -> bool:
    pw = cc.pairwise
    probe = ka.LatticeProbe(ka.site(point, HALF), net)
    for c, d, cnet, hw, iclr in cc.copper[layer].query_seg(point, point, pad=50.0):
        if not probe.foreign(cnet):
            continue
        gap = probe.gap(ka.trace(c, d, hw, layer))
        if not ka.satisfies(gap, max(CLR, iclr)):
            return False
        if pw is not None:
            req = pw.required(net, cnet)
            if req > max(CLR, iclr) and not ka.satisfies(gap, req):
                return False
    for vpt, vnet, vclr in cc.vias:
        if not probe.foreign(vnet):
            continue
        gap = probe.gap(ka.site(vpt, VIA_R))
        if not ka.satisfies(gap, max(CLR, vclr)):
            return False
        if pw is not None:
            req = pw.required(net, vnet)
            if req > max(CLR, vclr) and not ka.satisfies(gap, req):
                return False
    return True


def test_row_matches_required():
    pw = _pairwise()
    for a in range(0, 7):
        for b in range(0, 7):
            assert pw.row(a).get(b, 0.0) == pw.required(a, b)
    assert pw.row(99) == {}


def test_node_clear_verdicts_unchanged_by_bbox_skip():
    rng = random.Random(7881)
    seen = {True: 0, False: 0}
    for seed in range(4):
        cc = _copper(_pairwise(), seed)
        for _ in range(250):
            pt = (rng.uniform(-2, 32), rng.uniform(-2, 32))
            layer, net = rng.randrange(2), rng.randrange(1, 6)
            want = _ref_node_clear(cc, pt, layer, net)
            assert cc.node_clear(pt, layer, net) is want
            seen[want] += 1
    assert seen[True] and seen[False]


def test_predicates_match_per_pair_required_with_populated_map():
    """Row lookups give the same seg/node/via verdicts as per-pair ``required``."""
    rng = random.Random(78810)
    seen = {True: 0, False: 0}
    for seed in range(3):
        new, old = _copper(_populated(), seed), _copper(_populated(_PerPairPairwise), seed)
        for _ in range(200):
            a = (rng.uniform(-2, 32), rng.uniform(-2, 32))
            b = (a[0] + rng.uniform(-3, 3), a[1] + rng.uniform(-3, 3))
            layer, net = rng.randrange(2), rng.randrange(1, 6)
            want = old.seg_clear(a, b, layer, net)
            assert new.seg_clear(a, b, layer, net) is want
            seen[want] += 1
            assert new.node_clear(a, layer, net) is old.node_clear(a, layer, net)
            assert new.via_clear(a, net) is old.via_clear(a, net)
    assert seen[True] and seen[False]


def test_pad_check_matches_per_pair_required_with_populated_map():
    rng = random.Random(78811)
    pads = [
        Pad(
            x=rng.uniform(2, 28),
            y=rng.uniform(2, 28),
            width=rng.uniform(0.5, 2.0),
            height=rng.uniform(0.5, 2.0),
            net=rng.randrange(1, 6),
            net_name="",
        )
        for _ in range(40)
    ]
    model = LatticeObstacleModel(
        OctilinearLattice((0.0, 0.0, 30.0, 30.0), []),
        pads,
        [(0,)] * len(pads),
        1,
        agent_radius=HALF + CLR,
    )
    new, old = _populated(), _populated(_PerPairPairwise)
    seen = {True: 0, False: 0}
    for _ in range(400):
        a = (rng.uniform(0, 30), rng.uniform(0, 30))
        b = (a[0] + rng.uniform(-3, 3), a[1] + rng.uniform(-3, 3))
        layer = rng.choice((0, None))
        net = rng.randrange(1, 6)
        want = model.pairwise_pad_blocked(a, b, layer, net, HALF, 0.0, old)
        assert model.pairwise_pad_blocked(a, b, layer, net, HALF, 0.0, new) is want
        seen[want] += 1
    assert seen[True] and seen[False]


def test_seg_clear_without_pairwise_matches_with_empty_matrix():
    empty = LatticePairwise(required_by_pair={}, zones=(), max_by_net={})
    rng = random.Random(3)
    off, on = _copper(None, 5), _copper(empty, 5)
    for _ in range(200):
        a = (rng.uniform(0, 30), rng.uniform(0, 30))
        b = (a[0] + rng.uniform(-3, 3), a[1] + rng.uniform(-3, 3))
        layer, net = rng.randrange(2), rng.randrange(1, 6)
        assert off.seg_clear(a, b, layer, net) == on.seg_clear(a, b, layer, net)


def test_expired_deadline_reports_rounds_with_matrix_active(tmp_path, capsys):
    """A real lattice run with a voltage map hits its deadline and says so."""
    import time

    from kicad_tools.router.io import load_pcb_for_routing
    from kicad_tools.router.pairwise_clearance import build_pairwise_clearance_table
    from tests.test_cli_route_complete_4471 import STRANDED_BOARD

    pcb = tmp_path / "stranded.kicad_pcb"
    pcb.write_text(STRANDED_BOARD)
    router, _ = load_pcb_for_routing(
        pcb, strategy="lattice", lattice_max_iterations=5, force_python=True
    )
    # Matrix active: NET1 is the 300 V net, NET2 the logic net.
    router.rules.pairwise_clearance = build_pairwise_clearance_table(
        {"NET1": 300.0, "NET2": 0.0}, dru=router.rules.trace_clearance
    )
    assert router._lattice_pairwise_projection() is not None
    router._lattice_deadline = time.monotonic() - 1.0  # already expired

    router._negotiate_lattice_netset()

    out = capsys.readouterr().out
    assert "Lattice negotiation:" in out and "converged=False" in out
    assert "stopped by: deadline after " in out and "/5 round(s)" in out
    assert out.count("stopped by: deadline after ") == 1
