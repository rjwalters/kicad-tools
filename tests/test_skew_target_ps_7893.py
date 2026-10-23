"""Issue #7893: the router and its tuners tune to the derived ps skew budget.

Step 2 of the #7883 ruling.  ``kct check`` judges intra-pair skew in time
(#7892); these tests pin the router's side of the same rule:

* one resolver (:func:`~kicad_tools.router.pair_skew_target.resolve_pair_skew_target`)
  behind every skew-limit lookup, carrying the budget and the transitional
  error-level millimetre caps;
* the tuner measures progress in ps, so a split-layer pair that is matched
  in length but not in time is tuned until its *delay* skew meets the budget,
  and inner-layer length counts for more than the same outer-layer length;
* the tuned board passes the checker's own time-domain measurement.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.router import pair_postpass_invariant as ppi
from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair_detection import detect_diff_pairs
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.length import LengthTracker
from kicad_tools.router.pair_skew_target import (
    PairSkewTarget,
    resolve_pair_skew_target,
    skew_stackup,
)
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import NetClassRouting

P, N = "USB3_TX1+", "USB3_TX1-"
F, IN1 = Layer.F_CU, Layer.IN1_CU
LENGTH = 20.0


def _usb3(**kw) -> NetClassRouting:
    return NetClassRouting(name="USB3", pair_protocol="USB3_SS", **kw)


def _leg(net: int, name: str, layer: Layer, length: float, y: float) -> Route:
    return Route(
        net=net,
        net_name=name,
        segments=[Segment(0.0, y, length, y, 0.15, layer, net=net, net_name=name)],
    )


def _router(
    p_layer: Layer, n_layer: Layer, p_len: float, n_len: float, nc: NetClassRouting | None = None
) -> Autorouter:
    """A four-layer pair 1 mm apart (well clear of each other's bumps)."""
    ar = Autorouter(width=40.0, height=20.0, layer_stack=LayerStack.four_layer_sig_sig_gnd_pwr())
    ar.net_names = {1: P, 2: N}
    nc = nc or _usb3()
    ar.net_class_map = {P: nc, N: nc}
    ar.routes = [_leg(1, P, p_layer, p_len, 5.0), _leg(2, N, n_layer, n_len, 6.0)]
    return ar


def _tune(ar: Autorouter):
    pairs = detect_diff_pairs(net_names=ar.net_names)
    assert len(pairs) == 1
    return ar.apply_diffpair_length_tuning(pairs, verbose=False, engage="checker_skew_limit")[
        (P, N)
    ]


def _delays():
    return resolve_pair_skew_target({P: _usb3(), N: _usb3()}, P, N, skew_stackup(None, 4)).delays


# ---------------------------------------------------------------------------
# The single resolver
# ---------------------------------------------------------------------------


def test_target_is_the_derived_budget_with_the_error_caps():
    stackup = skew_stackup(None, 4)
    bare = resolve_pair_skew_target({P: _usb3(), N: _usb3()}, P, N, stackup)
    assert bare.budget_ps == pytest.approx(4.03, abs=0.01)  # USB 3 Gen 1 at Nyquist
    assert bare.budget_severity == "warning"
    assert bare.cap_mm is None  # the 0.127 mm protocol table value is superseded
    assert bare.enforced

    capped = resolve_pair_skew_target(
        {P: _usb3(skew_tolerance_mm=0.5), N: _usb3(skew_tolerance_mm=0.5)}, P, N, stackup
    )
    assert capped.cap_mm == pytest.approx(0.5)  # transitional class cap (#7894)
    assert capped.budget_ps == pytest.approx(bare.budget_ps)
    assert capped.tolerance_mm == pytest.approx(0.5)  # tighter than 4.03 ps at the slowest layer

    # A protocol only guessed from the name keeps the generic 0.5 mm as an error cap.
    guessed = resolve_pair_skew_target({}, P, N, stackup)
    assert guessed.cap_mm == pytest.approx(0.5)
    assert guessed.budget_ps == pytest.approx(bare.budget_ps)

    # No operating point and an uncited table value: nothing to tune to.
    eth = NetClassRouting(name="ETH", pair_protocol="ETHERNET")
    assert not resolve_pair_skew_target({P: eth, N: eth}, P, N, stackup).enforced


def test_target_meets_budget_and_caps_together():
    target = PairSkewTarget(P, N, budget_ps=4.0, cap_mm=0.5)
    assert target.within(3.9, 0.4)
    assert not target.within(4.1, 0.4)  # over the budget
    assert not target.within(3.9, 0.6)  # over the cap
    assert target.within(None, 0.4)  # no ps figure: only the cap can be judged


def test_millimetre_lookups_are_retired():
    """No second skew-limit lookup remains on the router side (the #7797 drift risk)."""
    assert not hasattr(ppi, "checker_skew_limit_mm")
    from kicad_tools.router.diffpair_routing import DiffPairRouter

    assert not hasattr(DiffPairRouter, "_checker_skew_limit_mm")
    root = Path(__file__).resolve().parents[1] / "src" / "kicad_tools"
    offenders = []
    for path in [*(root / "router").rglob("*.py"), root / "cli" / "route_cmd.py"]:
        for no, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if "effective_skew_tolerance(" in code and "def effective_skew_tolerance" not in code:
                if "``" not in code:
                    offenders.append(f"{path.name}:{no}")
            if ").skew_mm" in code or "thresholds.skew_mm" in code:
                offenders.append(f"{path.name}:{no}")
    assert offenders == []


# ---------------------------------------------------------------------------
# The tuner in ps
# ---------------------------------------------------------------------------


def test_split_layer_pair_matched_in_length_is_tuned_to_the_ps_budget():
    """P on F.Cu, N on In1.Cu, equal lengths: 0 mm skew, ~27 ps of delay skew."""
    ar = _router(F, IN1, LENGTH, LENGTH)
    delays = _delays()
    skew_ps = LENGTH * (delays.of("In1.Cu") - delays.of("F.Cu"))
    assert skew_ps > 20.0

    result = _tune(ar)

    assert result.skew_before_mm == pytest.approx(0.0, abs=1e-9)
    assert result.skew_before_ps == pytest.approx(skew_ps, rel=1e-6)
    assert result.reason == "tuned"
    assert result.budget_ps == pytest.approx(4.03, abs=0.01)
    assert result.skew_after_ps <= result.budget_ps
    # The fast (outer) leg got the copper: the pair is now "unmatched" in length.
    p_len = sum(LengthTracker.calculate_route_length(r) for r in ar.routes if r.net == 1)
    assert p_len > LENGTH + 3.0
    assert result.skew_after_mm == pytest.approx(p_len - LENGTH, abs=0.05)


def test_inner_layer_length_counts_for_more_delay():
    """0.62 mm of skew is inside 4.03 ps on F.Cu but over it on In1.Cu."""
    outer = _tune(_router(F, F, LENGTH, LENGTH + 0.62))
    assert outer.reason == "already_within_tolerance"
    assert outer.skew_before_ps < 4.03

    inner = _tune(_router(IN1, IN1, LENGTH, LENGTH + 0.62))
    assert inner.skew_before_ps > 4.03
    assert inner.reason == "tuned"
    assert inner.skew_after_ps <= inner.budget_ps


def test_error_cap_still_binds_beside_the_budget():
    """A transitional 0.05 mm class cap is met as well as the budget."""
    ar = _router(F, F, LENGTH, LENGTH + 0.4, _usb3(skew_tolerance_mm=0.05))
    result = _tune(ar)
    assert result.skew_before_ps < 4.03  # the budget alone would not tune it
    assert result.reason == "tuned"
    assert result.skew_after_mm <= 0.05 + 1e-6


def test_error_cap_is_never_traded_for_ps_on_a_split_layer_pair():
    """The delay-shorter leg is the LONGER one in mm (board 06 MIPI_CLK shape).

    Closing the ~25 ps of delay skew would grow the length skew far past the
    0.5 mm class cap, an error, while the budget is only a warning: the tuner
    stops at the cap instead.
    """
    nc = _usb3(skew_tolerance_mm=0.5)
    ar = _router(F, IN1, LENGTH + 0.3, LENGTH, nc)
    result = _tune(ar)
    assert result.skew_before_ps > 20.0
    assert result.skew_before_mm == pytest.approx(0.3, abs=1e-6)
    assert result.reason != "tuned"
    assert result.skew_after_mm <= 0.5 + 1e-6
    assert result.skew_after_ps <= result.skew_before_ps


def test_violated_error_cap_is_fixed_first_when_time_and_length_disagree():
    """Board 06 MIPI_CLK at the end-of-recipe tuner: 1.29 mm over a 0.3 mm cap.

    The delay-shorter leg is the longer one in mm, so no bump can serve both.
    The cap is an error, the budget a warning: the length skew is fixed.
    """
    nc = _usb3(skew_tolerance_mm=0.3)
    ar = _router(F, IN1, LENGTH + 1.3, LENGTH, nc)
    result = _tune(ar)
    assert result.skew_before_mm == pytest.approx(1.3, abs=1e-6)
    assert result.reason == "tuned"
    assert result.skew_after_mm <= 0.3 + 1e-6


def test_tuned_split_layer_board_passes_kct_check_in_time(tmp_path):
    """Written to a board, the tuned pair passes the checker's own ps measurement."""
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.diffpair_skew import derive_pair_skews

    ar = _router(F, IN1, LENGTH, LENGTH)
    assert _tune(ar).reason == "tuned"
    board = tmp_path / "pair.kicad_pcb"
    board.write_text(_board_text(ar))
    pcb = PCB.load(str(board))
    ncm = ar.net_class_map
    [m] = derive_pair_skews(pcb, ncm, num_copper_layers=4)
    assert m.limits.budget is not None
    assert m.skew_ps is not None
    assert m.skew_ps <= m.limits.budget.budget_ps + 1e-3


def _board_text(ar: Autorouter) -> str:
    segs = []
    for route in ar.routes:
        for s in route.segments:
            segs.append(
                f"  (segment (start {s.x1:.4f} {s.y1:.4f}) (end {s.x2:.4f} {s.y2:.4f}) "
                f'(width {s.width}) (layer "{s.layer.kicad_name}") (net {route.net}))'
            )
    layers = "\n".join(
        f'    ({i} "{n}" signal)'
        for i, n in ((0, "F.Cu"), (1, "In1.Cu"), (2, "In2.Cu"), (31, "B.Cu"))
    )
    return (
        "(kicad_pcb (version 20240108) (generator test)\n"
        "  (general (thickness 1.6))\n"
        f"  (layers\n{layers}\n  )\n"
        '  (net 0 "")\n'
        f'  (net 1 "{P}")\n'
        f'  (net 2 "{N}")\n' + "\n".join(segs) + "\n)\n"
    )
