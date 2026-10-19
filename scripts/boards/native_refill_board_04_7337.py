"""Re-save board 04's zone fills as kicad-cli's own refill computes them (Issue #7337).

#7317 re-filled board 04 with ``runner.run_fill_zones`` (via
``refill_thermal_ladder_7295.py``).  That production fill finishes with the
foreign-pad clearance carve (#3711), a pure-Python edit of the saved
``filled_polygon`` s that a native refill does not reproduce, so the saved
copper ended 9.37 mm^2 (B.Cu) / 18.71 mm^2 (F.Cu) below an independent
``kicad-cli pcb drc --refill-zones`` refill and ``kct readiness --verify``
failed its ``zone_fill`` gate.  The thermal-spoke ladder's decisions are
persisted on the pads themselves, so a native refill keeps them
(``thermal_asymmetry`` stays 0) and the refilled board is DRC clean.

This script refills only the routed PCB's ``(zone ...)`` blocks natively
(``declare_router_netclasses_6390.refill_zones_in_place``: footprints, pads,
tracks and vias are untouched), refuses unless a second native refill
reproduces the saved copper exactly, then regenerates the hash-bound bundle and
re-pins ``readiness.json`` with ``refresh_bundles_6404.py`` (Issue #7357)::

    KCT_KICAD_DOCKER=1 uv run --with shapely python \\
        scripts/boards/native_refill_board_04_7337.py --checked-at <ISO>
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
BOARD = REPO_ROOT / "boards" / "04-stm32-devboard"
ROUTED = BOARD / "output" / "stm32_devboard_routed.kicad_pcb"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--checked-at", required=True, help="ISO timestamp for readiness.json")
    args = parser.parse_args(argv)
    netclasses = _load("declare_router_netclasses_6390", HERE / "declare_router_netclasses_6390.py")
    netclasses.refill_zones_in_place(ROUTED)
    regen = _load("regenerate_boards_03_04_6076", HERE / "regenerate_boards_03_04_6076.py")
    try:
        delta = regen.fill_consistency(ROUTED)
    except RuntimeError as exc:
        print(f"REFUSED: the native refill does not reproduce itself ({exc})", file=sys.stderr)
        return 1
    print(f"  saved vs independent refill: {delta}")
    bundles = _load("refresh_bundles_6404", HERE / "refresh_bundles_6404.py")
    return bundles.main([str(BOARD), "--checked-at", args.checked_at])


if __name__ == "__main__":
    from kicad_tools.cli.kicad_toolchain import run_on_evidence_toolchain

    sys.exit(run_on_evidence_toolchain(main, [REPO_ROOT]))
