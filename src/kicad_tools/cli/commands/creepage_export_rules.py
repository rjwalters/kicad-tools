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

Machine output (``--format json``, issue #4674): one document naming the
resolved ``project``/``pcb``/``dru`` paths, the derived voltage ``domains``,
the pairwise ``rules`` and the domain-bridging exemptions, plus ``written``
(false under ``--dry-run`` and for the two clean no-op paths, so exit 0 can
never be read as "files were written").  Failures emit
``{"error": ..., "success": false}`` with the exit code unchanged.  See
``docs/reference/machine-output.md``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ..format_options import FORMAT_JSON, emit_json


def _fail(as_json: bool, project: str, message: str, *, text_lines: list[str] | None = None) -> int:
    """Report an export failure as a document (JSON) or prose (text).

    ``text_lines`` overrides the conventional single ``Error: <message>`` line
    for the one failure that prints a follow-up hint, so text-mode output stays
    byte-identical.
    """
    if as_json:
        emit_json(
            {
                "command": "creepage-export-rules",
                "project": project,
                "error": message,
                "written": False,
                "success": False,
            }
        )
    else:
        for line in text_lines if text_lines is not None else [f"Error: {message}"]:
            print(line, file=sys.stderr)
    return 1


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

    as_json = getattr(args, "format", "text") == FORMAT_JSON

    project_path = Path(args.project)
    if not project_path.exists():
        return _fail(as_json, str(project_path), f"project file not found: {project_path}")
    if project_path.suffix != ".kicad_pro":
        return _fail(
            as_json,
            str(project_path),
            f"expected a .kicad_pro project file, got {project_path.name}",
        )

    # Resolve the sibling board (nets + footprints) and the .kicad_dru target.
    pcb_arg = getattr(args, "pcb", None)
    pcb_path = Path(pcb_arg) if pcb_arg else project_path.with_suffix(".kicad_pcb")
    dru_path = project_path.with_suffix(".kicad_dru")

    # No voltage map -> clean no-op (nothing to derive, nothing written).
    vmap_arg = getattr(args, "voltage_map", None)
    if not vmap_arg:
        message = (
            "No --voltage-map supplied: nothing to export "
            "(pairwise HV rules are derived from the voltage map).  No files written."
        )
        if as_json:
            emit_json(
                {
                    "command": "creepage-export-rules",
                    "project": str(project_path),
                    "pcb": str(pcb_path),
                    "dru": str(dru_path),
                    "voltage_map": None,
                    "domains": {},
                    "nets_assigned": 0,
                    "rules": [],
                    "bridging_exemptions": [],
                    "dry_run": bool(getattr(args, "dry_run", False)),
                    "written": False,
                    "skipped_reason": "no-voltage-map",
                    "success": True,
                }
            )
        else:
            print(message)
        return 0

    vmap_path = Path(vmap_arg)
    if not vmap_path.exists():
        return _fail(as_json, str(project_path), f"voltage-map file not found: {vmap_path}")
    try:
        intervals, _edge_voltage = voltage_map_from_dict(json.loads(vmap_path.read_text()))
    except json.JSONDecodeError as e:
        return _fail(as_json, str(project_path), f"parsing voltage-map JSON: {e}")
    except (TypeError, ValueError) as e:
        return _fail(as_json, str(project_path), f"invalid voltage-map structure: {e}")

    # Collapse each net's interval to its worst-case magnitude (the domain model
    # is magnitude-only, matching placement's load_voltage_map).
    voltage_map = {name: max(abs(iv.lo), abs(iv.hi)) for name, iv in intervals.items()}

    if not pcb_path.exists():
        return _fail(
            as_json,
            str(project_path),
            f"board file not found: {pcb_path}",
            text_lines=[
                f"Error: board file not found: {pcb_path}",
                "  (netclass patterns + domain-bridging exemptions require the .kicad_pcb; "
                "pass --pcb to point at it explicitly).",
            ],
        )
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
        return _fail(as_json, str(project_path), f"standard-table lookup failed: {e}")

    dry_run = bool(getattr(args, "dry_run", False))

    def _document(*, written: bool, skipped_reason: str | None, dru_body: str | None) -> dict:
        return {
            "command": "creepage-export-rules",
            "project": str(project_path),
            "pcb": str(pcb_path),
            "dru": str(dru_path),
            "voltage_map": str(vmap_path),
            "standard": getattr(args, "standard", "iec60664") or "iec60664",
            "pollution_degree": getattr(args, "pollution_degree", 2) or 2,
            "material_group": getattr(args, "material_group", "IIIa") or "IIIa",
            "hv_threshold_v": getattr(args, "hv_threshold", 30.0),
            "dru_floor_mm": plan.dru_floor_mm,
            "domains": dict(plan.domain_voltages),
            "nets_assigned": len(plan.net_domains),
            "net_domains": dict(plan.net_domains),
            "rules": [
                {"name": rule.name, "condition": rule.condition, "min_mm": rule.min_mm}
                for rule in plan.rules
            ],
            "bridging_exemptions": [
                {"domains": [a, b], "references": sorted(refs)}
                for (a, b), refs in sorted(plan.bridging_by_pair.items())
            ],
            # The rendered block is the artifact; it is carried only on the
            # dry-run path, mirroring the prose that prints it instead of
            # writing it.
            "dru_block": dru_body,
            "dry_run": dry_run,
            "written": written,
            "skipped_reason": skipped_reason,
            "success": True,
        }

    if plan.is_empty:
        if as_json:
            emit_json(_document(written=False, skipped_reason="no-mapped-nets", dru_body=None))
        else:
            print("No mapped nets matched the board -- nothing to export.  No files written.")
        return 0

    dru_body = render_dru_block_body(plan)
    dru_block = merge_dru_block(
        dru_path.read_text() if dru_path.exists() else None,
        dru_body,
    )

    if not as_json:
        _print_summary(plan, project_path, pcb_path, dru_path)

    if dry_run:
        if as_json:
            emit_json(_document(written=False, skipped_reason="dry-run", dru_body=dru_body))
        else:
            print("\n--- .kicad_dru block (dry run, not written) ---")
            print(dru_body)
        return 0

    apply_netclass_assignments(project_data, plan)
    save_project(project_data, project_path)
    dru_path.write_text(dru_block)
    if as_json:
        emit_json(_document(written=True, skipped_reason=None, dru_body=None))
    else:
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
