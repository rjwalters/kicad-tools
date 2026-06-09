"""Regression guard: board-05 U3 (gate driver) BOM/PCB/schematic part number must agree.

Issue #3384: ``kct pcb sync-netlist`` reports ~40 U3 pad mismatches against
the committed ``bldc_controller_routed.kicad_pcb`` because the schematic
emits its gate-driver symbol using KiCad's stock ``Driver_Motor:DRV8308``
(a 39-pin part with a completely different pinout than the DRV8301), while
the BOM ships ``DRV8301`` (HTSSOP-56, LCSC C129292) and the PCB carries
the matching ``Package_SO:HTSSOP-56-1EP_6.1x14mm_P0.5mm_EP3.61x6.35mm``
footprint with 57 pads.  See PR description for the full investigation.

This test pins down the **BOM/PCB/schematic-value** invariants that are
load-bearing for physical manufacturing:

* The BOM lists ``DRV8301`` with the HTSSOP-56 footprint.  This is what
  physically ships -- whatever it says is the truth.
* The PCB footprint is ``Package_SO:HTSSOP-56-1EP_*`` (DRV8301's DCA
  package).  Matches the BOM.
* The schematic's U3 ``Value`` field is ``DRV8301`` -- matches BOM.

The schematic's ``lib_id`` (``Driver_Motor:DRV8308``) is **intentionally
not asserted here** because the stock KiCad library doesn't ship a
DRV8301 symbol and replacing the symbol requires a project-local symbol
library plus a non-trivial schematic re-layout (the existing
MCU-to-U3 routing infrastructure in ``deferred_mcu_labels`` /
``_connect_mcu_pin_to_label`` is wired against the DRV8308 pin layout).
See the spun-off follow-on issue (linked from #3384) for the symbol-side
fix.  Filing that under a separate issue keeps this regression test
focused on the BOM<->PCB<->schematic-value triangle that any future
gate-driver replacement (e.g. swap to a real DRV8301 symbol) must
preserve.

If any of the three assertions below regress -- e.g. a Builder changes the
``generate_htssop56`` call value, edits the BOM template, or renames the
``GateDriverBlock`` value kwarg -- this test fails loudly with a pointer
to the asymmetric edit.
"""

from __future__ import annotations

import csv
import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "05-bldc-motor-controller"
DESIGN_PY = BOARD_DIR / "design.py"

# Expected DRV8301 invariants -- the BOM is the ground truth (it
# determines what TI part actually ships on the board).
EXPECTED_PART_NUMBER = "DRV8301"
EXPECTED_PCB_FOOTPRINT_PREFIX = "Package_SO:HTSSOP-56-1EP"
EXPECTED_LCSC = "C129292"  # JLCPCB / LCSC part number for DRV8301


def _load_design_module():
    """Import the board-05 ``design.py`` as a fresh module."""
    if not DESIGN_PY.exists():
        pytest.skip(f"Board 05 design.py not found at {DESIGN_PY!s}")

    spec = importlib.util.spec_from_file_location("board_05_design", DESIGN_PY)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        pytest.skip(f"Could not load spec for {DESIGN_PY!s}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["board_05_design"] = module
    spec.loader.exec_module(module)
    return module


def test_bom_lists_drv8301_with_htssop56() -> None:
    """The committed JLCPCB BOM must list U3 as DRV8301, HTSSOP-56.

    The BOM is the authoritative source for what TI part ships on the
    fabricated board.  ``kct pcb sync-netlist`` checks pad nets against
    the schematic netlist export; the schematic symbol must match the
    pin/pad numbering of the physical part, which is determined by the
    BOM row for U3.
    """
    bom_path = BOARD_DIR / "output" / "manufacturing" / "bom_jlcpcb.csv"
    if not bom_path.exists():
        pytest.skip(
            f"BOM file not found at {bom_path!s} -- run "
            "boards/05-bldc-motor-controller/design.py first"
        )

    with bom_path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))

    u3_rows = [row for row in rows if "U3" in (row.get("Designator", "") or "")]
    assert u3_rows, (
        f"BOM at {bom_path!s} does not reference U3.  Expected the gate "
        f"driver row to designate U3 as {EXPECTED_PART_NUMBER}."
    )
    # The Designator field may aggregate multiple refs (e.g. "U1,U2"), so
    # narrow to rows where U3 is one of the comma-separated tokens.
    u3_rows = [
        row for row in u3_rows
        if "U3" in [token.strip() for token in row["Designator"].split(",")]
    ]
    assert len(u3_rows) == 1, (
        f"Expected exactly one BOM row referencing U3, got {len(u3_rows)}: "
        f"{u3_rows}"
    )

    row = u3_rows[0]
    comment = row.get("Comment", "") or ""
    footprint = row.get("Footprint", "") or ""
    lcsc = (row.get("LCSC Part #", "") or "").strip()

    assert comment == EXPECTED_PART_NUMBER, (
        f"BOM U3 Comment is {comment!r}, expected {EXPECTED_PART_NUMBER!r}. "
        f"The Comment field drives the JLCPCB part lookup and must match "
        f"what TI catalogues (DRV8301 ships only in the HTSSOP-56 / DCA "
        f"package per datasheet SLOS719F)."
    )
    assert footprint.startswith(EXPECTED_PCB_FOOTPRINT_PREFIX), (
        f"BOM U3 Footprint is {footprint!r}, expected to start with "
        f"{EXPECTED_PCB_FOOTPRINT_PREFIX!r} (the DRV8301 DCA package).  "
        f"If U3 is genuinely a different part, update the schematic and "
        f"PCB to match -- this test guards the BOM<->PCB consistency."
    )
    # LCSC C129292 is JLCPCB's catalogue entry for DRV8301PHPR -- pin
    # this to make a silent part substitution surface loudly here.
    assert lcsc == EXPECTED_LCSC, (
        f"BOM U3 LCSC part number is {lcsc!r}, expected {EXPECTED_LCSC!r} "
        f"(DRV8301 on JLCPCB).  A different LCSC code likely means the "
        f"footprint or part number drifted -- reconcile before shipping."
    )


def test_committed_pcb_u3_footprint_matches_bom() -> None:
    """The committed routed PCB's U3 footprint must be HTSSOP-56.

    Sync-netlist resolves PCB pads against schematic pins; a footprint
    swap (e.g. to LQFP-48 for DRV8308) would invalidate the pad numbering
    and silently bridge nets.  This test catches a regression of the
    committed PCB to a non-DRV8301 footprint.
    """
    pcb_path = BOARD_DIR / "output" / "bldc_controller_routed.kicad_pcb"
    if not pcb_path.exists():
        pytest.skip(
            f"Committed routed PCB not found at {pcb_path!s} -- run "
            "boards/05-bldc-motor-controller/design.py first"
        )

    text = pcb_path.read_text()

    # Locate U3's footprint declaration.  S-expr blocks are bracket-
    # balanced; we use a relaxed regex to grab the footprint string that
    # precedes the ``"U3"`` Reference property.
    # Pattern: ``(footprint "<fp_string>" ... (property "Reference" "U3"``
    pattern = re.compile(
        r'\(footprint\s+"([^"]+)".*?\(property\s+"Reference"\s+"U3"',
        re.DOTALL,
    )
    matches = pattern.findall(text)
    assert matches, (
        f"Could not find U3's footprint declaration in {pcb_path!s}.  The "
        f"committed PCB must include a U3 footprint with a "
        f"{EXPECTED_PCB_FOOTPRINT_PREFIX!r}-prefixed library reference."
    )

    # Match the LAST occurrence -- regex is greedy in DOTALL mode and may
    # capture a footprint string from an earlier component; the closest
    # ``(footprint ...`` block before ``"U3"`` is the relevant one.
    # Re-scan for the closest match by walking backwards from each
    # ``(property "Reference" "U3"`` position.
    fp_string: str | None = None
    for u3_ref_match in re.finditer(r'\(property\s+"Reference"\s+"U3"', text):
        # Walk backwards to the nearest preceding ``(footprint "..."``.
        upto = text[: u3_ref_match.start()]
        fp_match = list(re.finditer(r'\(footprint\s+"([^"]+)"', upto))
        if fp_match:
            fp_string = fp_match[-1].group(1)
            break

    assert fp_string is not None, (
        f"Could not resolve U3's library footprint in {pcb_path!s}.  "
        f"Expected a {EXPECTED_PCB_FOOTPRINT_PREFIX!r}-prefixed lib_id."
    )
    assert fp_string.startswith(EXPECTED_PCB_FOOTPRINT_PREFIX), (
        f"Committed PCB U3 footprint is {fp_string!r}, expected to start "
        f"with {EXPECTED_PCB_FOOTPRINT_PREFIX!r}.  A footprint swap "
        f"requires re-routing -- file a follow-on issue, do not ship."
    )


def test_design_py_emits_u3_value_drv8301(tmp_path: Path) -> None:
    """Fresh design.py build must emit U3 with Value=DRV8301 on both sides.

    The schematic-side ``Value`` field and the PCB-side footprint Value
    text both flow into the BOM.  This regression test rebuilds the
    schematic + unrouted PCB from ``design.py`` and confirms both sides
    emit ``"DRV8301"`` as U3's value (matching the BOM expectation).

    Note: this test does NOT assert the schematic's ``lib_id`` -- the
    stock KiCad library only ships ``Driver_Motor:DRV8308``, so the
    current schematic uses that symbol with a ``value="DRV8301"``
    override.  The full symbol-side fix (custom DRV8301 library +
    DRV8301-pin-numbered emission) is tracked in a follow-on issue
    linked from #3384.
    """
    module = _load_design_module()

    sch_path = module.create_bldc_controller(tmp_path)
    pcb_path = module.create_bldc_pcb(tmp_path)

    assert sch_path.exists(), f"design.py did not write schematic to {sch_path!s}"
    assert pcb_path.exists(), f"design.py did not write PCB to {pcb_path!s}"

    # Schematic side: locate U3's Value property.
    sch_text = sch_path.read_text()
    # The symbol block has a ``Reference`` property of "U3" and a
    # ``Value`` property with the part number.  We scan forward from the
    # first occurrence of ``"U3"`` as a Reference value to the next
    # Value property.
    sch_u3_ref = re.search(
        r'\(property\s+"Reference"\s+"U3"', sch_text
    )
    assert sch_u3_ref is not None, (
        f"Could not locate U3 Reference property in schematic at {sch_path!s}"
    )
    sch_after = sch_text[sch_u3_ref.end():]
    sch_value = re.search(r'\(property\s+"Value"\s+"([^"]+)"', sch_after)
    assert sch_value is not None, (
        f"Could not locate U3 Value property in schematic at {sch_path!s}"
    )
    assert sch_value.group(1) == EXPECTED_PART_NUMBER, (
        f"Schematic U3 Value is {sch_value.group(1)!r}, expected "
        f"{EXPECTED_PART_NUMBER!r}.  The schematic Value field drives "
        f"the BOM Comment column -- a mismatch would ship the wrong TI "
        f"part number to JLCPCB."
    )

    # PCB side: locate U3's footprint block.  The PCB uses
    # ``(fp_text reference "U3")`` (KiCad ``.kicad_pcb`` text format) and
    # ``(fp_text value "DRV8301")``, NOT the schematic's ``(property
    # "Reference" "U3")`` form.
    pcb_text = pcb_path.read_text()
    pcb_u3_ref = re.search(
        r'\(fp_text\s+reference\s+"U3"', pcb_text
    )
    assert pcb_u3_ref is not None, (
        f"Could not locate U3 fp_text reference in PCB at {pcb_path!s}"
    )
    # Walk back to the enclosing ``(footprint ...)`` block and forward
    # to its ``(fp_text value "...")`` to read U3's value text.
    upto = pcb_text[: pcb_u3_ref.start()]
    fp_match_list = list(re.finditer(r'\(footprint\s+"[^"]+"', upto))
    assert fp_match_list, (
        f"Could not locate enclosing footprint block for U3 in {pcb_path!s}"
    )
    fp_start = fp_match_list[-1].start()
    # Search for the next fp_text value within this block, before the
    # next footprint block starts.
    next_fp = re.search(r'\(footprint\s+"[^"]+"', pcb_text[pcb_u3_ref.end():])
    block_end = (
        pcb_u3_ref.end() + next_fp.start() if next_fp else len(pcb_text)
    )
    fp_block = pcb_text[fp_start:block_end]
    fp_value = re.search(r'\(fp_text\s+value\s+"([^"]+)"', fp_block)
    if fp_value is not None:
        # If the PCB stamps a Value, it must agree with the schematic
        # and BOM.  ``generate_htssop56`` writes "DRV8301" via the value
        # kwarg passed at the call site -- a typo there would surface
        # here.
        assert fp_value.group(1) == EXPECTED_PART_NUMBER, (
            f"PCB U3 fp_text value is {fp_value.group(1)!r}, expected "
            f"{EXPECTED_PART_NUMBER!r}.  A mismatch indicates "
            f"``generate_htssop56(\"U3\", ..., value=...)`` was edited "
            f"to a different string -- reconcile with the schematic."
        )
