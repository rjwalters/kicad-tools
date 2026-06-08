"""Rev B P4 routing baseline regression guard (Issue #3343 P4).

This test re-routes the rev B softstart PCB with the post-#3343 P4
recipe (``kct route --backend cpp`` + ``--clearance 0.20`` +
``--manufacturer jlcpcb``) and asserts the documented best-effort
baseline.

Rev B introduces ~3x more signal nets than rev A (back-to-back FETs +
UCC27211 drivers + precharge subsystem + bus envelope + bank dividers +
OC comparator), so the architect-predicted 0.2mm clearance regression
is accepted as best-effort residual.  This test pins the measured
ship-state so a future regression below the floor is caught.

Baseline measurement at HEAD with PYTHONHASHSEED=42 (seeds 42/43/44
produce identical headlines, so reach is deterministic):

- ``kct route --backend cpp --layers 2 --manufacturer jlcpcb
    --clearance 0.20 --skip-nets <power>``
  * Multi-pad routing target: 30 nets (incl. 11 power skipped)
  * Signal nets fully routed (no partial pads): 11-15/19 typical
  * Total connectivity violations: ~8 (all power-net partials by design
    — filled by copper pours)

This test is gated behind ``KICAD_RUN_SLOW_SOFTSTART_REACH=1`` because
fresh routing takes ~3 minutes (uses the rip-up + reroute negotiated
loop with iter-2 typically triggering).

To run locally::

    KICAD_RUN_SLOW_SOFTSTART_REACH=1 uv run pytest \\
      tests/router/test_softstart_revb_p4_routing.py -v --no-cov
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

# Rev B P4 acceptance criteria (Issue #3343 P4).
# Rev B has ~19 signal nets (vs rev A's 10) because of:
#   - back-to-back FETs (Q1A/B, Q2A/B)
#   - UCC27211 drivers (U5, U6) with VBOOT_*, UCC_HO_*, UCC_LO_*
#   - precharge subsystem (Q5, Q6, R20, R21)
#   - bus envelope (U8 + R29 + C30/31)
#   - bank dividers (R25-R28)
#   - OC comparator (U7, V_OC_TH)
# At 0.2mm clearance (vs rev A's 0.15mm) the architect-predicted reach
# regression is accepted.  These floors codify the measured ship-state
# baseline so any future regression below them is caught.
#
# Architect proposal: 0.2mm is the load-bearing rev B JLCPCB spec, so
# we accept best-effort residuals here.  P5 (manufacturing export) may
# require manual touch-up routing in KiCad for the residual nets.
SOFTSTART_REVB_P4_NETS_ROUTED_FLOOR = 22  # of 30 (8 power-skipped are 1-pad)
SOFTSTART_REVB_P4_NETS_TOTAL = 30
SOFTSTART_REVB_P4_UNROUTED_CEILING = 2  # at most 2 nets totally unrouted

# Power nets that are intentionally skipped from the autorouter and
# filled by copper pours (see ``generate_design.py`` ``route_pcb``).
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


# Only the routing-reach class is gated as slow.  The design-rule and
# pin-map smoke tests run as fast string-checks against the recipe.
slow_routing_only = pytest.mark.skipif(
    not _slow_tests_enabled(),
    reason=(
        "Slow softstart rev B P4 routing test (~3 min).  Set "
        "KICAD_RUN_SLOW_SOFTSTART_REACH=1 to enable."
    ),
)


def _parse_nets_routed(stdout: str) -> tuple[int | None, int | None]:
    """Extract the ``Nets routed: N/M`` count from kct route output."""
    pattern = re.compile(r"Nets routed:\s+(\d+)/(\d+)")
    matches = pattern.findall(stdout)
    if matches:
        n, m = matches[-1]
        return int(n), int(m)
    return None, None


def _parse_unrouted(stdout: str) -> int | None:
    """Extract the ``Unrouted: N/M`` count from kct route output."""
    pattern = re.compile(r"Unrouted:\s+(\d+)/\d+")
    matches = pattern.findall(stdout)
    if matches:
        return int(matches[-1])
    return None


@pytest.fixture(scope="module")
def unrouted_pcb_path() -> Path:
    """Regenerate the unrouted softstart PCB from the recipe."""
    if not UNROUTED_PCB.exists():
        regen_cmd = [sys.executable, str(BOARD_DIR / "generate_design.py")]
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
                "regenerate via "
                "`uv run python boards/external/softstart/generate_design.py`"
            )
    return UNROUTED_PCB


@slow_routing_only
@pytest.mark.slow
class TestSoftstartRevBP4Routing:
    """Rev B P4 routing baseline regression guard (Issue #3343 P4).

    Pins the post-#3343 P4 routing ship-state so the rev B JLCPCB spec
    (``--clearance 0.20`` + ``--manufacturer jlcpcb``) is enforced and
    any future degradation below the documented best-effort baseline is
    surfaced.
    """

    @pytest.fixture(scope="class")
    def route_stdout(self, unrouted_pcb_path: Path) -> str:
        """Run ``kct route`` with rev B P4 parameters."""
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
                "jlcpcb",
                "--backend",
                "cpp",
                "--clearance",
                "0.20",
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
            # Rev B P4 accepts exit codes 2/3/4 (best-effort partials);
            # 1 and 5 are fatal.
            if proc.returncode in (1, 5):
                pytest.fail(
                    f"kct route returned fatal exit code {proc.returncode}\n"
                    f"stderr (last 2000 chars):\n{proc.stderr[-2000:]}\n"
                    f"stdout (last 2000 chars):\n{proc.stdout[-2000:]}"
                )
            return proc.stdout

    def test_nets_routed_meets_floor(self, route_stdout: str) -> None:
        """Rev B P4: at least 22/30 nets must report 'routed'.

        Rev B has more signal nets than rev A (~19 vs 10) due to the
        UCC27211 drivers, back-to-back FETs, precharge, OC trip, and
        bank dividers.  At 0.2mm clearance the architect predicted
        reach degradation; the measured baseline is 24/30 routed at
        seeds 42/43/44.  Floor pinned at 22/30 to allow ±2 noise.
        """
        nets_routed, nets_total = _parse_nets_routed(route_stdout)
        assert nets_routed is not None, (
            "Could not find 'Nets routed: N/M' in kct route output.  "
            f"Last 2000 chars:\n{route_stdout[-2000:]}"
        )
        print(
            f"  softstart rev B P4 nets routed: {nets_routed}/{nets_total}"
        )
        assert nets_routed >= SOFTSTART_REVB_P4_NETS_ROUTED_FLOOR, (
            f"softstart rev B P4 routing regressed to {nets_routed}/"
            f"{nets_total} (floor is "
            f"{SOFTSTART_REVB_P4_NETS_ROUTED_FLOOR}/"
            f"{SOFTSTART_REVB_P4_NETS_TOTAL}).  See Issue #3343 P4 "
            "for the best-effort policy."
        )

    def test_unrouted_within_ceiling(self, route_stdout: str) -> None:
        """Rev B P4: at most 2 nets may be totally unrouted.

        Baseline at seeds 42/43/44: SWDIO is the only fully-unrouted
        net (1/30).  Ceiling at 2/30 allows ±1 noise on a different
        seed.  If a third net goes fully unrouted, the placement may
        have regressed or the router lost a key escape — investigate
        via the ``Unrouted nets:`` and ``Partially connected nets:``
        sections of the kct route output.
        """
        unrouted = _parse_unrouted(route_stdout)
        if unrouted is None:
            # No "Unrouted: N/M" line means 0 unrouted, which is best case.
            unrouted = 0
        print(f"  softstart rev B P4 unrouted: {unrouted}")
        assert unrouted <= SOFTSTART_REVB_P4_UNROUTED_CEILING, (
            f"softstart rev B P4 unrouted count {unrouted} exceeds ceiling "
            f"{SOFTSTART_REVB_P4_UNROUTED_CEILING}.  Likely placement "
            "regression in the U1 east-side cluster or the U5/U6 + FET "
            "Kelvin-source region."
        )


class TestRevBP4DesignRules:
    """Rev B P4 design-rule wiring in the generate_design.py recipe.

    Smoke tests that read the recipe to verify the load-bearing rev B
    P4 parameters are wired into ``route_pcb`` and ``run_drc``.
    """

    @pytest.fixture(scope="class")
    def recipe_text(self) -> str:
        return (BOARD_DIR / "generate_design.py").read_text()

    def test_route_pcb_uses_0_20mm_clearance(self, recipe_text: str) -> None:
        """``route_pcb`` invokes ``kct route --clearance 0.20``."""
        # The invocation passes the clearance as two separate CLI args
        # (``--clearance``, ``"0.20"``).  Accept either form.
        assert (
            '"--clearance", "0.20"' in recipe_text
            or "--clearance 0.20" in recipe_text
        ), (
            "route_pcb does not pass --clearance 0.20 to kct route.  "
            "Rev B P4 requires 0.20mm trace clearance per the rev B "
            "project.kct min_space spec."
        )

    def test_route_pcb_uses_jlcpcb_manufacturer(self, recipe_text: str) -> None:
        """``route_pcb`` invokes ``--manufacturer jlcpcb`` (not tier1)."""
        assert '"--manufacturer", "jlcpcb"' in recipe_text or (
            "--manufacturer jlcpcb\n" in recipe_text
        ), (
            "route_pcb does not target the jlcpcb manufacturer profile.  "
            "Rev B target_fab is jlcpcb per the canonical project.kct."
        )

    def test_route_pcb_uses_cpp_backend(self, recipe_text: str) -> None:
        """``route_pcb`` invokes ``--backend cpp``."""
        assert '"--backend", "cpp"' in recipe_text or (
            "--backend cpp" in recipe_text
        ), (
            "route_pcb must use the C++ adaptive-grid backend for "
            "rev B (PRs #3256/#3287/#3306 baseline)."
        )

    def test_run_drc_uses_jlcpcb_not_tier1(self, recipe_text: str) -> None:
        """``run_drc`` invokes ``kct check --mfr jlcpcb`` (rev B target).

        Rev A used jlcpcb-tier1 to enable via-in-pad; rev B doesn't
        need that (the canonical ``project.kct`` ``target_fab: jlcpcb``)
        and the stricter rev B clearance is the load-bearing change.
        """
        # Find the body of run_drc(...) up to the next top-level def.
        m = re.search(r'def run_drc\(.*?\n(?=def |\nclass )',
                       recipe_text, re.DOTALL)
        assert m, "run_drc(...) function body not found in recipe"
        body = m.group(0)
        # Within run_drc, the kct check invocation must use "jlcpcb"
        # (not "jlcpcb-tier1") as the --mfr argument.
        assert '"--mfr", "jlcpcb"' in body, (
            "run_drc should pass --mfr jlcpcb (rev B target).  "
            "Rev A used jlcpcb-tier1 to enable via-in-pad; rev B "
            "doesn't need that. run_drc body (last 600 chars):\n"
            + body[-600:]
        )
        # And must NOT reference the rev A tier1 profile.
        assert '"jlcpcb-tier1"' not in body, (
            "run_drc still references jlcpcb-tier1 (rev A choice).  "
            "Rev B should drop tier1 in favour of standard jlcpcb."
        )


class TestRevBP4LQFP32PinMap:
    """Verify the LQFP-32 PCB footprint pin map matches the schematic
    symbol's pin numbering (Issue #3343 P4 forward-annotation fix).

    P3 originally used the architect's nominal pin map which was offset
    by ~2 positions from the canonical KiCad symbol
    ``MCU_ST_STM32G0:STM32G031K8Tx``.  This caused DRC connectivity
    errors on every MCU net.  P4 reconciles the PCB-side pin-to-net
    dict so the pad numbers match the schematic symbol exactly.
    """

    @pytest.fixture(scope="class")
    def recipe_text(self) -> str:
        return (BOARD_DIR / "generate_design.py").read_text()

    def test_pin_4_is_vdd(self, recipe_text: str) -> None:
        """Pin 4 of LQFP-32 is VDD (per KiCad symbol)."""
        # The pin_net dict literal in the LQFP-32 generator should have
        # 4: "+3.3V" (the VDD pin on the K8Tx symbol).
        assert '4: "+3.3V",            # VDD' in recipe_text, (
            "LQFP-32 pin 4 must be +3.3V (VDD per KiCad symbol).  "
            "P3 originally mapped pin 4 to NRST which is wrong."
        )

    def test_pin_5_is_gnd(self, recipe_text: str) -> None:
        """Pin 5 of LQFP-32 is VSS/GND (per KiCad symbol)."""
        assert '5: "GND",              # VSS' in recipe_text, (
            "LQFP-32 pin 5 must be GND (VSS per KiCad symbol)."
        )

    def test_pin_6_is_nrst(self, recipe_text: str) -> None:
        """Pin 6 of LQFP-32 is PF2 = NRST (per KiCad symbol)."""
        assert '6: "NRST",             # PF2' in recipe_text, (
            "LQFP-32 pin 6 must be NRST (PF2 per KiCad symbol)."
        )

    def test_pin_7_is_pa0_v_ac_sense(self, recipe_text: str) -> None:
        """Pin 7 of LQFP-32 is PA0 = V_AC_SENSE (per KiCad symbol)."""
        assert '7: "V_AC_SENSE",       # PA0' in recipe_text, (
            "LQFP-32 pin 7 must be V_AC_SENSE (PA0 per KiCad symbol). "
            "P3 originally mapped pin 7 to PA1=V_BUS_DVDT which is wrong."
        )

    def test_pin_20_is_precharge_neg(self, recipe_text: str) -> None:
        """Pin 20 of LQFP-32 is PC6 = PRECHARGE_NEG (per KiCad symbol).

        This is a key load-bearing pin: PC6 lands at pin 20 (not pin 18)
        because pins 19 + 21 are NC/PAx unbonded pins, and pin 20 between
        them is the only PC* on the right side of the symbol.
        """
        assert '20: "PRECHARGE_NEG",   # PC6' in recipe_text, (
            "LQFP-32 pin 20 must be PRECHARGE_NEG (PC6 per KiCad symbol)."
        )

    def test_pin_24_is_swdio(self, recipe_text: str) -> None:
        """Pin 24 of LQFP-32 is PA13 = SWDIO (per KiCad symbol)."""
        assert '24: "SWDIO",           # PA13' in recipe_text, (
            "LQFP-32 pin 24 must be SWDIO (PA13 per KiCad symbol)."
        )
