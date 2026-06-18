"""Copper-LVS coverage for board 04 (stm32-devboard) — issue #3794.

Board 04 carries a GND plane on ``B.Cu`` only (the ``+3.3V`` / ``+5V`` rails
pour on ``F.Cu``).  Every GND SMD pad therefore reaches the plane through a
``pad -> F.Cu trace -> stitch via -> B.Cu pour`` path, and the four LQFP-48
VSS / VDD power pads that the dense 0.5 mm-pitch escape leaves un-stitched are
tied in by the #3794 Leg B recipe step (``tie_power_pads``):

  * three GND micro-via-in-pads (U2.8 / U2.23 / U2.35), and
  * a +3.3V / GND zone connect-pad clearance tighten so the re-pour bonds the
    moated-out +3.3V VDD pads (U2.9 / U2.24 / U2.36 / U2.48).

Before #3794 the committed routed PCB read ``0 shorts / 20 opens`` under
``compare_copper_netlist`` — all *same-net* power-pad opens.  The 16 GND opens
were an extractor gap (the label-free partition never bonded a via / trace
endpoint landing in a pour, only pad boxes); the 4 +3.3V opens were a genuine
board defect (pads moated out of their own F.Cu pour).  This test pins the
post-fix outcome on the committed artifact: copper-LVS must be clean and the
three GND tie vias must be present.

It is fast (sub-second) and hermetic — it reads the committed files only,
never spawns ``kicad-cli`` and never invokes the router.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kicad_tools.lvs import compare_copper_netlist

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_OUTPUT = REPO_ROOT / "boards" / "04-stm32-devboard" / "output"
BOARD_SCH = BOARD_OUTPUT / "stm32_devboard.kicad_sch"
BOARD_PCB = BOARD_OUTPUT / "stm32_devboard_routed.kicad_pcb"


@pytest.fixture(scope="module")
def board04_artifacts() -> tuple[Path, Path]:
    """The committed board 04 schematic + routed PCB.

    Skips if either artifact is missing (e.g. a contributor wiped the
    output directory) — run ``boards/04-stm32-devboard/generate_design.py``
    to regenerate.
    """
    if not BOARD_SCH.exists() or not BOARD_PCB.exists():
        pytest.skip(
            f"board 04 artifacts not present "
            f"(sch={BOARD_SCH.exists()}, pcb={BOARD_PCB.exists()}); "
            "run boards/04-stm32-devboard/generate_design.py to regenerate."
        )
    return BOARD_SCH, BOARD_PCB


class TestBoard04CopperLVSClean:
    """The committed board 04 routed PCB must be copper-LVS clean (#3794)."""

    def test_no_copper_opens_or_shorts(self, board04_artifacts: tuple[Path, Path]) -> None:
        sch, pcb = board04_artifacts
        result = compare_copper_netlist(sch, pcb)
        assert result.clean is True, (
            "copper-LVS unexpectedly dirty on the committed board 04 routed "
            f"PCB: shorts={list(result.shorts)} opens={list(result.opens)}.  "
            "The #3794 power-pad ties (tie_power_pads: 3 GND micro-via-in-pads "
            "+ tightened +3.3V/GND zone clearance) or the via-into-pour "
            "extractor bond may be missing / regressed — regenerate via "
            "generate_design.py."
        )
        assert result.shorts == ()
        assert result.opens == ()

    def test_gnd_tie_micro_vias_present(self, board04_artifacts: tuple[Path, Path]) -> None:
        """The three GND micro-via-in-pads (#3794 Leg B) must be present."""
        _, pcb = board04_artifacts
        text = pcb.read_text()
        net_table = dict(re.findall(r'\(net (\d+) "([^"]*)"\)', text))
        gnd_ids = {nid for nid, name in net_table.items() if name == "GND"}
        assert gnd_ids, 'board 04 PCB has no (net N "GND") entry'

        # The serializer may emit either the compact single-line via form or
        # kicad-cli's pretty-printed multi-line form (after a zone re-fill), so
        # match a ``(via micro ...)`` block non-greedily up to its net atom and
        # tolerate newlines/indentation between children.
        gnd_micro_vias = 0
        for via in re.finditer(
            r"\(via micro\b.*?\(layers\s+([^\)]*)\).*?\(net\s+(\d+)\)",
            text,
            re.S,
        ):
            layers, net_id = via.group(1), via.group(2)
            if net_id in gnd_ids and '"F.Cu"' in layers and '"B.Cu"' in layers:
                gnd_micro_vias += 1

        assert gnd_micro_vias >= 3, (
            "board 04 routed PCB is missing the 3 GND micro-via-in-pads added "
            "by tie_power_pads (#3794); the LQFP-48 VSS pads U2.8/U2.23/U2.35 "
            f"are unbonded and copper-LVS will report opens (found "
            f"{gnd_micro_vias})."
        )
