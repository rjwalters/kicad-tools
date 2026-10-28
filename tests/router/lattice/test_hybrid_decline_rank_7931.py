"""The hybrid retry reports its furthest-progress decline (issue #7931).

``_route_pair_hybrid_end`` and ``_route_pair_hybrid`` used to latch the FIRST
non-budget decline.  On board 03 (USB_D J1.A6->U2.3) the first start tried is
a hooked one whose trunk degenerates, so the log said
``trunk-offset-degenerate`` while later hook-free starts routed a trunk and
failed at the leg (``leg-pad-escape-end``).  This is a diagnostics change only:
the ranking picks which reason is REPORTED, never which copper is routed.

The ranking helpers themselves are unit-tested in
``tests/router/pairplan/test_hybrid.py``; these tests drive the real latches
through the ring fixture.
"""

from __future__ import annotations

import re

import pytest

from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from tests.router.lattice.test_pair_hybrid_6446 import _board, _route, _signature


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
    assert re.search(rf"\(leg-pad-escape-end at (F|B)\.Cu {re.escape(start_text)}\)", log), log


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
    assert re.search(rf"\(trunk-offset-degenerate at (F|B)\.Cu {re.escape(start_text)}\)", log), log


def test_routed_copper_is_unchanged_by_the_ranking() -> None:
    # The unpatched ring fixture still couples as a hybrid, deterministically.
    sigs = []
    for _ in range(2):
        pf, pc, _balls = _board()
        routes, _stats = _route(pf, pc)
        assert pf.pair_outcomes[pc.key] == "hybrid:U1.1"
        sigs.append(_signature(routes))
    assert sigs[0] == sigs[1]
