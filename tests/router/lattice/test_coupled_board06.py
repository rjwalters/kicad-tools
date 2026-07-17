"""Board-06 diff-pair coupled-routing witness on the lattice engine (#4270).

Negotiates the real ``boards/06-diffpair-test`` signal net set through the
lattice engine with the production pair grouping
(:meth:`Autorouter._lattice_coupled_connections` -- detection, engagement,
endpoint pairing, impedance-solved pitch + fab floor) and asserts the
honest v1 acceptance measurements:

* >= 5 of the 9 detected pairs route COUPLED (the measured v1 result:
  USB2_D, PCIE_TX/RX, MIPI_CLK/D0 couple; the four USB3 pairs decline
  honestly -- the U2 BGA GND guard ring's 0.82 mm gap cannot pass the
  0.402 mm-pitch pair envelope planar, see the issue's decline census);
* every non-coupled pair carries a decline reason (never silently split);
* every coupled pair: 0 intra-pair clearance violations at the CLASS
  threshold, coupled body at EXACTLY the emitted pitch, via-free legs;
* ``lattice_builds == 1`` across the whole negotiation.

Power/pour nets are excluded from this witness (the board serves them via
zones, exactly like the committed grid output); the full-board CLI run is
the same measurement with GND star routing on top and is exercised
manually (see PR #4270 test plan).
"""

from __future__ import annotations

import json
import math
import warnings
from pathlib import Path

import pytest

from kicad_tools.router.lattice.geometry import seg_seg_dist
from kicad_tools.router.rules import DesignRules

# Runtime-heavy full-board negotiation: excluded from the CI fast lane
# (`pytest -m "not slow"`); runs in the scheduled/full suites.
pytestmark = pytest.mark.slow

_REPO = Path(__file__).resolve().parents[3]
_BOARD = _REPO / "boards/06-diffpair-test/output/diffpair_test.kicad_pcb"
_CLASS_MAP = _REPO / "boards/06-diffpair-test/output/net_class_map.json"

_SIGNAL_ONLY = {"GND", "+3V3", "+1V8", "+1V2", "VBUS_USB"}

# The four USB3 pairs cannot couple planar through the U2 BGA guard ring
# at their impedance-solved pitch (honest v1 decline; see issue #4270).
_EXPECTED_COUPLED_FLOOR = 5


@pytest.fixture(scope="module")
def negotiated():
    from kicad_tools.router.io import load_pcb_for_routing
    from kicad_tools.router.rules import net_class_map_from_dict

    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, manufacturer="jlcpcb")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        router, _net_map = load_pcb_for_routing(
            str(_BOARD),
            rules=rules,
            use_pcb_rules=False,
            validate_drc=False,
            strategy="lattice",
            skip_nets=sorted(_SIGNAL_ONLY),
        )
    router.net_class_map.update(net_class_map_from_dict(json.loads(_CLASS_MAP.read_text())))

    pf = router._ensure_lattice_pathfinder()
    coupled, reserved = router._lattice_coupled_connections()

    # Signal-only star connections, mirroring _negotiate_lattice_netset's
    # reserved-aware topology (extras attach to the nearest endpoint).
    connections = []
    for net, pad_keys in router.nets.items():
        if net == 0:
            continue
        keyed = [(k, router.pads[k]) for k in pad_keys if k in router.pads]
        if len(keyed) < 2:
            continue
        res_keys = reserved.get(net)
        if res_keys:
            res_pads = [p for k, p in keyed if k in res_keys]
            extras = [p for k, p in keyed if k not in res_keys]
            for seq, other in enumerate(extras):
                anchor = min(res_pads, key=lambda rp: math.hypot(rp.x - other.x, rp.y - other.y))
                connections.append(((net, seq), anchor, other, None))
            continue
        pads = [p for _k, p in keyed]
        for seq, other in enumerate(pads[1:]):
            connections.append(((net, seq), pads[0], other, None))

    routes, stats = pf.route_netset(connections, coupled=coupled, max_iterations=12)
    return router, pf, coupled, routes, stats


def test_engaged_pair_census_and_coupled_floor(negotiated) -> None:
    router, pf, coupled, routes, stats = negotiated
    assert len(coupled) == 9, "board-06 must engage all 9 detected pairs"
    outcomes = {pc.pair_name: pf.pair_outcomes.get(pc.key, "not-attempted") for pc in coupled}
    n_coupled = sum(1 for v in outcomes.values() if v == "coupled")
    assert n_coupled >= _EXPECTED_COUPLED_FLOOR, f"coupled census regressed: {outcomes}"
    # Honest decline discipline: every shortfall names its reason and no
    # partial pair copper exists.
    for pc in coupled:
        outcome = outcomes[pc.pair_name]
        if outcome == "coupled":
            assert (pc.key, "P") in routes and (pc.key, "N") in routes
        else:
            assert outcome != "not-attempted"
            assert (pc.key, "P") not in routes and (pc.key, "N") not in routes
    assert stats.lattice_builds == 1


def test_coupled_pairs_meet_class_intra_floor_and_exact_pitch(negotiated) -> None:
    from kicad_tools.router.diffpair_routing import find_intra_pair_clearance_violations

    router, pf, coupled, routes, _stats = negotiated
    checked = 0
    for pc in coupled:
        if pf.pair_outcomes.get(pc.key) != "coupled":
            continue
        route_p = routes[(pc.key, "P")]
        route_n = routes[(pc.key, "N")]
        assert not route_p.vias and not route_n.vias, "v1 coupled runs are planar"
        # Engine-agnostic validator at the CLASS threshold (issue AC #2).
        threshold = pc.net_class.effective_intra_pair_clearance()
        violation = find_intra_pair_clearance_violations(
            route_p, route_n, threshold, pair_name=pc.pair_name
        )
        assert violation is None, f"{pc.pair_name}: {violation}"
        # The coupled body sits at exactly the emitted pitch.
        min_sep = min(
            seg_seg_dist((sp.x1, sp.y1), (sp.x2, sp.y2), (sn.x1, sn.y1), (sn.x2, sn.y2))
            for sp in route_p.segments
            for sn in route_n.segments
        )
        assert min_sep == pytest.approx(pc.pitch, abs=1e-3), pc.pair_name
        checked += 1
    assert checked >= _EXPECTED_COUPLED_FLOOR


def test_pitch_carries_solved_width_plus_fab_floor(negotiated) -> None:
    router, _pf, coupled, _routes, _stats = negotiated
    from kicad_tools.router.mfr_limits import get_mfr_limits

    floor = get_mfr_limits("jlcpcb").min_clearance
    by_name = {pc.pair_name: pc for pc in coupled}
    usb2 = by_name["USB2_D"]
    # Impedance-solved width 0.25 + max(intra 0.075, jlcpcb floor 0.127).
    assert usb2.pitch == pytest.approx(0.25 + max(0.075, floor))
    usb3 = by_name["USB3_TX1"]
    assert usb3.pitch == pytest.approx(0.275 + max(0.10, floor))
