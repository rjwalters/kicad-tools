"""Regression guard: board-04 fresh regen is jlcpcb-tier1 hole-to-hole clean.

Issue #4408.  Board-04's LQFP-48 west escape routes three in-pad micro-vias
(OSC_OUT / NRST / GND) stacked at the 0.5 mm pin pitch, leaving two 0.350 mm
drill pairs below the jlcpcb-tier1 0.500 mm ``min_hole_to_hole_mm`` floor.  #4017
fixed this ARTIFACT-ONLY (a hand nudge that was deliberately not back-ported),
so every fresh regen reintroduced 2 ``hole_to_hole_clearance`` errors and the
committed board diverged from the recipe.

#4408 back-ports the fix as a generic, ``--mfr``-driven post-route pass
(``relocate_drill_clearance_step`` in the recipe, wired between
``tie_power_pads`` and ``quantize_escapes``).  Two guards:

1. A fast source-pin (no routing) that the recipe still wires the step -- so a
   future recipe cleanup that drops it fails at PR time rather than silently
   regressing to artifact-only.
2. A ``@slow`` end-to-end regen that runs ``generate_design.py`` and asserts the
   FRESH routed board is tier1-clean (0 blocking DRC errors), which is the real
   acceptance criterion the CI fresh-regen gate now enforces at strict-0.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_04_RECIPE = REPO_ROOT / "boards" / "04-stm32-devboard" / "generate_design.py"


def test_recipe_wires_hole_to_hole_relocation_step() -> None:
    """Fast source-pin: the recipe must invoke the hole-to-hole relocation pass.

    Pins both the step function and its call site so a future edit that removes
    either (silently reverting to the artifact-only #4017 state) trips here.
    """
    source = BOARD_04_RECIPE.read_text()

    assert "def relocate_drill_clearance_step(" in source, (
        "board-04 recipe no longer defines relocate_drill_clearance_step -- the "
        "generic #4408 hole-to-hole relocation pass.  Without it the fresh regen "
        "reintroduces the 2 grandfathered 0.350mm drill pairs and diverges from "
        "the committed artifact (the #4017 artifact-only state)."
    )
    assert "relocate_drill_clearance_step(routed_path)" in source, (
        "board-04 recipe defines relocate_drill_clearance_step but no longer "
        "CALLS it in the post-route pipeline.  Re-wire it between tie_power_pads "
        "and quantize_escapes (issue #4408)."
    )
    # It must import the library engine (the shared clearance-safe pass), not a
    # board-specific hack.
    assert (
        "from kicad_tools.drc.relocate_drill_clearance import relocate_drill_clearance" in source
    ), (
        "board-04 recipe no longer imports the library relocate_drill_clearance "
        "engine -- the fix must live in the library, not a board-local hack "
        "(issue #4408)."
    )


@pytest.mark.slow
def test_fresh_regen_is_tier1_hole_to_hole_clean(tmp_path: Path) -> None:
    """End-to-end: a fresh ``generate_design.py`` run is jlcpcb-tier1-clean.

    This is the #4408 acceptance criterion: the GENERATOR (not a committed
    artifact) produces a tier1-clean board unattended.  Mirrors the CI
    fresh-regen strict-0 gate.
    """
    out_dir = tmp_path / "board04"
    out_dir.mkdir()

    # Regen may exit non-zero on a partial route under load (documented
    # host-vs-CI router divergence, #3822); the DRC assertion below is the real
    # gate, so tolerate a non-zero recipe exit as long as the routed PCB exists.
    proc = subprocess.run(
        [sys.executable, str(BOARD_04_RECIPE), str(out_dir)],
        capture_output=True,
        text=True,
        timeout=1200,
        check=False,
    )

    routed = out_dir / "stm32_devboard_routed.kicad_pcb"
    if not routed.exists():
        pytest.fail(
            "board-04 regen did not produce a routed PCB.\n"
            f"stdout tail:\n{proc.stdout[-2000:]}\n"
            f"stderr tail:\n{proc.stderr[-2000:]}"
        )

    check = subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "check",
            str(routed),
            "--mfr",
            "jlcpcb-tier1",
            "--errors-only",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    brace = check.stdout.find("{")
    assert brace >= 0, f"kct check produced no JSON payload:\n{check.stdout}"
    payload = json.loads(check.stdout[brace:])

    violations = payload.get("violations", [])
    # Blocking (non-advisory) errors, counted the way the CI gate counts them.
    blocking = [
        v for v in violations if v.get("severity") == "error" and v.get("rule_id") != "connectivity"
    ]
    hole_to_hole = [v for v in blocking if v.get("rule_id") == "hole_to_hole_clearance"]

    assert not hole_to_hole, (
        "Fresh board-04 regen still has hole_to_hole_clearance errors -- the "
        "relocation pass did not relieve the LQFP-48 west escape stack:\n"
        + "\n".join(f"  - {v.get('message')} at {v.get('location')}" for v in hole_to_hole)
    )
    assert not blocking, "Fresh board-04 regen has blocking jlcpcb-tier1 DRC errors:\n" + "\n".join(
        f"  - {v.get('rule_id')}: {v.get('message')}" for v in blocking
    )
