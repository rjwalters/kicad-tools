"""Copper-LVS and escape safety for the reviewed board04 construction.

The paid mechanical-drilling process replaces historical microvia-in-pad
stitches with tented through vias outside SMT lands. Copper-LVS must still
prove every power pad connected, while the process validator checks exact
physical identity, selected options, drill/land/ring floors and pad clearance.
The OSC escape mutation tests retain the original short-prevention coverage.
"""

from __future__ import annotations

import importlib.util
import runpy
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


def _fmt(v: float) -> str:
    """KiCad's minimal decimal formatting (mirrors fix_osc_escape's _fmt)."""
    return f"{v:.6f}".rstrip("0").rstrip(".")


def _osc_hops(recipe) -> tuple[str, str, str]:
    """(offending_hop, reaimed_hop, follow_on_hop) needles derived from the
    recipe's _OSC_* constants.

    Derived (not hardcoded) so the test tracks the recipe's
    ``centered_origin``-based coordinates and cannot go stale against a
    sheet-position change (PR #4015 judge feedback: the old hardcoded
    (100, 100)-origin absolutes went stale when the boards were
    sheet-centered).
    """
    via = f"(start {_fmt(recipe._OSC_VIA[0])} {_fmt(recipe._OSC_VIA[1])})"
    pad = f"{_fmt(recipe._OSC_IN_PAD[0])} {_fmt(recipe._OSC_IN_PAD[1])}"
    reaim = f"{_fmt(recipe._OSC_REAIM[0])} {_fmt(recipe._OSC_REAIM[1])}"
    offending = f"{via}\n\t\t(end {pad})"
    reaimed = f"{via}\n\t\t(end {reaim})"
    follow_on = f"(start {pad})\n\t\t(end {reaim})"
    return offending, reaimed, follow_on


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
            "The reviewed through-via power ties and +3.3V/GND zone clearances "
            "or the via-into-pour "
            "extractor bond may be missing / regressed — regenerate via "
            "generate_design.py."
        )
        assert result.shorts == ()
        assert result.opens == ()

    def test_reviewed_through_vias_keep_gnd_connected(
        self, board04_artifacts: tuple[Path, Path]
    ) -> None:
        """Paid mechanical drills replace the historical GND microvias."""
        _, pcb_path = board04_artifacts
        process = runpy.run_path(str(BOARD_DIR / "manufacturing_process.py"))
        pcb = process["validate_process"](pcb_path)
        assert pcb.vias, "The reviewed ground-plane stitches must remain present"
        assert all(v.via_type in (None, "through") for v in pcb.vias)
        assert all(v.drill >= 0.15 - 1e-6 for v in pcb.vias)
        assert all(v.size >= 0.30 - 1e-6 for v in pcb.vias)
        assert all((v.size - v.drill) / 2 >= 0.075 - 1e-6 for v in pcb.vias)
        # validate_process checks the physical fingerprint, paid option,
        # front/back tenting, native floors and clearance from every SMT land.
        # The adjacent copper-LVS test proves these through vias still bond
        # every power pad, without pinning an obsolete microvia count.

    def test_osc_escape_does_not_cross_osc_in_pad(
        self, board04_artifacts: tuple[Path, Path]
    ) -> None:
        """The OSC_OUT escape must not run into the U2.5 OSC_IN pad (#3797).

        The deterministic fresh route drops the OSC_OUT (net 5) B.Cu escape
        straight north into the U2.5 OSC_IN pad centre (board origin +
        (26.8375, 21.25)), shorting the two crystal pins.  ``fix_osc_escape``
        re-aims that hop, so the committed (fresh) artifact must NOT contain
        the offending hop.
        """
        _, pcb = board04_artifacts
        text = pcb.read_text()
        offending, _reaimed, _follow_on = _osc_hops(_load_recipe())
        assert offending not in text, (
            "board 04 routed PCB still contains the OSC_OUT escape hop running "
            "into the U2.5 OSC_IN pad centre (board origin + (26.8375, 21.25)) "
            "— the #3797 fix_osc_escape re-aim is missing or regressed; "
            "copper-LVS will report an OSC_IN<->OSC_OUT short."
        )


class TestFixOscEscapeStep:
    """Unit coverage for the deterministic ``fix_osc_escape`` recipe step (#3797)."""

    def _routed_with_offending_hop(self, tmp_path: Path, recipe) -> Path:
        """A synthetic routed-PCB fixture carrying the pre-fix OSC_OUT escape.

        ``fix_osc_escape`` runs before the recipe's 45-quantize step (#3797),
        so the committed artifact no longer carries the literal re-aimed hop
        (quantize doglegs it).  To keep this unit test hermetic and independent
        of the post-route quantization, build a minimal PCB containing exactly
        the geometry ``fix_osc_escape`` operates on as a fresh route emits it:
        the straight-north first hop into the U2.5 OSC_IN pad centre plus the
        degenerate follow-on segment (pad centre -> re-aim point).  Coordinates
        derive from the recipe's ``_OSC_*`` constants via :func:`_osc_hops`.
        """
        offending, _reaimed, follow_on_hop = _osc_hops(recipe)
        original = (
            f"\t(segment\n\t\t{offending}\n"
            '\t\t(width 0.2)\n\t\t(layer "B.Cu")\n'
            '\t\t(uuid "11111111-1111-1111-1111-111111111111")\n\t\t(net 5)\n\t)\n'
        )
        follow_on = (
            f"\t(segment\n\t\t{follow_on_hop}\n"
            '\t\t(width 0.2)\n\t\t(layer "B.Cu")\n'
            '\t\t(uuid "00000000-0000-0000-0000-000000000000")\n\t\t(net 5)\n\t)\n'
        )
        text = (
            '(kicad_pcb\n\t(version 20240108)\n\t(generator "test")\n'
            '\t(net 0 "")\n\t(net 5 "OSC_OUT")\n' + original + follow_on + ")\n"
        )
        out = tmp_path / "pre_fix_routed.kicad_pcb"
        out.write_text(text)
        return out

    def test_fix_clears_offending_hop_and_is_idempotent(self, tmp_path: Path) -> None:
        recipe = _load_recipe()
        offending, reaimed, follow_on = _osc_hops(recipe)
        pcb = self._routed_with_offending_hop(tmp_path, recipe)
        text = pcb.read_text()
        assert offending in text

        assert recipe.fix_osc_escape(pcb) is True
        fixed = pcb.read_text()
        # Offending hop gone, re-aimed hop present, follow-on dropped.
        assert offending not in fixed
        assert reaimed in fixed
        assert follow_on not in fixed

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
