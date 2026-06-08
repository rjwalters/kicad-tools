"""Softstart manufacturability baseline regression guard (Issue #3235 follow-up).

Updated for **rev B P4** (Issue #3343 P4) — the softstart board has
been migrated from the rev A recipe (10 multi-pad signal nets,
``jlcpcb-tier1`` profile, 0.15mm clearance) to the rev B recipe
(30 multi-pad nets including 11 power-skipped, ``jlcpcb`` profile,
0.20mm clearance).  The architect predicted reach regression at the
tighter 0.2mm rule and #3343 P4 accepts best-effort residuals; this
test pins the **measured rev B P4 ship-state** so future regressions
below the new floor are caught.

Rev B P4 baseline measurement (seeds 42/43/44 produce identical
headlines; routing topology is deterministic across them):

- ``kct route --backend cpp --layers 2 --manufacturer jlcpcb
    --clearance 0.20 --skip-nets <power>``
  * Multi-pad nets: 30 (incl. 11 power-skipped at 1 pad each)
  * **Nets routed: 24/30** -- 5 partials + 1 unrouted (SWDIO)
  * ``connectivity`` errors: ~16 = 8 expected power-net partials
    (filled by copper pours, not the router) + ~8 signal-net partials
    (best-effort, architect-predicted at 0.2mm)
  * Clearance violations: ~140 (acceptable per rev B P4 best-effort
    policy; the rev A SWDIO/STATUS_LED 0-clearance baseline does NOT
    apply to rev B because the U1 LQFP-32 footprint is structurally
    different from rev A's TSSOP-20)

The acceptance criteria pinned by this test:

1. CPP routing reach >= 22/30 nets routed (best-effort floor with
   ±2 noise allowance below the measured 24/30).
2. The rev A 0-clearance baseline is intentionally NOT enforced for
   rev B — the architect plan accepted regression risk at 0.20mm and
   the residual clearance violations are tracked but not gated.  See
   Issue #3343 P5 for the manufacturing-export gate that re-introduces
   a stricter clearance ceiling once placement is iterated.

History:

- **PR #3198 (Issue #3143)**: per-pad channel budget infrastructure;
  established the original 5/10 baseline.
- **PR #3203 (Issue #3201)**: per-pad budget edge-classification fix;
  lifted baseline to 6/10.
- **PR #3223 (Issue #3201)**: endpoint-aware lateral-channel strip;
  documented the 9-parameter geometry-tuning floor.
- **PR #3227 / PR #3232**: pad-exit clearance + Euclidean trace-
  clearance kernel; lifted unaided reach to 8/10 by accident of
  upstream work.
- **PR #3248**: Euclidean via-clearance kernel (sibling to #3232).
- **PR #3249 (Issue #3235)**: ``find_nets_in_foreign_budgets`` helper
  + documented negative results for direction-1 (cohort augmentation)
  and direction-2 (escape-route layer diversification).
- **PR #3250 (Issue #3233)**: ``_add_pad_unsafe`` sub-cell pad-metal
  margin closure.

The 10/10 reach is achieved by the **adaptive-grid auto-resolution**
path that ``kct route`` invokes when auto-grid selects 0.127mm (memory-
budget-capped above the 0.075mm DRC-safe target).  The 4 pre-#3287
SWDIO/STATUS_LED clearance violations were drained to **0** by the
D2/R12 placement nudge in PR #3287 (Issue #3257): pushing the LED
column 7 mm east (130 -> 137 mm) takes STATUS_LED's R12->D2 vertical
leg out of the SWDIO B.Cu corridor at y~173.3 mm.  Issue #3297
verified the post-#3287 state is fully JLCPCB ship-ready.

This test is gated behind ``KICAD_RUN_SLOW_SOFTSTART_REACH=1`` and
slated for the slow-tests CI workflow.

To run locally::

    KICAD_RUN_SLOW_SOFTSTART_REACH=1 uv run pytest \\
      tests/router/test_softstart_manufacturable_baseline.py -v --no-cov
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOARD_DIR = REPO_ROOT / "boards" / "external" / "softstart"
UNROUTED_PCB = BOARD_DIR / "output" / "softstart.kicad_pcb"

# Acceptance criteria — UPDATED FOR REV B P4 (Issue #3343 P4).
#
# Rev A used 10 multi-pad signal nets at jlcpcb-tier1 with 0.15mm
# clearance.  Rev B (#3343) is structurally different:
#   - 30 multi-pad nets (incl. 11 power skipped)
#   - jlcpcb profile (not tier1)
#   - 0.20mm clearance (rev B project.kct min_space)
#
# Architect-predicted reach regression at 0.20mm is accepted (best-
# effort residuals per #3343 P4 plan).  The post-Wave-3 0-clearance
# baseline (rev A) does NOT carry forward; rev B has its own residuals
# tracked by ``tests/router/test_softstart_revb_p4_routing.py``.
REQUIRED_NETS_ROUTED = 22  # of 30 -- best-effort floor; measured 24/30 baseline
REQUIRED_NETS_TOTAL = 30

# Power nets that are intentionally skipped from the autorouter and
# filled by copper pours / hand-routed by the user (see
# ``generate_design.py:1639``).
SKIP_NETS = [
    "AC_LINE",
    "AC_NEUTRAL",
    "FUSED_LINE",
    "GND",
    "+3.3V",
    "VRECT",
    "SCAP_POS+",
    "SCAP_POS_GND",
    "SCAP_NEG+",
    "SCAP_NEG_GND",
    "ISENSE_POS",
]


def _slow_tests_enabled() -> bool:
    return os.environ.get("KICAD_RUN_SLOW_SOFTSTART_REACH") == "1"


pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not _slow_tests_enabled(),
        reason=(
            "Slow softstart routing-reach test (~60s).  Set "
            "KICAD_RUN_SLOW_SOFTSTART_REACH=1 to enable."
        ),
    ),
]


def _parse_nets_connected(stdout: str) -> int | None:
    """Extract the ``Nets connected: N (topologically complete)`` count.

    The CPP backend's negotiated routing summary uses this line as the
    canonical reach metric: it counts nets where every pad has a graph
    path to every other pad on the same net, ignoring DRC clearance.
    ``Nets routed: N/M`` is also produced but counts attempts, not
    completed connectivity.
    """
    pattern = re.compile(r"Nets connected:\s+(\d+)\s+\(topologically complete\)")
    m = pattern.search(stdout)
    return int(m.group(1)) if m else None


def _parse_violations(stdout: str) -> int | None:
    """Extract the final ``Violations: N`` count from the kct route summary."""
    pattern = re.compile(r"Violations:\s+(\d+)")
    matches = pattern.findall(stdout)
    return int(matches[-1]) if matches else None


@pytest.fixture(scope="module")
def unrouted_pcb_path() -> Path:
    """Verify the committed unrouted softstart PCB exists; regenerate if not."""
    if not UNROUTED_PCB.exists():
        # Try to regenerate from the design recipe
        regen_cmd = [
            sys.executable,
            str(BOARD_DIR / "generate_design.py"),
        ]
        env = os.environ.copy()
        env.setdefault("PYTHONHASHSEED", "42")
        try:
            subprocess.run(
                regen_cmd,
                cwd=str(REPO_ROOT),
                env=env,
                check=False,
                timeout=600,
                capture_output=True,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass
        if not UNROUTED_PCB.exists():
            pytest.skip(
                f"Softstart unrouted PCB not found at {UNROUTED_PCB!s}; "
                "regenerate via `uv run python boards/external/softstart/generate_design.py`"
            )
    return UNROUTED_PCB


def _parse_nets_routed(stdout: str) -> tuple[int | None, int | None]:
    """Extract the ``Nets routed: N/M`` count (rev B parser)."""
    pattern = re.compile(r"Nets routed:\s+(\d+)/(\d+)")
    matches = pattern.findall(stdout)
    if matches:
        n, m = matches[-1]
        return int(n), int(m)
    return None, None


class TestSoftstartManufacturableBaseline:
    """Pin the rev B P4 manufacturable baseline for the softstart board.

    Runs ``kct route --backend cpp --manufacturer jlcpcb
    --clearance 0.20`` (the rev B P4 production invocation per
    ``generate_design.py:route_pcb``) and asserts the measured reach
    profile matches the documented rev B P4 baseline.
    """

    @pytest.fixture(scope="class")
    def route_stdout(self, unrouted_pcb_path: Path) -> str:
        """Run ``kct route --backend cpp`` with rev B P4 parameters."""
        with tempfile.TemporaryDirectory() as td:
            pcb_copy = Path(td) / "softstart.kicad_pcb"
            shutil.copy2(unrouted_pcb_path, pcb_copy)
            output_path = Path(td) / "softstart_routed.kicad_pcb"
            cmd = [
                sys.executable,
                "-m",
                "kicad_tools.cli",
                "route",
                str(pcb_copy),
                "--output",
                str(output_path),
                "--seed",
                "42",
                "--no-auto-layers",
                "--layers",
                "2",
                "--manufacturer",
                "jlcpcb",       # rev B target (was jlcpcb-tier1 for rev A)
                "--backend",
                "cpp",
                "--clearance",
                "0.20",         # rev B project.kct min_space (was 0.15 for rev A)
                "--skip-nets",
                ",".join(SKIP_NETS),
                "--timeout",
                "300",
            ]
            env = os.environ.copy()
            env.setdefault("PYTHONHASHSEED", "42")
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=env,
                timeout=480,
                check=False,
            )
            # Exit codes from cli/route_cmd.py:
            #   0 = full route + DRC clean
            #   2 = partial routing below --min-completion
            #   3 = >= min-completion but DRC violations remain
            #   4 = partial routing AND clearance violations
            # Rev B P4: expect 4 (partial + DRC), accept 2/3/4 as best-effort.
            # Codes 1 and 5 are fatal (crash / unhandled exception).
            if proc.returncode in (1, 5):
                pytest.fail(
                    f"kct route returned fatal exit code {proc.returncode}\n"
                    f"stderr (last 2000 chars):\n{proc.stderr[-2000:]}\n"
                    f"stdout (last 2000 chars):\n{proc.stdout[-2000:]}"
                )
            return proc.stdout

    def test_rev_b_nets_routed_meets_floor(
        self, route_stdout: str
    ) -> None:
        """Rev B P4: at least 22/30 nets must report 'routed'.

        Rev B has 30 multi-pad nets (incl. 11 power-skipped) per the
        upgraded BOM (back-to-back FETs + UCC27211 drivers + precharge
        + bus envelope + bank dividers + OC comparator).  The
        architect predicted reach regression at the tighter 0.20mm
        clearance (vs rev A's 0.15mm) and #3343 P4 accepts best-effort
        residuals.  The measured baseline is 24/30 routed at seeds
        42/43/44; floor at 22/30 allows ±2 noise.

        A regression below 22/30 indicates either:
        - A regression in the negotiated rip-up loop in
          ``router/core.py:7074``
        - A regression in the adaptive-grid Phase 1 escape on the
          LQFP-32 cluster (8 pads per side)
        - A placement regression in the U1 east-side cluster or the
          U5/U6 + FET Kelvin-source rows
        """
        nets_routed, nets_total = _parse_nets_routed(route_stdout)
        assert nets_routed is not None, (
            "Could not find 'Nets routed: N/M' line in kct route "
            "output.  Last 2000 chars:\n"
            f"{route_stdout[-2000:]}"
        )
        assert nets_routed >= REQUIRED_NETS_ROUTED, (
            f"Softstart rev B P4 routing regressed to {nets_routed}/"
            f"{nets_total} (floor is "
            f"{REQUIRED_NETS_ROUTED}/{REQUIRED_NETS_TOTAL}).  "
            "See Issue #3343 P4 for the best-effort policy."
        )
