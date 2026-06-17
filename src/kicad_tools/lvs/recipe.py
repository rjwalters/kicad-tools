"""Shared board-recipe LVS step (issue #3762).

Board 00's ``run_lvs()`` (``boards/00-simple-led/generate_design.py``) was
the original template: it runs LVS, writes ``output/lvs.json`` (v1 schema),
and raises :class:`BoardNetlistMismatch` on a dirty board so the recipe's
exit gate trips.  This module extracts that logic into a single reusable
entrypoint, :func:`write_lvs_report`, so the copper-LVS manufacturability
leg can be wired into every demo board without 7x copy-paste drift.

It runs **both** comparators:

* the label-based :func:`compare_netlists` (trusts each pad's ``(net ...)``
  label), and
* the copper-extracted :func:`compare_copper_netlist` (#3742; diffs the
  *physical* copper partition against the schematic, catching shorts/opens
  a mislabeled router would hide).

The emitted ``lvs.json`` records both comparators' results.  ``clean`` is
the AND of the *gated* comparators (the ones selected via ``run_copper`` /
``run_label``); a comparator that is run-but-not-gated is reflected in the
payload but does not flip ``clean`` or trigger a raise.

Gate policy is per-board (see the curator matrix on #3762):

* Boards verified clean (00, 01, 02) gate on both comparators.
* Boards 06/07 are copper-clean but label-dirty (PCB-first test fixtures
  whose floating schematic pins read ``schematic_net=None``); they gate on
  copper only (``run_label=False``).
* Boards in :data:`ADVISORY_LVS_BOARDS` (03/04/05) are genuinely dirty
  today; they still run LVS and emit ``lvs.json`` so the gallery chip and
  ``board-metrics`` surface the true state, but pass ``require_clean=False``
  so the recipe logs the mismatch summary without raising.
"""

from __future__ import annotations

import json
from pathlib import Path

from kicad_tools.lvs.board_lvs import (
    BoardNetlistMismatch,
    LVSResult,
    compare_netlists,
)
from kicad_tools.lvs.copper_lvs import CopperLVSResult, compare_copper_netlist

# Boards that run LVS + emit ``lvs.json`` but are NOT yet copper/label clean.
# Recipes for these boards must pass ``require_clean=False`` so a dirty
# comparator logs a summary instead of raising.  CI does NOT assert
# ``clean=true`` for these boards.  This allowlist is the single auditable
# place for the exemption -- shrink it as per-board fix follow-ups land
# (boards 03/04 board-fix issues; board 05 blocked behind #3775/#3766).
ADVISORY_LVS_BOARDS: frozenset[str] = frozenset(
    {
        "03-usb-joystick",
        "04-stm32-devboard",
        "05-bldc-motor-controller",
    }
)

# JSON Schema URL stamped into every emitted ``lvs.json``.  Kept identical
# to board-00's historical value so downstream readers (board-metrics
# ``_parse_lvs``, ``scripts/ci/check_board_00_e2e.py``) are unaffected.
_LVS_SCHEMA_URL = "https://kicad-tools.org/schemas/lvs/v1.json"


def write_lvs_report(
    sch_path: Path,
    routed_pcb_path: Path,
    output_dir: Path,
    *,
    require_clean: bool = True,
    run_copper: bool = True,
    run_label: bool = True,
) -> tuple[bool, bool]:
    """Run copper + label LVS, write ``output/lvs.json``, optionally raise.

    Args:
        sch_path: Path to the schematic (design intent).
        routed_pcb_path: Path to the routed PCB (what manufacturing sees).
        output_dir: Directory to write ``lvs.json`` into (created if absent).
        require_clean: When ``True`` (hard gate), raise
            :class:`BoardNetlistMismatch` if any *gated* comparator is dirty.
            When ``False`` (advisory), log the mismatch summary and return
            the dirty flags without raising.
        run_copper: When ``True``, run the copper-extracted comparator and
            include it in the gated ``clean`` decision.
        run_label: When ``True``, run the label-based comparator and include
            it in the gated ``clean`` decision.

    Returns:
        ``(copper_clean, label_clean)``.  A comparator that was not run is
        reported as ``True`` (vacuously clean) so callers can treat the
        return as "nothing gated is dirty".

    Raises:
        BoardNetlistMismatch: when ``require_clean`` and a gated comparator
            is dirty.  The report is still written before raising.
        ValueError: when neither comparator is selected to run.
    """
    if not run_copper and not run_label:
        raise ValueError("write_lvs_report: at least one of run_copper/run_label must be True")

    print("\n" + "=" * 60)
    print("Running LVS (schematic <-> PCB netlist match)...")
    print("=" * 60)

    copper_result = compare_copper_netlist(sch_path, routed_pcb_path) if run_copper else None
    label_result = compare_netlists(sch_path, routed_pcb_path) if run_label else None

    copper_clean = copper_result.clean if copper_result is not None else True
    label_clean = label_result.clean if label_result is not None else True

    # ``clean`` is the AND of only the *gated* comparators.  A comparator
    # that was run but not selected does not flip ``clean``.
    gated_clean = copper_clean and label_clean

    output_dir.mkdir(parents=True, exist_ok=True)
    lvs_path = output_dir / "lvs.json"
    lvs_path.write_text(
        json.dumps(
            _build_payload(copper_result, label_result, clean=gated_clean),
            indent=2,
        )
        + "\n"
    )

    _print_summary(copper_result, label_result, lvs_path, run_copper, run_label)

    if not gated_clean and require_clean:
        # Reuse BoardNetlistMismatch (board-00's exit-gate exception).  When
        # only copper is dirty we synthesize an LVSResult so the exception's
        # carried ``.result`` still reflects "dirty"; the human-readable
        # copper detail was already printed by ``_print_summary``.
        raise BoardNetlistMismatch(_mismatch_result(copper_result, label_result))

    return copper_clean, label_clean


def _build_payload(
    copper_result: CopperLVSResult | None,
    label_result: LVSResult | None,
    *,
    clean: bool,
) -> dict:
    """Assemble the v1 ``lvs.json`` payload.

    ``mismatches`` carries the label-based mismatches (unchanged from the
    historical board-00 schema so ``_parse_lvs`` and the e2e asserter keep
    working).  ``copper_mismatches`` is an additive field recording the
    copper-extracted shorts/opens so a copper-dirty board is reflected too.
    """
    payload: dict = {
        "$schema": _LVS_SCHEMA_URL,
        "clean": clean,
        "mismatches": [
            {
                "ref": lm.ref,
                "pad": lm.pad,
                "schematic_net": lm.schematic_net,
                "pcb_net": lm.pcb_net,
            }
            for lm in (label_result.mismatches if label_result is not None else ())
        ],
        "copper_mismatches": [
            {
                "kind": cm.kind,
                "net_a": cm.net_a,
                "net_b": cm.net_b,
                "pad_a": cm.pad_a,
                "pad_b": cm.pad_b,
            }
            for cm in (copper_result.mismatches if copper_result is not None else ())
        ],
    }
    return payload


def _mismatch_result(
    copper_result: CopperLVSResult | None,
    label_result: LVSResult | None,
) -> LVSResult:
    """Pick the LVSResult to carry on the raised exception.

    Prefer the label result when it is the dirty one (it has the
    pin-level ``ref/pad`` detail the exception message renders).  When only
    copper is dirty, fall back to a synthetic dirty ``LVSResult`` (the
    copper detail was already printed in the summary).
    """
    if label_result is not None and not label_result.clean:
        return label_result
    return LVSResult(clean=False, mismatches=())


def _print_summary(
    copper_result: CopperLVSResult | None,
    label_result: LVSResult | None,
    lvs_path: Path,
    run_copper: bool,
    run_label: bool,
) -> None:
    """Print a human-readable LVS summary to the recipe log."""
    if run_label and label_result is not None:
        if label_result.clean:
            print(f"\n   label-LVS PASS: 0 mismatches ({lvs_path.name})")
        else:
            print(f"\n   label-LVS FAIL: {len(label_result.mismatches)} mismatch(es):")
            for lm in label_result.mismatches[:5]:
                print(
                    f"      - {lm.ref}.{lm.pad}: schematic={lm.schematic_net!r} pcb={lm.pcb_net!r}"
                )

    if run_copper and copper_result is not None:
        if copper_result.clean:
            print("   copper-LVS PASS: 0 shorts / 0 opens")
        else:
            print(
                f"   copper-LVS FAIL: {len(copper_result.shorts)} short(s) / "
                f"{len(copper_result.opens)} open(s):"
            )
            for cm in copper_result.mismatches[:5]:
                print(f"      - {cm.kind}: {cm.net_a} <-> {cm.net_b} ({cm.pad_a}, {cm.pad_b})")
