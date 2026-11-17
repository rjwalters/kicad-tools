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


# ---------------------------------------------------------------------------
# C++ build-version invariant
# ---------------------------------------------------------------------------
#
# The Python ``_REQUIRED_CPP_BUILD_VERSION`` must equal the C++
# ``ROUTER_CPP_BUILD_VERSION`` constant.  Both sides need to bump together
# whenever the C++ binding surface gains/loses/renames a symbol or struct
# field; otherwise stale .so files silently drift and produce ABI mismatches.
# A* tie-break determinism is covered by ``tests/router/test_astar_tiebreak_determinism.py``
# (Issue #3144 / PR #3192) -- this file only owns the version-match assertion.


class TestCppPathfinderBuildVersionBumped:
    """Cross-language invariant: Python ``_REQUIRED_CPP_BUILD_VERSION`` equals
    the C++ ``ROUTER_CPP_BUILD_VERSION`` constant.

    Both sides must be bumped together whenever the C++ binding surface
    gains/loses/renames a symbol or struct field; otherwise stale .so files
    silently drift and produce ABI mismatches (the bindings.cpp / .so
    coordination protocol from Issue #2501).
    """

    def test_required_build_version_matches_types_hpp(self):
        """Python-side required version equals the C++-side constant.

        This is a deliberately fragile assertion: if either side bumps
        without the other, this test fails and the next ``kct
        build-native`` will surface the mismatch with an actionable
        error.
        """
        import re
        from pathlib import Path

        from kicad_tools.router import cpp_backend

        types_hpp = Path(cpp_backend.__file__).resolve().parent / "cpp" / "include" / "types.hpp"
        text = types_hpp.read_text(encoding="utf-8")
        m = re.search(r"constexpr int ROUTER_CPP_BUILD_VERSION\s*=\s*(\d+)\s*;", text)
        assert m is not None, (
            "Could not find ROUTER_CPP_BUILD_VERSION in types.hpp -- if you "
            "renamed the constant, update this test and cpp_backend.py too."
        )
        cpp_version = int(m.group(1))
        py_version = cpp_backend._REQUIRED_CPP_BUILD_VERSION
        assert cpp_version == py_version, (
            f"types.hpp ROUTER_CPP_BUILD_VERSION ({cpp_version}) does not "
            f"match cpp_backend._REQUIRED_CPP_BUILD_VERSION ({py_version}). "
            "Bump both constants together when changing the C++ binding "
            "surface (added/removed/renamed symbols, struct fields)."
        )


# ---------------------------------------------------------------------------
# Issue #3272: deterministic-UUID toggle for Segment / Via / Zone S-exprs
# ---------------------------------------------------------------------------
#
# Before #3272 the routed PCB was geometry-deterministic under ``seed=N``
# (the routing decisions were reproducible) BUT the file-level MD5 still
# drifted run-to-run because :meth:`Segment.to_sexp`, :meth:`Via.to_sexp`
# and the zone generator's UUID factory all emitted ``uuid.uuid4()`` --
# which reads ``os.urandom`` independently of the seeded global RNG.  As
# a result the board-06 determinism smoke harness produced
# false-positive failures (MD5 mismatch on byte-different but
# semantically-identical files) and the regression test at
# ``tests/router/test_board06_determinism.py`` could not assert the
# stronger "identical PCB content" invariant.
#
# These tests pin the toggle behaviour at the unit-test level so a
# future refactor that re-introduces unseeded ``uuid.uuid4()`` calls in
# the router primitives or zone generator fails fast.


class TestDeterministicUuidToggle:
    """Issue #3272: ``enable_deterministic_uuids`` makes Segment/Via UUIDs reproducible."""

    def test_segment_uuid_deterministic_under_seeded_rng(self) -> None:
        """Two ``Segment.to_sexp`` calls with the same seeded RNG produce identical UUIDs.

        Without #3272 the per-segment UUID came from ``uuid.uuid4()``
        (i.e. ``os.urandom``) and was independent of the seeded global
        RNG, so the routed PCB MD5 drifted even when routing was
        otherwise reproducible.
        """
        from kicad_tools.router.layers import Layer
        from kicad_tools.router.primitives import (
            Segment,
            enable_deterministic_uuids,
            reset_deterministic_uuids,
        )

        try:
            enable_deterministic_uuids(True)

            random.seed(42)
            sexp_a = Segment(0, 0, 1, 0, 0.2, Layer.F_CU, net=1).to_sexp()

            random.seed(42)
            sexp_b = Segment(0, 0, 1, 0, 0.2, Layer.F_CU, net=1).to_sexp()
        finally:
            reset_deterministic_uuids()

        assert sexp_a == sexp_b, (
            "Segment.to_sexp must emit identical UUIDs when the global "
            "RNG is re-seeded and the deterministic-UUID toggle is on. "
            "If this fails, primitives._make_uuid is no longer reading "
            "from random.getrandbits -- Issue #3272 has regressed."
        )

    def test_via_uuid_deterministic_under_seeded_rng(self) -> None:
        """Two ``Via.to_sexp`` calls with the same seeded RNG produce identical UUIDs."""
        from kicad_tools.router.layers import Layer
        from kicad_tools.router.primitives import (
            Via,
            enable_deterministic_uuids,
            reset_deterministic_uuids,
        )

        try:
            enable_deterministic_uuids(True)

            random.seed(42)
            sexp_a = Via(
                x=1.0,
                y=2.0,
                drill=0.3,
                diameter=0.6,
                layers=(Layer.F_CU, Layer.B_CU),
                net=1,
            ).to_sexp()

            random.seed(42)
            sexp_b = Via(
                x=1.0,
                y=2.0,
                drill=0.3,
                diameter=0.6,
                layers=(Layer.F_CU, Layer.B_CU),
                net=1,
            ).to_sexp()
        finally:
            reset_deterministic_uuids()

        assert sexp_a == sexp_b, (
            "Via.to_sexp must emit identical UUIDs when the global RNG "
            "is re-seeded and the deterministic-UUID toggle is on. "
            "Issue #3272 regression."
        )

    def test_toggle_off_yields_distinct_uuids(self) -> None:
        """When the toggle is off, ``Segment.to_sexp`` calls emit different UUIDs.

        Preserves the historical ``uuid.uuid4()`` semantics for callers
        that haven't opted into deterministic routing (i.e. routes
        created without ``seed=...``).
        """
        from kicad_tools.router.layers import Layer
        from kicad_tools.router.primitives import (
            Segment,
            reset_deterministic_uuids,
        )

        # Make sure we're starting from a known state -- another test
        # in the session may have left the toggle on.
        reset_deterministic_uuids()

        sexp_a = Segment(0, 0, 1, 0, 0.2, Layer.F_CU, net=1).to_sexp()
        sexp_b = Segment(0, 0, 1, 0, 0.2, Layer.F_CU, net=1).to_sexp()

        # Pull the UUIDs out for a clearer diagnostic when this fails.
        import re

        uuid_a = re.search(r'uuid "([^"]+)"', sexp_a).group(1)
        uuid_b = re.search(r'uuid "([^"]+)"', sexp_b).group(1)
        assert uuid_a != uuid_b, (
            f"Default Segment.to_sexp must emit a fresh UUID per call "
            f"(toggle off).  Got identical UUIDs: {uuid_a}.  If you "
            f"flipped the default to deterministic, downstream callers "
            f"that rely on uniqueness (e.g. KiCad's element ID resolver) "
            f"may silently collide."
        )

    def test_zone_uuid_factory_deterministic_under_seeded_rng(self) -> None:
        """Zone UUIDs track the deterministic-UUID toggle like Segment / Via.

        Without this, ``auto_create_zones_for_pour_nets`` would still
        introduce file-level non-determinism in board-06's output even
        with a seeded router, because zones come from
        :mod:`kicad_tools.zones.generator` which has its own UUID
        factory.
        """
        from kicad_tools.router.primitives import (
            enable_deterministic_uuids,
            reset_deterministic_uuids,
        )
        from kicad_tools.zones.generator import _zone_uuid_factory

        try:
            enable_deterministic_uuids(True)

            random.seed(42)
            u1 = _zone_uuid_factory()
            u2 = _zone_uuid_factory()

            random.seed(42)
            u1_replay = _zone_uuid_factory()
            u2_replay = _zone_uuid_factory()
        finally:
            reset_deterministic_uuids()

        assert u1 == u1_replay and u2 == u2_replay, (
            "Zone UUID factory must honour the router primitives "
            "deterministic-UUID toggle so board-06's auto-pour zones "
            "produce a byte-identical .kicad_pcb across runs.  Issue "
            "#3272 regression."
        )
        assert u1 != u2, (
            "Successive calls within a single seeded run must still "
            "produce distinct UUIDs (different positions in the RNG "
            "stream) -- otherwise every zone would collide on the "
            "same UUID and KiCad would treat them as the same element."
        )

    def test_route_all_negotiated_enables_uuid_toggle_under_seed(self) -> None:
        """Passing ``seed=...`` to ``route_all_negotiated`` activates the toggle.

        End-to-end wiring check: the user-visible API for opting into
        determinism is ``router.route_all_negotiated(seed=N)``.  That
        call MUST flip the module-level toggle on as a side effect, so
        any downstream ``to_sexp()`` emission produces a deterministic
        UUID.  This is the integration site #3272 cares about.
        """
        from kicad_tools.router.primitives import (
            is_deterministic_uuids_enabled,
            reset_deterministic_uuids,
        )

        # Ensure we don't pre-bias the assertion below.
        reset_deterministic_uuids()
        assert not is_deterministic_uuids_enabled(), (
            "test pre-condition: toggle should be off at start"
        )

        router = _make_seed_test_router()
        router.route_all_negotiated(max_iterations=1, seed=42)

        assert is_deterministic_uuids_enabled(), (
            "route_all_negotiated(seed=...) must activate the "
            "deterministic-UUID toggle (Issue #3272).  Without this "
            "the routed PCB's segment / via UUIDs continue to drift "
            "across runs even when routing decisions are reproducible."
        )

        # Clean up so we don't bleed state into other tests in the
        # session.
        reset_deterministic_uuids()

    def test_route_all_negotiated_leaves_toggle_off_without_seed(self) -> None:
        """Default behaviour (no seed) does NOT flip the toggle on.

        Guards against an accidental refactor that flips the toggle
        unconditionally.  Users who didn't opt into determinism
        should still see fresh ``uuid.uuid4()``-derived UUIDs.
        """
        from kicad_tools.router.primitives import (
            is_deterministic_uuids_enabled,
            reset_deterministic_uuids,
        )

        reset_deterministic_uuids()
        router = _make_seed_test_router()
        router.route_all_negotiated(max_iterations=1)

        assert not is_deterministic_uuids_enabled(), (
            "route_all_negotiated() WITHOUT seed must NOT activate "
            "the deterministic-UUID toggle.  If this fails, the "
            "default code path silently makes every routed PCB "
            "reproducible -- masking future regressions in the "
            "unseeded route path."
        )
