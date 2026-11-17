"""``scripts/boards/regenerate_boards_03_04_6076.py`` remap is pure and idempotent (Issue #6076)."""

from __future__ import annotations

import importlib.util
import re
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "boards" / "regenerate_boards_03_04_6076.py"
BOARD04 = REPO / "boards" / "04-stm32-devboard" / "output"
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("regenerate_boards_03_04", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_committed_board04_remap_is_identity(mod, tmp_path):
    """Re-running on the regenerated board changes no byte (the map is the identity)."""
    unrouted = BOARD04 / "stm32_devboard.kicad_pcb"
    routed = tmp_path / "routed.kicad_pcb"
    shutil.copy2(BOARD04 / "stm32_devboard_routed.kicad_pcb", routed)
    stats = mod.remap_routed(unrouted, unrouted, routed)
    assert stats["changed"] == 0
    assert routed.read_bytes() == (BOARD04 / "stm32_devboard_routed.kicad_pcb").read_bytes()


def test_remap_changes_only_uuid_strings(mod, tmp_path):
    """Scrambling the generator UUIDs and remapping back touches nothing else."""
    unrouted = BOARD04 / "stm32_devboard.kicad_pcb"
    # A stand-in "old" generator build: same board, every UUID different.
    old = tmp_path / "old.kicad_pcb"
    old.write_text(
        UUID.sub(
            lambda m: ("e" if m.group(0)[0] != "e" else "f") + m.group(0)[1:], unrouted.read_text()
        )
    )
    routed = tmp_path / "routed.kicad_pcb"
    original = (BOARD04 / "stm32_devboard_routed.kicad_pcb").read_text()
    routed.write_text(original)
    mod.remap_routed(old, unrouted, routed)
    out = routed.read_text()
    assert UUID.sub("U", out) == UUID.sub("U", original)
