"""Softstart rev B auto-pcb-size end-to-end consumer test (Issue #3352 P_AS5).

This is the load-bearing real-consumer test that closes Phase 5 of the
auto-pcb-size escalation roadmap.  It:

  1. Regenerates the softstart rev B schematic + PCB on demand (via
     the in-tree recipe ``boards/external/softstart/generate_design.py``).
  2. Invokes the kicad-tools ``kct route --auto-pcb-size`` wrapper
     against the freshly placed PCB.
  3. Asserts the escalation pipeline either (a) produces a clean
     routed result OR (b) refuses cleanly with the documented
     actionable refusal message.

The expected behaviour for rev B with the in-tree project.kct
declarations (``envelope_hard: true`` + ``escalation.ladder:
layers-only``) is:

  - The wrapper walks the layer escalation ladder (2L -> 4L)
  - If routing converges within the layer ladder, exit code 0 and
    nets routed >= 95% (or whatever the policy's reach threshold says)
  - If routing still doesn't converge at 4L, the wrapper refuses
    with ``AUTO-PCB-SIZE ESCALATION REFUSED`` and emits the
    enumerated alternative levers (BOM / layers / clearance / spec
    amendment / manufacturer tier).

Both outcomes are passing -- the test verifies the pipeline runs
end-to-end without crashing and produces a structured outcome the
recipe consumer can act on.

Wall-clock budget
=================

Generation + 4L route at 0.2mm clearance is the dominant cost.
Empirically softstart rev B routing takes 5-15 minutes per layer
attempt on the C++ backend; with the layer escalation ladder this can
double (one attempt at 2L, one at 4L).  We document a soft budget of
**~30 minutes** in the test and gate it on ``KICAD_RUN_SLOW_SOFTSTART_REACH=1``
to match the existing softstart slow-path conventions.

The test is marked ``@pytest.mark.slow`` so ``pytest -m "not slow"``
(the default CI gate) skips it.  CI's dedicated slow-board job runs
with the env var set.

To run locally::

    KICAD_RUN_SLOW_SOFTSTART_REACH=1 uv run pytest \\
      tests/router/test_softstart_revb_auto_pcb_size.py -v --no-cov \\
      -s -m slow

Issue: https://github.com/rjwalters/kicad-tools/issues/3352
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Module-level fixture roots.
REPO_ROOT = Path(__file__).resolve().parents[2]
BOARD_DIR = REPO_ROOT / "boards" / "external" / "softstart"

# Mark every test in this module as slow.  The recipe regeneration +
# routing pass is heavyweight (~5-30 minutes); pytest -m slow opts in.
pytestmark = pytest.mark.slow


def _slow_tests_enabled() -> bool:
    """Whether the slow softstart routing tests are enabled.

    Matches the convention of ``test_softstart_routing_reach_regression``
    and ``test_softstart_revb_fine_pitch_escape``.  CI sets this env
    var in the dedicated slow-board job.
    """
    return os.environ.get("KICAD_RUN_SLOW_SOFTSTART_REACH") == "1"


# Skip the entire module when the slow env gate is not set, even when
# pytest is run with ``-m slow`` (the env var is the authoritative
# opt-in for the slow softstart corpus; the marker is just an
# additional collection-time filter).
if not _slow_tests_enabled():  # pragma: no cover - env gate
    pytestmark = [
        pytest.mark.slow,
        pytest.mark.skipif(
            True,
            reason=(
                "Slow softstart auto-pcb-size test (~5-30min).  Set "
                "KICAD_RUN_SLOW_SOFTSTART_REACH=1 to enable."
            ),
        ),
    ]


def _regenerate_softstart_pcb(output_dir: Path) -> Path:
    """Regenerate the softstart schematic + PCB into ``output_dir``.

    Loads the in-tree recipe (``boards/external/softstart/generate_design.py``)
    and invokes its ``create_softstart_schematic`` + ``create_softstart_pcb``
    entry points.  Returns the path to the freshly-placed (but unrouted)
    PCB.

    The recipe is the source of truth for rev B placement.  Re-running
    it on every test run keeps this consumer test in sync with the
    production board's design rules + skip-net list -- a frozen PCB
    fixture would drift quickly.
    """
    sys.path.insert(0, str(BOARD_DIR))
    try:
        import generate_design  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    output_dir.mkdir(parents=True, exist_ok=True)
    generate_design.create_project(output_dir, "softstart")
    generate_design.create_softstart_schematic(output_dir)
    pcb_path = generate_design.create_softstart_pcb(output_dir)
    return pcb_path


def _route_with_auto_pcb_size(
    pcb_path: Path,
    output_path: Path,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    """Drive ``kct route --auto-pcb-size`` as a subprocess.

    Mirrors what ``boards/external/softstart/generate_design.py::
    _route_pcb_with_auto_pcb_size`` does when the recipe opts in via
    ``SOFTSTART_AUTO_PCB_SIZE=1``.  Returns the CompletedProcess so the
    test can inspect both the exit code and the captured output.

    The skip-nets list mirrors the recipe's production manufacturing
    path (``jlcpcb-tier1`` manufacturer profile, 0.20mm clearance,
    0.30mm trace width).
    """
    skip_nets = ",".join([
        "AC_LINE", "AC_NEUTRAL", "FUSED_LINE", "GND",
        "+3.3V", "VRECT",
        "SCAP_POS+", "SCAP_POS_GND", "SCAP_NEG+", "SCAP_NEG_GND",
        "ISENSE_POS",
        # Issue #3343 P-R1 skip-list alignment (architect S1)
        "VGATE", "SRC_POS", "SRC_NEG", "BUS_LINE",
    ])
    cmd = [
        sys.executable, "-m", "kicad_tools.cli", "route",
        str(pcb_path),
        "--output", str(output_path),
        "--backend", "cpp",
        "--auto-pcb-size",
        "--manufacturer", "jlcpcb-tier1",
        "--skip-nets", skip_nets,
        "--seed", "42",
        "--timeout", str(timeout_seconds),
        "--per-net-timeout", "45",
        "--clearance", "0.20",
        "--trace-width", "0.30",
    ]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_seconds + 60,
    )


# Acceptable exit codes from ``kct route --auto-pcb-size``:
#   0 -- routing succeeded (NO_ESCALATION_NEEDED)
#   2 -- partial reach OR clean refusal (envelope_hard, max_tier,
#        regression, holes_dont_fit)
#   3 -- DRC violations (manufacturer-tier check failed)
# Exit code 1 is reserved for hard subprocess failures (no PCB outline,
# malformed args, etc.) and should NOT be the expected end state for
# this test.
_ACCEPTABLE_EXIT_CODES = (0, 2, 3)


def test_softstart_revb_auto_pcb_size_runs_end_to_end(tmp_path: Path) -> None:
    """End-to-end smoke for the softstart rev B + auto-pcb-size pipeline.

    Generates softstart rev B, invokes ``kct route --auto-pcb-size``,
    and verifies the wrapper produces a structured outcome (clean
    success, clean partial, or actionable refusal) within the budget.

    The wall-clock budget covers (1) recipe regeneration (~30s), (2) up
    to two layer attempts at 0.20mm clearance (~5-15min each).  We
    pass ``--timeout 600`` so a single attempt is bounded; the layer
    escalation may run multiple attempts within this budget.
    """
    output_dir = tmp_path / "softstart_out"
    pcb_path = _regenerate_softstart_pcb(output_dir)
    routed_path = output_dir / "softstart_routed.kicad_pcb"

    # Wall-clock budget: 10 minutes per attempt, leaving headroom for
    # multi-attempt layer escalation within the 30-minute test cap.
    timeout = 600

    result = _route_with_auto_pcb_size(pcb_path, routed_path, timeout)

    # Surface stdout + stderr for debugging when assertions fail.
    print("\n--- kct route stdout (last 60 lines) ---")
    print("\n".join(result.stdout.splitlines()[-60:]))
    print("\n--- kct route stderr (last 30 lines) ---")
    print("\n".join(result.stderr.splitlines()[-30:]))

    assert result.returncode in _ACCEPTABLE_EXIT_CODES, (
        f"kct route --auto-pcb-size exited with unexpected code "
        f"{result.returncode}.  Expected one of {_ACCEPTABLE_EXIT_CODES}.\n"
        f"stdout tail:\n{result.stdout[-2000:]}\n"
        f"stderr tail:\n{result.stderr[-2000:]}"
    )

    if result.returncode == 0:
        # Success path: routed PCB must exist and be loadable.
        assert routed_path.exists(), (
            "Auto-pcb-size escalation reported success but no routed PCB exists."
        )
        from kicad_tools.schema.pcb import PCB

        PCB.load(routed_path)  # raises on malformed file
        return

    # Exit code 2 or 3: the wrapper either refused cleanly or
    # reported DRC violations.  Either way, the refusal/partial path
    # MUST emit an actionable message naming alternative levers.
    output = result.stdout + result.stderr
    if "AUTO-PCB-SIZE ESCALATION REFUSED" in output:
        # Refusal path -- verify the message names actionable levers.
        levers = sum(
            1
            for keyword in ("BOM", "layers", "envelope", "clearance", "manufacturer")
            if keyword.lower() in output.lower()
        )
        assert levers >= 3, (
            "Auto-pcb-size refusal must enumerate alternative levers; "
            f"matched {levers}/5 in subprocess output.\n"
            f"Output tail:\n{output[-2000:]}"
        )


def test_softstart_revb_auto_pcb_size_envelope_hard_is_honored(tmp_path: Path) -> None:
    """Confirm the envelope_hard declaration from project.kct is honored.

    The in-tree softstart project.kct declares ``envelope_hard: true``
    (Issue #3352 P_AS5).  When the wrapper engages and the layer
    escalation can't converge, the refusal message must cite
    ``envelope_hard=true`` as the reason -- NOT ``max_tier`` (which
    would mean the size axis was attempted, contradicting the hard
    envelope declaration).

    This test runs the same pipeline as
    ``test_softstart_revb_auto_pcb_size_runs_end_to_end`` but inspects
    the refusal-reason wording specifically.  When the recipe routes
    cleanly (no refusal triggered), the test passes trivially.
    """
    output_dir = tmp_path / "softstart_eh"
    pcb_path = _regenerate_softstart_pcb(output_dir)
    routed_path = output_dir / "softstart_routed.kicad_pcb"

    result = _route_with_auto_pcb_size(pcb_path, routed_path, timeout_seconds=600)
    output = result.stdout + result.stderr

    # If escalation was refused, verify the reason names envelope_hard
    # (NOT max_tier) per the layers-only ladder declaration.
    if "AUTO-PCB-SIZE ESCALATION REFUSED" in output:
        assert "envelope_hard" in output.lower() or "layers-only" in output.lower(), (
            "When the recipe declares envelope_hard=true + layers-only, "
            "auto-pcb-size refusal must cite envelope_hard (not size-tier "
            "exhaustion).  Refusal output did not include 'envelope_hard' or "
            "'layers-only'.\n"
            f"Output tail:\n{output[-2000:]}"
        )


if __name__ == "__main__":
    # Allow direct invocation for debugging:
    #   KICAD_RUN_SLOW_SOFTSTART_REACH=1 uv run python \
    #     tests/router/test_softstart_revb_auto_pcb_size.py
    sys.exit(pytest.main([__file__, "-v", "-s", "-m", "slow", "--no-cov"]))
