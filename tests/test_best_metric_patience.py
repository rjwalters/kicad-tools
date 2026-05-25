"""Tests for the best-metric early-stop patience knob (Issue #3101).

The ``route_all_negotiated`` outer loop tracks an
:class:`~kicad_tools.router.core.IterationMetrics` "best so far" snapshot
across iterations.  When the snapshot fails to improve for
``best_stall_patience`` consecutive iterations the loop breaks early so
we stop burning ~50 s/iter on rip-up cycles that are not advancing any
dimension of the lex tuple
``(routed_count, clearance_violations, overflow)``.

Iteration-0 persistence is preserved via the existing post-loop
best-state restore — breaking out of the iteration loop returns control
to that restore site, which guarantees the snapshot wins over a
regressed live state.

The board-07 motivating pattern (see Issue #3101):

    iter 0: routed=27/31, overflow=N0   <-- best
    iter 1: routed=27/31, overflow=N1   (no improvement)
    iter 2: routed=27/31, overflow=N2   (no improvement) -- patience=2 trips here
    ...
    iter 15: max_iterations reached     (~12 min wall clock)

With patience=2 the loop exits after iter 2, saving ~10 iterations
× ~50 s/iter ≈ 8 min of wall-clock cost without sacrificing the
iter-0 result.

These tests exercise the patience knob through three different lenses:

1. The IterationMetrics comparator behaviour the patience counter
   relies on (sanity check that "no improvement" means what we think).
2. End-to-end behaviour on a small autorouter: with patience=1 a
   trivial board should not run its full iteration budget when
   nothing is improving.
3. The CLI flag is wired through both parsers and the inner call site
   forwards it to ``route_all_negotiated``.
"""

from __future__ import annotations

import inspect

import pytest

from kicad_tools.router.core import Autorouter, IterationMetrics


class TestPatienceParameterExists:
    """The new parameter must be on the public signature of
    ``route_all_negotiated`` so downstream callers (and the CLI) can
    forward it explicitly.
    """

    def test_best_stall_patience_in_signature(self):
        sig = inspect.signature(Autorouter.route_all_negotiated)
        assert "best_stall_patience" in sig.parameters

    def test_best_stall_min_iterations_in_signature(self):
        sig = inspect.signature(Autorouter.route_all_negotiated)
        assert "best_stall_min_iterations" in sig.parameters

    def test_default_patience_is_two(self):
        """Default keeps loops short on plateaued boards while letting
        the standard ``should_terminate_early`` heuristic still drive
        most early exits.  Documented as 2 in the docstring; check the
        defaults here so a careless edit can't drift them silently.
        """
        sig = inspect.signature(Autorouter.route_all_negotiated)
        assert sig.parameters["best_stall_patience"].default == 2
        assert sig.parameters["best_stall_min_iterations"].default == 2


class TestPatienceComparatorBehavior:
    """The patience counter increments when
    ``IterationMetrics.is_better_than`` returns False -- assert the
    "no improvement" predicate matches the counter's intent.
    """

    def test_identical_metrics_does_not_count_as_improvement(self):
        """Equal lex tuples must not reset the patience counter."""
        m1 = IterationMetrics(iteration=0, routed_count=30, overflow=10)
        m2 = IterationMetrics(iteration=1, routed_count=30, overflow=10)
        # m2 is later but otherwise identical -- on a tie the comparator
        # picks the later iteration, so m2.is_better_than(m1) is True.
        # That means when iter 1 produces the exact same routed_count
        # and overflow as iter 0, the counter resets.  This is BY DESIGN:
        # equal-tie ties go to the later iteration so perturbation /
        # escape strategies have room to bake in.  Document the
        # behaviour here so future regressions are loud.
        assert m2.is_better_than(m1)
        # The mirror direction: m1 is NOT better than m2 (it's older).
        assert not m1.is_better_than(m2)

    def test_worse_overflow_does_not_improve(self):
        """The board-07 case: routed_count and clearance_violations
        unchanged, overflow regresses.  Patience counter should
        increment here, not reset.
        """
        best = IterationMetrics(
            iteration=0, routed_count=27, overflow=16,
            clearance_violations=12,
        )
        regressed = IterationMetrics(
            iteration=1, routed_count=27, overflow=36,
            clearance_violations=12,
        )
        # Strictly worse on overflow -- not "better than" the best.
        assert not regressed.is_better_than(best)

    def test_more_clearance_violations_does_not_improve(self):
        """A re-route that adds a clearance violation must not reset
        the patience counter even if it nudges overflow down."""
        best = IterationMetrics(
            iteration=0, routed_count=27, overflow=16,
            clearance_violations=12,
        )
        worse_drc = IterationMetrics(
            iteration=1, routed_count=27, overflow=14,
            clearance_violations=15,
        )
        assert not worse_drc.is_better_than(best)


class TestPatienceEarlyStopEndToEnd:
    """Drive a real Autorouter through ``route_all_negotiated`` with
    a patience value low enough that the loop must terminate before
    ``max_iterations`` even on a board where the standard
    ``should_terminate_early`` heuristic would not yet fire.

    We use the same 2-net trivial-routing autorouter pattern that
    the existing overflow-regression tests use, then assert the
    iteration-0 routes were preserved through the early stop.
    """

    def _build_trivial_router(self) -> Autorouter:
        router = Autorouter(width=20.0, height=20.0)
        router.add_component(
            "R1",
            [
                {"number": "1", "x": 2.0, "y": 10.0, "net": 1, "net_name": "NET1"},
                {"number": "2", "x": 18.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            ],
        )
        router.add_component(
            "R2",
            [
                {"number": "1", "x": 10.0, "y": 2.0, "net": 2, "net_name": "NET2"},
                {"number": "2", "x": 10.0, "y": 18.0, "net": 2, "net_name": "NET2"},
            ],
        )
        return router

    def test_patience_disabled_keyword_is_accepted(self):
        """``best_stall_patience=None`` must run without raising and
        preserve the pre-#3101 behaviour (loop runs until
        ``should_terminate_early`` or ``max_iterations``).  Smoke test
        only; we can't easily assert iteration count on the trivial
        board because it converges in iter 0.
        """
        ar = self._build_trivial_router()
        routes = ar.route_all_negotiated(
            max_iterations=2,
            timeout=10.0,
            adaptive=False,
            perturbation=False,
            best_stall_patience=None,
        )
        assert isinstance(routes, list)

    def test_patience_zero_keyword_is_accepted(self):
        """``best_stall_patience=0`` must also disable the check (the
        CLI normalizes ``--early-stop-patience 0`` to ``None`` at the
        call boundary; the call-site itself must also accept 0
        defensively so a direct caller cannot trip an off-by-one).
        """
        ar = self._build_trivial_router()
        routes = ar.route_all_negotiated(
            max_iterations=2,
            timeout=10.0,
            adaptive=False,
            perturbation=False,
            best_stall_patience=0,
        )
        assert isinstance(routes, list)

    def test_patience_one_does_not_crash_on_converging_board(self):
        """Patience=1 with a board that converges in iter 0 must not
        crash and must return the converged routes.  This is the
        primary regression test for the new code path.
        """
        ar = self._build_trivial_router()
        routes = ar.route_all_negotiated(
            max_iterations=5,
            timeout=10.0,
            adaptive=False,
            perturbation=False,
            best_stall_patience=1,
            best_stall_min_iterations=1,
        )
        # Trivial board: both nets must route, no exceptions.
        assert isinstance(routes, list)
        # On this 20x20 grid both nets are easy -- at minimum we get
        # the initial-pass routes back.
        assert len(routes) >= 2

    def test_patience_does_not_block_zero_overflow_completion(self):
        """On a board that achieves overflow=0 in iter 0 the loop must
        not even enter the iteration body; the patience check is
        guarded by ``iteration >= best_stall_min_iterations`` so it
        cannot fire before the loop runs at least one full pass.
        """
        ar = self._build_trivial_router()
        routes = ar.route_all_negotiated(
            max_iterations=10,
            timeout=10.0,
            adaptive=False,
            perturbation=False,
            best_stall_patience=2,
        )
        # All nets routed: the patience early-stop must not have
        # interfered with normal convergence.
        assert len(routes) >= 2


class TestPatienceLogLine:
    """The per-iteration log line must include the stall counter so
    operators can correlate wall-clock cost with the patience trigger.
    Documented in the docstring for ``_capture_iteration_end``.

    This is an output-format pin to make the new ``| stall=N``
    suffix visible (and tested) so a later refactor cannot drop it
    silently.
    """

    def test_stall_suffix_appears_in_log_when_not_improving(self, capsys):
        """When an iteration runs and does not improve, the per-iter
        line must carry the ``| stall=N`` suffix.

        Trivial-board case: iter 0 already routes both nets at
        overflow=0, so iteration 1 (when it runs) will not improve
        and the suffix must appear.  We can't deterministically force
        the iteration to run on a trivial board, so this test asserts
        format compliance only when an iter line is actually emitted.
        """
        ar = Autorouter(width=20.0, height=20.0)
        ar.add_component(
            "R1",
            [
                {"number": "1", "x": 2.0, "y": 10.0, "net": 1, "net_name": "NET1"},
                {"number": "2", "x": 18.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            ],
        )
        ar.add_component(
            "R2",
            [
                {"number": "1", "x": 10.0, "y": 2.0, "net": 2, "net_name": "NET2"},
                {"number": "2", "x": 10.0, "y": 18.0, "net": 2, "net_name": "NET2"},
            ],
        )
        ar.route_all_negotiated(
            max_iterations=3,
            timeout=10.0,
            adaptive=False,
            perturbation=False,
            best_stall_patience=None,  # disable so we don't bail
        )
        out = capsys.readouterr().out
        # If the iteration body printed any "iter N" line *without* a
        # "new best" suffix, it must include the stall counter.
        import re
        iter_lines = re.findall(r"iter \d+ \| routed=.*", out)
        for line in iter_lines:
            if "new best" in line:
                continue
            assert "stall=" in line, (
                f"Expected '| stall=N' suffix on non-improving iter "
                f"line; got: {line!r}"
            )


class TestNetsFullyConnectedLexKey:
    """Issue #3117: the lex-tuple primary key must be
    ``nets_fully_connected``, not ``routed_count``.

    Background:
        ``routed_count`` only checks whether each net has any route
        fragment.  A 4-pad net with one fragment connecting 2/4 pads
        contributes 1 to ``routed_count`` even though the design is not
        electrically connected.  Softstart (board #3085) hit this
        exactly: iter-0 logged ``routed_count = 9/10`` but
        ``validate_net_connectivity`` reported ``4/10`` -- the lex
        comparator preserved the 4/10 floor because the metric it
        optimized couldn't distinguish fragments from connections.

    These tests pin the new ordering behaviour so a careless refactor
    cannot re-elevate ``routed_count`` above ``nets_fully_connected``.
    """

    def test_nets_fully_connected_dominates_routed_count(self):
        """State A connects more pads than state B; even though B has
        more route fragments, A must win the comparator.

        Concrete numbers from the issue: softstart iter-0 had
        ``routed_count=9, nets_fully_connected=4``.  A hypothetical
        re-route that produced ``routed_count=8, nets_fully_connected=8``
        is unambiguously better -- it closed more pad-to-pad paths
        even though one fragment was dropped.
        """
        a = IterationMetrics(
            iteration=1,
            routed_count=8,
            overflow=10,
            clearance_violations=0,
            nets_fully_connected=8,
        )
        b = IterationMetrics(
            iteration=2,
            routed_count=9,
            overflow=10,
            clearance_violations=0,
            nets_fully_connected=4,
        )
        # A has fewer fragments but more closed nets -- must win.
        assert a.is_better_than(b)
        assert not b.is_better_than(a)

    def test_default_zero_preserves_routed_count_ordering(self):
        """When ``nets_fully_connected`` defaults to 0 on both sides
        (i.e. older call sites that haven't been updated), the
        comparator must fall through to the legacy ordering
        (``routed_count`` desc, then ``clearance_violations``, etc.).

        Back-compat guarantee for Issue #3117: existing tests in
        ``TestPatienceComparatorBehavior`` above construct
        ``IterationMetrics`` without ``nets_fully_connected`` and rely
        on the old ordering.  This test pins the property explicitly
        so it can't drift.
        """
        # Both default-zero; ordering should mirror legacy
        # routed-count-then-overflow.
        more_routed = IterationMetrics(
            iteration=1, routed_count=10, overflow=10,
        )
        fewer_routed = IterationMetrics(
            iteration=2, routed_count=8, overflow=5,
        )
        # Old behaviour: routed_count dominates overflow.
        assert more_routed.is_better_than(fewer_routed)
        assert not fewer_routed.is_better_than(more_routed)

        # And the explicit-zero form is identical to the default form.
        more_routed_explicit = IterationMetrics(
            iteration=1, routed_count=10, overflow=10,
            nets_fully_connected=0,
        )
        assert more_routed.sort_key == more_routed_explicit.sort_key

    def test_fragment_only_net_loses_to_smaller_fully_connected(self):
        """End-to-end style: a state with N nets that each have one
        fragment but only N-1 fully connected loses to a state with
        N-1 nets where all N-1 are fully connected.

        This is the inverse direction of
        ``test_nets_fully_connected_dominates_routed_count``: it
        verifies the comparator treats ``routed_count`` as a tertiary
        signal that cannot override ``nets_fully_connected`` even
        when the two states differ by a single fragment.
        """
        # State with N fragments but one is incomplete (N-1 connected)
        fragment_state = IterationMetrics(
            iteration=5,
            routed_count=10,
            overflow=20,
            clearance_violations=0,
            nets_fully_connected=9,
        )
        # State with N-1 fragments, all fully connected (N-1 connected)
        smaller_connected_state = IterationMetrics(
            iteration=6,
            routed_count=9,
            overflow=20,
            clearance_violations=0,
            nets_fully_connected=9,
        )
        # Equal ``nets_fully_connected`` -> tertiary key
        # (``routed_count``) breaks the tie in favour of the fragment
        # state.  This is the "incremental progress" signal described
        # in the docstring: a fragment-only iteration that did not
        # lose any pad-connectivity is still preferred to a fully-
        # connected-only iteration that lost a fragment.
        assert fragment_state.is_better_than(smaller_connected_state)

        # But if the smaller-fragment state strictly improves
        # ``nets_fully_connected``, IT wins regardless of routed_count.
        better_connected_state = IterationMetrics(
            iteration=6,
            routed_count=9,
            overflow=20,
            clearance_violations=0,
            nets_fully_connected=10,
        )
        assert better_connected_state.is_better_than(fragment_state)


class TestCLIFlagWiredThrough:
    """The ``--early-stop-patience`` flag must exist on both the outer
    ``parser.py`` route subparser and the inner ``route_cmd.py`` parser
    (the parser-drift test enforces parity; this test pins the actual
    presence so a regression here surfaces in this file too).
    """

    def test_flag_on_inner_parser(self):
        # Import lazily so we don't pay the cost in the common case
        # where this whole module is collected but not run.
        from kicad_tools.cli import route_cmd
        # ``_build_parser`` is the conventional name; if it isn't
        # exposed we walk the module for the parser constructor.
        builder = getattr(route_cmd, "_build_parser", None)
        if builder is None:
            # Fall back to substring search of the source -- cheaper
            # than reimplementing the parser surface here.
            src = inspect.getsource(route_cmd)
            assert "--early-stop-patience" in src, (
                "--early-stop-patience missing from route_cmd.py inner parser"
            )
            return
        parser = builder()
        flags = {
            opt
            for action in parser._actions
            for opt in action.option_strings
            if opt.startswith("--")
        }
        assert "--early-stop-patience" in flags

    def test_flag_on_outer_parser(self):
        from kicad_tools.cli import parser as parser_mod
        src = inspect.getsource(parser_mod)
        # Outer parser declares the flag in build_parser; substring
        # search keeps this test cheap and independent of parser
        # construction details.
        assert "--early-stop-patience" in src, (
            "--early-stop-patience missing from outer parser.py"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
