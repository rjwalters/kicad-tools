"""Regression test: board 01 (voltage-divider) is JLCPCB ship-ready.

Pins the post-fix invariants from #3291:

1. The schematic's named-net set matches the routed PCB's named-net set
   exactly (no ``VIN``/``+5V`` drift). ``kct fleet status`` uses the same
   comparison; if this test passes, the fleet status verdict for board 01
   will be ``YES`` rather than ``NO (schematic drift)``.

2. The BOM CSV emitted by ``kct export`` does NOT include the synthesized
   ``VIN`` power symbol (``#PWR01`` referencing ``kicad_tools_pwr:VIN``).
   Prior to the fix in ``schema/bom.py::is_power_symbol`` and
   ``export/bom_formats.py::filter_items``, the synthesized rail leaked
   into ``bom_jlcpcb.csv`` causing a BOM/PCB mismatch in the export
   preflight.

3. ``kct check --mfr jlcpcb`` reports 0 errors / 0 warnings on the
   committed routed PCB.

Board 01 is the gold standard per #2394; if any of these regress, every
downstream gate that uses board 01 as a fleet smoke test is invalidated.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BOARD_DIR = REPO_ROOT / "boards" / "01-voltage-divider"
SCH = BOARD_DIR / "output" / "voltage_divider.kicad_sch"
PCB = BOARD_DIR / "output" / "voltage_divider_routed.kicad_pcb"
BOM_CSV = BOARD_DIR / "output" / "manufacturing" / "bom_jlcpcb.csv"


def _require_artifacts() -> None:
    if not SCH.exists() or not PCB.exists():
        pytest.skip("Board 01 schematic/PCB artifacts not committed")


def test_board_01_schematic_pcb_net_sets_match() -> None:
    """Schematic and PCB must agree on net names (no VIN/+5V drift)."""
    _require_artifacts()
    from kicad_tools.cli.fleet_cmd import (
        _extract_pcb_named_nets,
        _extract_schematic_nets,
    )

    sch_nets = _extract_schematic_nets(SCH)
    pcb_nets = _extract_pcb_named_nets(PCB)
    assert sch_nets is not None, "Schematic must be parseable"
    assert pcb_nets is not None, "PCB must be parseable"

    added = pcb_nets - sch_nets
    removed = sch_nets - pcb_nets
    assert not added and not removed, (
        f"Net drift: added in PCB={sorted(added)}, "
        f"removed from PCB={sorted(removed)}; "
        "regenerate via boards/01-voltage-divider/generate_design.py"
    )

    # Lock in the expected canonical names so future renames are visible.
    assert sch_nets == {"VIN", "VOUT", "GND"}, f"Unexpected schematic net set: {sorted(sch_nets)}"


def test_board_01_bom_excludes_synthesized_power_symbol() -> None:
    """BOM CSV must not include the synthesized VIN power symbol (#PWR01)."""
    if not BOM_CSV.exists():
        pytest.skip("Manufacturing BOM not yet generated for board 01")

    with BOM_CSV.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    designators = [r.get("Designator", "") for r in rows]
    flat = ",".join(designators)
    assert "#PWR" not in flat, f"Power symbol leaked into BOM CSV: designators={designators}"
    # Must have exactly the four real components (R1+R2 grouped).
    refs = sorted(d.strip() for r in designators for d in r.split(","))
    assert refs == ["J1", "J2", "R1", "R2"], f"Unexpected BOM designators: {refs}"


def test_board_01_bom_formatter_skips_virtual_items() -> None:
    """Unit-level guard: BOMFormatter.filter_items drops virtual symbols."""
    from kicad_tools.export.bom_formats import JLCPCBBOMFormatter
    from kicad_tools.schema.bom import BOMItem

    real = BOMItem(
        reference="R1",
        value="10k",
        footprint="Resistor_SMD:R_0805_2012Metric",
        lib_id="Device:R",
    )
    stock_power = BOMItem(
        reference="#PWR02",
        value="GND",
        footprint="",
        lib_id="power:GND",
    )
    synth_power = BOMItem(
        reference="#PWR01",
        value="VIN",
        footprint="",
        lib_id="kicad_tools_pwr:VIN",
    )

    formatter = JLCPCBBOMFormatter()
    kept = formatter.filter_items([real, stock_power, synth_power])
    kept_refs = {item.reference for item in kept}

    assert kept_refs == {"R1"}, f"Expected only R1 to survive filter, got {kept_refs}"


def test_board_01_drc_clean_with_jlcpcb_rules() -> None:
    """Routed PCB must pass JLCPCB DRC via the same pure-Python path the
    CLI uses (``DRCChecker`` + ``run_selected_checks``)."""
    _require_artifacts()
    from kicad_tools.cli.check_cmd import run_selected_checks
    from kicad_tools.schema.pcb import PCB as PCBModel
    from kicad_tools.validate.checker import DRCChecker

    pcb = PCBModel.load(str(PCB))
    checker = DRCChecker(
        pcb,
        manufacturer="jlcpcb",
        layers=2,
        copper_oz=1.0,
        suppress_library=True,
    )
    results = run_selected_checks(
        checker,
        only_set=None,
        skip_set=set(),
    )
    # Issue #3527 / #3549 (June 2026): the ``clearance_segment_zone``
    # rule surfaced 4 stale-fill shorts (VIN/VOUT vs the GND F.Cu fill)
    # in the committed artifact; the fix re-filled the zones against the
    # final copper, so the board is back on the strict 0-error gate (the
    # grandfathered entry in .github/routed-drc-tolerance.yml was removed
    # in the same PR).  No error class is exempt here.
    error_count = sum(1 for v in results.violations if v.is_error)

    # Issue #3939: the connector refdes offset in
    # ``boards/01-voltage-divider/generate_design.py`` was moved from
    # ``(at 0 -2.5)`` to ``(at 0 -3.5)`` so J1/J2's reference designators
    # clear pad 1's mask aperture.  A fresh regen of the committed artifact
    # now passes the geometric ``silk_over_copper`` rule cleanly, so the
    # earlier ``_SILK_PLACEMENT_RULES`` carve-out is removed and the strict
    # 0-warning gate holds unconditionally.
    warn_count = sum(1 for v in results.violations if v.is_warning)

    sample = [
        f"{v.rule_id}: {v.message}" for v in results.violations if v.is_error or v.is_warning
    ][:5]
    assert error_count == 0, f"Expected 0 DRC errors, got {error_count}; sample={sample}"
    assert warn_count == 0, f"Expected 0 DRC warnings, got {warn_count}; sample={sample}"
