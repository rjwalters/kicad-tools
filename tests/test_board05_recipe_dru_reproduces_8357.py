"""Board 05's committed ``.kicad_dru`` is what its recipe emits (Issue #8357).

``design.py`` writes the routed ``.kicad_dru`` in three steps:
``redesign/build.py:declare_router_netclasses`` (the sidecar writer, which
appends the #8048 attach-zone block), ``kct check --emit-dru`` (which refreshes
the fab-floor block from the checker's rules), and finally
``redesign/package.py``'s ``kct export``, whose ``_write_drc_constraints``
re-runs the sidecar writer from the bare manufacturer profile.  The last write
is what the recipe leaves on disk.  Before #8357 the committed file lacked the
attach-zone block the recipe emits, so every honest regeneration had to
hand-restore it and refill the zones against the hand-restored file.  The
operator ruled (2026-11-04) to commit what the recipe emits.

This test replays exactly those three steps on the committed routed board in a
scratch directory, starting -- as the recipe does -- from no ``.kicad_dru``,
and asserts the result is byte-identical to the committed file.  It needs no
kicad-cli: board 05 has no rule areas, so the keepout step writes nothing.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

from kicad_tools.cli import main as kct_main
from kicad_tools.export.manufacturing import ManufacturingPackage, ManufacturingResult
from kicad_tools.manufacturers.attach_zone_dru import has_attach_zone_block

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD = REPO_ROOT / "boards" / "05-bldc-motor-controller"
OUTPUT = BOARD / "output"
REDESIGN = BOARD / "redesign"
STEM = "bldc_controller_routed"


def _load_build():
    sys.path.insert(0, str(REDESIGN))
    try:
        spec = importlib.util.spec_from_file_location("board05_build_8357", REDESIGN / "build.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(REDESIGN))


def _recipe_dru(tmp_path: Path) -> str:
    for suffix in (".kicad_pcb", ".kicad_pro"):
        shutil.copy2(OUTPUT / f"{STEM}{suffix}", tmp_path / f"{STEM}{suffix}")
    shutil.copy2(OUTPUT / "net_class_map.json", tmp_path / "net_class_map.json")
    routed = tmp_path / f"{STEM}.kicad_pcb"
    assert not routed.with_suffix(".kicad_dru").exists()

    _load_build().declare_router_netclasses(routed)
    kct_main(
        [
            "check",
            str(routed),
            "--mfr",
            "jlcpcb",
            "--emit-dru",
            "--format",
            "json",
            "--output",
            str(tmp_path / "before-refill.json"),
        ]
    )
    # ``kct export`` (redesign/package.py) writes the sidecars once more,
    # next to the input board, before anything else it does.
    result = ManufacturingResult(output_dir=tmp_path / "manufacturing")
    ManufacturingPackage(routed, manufacturer="jlcpcb")._write_drc_constraints(result)
    assert routed.with_suffix(".kicad_dru") in result.drc_constraint_paths
    return routed.with_suffix(".kicad_dru").read_text(encoding="utf-8")


def test_committed_dru_carries_the_attach_zone_block():
    assert has_attach_zone_block((OUTPUT / f"{STEM}.kicad_dru").read_text(encoding="utf-8"))


# Default-on fab view (#8843) roughly doubles `kct check --mfr jlcpcb` on board 07
# (~10 s -> ~21 s locally, measured); each subprocess run is deliberately given
# headroom above the global 60 s cap on the 2-vCPU CI runner.
@pytest.mark.timeout(180)
def test_recipe_emits_the_committed_dru_byte_for_byte(tmp_path):
    committed = (OUTPUT / f"{STEM}.kicad_dru").read_text(encoding="utf-8")
    assert _recipe_dru(tmp_path) == committed, (
        "board 05's recipe no longer emits the committed .kicad_dru; regenerate "
        "the board (design.py) on the pinned toolchain and re-pin its evidence"
    )
