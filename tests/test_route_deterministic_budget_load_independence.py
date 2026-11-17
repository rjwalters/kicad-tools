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

import importlib.util
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


def _load_normalize_copper_module():
    """Import ``scripts/ci/normalize_copper.py`` as a module.

    Issue #5586: this module's ``_normalized_copper`` used to be an
    independent line-based reimplementation (``grep``-equivalent regex over
    raw lines) that kept only the bare ``(segment|via|arc)`` HEADER line of
    each MULTI-LINE s-expression node this repo actually writes, discarding
    every ``(start ...)`` / ``(end ...)`` / ``(width ...)`` / ``(layer ...)``
    / ``(net ...)`` child.  That degenerated the comparison to
    ``segment_count == segment_count && via_count == via_count`` -- two
    routes placing every trace on a different path, layer, or width compared
    EQUAL.  Delegating to the shared, paren-balanced
    ``scripts/ci/normalize_copper.py`` helper (added by #5580/#5585) fixes
    that blind spot and keeps this test in lockstep with the shell gate it
    mirrors.
    """
    path = REPO_ROOT / "scripts" / "ci" / "normalize_copper.py"
    spec = importlib.util.spec_from_file_location("ci_normalize_copper", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_normalize_copper_module = _load_normalize_copper_module()


def _cpp_available() -> bool:
    """True when the C++ router extension is importable in this worktree."""
    try:
        import kicad_tools.router.router_cpp  # noqa: F401

        return True
    except ImportError:
        return False


def _normalized_copper(pcb_path: Path) -> list[str]:
    """Return the sorted, UUID-stripped routed-copper records of *pcb_path*.

    Delegates to ``scripts/ci/normalize_copper.py::normalize_copper``, which
    mirrors ``scripts/ci/board_route_determinism_smoke.sh``: whole,
    paren-balanced ``(segment ...)`` / ``(via ...)`` / ``(arc ...)`` nodes
    (geometry, width, layer, net included), per-element ``uuid``/``tstamp``
    children stripped (deterministic per-seed but stripped defensively so a
    UUID toggle regression cannot mask a true copper divergence), sorted so
    element ORDER in the file does not matter -- only the SET of copper
    geometry.
    """
    return _normalize_copper_module.normalize_copper(pcb_path.read_text())


# ---------------------------------------------------------------------------
# Positive control for ``_normalized_copper`` (#5586 AC): prove the wrapper
# used by the (slow, C++-backend-gated) tests below actually discriminates
# routed-copper geometry rather than degenerating to element COUNTS, the
# way the superseded line-based implementation did.  Synthetic, fast, and
# unconditional -- no real route or C++ backend required.  Mirrors
# ``tests/test_ci_normalize_copper.py``'s coverage of the sibling shell gate.
# ---------------------------------------------------------------------------

_CONTROL_SEGMENT = """\t(segment
\t\t(start 12 26)
\t\t(end 14.0135 23.9865)
\t\t(width 0.2)
\t\t(layer "F.Cu")
\t\t(uuid "seg-uuid-1")
\t\t(net 3)
\t)"""

_CONTROL_VIA = """\t(via
\t\t(at 14.0135 23.9865)
\t\t(size 0.6)
\t\t(drill 0.3)
\t\t(layers "F.Cu" "B.Cu")
\t\t(uuid "via-uuid-1")
\t\t(net 3)
\t)"""


def _control_pcb(*nodes: str) -> str:
    body = "\n".join(nodes)
    return f'(kicad_pcb\n\t(version 20241229)\n\t(generator "kicad-tools")\n{body}\n)\n'


def test_normalized_copper_detects_a_single_coordinate_change(tmp_path: Path) -> None:
    pcb_text = _control_pcb(_CONTROL_SEGMENT, _CONTROL_VIA)
    moved_text = pcb_text.replace("(start 12 26)", "(start 12 27)", 1)
    assert moved_text != pcb_text

    pcb = tmp_path / "a.kicad_pcb"
    moved = tmp_path / "b.kicad_pcb"
    pcb.write_text(pcb_text)
    moved.write_text(moved_text)

    assert _normalized_copper(pcb) != _normalized_copper(moved), (
        "_normalized_copper is blind to a moved trace -- it has regressed to "
        "comparing element counts (the #5586 bug)"
    )


def test_normalized_copper_ignores_emission_order(tmp_path: Path) -> None:
    pcb = tmp_path / "a.kicad_pcb"
    reordered = tmp_path / "b.kicad_pcb"
    pcb.write_text(_control_pcb(_CONTROL_SEGMENT, _CONTROL_VIA))
    reordered.write_text(_control_pcb(_CONTROL_VIA, _CONTROL_SEGMENT))

    assert _normalized_copper(pcb) == _normalized_copper(reordered)


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
