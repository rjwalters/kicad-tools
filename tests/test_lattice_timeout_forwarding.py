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
