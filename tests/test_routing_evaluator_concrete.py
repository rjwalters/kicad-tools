"""Tests for the concrete :class:`CppAstarRoutingEvaluator` (Issue #2719).

These tests exercise:

1. **Protocol conformance** — the evaluator structurally satisfies the
   :class:`kicad_tools.optim.evolutionary.RoutingEvaluator` Protocol.
2. **Trivial 1-net case** — a placement with one fully-stitchable net
   returns 1.0.
3. **Zero completion case** — a placement that cannot route returns 0.0
   without raising.
4. **Determinism** — same ``seed`` → same returned float.
5. **Timeout respect** — the inner GA exits before completing all
   generations when ``timeout_seconds`` is short.
6. **Hybrid completion semantic** — partial connectivity contributes
   ``connected_pairs / required_pairs``, full connectivity contributes 1.0.
7. **Factory failure** — an exception in the router factory returns 0.0
   instead of propagating.
8. **Default ``num_workers``** — guards against nested-pool regressions.

Test patterns mirror ``tests/test_evolutionary_routing.py`` and
``tests/test_routing_fitness.py`` (the protocol's _MockRoutingEvaluator
contract).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from kicad_tools.optim.evolutionary import RoutingEvaluator
from kicad_tools.router.evaluators import (
    CppAstarRoutingEvaluator,
    RoutingEvaluatorConfig,
    compute_hybrid_completion_rate,
)

# ---------------------------------------------------------------------------
# Lightweight fakes for routes / segments / pads
# ---------------------------------------------------------------------------


@dataclass
class _FakeSegment:
    x1: float
    y1: float
    x2: float
    y2: float
    net: int = 0
    width: float = 0.2


@dataclass
class _FakeVia:
    x: float
    y: float
    net: int = 0


@dataclass
class _FakeRoute:
    net: int
    net_name: str
    segments: list
    vias: list


@dataclass
class _FakePad:
    x: float
    y: float
    width: float = 0.5
    height: float = 0.5
    net: int = 0
    net_name: str = ""
    ref: str = ""
    pin: str = ""


class _FakeRouter:
    """Just enough surface for compute_hybrid_completion_rate."""

    def __init__(self, pads: dict, nets: dict[int, list]) -> None:
        self.pads = pads
        self.nets = nets
        self.net_names = {nid: f"NET{nid}" for nid in nets}


# ---------------------------------------------------------------------------
# compute_hybrid_completion_rate unit tests
# ---------------------------------------------------------------------------


class TestHybridCompletionRate:
    def test_empty_signal_nets_returns_one(self):
        router = _FakeRouter(pads={}, nets={0: []})
        assert compute_hybrid_completion_rate(router, []) == 1.0

    def test_no_routes_zero_for_multi_pad_net(self):
        pads = {
            ("U1", "1"): _FakePad(x=0.0, y=0.0, net=1, ref="U1", pin="1"),
            ("U2", "1"): _FakePad(x=10.0, y=0.0, net=1, ref="U2", pin="1"),
        }
        router = _FakeRouter(pads=pads, nets={1: [("U1", "1"), ("U2", "1")]})
        assert compute_hybrid_completion_rate(router, []) == 0.0

    def test_single_pad_net_contributes_one(self):
        """A net with only one pad has zero required pad-pairs, contributing 1.0."""
        pads = {
            ("U1", "1"): _FakePad(x=0.0, y=0.0, net=1, ref="U1", pin="1"),
        }
        router = _FakeRouter(pads=pads, nets={1: [("U1", "1")]})
        assert compute_hybrid_completion_rate(router, []) == 1.0

    def test_full_stitch_returns_one(self):
        """Three pads on one net, all connected by segments → 1.0."""
        pads = {
            ("U1", "1"): _FakePad(x=0.0, y=0.0, net=1, ref="U1", pin="1"),
            ("U2", "1"): _FakePad(x=10.0, y=0.0, net=1, ref="U2", pin="1"),
            ("U3", "1"): _FakePad(x=20.0, y=0.0, net=1, ref="U3", pin="1"),
        }
        router = _FakeRouter(
            pads=pads,
            nets={1: [("U1", "1"), ("U2", "1"), ("U3", "1")]},
        )
        # Two segments stitching all three pads.
        routes = [
            _FakeRoute(
                net=1,
                net_name="NET1",
                segments=[
                    _FakeSegment(x1=0.0, y1=0.0, x2=10.0, y2=0.0, net=1),
                    _FakeSegment(x1=10.0, y1=0.0, x2=20.0, y2=0.0, net=1),
                ],
                vias=[],
            ),
        ]
        rate = compute_hybrid_completion_rate(router, routes)
        assert rate == pytest.approx(1.0)

    def test_partial_stitch_returns_fraction(self):
        """Three pads, only first two connected → 1/2 contribution → 0.5 total."""
        pads = {
            ("U1", "1"): _FakePad(x=0.0, y=0.0, net=1, ref="U1", pin="1"),
            ("U2", "1"): _FakePad(x=10.0, y=0.0, net=1, ref="U2", pin="1"),
            ("U3", "1"): _FakePad(x=20.0, y=0.0, net=1, ref="U3", pin="1"),
        }
        router = _FakeRouter(
            pads=pads,
            nets={1: [("U1", "1"), ("U2", "1"), ("U3", "1")]},
        )
        # Only one segment connecting U1–U2; U3 is electrically alone.
        routes = [
            _FakeRoute(
                net=1,
                net_name="NET1",
                segments=[
                    _FakeSegment(x1=0.0, y1=0.0, x2=10.0, y2=0.0, net=1),
                ],
                vias=[],
            ),
        ]
        rate = compute_hybrid_completion_rate(router, routes)
        # Largest component has 2 pads → connected_pairs = 1 of 2 required.
        assert rate == pytest.approx(0.5)

    def test_excludes_net_zero(self):
        """Net 0 (unconnected) must not be counted."""
        pads = {
            ("U1", "1"): _FakePad(x=0.0, y=0.0, net=0, ref="U1", pin="1"),
            ("U2", "1"): _FakePad(x=10.0, y=0.0, net=0, ref="U2", pin="1"),
        }
        router = _FakeRouter(pads=pads, nets={0: [("U1", "1"), ("U2", "1")]})
        rate = compute_hybrid_completion_rate(router, [])
        assert rate == 1.0  # No signal nets at all.

    def test_average_across_nets(self):
        """Two nets: one full (1.0), one with 0 contribution → average 0.5."""
        pads = {
            ("U1", "1"): _FakePad(x=0.0, y=0.0, net=1, ref="U1", pin="1"),
            ("U2", "1"): _FakePad(x=10.0, y=0.0, net=1, ref="U2", pin="1"),
            ("U3", "1"): _FakePad(x=0.0, y=5.0, net=2, ref="U3", pin="1"),
            ("U4", "1"): _FakePad(x=10.0, y=5.0, net=2, ref="U4", pin="1"),
        }
        router = _FakeRouter(
            pads=pads,
            nets={
                1: [("U1", "1"), ("U2", "1")],
                2: [("U3", "1"), ("U4", "1")],
            },
        )
        # Only net 1 is routed.
        routes = [
            _FakeRoute(
                net=1,
                net_name="NET1",
                segments=[_FakeSegment(x1=0.0, y1=0.0, x2=10.0, y2=0.0, net=1)],
                vias=[],
            ),
        ]
        rate = compute_hybrid_completion_rate(router, routes)
        assert rate == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# CppAstarRoutingEvaluator: Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_satisfies_routing_evaluator_protocol(self):
        """Structural typing: a CppAstarRoutingEvaluator IS a RoutingEvaluator."""

        def _factory(positions, rotations):
            raise RuntimeError("unused in this test")

        ev: RoutingEvaluator = CppAstarRoutingEvaluator(router_factory=_factory)
        # The Protocol check is at type-check time; verify the attribute
        # also exists at runtime.
        assert callable(ev.evaluate_routability)

    def test_evaluate_routability_returns_float(self):
        """A factory that returns a router with no signal nets → 1.0."""

        def _factory(positions, rotations):
            return _FakeRouter(pads={}, nets={0: []})

        ev = CppAstarRoutingEvaluator(router_factory=_factory)
        result = ev.evaluate_routability({"U1": (0.0, 0.0)}, {"U1": 0.0})
        assert isinstance(result, float)
        assert result == 1.0


# ---------------------------------------------------------------------------
# CppAstarRoutingEvaluator: behavioral tests with monkeypatched inner GA
# ---------------------------------------------------------------------------


class TestEvaluatorBehavior:
    def test_factory_exception_returns_zero(self):
        """A router factory that raises must produce a 0.0 score, not propagate."""

        def _bad_factory(positions, rotations):
            raise RuntimeError("placement infeasible")

        ev = CppAstarRoutingEvaluator(router_factory=_bad_factory)
        result = ev.evaluate_routability({"U1": (0.0, 0.0)}, {"U1": 0.0})
        assert result == 0.0
        assert ev.last_completion_rate == 0.0

    def test_empty_router_returns_one(self):
        """A router with only net 0 → no work to do → 1.0."""

        def _factory(positions, rotations):
            return _FakeRouter(pads={}, nets={0: []})

        ev = CppAstarRoutingEvaluator(router_factory=_factory)
        assert ev.evaluate_routability({}, {}) == 1.0
        assert ev.calls == 1

    def test_inner_ga_exception_returns_zero(self, monkeypatch):
        """If ``run_evolutionary`` raises, the evaluator returns 0.0."""

        pads = {
            ("U1", "1"): _FakePad(x=0.0, y=0.0, net=1),
            ("U2", "1"): _FakePad(x=10.0, y=0.0, net=1),
        }
        router = _FakeRouter(pads=pads, nets={1: [("U1", "1"), ("U2", "1")]})

        def _factory(positions, rotations):
            return router

        # Monkeypatch the inner GA to raise.
        import kicad_tools.router.algorithms.evolutionary as evo_mod

        def _bad_run(**kwargs):
            raise RuntimeError("router crashed")

        monkeypatch.setattr(evo_mod, "run_evolutionary", _bad_run)

        ev = CppAstarRoutingEvaluator(router_factory=_factory)
        assert ev.evaluate_routability({}, {}) == 0.0

    def test_full_routing_returns_one(self, monkeypatch):
        """If the inner GA returns fully-stitched routes, the score is 1.0."""

        pads = {
            ("U1", "1"): _FakePad(x=0.0, y=0.0, net=1),
            ("U2", "1"): _FakePad(x=10.0, y=0.0, net=1),
        }
        router = _FakeRouter(pads=pads, nets={1: [("U1", "1"), ("U2", "1")]})

        def _factory(positions, rotations):
            return router

        # Monkeypatch run_evolutionary to return a fully-stitched route.
        import kicad_tools.router.algorithms.evolutionary as evo_mod

        def _full_run(**kwargs):
            return [
                _FakeRoute(
                    net=1,
                    net_name="NET1",
                    segments=[_FakeSegment(x1=0.0, y1=0.0, x2=10.0, y2=0.0, net=1)],
                    vias=[],
                ),
            ]

        monkeypatch.setattr(evo_mod, "run_evolutionary", _full_run)

        ev = CppAstarRoutingEvaluator(router_factory=_factory)
        assert ev.evaluate_routability({}, {}) == pytest.approx(1.0)
        assert ev.last_routes_count == 1
        assert ev.last_elapsed_seconds >= 0.0

    def test_no_routes_returns_zero(self, monkeypatch):
        """If the inner GA returns an empty list and there are signal nets → 0.0."""

        pads = {
            ("U1", "1"): _FakePad(x=0.0, y=0.0, net=1),
            ("U2", "1"): _FakePad(x=10.0, y=0.0, net=1),
        }
        router = _FakeRouter(pads=pads, nets={1: [("U1", "1"), ("U2", "1")]})

        def _factory(positions, rotations):
            return router

        import kicad_tools.router.algorithms.evolutionary as evo_mod

        monkeypatch.setattr(evo_mod, "run_evolutionary", lambda **kw: [])

        ev = CppAstarRoutingEvaluator(router_factory=_factory)
        assert ev.evaluate_routability({}, {}) == 0.0

    def test_seed_forwarded_to_inner_ga(self, monkeypatch):
        """The configured seed must reach ``run_evolutionary``."""

        pads = {("U1", "1"): _FakePad(x=0.0, y=0.0, net=1)}
        router = _FakeRouter(pads=pads, nets={1: [("U1", "1")]})

        seen_kwargs: dict = {}
        import kicad_tools.router.algorithms.evolutionary as evo_mod

        def _spy(**kwargs):
            seen_kwargs.update(kwargs)
            return []

        monkeypatch.setattr(evo_mod, "run_evolutionary", _spy)

        cfg = RoutingEvaluatorConfig(seed=12345, num_workers=1)
        ev = CppAstarRoutingEvaluator(router_factory=lambda p, r: router, config=cfg)
        ev.evaluate_routability({}, {})
        assert seen_kwargs.get("seed") == 12345
        assert seen_kwargs.get("num_workers") == 1
        assert seen_kwargs.get("timeout") == cfg.timeout_seconds

    def test_determinism_with_fixed_seed(self, monkeypatch):
        """Calling the evaluator twice with the same seed produces the same result.

        We can verify this without invoking the real C++ A* by having the
        spy return a deterministic route list — the contract we want is that
        the *evaluator* doesn't add nondeterminism on top of the inner GA.
        """

        pads = {
            ("U1", "1"): _FakePad(x=0.0, y=0.0, net=1),
            ("U2", "1"): _FakePad(x=10.0, y=0.0, net=1),
        }
        router = _FakeRouter(pads=pads, nets={1: [("U1", "1"), ("U2", "1")]})
        import kicad_tools.router.algorithms.evolutionary as evo_mod

        def _deterministic_run(**kwargs):
            # Half-stitched: U1–midpoint only — partial credit.
            return [
                _FakeRoute(
                    net=1,
                    net_name="NET1",
                    segments=[_FakeSegment(x1=0.0, y1=0.0, x2=10.0, y2=0.0, net=1)],
                    vias=[],
                ),
            ]

        monkeypatch.setattr(evo_mod, "run_evolutionary", _deterministic_run)

        cfg = RoutingEvaluatorConfig(seed=999, num_workers=1)
        ev1 = CppAstarRoutingEvaluator(router_factory=lambda p, r: router, config=cfg)
        ev2 = CppAstarRoutingEvaluator(router_factory=lambda p, r: router, config=cfg)
        r1 = ev1.evaluate_routability({}, {})
        r2 = ev2.evaluate_routability({}, {})
        assert r1 == r2

    def test_default_num_workers_is_one(self):
        """Guard against accidental nested-pool regression (curator note)."""
        cfg = RoutingEvaluatorConfig()
        assert cfg.num_workers == 1, (
            "RoutingEvaluatorConfig.num_workers must default to 1 to avoid "
            "nested ProcessPoolExecutor deadlock when the outer placement GA "
            "already forks workers."
        )

    def test_stats_recorded(self, monkeypatch):
        """Calls counter and last-result fields are populated."""

        pads = {("U1", "1"): _FakePad(x=0.0, y=0.0, net=1)}
        router = _FakeRouter(pads=pads, nets={1: [("U1", "1")]})

        import kicad_tools.router.algorithms.evolutionary as evo_mod

        monkeypatch.setattr(evo_mod, "run_evolutionary", lambda **kw: [])

        ev = CppAstarRoutingEvaluator(router_factory=lambda p, r: router)
        assert ev.calls == 0
        ev.evaluate_routability({}, {})
        assert ev.calls == 1
        ev.evaluate_routability({}, {})
        assert ev.calls == 2
        assert ev.last_elapsed_seconds >= 0.0


# ---------------------------------------------------------------------------
# Speed bar / timeout integration
# ---------------------------------------------------------------------------


class TestTimeoutRespected:
    def test_timeout_forwarded_to_inner_ga(self, monkeypatch):
        """The configured ``timeout_seconds`` must be forwarded as the inner GA's ``timeout`` kwarg."""

        pads = {("U1", "1"): _FakePad(x=0.0, y=0.0, net=1)}
        router = _FakeRouter(pads=pads, nets={1: [("U1", "1")]})

        seen: dict = {}
        import kicad_tools.router.algorithms.evolutionary as evo_mod

        def _spy(**kwargs):
            seen.update(kwargs)
            return []

        monkeypatch.setattr(evo_mod, "run_evolutionary", _spy)

        cfg = RoutingEvaluatorConfig(timeout_seconds=0.5, num_workers=1)
        ev = CppAstarRoutingEvaluator(router_factory=lambda p, r: router, config=cfg)
        ev.evaluate_routability({}, {})
        assert seen.get("timeout") == 0.5

    def test_evaluator_returns_within_inner_timeout(self, monkeypatch):
        """The evaluator must return promptly when the inner GA respects timeout.

        We simulate a "slow inner GA" that itself returns within the timeout
        budget and verify the evaluator's wall-clock cost matches.
        """

        pads = {("U1", "1"): _FakePad(x=0.0, y=0.0, net=1)}
        router = _FakeRouter(pads=pads, nets={1: [("U1", "1")]})

        import kicad_tools.router.algorithms.evolutionary as evo_mod

        def _slow_but_bounded(**kwargs):
            # Honor the timeout the way the real GA does.
            sleep_for = min(0.1, kwargs.get("timeout") or 0.1)
            time.sleep(sleep_for)
            return []

        monkeypatch.setattr(evo_mod, "run_evolutionary", _slow_but_bounded)

        cfg = RoutingEvaluatorConfig(timeout_seconds=0.2, num_workers=1)
        ev = CppAstarRoutingEvaluator(router_factory=lambda p, r: router, config=cfg)
        start = time.monotonic()
        result = ev.evaluate_routability({}, {})
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"Evaluator took {elapsed:.2f}s — too slow"
        assert 0.0 <= result <= 1.0


# ---------------------------------------------------------------------------
# Drift-prevention: monotonicity on a benchmark fixture
# ---------------------------------------------------------------------------


class TestMonotonicity:
    """The evaluator's score should monotonically increase as placement quality improves.

    We don't need a real PCB for this — we use the monkeypatched inner GA
    to return route lists that reflect "more pads connected when components
    are closer together", which is the property the placement GA expects.
    """

    def test_better_placement_scores_higher(self, monkeypatch):
        """Closer pad positions → more connectivity in the simulated GA → higher score."""

        import kicad_tools.router.algorithms.evolutionary as evo_mod

        # 3-pad net at (0, 0), (D, 0), (2D, 0).  The "GA" stitches all pads
        # if D < 5, only U1↔U2 if 5 <= D < 15, none otherwise.

        captured_router = {}

        def _factory(positions, rotations):
            (x1, y1) = positions["U1"]
            (x2, y2) = positions["U2"]
            (x3, y3) = positions["U3"]
            pads = {
                ("U1", "1"): _FakePad(x=x1, y=y1, net=1),
                ("U2", "1"): _FakePad(x=x2, y=y2, net=1),
                ("U3", "1"): _FakePad(x=x3, y=y3, net=1),
            }
            r = _FakeRouter(
                pads=pads,
                nets={1: [("U1", "1"), ("U2", "1"), ("U3", "1")]},
            )
            captured_router["r"] = r
            return r

        def _adaptive_run(**kwargs):
            r = captured_router["r"]
            u1 = r.pads[("U1", "1")]
            u2 = r.pads[("U2", "1")]
            u3 = r.pads[("U3", "1")]
            d12 = abs(u2.x - u1.x)
            d23 = abs(u3.x - u2.x)

            segs = []
            if d12 < 5.0:
                segs.append(_FakeSegment(x1=u1.x, y1=u1.y, x2=u2.x, y2=u2.y, net=1))
            if d23 < 5.0:
                segs.append(_FakeSegment(x1=u2.x, y1=u2.y, x2=u3.x, y2=u3.y, net=1))
            if not segs:
                return []
            return [_FakeRoute(net=1, net_name="NET1", segments=segs, vias=[])]

        monkeypatch.setattr(evo_mod, "run_evolutionary", _adaptive_run)

        cfg = RoutingEvaluatorConfig(seed=0, num_workers=1)
        ev = CppAstarRoutingEvaluator(router_factory=_factory, config=cfg)

        # Three placements with strictly improving connectivity.
        score_far = ev.evaluate_routability(
            {"U1": (0.0, 0.0), "U2": (20.0, 0.0), "U3": (40.0, 0.0)},
            {"U1": 0.0, "U2": 0.0, "U3": 0.0},
        )
        score_mid = ev.evaluate_routability(
            {"U1": (0.0, 0.0), "U2": (3.0, 0.0), "U3": (40.0, 0.0)},
            {"U1": 0.0, "U2": 0.0, "U3": 0.0},
        )
        score_close = ev.evaluate_routability(
            {"U1": (0.0, 0.0), "U2": (3.0, 0.0), "U3": (6.0, 0.0)},
            {"U1": 0.0, "U2": 0.0, "U3": 0.0},
        )
        assert score_far <= score_mid <= score_close
        assert score_close == pytest.approx(1.0)
        assert score_far == 0.0
