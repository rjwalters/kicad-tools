"""Unequal diff-pair via counts are a reported pair-rule failure (issue #7093).

The router keeps a pair's P/N via counts equal where it can.  Where it cannot,
the committed copper must be reported, on every engine and every path:

* the lattice's per-segment report reads each segment's COMMITTED legs
  (``segment_via_counts``), so a #6446 hybrid segment is covered and a
  discarded retry's fallback record cannot hide or invent a mismatch;
* ``Autorouter._finalize_routing`` audits every engaged pair's committed
  routes (``_audit_pair_via_parity``) -- the backstop for the grid engine's
  independent fallback and for pairs deferred to the single-ended main loop,
  which had no via-count report at all.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.core import (
    Autorouter,
    pair_via_mismatch_warnings,
    segment_via_counts,
)
from kicad_tools.router.lattice.coupled import CoupledConnection
from kicad_tools.router.layers import Layer
from kicad_tools.router.pair_via_parity import (
    PairViaParityFailure,
    audit_pair_via_parity,
    net_via_counts,
)
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import NetClassRouting

_USB = NetClassRouting(name="USB", coupled_routing=True)


def _route(net: int, name: str, vias: int, *, x0: float = 0.0) -> Route:
    """A short route on ``net`` carrying ``vias`` through vias."""
    return Route(
        net=net,
        net_name=name,
        segments=[Segment(x0, 0.0, x0 + 5.0, 0.0, 0.2, Layer.F_CU, net=net, net_name=name)],
        vias=[
            Via(
                x=x0 + i,
                y=0.0,
                drill=0.3,
                diameter=0.6,
                layers=(Layer.F_CU, Layer.B_CU),
                net=net,
                net_name=name,
            )
            for i in range(vias)
        ],
    )


# -- pure audit ---------------------------------------------------------------


def test_audit_reports_unequal_counts_summed_over_every_route_of_a_net() -> None:
    routes = [_route(1, "A+", 1), _route(1, "A+", 1, x0=10.0), _route(2, "A-", 1)]
    assert net_via_counts(routes) == {1: 2, 2: 1}
    assert audit_pair_via_parity(routes, [(1, "A+", 2, "A-")]) == [
        PairViaParityFailure("A+", "A-", 2, 1)
    ]


def test_audit_is_silent_on_equal_counts() -> None:
    routes = [_route(1, "A+", 2), _route(2, "A-", 2)]
    assert audit_pair_via_parity(routes, [(1, "A+", 2, "A-")]) == []


def test_audit_skips_a_pair_with_an_unrouted_net() -> None:
    # An open net is reported as an open; its zero via count says nothing.
    assert audit_pair_via_parity([_route(1, "A+", 1)], [(1, "A+", 2, "A-")]) == []


def test_failure_warning_names_both_nets_and_counts() -> None:
    line = PairViaParityFailure("USB_D+", "USB_D-", 2, 3).warning()
    assert "WARNING: diff pair USB_D+/USB_D- committed with unequal via counts" in line
    assert "(USB_D+: 2, USB_D-: 3)" in line
    assert "pair-rule failure" in line


# -- lattice per-segment report reads the committed legs ----------------------


def _pad(x: float, y: float, net: int, name: str, ref: str) -> Pad:
    return Pad(
        x=x, y=y, width=0.6, height=0.6, net=net, net_name=name, layer=Layer.F_CU, ref=ref, pin="1"
    )


def _segment(idx: int, a: str, b: str) -> CoupledConnection:
    return CoupledConnection(
        key=("pair", 20, 21, idx),
        pair_name="USB_D",
        pad_p_a=_pad(3.0, 5.0, 20, "USB_D+", a),
        pad_n_a=_pad(3.0, 6.0, 21, "USB_D-", a),
        pad_p_b=_pad(27.0, 5.0, 20, "USB_D+", b),
        pad_n_b=_pad(27.0, 6.0, 21, "USB_D-", b),
        net_class=_USB,
        pitch=0.4,
    )


def test_segment_via_counts_read_each_segments_committed_legs() -> None:
    segs = [_segment(0, "J1", "U2"), _segment(1, "U2", "U1")]
    routes = {
        (segs[0].key, "P"): _route(20, "USB_D+", 0),
        (segs[0].key, "N"): _route(21, "USB_D-", 1),
        (segs[1].key, "P"): _route(20, "USB_D+", 0),
        # segment 1's N leg is missing: no counts for it
    }
    assert segment_via_counts(segs, routes) == {segs[0].key: (0, 1)}


def test_a_stale_fallback_record_cannot_hide_or_invent_a_mismatch() -> None:
    # A discarded tap-chain / gateway retry re-flushed the pair, so the
    # pathfinder's ``pair_via_mismatch`` record described copper that was
    # thrown away.  The report now follows the committed legs only.
    seg = _segment(0, "J1", "U2")
    outcomes = {seg.key: "uncoupled-fallback:pair-no-path"}
    equal = {(seg.key, "P"): _route(20, "USB_D+", 1), (seg.key, "N"): _route(21, "USB_D-", 1)}
    assert pair_via_mismatch_warnings([seg], outcomes, segment_via_counts([seg], equal)) == []
    unequal = {(seg.key, "P"): _route(20, "USB_D+", 0), (seg.key, "N"): _route(21, "USB_D-", 1)}
    lines = pair_via_mismatch_warnings([seg], outcomes, segment_via_counts([seg], unequal))
    assert len(lines) == 1 and "(P=0, N=1)" in lines[0]
    assert "no fallback candidate had via parity" in lines[0]


# -- the engine-agnostic audit in _finalize_routing ---------------------------


def _usb_router(engaged: bool = True) -> Autorouter:
    ar = Autorouter(width=20.0, height=20.0)
    for ref, x in (("J1", 2.0), ("U1", 18.0)):
        ar.add_component(
            ref,
            [
                {"number": "1", "x": x, "y": 10.0, "width": 0.5, "height": 0.5, "net": 1,
                 "net_name": "USB_D+"},
                {"number": "2", "x": x, "y": 12.0, "width": 0.5, "height": 0.5, "net": 2,
                 "net_name": "USB_D-"},
            ],
        )  # fmt: skip
    if engaged:
        ar.net_class_map = {"USB_D+": _USB, "USB_D-": _USB}
    return ar


def test_finalize_reports_an_engaged_pair_committed_with_unequal_vias(capsys) -> None:
    # The grid engine's independent fallback (``route_differential_pair_
    # independent``) and a pair deferred to the main loop route P and N as
    # unrelated nets; nothing compared their via counts before #7093.
    ar = _usb_router()
    ar.routes = [_route(1, "USB_D+", 2), _route(2, "USB_D-", 3)]
    ar._finalize_routing()
    assert ar.pair_via_parity_failures == [PairViaParityFailure("USB_D+", "USB_D-", 2, 3)]
    out = capsys.readouterr().out
    assert out.count("committed with unequal via counts (USB_D+: 2, USB_D-: 3)") == 1
    # Repeated finalisation (several ``route_all_*`` tails) prints it once.
    ar._finalize_routing()
    assert ar.pair_via_parity_failures
    assert "unequal via counts" not in capsys.readouterr().out


def test_finalize_is_silent_when_the_counts_match(capsys) -> None:
    ar = _usb_router()
    ar.routes = [_route(1, "USB_D+", 2), _route(2, "USB_D-", 2)]
    ar._finalize_routing()
    assert ar.pair_via_parity_failures == []
    assert "unequal via counts" not in capsys.readouterr().out


def test_finalize_ignores_a_pair_no_net_class_engages(capsys) -> None:
    # Like ``kct check``: a name-detected pair with no coupled-routing class
    # is routed single-ended by design and carries no pair rule.
    ar = _usb_router(engaged=False)
    ar.routes = [_route(1, "USB_D+", 0), _route(2, "USB_D-", 1)]
    ar._finalize_routing()
    assert ar.pair_via_parity_failures == []
    assert "unequal via counts" not in capsys.readouterr().out


def test_finalize_records_but_does_not_repeat_a_lattice_segment_warning(capsys) -> None:
    ar = _usb_router()
    ar.routes = [_route(1, "USB_D+", 0), _route(2, "USB_D-", 1)]
    ar._lattice_via_warned_nets = {frozenset((1, 2))}
    ar._finalize_routing()
    assert ar.pair_via_parity_failures == [PairViaParityFailure("USB_D+", "USB_D-", 0, 1)]
    assert "unequal via counts" not in capsys.readouterr().out


@pytest.mark.parametrize("strategy", ["route_all", "route_all_negotiated"])
def test_every_route_all_tail_runs_the_audit(strategy: str) -> None:
    ar = _usb_router()
    ar.pair_via_parity_failures = [PairViaParityFailure("x", "y", 0, 1)]  # sentinel
    run = getattr(ar, strategy)
    run(max_iterations=2) if strategy == "route_all_negotiated" else run()
    # The audit replaced the sentinel with the real (equal, planar) result.
    assert all(f.p_name != "x" for f in ar.pair_via_parity_failures)
