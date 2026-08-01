"""``kct creepage-export-rules`` command handler (Issue #4508).

Emits referee-enforceable KiCad artifacts from a ``--voltage-map`` +
creepage-standard input:

* voltage-domain **netclasses** + net-name patterns into ``<project>.kicad_pro``,
  and
* pairwise clearance **(rule ...)** clauses into a sentinel-delimited block in
  ``<project>.kicad_dru``,

so ``kicad-cli pcb drc`` independently confirms the pairwise HV<->LV creepage
requirement kct's own router/placement/census already enforce (the project's
two-engine 0-DRC manufacturability bar).

This is a NEW SIBLING top-level command (``kct creepage-export-rules``), not a
subcommand of the flat ``kct creepage <pcb>`` census -- restructuring ``creepage``
into a group would break its documented flat invocation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def run_creepage_export_rules_command(args) -> int:
    """Handle ``creepage-export-rules``.  Returns the process exit code."""
    from kicad_tools.core.project_file import load_project, save_project
    from kicad_tools.creepage.engine import voltage_map_from_dict
    from kicad_tools.creepage.export_rules import (
        apply_netclass_assignments,
        build_export,
        merge_dru_block,
        render_dru_block_body,
    )
    from kicad_tools.creepage.standards import StandardLookupError
    from kicad_tools.schema.pcb import PCB

    project_path = Path(args.project)
    if not project_path.exists():
        print(f"Error: project file not found: {project_path}", file=sys.stderr)
        return 1
    if project_path.suffix != ".kicad_pro":
        print(
            f"Error: expected a .kicad_pro project file, got {project_path.name}",
            file=sys.stderr,
        )
        return 1

    # Resolve the sibling board (nets + footprints) and the .kicad_dru target.
    pcb_arg = getattr(args, "pcb", None)
    pcb_path = Path(pcb_arg) if pcb_arg else project_path.with_suffix(".kicad_pcb")
    dru_path = project_path.with_suffix(".kicad_dru")

    # No voltage map -> clean no-op (nothing to derive, nothing written).
    vmap_arg = getattr(args, "voltage_map", None)
    if not vmap_arg:
        print(
            "No --voltage-map supplied: nothing to export "
            "(pairwise HV rules are derived from the voltage map).  No files written."
        )
        return 0

    vmap_path = Path(vmap_arg)
    if not vmap_path.exists():
        print(f"Error: voltage-map file not found: {vmap_path}", file=sys.stderr)
        return 1
    try:
        intervals, _edge_voltage = voltage_map_from_dict(json.loads(vmap_path.read_text()))
    except json.JSONDecodeError as e:
        print(f"Error: parsing voltage-map JSON: {e}", file=sys.stderr)
        return 1
    except (TypeError, ValueError) as e:
        print(f"Error: invalid voltage-map structure: {e}", file=sys.stderr)
        return 1

    # Collapse each net's interval to its worst-case magnitude (the domain model
    # is magnitude-only, matching placement's load_voltage_map).
    voltage_map = {name: max(abs(iv.lo), abs(iv.hi)) for name, iv in intervals.items()}

    if not pcb_path.exists():
        print(f"Error: board file not found: {pcb_path}", file=sys.stderr)
        print(
            "  (netclass patterns + domain-bridging exemptions require the .kicad_pcb; "
            "pass --pcb to point at it explicitly).",
            file=sys.stderr,
        )
        return 1
    pcb = PCB.load(pcb_path)
    net_names = [n.name for n in pcb.nets.values() if n.number != 0 and n.name]

    # Resolve the DRU clearance floor: explicit flag wins, else the project's
    # own min_clearance, else a conservative default.
    dru_floor = getattr(args, "dru_floor", None)
    project_data = load_project(project_path)
    if dru_floor is None:
        dru_floor = _project_min_clearance(project_data)
    dru_floor = float(dru_floor)

    try:
        plan = build_export(
            voltage_map,
            net_names,
            pcb.footprints,
            standard_id=getattr(args, "standard", "iec60664") or "iec60664",
            pollution_degree=getattr(args, "pollution_degree", 2) or 2,
            material_group=getattr(args, "material_group", "IIIa") or "IIIa",
            hv_threshold=getattr(args, "hv_threshold", 30.0),
            dru_floor_mm=dru_floor,
        )
    except StandardLookupError as e:
        # Safety-critical: fail LOUD, never emit a guessed number.
        print(f"Error: standard-table lookup failed: {e}", file=sys.stderr)
        return 1

    if plan.is_empty:
        print("No mapped nets matched the board -- nothing to export.  No files written.")
        return 0

    dru_body = render_dru_block_body(plan)
    dru_block = merge_dru_block(
        dru_path.read_text() if dru_path.exists() else None,
        dru_body,
    )

    _print_summary(plan, project_path, pcb_path, dru_path)

    if getattr(args, "dry_run", False):
        print("\n--- .kicad_dru block (dry run, not written) ---")
        print(dru_body)
        return 0

    apply_netclass_assignments(project_data, plan)
    save_project(project_data, project_path)
    dru_path.write_text(dru_block)
    print(f"\nWrote {len(plan.domain_voltages)} netclass(es) -> {project_path.name}")
    print(f"Wrote {len(plan.rules)} pairwise rule(s) -> {dru_path.name}")
    return 0


def _project_min_clearance(project_data: dict) -> float:
    """Best-effort read of the project's board-wide minimum clearance (mm)."""
    try:
        rules = project_data.get("board", {}).get("design_settings", {}).get("rules", {})
        val = rules.get("min_clearance")
        if isinstance(val, (int, float)) and val > 0:
            return float(val)
    except (AttributeError, TypeError):
        pass
    return 0.2


def _print_summary(plan, project_path: Path, pcb_path: Path, dru_path: Path) -> None:
    print("kct creepage-export-rules")
    print(f"  Project: {project_path}")
    print(f"  Board:   {pcb_path}")
    print(f"  DRU:     {dru_path}")
    print(f"  DRU clearance floor: {plan.dru_floor_mm:g} mm")
    print(
        f"  Voltage domains ({len(plan.domain_voltages)}): "
        + ", ".join(f"{d}={plan.domain_voltages[d]:g}V" for d in sorted(plan.domain_voltages))
    )
    print(f"  Nets assigned: {len(plan.net_domains)}")
    if plan.rules:
        print(f"  Pairwise rules ({len(plan.rules)}):")
        for rule in plan.rules:
            print(f"    - {rule.name}: clearance >= {rule.min_mm:g} mm")
    else:
        print("  Pairwise rules: none (no domain pair exceeds the DRU floor)")
    if plan.bridging_by_pair:
        for (a, b), refs in sorted(plan.bridging_by_pair.items()):
            print(f"  Attach-zone exemption {a}<->{b}: {', '.join(refs)}")
