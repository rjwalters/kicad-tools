"""End-to-end load-independence proof for ``--deterministic-budget`` (Issue #3877).

The unit tests in ``test_route_deterministic_budget.py`` prove the
*normalization wiring* is correct (per-net wall-clock cutoff disabled, fixed
iteration backstop pinned, flag forwarded through both parsers).  This module
proves the *observable consequence*: a real board routed TWICE with
``--deterministic-budget`` at the same ``--seed`` produces byte-identical
routed copper.

WHY this matters (the bug #3877 closes)
---------------------------------------
``--seed`` only seeds Python's global ``random``.  Under the legacy
``--per-net-timeout`` recipe the per-net A* search is bounded by a WALL-CLOCK
budget checked inside the C++ loop, so on a loaded/slow machine that budget
fires mid-search and the net lands LESS copper -- SAME seed, DIFFERENT
output.  That load-sensitivity is exactly why the chorus measurement swung
8/51 -> 31/51 depending on machine load and why the board re-route gates
flaked.  ``--deterministic-budget`` (#3538) swaps the wall-clock cutoff for a
fixed node-expansion ITERATION backstop, so each per-net search either finds a
path or aborts after the SAME amount of work on EVERY machine.

We cannot synthesize machine load inside a unit test, but iteration-bounded
routing is reproducible run-to-run on the SAME machine -- and the run-to-run
invariant is the same mechanism that makes it machine-INdependent (the binding
constraint is a fixed integer, not wall-clock).  So we assert run-to-run
byte-identical routed copper, which is the load-independence guarantee in
practice.

These tests are ``slow``/``integration`` (they invoke the real ``kct route``
CLI on a committed board fixture) and are skipped when the C++ backend is not
built.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Board-01 (voltage divider) is the smallest committed board fixture (~12
#: nets) so two full routes complete in well under a minute even on a loaded
#: host -- ideal for a determinism assertion that must run in CI.
BOARD_01_UNROUTED = (
    REPO_ROOT / "boards" / "01-voltage-divider" / "output" / "voltage_divider.kicad_pcb"
)

#: Copper-element prefixes that constitute the routed output.  Pads, zones, and
#: footprints are part of the input and never change between runs.
_COPPER_RE = re.compile(r"^\s*\((segment|via|arc)\b")
_UUID_RE = re.compile(r'\(uuid "[^"]*"\)')
_TSTAMP_RE = re.compile(r"\(tstamp [^)]*\)")


def _cpp_available() -> bool:
    """True when the C++ router extension is importable in this worktree."""
    try:
        import kicad_tools.router.router_cpp  # noqa: F401

        return True
    except ImportError:
        return False


def _normalized_copper(pcb_path: Path) -> list[str]:
    """Return the sorted, UUID-stripped routed-copper lines of *pcb_path*.

    Mirrors ``scripts/ci/board_route_determinism_smoke.sh``: keep only the
    ``(segment|via|arc)`` lines, strip the per-element ``uuid``/``tstamp``
    tokens (deterministic per-seed but stripped defensively so a UUID toggle
    regression cannot mask a true copper divergence), and sort so element
    ORDER in the file does not matter -- only the SET of copper geometry.
    """
    lines = []
    for raw in pcb_path.read_text().splitlines():
        if not _COPPER_RE.match(raw):
            continue
        norm = _UUID_RE.sub('(uuid "X")', raw)
        norm = _TSTAMP_RE.sub("(tstamp X)", norm)
        lines.append(norm)
    return sorted(lines)


def _route(board: Path, output: Path) -> subprocess.CompletedProcess[str]:
    """Route *board* to *output* with the deterministic-budget recipe."""
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "route",
        str(board),
        "--output",
        str(output),
        "--manufacturer",
        "jlcpcb",
        "--backend",
        "cpp",
        "--deterministic-budget",
        # Outer wall-clock retained ONLY as a safety backstop; the iteration
        # backstop -- not this -- is the binding constraint.
        "--timeout",
        "180",
        "--seed",
        "42",
    ]
    # Pin PYTHONHASHSEED so dict/set string-iteration entropy cannot re-enter
    # and mask the iteration-budget determinism we are proving.
    env = {"PYTHONHASHSEED": "42"}
    import os

    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env={**os.environ, **env},
    )


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.skipif(
    not _cpp_available(),
    reason="C++ router backend not built (run `uv run kct build-native`)",
)
class TestDeterministicBudgetLoadIndependence:
    """A real route is reproducible run-to-run under ``--deterministic-budget``."""

    def test_routed_copper_is_byte_identical_across_two_runs(self, tmp_path):
        """Two routes at the same seed produce byte-identical routed copper.

        This is the load-independence guarantee in practice: because the
        per-net search is bounded by a fixed node-expansion count (not a
        wall-clock budget), the amount of work -- and therefore the copper
        landed -- does not depend on how fast/loaded the machine is, so two
        runs land the IDENTICAL geometry.
        """
        assert BOARD_01_UNROUTED.is_file(), (
            f"board-01 unrouted fixture missing: {BOARD_01_UNROUTED}. "
            "Regenerate it with the board-01 recipe."
        )

        out_a = tmp_path / "run_a.kicad_pcb"
        out_b = tmp_path / "run_b.kicad_pcb"

        result_a = _route(BOARD_01_UNROUTED, out_a)
        result_b = _route(BOARD_01_UNROUTED, out_b)

        # ``kct route`` exits 0 on a fully routed board; the tiny voltage
        # divider routes completely, but tolerate the partial-route codes
        # (2/3) defensively and let the copper comparison be the real gate.
        assert result_a.returncode in (0, 2, 3), (
            f"run A failed (rc={result_a.returncode}):\n{result_a.stderr[-2000:]}"
        )
        assert result_b.returncode in (0, 2, 3), (
            f"run B failed (rc={result_b.returncode}):\n{result_b.stderr[-2000:]}"
        )
        assert out_a.is_file() and out_b.is_file(), "both runs must write a routed PCB"

        copper_a = _normalized_copper(out_a)
        copper_b = _normalized_copper(out_b)

        # The route must actually lay copper -- an empty result would make the
        # determinism assertion vacuously true.
        assert copper_a, "run A produced no routed copper; cannot prove determinism"

        # Identical COUNT (the routed/strict reach proxy) ...
        assert len(copper_a) == len(copper_b), (
            f"routed-copper element count diverged across runs "
            f"({len(copper_a)} vs {len(copper_b)}) -- --deterministic-budget "
            "did NOT make the route load-independent."
        )

        # ... AND identical geometry (the byte-for-byte reproducibility bar).
        assert copper_a == copper_b, (
            "routed copper diverged across two --deterministic-budget runs at "
            "the same seed. The iteration-budgeted route must be reproducible "
            "run-to-run (and therefore machine-independent)."
        )
