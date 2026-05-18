"""Tests for router determinism via ``--seed`` (Issue #2589).

Verifies that seeding Python's global ``random`` module makes the
unseeded escape-strategy callsites in
``kicad_tools.router.algorithms.negotiated`` (and the MST trial shuffle
in ``kicad_tools.router.core``) deterministic.

Rationale
=========

The CI-visible symptom on issue #2589 was board 03 producing 0-3 DRC
errors run-to-run with ``kct route --backend python``.  The root cause
was unseeded ``random.shuffle`` / ``random.sample`` calls in:

* ``algorithms/negotiated.py::_escape_shuffle_order``
* ``algorithms/negotiated.py::_escape_random_subset``
* ``algorithms/negotiated.py::_escape_full_reorder``
* ``core.py`` MST fine-grid trial loop

These calls all consume the process-wide ``random`` state, which is
otherwise seeded from ``os.urandom`` and varies per process.

This test suite verifies that:

1. ``random.seed(N)`` makes ``random.shuffle`` reproducible on a fixed
   input -- a direct check that the primary mechanism works.
2. The ``kct route`` CLI exposes ``--seed`` and the value reaches the
   ``main()`` function (smoke test of the wiring).

The full end-to-end "two ``kct route`` invocations produce identical
output" verification is left to manual / CI testing because routing
even a small board takes 30+ seconds, which is too slow for the unit
test suite.  The smoke test below at least confirms the seed is
plumbed through to ``route_main``.
"""

from __future__ import annotations

import random
from pathlib import Path
from unittest.mock import patch


def test_global_random_seed_is_reproducible():
    """``random.seed(N)`` makes ``random.shuffle`` byte-identical.

    This is the underlying mechanism the ``--seed`` flag relies on:
    after ``random.seed(42)``, two calls to ``random.shuffle`` on the
    same input produce the same permutation.  If this ever breaks
    (e.g., Python changes its PRNG implementation in a way that
    invalidates seed reproducibility), the determinism guarantee of
    ``kct route --seed`` no longer holds.
    """
    items_a = list(range(20))
    items_b = list(range(20))

    random.seed(42)
    random.shuffle(items_a)

    random.seed(42)
    random.shuffle(items_b)

    assert items_a == items_b, (
        "random.seed(N) must make random.shuffle reproducible -- "
        "this is the core mechanism --seed relies on"
    )


def test_global_random_seed_differs_between_seeds():
    """Different seeds produce different shuffles.

    This guards against the seed value being ignored (e.g., if the CLI
    layer reads ``--seed`` but never calls ``random.seed``).  If two
    different seed values shuffled the same list to the same
    permutation, the seed parameter would be a no-op.
    """
    items_a = list(range(20))
    items_b = list(range(20))

    random.seed(42)
    random.shuffle(items_a)

    random.seed(7)
    random.shuffle(items_b)

    assert items_a != items_b, (
        "different seeds should produce different shuffles on a non-trivial input (20 items)"
    )


def test_negotiated_random_callsites_use_global_random():
    """The four escape/MST random callsites import the ``random`` module.

    This test guards against accidental refactoring that introduces a
    per-instance ``random.Random()`` without also threading the seed
    through.  If any escape strategy is rewritten to use a local
    ``random.Random(seed=...)``, the ``--seed`` CLI flag would silently
    stop working for that strategy -- the test fails fast and the
    refactor must also update the CLI plumbing.

    The check is intentionally lightweight (substring match): a deeper
    AST inspection would be brittle to formatting changes.
    """
    from kicad_tools.router import core
    from kicad_tools.router.algorithms import negotiated

    neg_src = Path(negotiated.__file__).read_text(encoding="utf-8")
    core_src = Path(core.__file__).read_text(encoding="utf-8")

    # The four sites the curator's investigation identified.
    assert "random.shuffle(shuffled)" in neg_src, (
        "expected _escape_shuffle_order to call random.shuffle(shuffled); "
        "if you moved this to a per-instance RNG, update route_cmd.py too"
    )
    assert "random.sample(nets_to_reroute" in neg_src, (
        "expected _escape_random_subset to call "
        "random.sample(nets_to_reroute, ...); if you moved this to a "
        "per-instance RNG, update route_cmd.py too"
    )
    assert "random.shuffle(remaining)" in neg_src, (
        "expected _escape_full_reorder to call random.shuffle(remaining); "
        "if you moved this to a per-instance RNG, update route_cmd.py too"
    )
    assert "random.shuffle(trial_pads)" in core_src, (
        "expected MST trial loop in core.py to call "
        "random.shuffle(trial_pads); if you moved this to a "
        "per-instance RNG, update route_cmd.py too"
    )


def test_route_cmd_accepts_seed_argument():
    """``kct route --seed N`` is accepted by the argparse parser.

    Tests the CLI surface: the route command's argparse accepts a
    ``--seed`` integer.  Uses a malformed PCB path so the parse
    succeeds but the load fails fast -- we only want to exercise the
    argparse layer.
    """
    from kicad_tools.cli.route_cmd import main as route_main

    # Use a non-existent pcb path; main() will fail at the
    # "file not found" check, but parse_args must accept --seed first.
    # If --seed weren't registered, argparse would exit(2) before the
    # path check.
    rc = route_main(["/nonexistent/path/file.kicad_pcb", "--seed", "42"])
    # Expected: rc == 1 (file not found), NOT exit code 2 (parse error)
    assert rc == 1, (
        f"expected route main() to return 1 (file not found) after "
        f"accepting --seed; got {rc}.  If rc == 2, argparse rejected "
        f"--seed -- check the add_argument call in route_cmd.py."
    )


def test_route_cmd_seed_is_optional():
    """``--seed`` is not required.

    Default behaviour (no ``--seed``) must continue to work, otherwise
    every existing caller of ``kct route`` would suddenly need to pass
    a seed.
    """
    from kicad_tools.cli.route_cmd import main as route_main

    rc = route_main(["/nonexistent/path/file.kicad_pcb"])
    # Same as above: rc == 1 (file not found), not 2 (parse error)
    assert rc == 1, (
        f"expected route main() to accept invocation with no --seed; "
        f"got rc={rc} (parse error would be 2)"
    )


def test_route_cmd_calls_random_seed_when_seed_provided():
    """When ``--seed N`` is passed, ``random.seed(N)`` is called.

    Patches ``random.seed`` and verifies it's invoked with the right
    integer.  This is the direct check that the CLI wiring connects
    ``--seed`` to the global RNG -- not just that the argument is
    accepted.
    """
    from kicad_tools.cli import route_cmd

    with patch.object(route_cmd.random, "seed") as mock_seed:
        # Invoke with a bogus PCB so we bail out early (after the
        # seed call but before any router work).
        route_cmd.main(["/nonexistent/path/file.kicad_pcb", "--seed", "12345"])

    # Verify random.seed was called at least once with 12345.
    seed_calls = [c.args[0] for c in mock_seed.call_args_list if c.args]
    assert 12345 in seed_calls, (
        f"expected random.seed(12345) call when --seed 12345 is passed; "
        f"observed seed calls: {seed_calls}"
    )


def test_route_cmd_does_not_call_random_seed_when_seed_omitted():
    """When ``--seed`` is omitted, ``random.seed`` is NOT called.

    Preserves existing run-to-run variance behaviour for users who
    don't opt into determinism.  If we silently seeded on every run,
    the route output would be reproducible by accident -- but only
    until someone reordered the route command's initialization,
    making the regression hard to spot.
    """
    from kicad_tools.cli import route_cmd

    with patch.object(route_cmd.random, "seed") as mock_seed:
        route_cmd.main(["/nonexistent/path/file.kicad_pcb"])

    assert mock_seed.call_count == 0, (
        f"expected random.seed to NOT be called when --seed is omitted; "
        f"got {mock_seed.call_count} call(s).  This would silently make "
        f"every route run deterministic, masking future regressions in "
        f"the unseeded code path."
    )


# ---------------------------------------------------------------------------
# Issue #3039: route_all_negotiated(seed=...) API
# ---------------------------------------------------------------------------
#
# Issue #2589 (above) seeded only the GLOBAL random module from the CLI layer.
# That fix did not cover the per-Autorouter perturbation RNG
# (``self._perturbation_rng``, used by the stochastic-perturbation escape from
# Issue #2334) -- so even with ``--seed 42``, the negotiated routing path
# remained non-deterministic in the perturbation-activated branch.  PR #3034
# and PR #3036 shipped boards whose verification numbers did not survive a
# re-run; the curator's investigation traced this to the missing seed plumb
# on the ``route_all_negotiated`` public API.
#
# The tests below verify the Issue #3039 fix: ``route_all_negotiated`` now
# accepts a ``seed`` parameter that flows into both the perturbation RNG and
# the global module, producing identical outcome tuples across runs.


from kicad_tools.router.core import Autorouter  # noqa: E402  (test-section import)


def _make_seed_test_router() -> Autorouter:
    """Build a small router with overlapping nets that exercise the
    negotiated rip-up loop.  Three two-pad nets whose obvious routes cross
    -- forces the loop to iterate at least once.
    """
    router = Autorouter(width=40.0, height=30.0)

    # Net 1: horizontal across the board.
    router.add_component(
        "R1",
        [{"number": "1", "x": 5.0, "y": 15.0, "net": 1, "net_name": "NET1"}],
    )
    router.add_component(
        "R2",
        [{"number": "1", "x": 35.0, "y": 15.0, "net": 1, "net_name": "NET1"}],
    )

    # Net 2: vertical across the board (crosses net 1).
    router.add_component(
        "R3",
        [{"number": "1", "x": 20.0, "y": 5.0, "net": 2, "net_name": "NET2"}],
    )
    router.add_component(
        "R4",
        [{"number": "1", "x": 20.0, "y": 25.0, "net": 2, "net_name": "NET2"}],
    )

    # Net 3: diagonal, adds congestion.
    router.add_component(
        "R5",
        [{"number": "1", "x": 10.0, "y": 10.0, "net": 3, "net_name": "NET3"}],
    )
    router.add_component(
        "R6",
        [{"number": "1", "x": 30.0, "y": 20.0, "net": 3, "net_name": "NET3"}],
    )

    return router


def _outcome_tuple(router: Autorouter) -> tuple[int, int, int]:
    """Reduce a router's post-route state to the AC tuple from Issue #3039:
    ``(nets_routed, total_segments, total_vias)``.  We deliberately drop
    ``completion_pct`` because it is a derived ratio of ``nets_routed`` and
    ``nets_to_route`` and adds no independent information.
    """
    stats = router.get_statistics()
    return (
        int(stats.get("nets_routed", 0)),
        int(stats.get("segments", 0)),
        int(stats.get("vias", 0)),
    )


class TestRouteAllNegotiatedSeedParameter:
    """Issue #3039: ``route_all_negotiated`` exposes ``seed`` kwarg."""

    def test_route_all_negotiated_accepts_seed_kwarg(self):
        """Signature change: ``seed`` is now a keyword arg, not TypeError."""
        router = _make_seed_test_router()
        routes = router.route_all_negotiated(max_iterations=2, seed=42)
        assert isinstance(routes, list)

    def test_seed_is_stashed_on_router_instance(self):
        """``seed`` flows into ``self._perturbation_seed`` so
        ``_activate_perturbation`` can derive per-episode RNG seeds from it.
        """
        router = _make_seed_test_router()
        assert router._perturbation_seed is None  # __init__ default
        router.route_all_negotiated(max_iterations=1, seed=12345)
        assert router._perturbation_seed == 12345

    def test_seed_none_preserves_legacy_behaviour(self):
        """``seed=None`` (default) leaves ``_perturbation_seed`` cleared.
        Non-regression guard from Issue #3039: callers that do NOT opt in
        must keep today's non-deterministic-trigger-timing behaviour.
        """
        router = _make_seed_test_router()
        router.route_all_negotiated(max_iterations=1)
        assert router._perturbation_seed is None


class TestRouteAllNegotiatedDeterminism:
    """Issue #3039 AC: identical outcome tuple across 3 seeded runs."""

    def test_same_seed_produces_same_outcome_tuple(self):
        """Three consecutive ``route_all_negotiated(seed=42)`` runs on the
        same fixture must produce the same ``(nets_routed, segments, vias)``
        tuple.  This is the acceptance criterion from Issue #3039.
        """
        outcomes: list[tuple[int, int, int]] = []
        for _ in range(3):
            router = _make_seed_test_router()
            router.route_all_negotiated(max_iterations=3, seed=42)
            outcomes.append(_outcome_tuple(router))

        assert outcomes[0] == outcomes[1] == outcomes[2], (
            f"Three runs with seed=42 produced different outcomes: {outcomes}. "
            "route_all_negotiated must be deterministic when seeded."
        )

    def test_different_seeds_produce_distinct_perturbation_rng_state(self):
        """Sanity check that the seed actually has an effect.

        We do not assert outcome tuples *must* differ for distinct seeds
        (this fixture may converge regardless of perturbation ordering),
        but the underlying perturbation RNG state must differ -- otherwise
        the seed parameter is a no-op.
        """
        router_a = _make_seed_test_router()
        router_a.route_all_negotiated(max_iterations=1, seed=42)

        router_b = _make_seed_test_router()
        router_b.route_all_negotiated(max_iterations=1, seed=99)

        assert router_a._perturbation_seed != router_b._perturbation_seed
        sample_a = router_a._perturbation_rng.random()
        sample_b = router_b._perturbation_rng.random()
        assert sample_a != sample_b, (
            "Different seeds (42 vs 99) produced identical perturbation RNG "
            "samples -- the seed is not propagating to self._perturbation_rng."
        )

    def test_seed_kwarg_re_seeds_global_random_for_mst_shuffle(self):
        """``seed`` re-seeds the global ``random`` module so the MST
        trial-pad shuffle (``core.py:~11475``, used by the multi-resolution
        path) becomes deterministic.  Verified indirectly: post-route
        ``random.random()`` samples match across two seeded runs.
        """
        router1 = _make_seed_test_router()
        router1.route_all_negotiated(max_iterations=1, seed=7)
        sample1 = random.random()

        router2 = _make_seed_test_router()
        router2.route_all_negotiated(max_iterations=1, seed=7)
        sample2 = random.random()

        assert sample1 == sample2, (
            "random.random() draw after route_all_negotiated(seed=7) "
            "must be identical across two invocations -- otherwise the "
            "seed did not propagate to the global RNG that drives the "
            "MST trial shuffle and negotiated escape strategies."
        )


class TestActivatePerturbationSeed:
    """``_activate_perturbation`` honors the stashed seed when present."""

    def test_activate_perturbation_seed_none_uses_legacy_derivation(self):
        """Without a stashed seed, ``_activate_perturbation(n)`` re-seeds
        the RNG with ``Random(n * 7 + 13)`` -- the pre-Issue-#3039 formula.
        """
        router = Autorouter(width=20.0, height=20.0)
        assert router._perturbation_seed is None

        router._activate_perturbation(stagnation_count=5)
        sample = router._perturbation_rng.random()

        ref = random.Random(5 * 7 + 13)
        assert sample == ref.random()

    def test_activate_perturbation_seed_set_folds_seed_into_derivation(self):
        """With a stashed seed S, ``_activate_perturbation(n)`` re-seeds
        the RNG with ``Random(S + n * 7 + 13)`` -- distinct per stagnation
        episode but deterministic across runs.
        """
        router = Autorouter(width=20.0, height=20.0)
        router._perturbation_seed = 1000

        router._activate_perturbation(stagnation_count=5)
        sample = router._perturbation_rng.random()

        ref = random.Random(1000 + 5 * 7 + 13)
        assert sample == ref.random()

    def test_activate_perturbation_distinct_episodes_use_distinct_rngs(self):
        """Two stagnation episodes (different ``stagnation_count`` values)
        produce different RNG streams even when seed is fixed -- the
        escape-strategy variety guarantee from Issue #2334.
        """
        router = Autorouter(width=20.0, height=20.0)
        router._perturbation_seed = 42

        router._activate_perturbation(stagnation_count=2)
        sample_episode_a = router._perturbation_rng.random()

        router._activate_perturbation(stagnation_count=3)
        sample_episode_b = router._perturbation_rng.random()

        assert sample_episode_a != sample_episode_b, (
            "Distinct stagnation_count values must seed distinct RNG streams "
            "so successive perturbation episodes explore different orderings."
        )
