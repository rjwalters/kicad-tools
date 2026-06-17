"""Copper-LVS coverage for board 03 (usb-joystick) — issue #3787.

Board 03 carries GND planes on *both* ``F.Cu`` and ``B.Cu`` but the
router emits zero GND vias (every via is a signal-net layer change).
Without a via tying the two GND planes together, the J1 USB-C connector's
F.Cu-only shield/return pads (``J1.A12`` / ``J1.B1``) strand in their own
copper islands and ``compare_copper_netlist`` correctly reports them as
GND *opens* — a physical bond the schematic-blind copper extractor
catches even though KiCad's DRC (which treats same-net zones as logically
connected) does not.

Issue #3787 adds an ``add_gnd_stitching_vias`` recipe step that drops a
GND ``F.Cu``<->``B.Cu`` stitching via at each fragmented GND island; the
subsequent zone re-fill bonds them into one net.  This test pins that
outcome on the committed routed artifact: copper-LVS must be clean and the
GND stitching vias must be present.

It is fast (sub-second) and hermetic — it reads the committed files only,
never spawns ``kicad-cli`` and never invokes the router.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kicad_tools.lvs import compare_copper_netlist

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_OUTPUT = REPO_ROOT / "boards" / "03-usb-joystick" / "output"
BOARD_SCH = BOARD_OUTPUT / "usb_joystick.kicad_sch"
BOARD_PCB = BOARD_OUTPUT / "usb_joystick_routed.kicad_pcb"


@pytest.fixture(scope="module")
def board03_artifacts() -> tuple[Path, Path]:
    """The committed board 03 schematic + routed PCB.

    Skips if either artifact is missing (e.g. a contributor wiped the
    output directory) — run ``boards/03-usb-joystick/generate_design.py``
    to regenerate.
    """
    if not BOARD_SCH.exists() or not BOARD_PCB.exists():
        pytest.skip(
            f"board 03 artifacts not present "
            f"(sch={BOARD_SCH.exists()}, pcb={BOARD_PCB.exists()}); "
            "run boards/03-usb-joystick/generate_design.py to regenerate."
        )
    return BOARD_SCH, BOARD_PCB


class TestBoard03CopperLVSClean:
    """The committed board 03 routed PCB must be copper-LVS clean (#3787)."""

    def test_no_copper_opens_or_shorts(self, board03_artifacts: tuple[Path, Path]) -> None:
        sch, pcb = board03_artifacts
        result = compare_copper_netlist(sch, pcb)
        assert result.clean is True, (
            "copper-LVS unexpectedly dirty on the committed board 03 routed "
            f"PCB: shorts={list(result.shorts)} opens={list(result.opens)}.  "
            "The GND F.Cu<->B.Cu stitching vias (add_gnd_stitching_vias, "
            "#3787) may be missing or the route/fill regressed — regenerate "
            "via generate_design.py."
        )
        assert result.shorts == ()
        assert result.opens == ()

    def test_gnd_stitching_vias_present(self, board03_artifacts: tuple[Path, Path]) -> None:
        """At least one GND-net via must bond the F.Cu and B.Cu GND planes."""
        _, pcb = board03_artifacts
        text = pcb.read_text()
        net_table = dict(re.findall(r'\(net (\d+) "([^"]*)"\)', text))
        gnd_ids = {nid for nid, name in net_table.items() if name == "GND"}
        assert gnd_ids, 'board 03 PCB has no (net N "GND") entry'

        gnd_fb_vias = 0
        for via in re.finditer(
            r"\(via\s*\(at [^\)]*\)\s*\(size [\d.]+\)\s*\(drill [\d.]+\)\s*"
            r'\(layers ([^\)]*)\)\s*\(uuid "[^"]*"\)\s*\(net (\d+)\)',
            text,
        ):
            layers, net_id = via.group(1), via.group(2)
            if net_id in gnd_ids and '"F.Cu"' in layers and '"B.Cu"' in layers:
                gnd_fb_vias += 1

        assert gnd_fb_vias >= 1, (
            "board 03 routed PCB has no GND F.Cu<->B.Cu stitching via; "
            "the two GND planes are unbonded and copper-LVS will report "
            "opens (#3787)."
        )
