"""Tests for the Issue #3439 aggregate coupled-phase budget cap.

Background: ``per_pair_timeout`` (issue #3089/#3321) bounds any single
``CoupledPathfinder.route_coupled`` call, but a board with many
pathological pairs still burns ``num_pairs * per_pair_timeout`` of the
outer ``--timeout`` budget before the single-ended main strategy runs.
On board 07 (7 declared pairs, every one blowing its 60 s budget) the
coupled pre-pass consumed 420 s of the 600 s ``--timeout``, starving
the negotiated strategy and collapsing final reach to 7/31 nets.

Issue #3439 adds an AGGREGATE coupled-phase budget:

1. ``DifferentialPairConfig.aggregate_timeout`` (explicit config).
2. ``DiffPairRouter.route_all_with_diffpairs(aggregate_timeout=...)``
   (kwarg, takes precedence over config).
3. ``Autorouter.route_all_with_diffpairs`` auto-derives
   ``max(per_pair_budget, timeout * 0.25)`` from ``--timeout`` when the
   config does not set one.

The load-bearing invariant (pinned below): once the aggregate budget is
exhausted, remaining pairs are deferred to the main strategy WITHOUT
attempting coupled routing, so a failed coupled pre-pass can never
reduce the final reach below the ``--differential-pairs``-off baseline.
"""

from __future__ import annotations

import inspect
from dataclasses import fields
from unittest.mock import MagicMock

from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair import DifferentialPairConfig
from kicad_tools.router.diffpair_routing import DiffPairRouter
from kicad_tools.router.rules import DesignRules, NetClassRouting

# ---------------------------------------------------------------------------
# Config / signature surface
# ---------------------------------------------------------------------------


def test_diffpair_config_has_aggregate_timeout_field():
    field_names = {f.name for f in fields(DifferentialPairConfig)}
    assert "aggregate_timeout" in field_names


def test_diffpair_config_aggregate_timeout_default_none():
    """Back-compat: legacy per-pair-only behaviour by default."""
    assert DifferentialPairConfig().aggregate_timeout is None


def test_route_all_with_diffpairs_signature_accepts_aggregate_timeout():
    sig = inspect.signature(DiffPairRouter.route_all_with_diffpairs)
    assert "aggregate_timeout" in sig.parameters
    assert sig.parameters["aggregate_timeout"].default is None


# ---------------------------------------------------------------------------
# Autorouter derivation from --timeout (mirrors the #3321 test pattern)
# ---------------------------------------------------------------------------


def _make_autorouter_stub() -> Autorouter:
    router = Autorouter.__new__(Autorouter)
    inner = MagicMock()
    inner.route_all_with_diffpairs = MagicMock(return_value=([], []))
    inner.intra_clearance_violations = MagicMock(return_value=[])
    inner.repair_intra_clearance_violations = MagicMock()
    router._diffpair_router = inner
    router._finalize_routing = MagicMock()
    return router


def test_timeout_derives_aggregate_budget():
    """``--timeout 600`` -> aggregate cap ``max(60, 600*0.25) = 150``."""
    router = _make_autorouter_stub()
    cfg = DifferentialPairConfig(enabled=True)

    router.route_all_with_diffpairs(cfg, timeout=600.0)

    kwargs = router._diffpair_router.route_all_with_diffpairs.call_args.kwargs
    assert kwargs["aggregate_timeout"] == 150.0


def test_aggregate_budget_floored_at_per_pair_budget():
    """The aggregate cap is never smaller than one full per-pair budget
    so a single pair always gets a complete attempt.  ``--timeout 100``
    derives per-pair = 30 and aggregate = max(30, 25) = 30."""
    router = _make_autorouter_stub()
    cfg = DifferentialPairConfig(enabled=True)

    router.route_all_with_diffpairs(cfg, timeout=100.0)

    kwargs = router._diffpair_router.route_all_with_diffpairs.call_args.kwargs
    assert kwargs["per_pair_timeout"] == 30.0
    assert kwargs["aggregate_timeout"] == 30.0


def test_explicit_per_pair_budget_floors_aggregate():
    """When the user set ``--diffpair-per-pair-timeout``, the aggregate
    floor uses the explicit value."""
    router = _make_autorouter_stub()
    cfg = DifferentialPairConfig(enabled=True, per_pair_timeout=200.0)

    router.route_all_with_diffpairs(cfg, timeout=600.0)

    kwargs = router._diffpair_router.route_all_with_diffpairs.call_args.kwargs
    # max(200, 150) = 200.
    assert kwargs["aggregate_timeout"] == 200.0


def test_explicit_aggregate_timeout_takes_precedence_over_derived():
    """An explicit ``DifferentialPairConfig.aggregate_timeout`` disables
    the auto-derivation (kwarg forwarded as None; the inner router's
    own precedence rule falls back to the config value)."""
    router = _make_autorouter_stub()
    cfg = DifferentialPairConfig(enabled=True, aggregate_timeout=42.0)

    router.route_all_with_diffpairs(cfg, timeout=600.0)

    kwargs = router._diffpair_router.route_all_with_diffpairs.call_args.kwargs
    assert kwargs["aggregate_timeout"] is None


def test_no_timeout_means_no_derived_aggregate_budget():
    """Back-compat: callers that never pass ``timeout`` see ``None``."""
    router = _make_autorouter_stub()
    cfg = DifferentialPairConfig(enabled=True)

    router.route_all_with_diffpairs(cfg)

    kwargs = router._diffpair_router.route_all_with_diffpairs.call_args.kwargs
    assert kwargs["aggregate_timeout"] is None


def test_aggregate_derivation_emits_structured_log_signal(caplog):
    import logging

    router = _make_autorouter_stub()
    cfg = DifferentialPairConfig(enabled=True)

    with caplog.at_level(logging.INFO, logger="kicad_tools.router.core"):
        router.route_all_with_diffpairs(cfg, timeout=600.0)

    assert any("DIFFPAIR_AGGREGATE_TIMEOUT_AUTODERIVED" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Behavioural invariant: exhausted aggregate budget defers pairs to the
# main strategy and never collapses reach below the diffpairs-off baseline
# ---------------------------------------------------------------------------


def _opt_in_diffpair_class_map(net_names: list[str]) -> dict[str, NetClassRouting]:
    nc = NetClassRouting(name="HighSpeedOptIn", coupled_routing=True)
    return dict.fromkeys(net_names, nc)


def _two_pad_diffpair_router() -> Autorouter:
    """30x10mm board with one straight two-pad diff pair plus one plain
    net, so 'reach' covers both diff and non-diff nets."""
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, grid_resolution=0.1)
    router = Autorouter(
        width=30.0,
        height=10.0,
        rules=rules,
        net_class_map=_opt_in_diffpair_class_map(["USB_D+", "USB_D-"]),
    )
    router.add_component(
        "U1",
        [
            {
                "number": "1",
                "x": 5.0,
                "y": 4.6,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "USB_D+",
            },
            {
                "number": "2",
                "x": 5.0,
                "y": 5.4,
                "width": 0.4,
                "height": 0.4,
                "net": 2,
                "net_name": "USB_D-",
            },
            {
                "number": "3",
                "x": 5.0,
                "y": 8.0,
                "width": 0.4,
                "height": 0.4,
                "net": 3,
                "net_name": "GPIO1",
            },
        ],
    )
    router.add_component(
        "J1",
        [
            {
                "number": "1",
                "x": 25.0,
                "y": 4.6,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "USB_D+",
            },
            {
                "number": "2",
                "x": 25.0,
                "y": 5.4,
                "width": 0.4,
                "height": 0.4,
                "net": 2,
                "net_name": "USB_D-",
            },
            {
                "number": "3",
                "x": 25.0,
                "y": 8.0,
                "width": 0.4,
                "height": 0.4,
                "net": 3,
                "net_name": "GPIO1",
            },
        ],
    )
    return router


def test_exhausted_aggregate_budget_skips_coupled_and_preserves_reach(monkeypatch):
    """The load-bearing #3439 invariant: with a (near-)zero aggregate
    budget, the coupled pathfinder is never invoked and every net --
    including the diff-pair members -- is still routed by the fallback
    per-net path.  Final reach therefore equals the
    ``--differential-pairs``-off baseline."""
    router = _two_pad_diffpair_router()
    config = DifferentialPairConfig(enabled=True, spacing=0.8)

    coupled_calls: list[str] = []
    original = DiffPairRouter.route_differential_pair_coupled

    def _spy(self, pair, *args, **kwargs):
        coupled_calls.append(pair.name)
        return original(self, pair, *args, **kwargs)

    monkeypatch.setattr(DiffPairRouter, "route_differential_pair_coupled", _spy)

    routes, _warnings = router._diffpair.route_all_with_diffpairs(
        config,
        aggregate_timeout=0.001,
    )

    assert coupled_calls == [], (
        "With an exhausted aggregate budget the coupled pathfinder must "
        f"never be invoked; saw calls for {coupled_calls}"
    )

    routed_nets = {r.net for r in routes}
    assert {1, 2, 3} <= routed_nets, (
        "Aggregate-budget deferral must leave the diff-pair nets to the "
        "main per-net strategy so reach never drops below the "
        f"diffpairs-off baseline; routed nets: {routed_nets}"
    )


def test_aggregate_cap_unset_preserves_coupled_routing():
    """Control for the invariant test: with no aggregate cap the pair
    routes through the coupled pathfinder as before."""
    router = _two_pad_diffpair_router()
    config = DifferentialPairConfig(enabled=True, spacing=0.8)

    routes, _warnings = router._diffpair.route_all_with_diffpairs(config)

    routed_nets = {r.net for r in routes}
    assert {1, 2, 3} <= routed_nets
