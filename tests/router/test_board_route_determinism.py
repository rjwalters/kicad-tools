"""Routed-copper determinism regression for boards 02 / 04 (Issue #3799).

Background
---------
``--seed 42`` only seeds Python's global ``random``; it does NOT control
the per-net A* WALL-CLOCK cutoff (``--per-net-timeout``, default 30 s)
checked inside the C++ A* loop (``cpp_backend.py``).  On a loaded machine
that budget fires mid-search and the net lands less copper -- SAME seed,
DIFFERENT copper (the observed board-04 153 / 145 / 145-segment
divergence).

``--deterministic-budget`` (Issue #3538) swaps that wall-clock cutoff for
a fixed node-expansion ITERATION budget, so the abort point is
machine-independent and the seed-42 route is byte-identical across
machines.  Boards 02 / 03 / 04 opt into it in their
``generate_design.py:route_pcb()`` recipe (this issue), combined with a
pinned ``PYTHONHASHSEED=42``.

These tests re-route a board's committed UNROUTED PCB twice with the
production route flags and assert the UUID-normalized routed COPPER (the
``(segment ...)`` / ``(via ...)`` / ``(arc ...)`` set) is byte-identical.

* ``board 02`` routes in ~20-30 s, so its test runs UNCONDITIONALLY (PR
  CI included) -- it is the fast regression backstop.
* ``board 04`` takes longer (a fuller route + auto-fix passes); its test
  is gated behind ``KICAD_RUN_SLOW_BOARD04_DETERMINISM=1`` so ``pnpm
  check:ci`` stays fast, mirroring the ``KICAD_RUN_SLOW_BOARD06_DETERMINISM``
  convention in ``test_board06_determinism.py``.

Negative control (NOT asserted here): WITHOUT ``--deterministic-budget``,
a tight ``--per-net-timeout 0.05`` makes the wall-clock cutoff bind and
the copper diverges under load.  That divergence is load-dependent (it is
the whole point of the bug), so asserting it would be FLAKY on an unloaded
CI runner -- it is documented as the failure mode the positive test
guards against, not encoded as a hard assertion.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


class _BoardRoute(NamedTuple):
    """A board's unrouted-PCB location and production ``kct route`` flags."""

    directory: str
    stem: str
    flags: list[str]


# Per-board production route flags -- MUST mirror the ``kct route`` argv in
# each board's ``generate_design.py:route_pcb()``.  Keep in sync.
_BOARD_CONFIG: dict[str, _BoardRoute] = {
    "02": _BoardRoute(
        directory="boards/02-charlieplex-led",
        stem="charlieplex_3x3",
        flags=[
            "--strategy",
            "negotiated",
            "--iterations",
            "30",
            "--deterministic-budget",
            "--timeout",
            "240",
            "--seed",
            "42",
            "--skip-nets",
            "GND",
            "--manufacturer",
            "jlcpcb",
        ],
    ),
    "04": _BoardRoute(
        directory="boards/04-stm32-devboard",
        stem="stm32_devboard",
        flags=[
            "--mfr",
            "jlcpcb-tier1",
            "--auto-fix",
            "--auto-layers",
            "--auto-mfr-tier",
            "--placement-feedback",
            "--micro-via-in-pad-fallback",
            "--seed",
            "42",
            "--deterministic-budget",
            "--timeout",
            "600",
        ],
    ),
}

_COPPER_LINE_RE = re.compile(r"^\s*\((segment|via|arc)\b")
_UUID_RE = re.compile(r'\(uuid "[^"]*"\)')
_TSTAMP_RE = re.compile(r"\(tstamp [^)]*\)")


def _normalize_copper(pcb_text: str) -> list[str]:
    """Return the sorted, UUID/tstamp-stripped routed-copper line set.

    Keeps only ``(segment|via|arc)`` lines, strips the per-element UUID /
    tstamp tokens (deterministic per-seed, but stripped defensively so a
    UUID-toggle regression surfaces as a CONTENT mismatch rather than
    masking a real routing-path divergence), and sorts so the ORDER of
    elements in the file does not matter -- only the SET of copper geometry.
    """
    lines: list[str] = []
    for line in pcb_text.splitlines():
        if not _COPPER_LINE_RE.match(line):
            continue
        line = _UUID_RE.sub('(uuid "X")', line)
        line = _TSTAMP_RE.sub("(tstamp X)", line)
        lines.append(line)
    lines.sort()
    return lines


def _route_once(board: str, out_pcb: Path, log: Path) -> None:
    """Route a board's unrouted PCB once with the production flags.

    Pins ``PYTHONHASHSEED=42`` on the subprocess (mirrors the recipe) so
    dict/set string-iteration entropy cannot re-enter.  Tolerates the
    non-zero exit codes ``kct route`` returns on partial routing (2/3) --
    the routed PCB is still written; a missing/empty output (fatal crash)
    fails the existence check in the caller.
    """
    config = _BOARD_CONFIG[board]
    board_dir = REPO_ROOT / config.directory
    input_pcb = board_dir / "output" / f"{config.stem}.kicad_pcb"
    assert input_pcb.exists(), (
        f"Unrouted PCB not found: {input_pcb}.  Run the board recipe once to generate it."
    )

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "route",
        str(input_pcb),
        "--output",
        str(out_pcb),
        *config.flags,
    ]
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = "42"
    with log.open("w") as log_fh:
        subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            timeout=1800,
            check=False,
        )


def _assert_route_reproducible(board: str, tmp_path: Path) -> None:
    """Route ``board`` twice and assert byte-identical normalized copper."""
    norms: list[list[str]] = []
    for i in (1, 2):
        out_pcb = tmp_path / f"run-{i}.kicad_pcb"
        log = tmp_path / f"run-{i}.log"
        _route_once(board, out_pcb, log)
        assert out_pcb.exists() and out_pcb.stat().st_size > 0, (
            f"Run {i} produced no routed PCB.  Log tail:\n"
            f"{log.read_text()[-2000:] if log.exists() else '(no log)'}"
        )
        norms.append(_normalize_copper(out_pcb.read_text()))

    # Both runs must land at least some copper (guards against a silent
    # all-failed route passing the equality check trivially).
    assert norms[0], f"Board {board} run 1 produced no routed copper at all."

    if norms[0] != norms[1]:
        # Build a compact diff for the failure message.
        only_1 = sorted(set(norms[0]) - set(norms[1]))[:10]
        only_2 = sorted(set(norms[1]) - set(norms[0]))[:10]
        pytest.fail(
            f"Board {board} routed copper diverged across two seed-42 + "
            f"--deterministic-budget + PYTHONHASHSEED=42 routes (Issue "
            f"#3799 regression).\n"
            f"  run1 copper lines: {len(norms[0])}\n"
            f"  run2 copper lines: {len(norms[1])}\n"
            f"  only in run1 (up to 10): {only_1}\n"
            f"  only in run2 (up to 10): {only_2}\n"
            f"  PCBs preserved at {tmp_path}"
        )


def test_normalize_copper_strips_uuid_and_sorts() -> None:
    """``_normalize_copper`` keeps copper, strips UUIDs, and is order-free."""
    pcb_a = (
        '  (segment (start 1 2) (end 3 4) (uuid "aaaa"))\n'
        '  (via (at 5 6) (uuid "bbbb"))\n'
        "  (gr_line (start 0 0) (end 1 1))\n"  # non-copper, dropped
    )
    pcb_b = (
        '  (via (at 5 6) (uuid "different"))\n'  # reordered + different UUID
        '  (segment (start 1 2) (end 3 4) (uuid "cccc"))\n'
    )
    norm_a = _normalize_copper(pcb_a)
    norm_b = _normalize_copper(pcb_b)
    # Non-copper line dropped, UUIDs stripped, sort makes order irrelevant.
    assert norm_a == norm_b
    assert all('uuid "X"' in line for line in norm_a)
    assert not any("gr_line" in line for line in norm_a)


def test_board02_route_is_reproducible(tmp_path: Path) -> None:
    """Board 02 routes byte-identical copper twice at seed 42 (Issue #3799).

    Runs unconditionally (board 02 routes in ~20-30 s): the fast
    determinism regression backstop for the ``--deterministic-budget``
    opt-in.  If this fails, a board-02 route flag regressed (most likely
    ``--deterministic-budget`` was dropped, re-introducing the per-net
    wall-clock cutoff).
    """
    _assert_route_reproducible("02", tmp_path)


@pytest.mark.skipif(
    os.environ.get("KICAD_RUN_SLOW_BOARD04_DETERMINISM") != "1",
    reason=(
        "Slow board-04 determinism test (two full routes + auto-fix passes). "
        "Set KICAD_RUN_SLOW_BOARD04_DETERMINISM=1 to enable."
    ),
)
def test_board04_route_is_reproducible(tmp_path: Path) -> None:
    """Board 04 routes byte-identical copper twice at seed 42 (Issue #3799).

    Gated behind ``KICAD_RUN_SLOW_BOARD04_DETERMINISM=1`` (mirrors the
    board-06 slow gate) so ``pnpm check:ci`` stays fast.  Board 04 is the
    board where the wall-clock-budget divergence was first observed
    (153 / 145 / 145 segments); this proves ``--deterministic-budget``
    makes its seed-42 route reproducible.
    """
    _assert_route_reproducible("04", tmp_path)
