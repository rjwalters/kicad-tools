"""Opt-in diff-pair gap tightening post-pass (Issue #7344).

Two single-ended rails that keep their breakout pitch through a long trunk
miss the class's ``target_diff_impedance`` once ``ImpedanceRule`` judges the
coupled segments (board 07 MIPI_DAT1: 0.283 mm gap, 116 ohm vs 100 ohm).
``Autorouter.apply_diffpair_gap_tightening`` squeezes such a trunk to the
gap at which the rule's own coupled model hits the target, with 45-degree
jogs on both rails, and leaves anything it cannot prove legal untouched.

The impedance post-conditions here are checked with an independently built
``ImpedanceRule`` through its public ``check()`` -- the ``kct check`` path.
"""

from __future__ import annotations

import argparse
import copy
import math
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kicad_tools.core.geometry import segment_clearance
from kicad_tools.physics import CoupledLines, Stackup
from kicad_tools.router.connectivity_invariant import build_multi_pad_net_pads
from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair import detect_differential_pairs
from kicad_tools.router.grid import pad_segment_edge_clearance
from kicad_tools.router.layers import Layer
from kicad_tools.router.observability import validate_net_connectivity
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import NetClassRouting
from kicad_tools.validate.impedance_specs import derive_diff_impedance_specs
from kicad_tools.validate.rules.impedance import ImpedanceRule

STACKUP = Stackup.jlcpcb_4layer()
FLOOR = 0.100


def _seg(net, name, a, b, width, layer=Layer.F_CU):
    return Segment(a[0], a[1], b[0], b[1], width, layer, net=net, net_name=name)


def _class(target, tol, *, floor=FLOOR, name="HS"):
    return NetClassRouting(
        name=name,
        target_diff_impedance=target,
        impedance_tolerance_percent=tol,
        intra_pair_clearance=floor,
    )


def _board(p_name, n_name, nc, rails, pads, *, extra_pads=(), extra_classes=None):
    """Router with one pair: ``rails`` = {net_id: [(a, b, width), ...]}."""
    ar = Autorouter(width=40.0, height=30.0)
    names = {1: p_name, 2: n_name}
    ar.net_names = dict(names)
    ar.net_class_map = {p_name: nc, n_name: nc, **(extra_classes or {})}
    for ref, net, (x, y) in pads:
        ar.add_component(
            ref,
            [
                {
                    "number": "1",
                    "x": x,
                    "y": y,
                    "width": 0.3,
                    "height": 0.25,
                    "net": net,
                    "net_name": names.get(net, ""),
                }
            ],
        )
    for ref, net, name, (x, y), size in extra_pads:
        ar.net_names[net] = name
        ar.add_component(
            ref,
            [
                {
                    "number": "1",
                    "x": x,
                    "y": y,
                    "width": size,
                    "height": size,
                    "net": net,
                    "net_name": name,
                }
            ],
        )
    ar.routes = [
        Route(
            net=net,
            net_name=names[net],
            segments=[_seg(net, names[net], a, b, w) for a, b, w in segs],
        )
        for net, segs in rails.items()
    ]
    return ar


def _impedance_findings(ar):
    """``kct check``-style ImpedanceRule run over the router's copper."""
    rule = ImpedanceRule(
        specs=derive_diff_impedance_specs(ar.net_class_map),
        stackup=STACKUP,
        detected_pairs=detect_differential_pairs(ar.net_names),
    )
    segs = [
        SimpleNamespace(
            net_name=s.net_name,
            width=s.width,
            layer=s.layer.kicad_name,
            start=(s.x1, s.y1),
            end=(s.x2, s.y2),
        )
        for r in ar.routes
        for s in r.segments
    ]
    return rule.check(SimpleNamespace(segments=segs), None).violations


def _lengths(ar):
    out = {}
    for r in ar.routes:
        out[r.net] = out.get(r.net, 0.0) + sum(
            math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in r.segments
        )
    return out


def _skew(ar):
    lengths = _lengths(ar)
    return lengths[1] - lengths[2]


def _clearance_errors(ar, *, floor=FLOOR, foreign=0.2):
    """Same-layer copper closer than its required clearance (independent check)."""
    errors = []
    segs = [s for r in ar.routes for s in r.segments]
    for i, a in enumerate(segs):
        for b in segs[i + 1 :]:
            if a.net == b.net or a.layer != b.layer:
                continue
            gap = segment_clearance(
                a.x1, a.y1, a.x2, a.y2, a.width, b.x1, b.y1, b.x2, b.y2, b.width
            )
            need = floor if {a.net, b.net} == {1, 2} else foreign
            if gap + 1e-6 < need:
                errors.append((a, b, gap))
        for pad in ar.pads.values():
            if pad.net == a.net:
                continue
            gap = pad_segment_edge_clearance(pad, a.x1, a.y1, a.x2, a.y2, a.width / 2)
            need = floor if {a.net, pad.net} == {1, 2} else foreign
            if gap + 1e-6 < need:
                errors.append((a, pad, gap))
    return errors


def _connected(ar):
    info = validate_net_connectivity(ar.routes, build_multi_pad_net_pads(ar, {1, 2}))
    return {net: v["connected"] for net, v in info.items()}


def _trunk_gaps(ar, min_len=1.0):
    """Edge gaps of the judged (>= 1 mm, parallel) P segments to N."""
    p = [s for r in ar.routes if r.net == 1 for s in r.segments]
    n = [s for r in ar.routes if r.net == 2 for s in r.segments]
    gaps = []
    for a in p:
        la = math.hypot(a.x2 - a.x1, a.y2 - a.y1)
        if la < min_len:
            continue
        ux, uy = (a.x2 - a.x1) / la, (a.y2 - a.y1) / la
        for b in n:
            lb = math.hypot(b.x2 - b.x1, b.y2 - b.y1)
            if lb < min_len or abs(ux * (b.y2 - b.y1) - uy * (b.x2 - b.x1)) / lb > 1e-3:
                continue
            centre = abs((b.x1 - a.x1) * uy - (b.y1 - a.y1) * ux)
            gaps.append(centre - a.width / 2 - b.width / 2)
    return gaps


def _gap_for(width, zdiff):
    cl = CoupledLines(STACKUP)
    lo, hi = 0.005, 3.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if cl.edge_coupled_microstrip(width, mid, "F.Cu").zdiff < zdiff:
            lo = mid
        else:
            hi = mid
    return lo


def _assert_45(ar):
    for r in ar.routes:
        for s in r.segments:
            dx, dy = round(s.x2 - s.x1, 4), round(s.y2 - s.y1, 4)
            assert dx == 0 or dy == 0 or abs(abs(dx) - abs(dy)) < 2e-4, (dx, dy)


def _snapshot(ar):
    return [copy.deepcopy(r.segments) for r in ar.routes]


# --- Board 07 MIPI_DAT1 shape --------------------------------------------

MIPI_W = 0.225


def _mipi_board(*, cc=0.508, nc=None, extra_pads=(), extra_classes=None):
    nc = nc or _class(100.0, 10.0)
    y_p, y_n = 10.0, round(10.0 + cc, 4)
    rails = {
        1: [((4.4, y_p), (5.0, y_p), MIPI_W), ((5.0, y_p), (20.0, y_p), MIPI_W)]
        + [((20.0, y_p), (20.6, y_p), MIPI_W)],
        2: [((4.4, y_n), (5.0, y_n), MIPI_W), ((5.0, y_n), (20.0, y_n), MIPI_W)]
        + [((20.0, y_n), (20.6, y_n), MIPI_W)],
    }
    pads = [
        ("U1", 1, (4.4, y_p)),
        ("U2", 2, (4.4, y_n)),
        ("J1", 1, (20.6, y_p)),
        ("J2", 2, (20.6, y_n)),
    ]
    return _board(
        "MIPI_DAT1_P",
        "MIPI_DAT1_N",
        nc,
        rails,
        pads,
        extra_pads=extra_pads,
        extra_classes=extra_classes,
    )


def test_mipi_dat1_shape_is_tightened_into_band():
    ar = _mipi_board()
    before = _impedance_findings(ar)
    assert before, "fixture must start out of band"
    skew_before = _skew(ar)
    band_max = _gap_for(MIPI_W, 110.0)

    results = ar.apply_diffpair_gap_tightening(verbose=False, stackup=STACKUP)

    res = results[("MIPI_DAT1_P", "MIPI_DAT1_N")]
    assert res.reason == "tightened", res
    assert res.target_gap_mm == pytest.approx(_gap_for(MIPI_W, 100.0), abs=1e-3)
    gaps = _trunk_gaps(ar)
    assert gaps
    for gap in gaps:
        assert FLOOR - 1e-6 <= gap <= band_max + 1e-6
    assert _impedance_findings(ar) == []
    assert abs(_skew(ar) - skew_before) <= 0.01
    assert _clearance_errors(ar) == []
    assert _connected(ar) == {1: True, 2: True}
    _assert_45(ar)


# --- Board 06 USB3_TX2 45-degree shape -----------------------------------

USB_W = 0.275


def _usb_board(*, gap=None):
    # 45-degree trunk, centre-to-centre 0.707 mm (perpendicular offset
    # (0.5, -0.5)); short (< 1 mm, unjudged) horizontal / vertical breakout
    # stubs to the pads.
    off = 0.5 if gap is None else round((gap + USB_W) / math.sqrt(2), 4)
    p0, p1 = (5.0, 5.0), (16.65, 16.65)
    n0, n1 = (5.0 + off, 5.0 - off), (16.65 + off, 16.65 - off)
    rails = {
        1: [((4.4, 5.0), p0, USB_W), (p0, p1, USB_W), (p1, (16.65, 17.25), USB_W)],
        2: [((n0[0], n0[1] - 0.6), n0, USB_W), (n0, n1, USB_W), (n1, (n1[0] + 0.6, n1[1]), USB_W)],
    }
    pads = [
        ("U1", 1, (4.4, 5.0)),
        ("U2", 2, (n0[0], n0[1] - 0.6)),
        ("J1", 1, (16.65, 17.25)),
        ("J2", 2, (n1[0] + 0.6, n1[1])),
    ]
    return _board("USB3_TX2+", "USB3_TX2-", _class(90.0, 15.0), rails, pads)


def test_usb3_tx2_45_degree_shape_is_tightened_into_band():
    ar = _usb_board()
    assert _impedance_findings(ar)
    skew_before = _skew(ar)
    band_max = _gap_for(USB_W, 90.0 * 1.15)

    results = ar.apply_diffpair_gap_tightening(verbose=False, stackup=STACKUP)

    assert results[("USB3_TX2+", "USB3_TX2-")].reason == "tightened"
    gaps = _trunk_gaps(ar)
    assert gaps
    for gap in gaps:
        assert FLOOR - 1e-6 <= gap <= band_max + 1e-6
    assert _impedance_findings(ar) == []
    assert abs(_skew(ar) - skew_before) <= 0.01
    assert _clearance_errors(ar) == []
    assert _connected(ar) == {1: True, 2: True}
    _assert_45(ar)


# --- No-op on an in-band pair (board 03 style, ~89 ohm of 90) -------------


def test_in_band_pair_is_left_unchanged():
    gap = round(_gap_for(USB_W, 89.0), 3)
    ar = _usb_board(gap=gap)
    assert _impedance_findings(ar) == []
    before = _snapshot(ar)

    results = ar.apply_diffpair_gap_tightening(verbose=False, stackup=STACKUP)

    assert results[("USB3_TX2+", "USB3_TX2-")].reason == "in_band"
    assert _snapshot(ar) == before


# --- Declined: a foreign pad sits where the squeezed copper must go -------


def test_foreign_pad_in_the_jog_path_declines_and_keeps_copper():
    gnd = NetClassRouting(name="GND", clearance=0.08)
    ar = _mipi_board(
        extra_pads=[("TP1", 3, "GND", (5.1, 10.254), 0.1)],
        extra_classes={"GND": gnd},
    )
    ar.rules.trace_clearance = 0.05
    before = _snapshot(ar)

    results = ar.apply_diffpair_gap_tightening(verbose=False, stackup=STACKUP)

    res = results[("MIPI_DAT1_P", "MIPI_DAT1_N")]
    assert res.reason == "declined:clearance", res
    assert "declined:clearance" in res.format_line()
    assert _snapshot(ar) == before


# --- Window guard: a run that can only reach an out-of-band gap -----------


def test_run_that_cannot_reach_the_band_is_not_committed():
    # 0.9 mm gap (outside the 0.5 mm coupling window: unjudged today); the
    # class floor of 0.4 mm is itself out of band (122 ohm vs 100 +/-10 %),
    # so squeezing would create a newly judged out-of-band trunk.
    ar = _mipi_board(cc=0.9 + MIPI_W, nc=_class(100.0, 10.0, floor=0.4))
    assert _impedance_findings(ar) == []
    before = _snapshot(ar)

    results = ar.apply_diffpair_gap_tightening(verbose=False, stackup=STACKUP)

    res = results[("MIPI_DAT1_P", "MIPI_DAT1_N")]
    assert res.reason == "declined:band_unreachable", res
    assert _snapshot(ar) == before
    assert _impedance_findings(ar) == []


def test_pair_without_target_impedance_is_declined():
    ar = _mipi_board(nc=NetClassRouting(name="HS", intra_pair_clearance=FLOOR))
    before = _snapshot(ar)
    results = ar.apply_diffpair_gap_tightening(verbose=False, stackup=STACKUP)
    assert results[("MIPI_DAT1_P", "MIPI_DAT1_N")].reason == "declined:no_target_diff_impedance"
    assert _snapshot(ar) == before


# --- CLI wiring: off by default, byte-identical without the flag ----------


def test_flag_is_off_by_default_and_phase_is_a_noop():
    from kicad_tools.cli import route_cmd

    args = route_cmd._route_parser().parse_args(["board.kicad_pcb"])
    assert args.tighten_diffpair_gap is False

    router = MagicMock()
    router.routes = [object()]
    assert route_cmd._run_diffpair_gap_tightening_phase(router, args, quiet=True) is None
    router.apply_diffpair_gap_tightening.assert_not_called()


def test_flag_off_leaves_real_routes_identical():
    from kicad_tools.cli import route_cmd

    ar = _mipi_board()
    before = _snapshot(ar)
    args = argparse.Namespace(tighten_diffpair_gap=False)
    assert route_cmd._run_diffpair_gap_tightening_phase(ar, args, quiet=True) is None
    assert _snapshot(ar) == before


def test_flag_on_runs_the_pass_under_the_connectivity_guard():
    from unittest.mock import patch

    from kicad_tools.cli import route_cmd

    args = route_cmd._route_parser().parse_args(["board.kicad_pcb", "--tighten-diffpair-gap"])
    assert args.tighten_diffpair_gap is True
    router = MagicMock()
    router.routes = [object()]
    router.apply_diffpair_gap_tightening.return_value = {}
    with (
        patch.object(route_cmd, "_connectivity_snapshot", return_value=None),
        patch.object(route_cmd, "_enforce_connectivity_invariant_or_exit") as enforce,
    ):
        route_cmd._run_diffpair_gap_tightening_phase(router, args, quiet=True)
    router.apply_diffpair_gap_tightening.assert_called_once()
    assert enforce.call_args.kwargs["phase"] == "tighten_diffpair_gap"
