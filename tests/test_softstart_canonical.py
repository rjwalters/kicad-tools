"""Copper-LVS coverage for the canonical (external) softstart board.

``boards/external/softstart`` is a symlink into the canonical product repo
(``rjwalters/softstart`` at ``hardware/kicad``), mirroring the
``chorus-test-revA`` local-only pattern.  Unlike a native fleet board the
artifacts live directly in the symlink root
(``softstart.kicad_sch`` / ``softstart.kicad_pcb``), not under ``output/``
with a ``_routed`` suffix.

The board is local-only: the symlink dangles on CI runners and in fresh
worktrees where the external repo is not checked out, so this test SKIPS
cleanly when the artifacts are absent (the chorus idiom -- see
``tests/test_chorus_reach_floor_3237.py::_chorus_fixture_present``).

The canonical design is mid-flight (the HLK-PM12 supply redesign; routing
is incomplete), so copper-LVS is currently dirty.  The clean check is
marked ``xfail(strict=True)`` so that once the upstream design is
completed (full route + LVS-clean) the XPASS trips CI and prompts removal
of the marker.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.lvs import compare_copper_netlist

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "external" / "softstart"
BOARD_SCH = BOARD_DIR / "softstart.kicad_sch"
BOARD_PCB = BOARD_DIR / "softstart.kicad_pcb"


def _artifacts_present() -> bool:
    """The canonical softstart artifacts live in the external (symlinked) repo."""
    return BOARD_SCH.exists() and BOARD_PCB.exists()


@pytest.fixture(scope="module")
def softstart_artifacts() -> tuple[Path, Path]:
    if not _artifacts_present():
        pytest.skip(
            f"canonical softstart artifacts not present "
            f"(sch={BOARD_SCH.exists()}, pcb={BOARD_PCB.exists()}); the "
            "external rjwalters/softstart repo is not checked out at the "
            "symlink target.  This test is intentionally a no-op without it."
        )
    return BOARD_SCH, BOARD_PCB


@pytest.mark.xfail(
    reason=(
        "the canonical softstart design is mid-flight (HLK-PM12 supply "
        "redesign; routing incomplete) so copper-LVS is dirty.  Remove this "
        "marker once the upstream design is completed and routes clean."
    ),
    strict=True,
)
def test_canonical_softstart_copper_lvs_clean(
    softstart_artifacts: tuple[Path, Path],
) -> None:
    sch, pcb = softstart_artifacts
    result = compare_copper_netlist(sch, pcb)
    assert result.clean is True, (
        "canonical softstart copper-LVS is dirty: "
        f"{[(m.kind, m.net_a, m.net_b) for m in result.mismatches]}"
    )
