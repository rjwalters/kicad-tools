"""Issue #6345: Monte Carlo / evolutionary trials route in their own net order.

Every Monte Carlo trial and every evolutionary chromosome on board 05 produced
the byte-identical route.  Three defects:

1. ``Autorouter.route_all(net_order)`` re-sorts a caller-supplied order by
   ``_get_net_priority`` -- a key that is effectively unique per net -- so each
   shuffle was undone and every trial routed the canonical priority sequence.
2. Negotiated trials called ``route_all_negotiated()`` with no order at all,
   and the CLI never asked for negotiated trials, so each trial was a single
   basic pass (8 trials finished worse than one default negotiated run).
3. The evolutionary ``astar_weights`` / ``preferred_layers`` /
   ``strategy_flags`` genes never reach the router (documented as inert).

These tests observe the per-net routing order with a spy on ``route_net``
(not just the returned routes) on every trial path: the Monte Carlo worker,
the Monte Carlo sequential loop, the evolutionary worker and the evolutionary
sequential evaluator.
"""

from __future__ import annotations

import random

import pytest

from kicad_tools.router.algorithms import evolutionary as evo_mod
from kicad_tools.router.algorithms import monte_carlo as mc_mod
from kicad_tools.router.algorithms.evolutionary import RoutingChromosome
from kicad_tools.router.core import Autorouter, _run_monte_carlo_trial
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment

NUM_NETS = 6


def _router() -> Autorouter:
    """Six same-tier 2-pin nets with distinct spans (distinct priority keys)."""
    router = Autorouter(width=60, height=60, force_python=True)
    for i in range(NUM_NETS):
        net = i + 1
        y = 5.0 + 8.0 * i
        span = 3.0 + 0.4 * i
        router.add_component(
            f"R{net}",
            [
                {"number": "1", "x": 5.0, "y": y, "net": net, "net_name": f"N{net}"},
                {"number": "2", "x": 5.0 + span, "y": y, "net": net, "net_name": f"N{net}"},
            ],
        )
    return router


def _priority_order(router: Autorouter) -> list[int]:
    return sorted(
        (n for n in router.nets if n != 0),
        key=lambda n: router._get_net_priority(n),
    )


@pytest.fixture
def spy(monkeypatch):
    """Record the nets ``route_net`` is asked to route, in call order."""
    calls: list[int] = []

    def fake_route_net(self, net, *args, **kwargs):
        calls.append(net)
        return []

    monkeypatch.setattr(Autorouter, "route_net", fake_route_net)
    return calls


def _same_tier(router: Autorouter) -> None:
    keys = {router._get_net_priority(n)[:2] for n in router.nets if n != 0}
    assert len(keys) == 1, f"fixture nets must share one (class, tier) group: {keys}"


# ---------------------------------------------------------------------------
# Monte Carlo worker (the default parallel path)
# ---------------------------------------------------------------------------


def _mc_config(router: Autorouter, trial_num: int, **extra) -> dict:
    config = router._serialize_for_parallel()
    config.update(
        {
            "trial_num": trial_num,
            "seed": 1000 + trial_num,
            "base_order": _priority_order(router),
            "use_negotiated": False,
        }
    )
    config.update(extra)
    return config


def test_mc_worker_trial_routes_its_own_shuffled_order(spy):
    router = _router()
    _same_tier(router)
    priority = _priority_order(router)

    # Reproduce the worker's shuffle (same seed, same promotion rate).
    trial_num = 3
    random.seed(1000 + trial_num)
    expected = router._shuffle_within_tiers(
        priority, promotion_rate=min(0.1 + 0.05 * trial_num, 0.5)
    )
    assert expected != priority, "seed must yield a non-identity shuffle"

    _run_monte_carlo_trial(_mc_config(router, trial_num))

    # Before #6345 the recorded order was the priority sort for every trial.
    assert spy == expected
    assert spy != priority


def test_mc_worker_trial_zero_is_the_default_order(spy):
    router = _router()
    default_router = _router()
    default_router.route_all(suppress_no_timeout_warning=True)
    default_calls = list(spy)
    spy.clear()

    _run_monte_carlo_trial(_mc_config(router, 0))

    assert spy == default_calls == _priority_order(router)


def test_mc_worker_trials_differ_from_each_other(spy):
    router = _router()
    orders = []
    for trial in range(1, 6):
        spy.clear()
        _run_monte_carlo_trial(_mc_config(router, trial))
        orders.append(tuple(spy))
    assert len(set(orders)) > 1


def test_mc_worker_negotiated_trial_receives_its_order(monkeypatch):
    seen: dict = {}

    def fake_negotiated(self, **kwargs):
        seen["order"] = self._forced_net_order
        seen["kwargs"] = kwargs
        return []

    monkeypatch.setattr(Autorouter, "route_all_negotiated", fake_negotiated)
    router = _router()
    priority = _priority_order(router)
    trial_num = 3
    random.seed(1000 + trial_num)
    expected = router._shuffle_within_tiers(
        priority, promotion_rate=min(0.1 + 0.05 * trial_num, 0.5)
    )

    _run_monte_carlo_trial(
        _mc_config(
            router,
            trial_num,
            use_negotiated=True,
            negotiated_kwargs={"max_iterations": 7},
            trial_timeout=12.5,
            per_net_timeout=3.0,
        )
    )

    assert seen["order"] == expected
    assert seen["kwargs"]["max_iterations"] == 7
    assert seen["kwargs"]["timeout"] == 12.5
    assert seen["kwargs"]["per_net_timeout"] == 3.0
    assert seen["kwargs"]["seed"] == 1000 + trial_num


# ---------------------------------------------------------------------------
# Monte Carlo sequential loop
# ---------------------------------------------------------------------------


def test_mc_sequential_trials_route_in_different_orders(spy):
    router = _router()
    priority = _priority_order(router)
    orders: list[list[int]] = []
    real_reset = Autorouter._reset_for_new_trial

    def reset_and_mark(self):
        real_reset(self)
        orders.append([])

    original_route_net = Autorouter.route_net

    def recording_route_net(self, net, *args, **kwargs):
        orders[-1].append(net)
        return original_route_net(self, net, *args, **kwargs)

    router._reset_for_new_trial = reset_and_mark.__get__(router)
    router.route_net = recording_route_net.__get__(router)

    mc_mod.run_monte_carlo(router, num_trials=5, seed=7, verbose=False, num_workers=1)

    trial_orders = [o for o in orders if o]
    assert len(trial_orders) == 5
    assert trial_orders[0] == priority  # trial 0 is the faithful baseline
    assert len({tuple(o) for o in trial_orders}) > 1
    # The parent router is never left with a forced order.
    assert router._forced_net_order is None


def test_mc_sequential_preserves_user_forced_order(spy):
    router = _router()
    user_order = list(reversed(_priority_order(router)))
    router._forced_net_order = list(user_order)

    mc_mod.run_monte_carlo(router, num_trials=1, seed=7, verbose=False, num_workers=1)

    assert spy == user_order  # trial 0 honours the --order-method order
    assert router._forced_net_order == user_order  # and it is restored


# ---------------------------------------------------------------------------
# Evolutionary worker and sequential evaluator
# ---------------------------------------------------------------------------


def test_evolutionary_worker_routes_the_chromosome_order(spy):
    router = _router()
    priority = _priority_order(router)
    reversed_order = list(reversed(priority))
    config = router._serialize_for_parallel()
    config.update({"chrom_idx": 0, "seed": 5, "net_order": reversed_order})

    evo_mod._run_evolutionary_trial(config)

    assert spy == reversed_order
    assert spy != priority


def test_evolutionary_worker_negotiated_receives_order(monkeypatch):
    seen: dict = {}

    def fake_negotiated(self, **kwargs):
        seen["order"] = self._forced_net_order
        seen["timeout"] = kwargs.get("timeout")
        return []

    monkeypatch.setattr(Autorouter, "route_all_negotiated", fake_negotiated)
    router = _router()
    reversed_order = list(reversed(_priority_order(router)))
    config = router._serialize_for_parallel()
    config.update(
        {
            "chrom_idx": 0,
            "seed": 5,
            "net_order": reversed_order,
            "use_negotiated": True,
            "trial_timeout": 9.0,
        }
    )

    evo_mod._run_evolutionary_trial(config)

    assert seen == {"order": reversed_order, "timeout": 9.0}


def test_evolutionary_sequential_routes_each_chromosome_order(spy):
    router = _router()
    priority = _priority_order(router)
    reversed_order = list(reversed(priority))
    population = [
        RoutingChromosome(net_order=list(priority)),
        RoutingChromosome(net_order=list(reversed_order)),
    ]

    evo_mod._evaluate_sequential(
        router, population, base_seed=1, gen=0, total_nets=NUM_NETS, base_order=priority
    )

    assert spy == priority + reversed_order
    assert router._forced_net_order is None


# ---------------------------------------------------------------------------
# Results differ when order matters
# ---------------------------------------------------------------------------


def test_mc_trials_score_differently_when_order_matters(monkeypatch):
    """First-routed net wins a contested channel: different orders, different scores."""
    router = _router()

    def contested_route_net(self, net, *args, **kwargs):
        # Whichever of nets 1/2 routes first takes the shared channel; the
        # other fails.  Net 1 is long (many points), net 2 short.
        taken = getattr(self, "_channel_taken", False)
        if net in (1, 2):
            if taken:
                return []
            self._channel_taken = True
        length = 20.0 if net == 1 else 2.0
        seg = Segment(0.0, float(net), length, float(net), 0.25, Layer.F_CU, net=net)
        route = Route(net=net, net_name=f"N{net}", segments=[seg])
        return [route]

    real_reset = Autorouter._reset_for_new_trial

    def reset(self):
        real_reset(self)
        self._channel_taken = False

    monkeypatch.setattr(Autorouter, "route_net", contested_route_net)
    monkeypatch.setattr(Autorouter, "_reset_for_new_trial", reset)
    monkeypatch.setattr(
        Autorouter,
        "_evaluate_solution",
        lambda self, routes: -sum(s.x2 - s.x1 for r in routes for s in r.segments),
    )

    scores: list[float] = []
    real_eval = Autorouter._evaluate_solution

    def recording_eval(self, routes):
        score = real_eval(self, routes)
        scores.append(score)
        return score

    monkeypatch.setattr(Autorouter, "_evaluate_solution", recording_eval)
    mc_mod.run_monte_carlo(router, num_trials=8, seed=3, verbose=False, num_workers=1)

    assert len(scores) == 8
    assert len(set(scores)) > 1, f"all MC trials scored the same: {scores}"


# ---------------------------------------------------------------------------
# CLI: --trial-router (default negotiated) reaches every trial
# ---------------------------------------------------------------------------


def test_cli_trial_router_defaults_to_negotiated():
    from kicad_tools.cli.route_cmd import _multistart_trial_kwargs, _route_parser

    args = _route_parser().parse_args(
        ["b.kicad_pcb", "--strategy", "monte-carlo", "--seed", "42", "--iterations", "9"]
    )
    kwargs = _multistart_trial_kwargs(args)
    assert kwargs["use_negotiated"] is True
    assert kwargs["seed"] == 42
    assert kwargs["negotiated_kwargs"]["max_iterations"] == 9

    basic = _route_parser().parse_args(["b.kicad_pcb", "--trial-router", "basic"])
    assert _multistart_trial_kwargs(basic)["use_negotiated"] is False


def test_cli_trial_router_is_forwarded_by_outer_parser(tmp_path):
    from unittest.mock import patch

    from kicad_tools.cli.commands.routing import run_route_command
    from kicad_tools.cli.parser import create_parser
    from kicad_tools.cli.route_cmd import _route_parser

    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20221018) (generator pcbnew))")
    args = create_parser().parse_args(["route", str(pcb), "--trial-router", "basic", "--quiet"])
    with patch("kicad_tools.cli.route_cmd.main") as mock_main:
        mock_main.return_value = 0
        run_route_command(args)
        sub_argv = mock_main.call_args[0][0]
    assert _route_parser().parse_args(sub_argv).trial_router == "basic"


def test_route_all_monte_carlo_forwards_trial_kwargs(monkeypatch):
    captured: dict = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(mc_mod, "run_monte_carlo", fake_run)
    monkeypatch.setattr(evo_mod, "run_evolutionary", fake_run)
    router = _router()
    router.route_all_monte_carlo(
        num_trials=2, use_negotiated=True, negotiated_kwargs={"max_iterations": 3},
        per_net_timeout=4.0, verbose=False,
    )  # fmt: skip
    assert captured["use_negotiated"] is True
    assert captured["negotiated_kwargs"] == {"max_iterations": 3}
    assert captured["per_net_timeout"] == 4.0
    captured.clear()
    router.route_all_evolutionary(
        pop_size=2, generations=1, use_negotiated=True, per_net_timeout=5.0, verbose=False
    )
    assert captured["use_negotiated"] is True
    assert captured["per_net_timeout"] == 5.0


def test_trial_timeout_leaves_headroom_before_the_parent_deadline():
    import time

    assert mc_mod.trial_timeout(None, 4, 4) is None
    deadline = time.monotonic() + 4000.0
    budget = mc_mod.trial_timeout(deadline, 4, 4)  # one wave
    assert 3500.0 < budget <= 3600.0 + 1.0  # 10% headroom
    small = mc_mod.trial_timeout(time.monotonic() + 100.0, 1, 1)
    assert 45.0 <= small <= 50.5  # headroom capped at half the share
    two_waves = mc_mod.trial_timeout(time.monotonic() + 2000.0, 8, 4)
    assert 850.0 < two_waves < 910.0
