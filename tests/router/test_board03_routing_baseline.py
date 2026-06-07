"""Board 03 (usb-joystick) routing baseline regression guard.

This test pins the **measured routing reach** of
``boards/03-usb-joystick/`` against the ``kct route`` CLI as of
June 2026.

Baseline measurement at HEAD (with ``kct route --backend cpp --seed 42
--auto-fix --auto-fix-passes 2 --manufacturer jlcpcb-tier1``):

- **Routed: 11/13 nets (85%)** — all signal nets except USB_D+ / USB_D-
- **Routes created: 26**, ~1046 segments, 19 vias
- **Layer count: 2** (the 2-layer attempt produces the best result)
- USB_D+ and USB_D- are deterministically partial (2/3 pads stranded
  each) due to escape-geometry interactions with J1's USB-C connector
  pad layout — tracked in #3278.
- The committed unrouted PCB has 16 nets total: 13 are routed, 3 are
  power/pour nets (VCC, VBUS, GND) that are skipped by the router and
  served by auto-pour zones instead.

Context (the "1/16" myth):
    The pre-test ``kct fleet status`` output for board 03 reads
    ``incomplete routing (1/16 nets)`` because it queries the stale
    committed ``output/usb_joystick_routed.kicad_pcb`` snapshot.  Live
    routing produces 11/13.  The stale-fleet-status reporting gap is
    tracked separately in #3280.

Known follow-on issues that prevent a higher baseline:
    - **#3278**: Escape generator uses ``pads[0].net_name``'s
      net-class trace width for the whole row, pulling Power-class
      0.5mm width into the USB_D+/USB_D- HighSpeed escapes.  The
      resulting fat-segment B.Cu escape clearance violation defers
      both diff-pair pads to the main router, which can't path-find
      the remaining geometry — hence 2/3 pads stranded.
    - **#3279**: 2-layer boards with GND pour on B.Cu have no
      pipeline step to stitch F.Cu SMD GND pads to the pour, so
      ``kct check`` reports ``Net 'GND' is partially routed: 26 of 29
      pads stranded``.  Routing itself is fine; it's a pipeline gap.

Acceptance criteria pinned by this test:

1. **Reach floor**: ``kct route`` produces >= 11 routed signal nets
   (out of 13).  Drops to 10 or fewer indicate a routing-quality
   regression on USB-C-class pad-density boards.
2. **Deterministic across seeds**: seeds 1 / 42 / 99 all produce the
   same routed-net count, so a single-seed run is a reliable
   indicator of overall quality.
3. **The 1/16 myth stays dead**: if the test ever measures <= 1 net
   routed, somebody re-broke board 03 in a non-trivial way.

References:
    - Parent tracking issue: #3259
    - Stale fleet-status reporting: #3280
    - Escape clearance bug: #3278
    - 2-layer pour stitching gap: #3279
    - Existing board-03 demo-path test: tests/test_board_03_regression.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOARD_DIR = REPO_ROOT / "boards" / "03-usb-joystick"
UNROUTED_PCB = BOARD_DIR / "output" / "usb_joystick.kicad_pcb"

# Acceptance criteria for the June 2026 baseline.
#
# 13 signal nets total (USB_CC1, USB_CC2, USB_D+, USB_D-, XTAL1, XTAL2,
# JOY_X, JOY_Y, JOY_BTN, BTN1, BTN2, BTN3, BTN4).  The 3 pour/power
# nets (VCC, VBUS, GND) are auto-skipped and served by zones; they do
# NOT appear in the ``Nets routed: N/M`` line.
#
# USB_D+ and USB_D- are partial in the current baseline (see #3278), so
# the typical run lands at 11/13.  We pin the floor at 11 to catch a
# routing-quality regression; ratchet up to 12 or 13 when #3278 lands.
REQUIRED_NETS_ROUTED = 11
EXPECTED_TOTAL_NETS = 13


def _parse_routed_net_count(stdout: str) -> tuple[int, int] | None:
    """Extract the final ``Nets routed: N/M`` count from kct route output.

    The expected summary block contains a line of the form::

        Nets routed:     11/13

    (Multiple matches may exist in escalation mode; we return the LAST
    one since that reflects the final state the router landed on.)

    Returns ``(routed, total)`` or ``None`` if no such line was found.
    """
    pattern = re.compile(r"Nets routed:\s+(\d+)/(\d+)")
    matches = pattern.findall(stdout)
    if not matches:
        return None
    routed, total = matches[-1]
    return int(routed), int(total)


@pytest.fixture(scope="module")
def unrouted_pcb_path() -> Path:
    """Verify the committed unrouted board 03 PCB exists.

    The PCB is committed under ``boards/03-usb-joystick/output/``.  If
    the file is missing the test cannot run -- skip with a clear message.
    """
    if not UNROUTED_PCB.exists():
        pytest.skip(
            f"Board 03 unrouted PCB not found at {UNROUTED_PCB!s}; "
            "regenerate via `uv run python boards/03-usb-joystick/generate_pcb.py`."
        )
    return UNROUTED_PCB


def _run_kct_route(unrouted: Path, seed: int) -> str:
    """Run ``kct route --backend cpp --seed N --auto-fix`` and capture stdout.

    Mirrors the recipe in the parent issue (#3259) and the standard
    fleet/build invocation.  Routes to a tmpdir so it never overwrites
    the committed artifact.
    """
    with tempfile.TemporaryDirectory() as td:
        pcb_copy = Path(td) / "usb_joystick.kicad_pcb"
        shutil.copy2(unrouted, pcb_copy)
        output_path = Path(td) / "usb_joystick_routed.kicad_pcb"
        cmd = [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "route",
            str(pcb_copy),
            "--output",
            str(output_path),
            "--seed",
            str(seed),
            "--manufacturer",
            "jlcpcb-tier1",
            "--backend",
            "cpp",
            "--timeout",
            "600",
            "--auto-fix",
            "--auto-fix-passes",
            "2",
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        # Exit codes from cli/route_cmd.py:
        #   0 = full route + DRC clean
        #   2 = partial routing below --min-completion
        #   3 = >= min-completion but DRC violations remain
        # Board 03 lands at 2 or 3 (partial + DRC).  Codes 1 and 5 are
        # fatal (crash / SIGINT).
        if proc.returncode in (1, 5):
            pytest.fail(
                f"kct route returned fatal exit code {proc.returncode}\n"
                f"stderr (last 2000 chars):\n{proc.stderr[-2000:]}\n"
                f"stdout (last 2000 chars):\n{proc.stdout[-2000:]}"
            )
        return proc.stdout


@pytest.fixture(scope="module")
def route_stdout(unrouted_pcb_path: Path) -> str:
    """Run the canonical ``kct route`` invocation once per module."""
    return _run_kct_route(unrouted_pcb_path, seed=42)


@pytest.mark.slow
class TestBoard03RoutingBaseline:
    """Pin the June 2026 routing reach baseline for board 03.

    The full CLI subprocess takes ~2.5 minutes wall-clock (timeout 600 s
    on the route step + setup/teardown), so the class is marked
    ``@pytest.mark.slow``.  The nightly slow-tests workflow picks it up;
    PR-time CI skips it by default.
    """

    def test_reach_meets_floor(self, route_stdout: str) -> None:
        """``kct route --backend cpp`` produces at least 11/13 nets routed.

        This is the June 2026 baseline: USB_D+ and USB_D- defer to the
        main router due to escape geometry interactions on the USB-C
        connector J1 (tracked in #3278), but the other 11 signal nets
        connect successfully.  A regression below 11 means the router
        lost ground on a USB-C-class board — bisect against escape /
        diff-pair / negotiated-loop changes.
        """
        parsed = _parse_routed_net_count(route_stdout)
        assert parsed is not None, (
            "Could not find 'Nets routed: N/M' line in kct route stdout.  "
            "This typically means the router crashed before producing a "
            "summary.\n"
            f"stdout (last 4000 chars):\n{route_stdout[-4000:]}"
        )
        routed, total = parsed
        assert total == EXPECTED_TOTAL_NETS, (
            f"Board 03 routable-net count changed from {EXPECTED_TOTAL_NETS} "
            f"to {total}.  The schematic/PCB generator may have added or "
            "removed nets; update EXPECTED_TOTAL_NETS and REQUIRED_NETS_"
            "ROUTED to match the new topology after re-baselining."
        )
        assert routed >= REQUIRED_NETS_ROUTED, (
            f"Board 03 routing reach regressed: routed {routed}/{total}, "
            f"expected >= {REQUIRED_NETS_ROUTED}/{EXPECTED_TOTAL_NETS} "
            "(June 2026 baseline).  Common regression sources to bisect:\n"
            "  - escape clearance / lateral_offset changes for USB-C "
            "(see #3278)\n"
            "  - negotiated-loop rip-up policy on BLOCKED_BY_COMPONENT\n"
            "  - per-pad channel budget for J1's 12 SMT signal pads\n"
            "  - any change to ``_create_intra_ic_routes`` that affects "
            "diff-pair partner consolidation on the same package."
        )

    def test_the_1_of_16_myth_stays_dead(self, route_stdout: str) -> None:
        """If routing reach ever drops to 1 or fewer, somebody broke it badly.

        The pre-test ``kct fleet status`` reported board 03 as
        ``1/16 nets routed`` based on a stale committed routed PCB
        snapshot.  Live routing produces 11/13.  This test guards against
        accidentally re-introducing a catastrophic regression: anything
        that drops the live count to 1 or below is a hard fail.

        See #3280 for the fleet-status staleness gap that gave rise to
        the original "1/16" report.
        """
        parsed = _parse_routed_net_count(route_stdout)
        assert parsed is not None, "Could not parse routed net count; see test_reach_meets_floor"
        routed, _total = parsed
        assert routed > 1, (
            f"Board 03 live routing produced {routed} routed nets — "
            "the catastrophic baseline that #3259 was opened to prevent.  "
            "Either a router fix landed that regressed J1's escape "
            "geometry catastrophically, or the unrouted PCB is missing "
            "the destination MCU U1 (in which case generate_pcb.py "
            "needs to be regenerated)."
        )


@pytest.mark.slow
def test_routing_reach_deterministic_across_seeds(unrouted_pcb_path: Path) -> None:
    """The same reach (11/13) is produced for seeds 1, 42, and 99.

    The negotiated A* router uses the global seed for tie-breaks during
    A* and for the rip-up selection in BLOCKED_BY_COMPONENT.  For a
    USB-C-class board where the bottleneck is escape geometry (not A*
    tie-breaks), the reach should be stable across seeds.  If a future
    change introduces seed sensitivity, that's a determinism regression
    worth investigating.

    Marked slow because it runs ``kct route`` THREE times (~7.5 minutes
    wall-clock).
    """
    counts = {}
    for seed in (1, 42, 99):
        stdout = _run_kct_route(unrouted_pcb_path, seed=seed)
        parsed = _parse_routed_net_count(stdout)
        assert parsed is not None, (
            f"Could not parse routed net count for seed {seed}; "
            f"stdout (last 4000 chars):\n{stdout[-4000:]}"
        )
        counts[seed] = parsed[0]
    unique_counts = set(counts.values())
    assert len(unique_counts) == 1, (
        f"Board 03 routing reach is NOT seed-stable: got {counts!r}.  "
        "Variance across seeds indicates the A* tie-break or the "
        "BLOCKED_BY_COMPONENT rip-up order is now load-bearing for the "
        "USB_D+/USB_D- failure mode.  Either the escape geometry is "
        "fragile in a new way, or the negotiated loop's stall detection "
        "introduced seed-dependence.  Investigate before relaxing this "
        "assertion."
    )
    # And the seed-stable count must be at the floor.
    routed = next(iter(unique_counts))
    assert routed >= REQUIRED_NETS_ROUTED, (
        f"Seed-stable count {routed} is below the floor of "
        f"{REQUIRED_NETS_ROUTED}; see test_reach_meets_floor for "
        "bisection guidance."
    )
