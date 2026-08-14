"""``--timeout`` / ``--per-net-timeout`` actually bound the lattice (Issue #4697).

Before this fix the lattice engine was time-bounded **only** under ``--complete``:
``_lattice_link_budget_s`` was assigned from ``args._complete_link_budget_s``, which
is stamped nowhere else, so every other invocation handed ``route_netset`` a
``deadline`` of ``None``.  ``--timeout`` / ``--per-net-timeout`` were accepted,
echoed in the routing banner, and silently discarded -- and because the lattice
negotiates the *whole netset* in a single ``route_netset`` call, ``route_all``'s
between-nets ``timeout`` check can never fire mid-run either.  The observed result
was 60+ minute unbounded runs on the only strategy the #4280 gate allows.

These tests pin the fix at the seam it actually lands on -- the ``deadline`` the
lattice negotiation receives -- rather than at the ``route_all()`` call site
(verified ineffective in the issue's correction comment).
"""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path

import pytest

from kicad_tools.cli.route_cmd import (
    _COMPLETE_LINK_BUDGET_DEFAULT_S,
    _lattice_absolute_deadline,
    _lattice_attempt_deadline,
    _per_attempt_budgeted_timeout,
    _resolve_lattice_link_budget,
)
from kicad_tools.cli.route_cmd import (
    main as route_main,
)

# The Phase 1 stranded-board fixture: two 0402s, NET1 pre-routed, NET2 stranded.
# Tiny and fully deterministic -- routes in ~1s through the lattice.
from tests.test_cli_route_complete_4471 import STRANDED_BOARD


@pytest.fixture
def board(tmp_path: Path) -> Path:
    pcb = tmp_path / "stranded.kicad_pcb"
    pcb.write_text(STRANDED_BOARD)
    return pcb


@pytest.fixture
def route_netset_spy(monkeypatch):
    """Record every ``deadline`` the lattice negotiation is handed."""
    from kicad_tools.router.lattice.pathfinder import LatticePathfinder

    original = LatticePathfinder.route_netset
    seen: list[float | None] = []

    def spy(self, connections, **kwargs):
        seen.append(kwargs.get("deadline"))
        return original(self, connections, **kwargs)

    monkeypatch.setattr(LatticePathfinder, "route_netset", spy)
    return seen


def _lattice_route(board: Path, out: Path, *extra: str) -> int:
    return route_main(
        [
            str(board),
            "-o",
            str(out),
            "--route-engine",
            "lattice",
            "--strategy",
            "basic",
            "--backend",
            "cpp",
            # Pin the layer count so the run does not enter layer-escalation
            # mode, whose own #2802 deadline check would mask the engine bound
            # under test.
            "--layers",
            "2",
            # The routing cache is keyed on board content + rules, NOT on the
            # time budget, so a warm cache would skip the negotiation entirely
            # and the deadline under test would never be observed.
            "--no-cache",
            *extra,
        ]
    )


# ---------------------------------------------------------------------------
# _resolve_lattice_link_budget: generalizes --complete's per-link budget.
# ---------------------------------------------------------------------------
class TestResolveLatticeLinkBudget:
    def test_complete_stamp_wins_verbatim(self):
        # --complete's #4472 stamp is authoritative -- including its 60s
        # backstop, which must not be re-derived from a disabled
        # --per-net-timeout.
        args = argparse.Namespace(
            per_net_timeout=0.0,
            _complete_link_budget_s=_COMPLETE_LINK_BUDGET_DEFAULT_S,
        )
        assert _resolve_lattice_link_budget(args) == _COMPLETE_LINK_BUDGET_DEFAULT_S

    def test_complete_stamp_wins_over_per_net_timeout(self):
        args = argparse.Namespace(per_net_timeout=30.0, _complete_link_budget_s=12.5)
        assert _resolve_lattice_link_budget(args) == 12.5

    def test_derives_from_per_net_timeout_without_complete(self):
        # The bug: this used to be None on every non---complete run.
        args = argparse.Namespace(per_net_timeout=30.0, _complete_link_budget_s=None)
        assert _resolve_lattice_link_budget(args) == 30.0

    def test_explicit_per_net_timeout_honored(self):
        args = argparse.Namespace(per_net_timeout=5.0, _complete_link_budget_s=None)
        assert _resolve_lattice_link_budget(args) == 5.0

    def test_disabled_per_net_timeout_preserves_unbudgeted(self):
        # --per-net-timeout 0 (e.g. --deterministic-budget, where a wall-clock
        # bound would destroy reproducibility) keeps the legacy unbudgeted
        # negotiation.  --timeout can still cap such a run.
        args = argparse.Namespace(per_net_timeout=0.0, _complete_link_budget_s=None)
        assert _resolve_lattice_link_budget(args) is None

    def test_missing_attributes_are_unbudgeted(self):
        assert _resolve_lattice_link_budget(argparse.Namespace()) is None


# ---------------------------------------------------------------------------
# _lattice_absolute_deadline: --timeout becomes a hard ceiling.
# ---------------------------------------------------------------------------
class TestLatticeAbsoluteDeadline:
    def test_none_without_timeout(self):
        args = argparse.Namespace(_routing_deadline=None, _wall_clock_deadline=None)
        assert _lattice_absolute_deadline(args) is None

    def test_missing_attributes_are_uncapped(self):
        assert _lattice_absolute_deadline(argparse.Namespace()) is None

    def test_returns_routing_deadline(self):
        stamp = time.monotonic() + 120.0
        args = argparse.Namespace(_routing_deadline=stamp, _wall_clock_deadline=stamp + 30.0)
        assert _lattice_absolute_deadline(args) == stamp

    def test_falls_back_to_wall_clock_deadline(self):
        stamp = time.monotonic() + 120.0
        args = argparse.Namespace(_routing_deadline=None, _wall_clock_deadline=stamp)
        assert _lattice_absolute_deadline(args) == stamp

    def test_set_wall_clock_deadline_feeds_it(self):
        from kicad_tools.cli.route_cmd import _set_wall_clock_deadline

        args = argparse.Namespace(timeout=60.0, auto_fix=False)
        before = time.monotonic()
        _set_wall_clock_deadline(args)
        got = _lattice_absolute_deadline(args)
        assert got is not None
        assert before < got <= before + 60.0 + 1.0


# ---------------------------------------------------------------------------
# _lattice_attempt_deadline: within a tier, the cap is the ATTEMPT's slice.
#
# Issue #4798.  ``_lattice_absolute_deadline`` returns the whole-invocation
# deadline, which is the correct cap at the two single-attempt call sites but
# wrong inside a multi-attempt escalation loop: the lattice negotiates the whole
# netset in ONE ``route_netset`` call, so an unsliced absolute deadline lets
# attempt N consume every later attempt's budget.  These cases pin the
# arithmetic itself (never looser than the absolute deadline; only ever tighter,
# and only when a fair slice is actually computed).
# ---------------------------------------------------------------------------
class TestLatticeAttemptDeadline:
    def test_none_attempt_timeout_is_passthrough(self):
        """No fair slice -> byte-identical to the pre-#4798 absolute cap."""
        stamp = time.monotonic() + 120.0
        args = argparse.Namespace(_routing_deadline=stamp, _wall_clock_deadline=stamp)
        assert _lattice_attempt_deadline(args, None) == _lattice_absolute_deadline(args) == stamp

    def test_none_everywhere_stays_uncapped(self):
        args = argparse.Namespace(_routing_deadline=None, _wall_clock_deadline=None)
        assert _lattice_attempt_deadline(args, None) is None

    def test_attempt_slice_tighter_than_absolute_wins(self):
        """The whole-run deadline is 600s out; this attempt only owns 150s."""
        absolute = time.monotonic() + 600.0
        args = argparse.Namespace(_routing_deadline=absolute, _wall_clock_deadline=absolute)
        before = time.monotonic()
        got = _lattice_attempt_deadline(args, 150.0)
        assert got is not None
        # now + 150s, comfortably inside the absolute cap.
        assert before + 150.0 <= got <= time.monotonic() + 150.0
        assert got < absolute

    def test_absolute_tighter_than_attempt_slice_wins(self):
        """A nearly-expired run budget clamps a generous per-attempt slice."""
        absolute = time.monotonic() + 5.0
        args = argparse.Namespace(_routing_deadline=absolute, _wall_clock_deadline=absolute)
        assert _lattice_attempt_deadline(args, 900.0) == absolute

    def test_no_absolute_deadline_returns_candidate_verbatim(self):
        """--timeout absent but a slice supplied -> the slice becomes the cap."""
        args = argparse.Namespace(_routing_deadline=None, _wall_clock_deadline=None)
        before = time.monotonic()
        got = _lattice_attempt_deadline(args, 30.0)
        assert got is not None
        assert before + 30.0 <= got <= time.monotonic() + 30.0

    def test_missing_attributes_with_a_slice(self):
        before = time.monotonic()
        got = _lattice_attempt_deadline(argparse.Namespace(), 12.0)
        assert got is not None
        assert before + 12.0 <= got <= time.monotonic() + 12.0

    @pytest.mark.parametrize("slice_s", [0.0, 1.0, 60.0, 600.0, 100_000.0])
    def test_never_looser_than_the_absolute_deadline(self, slice_s: float):
        """The invariant: this helper only ever TIGHTENS the pre-#4798 cap."""
        absolute = time.monotonic() + 300.0
        args = argparse.Namespace(_routing_deadline=absolute, _wall_clock_deadline=absolute)
        got = _lattice_attempt_deadline(args, slice_s)
        assert got is not None and got <= absolute

    # -- the ``--timeout 0`` / negative UNBOUNDED sentinel must not expire ----
    #
    # ``_set_wall_clock_deadline``: "If ``args.timeout`` is falsy (None, 0, or
    # negative) the deadline is set to ``None`` so the rest of the
    # orchestration treats the run as unbounded."  On that path
    # ``_per_attempt_budgeted_timeout`` returns the timeout verbatim (0.0 /
    # -5.0) and no absolute deadline is stamped, so a naive ``now +
    # attempt_timeout`` would hand the lattice an already-expired ceiling and
    # abort an explicitly unbounded run instantly.
    def test_zero_slice_without_an_absolute_deadline_stays_unbounded(self):
        """``--timeout 0`` is the unbounded sentinel, not a zero-length slice."""
        args = argparse.Namespace(_routing_deadline=None, _wall_clock_deadline=None)
        assert _lattice_attempt_deadline(args, 0.0) is None

    def test_negative_slice_without_an_absolute_deadline_stays_unbounded(self):
        """Negative ``--timeout`` is unbounded too (``not -5.0`` is ``False``)."""
        args = argparse.Namespace(_routing_deadline=None, _wall_clock_deadline=None)
        assert _lattice_attempt_deadline(args, -5.0) is None

    @pytest.mark.parametrize("slice_s", [0.0, -5.0])
    def test_nonpositive_slice_with_an_absolute_deadline_returns_it_verbatim(self, slice_s: float):
        """A non-slice never tightens the cap -- the absolute deadline stands.

        Inert for a *genuine* exhausted-budget ``0.0``, which can only arise
        when ``_remaining_budget`` clamps at zero -- and that requires an
        absolute deadline already ``<= now``, where the old
        ``min(absolute, now)`` collapsed to ``absolute`` anyway.
        """
        absolute = time.monotonic() + 300.0
        args = argparse.Namespace(_routing_deadline=absolute, _wall_clock_deadline=absolute)
        assert _lattice_attempt_deadline(args, slice_s) == absolute

    def test_expired_absolute_budget_still_yields_a_past_deadline(self):
        """The real exhausted-budget shape: the absolute cap is already past."""
        absolute = time.monotonic() - 1.0
        args = argparse.Namespace(_routing_deadline=absolute, _wall_clock_deadline=absolute)
        got = _lattice_attempt_deadline(args, 0.0)
        assert got is not None and got <= time.monotonic()

    def test_auto_fix_reserve_is_still_honored(self):
        """The absolute term still comes from ``_routing_deadline`` (#3238)."""
        routing = time.monotonic() + 40.0
        args = argparse.Namespace(_routing_deadline=routing, _wall_clock_deadline=routing + 20.0)
        assert _lattice_attempt_deadline(args, 900.0) == routing

    # -- the #4802 --deterministic-budget sentinel must stay inert ----------
    def test_deterministic_budget_without_timeout_collapses_to_absolute(self):
        """``--deterministic-budget`` alone: no deadline -> nothing changes.

        ``--deterministic-budget`` pins ``per_net_timeout`` to the ``0.0``
        sentinel (#4776/#4802) and does NOT stamp a wall-clock deadline, so
        ``_per_attempt_budgeted_timeout`` returns ``None`` at every escalation
        site and ``_lattice_attempt_deadline`` collapses to
        ``_lattice_absolute_deadline`` -- byte-identical to pre-#4798.
        """
        args = argparse.Namespace(
            timeout=None,
            per_net_timeout=0.0,
            _routing_deadline=None,
            _wall_clock_deadline=None,
        )
        slice_s = _per_attempt_budgeted_timeout(args, attempt_index=0, max_attempts=4)
        assert slice_s is None
        assert _lattice_attempt_deadline(args, slice_s) == _lattice_absolute_deadline(args) is None

    def test_zero_per_net_timeout_sentinel_is_not_read_as_a_slice(self):
        """The ``0.0`` sentinel must never leak in as ``attempt_timeout``.

        Guards the #4798 hoist against accidentally coupling to
        ``args.per_net_timeout``: with a real ``--timeout`` present the slice
        comes from the wall-clock budget (75s of a 300s budget across 4
        attempts), not from the disabled per-link sentinel.
        """
        absolute = time.monotonic() + 300.0
        args = argparse.Namespace(
            timeout=300.0,
            per_net_timeout=0.0,
            _routing_deadline=absolute,
            _wall_clock_deadline=absolute,
        )
        slice_s = _per_attempt_budgeted_timeout(args, attempt_index=0, max_attempts=4)
        assert slice_s is not None
        assert 70.0 <= slice_s <= 75.0  # ~300/4, minus test-execution jitter
        got = _lattice_attempt_deadline(args, slice_s)
        assert got is not None and got < absolute

    def test_fair_slice_shrinks_the_cap_across_a_four_attempt_ladder(self):
        """End-to-end arithmetic: attempt 0 of 4 gets ~1/4 of the budget."""
        args = argparse.Namespace(timeout=400.0, auto_fix=False)
        from kicad_tools.cli.route_cmd import _set_wall_clock_deadline

        _set_wall_clock_deadline(args)
        absolute = _lattice_absolute_deadline(args)
        assert absolute is not None

        first = _lattice_attempt_deadline(
            args, _per_attempt_budgeted_timeout(args, attempt_index=0, max_attempts=4)
        )
        last = _lattice_attempt_deadline(
            args, _per_attempt_budgeted_timeout(args, attempt_index=3, max_attempts=4)
        )
        assert first is not None and last is not None
        # The first attempt is capped near now+100s, NOT near the now+400s
        # whole-run deadline it used to receive.
        assert first < absolute - 250.0
        # The final attempt may use whatever remains -> the absolute cap.
        assert last == absolute


# ---------------------------------------------------------------------------
# Deadline combination in _negotiate_lattice_netset: the tighter bound wins.
# ---------------------------------------------------------------------------
class TestNegotiationDeadlineCombination:
    def test_absolute_cap_alone_bounds_the_run(self, board: Path, tmp_path: Path):
        from kicad_tools.router.io import load_pcb_for_routing

        cap = time.monotonic() + 42.0
        router, _ = load_pcb_for_routing(
            board, strategy="lattice", lattice_deadline=cap, force_python=True
        )
        assert router._lattice_deadline == cap
        assert router._lattice_link_budget_s is None

    def test_link_budget_alone_is_unchanged(self, board: Path):
        from kicad_tools.router.io import load_pcb_for_routing

        router, _ = load_pcb_for_routing(
            board, strategy="lattice", lattice_link_budget_s=7.5, force_python=True
        )
        assert router._lattice_link_budget_s == 7.5
        assert router._lattice_deadline is None

    def test_tighter_of_the_two_wins(self, board: Path, monkeypatch):
        """A near-past absolute cap overrides a generous per-link budget."""
        from kicad_tools.router.io import load_pcb_for_routing
        from kicad_tools.router.lattice.pathfinder import LatticePathfinder

        cap = time.monotonic() + 0.5
        router, _ = load_pcb_for_routing(
            board,
            strategy="lattice",
            # 3600s x link-count would be effectively unbounded on its own.
            lattice_link_budget_s=3600.0,
            lattice_deadline=cap,
            force_python=True,
        )
        seen: list[float | None] = []
        original = LatticePathfinder.route_netset

        def spy(self, connections, **kwargs):
            seen.append(kwargs.get("deadline"))
            return original(self, connections, **kwargs)

        monkeypatch.setattr(LatticePathfinder, "route_netset", spy)
        router._negotiate_lattice_netset()
        assert seen == [cap], "the absolute cap must clamp the link-budget deadline"

    def test_loose_absolute_cap_does_not_loosen_link_budget(self, board: Path, monkeypatch):
        from kicad_tools.router.io import load_pcb_for_routing
        from kicad_tools.router.lattice.pathfinder import LatticePathfinder

        cap = time.monotonic() + 100_000.0
        router, _ = load_pcb_for_routing(
            board,
            strategy="lattice",
            lattice_link_budget_s=1.0,
            lattice_deadline=cap,
            force_python=True,
        )
        seen: list[float | None] = []
        original = LatticePathfinder.route_netset

        def spy(self, connections, **kwargs):
            seen.append(kwargs.get("deadline"))
            return original(self, connections, **kwargs)

        monkeypatch.setattr(LatticePathfinder, "route_netset", spy)
        router._negotiate_lattice_netset()
        assert len(seen) == 1
        assert seen[0] is not None and seen[0] < cap


# ---------------------------------------------------------------------------
# Guard: every `--strategy basic` lattice dispatch receives a real deadline.
# ---------------------------------------------------------------------------
class TestCliDispatchIsBounded:
    def test_per_net_timeout_bounds_the_lattice(self, board, tmp_path, route_netset_spy):
        out = tmp_path / "out.kicad_pcb"
        started = time.monotonic()
        assert _lattice_route(board, out, "--per-net-timeout", "20") == 0
        assert route_netset_spy, "the lattice negotiation must have run"
        for deadline in route_netset_spy:
            assert deadline is not None, "--per-net-timeout must reach the lattice"
            # 20s x link-count, measured from just before the negotiation.
            assert deadline > started

    def test_timeout_alone_bounds_the_lattice(self, board, tmp_path, route_netset_spy):
        out = tmp_path / "out.kicad_pcb"
        started = time.monotonic()
        # --per-net-timeout 0 removes the per-link budget, so ONLY the absolute
        # --timeout cap can bound this run.
        assert _lattice_route(board, out, "--per-net-timeout", "0", "--timeout", "300") == 0
        assert route_netset_spy
        for deadline in route_netset_spy:
            assert deadline is not None, "--timeout must reach the lattice"
            assert started < deadline <= started + 300.0 + 1.0

    def test_default_run_is_bounded_by_the_default_per_net_timeout(
        self, board, tmp_path, route_netset_spy
    ):
        out = tmp_path / "out.kicad_pcb"
        assert _lattice_route(board, out) == 0
        assert route_netset_spy
        assert all(d is not None for d in route_netset_spy)

    def test_no_budget_at_all_preserves_legacy_unbounded(self, board, tmp_path, route_netset_spy):
        # Neither bound requested -> deadline stays None, exactly as before.
        out = tmp_path / "out.kicad_pcb"
        assert _lattice_route(board, out, "--per-net-timeout", "0") == 0
        assert route_netset_spy
        assert all(d is None for d in route_netset_spy)


# ---------------------------------------------------------------------------
# End-to-end: a short --timeout terminates the run and reports honestly.
# ---------------------------------------------------------------------------
class TestShortTimeoutTerminatesEarly:
    def test_expired_timeout_declines_with_deadline_exceeded(self, board, tmp_path, capsys):
        out = tmp_path / "out.kicad_pcb"
        started = time.monotonic()
        # 1ms: already spent by the time the negotiation starts, so the bound
        # is deterministic rather than a race against the search.
        _lattice_route(board, out, "--timeout", "0.001")
        elapsed = time.monotonic() - started
        captured = capsys.readouterr().out

        assert "decline[deadline-exceeded]" in captured, (
            "a deadline-truncated lattice run must report partial results with "
            "the deadline-exceeded reason, not die silently"
        )
        # Zero negotiation passes ran: the deadline was checked and honored.
        assert re.search(r"Lattice negotiation: 0/\d+ connections \(iterations=0", captured)
        # Generous CI ceiling; the point is it is bounded at all.
        assert elapsed < 120.0

    def test_default_run_routes_the_board_and_declines_nothing(self, board, tmp_path, capsys):
        out = tmp_path / "out.kicad_pcb"
        assert _lattice_route(board, out) == 0
        captured = capsys.readouterr().out
        assert "deadline-exceeded" not in captured
        assert re.search(r"Lattice negotiation: 2/2 connections", captured)
        assert "(segment" in out.read_text()


# ---------------------------------------------------------------------------
# Banner honesty: never claim a budget the dispatched engine does not enforce.
# ---------------------------------------------------------------------------
class TestBannerHonesty:
    def test_lattice_annotates_per_net_timeout_as_per_link(self, board, tmp_path, capsys):
        out = tmp_path / "out.kicad_pcb"
        assert _lattice_route(board, out, "--per-net-timeout", "20") == 0
        captured = capsys.readouterr().out
        assert "Per-net timeout: 20.0s (lattice: per-LINK budget" in captured
        assert "netset deadline = 20.0s x link count" in captured

    def test_lattice_announces_an_unbounded_run(self, board, tmp_path, capsys):
        out = tmp_path / "out.kicad_pcb"
        assert _lattice_route(board, out, "--per-net-timeout", "0") == 0
        captured = capsys.readouterr().out
        assert "the lattice negotiation is UNBOUNDED" in captured.replace("\n", " ")

    def test_lattice_labels_timeout_as_a_hard_cap(self, board, tmp_path, capsys):
        out = tmp_path / "out.kicad_pcb"
        _lattice_route(board, out, "--timeout", "0.001")
        captured = capsys.readouterr().out
        assert "Timeout: 0.001s (hard cap on the lattice negotiation)" in captured

    def test_complete_backstop_is_reported_instead_of_unbounded(self, board, tmp_path, capsys):
        """Issue #4765: ``--complete --per-net-timeout 0`` IS bounded.

        ``_apply_complete_localization`` stamps the 60 s #4472 backstop on
        ``args._complete_link_budget_s`` BEFORE the banner runs, and
        ``_resolve_lattice_link_budget`` threads that value into the
        negotiation -- so keying the banner off the raw flags claimed the
        exact opposite of what the run does.
        """
        out = tmp_path / "out.kicad_pcb"
        _lattice_route(board, out, "--complete", "--per-net-timeout", "0")
        captured = capsys.readouterr().out.replace("\n", " ")
        assert "UNBOUNDED" not in captured
        assert (
            f"the lattice negotiation is bounded at "
            f"{_COMPLETE_LINK_BUDGET_DEFAULT_S:g}s per link" in captured
        )

    def test_unbounded_line_is_byte_identical_without_the_stamp(self, board, tmp_path, capsys):
        """No stamp + no --timeout still prints the original wording."""
        out = tmp_path / "out.kicad_pcb"
        assert _lattice_route(board, out, "--per-net-timeout", "0") == 0
        captured = capsys.readouterr().out
        assert (
            "  Per-net timeout: disabled (0) -- the lattice negotiation "
            "is UNBOUNDED; pass --timeout to cap it" in captured
        )

    def test_grid_banner_is_unannotated(self, board, tmp_path, capsys):
        out = tmp_path / "out.kicad_pcb"
        route_main(
            [
                str(board),
                "-o",
                str(out),
                "--route-engine",
                "grid",
                "--strategy",
                "basic",
                "--backend",
                "cpp",
                "--layers",
                "2",
                "--no-cache",
                "--per-net-timeout",
                "20",
            ]
        )
        captured = capsys.readouterr().out
        assert "Per-net timeout: 20.0s" in captured
        assert "per-LINK budget" not in captured
