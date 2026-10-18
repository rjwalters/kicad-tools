"""Re-fill boards 00/01/04 under the per-pad thermal-spoke ladder (Issue #7295).

The committed routed boards were filled before the ladder (#7302) existed, so
their small passives carry the old selective pass's ``(zone_connect 2)`` solid
override.  The selective pass leaves any pad that already declares a
``zone_connect`` untouched, so a plain refill cannot narrow those pads.  This
script drops every pad-level ``(zone_connect 2)`` (no generator writes one; they
are all products of the selective pass) and re-runs the production fill,
``runner.run_fill_zones``, which re-applies the ladder, the foreign-pad carve
and the starved-thermal / isolated-copper remediation.  Copper other than the
zone fills and those pad tokens is untouched.

Usage (repository root; pinned toolchain via ``KCT_KICAD_DOCKER=1`` off CI)::

    KCT_KICAD_DOCKER=1 uv run python scripts/boards/refill_thermal_ladder_7295.py [BOARD ...]

Follow with each board's ``package_release.py`` / ``refresh_readiness.py`` to
re-pin the hash-bound evidence.

Issue #7337: ``run_fill_zones`` ends with the foreign-pad carve, which an
independent ``kicad-cli`` refill does not reproduce; on board 04 it left the
saved copper ~28 mm^2 short of the refill.  ``native_refill_board_04_7337.py``
re-saved board 04 with the native fill, and ``refresh_readiness.py`` now
refuses a board whose saved copper differs from a refill.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BOARDS = {
    "00-simple-led": "simple_led_routed.kicad_pcb",
    "01-voltage-divider": "voltage_divider_routed.kicad_pcb",
    "04-stm32-devboard": "stm32_devboard_routed.kicad_pcb",
}
_PAD_SOLID = re.compile(r"\n\t+\(zone_connect 2\)")


def refill(board: str) -> None:
    from kicad_tools.cli.runner import run_fill_zones

    pcb = REPO_ROOT / "boards" / board / "output" / BOARDS[board]
    text = pcb.read_text(encoding="utf-8")
    stripped = _PAD_SOLID.sub("", text)
    pcb.write_text(stripped, encoding="utf-8")
    result = run_fill_zones(pcb)
    if not result.success:
        raise SystemExit(f"{board}: zone fill failed: {result.stderr}")
    after = pcb.read_text(encoding="utf-8")
    print(
        f"{board}: dropped {len(_PAD_SOLID.findall(text))} solid override(s); "
        f"{len(_PAD_SOLID.findall(after))} pad(s) still solid after the ladder"
    )


def main() -> int:
    for name in sys.argv[1:] or list(BOARDS):
        refill(name)
    return 0


if __name__ == "__main__":
    from kicad_tools.cli.kicad_toolchain import run_on_evidence_toolchain

    sys.exit(run_on_evidence_toolchain(main, [REPO_ROOT]))
