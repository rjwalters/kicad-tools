"""Issue #6366: the evolutionary genes reach the router (or are gone).

``astar_weights`` becomes a per-net A* weight on the pathfinder's ``route()``,
``preferred_layers`` becomes a ``net_class_map`` layer preference, and
``strategy_flags`` was removed.  The seed chromosome must stay byte-identical
to the default route, per-trial state must not leak, and the time budget must
be split across the remaining generations.
"""

from __future__ import annotations

import time

import pytest

from kicad_tools.router.algorithms import evolutionary as evo_mod
from kicad_tools.router.algorithms import monte_carlo as mc_mod
from kicad_tools.router.algorithms.evolutionary import (
    EvolutionaryRoutingOptimizer,
    RoutingChromosome,
    mutate,
    order_crossover,
)
from kicad_tools.router.core import Autorouter
from kicad_tools.router.rules import NetClassRouting

NUM_NETS = 4


def _router(**kw) -> Autorouter:
    router = Autorouter(width=40, height=40, force_python=True, **kw)
    for i in range(NUM_NETS):
        net = i + 1
        y = 5.0 + 6.0 * i
        router.add_component(
            f"R{net}",
            [
                {"number": "1", "x": 5.0, "y": y, "net": net, "net_name": f"N{net}"},
                {"number": "2", "x": 9.0 + 0.5 * i, "y": y, "net": net, "net_name": f"N{net}"},
            ],
        )
    return router


def _sig(routes):
    return [
        (
            r.net,
            [(s.x1, s.y1, s.x2, s.y2, s.layer) for s in r.segments],
            [(v.x, v.y) for v in r.vias],
        )
        for r in routes
    ]


def _base_order(router):
    return sorted((n for n in router.nets if n != 0), key=router._get_net_priority)


def test_no_strategy_flags_gene():
    assert not hasattr(RoutingChromosome(), "strategy_flags")


def test_seed_chromosome_has_no_gene_effect():
    router = _router()
    base = _base_order(router)
    pop = EvolutionaryRoutingOptimizer(pop_size=3)._init_population(
        base, router._get_net_priority, 7, num_layers=router.grid.num_layers
    )
    seed = pop[0]
    assert seed.preferred_layers == {}
    assert set(seed.astar_weights.values()) == {1.0}


def test_seed_chromosome_is_byte_identical_to_default_route():
    default = _router()
    expected = _sig(default.route_all())

    router = _router()
    base = _base_order(router)
    seed = EvolutionaryRoutingOptimizer(pop_size=2)._init_population(
        base, router._get_net_priority, 7, num_layers=router.grid.num_layers
    )[0]
    ((routes, _score),) = evo_mod._evaluate_sequential(
        router, [seed], base_seed=1, gen=0, total_nets=NUM_NETS, base_order=base
    )
    assert _sig(routes) == expected


def test_astar_weight_reaches_pathfinder_route_python(monkeypatch):
    from kicad_tools.router.pathfinder import Router

    seen: dict[int, float] = {}
    orig = Router._route_impl

    def spy(self, start, end, *args, **kwargs):
        seen[start.net] = kwargs.get("weight", 1.0)
        return orig(self, start, end, *args, **kwargs)

    monkeypatch.setattr(Router, "_route_impl", spy)
    router = _router()
    assert type(router.router).__name__ == "Router"
    base = _base_order(router)
    chrom = RoutingChromosome(net_order=list(base), astar_weights={base[0]: 2.0})
    evo_mod._evaluate_sequential(
        router, [chrom], base_seed=1, gen=0, total_nets=NUM_NETS, base_order=base
    )
    assert seen[base[0]] == 2.0
    assert all(seen[n] == 1.0 for n in base[1:] if n in seen)


def test_astar_weight_reaches_cpp_dispatch(monkeypatch):
    cpp = pytest.importorskip("kicad_tools.router.cpp_backend")
    if not cpp.is_cpp_available():
        pytest.skip("C++ backend not built")
    router_cpp = Autorouter(width=40, height=40)
    if type(router_cpp.router).__name__ != "CppPathfinder":
        pytest.skip("C++ pathfinder not in use")
    seen: dict[int, float] = {}

    def spy(self, start, end, **kwargs):
        seen[start.net] = kwargs.get("weight", 1.0)
        return None

    monkeypatch.setattr(cpp.CppPathfinder, "_route_impl", spy)
    for i in range(2):
        net = i + 1
        router_cpp.add_component(
            f"R{net}",
            [
                {"number": "1", "x": 5.0, "y": 5.0 + 6 * i, "net": net, "net_name": f"N{net}"},
                {"number": "2", "x": 9.0, "y": 5.0 + 6 * i, "net": net, "net_name": f"N{net}"},
            ],
        )
    with router_cpp.trial_gene_overrides({1: 2.0}, None):
        for net in (1, 2):
            pads = [router_cpp.pads[k] for k in router_cpp.nets[net]]
            router_cpp.router.route(pads[0], pads[1])
    assert seen == {1: 2.0, 2: 1.0}


def test_preferred_layer_reaches_net_class_map_and_is_restored():
    router = _router()
    base = _base_order(router)
    name = router.net_names[base[0]]
    before = dict(router.net_class_map)
    seen: dict = {}

    def fake_route_net(self, net, *a, **k):
        if net == base[0]:
            nc = self.net_class_map.get(name)
            seen["pref"] = nc.preferred_layers if nc else None
        return []

    orig = Autorouter.route_net
    Autorouter.route_net = fake_route_net
    try:
        chrom = RoutingChromosome(net_order=list(base), preferred_layers={base[0]: 1})
        evo_mod._evaluate_sequential(
            router, [chrom], base_seed=1, gen=0, total_nets=NUM_NETS, base_order=base
        )
    finally:
        Autorouter.route_net = orig
    assert seen["pref"] == [1]
    # No leftover preference (nor weight) for the next chromosome.
    assert router.net_class_map == before
    assert router._net_astar_weights == {}


def test_hard_avoided_preferred_layer_is_not_injected():
    router = _router()
    name = router.net_names[1]
    router.net_class_map[name] = NetClassRouting(name="x", avoid_layers=[1], target_ampacity=1.0)
    avoided = router.net_class_map[name].hard_avoided_layer_indices(router.rules.strict_layers)
    if 1 not in avoided:
        pytest.skip("avoid_layers not hard in this configuration")
    with router.trial_gene_overrides(None, {1: 1}):
        assert router.net_class_map[name].preferred_layers != [1]


def test_worker_applies_genes(monkeypatch):
    seen = {}

    def fake_route_net(self, net, *a, **k):
        seen[net] = (
            self._net_astar_weights.get(net),
            (
                self.net_class_map.get(self.net_names[net]) or NetClassRouting(name="d")
            ).preferred_layers,
        )
        return []

    monkeypatch.setattr(Autorouter, "route_net", fake_route_net)
    router = _router()
    base = _base_order(router)
    config = router._serialize_for_parallel()
    config.update(
        {
            "chrom_idx": 0,
            "seed": 1,
            "net_order": base,
            "astar_weights": {base[0]: 2.0},
            "preferred_layers": {base[0]: 1},
        }
    )
    evo_mod._run_evolutionary_trial(config)
    assert seen[base[0]] == (2.0, [1])
    assert seen[base[1]][0] is None


def test_mutation_moves_or_drops_preferences_within_board_layers():
    import random

    random.seed(3)
    nets = list(range(1, 9))
    c = RoutingChromosome(net_order=list(nets), preferred_layers={1: 0})
    seen = set()
    for _ in range(300):
        mutate(c, mutation_rate=1.0, num_layers=4)
        seen.update(c.preferred_layers.values())
    assert seen <= {0, 1, 2, 3}
    assert len(seen) > 2


def test_single_layer_board_has_no_layer_gene():
    import random

    random.seed(3)
    c = RoutingChromosome(net_order=[1, 2, 3])
    for _ in range(50):
        mutate(c, mutation_rate=1.0, num_layers=1)
    assert c.preferred_layers == {}
    pop = EvolutionaryRoutingOptimizer(pop_size=4)._init_population(
        [1, 2, 3], lambda n: (0, 0, 0, n, 0, 0), 1, num_layers=1
    )
    assert all(ch.preferred_layers == {} for ch in pop)


def test_crossover_inherits_preferences():
    p1 = RoutingChromosome(net_order=[1, 2, 3], preferred_layers={1: 0, 2: 1})
    p2 = RoutingChromosome(net_order=[3, 2, 1], preferred_layers={3: 1})
    for _ in range(20):
        child = order_crossover(p1, p2)
        assert set(child.preferred_layers) <= {1, 2, 3}


def test_budget_accounts_for_remaining_generations(monkeypatch):
    calls = []

    def fake_timeout(deadline, trials_left, workers=1):
        calls.append((trials_left, workers))
        return 1.0

    monkeypatch.setattr(evo_mod, "trial_timeout", fake_timeout)
    monkeypatch.setattr(evo_mod, "route_trial", lambda *a, **k: [])
    router = _router()
    base = _base_order(router)
    pop = [RoutingChromosome(net_order=list(base)) for _ in range(3)]
    evo_mod._evaluate_sequential(
        router,
        pop,
        1,
        0,
        NUM_NETS,
        deadline=time.monotonic() + 1000.0,
        base_order=base,
        generations_left=3,
    )
    assert [c[0] for c in calls] == [9, 8, 7]

    d = time.monotonic() + 9000.0
    gen1 = mc_mod.trial_timeout(d, 4 * 3, 2)
    today = mc_mod.trial_timeout(d, 4, 2)
    assert gen1 == pytest.approx(today / 3, rel=0.05)
