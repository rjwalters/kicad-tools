"""Tests for total wall-clock timeout semantics in ``kct route`` (issue #2802).

Historically ``--timeout`` was respected only at the per-inner-call level
(``Autorouter.route_all_negotiated`` etc.), but the orchestration loops in
``src/kicad_tools/cli/route_cmd.py`` re-invoked those inner routines multiple
times -- once per layer-escalation attempt, once per placement-feedback
iteration, once per rule-relaxation tier, etc. -- each with a fresh copy of
``args.timeout``.  Worst-case wall-clock for a single ``kct route`` invocation
could therefore exceed ``--timeout`` by 5-10x.

This module verifies the fix: a single monotonic deadline computed once in
``main()`` is threaded through every orchestration site via the
``_set_wall_clock_deadline`` / ``_remaining_budget`` / ``_deadline_expired`` /
``_budgeted_timeout`` helpers.  When the deadline fires, outer loops bail
early, inner calls receive shrinking timeouts, and the auto-fix /
placement-feedback hooks skip themselves.

The tests below cover the helpers, the deadline-expired short-circuits in the
orchestration helpers (``_run_auto_fix`` / ``_run_placement_feedback``), and a
backward-compat assertion that nothing changes when ``--timeout`` is omitted.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# =============================================================================
# Unit tests: the helper functions themselves
# =============================================================================


class TestWallClockHelpers:
    """Direct tests for the deadline-management helpers."""

    def test_set_deadline_with_timeout(self):
        """``_set_wall_clock_deadline`` stamps a future monotonic deadline."""
        from kicad_tools.cli.route_cmd import _set_wall_clock_deadline

        args = SimpleNamespace(timeout=30.0)
        before = time.monotonic()
        _set_wall_clock_deadline(args)
        after = time.monotonic()

        assert args._wall_clock_deadline is not None
        assert before + 30.0 <= args._wall_clock_deadline <= after + 30.0

    def test_set_deadline_without_timeout(self):
        """No deadline is stamped when ``--timeout`` is None / 0 / negative."""
        from kicad_tools.cli.route_cmd import _set_wall_clock_deadline

        for value in (None, 0, 0.0, -1.0):
            args = SimpleNamespace(timeout=value)
            _set_wall_clock_deadline(args)
            assert args._wall_clock_deadline is None, f"timeout={value!r}"

    def test_remaining_budget_is_none_without_deadline(self):
        """``_remaining_budget`` preserves legacy unbounded behaviour."""
        from kicad_tools.cli.route_cmd import _remaining_budget

        args = SimpleNamespace(timeout=None, _wall_clock_deadline=None)
        assert _remaining_budget(args) is None

    def test_remaining_budget_counts_down(self):
        """``_remaining_budget`` returns a shrinking non-negative float."""
        from kicad_tools.cli.route_cmd import _remaining_budget

        args = SimpleNamespace(timeout=10.0)
        args._wall_clock_deadline = time.monotonic() + 1.0
        first = _remaining_budget(args)
        assert first is not None
        assert 0.0 < first <= 1.0

        time.sleep(0.05)
        second = _remaining_budget(args)
        assert second is not None
        assert second < first

    def test_remaining_budget_floor_at_zero(self):
        """Past-deadline budgets clamp at exactly 0.0."""
        from kicad_tools.cli.route_cmd import _remaining_budget

        args = SimpleNamespace(timeout=1.0)
        args._wall_clock_deadline = time.monotonic() - 5.0
        assert _remaining_budget(args) == 0.0

    def test_deadline_expired_false_without_deadline(self):
        """Legacy runs (``--timeout`` omitted) never look expired."""
        from kicad_tools.cli.route_cmd import _deadline_expired

        args = SimpleNamespace(timeout=None, _wall_clock_deadline=None)
        assert _deadline_expired(args) is False

    def test_deadline_expired_false_in_future(self):
        """Future deadlines are not yet expired."""
        from kicad_tools.cli.route_cmd import _deadline_expired

        args = SimpleNamespace(timeout=60.0)
        args._wall_clock_deadline = time.monotonic() + 30.0
        assert _deadline_expired(args) is False

    def test_deadline_expired_true_in_past(self):
        """Past deadlines flip to expired."""
        from kicad_tools.cli.route_cmd import _deadline_expired

        args = SimpleNamespace(timeout=1.0)
        args._wall_clock_deadline = time.monotonic() - 0.1
        assert _deadline_expired(args) is True

    def test_budgeted_timeout_returns_min(self):
        """``_budgeted_timeout`` clamps ``args.timeout`` to the remaining
        budget so the final stage shortens as time runs out."""
        from kicad_tools.cli.route_cmd import _budgeted_timeout

        args = SimpleNamespace(timeout=100.0)
        # Remaining budget is ~5s; original timeout is 100s; expect min.
        args._wall_clock_deadline = time.monotonic() + 5.0

        budgeted = _budgeted_timeout(args)
        assert budgeted is not None
        assert 0.0 < budgeted <= 5.0

    def test_budgeted_timeout_preserves_args_timeout_without_deadline(self):
        """When no deadline is configured, the helper is a no-op."""
        from kicad_tools.cli.route_cmd import _budgeted_timeout

        args = SimpleNamespace(timeout=42.0, _wall_clock_deadline=None)
        assert _budgeted_timeout(args) == 42.0

    def test_budgeted_timeout_returns_none_when_legacy(self):
        """``--timeout`` omitted: pass through ``None``."""
        from kicad_tools.cli.route_cmd import _budgeted_timeout

        args = SimpleNamespace(timeout=None, _wall_clock_deadline=None)
        assert _budgeted_timeout(args) is None


# =============================================================================
# Issue #2823: per-attempt budget allocator for escalation loops
# =============================================================================


class TestPerAttemptBudgetedTimeout:
    """Direct tests for ``_per_attempt_budgeted_timeout`` (issue #2823).

    Unlike :func:`_budgeted_timeout`, this helper must *fairly slice* the
    remaining wall-clock budget across remaining escalation attempts so
    the first attempt does not greedily consume the entire ``--timeout``
    and starve the higher-layer / looser-rule attempts.
    """

    def test_returns_none_without_deadline(self):
        """Legacy unbounded behaviour: no deadline -> no cap."""
        from kicad_tools.cli.route_cmd import _per_attempt_budgeted_timeout

        args = SimpleNamespace(timeout=None, _wall_clock_deadline=None)
        assert _per_attempt_budgeted_timeout(args, attempt_index=0, max_attempts=4) is None

    def test_first_attempt_gets_fair_slice_not_full_budget(self):
        """The bug from issue #2823: when ``--timeout 30`` is set with 4
        escalation attempts, attempt 1 must NOT receive the full 30s -- it
        must receive ~7.5s so attempts 2-4 also have a real chance to run.

        This is the core regression guard against the original behaviour
        of :func:`_budgeted_timeout`, which let attempt 1 consume the
        entire 30s and left attempts 2-4 with nothing.
        """
        from kicad_tools.cli.route_cmd import _per_attempt_budgeted_timeout

        args = SimpleNamespace(timeout=30.0)
        args._wall_clock_deadline = time.monotonic() + 30.0  # ~30s remaining

        budgeted = _per_attempt_budgeted_timeout(args, attempt_index=0, max_attempts=4)
        assert budgeted is not None
        # Fair slice for 4 attempts is ~7.5s; allow small monotonic jitter.
        assert 0.0 < budgeted <= 7.5 + 0.1, (
            f"first attempt must get a fair slice (~7.5s for 4 attempts), "
            f"not the full {args.timeout}s; got {budgeted:.3f}s"
        )

    def test_never_exceeds_args_timeout(self):
        """Per-attempt budget is an *upper bound*, never larger than the
        user's original ``--timeout``.  When ``--timeout`` is generous
        (e.g. 100s) but the per-attempt slice would naively be larger
        (e.g. 1000s/4=250s), the helper still caps at ``args.timeout``.
        """
        from kicad_tools.cli.route_cmd import _per_attempt_budgeted_timeout

        args = SimpleNamespace(timeout=100.0)
        # Pretend the deadline is ridiculously far in the future so the
        # per-attempt slice is the larger value; ``args.timeout`` must
        # still bind.
        args._wall_clock_deadline = time.monotonic() + 1000.0

        budgeted = _per_attempt_budgeted_timeout(args, attempt_index=0, max_attempts=4)
        assert budgeted is not None
        assert budgeted <= 100.0

    def test_never_exceeds_remaining_budget(self):
        """Per-attempt budget never overruns the total wall-clock deadline,
        even if ``args.timeout`` and the fair slice are both larger.
        """
        from kicad_tools.cli.route_cmd import _per_attempt_budgeted_timeout

        args = SimpleNamespace(timeout=1000.0)
        args._wall_clock_deadline = time.monotonic() + 5.0  # only 5s left

        budgeted = _per_attempt_budgeted_timeout(args, attempt_index=0, max_attempts=4)
        assert budgeted is not None
        # Fair slice would be 5/4=1.25s; remaining budget is 5s; both <
        # ``args.timeout``=1000s.  The smaller fair slice should bind.
        assert 0.0 < budgeted <= 1.25 + 0.1

    def test_later_attempts_get_increasing_slice(self):
        """As attempts progress, ``remaining_attempts`` decreases, so the
        per-attempt slice of any unused budget *grows*.  This is the
        "unused budget rolls forward" property: an attempt that finishes
        well under its slice enlarges the slice for the next attempt.
        """
        from kicad_tools.cli.route_cmd import _per_attempt_budgeted_timeout

        args = SimpleNamespace(timeout=30.0)
        args._wall_clock_deadline = time.monotonic() + 30.0  # full budget

        slice_0 = _per_attempt_budgeted_timeout(args, attempt_index=0, max_attempts=4)
        # Simulate attempt 0 finishing instantly; attempt 1 sees the same
        # ~30s remaining but only 3 attempts outstanding.
        slice_1 = _per_attempt_budgeted_timeout(args, attempt_index=1, max_attempts=4)
        # And attempt 3 (the last) sees the full remaining as its slice.
        slice_3 = _per_attempt_budgeted_timeout(args, attempt_index=3, max_attempts=4)
        assert slice_0 is not None and slice_1 is not None and slice_3 is not None
        assert slice_0 < slice_1 < slice_3
        # slice_3 should be the *full* remaining budget (one attempt left).
        assert slice_3 <= 30.0
        # All slices must respect ``args.timeout`` bound.
        for s in (slice_0, slice_1, slice_3):
            assert s <= 30.0

    def test_single_attempt_collapses_to_full_budget(self):
        """``max_attempts=1`` -> no fair-slicing needed; behaves like
        :func:`_budgeted_timeout` (the entire remaining budget is the
        slice for the single outstanding attempt).
        """
        from kicad_tools.cli.route_cmd import _per_attempt_budgeted_timeout

        args = SimpleNamespace(timeout=100.0)
        args._wall_clock_deadline = time.monotonic() + 50.0

        budgeted = _per_attempt_budgeted_timeout(args, attempt_index=0, max_attempts=1)
        assert budgeted is not None
        # Fair slice == remaining == 50s; ``args.timeout``=100s; min is 50s.
        assert 49.0 < budgeted <= 50.0

    def test_zero_max_attempts_clamped_to_one(self):
        """Defensive: ``max_attempts=0`` (defensive programming) is
        clamped to 1 so no division-by-zero occurs.
        """
        from kicad_tools.cli.route_cmd import _per_attempt_budgeted_timeout

        args = SimpleNamespace(timeout=10.0)
        args._wall_clock_deadline = time.monotonic() + 10.0

        # Should not raise; should return a sensible value.
        budgeted = _per_attempt_budgeted_timeout(args, attempt_index=0, max_attempts=0)
        assert budgeted is not None
        assert budgeted > 0

    def test_attempt_beyond_max_clamped_to_one_remaining(self):
        """Defensive: if ``attempt_index >= max_attempts`` (caller bug)
        the helper still treats remaining_attempts as at least 1 so no
        division-by-zero or negative-slice occurs.
        """
        from kicad_tools.cli.route_cmd import _per_attempt_budgeted_timeout

        args = SimpleNamespace(timeout=10.0)
        args._wall_clock_deadline = time.monotonic() + 10.0

        budgeted = _per_attempt_budgeted_timeout(args, attempt_index=10, max_attempts=4)
        assert budgeted is not None
        assert budgeted > 0

    def test_with_args_timeout_none_falls_through_to_slice(self):
        """Defensive branch: ``args.timeout`` is None but a deadline is
        configured (unreachable in practice, since the deadline is
        derived from ``args.timeout``, but the helper guards against
        future refactors that decouple the two).
        """
        from kicad_tools.cli.route_cmd import _per_attempt_budgeted_timeout

        args = SimpleNamespace(timeout=None)
        args._wall_clock_deadline = time.monotonic() + 30.0

        budgeted = _per_attempt_budgeted_timeout(args, attempt_index=0, max_attempts=4)
        assert budgeted is not None
        # Fair slice for 4 attempts is ~7.5s; no ``args.timeout`` cap.
        assert 0.0 < budgeted <= 7.5 + 0.1


# =============================================================================
# Behavioral tests: deadline-expired short-circuits in orchestration helpers
# =============================================================================


class TestRunAutoFixDeadline:
    """Tests for the ``_run_auto_fix`` deadline guard."""

    def test_auto_fix_skipped_when_deadline_expired(self, tmp_path):
        """``_run_auto_fix`` returns early (without calling fix-drc) when
        the wall-clock deadline has already been consumed by upstream
        stages.  Without this guard, ``fix-drc`` (which has no
        ``--timeout`` flag of its own) would run unbounded."""
        from kicad_tools.cli.route_cmd import _run_auto_fix

        args = SimpleNamespace(timeout=1.0)
        args._wall_clock_deadline = time.monotonic() - 5.0  # already past

        dummy_pcb = tmp_path / "dummy.kicad_pcb"
        dummy_pcb.write_text("(kicad_pcb)\n")

        with patch("kicad_tools.cli.fix_drc_cmd.main") as mock_fix:
            result = _run_auto_fix(
                output_path=dummy_pcb,
                max_passes=3,
                quiet=True,
                args=args,
            )
            mock_fix.assert_not_called()
            # The non-zero "skipped" return signals the caller that the
            # fix step did not complete -- consistent with the
            # exit-code contract for partial routing.
            assert result != 0

    def test_auto_fix_runs_when_no_deadline(self, tmp_path):
        """Backward compat: when ``--timeout`` is absent, ``_run_auto_fix``
        delegates to ``fix-drc`` exactly as before (issue #2802 must not
        regress legacy unbounded runs)."""
        from kicad_tools.cli.route_cmd import _run_auto_fix

        args = SimpleNamespace(timeout=None, _wall_clock_deadline=None)

        dummy_pcb = tmp_path / "dummy.kicad_pcb"
        dummy_pcb.write_text("(kicad_pcb)\n")

        with patch("kicad_tools.cli.fix_drc_cmd.main", return_value=0) as mock_fix:
            result = _run_auto_fix(
                output_path=dummy_pcb,
                max_passes=3,
                quiet=True,
                args=args,
            )
            mock_fix.assert_called_once()
            assert result == 0

    def test_auto_fix_runs_when_deadline_still_in_future(self, tmp_path):
        """A non-expired deadline does not block ``fix-drc``."""
        from kicad_tools.cli.route_cmd import _run_auto_fix

        args = SimpleNamespace(timeout=60.0)
        args._wall_clock_deadline = time.monotonic() + 60.0

        dummy_pcb = tmp_path / "dummy.kicad_pcb"
        dummy_pcb.write_text("(kicad_pcb)\n")

        with patch("kicad_tools.cli.fix_drc_cmd.main", return_value=0) as mock_fix:
            _run_auto_fix(
                output_path=dummy_pcb,
                max_passes=3,
                quiet=True,
                args=args,
            )
            mock_fix.assert_called_once()

    def test_auto_fix_legacy_call_without_args_param(self, tmp_path):
        """Callers that don't pass ``args=`` retain pre-#2802 behaviour
        (no deadline check, ``fix-drc`` runs unconditionally)."""
        from kicad_tools.cli.route_cmd import _run_auto_fix

        dummy_pcb = tmp_path / "dummy.kicad_pcb"
        dummy_pcb.write_text("(kicad_pcb)\n")

        with patch("kicad_tools.cli.fix_drc_cmd.main", return_value=0) as mock_fix:
            _run_auto_fix(
                output_path=dummy_pcb,
                max_passes=3,
                quiet=True,
            )
            mock_fix.assert_called_once()


class TestRunPlacementFeedbackDeadline:
    """Tests for the ``_run_placement_feedback`` deadline guard."""

    def test_placement_feedback_skipped_when_deadline_expired(self, tmp_path):
        """``_run_placement_feedback`` short-circuits when the budget is
        already gone, returning ``None`` without ever calling
        ``Autorouter.route_with_placement_feedback``."""
        from kicad_tools.cli.route_cmd import _run_placement_feedback

        args = SimpleNamespace(
            timeout=1.0,
            output=None,
            placement_feedback_budget=3,
            placement_feedback_max_movement=5.0,
            per_net_timeout=None,
            placement_feedback_stagnation_patience=3,
            placement_feedback_outer_timeout=None,
            placement_feedback_anchor=None,
            placement_feedback_no_anchor=None,
            strategy="negotiated",
        )
        args._wall_clock_deadline = time.monotonic() - 5.0  # past

        # Build a minimal stand-in router; ``_run_placement_feedback`` must
        # not call into it on this code path.
        class _FakeRouter:
            def get_failed_nets(self):  # pragma: no cover - should not run
                raise AssertionError("must not be called when deadline is expired")

            def route_with_placement_feedback(self, **kwargs):  # pragma: no cover
                raise AssertionError("must not be called when deadline is expired")

        dummy_pcb = tmp_path / "dummy.kicad_pcb"
        dummy_pcb.write_text("(kicad_pcb)\n")

        result = _run_placement_feedback(
            router=_FakeRouter(),
            pcb_path=dummy_pcb,
            args=args,
            quiet=True,
        )
        assert result is None


# =============================================================================
# main() integration: deadline is stamped onto args after argparse
# =============================================================================


class TestMainStampsDeadline:
    """``main()`` must stamp ``_wall_clock_deadline`` onto ``args``
    immediately after argparse so every downstream helper sees it."""

    def _minimal_pcb(self, tmp_path: Path) -> Path:
        pcb_content = """(kicad_pcb
  (version 20240101)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup
    (grid_origin 0 0)
  )
  (net 0 "")
)"""
        pcb_path = tmp_path / "minimal.kicad_pcb"
        pcb_path.write_text(pcb_content)
        return pcb_path

    def test_main_dry_run_with_timeout_stamps_deadline(self, tmp_path):
        """A ``--timeout`` argument must produce a positive
        ``_wall_clock_deadline`` on the args namespace inside ``main()``.

        We can't observe the namespace directly after ``main`` returns,
        so we intercept ``_set_wall_clock_deadline`` and verify it is
        invoked with the same ``args`` object argparse produced.
        """
        from kicad_tools.cli import route_cmd as route_cmd_mod

        pcb_path = self._minimal_pcb(tmp_path)

        captured: list[float | None] = []

        original = route_cmd_mod._set_wall_clock_deadline

        def _spy(args):
            original(args)
            captured.append(getattr(args, "_wall_clock_deadline", None))

        with patch.object(route_cmd_mod, "_set_wall_clock_deadline", _spy):
            route_cmd_mod.main(
                [
                    str(pcb_path),
                    "--timeout",
                    "60",
                    "--dry-run",
                    "--quiet",
                ]
            )

        assert captured, "main() did not invoke _set_wall_clock_deadline"
        # With --timeout 60, the stamped deadline must be in the future.
        assert captured[0] is not None
        assert captured[0] > time.monotonic()

    def test_main_dry_run_without_timeout_leaves_deadline_none(self, tmp_path):
        """No ``--timeout`` -> no deadline -> legacy unbounded run."""
        from kicad_tools.cli import route_cmd as route_cmd_mod

        pcb_path = self._minimal_pcb(tmp_path)

        captured: list[float | None] = []

        original = route_cmd_mod._set_wall_clock_deadline

        def _spy(args):
            original(args)
            captured.append(getattr(args, "_wall_clock_deadline", None))

        with patch.object(route_cmd_mod, "_set_wall_clock_deadline", _spy):
            route_cmd_mod.main(
                [
                    str(pcb_path),
                    "--dry-run",
                    "--quiet",
                ]
            )

        assert captured, "main() did not invoke _set_wall_clock_deadline"
        assert captured[0] is None


# =============================================================================
# Issue #2823: integration test for layer-escalation per-attempt budget
#
# Verifies that with ``--timeout T`` and ``--auto-layers``, the layer
# escalation loop in ``route_with_layer_escalation`` calls each inner-router
# attempt with a *fairly sliced* timeout (not the full ``T`` for the first
# attempt).  We mock the inner router so the test is fast and hermetic.
# =============================================================================


class TestLayerEscalationPerAttemptBudget:
    """Tests for the per-attempt budget integration in
    ``route_with_layer_escalation`` (issue #2823).

    The escalation loop must call the new helper with the correct
    ``attempt_index`` and ``max_attempts`` arguments so that, with a tight
    ``--timeout``, every layer-stack attempt receives a real (non-zero)
    timeout slice rather than letting attempt 1 consume the entire budget.
    """

    def test_helper_invoked_by_every_escalation_loop(self):
        """All three escalation loops (layer, combined, rule-relaxation)
        must invoke ``_per_attempt_budgeted_timeout``.  The module-level
        count includes one ``def`` line and three call sites (one per
        loop), so the total must be at least 4.

        This is the high-level structural guard against the original
        bug from issue #2823 where every loop greedily called
        ``_budgeted_timeout`` and starved later attempts.
        """
        import inspect

        from kicad_tools.cli import route_cmd as route_cmd_mod

        source = inspect.getsource(route_cmd_mod)
        assert source.count("_per_attempt_budgeted_timeout(") >= 4, (
            "_per_attempt_budgeted_timeout must be defined and invoked "
            "by every escalation loop (layer / combined / rule-relaxation)"
        )

    def test_layer_escalation_call_site_uses_per_attempt_helper(self):
        """``route_with_layer_escalation`` must pass ``attempt_num - 1`` as
        ``attempt_index`` and ``len(layer_configs)`` as ``max_attempts``.

        This is a structural assertion against the source so a future
        refactor that accidentally reverts to ``_budgeted_timeout`` (the
        bug from issue #2823) would fail this test.
        """
        import inspect

        from kicad_tools.cli import route_cmd as route_cmd_mod

        source = inspect.getsource(route_cmd_mod.route_with_layer_escalation)
        assert "_per_attempt_budgeted_timeout(" in source, (
            "route_with_layer_escalation must call _per_attempt_budgeted_timeout, "
            "not the legacy _budgeted_timeout helper (issue #2823)"
        )
        assert "attempt_num - 1" in source, (
            "route_with_layer_escalation must pass the 0-based attempt index "
            "(attempt_num - 1) to the per-attempt helper"
        )
        assert "len(layer_configs)" in source, (
            "route_with_layer_escalation must pass len(layer_configs) as "
            "max_attempts to the per-attempt helper"
        )

    def test_combined_escalation_call_site_uses_per_attempt_helper(self):
        """``route_with_combined_escalation`` must pass a 2D linear index
        (``layer_idx * len(tiers) + tier_idx``) and total cell count
        (``len(layer_configs) * len(tiers)``) so the budget is divided
        across the *entire* matrix, not just one column.
        """
        import inspect

        from kicad_tools.cli import route_cmd as route_cmd_mod

        source = inspect.getsource(route_cmd_mod.route_with_combined_escalation)
        assert "_per_attempt_budgeted_timeout(" in source, (
            "route_with_combined_escalation must call _per_attempt_budgeted_timeout (issue #2823)"
        )
        # The 2D linear index requires precomputed total cell count.
        assert "len(layer_configs) * len(tiers)" in source, (
            "route_with_combined_escalation must compute max_attempts as "
            "len(layer_configs) * len(tiers) for the full 2D matrix"
        )

    def test_rule_relaxation_call_site_uses_per_attempt_helper(self):
        """``route_with_rule_relaxation`` must also slice the budget across
        tiers so the looser-rule attempts get a real chance to run.
        """
        import inspect

        from kicad_tools.cli import route_cmd as route_cmd_mod

        source = inspect.getsource(route_cmd_mod.route_with_rule_relaxation)
        assert "_per_attempt_budgeted_timeout(" in source, (
            "route_with_rule_relaxation must call _per_attempt_budgeted_timeout (issue #2823)"
        )

    def test_legacy_budgeted_timeout_still_used_by_non_escalation_callers(self):
        """The new helper is only for *multi-attempt* escalation loops.
        Single-stage callers (placement-feedback iterations, the inner
        ``route_with_strategy`` calls) must still use ``_budgeted_timeout``
        so backward compatibility for those paths is preserved.
        """
        import inspect

        from kicad_tools.cli import route_cmd as route_cmd_mod

        source = inspect.getsource(route_cmd_mod)
        # The legacy helper must remain in use somewhere; it is the right
        # tool for single-stage call sites.
        assert source.count("_budgeted_timeout(") >= 5, (
            "_budgeted_timeout must remain in use by single-stage callers "
            "(placement-feedback, route_with_strategy, etc.); the "
            "per-attempt helper only replaces escalation-loop call sites"
        )


# =============================================================================
# Optional slow test: end-to-end wall-clock bound via CLI
#
# Disabled by default because it exercises the real router on a non-trivial
# board.  When run, it asserts that ``--timeout N`` produces total wall-clock
# of at most ``N + safety_margin`` seconds even with placement feedback and
# auto-fix engaged -- the exact regression issue #2802 reports.
# =============================================================================


@pytest.mark.slow
def test_route_cli_respects_total_timeout(tmp_path):
    """End-to-end: ``kct route --timeout 30`` exits within ``30 + 30s``
    safety margin even with ``--auto-layers`` + ``--placement-feedback``
    + ``--auto-fix`` all engaged."""
    import shutil
    import subprocess

    fixture = (
        Path(__file__).parent.parent / "boards" / "01-voltage-divider" / "voltage_divider.kicad_pcb"
    )
    if not fixture.exists():
        pytest.skip(f"fixture not present: {fixture}")

    work_pcb = tmp_path / "voltage_divider.kicad_pcb"
    shutil.copy(fixture, work_pcb)

    timeout_seconds = 30.0
    safety_margin = 30.0

    start = time.monotonic()
    proc = subprocess.run(
        [
            "kct",
            "route",
            str(work_pcb),
            "--timeout",
            str(timeout_seconds),
            "--auto-layers",
            "--max-layers",
            "4",
            "--placement-feedback",
            "--placement-feedback-budget",
            "3",
            "--auto-fix",
            "--auto-fix-passes",
            "2",
            "--quiet",
        ],
        capture_output=True,
        timeout=timeout_seconds + safety_margin + 30.0,  # subprocess-level escape
    )
    elapsed = time.monotonic() - start

    # Exit code is allowed to be 0 (success), 2 (partial), 3 (DRC), or 5
    # (interrupt/timeout w/ partial save).  What we care about is the
    # wall-clock bound.
    assert proc.returncode in (0, 2, 3, 4, 5), (
        f"route exited with unexpected code {proc.returncode}; "
        f"stdout={proc.stdout!r}; stderr={proc.stderr!r}"
    )
    assert elapsed <= timeout_seconds + safety_margin, (
        f"route ran {elapsed:.1f}s with --timeout {timeout_seconds} "
        f"(should be <= {timeout_seconds + safety_margin}s)"
    )
