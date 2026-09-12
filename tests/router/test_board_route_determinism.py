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

These tests independently re-route a board's committed UNROUTED PCB twice with the
production route flags and assert the UUID-normalized routed COPPER (the
``(segment ...)`` / ``(via ...)`` / ``(arc ...)`` set) is byte-identical.
Board 02 also replays the first run's cache and compares its final copper.

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
    module: str = "kicad_tools.cli"


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
        module="kicad_tools.cli.route_cmd",
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


def _normalize_copper(pcb_text: str) -> list[str]:
    """Compare complete copper records, ignoring only UUID/tstamp and order.

    Native/routed files use multiline records. Matching their opening lines
    alone counts segments/vias but discards the geometry the witness must test.
    """
    from kicad_tools.sexp import parse_string

    wrapper = parse_string("(normalization " + pcb_text + ")")
    root = wrapper.find_child("kicad_pcb") or wrapper
    records = []
    for node in root.children:
        if node.is_atom or node.name not in ("segment", "via", "arc"):
            continue
        for key in ("uuid", "tstamp"):
            for identity in node.find_children(key):
                node.children.remove(identity)
        records.append(node.to_string())
    return sorted(records)


def _route_once(board: str, out_pcb: Path, log: Path, cache_home: Path) -> None:
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
        config.module,
        *(["route"] if config.module == "kicad_tools.cli" else []),
        str(input_pcb),
        "--output",
        str(out_pcb),
        *config.flags,
    ]
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = "42"
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
    """Compare independent cold routes and, for board 02, a cache replay."""
    norms: list[list[str]] = []
    runs = [("cold-1", "cache-1"), ("cold-2", "cache-2")]
    if board == "02":
        runs.append(("warm-1", "cache-1"))
    for label, cache in runs:
        out_pcb = tmp_path / f"{label}.kicad_pcb"
        log = tmp_path / f"{label}.log"
        _route_once(board, out_pcb, log, tmp_path / cache)
        assert out_pcb.exists() and out_pcb.stat().st_size > 0, (
            f"Run {label} produced no routed PCB. Log tail:\n"
            f"{log.read_text()[-2000:] if log.exists() else '(no log)'}"
        )
        if board == "02":
            expected = "Cache HIT" if label.startswith("warm") else "Cache MISS"
            assert expected in log.read_text(), f"{label} did not exercise {expected}: {log}"
        norm = _normalize_copper(out_pcb.read_text())
        assert norm, f"Board {board} run {label} produced no routed copper."
        if norms and norm != norms[0]:
            only_first = sorted(set(norms[0]) - set(norm))[:10]
            only_current = sorted(set(norm) - set(norms[0]))[:10]
            pytest.fail(
                f"Board {board} copper differs between cold-1 and {label} with "
                f"seed 42, --deterministic-budget and PYTHONHASHSEED=42.\n"
                f"  cold-1 records: {len(norms[0])}; {label} records: {len(norm)}\n"
                f"  only in cold-1 (up to 10): {only_first}\n"
                f"  only in {label} (up to 10): {only_current}\n"
                f"  PCBs preserved at {tmp_path}"
            )
        norms.append(norm)


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
    assert all("uuid" not in record for record in norm_a)
    assert not any("gr_line" in line for line in norm_a)


@pytest.mark.timeout(600)
def test_board02_route_is_reproducible(tmp_path: Path) -> None:
    """Compare two independent cold routes and one warm replay at seed 42.

    Private cache directories guarantee the first two invocations actually
    route. The third shares only the first cache, proving cached finalization
    has the same copper. Each invocation keeps the production 240 s deadline;
    the existing 600 s test budget also covers subprocess and comparison work.
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


@pytest.mark.parametrize(
    "recipe,function", [("generate_design.py", "route_pcb"), ("route_demo.py", "main")]
)
def test_board02_determinism_uses_actual_recipe_command(recipe, function):
    """Exercise the same route mode, power-net participation and grid as production."""
    import ast

    source = REPO_ROOT / _BOARD_CONFIG["02"].directory / recipe
    tree = ast.parse(source.read_text())
    entry = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function
    )
    command = next(
        node.value
        for node in ast.walk(entry)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "cmd" for target in node.targets)
        and isinstance(node.value, ast.List)
    )
    flags_start = next(
        index
        for index, value in enumerate(command.elts)
        if isinstance(value, ast.Constant) and value.value == "--strategy"
    )
    actual_flags = [ast.literal_eval(value) for value in command.elts[flags_start:]]
    assert _BOARD_CONFIG["02"].flags == actual_flags
    assert getattr(_BOARD_CONFIG["02"], "module", "kicad_tools.cli") == ast.literal_eval(
        command.elts[2]
    )


def test_normalize_copper_retains_multiline_coordinates():
    first = '(kicad_pcb (segment\n (start 1 2)\n (end 3 4)\n (width .2) (layer "F.Cu") (net 1)\n (uuid "a")))'
    moved = first.replace("(end 3 4)", "(end 3 5)")
    assert _normalize_copper(first) != _normalize_copper(moved)
    assert _normalize_copper(first) == _normalize_copper(first.replace('"a"', '"b"'))
