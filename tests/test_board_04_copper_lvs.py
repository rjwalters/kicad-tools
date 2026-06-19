"""Copper-LVS coverage for board 04 (stm32-devboard) — issues #3794 / #3797.

Board 04 carries a GND plane on ``B.Cu`` only (the ``+3.3V`` / ``+5V`` rails
pour on ``F.Cu``).  Every GND SMD pad therefore reaches the plane through a
``pad -> F.Cu trace -> stitch via -> B.Cu pour`` path, and the four LQFP-48
VSS / VDD power pads that the dense 0.5 mm-pitch escape leaves un-stitched are
tied in by the #3794 Leg B recipe step (``tie_power_pads``):

  * four GND micro-via-in-pads (U2.8 / U2.23 / U2.35 / U2.47 — the 4th VSS pad
    added in #3797 so the fresh deterministic regen bonds it too), and
  * a +3.3V / GND zone connect-pad clearance tighten so the re-pour bonds the
    moated-out +3.3V VDD pads (U2.9 / U2.24 / U2.36 / U2.48).

Before #3794 the committed routed PCB read ``0 shorts / 20 opens`` under
``compare_copper_netlist`` — all *same-net* power-pad opens.  The 16 GND opens
were an extractor gap (the label-free partition never bonded a via / trace
endpoint landing in a pour, only pad boxes); the 4 +3.3V opens were a genuine
board defect (pads moated out of their own F.Cu pour).  This test pins the
post-fix outcome on the committed artifact: copper-LVS must be clean and the
GND tie vias must be present.

Per #3797 the *committed* artifact is now the **fresh deterministic** route
(no longer hand-fixed): the recipe's deterministic ``fix_osc_escape`` step
re-aims the OSC_OUT B.Cu escape off the OSC_IN pad column (clearing the
``OSC_IN<->OSC_OUT`` escape-stub short) and the 4th ``_GND_TIE_VIAS`` entry
bonds U2.47, so a fresh regen reproduces a copper-LVS-clean board.  This module
also unit-tests ``fix_osc_escape`` directly (fast, hermetic) so a router drift
that moves the escape fails loudly.

The committed-artifact tests are fast (sub-second) and hermetic — they read the
committed files only, never spawn ``kicad-cli`` and never invoke the router.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from kicad_tools.lvs import compare_copper_netlist

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "04-stm32-devboard"
BOARD_OUTPUT = BOARD_DIR / "output"
BOARD_SCH = BOARD_OUTPUT / "stm32_devboard.kicad_sch"
BOARD_PCB = BOARD_OUTPUT / "stm32_devboard_routed.kicad_pcb"


def _load_recipe():
    """Import the board-04 ``generate_design`` recipe module by path."""
    path = BOARD_DIR / "generate_design.py"
    spec = importlib.util.spec_from_file_location("board04_generate_design", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        """The four GND micro-via-in-pads (#3794 Leg B + #3797) must be present."""
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

        assert gnd_micro_vias >= 4, (
            "board 04 routed PCB is missing the 4 GND micro-via-in-pads added "
            "by tie_power_pads (#3794 + #3797); the LQFP-48 VSS pads "
            "U2.8/U2.23/U2.35/U2.47 are unbonded and copper-LVS will report "
            f"opens (found {gnd_micro_vias})."
        )

    def test_osc_escape_does_not_cross_osc_in_pad(
        self, board04_artifacts: tuple[Path, Path]
    ) -> None:
        """The OSC_OUT escape must not run into the U2.5 OSC_IN pad (#3797).

        The deterministic fresh route drops the OSC_OUT (net 5) B.Cu escape
        straight north into the U2.5 OSC_IN pad centre at (126.8375, 121.25),
        shorting the two crystal pins.  ``fix_osc_escape`` re-aims that hop, so
        the committed (fresh) artifact must NOT contain the offending hop.
        """
        _, pcb = board04_artifacts
        text = pcb.read_text()
        offending = "(start 126.8375 121.75)\n\t\t(end 126.8375 121.25)"
        assert offending not in text, (
            "board 04 routed PCB still contains the OSC_OUT escape hop running "
            "into the U2.5 OSC_IN pad centre (126.8375, 121.25) — the #3797 "
            "fix_osc_escape re-aim is missing or regressed; copper-LVS will "
            "report an OSC_IN<->OSC_OUT short."
        )


class TestFixOscEscapeStep:
    """Unit coverage for the deterministic ``fix_osc_escape`` recipe step (#3797)."""

    def _routed_with_offending_hop(self, tmp_path: Path) -> Path:
        """A routed-PCB copy that re-inserts the pre-fix OSC_OUT escape stub.

        The committed artifact is already fixed, so reconstruct the pre-fix
        geometry (the straight-north hop into the U2.5 pad + the follow-on
        segment) to exercise the surgery the way a fresh route emits it.
        """
        if not BOARD_PCB.exists():
            pytest.skip("board 04 routed PCB not present; regenerate.")
        text = BOARD_PCB.read_text()
        # Replace the re-aimed first hop with the original straight-north hop,
        # and re-insert the degenerate follow-on segment the fix dropped.
        reaimed = "(start 126.8375 121.75)\n\t\t(end 126.6875 121.1)"
        assert reaimed in text, (
            "committed board 04 PCB does not contain the re-aimed OSC escape "
            "hop; the #3797 fix may have regressed."
        )
        original = "(start 126.8375 121.75)\n\t\t(end 126.8375 121.25)"
        text = text.replace(reaimed, original, 1)
        # Inject the follow-on (pad centre -> re-aim point) segment right after
        # the (now-original) first hop's block so fix_osc_escape can drop it.
        anchor = "(segment\n\t\t(start 126.8375 121.75)\n\t\t(end 126.8375 121.25)"
        idx = text.find(anchor)
        assert idx >= 0
        block_end = text.index("\n\t)\n", idx) + len("\n\t)\n")
        follow_on = (
            "\t(segment\n\t\t(start 126.8375 121.25)\n\t\t(end 126.6875 121.1)\n"
            '\t\t(width 0.2)\n\t\t(layer "B.Cu")\n'
            '\t\t(uuid "00000000-0000-0000-0000-000000000000")\n\t\t(net 5)\n\t)\n'
        )
        text = text[:block_end] + follow_on + text[block_end:]
        out = tmp_path / "pre_fix_routed.kicad_pcb"
        out.write_text(text)
        return out

    def test_fix_clears_offending_hop_and_is_idempotent(self, tmp_path: Path) -> None:
        recipe = _load_recipe()
        pcb = self._routed_with_offending_hop(tmp_path)
        text = pcb.read_text()
        assert "(start 126.8375 121.75)\n\t\t(end 126.8375 121.25)" in text

        assert recipe.fix_osc_escape(pcb) is True
        fixed = pcb.read_text()
        # Offending hop gone, re-aimed hop present, follow-on dropped.
        assert "(start 126.8375 121.75)\n\t\t(end 126.8375 121.25)" not in fixed
        assert "(start 126.8375 121.75)\n\t\t(end 126.6875 121.1)" in fixed
        assert "(start 126.8375 121.25)\n\t\t(end 126.6875 121.1)" not in fixed

        # Idempotent: a second pass is a no-op and leaves the file unchanged.
        assert recipe.fix_osc_escape(pcb) is True
        assert pcb.read_text() == fixed

    def test_fix_asserts_when_escape_geometry_missing(self, tmp_path: Path) -> None:
        """The router-drift guard must fire if the OSC escape hop is absent."""
        recipe = _load_recipe()
        # A PCB with neither the offending nor the re-aimed hop trips the guard.
        pcb = tmp_path / "no_escape.kicad_pcb"
        pcb.write_text("(kicad_pcb\n\t(version 20240108)\n)\n")
        with pytest.raises(AssertionError, match="OSC_OUT escape"):
            recipe.fix_osc_escape(pcb)
