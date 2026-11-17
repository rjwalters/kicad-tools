"""``kct readiness --verify`` never rewrites a committed file (Issue #7536).

``--verify`` writes its diagnostics under ``output/readiness-verification/``,
inside the board.  The receipt there records the verifying engine (checkout
``commit``, ``dirty``, ``source_digest``) and BOM text that depends on the host's
parts cache, so it differs on every commit and every host.  Boards 00, 03 and 06
used to commit it, and every verify run rewrote it.

The receipts are now gitignored.  The only tracked files under that directory are
board 03's evidence copies, which its ``output/readiness.json`` hash-binds:

* the unit tests (git only) assert that every tracked receipt file is bound and
  that an unbound receipt path is ignored;
* the native test (pinned toolchain) re-runs ``--verify`` on a copy of each board
  that tracks receipt files, and asserts each one comes back byte-for-byte.  It
  skips with the toolchain's own refusal reason when the pinned kicad-cli is
  unavailable.
"""

from __future__ import annotations

import functools
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_tools.cli.kicad_toolchain import KiCadEnvironmentError, evidence_toolchain

REPO_ROOT = Path(__file__).resolve().parent.parent
RECEIPTS = "output/readiness-verification"


@functools.cache
def _git(*args: str) -> list[str] | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.splitlines() if proc.returncode == 0 else None


def tracked_receipts() -> dict[str, list[str]]:
    """``{board: [board-relative tracked receipt files]}``; skips outside a git checkout."""
    files = _git("ls-files", "--", "boards/*/output/readiness-verification/*")
    if files is None:
        pytest.skip("not a git checkout: cannot list tracked receipt files")
    by_board: dict[str, list[str]] = {}
    for path in files:
        _, board, rel = path.split("/", 2)
        by_board.setdefault(board, []).append(rel)
    return by_board


def _bound_inputs(board: str) -> set[str]:
    report = REPO_ROOT / "boards" / board / "output" / "readiness.json"
    return set(json.loads(report.read_text()).get("inputs", {}))


def test_every_tracked_receipt_file_is_bound_by_the_boards_readiness():
    receipts = tracked_receipts()
    unbound = {
        board: sorted(set(files) - _bound_inputs(board)) for board, files in receipts.items()
    }
    unbound = {board: files for board, files in unbound.items() if files}
    assert not unbound, (
        f"tracked --verify diagnostics no readiness.json binds (they go stale on every "
        f"verify; untrack them): {unbound}"
    )


@pytest.mark.parametrize(
    "path",
    [
        "boards/00-simple-led/output/readiness-verification/readiness.json",
        "boards/06-diffpair-test/output/readiness-verification/kct-check.json",
        "boards/03-usb-joystick/output/readiness-verification/readiness.json",
        "boards/09-usbc-pd-power/output/readiness-verification/native-drc.json",
    ],
)
def test_unbound_receipt_paths_are_gitignored(path):
    if _git("rev-parse", "--git-dir") is None:
        pytest.skip("not a git checkout: cannot query .gitignore")
    ignored = _git("check-ignore", "--no-index", "--", path)
    assert ignored == [path], f"{path} is not gitignored"


def test_board03_bound_receipt_copies_stay_trackable():
    if _git("rev-parse", "--git-dir") is None:
        pytest.skip("not a git checkout: cannot query .gitignore")
    path = "boards/03-usb-joystick/output/readiness-verification/native-drc.json"
    assert _git("check-ignore", "--no-index", "--", path) is None


@functools.cache
def _toolchain_refusal() -> str | None:
    try:
        with evidence_toolchain():
            return None
    except KiCadEnvironmentError as exc:
        return str(exc)


def _boards_with_tracked_receipts() -> list[str]:
    files = _git("ls-files", "--", "boards/*/output/readiness-verification/*") or []
    return sorted({path.split("/")[1] for path in files})


@pytest.mark.slow
@pytest.mark.parametrize("board", _boards_with_tracked_receipts())
def test_verify_rewrites_tracked_receipt_files_byte_for_byte(board, tmp_path):
    refusal = _toolchain_refusal()
    if refusal is not None:
        pytest.skip(f"pinned evidence toolchain unavailable: {refusal}")
    from kicad_tools.cli import readiness_cmd

    source = REPO_ROOT / "boards" / board
    copy = tmp_path / board
    shutil.copytree(source, copy)
    checked_at = json.loads((copy / "output" / "readiness.json").read_text())["checked_at"]

    code = readiness_cmd.main([str(copy), "--verify", "--checked-at", checked_at])

    assert code == 0
    changed = [
        rel
        for rel in tracked_receipts()[board]
        if not (copy / rel).is_file() or (copy / rel).read_bytes() != (source / rel).read_bytes()
    ]
    assert not changed, f"{board}: --verify rewrote committed receipt files {changed}"
