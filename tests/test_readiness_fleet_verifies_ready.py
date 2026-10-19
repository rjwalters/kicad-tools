"""Every committed-``ready`` board must verify ``ready`` (Issue #7355).

The producer (``kct readiness --generate``, ``refresh_readiness.py``) and
``kct readiness --verify`` once disagreed on board 04 and nothing noticed: the
committed ``readiness.json`` said ``ready`` while ``--verify`` blocked.  This
fleet-wide guard enumerates ``boards/*/output/readiness.json`` and re-verifies
each board whose committed status is ``ready``; a committed ``blocked`` board
is never asserted ready.

* The engine-faked tests need no KiCad and always run.
* The native test needs the pinned evidence toolchain (``KCT_KICAD_DOCKER=1``
  or a matching kicad-cli) and skips with the refusal's own reason otherwise.
"""

from __future__ import annotations

import functools
import json
import shutil
from pathlib import Path

import pytest

from kicad_tools.cli import readiness_cmd
from kicad_tools.cli.kicad_toolchain import KiCadEnvironmentError, evidence_toolchain
from tests.test_kct_readiness_cmd import make_board, run
from tests.test_readiness_known_issues_7261 import (
    CHECKED_AT,
    SPEC,
    VIA_A,
    VIA_B,
    VIA_C,
    _LocatedErrorEngines,
)

BOARDS_DIR = Path(__file__).resolve().parent.parent / "boards"

# Boards whose committed ``ready`` evidence does not verify natively, keyed by
# board directory name.  Each entry cites the issue that owns the fix; remove
# it when that lands (strict xfail turns an unexpected pass into a failure).
EXPECTED_NOT_READY: dict[str, str] = {}


def committed_status(board: Path) -> str | None:
    report = board / "output" / "readiness.json"
    return json.loads(report.read_text()).get("status") if report.is_file() else None


def committed_ready_boards(boards_dir: Path) -> list[Path]:
    """Boards whose committed ``output/readiness.json`` claims ``ready``."""
    return sorted(p for p in boards_dir.glob("*") if committed_status(p) == "ready")


@functools.cache
def _toolchain_refusal() -> str | None:
    """Why the pinned evidence toolchain is unavailable, or ``None`` if it is."""
    try:
        with evidence_toolchain():
            return None
    except KiCadEnvironmentError as exc:
        return str(exc)


# ---------------------------------------------------------------------------
# Engine-faked: always runs
# ---------------------------------------------------------------------------


def test_enumeration_selects_only_committed_ready_boards(tmp_path):
    for name, status in (("a", "ready"), ("b", "blocked"), ("c", "ready")):
        out = tmp_path / name / "output"
        out.mkdir(parents=True)
        (out / "readiness.json").write_text(json.dumps({"status": status}))
    (tmp_path / "d" / "output").mkdir(parents=True)  # no committed report

    assert [b.name for b in committed_ready_boards(tmp_path)] == ["a", "c"]


def test_committed_boards_are_enumerated():
    ready = {b.name for b in committed_ready_boards(BOARDS_DIR)}
    assert "04-stm32-devboard" in ready  # the board that disagreed (#7355)
    assert "09-usbc-pd-power" not in ready  # committed blocked: never asserted ready


def test_committed_ready_with_exact_error_allowance_verifies_ready(tmp_path):
    board = make_board(tmp_path)
    fake = _LocatedErrorEngines(
        board, [VIA_A, VIA_B, VIA_C], meta_overall="FAILED", drc_status="FAILED"
    )
    code, report = run(board, fake, "--checked-at", CHECKED_AT, "--known-issue", SPEC)
    assert code == 0 and report["status"] == "ready", report.get("blockers")
    assert committed_status(board) == "ready"

    verified = readiness_cmd.main(
        [str(board), "--verify", "--mfr", "jlcpcb", "--checked-at", CHECKED_AT],
        engines=fake.bundle(),
    )
    result = json.loads(
        (board / "output" / "readiness-verification" / "readiness.json").read_text()
    )
    assert verified == 0 and result["status"] == "ready", result.get("blockers")


def test_verify_blocks_when_the_committed_allowance_no_longer_matches(tmp_path):
    board = make_board(tmp_path)
    fake = _LocatedErrorEngines(
        board, [VIA_A, VIA_B, VIA_C], meta_overall="FAILED", drc_status="FAILED"
    )
    code, _ = run(board, fake, "--checked-at", CHECKED_AT, "--known-issue", SPEC)
    assert code == 0

    moved = _LocatedErrorEngines(
        board,
        [VIA_A, ("dimension_via_drill", 99.0, 99.0), VIA_C],
        meta_overall="FAILED",
        drc_status="FAILED",
    )
    verified = readiness_cmd.main(
        [str(board), "--verify", "--mfr", "jlcpcb", "--checked-at", CHECKED_AT],
        engines=moved.bundle(),
    )
    result = json.loads(
        (board / "output" / "readiness-verification" / "readiness.json").read_text()
    )
    assert verified != 0 and result["status"] == "blocked"


# ---------------------------------------------------------------------------
# Native: pinned toolchain only
# ---------------------------------------------------------------------------

NATIVE_BOARDS = committed_ready_boards(BOARDS_DIR)


@pytest.mark.slow
@pytest.mark.parametrize("board", NATIVE_BOARDS, ids=lambda p: p.name)
def test_committed_ready_board_verifies_ready_natively(board: Path, tmp_path, request) -> None:
    refusal = _toolchain_refusal()
    if refusal is not None:
        pytest.skip(f"pinned evidence toolchain unavailable: {refusal}")
    if board.name in EXPECTED_NOT_READY:
        request.applymarker(pytest.mark.xfail(reason=EXPECTED_NOT_READY[board.name], strict=True))

    # ``--verify`` writes ``output/readiness-verification/``; keep the tree clean.
    copy = tmp_path / board.name
    shutil.copytree(board, copy)
    code = readiness_cmd.main([str(copy), "--verify", "--checked-at", CHECKED_AT])
    result = json.loads((copy / "output" / "readiness-verification" / "readiness.json").read_text())
    assert code == 0 and result["status"] == "ready", result.get("blockers")
