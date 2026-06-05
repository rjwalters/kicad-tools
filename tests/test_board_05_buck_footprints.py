"""Regression guard: board-05 buck-section symbols have populated footprints.

Issue #3211: ``create_dual_supply_cascade`` leaves the buck-stage Symbol
objects' ``.footprint`` attributes unset by default.  Before this fix,
five buck-section symbols (U1 LM2596, C3/C4 220uF caps, L1 33uH
inductor, D2 SS34 Schottky) emitted empty ``(property "Footprint" "")``
entries in ``boards/05-bldc-motor-controller/output/bldc_controller.kicad_sch``,
which caused the manufacturing preflight ``bom_fields`` check to FAIL
with "missing footprint" warnings on those five refs.

The fix, applied in ``boards/05-bldc-motor-controller/design.py`` right
after the existing LDO-stage footprint-patch block, assigns
schematic-side footprints that match the strings the PCB s-expression
already emits via ``generate_d2pak`` / ``generate_cap_0805`` /
``generate_inductor_smd`` / ``generate_diode_sma`` (see design.py:1433,
1758, 1851, 1870).  Mirroring the PCB-side strings keeps the
schematic↔PCB drift checker (recently tightened by #3210) clean.

This test parses the committed schematic and asserts that each of the
five buck-section symbols has a non-empty ``Footprint`` property whose
value matches the PCB-side helper hard-codes.  It is intentionally
parser-based (no KiCad-CLI dependency) so it runs on developer machines
without KiCad installed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "05-bldc-motor-controller"
SCH_PATH = BOARD_DIR / "output" / "bldc_controller.kicad_sch"

# Expected footprint strings for each buck-section symbol.  These mirror
# the PCB-side hard-codes in design.py at lines 1433 (TO-263-5),
# 1758 (C_0805), 1851 (L_1210), and 1870 (D_SMA).  A mismatch would be
# caught by the schematic↔PCB drift checker that PR #3217 tightened.
EXPECTED_BUCK_FOOTPRINTS = {
    "U1": "Package_TO_SOT_SMD:TO-263-5_TabPin3",
    "C3": "Capacitor_SMD:C_0805_2012Metric",
    "C4": "Capacitor_SMD:C_0805_2012Metric",
    "L1": "Inductor_SMD:L_1210_3225Metric",
    "D2": "Diode_SMD:D_SMA",
}


@pytest.fixture(scope="module")
def schematic_text() -> str:
    """Read the committed board-05 schematic or skip if absent.

    The schematic lives under ``boards/05-bldc-motor-controller/output/``
    and is committed to git so this test is self-contained.  If the file
    is missing (someone wiped output/), skip with a regen hint rather
    than fail spuriously.
    """
    if not SCH_PATH.exists():
        pytest.skip(
            f"Board 05 schematic not found at {SCH_PATH!s}; "
            "regenerate via "
            "`uv run python boards/05-bldc-motor-controller/design.py`"
        )
    return SCH_PATH.read_text()


def _extract_footprint_for_ref(sch_text: str, ref: str) -> str | None:
    """Return the value of the ``Footprint`` property for *ref*.

    KiCad schematic files emit each placed symbol as a ``(symbol ...)``
    block with several ``(property "<name>" "<value>" ...)`` lines, one
    of which is ``"Reference"`` and another ``"Footprint"``.  We anchor
    on the matching ``Reference`` line and then look for the nearest
    following ``Footprint`` line within the same symbol block (which
    KiCad always writes within ~10 lines of the Reference).

    Returns ``None`` if no Footprint property is found for the ref.
    """
    # Match the Reference line for our target ref, then capture the
    # value of the next Footprint property line.  Using a bounded
    # negative lookahead so we don't accidentally cross into the next
    # symbol block.
    pattern = (
        r'\(property "Reference" "' + re.escape(ref) + r'"'
        r'(?:(?!\(property "Reference").)*?'
        r'\(property "Footprint" "([^"]*)"'
    )
    match = re.search(pattern, sch_text, re.DOTALL)
    return match.group(1) if match else None


class TestBoard05BuckFootprints:
    """Pin the buck-section schematic footprints from PR fix for #3211."""

    @pytest.mark.parametrize("ref,expected", sorted(EXPECTED_BUCK_FOOTPRINTS.items()))
    def test_buck_symbol_footprint_matches_pcb(
        self, schematic_text: str, ref: str, expected: str
    ) -> None:
        """Each buck-section symbol's Footprint property is the expected string.

        Before the fix for #3211, ``create_dual_supply_cascade`` left
        these five symbols' ``.footprint`` attributes unset, so the
        schematic emitted ``(property "Footprint" "")``.  A regression
        that removes the patch block in design.py would reintroduce the
        empty-string failure and trip this assertion.

        The expected strings mirror the PCB-side helper hard-codes
        exactly so the schematic↔PCB drift checker stays clean.
        """
        actual = _extract_footprint_for_ref(schematic_text, ref)
        assert actual is not None, (
            f"No Footprint property found for {ref} in board-05 schematic; "
            f"symbol may have been removed or restructured."
        )
        assert actual == expected, (
            f"Board-05 {ref} footprint is {actual!r}, expected {expected!r} "
            f"(must match PCB-side hard-code to avoid schematic↔PCB drift)."
        )

    def test_all_buck_footprints_non_empty(self, schematic_text: str) -> None:
        """No buck-section symbol has an empty ``Footprint`` property.

        This is the headline acceptance check from issue #3211: the
        manufacturing preflight ``bom_fields`` check fails with
        "missing footprint" warnings whenever any of these five refs
        emits ``(property "Footprint" "")``.  Asserting non-empty
        catches the regression even if the specific string changes for
        a legitimate reason (e.g. moving to a different package family).
        """
        empty = []
        for ref in EXPECTED_BUCK_FOOTPRINTS:
            actual = _extract_footprint_for_ref(schematic_text, ref)
            if not actual:
                empty.append(ref)
        assert not empty, (
            f"Board-05 buck-section symbols with empty Footprint property: "
            f"{empty}.  Regression of issue #3211 fix in design.py."
        )
