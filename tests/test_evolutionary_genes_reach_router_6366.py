"""Issue #6366: the evolutionary genes reach the router (or are gone).

``astar_weights`` becomes a per-net A* weight on the pathfinder's ``route()``,
``preferred_layers`` becomes a ``net_class_map`` layer preference (only on a
backend that honours layer preferences -- the pure-Python ``Router``; the
native C++ pathfinder has no layer cost, so the gene is off there), and
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
from kicad_tools.router.rules import DesignRules, NetClassRouting

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


# --- review follow-ups (changes requested on 52a57b8e) -----------------------


class _Abort(Exception):
    pass


def _cpp_router():
    cpp = pytest.importorskip("kicad_tools.router.cpp_backend")
    if not cpp.is_cpp_available():
        pytest.skip("C++ backend not built")
    router = Autorouter(width=40, height=40)
    if type(router.router).__name__ != "CppPathfinder":
        pytest.skip("C++ pathfinder not in use")
    return router


def _obstacle_board() -> Autorouter:
    """Two THT pads on SIG_A with a small F.Cu-only obstacle between them.

    Without a preference the search mixes layers; the layer gene must steer
    it onto the preferred layer.  0.3/0.3 board rules (not the 0.2/0.2
    ``NetClassRouting`` literals) so a width/clearance leak would show.
    """
    router = Autorouter(
        width=40,
        height=40,
        force_python=True,
        rules=DesignRules(trace_width=0.3, trace_clearance=0.3),
    )
    th = {"through_hole": True, "drill": 0.8, "width": 1.6, "height": 1.6}
    router.add_component(
        "R1",
        [
            {"number": "1", "x": 5.0, "y": 20.0, "net": 1, "net_name": "SIG_A", **th},
            {"number": "2", "x": 25.0, "y": 20.0, "net": 1, "net_name": "SIG_A", **th},
        ],
    )
    router.add_component(
        "U1",
        [
            {
                "number": "1",
                "x": 15.0,
                "y": 20.0,
                "net": 2,
                "net_name": "OBST",
                "width": 2.0,
                "height": 1.0,
            }
        ],
    )
    return router


def _route_with_pref(pref):
    router = _obstacle_board()
    with router.trial_gene_overrides(None, pref):
        routes = router.route_all(suppress_no_timeout_warning=True)
    segs = [s for r in routes if r.net == 1 for s in r.segments]
    vias = [v for r in routes if r.net == 1 for v in r.vias]
    assert segs, "SIG_A must route"
    return router, segs, vias


def test_preferred_layer_changes_python_search():
    """The gene changes where the Python pathfinder routes the net."""
    router, segs0, vias0 = _route_with_pref({1: 0})
    layer0 = router.grid.index_to_layer(0)
    assert {s.layer.value for s in segs0} == {layer0}
    assert not vias0

    router, segs1, vias1 = _route_with_pref({1: 1})
    layer1 = router.grid.index_to_layer(1)
    assert {s.layer.value for s in segs1} == {layer1}
    assert not vias1

    # And neither equals the unpreferred route (which mixes layers via a via).
    _router_none, segs_none, _vias_none = _route_with_pref(None)
    assert {s.layer.value for s in segs_none} == {layer0, layer1}


def test_classless_net_keeps_board_rules_under_injection():
    """A preference must not swap a classless net onto 0.2/0.2/0.6 literals."""
    router = Autorouter(
        width=40,
        height=40,
        force_python=True,
        rules=DesignRules(trace_width=0.3, trace_clearance=0.3, via_diameter=0.8),
    )
    router.add_component(
        "R1",
        [
            {"number": "1", "x": 5.0, "y": 5.0, "net": 1, "net_name": "SIG_A"},
            {"number": "2", "x": 9.0, "y": 5.0, "net": 1, "net_name": "SIG_A"},
        ],
    )
    assert "SIG_A" not in router.net_class_map
    with router.trial_gene_overrides(None, {1: 1}):
        nc = router.net_class_map["SIG_A"]
        assert nc.preferred_layers == [1]
        assert nc.trace_width == pytest.approx(0.3)
        assert nc.clearance == pytest.approx(0.3)
        assert nc.via_size == pytest.approx(0.8)
        # Classless priority is 10 (``_get_net_priority``), not the class 5.
        assert nc.priority == 10
    assert "SIG_A" not in router.net_class_map


def test_routed_width_unchanged_for_classless_net_with_preference():
    _router, segs, vias = _route_with_pref({1: 1})
    assert all(s.width == pytest.approx(0.3) for s in segs)
    _router, segs_none, _ = _route_with_pref(None)
    assert all(s.width == pytest.approx(0.3) for s in segs_none)


def test_python_router_advertises_layer_preferences():
    assert _router().supports_layer_preferences is True


def test_cpp_backend_has_no_layer_gene():
    """C++ ignores preferred_layers, so the gene is off and inert there."""
    router = _cpp_router()
    assert router.supports_layer_preferences is False
    router.add_component(
        "R1",
        [
            {"number": "1", "x": 5.0, "y": 5.0, "net": 1, "net_name": "SIG_A"},
            {"number": "2", "x": 9.0, "y": 5.0, "net": 1, "net_name": "SIG_A"},
        ],
    )
    before = dict(router.net_class_map)
    with router.trial_gene_overrides({1: 2.0}, {1: 1}):
        # No net_class_map rewrite (it would have no search effect) ...
        assert router.net_class_map == before
        # ... while the A* weight gene still applies.
        assert router._net_astar_weights == {1: 2.0}


@pytest.mark.parametrize("backend", ["python", "cpp"])
def test_run_evolutionary_gene_domain_follows_backend(monkeypatch, backend):
    router = _router() if backend == "python" else _cpp_router()
    if backend == "cpp":
        router.add_component(
            "R1",
            [
                {"number": "1", "x": 5.0, "y": 5.0, "net": 1, "net_name": "N1"},
                {"number": "2", "x": 9.0, "y": 5.0, "net": 1, "net_name": "N1"},
            ],
        )
    seen = {}
    orig_init = EvolutionaryRoutingOptimizer._init_population

    def spy(self, *a, **k):
        pop = orig_init(self, *a, **k)
        seen["domain"] = self.num_layers
        seen["prefs"] = [c.preferred_layers for c in pop]
        raise _Abort

    monkeypatch.setattr(EvolutionaryRoutingOptimizer, "_init_population", spy)
    with pytest.raises(_Abort):
        evo_mod.run_evolutionary(router, pop_size=6, generations=1, seed=1, verbose=False)
    if backend == "python":
        assert seen["domain"] == router.grid.num_layers
    else:
        assert seen["domain"] == 0
        assert all(p == {} for p in seen["prefs"])


def test_init_population_defaults_to_optimizer_domain():
    import random

    random.seed(5)
    pop = EvolutionaryRoutingOptimizer(pop_size=6, num_layers=0)._init_population(
        [1, 2, 3], lambda n: (0, 0, 0, n, 0, 0), 1
    )
    assert all(c.preferred_layers == {} for c in pop)
    pop = EvolutionaryRoutingOptimizer(pop_size=6, num_layers=4)._init_population(
        [1, 2, 3, 4, 5, 6], lambda n: (0, 0, 0, n, 0, 0), 1
    )
    assert any(c.preferred_layers for c in pop)
