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
``(segment ...)`` / ``(via ...)`` / ``(arc ...)`` multiset) is identical.
Each run starts with an empty isolated cache so both execute the search.
Cold/warm cache equivalence is a separate regression tracked in #5261.

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

import ast
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import NamedTuple

import pytest

from kicad_tools.sexp import SExp, parse_string

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
            "--no-auto-pour",
            "--no-auto-layers",
            "--grid",
            "0.1",
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


@pytest.mark.parametrize("recipe", ["generate_design.py", "route_demo.py"])
def test_board02_determinism_flags_match_production_recipe(recipe: str) -> None:
    """Exercise the shipped all-net recipe, not an obsolete auto-grid route."""
    source = REPO_ROOT / _BOARD_CONFIG["02"].directory / recipe
    tree = ast.parse(source.read_text())
    commands = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "cmd" for target in node.targets)
        and isinstance(node.value, ast.List)
        and any(
            isinstance(item, ast.Constant) and item.value == "kicad_tools.cli.route_cmd"
            for item in node.value.elts
        )
    ]
    assert len(commands) == 1, f"Expected one production routing command in {recipe}"
    elements = commands[0].elts
    start = next(
        index
        for index, item in enumerate(elements)
        if isinstance(item, ast.Constant) and item.value == "--strategy"
    )
    production_flags = [ast.literal_eval(item) for item in elements[start:]]
    assert _BOARD_CONFIG["02"].flags == production_flags


def _normalize_copper(pcb_text: str) -> list[str]:
    """Compare complete copper expressions, ignoring IDs and file layout.

    Preserve multiplicity and all geometry, net, and layer attributes.
    Only direct board children count, excluding nested footprint drawings.
    """
    root = parse_string("(fixture " + pcb_text + ")")
    if len(root.children) == 1 and root.children[0].name == "kicad_pcb":
        root = root.children[0]

    def canonical(node: SExp) -> object:
        if node.name is None:
            return node.value
        return [
            node.name,
            *[canonical(child) for child in node.children if child.name not in {"uuid", "tstamp"}],
        ]

    return sorted(
        json.dumps(canonical(child))
        for child in root.children
        if child.name in {"segment", "via", "arc"}
    )


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
    # Both invocations must execute routing, independent of ambient cache
    # state. A warm hit bypasses the deterministic search under test (#5261).
    cache_home = out_pcb.parent / f"{out_pcb.stem}-cache"
    cache_home.mkdir()
    env["XDG_CACHE_HOME"] = str(cache_home)
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
        only_1 = sorted((Counter(norms[0]) - Counter(norms[1])).elements())[:10]
        only_2 = sorted((Counter(norms[1]) - Counter(norms[0])).elements())[:10]
        pytest.fail(
            f"Board {board} routed copper diverged across two seed-42 + "
            f"--deterministic-budget + PYTHONHASHSEED=42 routes (Issue "
            f"#3799 regression).\n"
            f"  run1 copper records: {len(norms[0])}\n"
            f"  run2 copper records: {len(norms[1])}\n"
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
    assert len(norm_a) == 2
    assert not any("uuid" in line for line in norm_a)
    assert not any("gr_line" in line for line in norm_a)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("(end 3 4)", "(end 3 5)"),
        ("(width 0.2)", "(width 0.3)"),
        ('(layer "F.Cu")', '(layer "B.Cu")'),
        ("(net 1)", "(net 2)"),
    ],
)
def test_normalize_copper_detects_multiline_changes(before: str, after: str) -> None:
    copper = '(segment\n (start 1 2)\n (end 3 4)\n (width 0.2)\n (layer "F.Cu")\n (net 1))'
    pcb = "(kicad_pcb\n" + copper + ")"
    assert _normalize_copper(pcb) != _normalize_copper(pcb.replace(before, after))


def test_normalize_copper_layout_ids_and_multiplicity() -> None:
    copper = '(arc (start 1 2) (mid 2 3) (end 3 4) (width 0.2) (layer "F.Cu") (net 1))'
    plain = _normalize_copper("(kicad_pcb " + copper + ")")
    multiline = copper.replace(" (", "\n (")[:-1] + ' (uuid "new") (tstamp old))'
    assert plain == _normalize_copper("(kicad_pcb " + multiline + ")")
    assert plain != _normalize_copper("(kicad_pcb " + copper + copper + ")")
    assert not _normalize_copper("(kicad_pcb (footprint " + copper + "))")


@pytest.mark.timeout(600)
def test_board02_route_is_reproducible(tmp_path: Path) -> None:
    """Board 02 routes byte-identical copper twice at seed 42 (Issue #3799).

    Runs UNCONDITIONALLY (PR CI included): the fast determinism
    regression backstop for the ``--deterministic-budget`` opt-in.  If
    this fails, a board-02 route flag regressed (most likely
    ``--deterministic-budget`` was dropped, re-introducing the per-net
    wall-clock cutoff).

    Timeout (Issue #3799 CI fix): a single board-02 route takes ~20-30 s
    locally and this test routes TWICE, so ~40-60 s of wall-clock.  CI's
    suite-wide default ``--timeout=60`` (see ``.github/workflows/ci.yml``
    Test job) killed the two-route run spuriously on the slower hosted
    runner.  The explicit ``@pytest.mark.timeout(600)`` marker OVERRIDES
    that default with a host-speed- and xdist-contention-tolerant budget
    while still catching a genuine router hang.  It does NOT slow the
    happy path (the marker only changes the reaper deadline).  This keeps
    the determinism regression running in the main Test job rather than
    deferring it to the nightly slow-tests workflow.
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
