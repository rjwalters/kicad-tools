"""A polarity-swapped pair reports a real pose-decline reason (Issue #7769).

The centreline pose trunk has no crossover, so ``route_differential_pair``
never offers a swapped pair to it.  The ``[coupled-select]`` line used to show
the opaque ``pose=declined:n/a`` for that case.
"""

from __future__ import annotations

from kicad_tools.router.diffpair_routing import _POSE_DECLINE_POLARITY_SWAP, DiffPairRouter


def _select_line(pose_decline):
    return DiffPairRouter._coupled_select_line(None, {}, {}, None, None, pose_decline, 0.0)


def test_swapped_pair_decline_reason_is_not_na():
    line = _select_line(_POSE_DECLINE_POLARITY_SWAP)
    assert "n/a" not in line
    assert "polarity-swap" in line


def test_routing_a_swapped_pair_reports_the_swap_decline(monkeypatch, capsys):
    """Through ``route_differential_pair_coupled``: the pose trunk is never offered."""
    from kicad_tools.router.core import Autorouter
    from kicad_tools.router.rules import DesignRules, NetClassRouting
    from tests import test_coupled_candidate_selection as csel

    def swapped_router():
        rules = DesignRules(trace_width=0.2, trace_clearance=0.15, grid_resolution=0.1)
        nc = NetClassRouting(name="HS", coupled_routing=True)
        router = Autorouter(
            width=30.0, height=10.0, rules=rules, net_class_map={"USB_D+": nc, "USB_D-": nc}
        )
        # P/N rows are mirrored between the two components: polarity swap.
        for ref, x, yp, yn in (("U1", 5.0, 4.6, 5.4), ("J1", 25.0, 5.4, 4.6)):
            router.add_component(
                ref,
                [
                    {"number": "1", "x": x, "y": yp, "width": 0.4, "height": 0.4,
                     "net": csel.P_NET, "net_name": "USB_D+"},
                    {"number": "2", "x": x, "y": yn, "width": 0.4, "height": 0.4,
                     "net": csel.N_NET, "net_name": "USB_D-"},
                ],
            )  # fmt: skip
        return router

    monkeypatch.setattr(csel, "_router", swapped_router)
    *_, out = csel._run(monkeypatch, capsys, csel._tight(), csel._tight())
    line = next(ln for ln in out.splitlines() if "[coupled-select]" in ln)
    assert "polarity-swap" in line
    assert "n/a" not in line
