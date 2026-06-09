"""Softstart rev B fine-pitch escape end-to-end consumer test (Issue #3371 P_FP4/P_FP5).

This is the heavyweight consumer test that closes Phase 4 of the
fine-pitch escape ladder.  It:

  1. Regenerates the softstart rev B schematic + PCB on demand (via
     the in-tree recipe ``boards/external/softstart/generate_design.py``).
  2. Invokes ``kct route`` with the manufacturing recipe
     (``jlcpcb-tier1``, 0.20mm clearance, 0.30mm trace).
  3. Asserts the pipeline produces a structured outcome -- the
     fine-pitch escape regions are detected and either (a) the routing
     converges with reach >= some baseline OR (b) the partial result
     is recorded for diagnostic purposes.

This is a slow test gated on ``KICAD_RUN_SLOW_SOFTSTART_REACH=1`` to
match the existing softstart slow-path conventions.  The headline
target from Issue #3371 AC #4 is ``>= 28/30 reach`` at the
jlcpcb-tier1 recipe; P_FP4 lands the infrastructure (adaptive radius,
in-region clearance threading, escape helper) that should bring the
routing reach up.  This test pins the floor at the pre-P_FP4 baseline
(20/30) so a future regression of the infrastructure surfaces.

To run locally::

    KICAD_RUN_SLOW_SOFTSTART_REACH=1 uv run pytest \\
      tests/router/test_softstart_revb_fine_pitch_escape.py -v --no-cov -s

Issue: https://github.com/rjwalters/kicad-tools/issues/3371
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOARD_DIR = REPO_ROOT / "boards" / "external" / "softstart"


pytestmark = pytest.mark.slow


def _slow_tests_enabled() -> bool:
    """Whether the slow softstart routing tests are enabled."""
    return os.environ.get("KICAD_RUN_SLOW_SOFTSTART_REACH") == "1"


if not _slow_tests_enabled():  # pragma: no cover - env gate
    pytestmark = [
        pytest.mark.slow,
        pytest.mark.skipif(
            True,
            reason=(
                "Slow softstart fine-pitch escape test (~10-15min).  Set "
                "KICAD_RUN_SLOW_SOFTSTART_REACH=1 to enable."
            ),
        ),
    ]


def _regenerate_softstart_pcb(output_dir: Path) -> Path:
    """Regenerate softstart rev B PCB on demand."""
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


def _route_softstart(pcb_path: Path, output_path: Path, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    """Drive ``kct route`` against softstart rev B with the manufacturing recipe."""
    skip_nets = ",".join([
        "AC_LINE", "AC_NEUTRAL", "FUSED_LINE", "GND",
        "+3.3V", "VRECT",
        "SCAP_POS+", "SCAP_POS_GND", "SCAP_NEG+", "SCAP_NEG_GND",
        "ISENSE_POS",
    ])
    cmd = [
        sys.executable, "-m", "kicad_tools.cli", "route",
        str(pcb_path),
        "--output", str(output_path),
        "--backend", "cpp",
        "--manufacturer", "jlcpcb-tier1",
        "--skip-nets", skip_nets,
        "--seed", "42",
        "--timeout", str(timeout_seconds),
        "--per-net-timeout", "45",
        "--clearance", "0.20",
        "--trace-width", "0.30",
    ]
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout_seconds + 60,
    )


def _extract_reach(output: str) -> tuple[int, int] | None:
    """Parse the ``Nets routed: N/M`` line from the routing output."""
    m = re.search(r"Nets routed:\s*(\d+)\s*/\s*(\d+)", output)
    if m:
        return int(m.group(1)), int(m.group(2))
    # Match "RECOMMENDATION: N/M nets" or similar.
    m = re.search(r"(\d+)\s*/\s*(\d+)\s*nets", output)
    if m:
        # When this pattern matches "failed nets" rather than routed, invert.
        total = int(m.group(2))
        # We can't tell direction without context; defer to first pattern.
    return None


def test_softstart_revb_fine_pitch_regions_install(tmp_path: Path) -> None:
    """Fine-pitch escape regions are installed during softstart rev B routing.

    Verifies the P_FP3 pipeline integration: ``load_pcb_for_routing``
    detects the UCC27211 SOIC-8, MCP6001, STM32 LQFP-32, etc. as
    fine-pitch escape regions and installs them on the grid.  The
    routing run surfaces the region count + escape clearance on the
    console as a one-line summary (per P_FP3 deliverable).
    """
    output_dir = tmp_path / "softstart_fp"
    pcb_path = _regenerate_softstart_pcb(output_dir)
    routed_path = output_dir / "softstart_routed.kicad_pcb"
    result = _route_softstart(pcb_path, routed_path, timeout_seconds=600)

    output = result.stdout + result.stderr
    # The detector must surface its one-line summary.
    assert "Fine-pitch escape regions detected" in output, (
        "Expected fine-pitch escape regions summary in routing output.\n"
        f"Output tail:\n{output[-2000:]}"
    )


def test_softstart_revb_reach_floor(tmp_path: Path) -> None:
    """Softstart rev B routing reach holds at the pre-P_FP4 baseline floor.

    The Issue #3371 AC #4 target is >= 28/30 reach.  P_FP4 lands the
    infrastructure (adaptive radius, in-region clearance threading,
    escape helper, dense-package union) that should bring routing
    reach up but does not aggressively change SOP escape dispatch
    semantics.  Pre-P_FP4 baseline on this fixture is ~22/30; this
    test pins a conservative floor of 20/30 to surface any
    infrastructure regression while leaving headroom for future
    optimisation.

    When the headline AC #4 is met (28/30+) a follow-up should tighten
    this floor.
    """
    output_dir = tmp_path / "softstart_reach"
    pcb_path = _regenerate_softstart_pcb(output_dir)
    routed_path = output_dir / "softstart_routed.kicad_pcb"
    result = _route_softstart(pcb_path, routed_path, timeout_seconds=600)

    output = result.stdout + result.stderr
    reach = _extract_reach(output)
    if reach is None:
        # Print the output for debugging when we can't parse it.
        print("\n--- routing output tail ---")
        print(output[-3000:])
        pytest.fail(
            "Could not parse 'Nets routed: N/M' from routing output."
        )
    routed_count, total = reach
    print(f"\nSoftstart rev B reach: {routed_count}/{total}")

    floor = 20  # Conservative pre-P_FP4 floor
    assert routed_count >= floor, (
        f"Softstart rev B reach {routed_count}/{total} below floor {floor}/{total}.\n"
        f"Output tail:\n{output[-2000:]}"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s", "-m", "slow", "--no-cov"]))
