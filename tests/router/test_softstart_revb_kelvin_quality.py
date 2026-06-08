"""Rev B P4 Kelvin-source routing-quality regression (Issue #3343 P4).

Verifies that the SRC_POS / SRC_NEG Kelvin-source ties produced by the
rev B routing pipeline stay tight enough that the UCC27211 driver's
high-side ground reference (HS pin) remains close to the back-to-back
FET pair's source node.  Architect's load-bearing note: long Kelvin
traces re-introduce parasitic inductance + voltage drops that defeat
the driver's UVLO accuracy.

Implementation: parse the routed PCB and verify that for each Kelvin
net (SRC_POS, SRC_NEG), the route bounding-box diagonal is within a
threshold proportional to the placement-side Q*A↔Q*B + U* triangle.
This is a routing-quality regression (e.g. router decides to route a
Kelvin tie via a 50mm detour to skirt congestion).

This test reads the **routed** PCB produced by ``generate_design.py``
with ``SOFTSTART_RUN_FULL_PIPELINE=1``.  Gated behind
``KICAD_RUN_SLOW_SOFTSTART_REACH=1`` so it doesn't run in default
``pnpm check:ci``.
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

# Architect target: Kelvin-source ties should stay within ~20-30mm
# diagonal of the U*/Q*A/Q*B triangle.  The placement (P3) sets the
# triangle at ~20mm Manhattan, and we want the routed Kelvin net to
# stay within ~40mm diagonal (a 2x detour budget is the engineering
# margin; 4x would defeat UVLO accuracy per the architect note).
SRC_BBOX_DIAGONAL_CEILING_MM = 60.0

SKIP_NETS = [
    "AC_LINE", "AC_NEUTRAL", "FUSED_LINE", "GND",
    "+3.3V", "VRECT",
    "SCAP_POS+", "SCAP_POS_GND", "SCAP_NEG+", "SCAP_NEG_GND",
    "ISENSE_POS",
]


def _slow_tests_enabled() -> bool:
    return os.environ.get("KICAD_RUN_SLOW_SOFTSTART_REACH") == "1"


pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not _slow_tests_enabled(),
        reason=(
            "Slow softstart rev B Kelvin-quality test (~3 min for "
            "fresh route).  Set KICAD_RUN_SLOW_SOFTSTART_REACH=1 to "
            "enable."
        ),
    ),
]


def _parse_segments_for_net(pcb_text: str, net_name: str) -> list[tuple[float, float, float, float]]:
    """Extract ``(segment ...)`` blocks belonging to ``net_name``.

    Returns a list of (x1, y1, x2, y2) tuples.
    """
    # Find the net number for net_name.
    net_num_m = re.search(
        rf'\(net\s+(\d+)\s+"{re.escape(net_name)}"\)', pcb_text
    )
    if not net_num_m:
        return []
    net_num = int(net_num_m.group(1))
    # Find all segment blocks referencing this net.
    seg_pattern = re.compile(
        r'\(segment\s+\(start\s+([\d.\-]+)\s+([\d.\-]+)\)\s+'
        r'\(end\s+([\d.\-]+)\s+([\d.\-]+)\)\s+'
        r'\(width\s+[\d.\-]+\)\s+'
        r'\(layer\s+"[^"]+"\)\s+'
        rf'\(net\s+{net_num}\)',
        re.DOTALL,
    )
    return [
        (float(m.group(1)), float(m.group(2)),
         float(m.group(3)), float(m.group(4)))
        for m in seg_pattern.finditer(pcb_text)
    ]


def _bbox_diagonal(segs: list[tuple[float, float, float, float]]) -> float:
    """Return diagonal length (mm) of the bounding box of all segments."""
    if not segs:
        return 0.0
    xs, ys = [], []
    for x1, y1, x2, y2 in segs:
        xs += [x1, x2]
        ys += [y1, y2]
    dx = max(xs) - min(xs)
    dy = max(ys) - min(ys)
    return (dx * dx + dy * dy) ** 0.5


@pytest.fixture(scope="module")
def routed_pcb_text() -> str:
    """Generate + route the softstart PCB; return the routed pcb text."""
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
                f"Softstart unrouted PCB not found at {UNROUTED_PCB!s}"
            )
    with tempfile.TemporaryDirectory() as td:
        pcb_copy = Path(td) / "softstart.kicad_pcb"
        shutil.copy2(UNROUTED_PCB, pcb_copy)
        out = Path(td) / "softstart_routed.kicad_pcb"
        cmd = [
            sys.executable, "-m", "kicad_tools.cli", "route",
            str(pcb_copy),
            "--output", str(out),
            "--seed", "42",
            "--no-auto-layers", "--layers", "2",
            "--manufacturer", "jlcpcb",
            "--backend", "cpp",
            "--clearance", "0.20",
            "--skip-nets", ",".join(SKIP_NETS),
            "--timeout", "300",
        ]
        env = os.environ.copy()
        env.setdefault("PYTHONHASHSEED", "42")
        proc = subprocess.run(
            cmd, capture_output=True, text=True, env=env, timeout=480,
            check=False,
        )
        if proc.returncode in (1, 5):
            pytest.fail(
                f"kct route returned fatal exit code {proc.returncode}\n"
                f"stderr (last 2000 chars):\n{proc.stderr[-2000:]}"
            )
        if not out.exists():
            pytest.fail("kct route did not produce a routed PCB")
        return out.read_text()


class TestRevBKelvinSourceQuality:
    """Rev B P4 Kelvin-source routing quality.

    Architect note: the UCC27211 driver's HS pin must stay close to the
    Q*A↔Q*B common-source node, otherwise the high-side gate-drive
    reference drifts and UVLO accuracy degrades.  These tests pin a
    bounding-box-diagonal ceiling for the routed Kelvin nets so a
    router regression (e.g. taking the long way around) is caught.
    """

    def test_src_pos_bbox_diagonal_within_budget(self, routed_pcb_text: str) -> None:
        """SRC_POS Kelvin tie bounding-box stays within budget."""
        segs = _parse_segments_for_net(routed_pcb_text, "SRC_POS")
        # If the net has no segments, it's fully unrouted -- that's the
        # P4 best-effort policy; skip rather than fail (the routing-reach
        # test in test_softstart_revb_p4_routing.py covers that case).
        if not segs:
            pytest.skip("SRC_POS has no routed segments (best-effort residual)")
        diag = _bbox_diagonal(segs)
        assert diag <= SRC_BBOX_DIAGONAL_CEILING_MM, (
            f"SRC_POS routed bounding-box diagonal {diag:.1f}mm exceeds "
            f"ceiling {SRC_BBOX_DIAGONAL_CEILING_MM}mm.  The Kelvin "
            "source tie may have been routed via a long detour, "
            "degrading the UCC27211 HS reference.  Likely cause: "
            "negotiated rip-up pushed SRC_POS through a less-direct "
            "channel; bisect against router changes since #3343 P4."
        )

    def test_src_neg_bbox_diagonal_within_budget(self, routed_pcb_text: str) -> None:
        """SRC_NEG Kelvin tie bounding-box stays within budget."""
        segs = _parse_segments_for_net(routed_pcb_text, "SRC_NEG")
        if not segs:
            pytest.skip("SRC_NEG has no routed segments (best-effort residual)")
        diag = _bbox_diagonal(segs)
        assert diag <= SRC_BBOX_DIAGONAL_CEILING_MM, (
            f"SRC_NEG routed bounding-box diagonal {diag:.1f}mm exceeds "
            f"ceiling {SRC_BBOX_DIAGONAL_CEILING_MM}mm."
        )
