"""``--complete`` localized per-link build wiring (Issue #4472, epic #4465).

Phase 2 restricts the lattice build to a per-link bounding box and bounds each
link's search with a wall-clock budget.  These tests exercise the CLI-layer
helpers that compute the localized region box + budget, and an end-to-end
completion pass that closes a stranded link through the localized build and
terminates within a bounded budget.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import pytest

from kicad_tools.cli.route_cmd import (
    _COMPLETE_LINK_BUDGET_DEFAULT_S,
    _apply_complete_localization,
    _resolve_complete_link_budget,
)
from kicad_tools.cli.route_cmd import (
    main as route_main,
)

# Reuse the Phase 1 stranded-board fixture text (NET1 pre-routed, NET2 stranded).
from tests.test_cli_route_complete_4471 import STRANDED_BOARD, _segment_nets


@pytest.fixture
def stranded_board(tmp_path: Path) -> Path:
    board = tmp_path / "stranded.kicad_pcb"
    board.write_text(STRANDED_BOARD)
    return board


def _loc_args(pcb: Path, **overrides) -> argparse.Namespace:
    base = {
        "complete": True,
        "_complete_noop": False,
        "nets": "NET2",
        "region": None,
        "quiet": False,
        "per_net_timeout": 30.0,
        "_region_box": None,
        "no_cache": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# _resolve_complete_link_budget: reuse the wall-clock plumbing.
# ---------------------------------------------------------------------------
class TestResolveLinkBudget:
    def test_honors_explicit_per_net_timeout(self):
        args = argparse.Namespace(per_net_timeout=12.5)
        assert _resolve_complete_link_budget(args) == 12.5

    def test_falls_back_to_default_when_disabled(self):
        # --per-net-timeout 0 (e.g. under --deterministic-budget) -> backstop.
        args = argparse.Namespace(per_net_timeout=0.0)
        assert _resolve_complete_link_budget(args) == _COMPLETE_LINK_BUDGET_DEFAULT_S

    def test_falls_back_when_missing(self):
        args = argparse.Namespace()
        assert _resolve_complete_link_budget(args) == _COMPLETE_LINK_BUDGET_DEFAULT_S


# ---------------------------------------------------------------------------
# _apply_complete_localization: stamps _region_box + budget from the pad bbox.
# ---------------------------------------------------------------------------
class TestApplyCompleteLocalization:
    def _net2_pads(self, pcb_path: Path) -> list[tuple[float, float]]:
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(pcb_path))
        pts = []
        for fp in pcb.footprints:
            for pad in fp.pads:
                if (getattr(pad, "net_name", "") or "") == "NET2":
                    pos = pcb.get_pad_position(fp.reference, pad.number)
                    if pos is not None:
                        pts.append((pos[0], pos[1]))
        return pts

    def test_stamps_localized_box_and_budget(self, stranded_board: Path, capsys):
        args = _loc_args(stranded_board)
        rc = _apply_complete_localization(args, stranded_board)
        assert rc == 0
        assert args._complete_localized is True
        assert args._complete_link_budget_s == 30.0
        assert args._region_box is not None
        assert args.no_cache is True

        # The box contains every NET2 pad (in the same board-relative frame).
        x0, y0, x1, y1 = args._region_box
        for px, py in self._net2_pads(stranded_board):
            assert x0 <= px <= x1 and y0 <= py <= y1
        # ...and is a genuine localization: smaller than the 30x20 board.
        assert (x1 - x0) < 30.0 and (y1 - y0) < 20.0
        assert "localized lattice build" in capsys.readouterr().out

    def test_respects_user_region(self, stranded_board: Path):
        # A user-supplied --region wins: no auto box is computed, but the
        # localization flag + budget are still armed (the lattice localizes to
        # the user's box, stamped later by _parse_and_apply_region).
        args = _loc_args(stranded_board, region="105,102,112,108")
        rc = _apply_complete_localization(args, stranded_board)
        assert rc == 0
        assert args._complete_localized is True
        assert args._complete_link_budget_s == 30.0
        assert args._region_box is None  # not overwritten by the auto path

    def test_noop_when_not_complete(self, stranded_board: Path):
        args = _loc_args(stranded_board, complete=False)
        rc = _apply_complete_localization(args, stranded_board)
        assert rc == 0
        assert getattr(args, "_region_box", None) is None

    def test_noop_when_complete_noop(self, stranded_board: Path):
        args = _loc_args(stranded_board, _complete_noop=True)
        rc = _apply_complete_localization(args, stranded_board)
        assert rc == 0
        assert args._region_box is None


# ---------------------------------------------------------------------------
# End-to-end: localized --complete closes the stranded link, and terminates
# within a bounded budget (the #4434 ">10 minutes" is now a per-link deadline).
# ---------------------------------------------------------------------------
class TestLocalizedCompleteEndToEnd:
    def test_closes_link_and_terminates_bounded(self, stranded_board: Path, tmp_path: Path):
        out = tmp_path / "out.kicad_pcb"
        started = time.monotonic()
        rc = route_main([str(stranded_board), "-o", str(out), "--complete", "--backend", "cpp"])
        elapsed = time.monotonic() - started
        assert rc == 0
        # The stranded link is closed by the localized build.
        assert 2 in _segment_nets(out.read_text())
        # NET1's pre-existing copper is preserved (fixed obstacle).
        assert 1 in _segment_nets(out.read_text())
        # Bounded: the localized build + per-link deadline terminate quickly --
        # generous ceiling for CI, but far below the #4434 >10-minute grind.
        assert elapsed < 120.0

    def test_localization_banner_printed(self, stranded_board: Path, tmp_path: Path, capsys):
        out = tmp_path / "out.kicad_pcb"
        rc = route_main([str(stranded_board), "-o", str(out), "--complete", "--backend", "cpp"])
        assert rc == 0
        assert "localized lattice build" in capsys.readouterr().out
