"""The hybrid retry reports its furthest-progress decline (issue #7931).

``_route_pair_hybrid_end`` and ``_route_pair_hybrid`` used to latch the FIRST
non-budget decline.  On board 03 (USB_D J1.A6->U2.3) the first start tried is
a hooked one whose trunk degenerates, so the log said
``trunk-offset-degenerate`` while later hook-free starts routed a trunk and
failed at the leg (``leg-pad-escape-end``).  This is a diagnostics change only:
the ranking picks which reason is REPORTED, never which copper is routed.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.lattice.pathfinder import (
    LatticePathfinder,
    _further_hybrid_decline,
    _hybrid_decline_rank,
)
from tests.router.lattice.test_pair_hybrid_6446 import _board, _route, _signature

_PRE_TRUNK = (
    "pair-hybrid-anchor",
    "pair-hybrid-trunk-offset-degenerate",
    "pair-hybrid-trunk-intra-clearance",
    "pair-hybrid-trunk-no-path",
    "pair-hybrid-trunk-short",
    "pair-hybrid-trunk-pad-class",
    "pair-hybrid-deadline",
    "pair-hybrid-no-trunk-start",
)
_POST_TRUNK = (
    "pair-hybrid-leg-pad-escape-end",
    "pair-hybrid-leg-no-path",
    "pair-hybrid-leg-end",
    "pair-hybrid-leg-pad-class",
    "pair-hybrid-via-limit",
    "pair-hybrid-junction",
)


def _furthest(reasons: list[str]) -> str:
    """Fold ``reasons`` the way both latches do."""
    best = "pair-hybrid-no-path"
    for why in reasons:
        if _further_hybrid_decline(best, why):
            best = why
    return best


def test_rank_orders_placeholder_trunk_leg_budget() -> None:
    assert _hybrid_decline_rank("pair-hybrid-no-path") == -1
    for r in _PRE_TRUNK:
        assert _hybrid_decline_rank(r) == 0, r
    for r in _POST_TRUNK:
        assert _hybrid_decline_rank(r) == 1, r
    assert _hybrid_decline_rank("pair-hybrid-leg-budget") == 2
    # The trunk's own no-path is a trunk decline, not the placeholder.
    assert _hybrid_decline_rank("pair-hybrid-trunk-no-path") == 0


@pytest.mark.parametrize("leg", _POST_TRUNK)
@pytest.mark.parametrize("trunk", _PRE_TRUNK)
def test_leg_stage_beats_trunk_stage_in_either_order(trunk: str, leg: str) -> None:
    assert _furthest([trunk, leg]) == leg
    assert _furthest([leg, trunk]) == leg


@pytest.mark.parametrize("other", _PRE_TRUNK + _POST_TRUNK)
def test_leg_budget_beats_every_other_reason(other: str) -> None:
    assert _furthest([other, "pair-hybrid-leg-budget"]) == "pair-hybrid-leg-budget"
    assert _furthest(["pair-hybrid-leg-budget", other]) == "pair-hybrid-leg-budget"


def test_ties_keep_the_first_seen() -> None:
    assert (
        _furthest(["pair-hybrid-trunk-offset-degenerate", "pair-hybrid-trunk-intra-clearance"])
        == "pair-hybrid-trunk-offset-degenerate"
    )
    assert (
        _furthest(["pair-hybrid-leg-pad-escape-end", "pair-hybrid-leg-no-path"])
        == "pair-hybrid-leg-pad-escape-end"
    )
    assert not _further_hybrid_decline("pair-hybrid-leg-budget", "pair-hybrid-leg-budget")


def test_any_real_reason_replaces_the_placeholder() -> None:
    for r in _PRE_TRUNK + _POST_TRUNK + ("pair-hybrid-leg-budget",):
        assert _further_hybrid_decline("pair-hybrid-no-path", r), r
    assert not _further_hybrid_decline("pair-hybrid-no-path", "pair-hybrid-no-path")


def _script_declines(monkeypatch: pytest.MonkeyPatch, script: list[str]) -> list[str]:
    """Make every hybrid start decline, in ``script`` order (then the last one)."""
    seen: list[str] = []

    def fake(self, pc, **kw):  # noqa: ANN001, ANN003
        why = script[min(len(seen), len(script) - 1)]
        seen.append(f"{kw['start'].describe()}|{kw['layer']}")
        return None, why, None, 0.0

    monkeypatch.setattr(LatticePathfinder, "_try_hybrid_start", fake)
    return seen


def test_the_end_note_names_the_furthest_reason_and_its_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Board 03's shape: a hooked start degenerates first, a later one routes
    # its trunk and fails at the leg, the rest degenerate again.
    seen = _script_declines(
        monkeypatch,
        [
            "pair-hybrid-trunk-offset-degenerate",
            "pair-hybrid-trunk-intra-clearance",
            "pair-hybrid-leg-pad-escape-end",
            "pair-hybrid-trunk-offset-degenerate",
        ],
    )
    pf, pc, _balls = _board()
    _route(pf, pc)
    assert pf.pair_outcomes[pc.key].startswith("uncoupled-fallback:")
    log = pf.pair_hybrid_log[pc.key]
    assert log.startswith("hybrid declined (hybrid-leg-pad-escape-end): "), log
    assert "trunk-offset-degenerate" not in log
    # The note names the THIRD start tried, the one that got to the leg.
    start_text, _layer = seen[2].split("|")
    assert f"(leg-pad-escape-end at {start_text}" in log, log
    assert " on F.Cu)" in log or " on B.Cu)" in log, log


def test_trunk_only_declines_keep_the_first_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _script_declines(
        monkeypatch,
        ["pair-hybrid-trunk-offset-degenerate", "pair-hybrid-trunk-intra-clearance"],
    )
    pf, pc, _balls = _board()
    _route(pf, pc)
    log = pf.pair_hybrid_log[pc.key]
    assert log.startswith("hybrid declined (hybrid-trunk-offset-degenerate): "), log
    start_text, _layer = seen[0].split("|")
    assert f"(trunk-offset-degenerate at {start_text}" in log, log


def test_routed_copper_is_unchanged_by_the_ranking() -> None:
    # The unpatched ring fixture still couples as a hybrid, deterministically.
    sigs = []
    for _ in range(2):
        pf, pc, _balls = _board()
        routes, _stats = _route(pf, pc)
        assert pf.pair_outcomes[pc.key] == "hybrid:U1.1"
        sigs.append(_signature(routes))
    assert sigs[0] == sigs[1]
