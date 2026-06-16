"""LVS coverage for board 00 (issue #3748).

These tests exercise the new :mod:`kicad_tools.lvs` comparator against
the committed board 00 artifacts and a deliberately mutated copy.  They
are fast (sub-second) and hermetic -- they read files only, never spawn
``kicad-cli``, and never invoke the router.

Why three tests:

1. **Clean positive** -- guards against a regression that breaks LVS for
   the canonical hello-world board.
2. **Dirty negative** -- swaps two pad-to-net bindings on the PCB so the
   comparator's output is provably non-trivial; also exercises the
   :class:`BoardNetlistMismatch` exception path that the recipe relies
   on to exit non-zero.
3. **``_ref_of`` polymorphism** -- locks down the two reference dialects
   the codebase already has to handle.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.lvs import (
    BoardNetlistMismatch,
    LVSResult,
    _ref_of,
    compare_netlists,
)
from kicad_tools.sexp import SExp, parse_file, parse_string

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "00-simple-led"
BOARD_OUTPUT = BOARD_DIR / "output"
BOARD_SCH = BOARD_OUTPUT / "simple_led.kicad_sch"
BOARD_PCB = BOARD_OUTPUT / "simple_led_routed.kicad_pcb"


@pytest.fixture(scope="module")
def board00_artifacts() -> tuple[Path, Path]:
    """The committed board 00 schematic + routed PCB.

    These are checked in under ``boards/00-simple-led/output/`` after the
    #3747 D1 polarity fix landed, so the comparator should report clean.
    The fixture skips if either artifact is missing -- it's possible (if
    unusual) for a contributor to wipe the output directory locally.
    """
    if not BOARD_SCH.exists() or not BOARD_PCB.exists():
        pytest.skip(
            f"board 00 artifacts not present "
            f"(sch={BOARD_SCH.exists()}, pcb={BOARD_PCB.exists()}); "
            "run boards/00-simple-led/generate_design.py to regenerate."
        )
    return BOARD_SCH, BOARD_PCB


class TestCompareNetlistsCleanOnBoard00:
    """Board 00's committed artifacts must compare clean (#3748 happy path)."""

    def test_clean_result(self, board00_artifacts: tuple[Path, Path]) -> None:
        sch, pcb = board00_artifacts
        result = compare_netlists(sch, pcb)
        assert isinstance(result, LVSResult)
        assert result.clean is True, (
            f"LVS unexpectedly dirty on the committed board 00 artifacts: {result.mismatches}"
        )
        assert result.mismatches == ()


def _swap_d1_pad_nets(pcb_path: Path, tmp_dest: Path) -> Path:
    """Copy ``pcb_path`` to ``tmp_dest`` with D1's pads 1/2 net names swapped.

    Produces a deliberately mismatched PCB so the LVS comparator must
    report two mismatches (D1.1 and D1.2 with their nets crossed).  Done
    via :func:`kicad_tools.sexp.parse_file` so we don't depend on the
    physical text layout of the source file.
    """
    doc = parse_file(pcb_path)
    for fp in doc.find_all("footprint"):
        if _ref_of(fp) != "D1":
            continue
        # Find the (pad "1" ...) and (pad "2" ...) children and swap
        # their (net N "NAME") child nodes wholesale.  Using full-node
        # replacement keeps both the net number and the name in sync.
        pad1 = None
        pad2 = None
        for pad in fp.find_all("pad"):
            num = pad.get_string(0)
            if num == "1":
                pad1 = pad
            elif num == "2":
                pad2 = pad
        assert pad1 is not None and pad2 is not None, (
            "D1 footprint must have pads 1 and 2; PCB structure changed."
        )
        net1 = pad1.find("net")
        net2 = pad2.find("net")
        assert net1 is not None and net2 is not None, (
            "D1 pads must have (net ...) entries; otherwise nothing to swap."
        )
        # Replace pad1's (net ...) with pad2's, and vice versa.  We
        # build replacements rather than mutating shared nodes to avoid
        # accidentally aliasing children across pads.
        new_net_for_1 = SExp.list("net", net2.get_int(0), net2.get_string(1) or "")
        new_net_for_2 = SExp.list("net", net1.get_int(0), net1.get_string(1) or "")
        # Splice in: replace the old net node at its original child index.
        for pad, old_net, new_net in (
            (pad1, net1, new_net_for_1),
            (pad2, net2, new_net_for_2),
        ):
            for i, child in enumerate(pad.children):
                if child is old_net:
                    pad.children[i] = new_net
                    break
        break
    tmp_dest.write_text(doc.to_string() + "\n")
    return tmp_dest


class TestCompareNetlistsDetectsReversedLed:
    """Swapping D1.1↔D1.2 nets on the PCB must produce a dirty LVS result."""

    @pytest.fixture
    def mismatched_pcb(
        self,
        board00_artifacts: tuple[Path, Path],
        tmp_path: Path,
    ) -> tuple[Path, Path]:
        sch, pcb = board00_artifacts
        bad_pcb = _swap_d1_pad_nets(pcb, tmp_path / "simple_led_bad.kicad_pcb")
        return sch, bad_pcb

    def test_reports_two_d1_mismatches(self, mismatched_pcb: tuple[Path, Path]) -> None:
        sch, bad_pcb = mismatched_pcb
        result = compare_netlists(sch, bad_pcb)
        assert result.clean is False
        # Pull just the D1 mismatches; the comparator emits both
        # (ref, pad) keys involved in the swap.
        d1_mismatches = {(m.ref, m.pad): m for m in result.mismatches if m.ref == "D1"}
        assert set(d1_mismatches.keys()) == {("D1", "1"), ("D1", "2")}, (
            f"Expected D1.1 and D1.2 in the mismatch list; got "
            f"{[(m.ref, m.pad) for m in result.mismatches]}"
        )
        # And the nets must be crossed (schematic still says cathode/anode
        # on the original pads; PCB now binds them the other way around).
        m1 = d1_mismatches[("D1", "1")]
        m2 = d1_mismatches[("D1", "2")]
        assert m1.schematic_net == "GND" and m1.pcb_net == "LED_ANODE", (
            f"D1.1 mismatch wrong: schematic={m1.schematic_net!r} pcb={m1.pcb_net!r}"
        )
        assert m2.schematic_net == "LED_ANODE" and m2.pcb_net == "GND", (
            f"D1.2 mismatch wrong: schematic={m2.schematic_net!r} pcb={m2.pcb_net!r}"
        )

    def test_board_netlist_mismatch_message_names_pads(
        self, mismatched_pcb: tuple[Path, Path]
    ) -> None:
        """The recipe-facing exception must surface both ref.pad keys.

        Regression for the #3747-style bug: a developer triaging a
        failed recipe should see the exact pin pair without having to
        open ``lvs.json`` first.
        """
        sch, bad_pcb = mismatched_pcb
        result = compare_netlists(sch, bad_pcb)
        exc = BoardNetlistMismatch(result)
        msg = str(exc)
        assert "D1.1" in msg, f"BoardNetlistMismatch message missing D1.1: {msg!r}"
        assert "D1.2" in msg, f"BoardNetlistMismatch message missing D1.2: {msg!r}"
        # And the exception keeps the structured result for callers that
        # want to inspect it programmatically (tests, CLI wrappers).
        assert exc.result is result


class TestRefOfHandlesBothReferenceForms:
    """``_ref_of`` must resolve both the generator and KiCad-CLI dialects."""

    def test_fp_text_reference_form(self) -> None:
        """Pre-round-trip form: ``(fp_text reference "R1" ...)``."""
        fp = parse_string(
            '(footprint "Resistor_SMD:R_0805_2012Metric" '
            '(layer "F.Cu") '
            '(fp_text reference "R1" (at 0 -1.5) (layer "F.SilkS")) '
            '(fp_text value "330" (at 0 1.5) (layer "F.Fab")))'
        )
        assert _ref_of(fp) == "R1"

    def test_property_reference_form(self) -> None:
        """Post-round-trip form: ``(property "Reference" "U2" ...)``."""
        fp = parse_string(
            '(footprint "Package_QFP:LQFP-48" '
            '(layer "F.Cu") '
            '(property "Reference" "U2" (at 0 -5 0) (layer "F.SilkS")) '
            '(property "Value" "STM32F0" (at 0 5 0) (layer "F.Fab")))'
        )
        assert _ref_of(fp) == "U2"

    def test_missing_reference_returns_none(self) -> None:
        """A footprint with no reference field must yield ``None`` (not raise)."""
        fp = parse_string(
            '(footprint "Misc:NoRef" (layer "F.Cu") (fp_text value "X" (at 0 0) (layer "F.Fab")))'
        )
        assert _ref_of(fp) is None
