#!/usr/bin/env python3
"""
Tidy (autoplace) Reference/Value fields on a KiCad schematic.

Resets the positions of visible ``Reference`` and ``Value`` fields to
deterministic offsets relative to each symbol's placed body bounding box:
Reference centered above the body, Value centered below, both grid-aligned
and horizontal. This is a headless, diff-reviewable stand-in for Eeschema's
"Autoplace Fields" -- byte-parity with Eeschema's algorithm (side selection
by pin density, collision avoidance) is an explicit non-goal.

The operation is strictly cosmetic: only the ``(at x y angle)`` of
Reference/Value ``property`` nodes changes. Symbol positions, pins, wires,
all other properties, and all other file content are untouched, so the
netlist, ERC, BOM, and CPL are provably unchanged.

Usage:
    kct sch tidy sheet.kicad_sch
    kct sch tidy sheet.kicad_sch --dry-run
    kct sch tidy sheet.kicad_sch --threshold 15
    kct sch tidy sheet.kicad_sch --refs C13,C15
    kct sch tidy sheet.kicad_sch --backup --format json

Options:
    --threshold <mm>       Only touch fields farther than this from the body
                           bbox (default 0 = tidy all in-scope fields)
    --refs R1,C13,...      Scope to the listed reference designators
    --dry-run              Show before/after offsets without modifying
    --backup               Create backup before modifying
    --format {text,json}   Output format (default: text)

Notes:
    - Power/virtual symbols (Reference starting with '#') are skipped unless
      explicitly named via --refs.
    - Hidden fields are never moved.
    - Symbols whose lib_id is not in the schematic's embedded lib_symbols
      are skipped with a warning.
    - Multi-unit symbols: each placed unit's body bbox is estimated from
      that unit's pin extents (per-unit body graphics are not tracked), so
      placement can be coarser than for single-unit symbols. Use --refs to
      scope around any unit that needs hand placement.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime
from pathlib import Path

from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.schema import Schematic
from kicad_tools.schema.field_geometry import (
    default_field_positions,
    field_offset_mm,
    placed_body_bbox,
)
from kicad_tools.schema.symbol import SymbolInstance
from kicad_tools.sexp import SExp

#: Fields managed by tidy.
TIDY_FIELDS = ("Reference", "Value")

_POSITION_EPSILON = 1e-4


@dataclass
class FieldChange:
    """A planned (or applied) position change for one field."""

    field: str
    old_position: tuple[float, float]
    new_position: tuple[float, float]
    old_angle: float
    new_angle: float
    old_offset: float
    new_offset: float
    prop_sexp: SExp | None = dc_field(repr=False, compare=False, default=None)

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "old_position": [round(self.old_position[0], 4), round(self.old_position[1], 4)],
            "new_position": [round(self.new_position[0], 4), round(self.new_position[1], 4)],
            "old_offset": round(self.old_offset, 3),
            "new_offset": round(self.new_offset, 3),
        }


@dataclass
class SymbolTidyPlan:
    """Planned field changes for one placed symbol (unit)."""

    reference: str
    lib_id: str
    unit: int = 1
    changes: list[FieldChange] = dc_field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "reference": self.reference,
            "lib_id": self.lib_id,
            "unit": self.unit,
            "changes": [c.to_dict() for c in self.changes],
        }


@dataclass
class TidyResult:
    """Result of a tidy plan or apply."""

    symbols: list[SymbolTidyPlan] = dc_field(default_factory=list)
    warnings: list[str] = dc_field(default_factory=list)

    @property
    def fields_changed(self) -> int:
        return sum(len(s.changes) for s in self.symbols)


def _iter_placed_symbols(schematic: Schematic) -> Iterator[tuple[SymbolInstance, SExp]]:
    """Yield (parsed instance, raw sexp node) for each placed symbol.

    Only direct children of the root are placed symbols; the embedded
    ``lib_symbols`` definitions are nested one level deeper and are not
    yielded.
    """
    for sym_sexp in schematic.sexp.find_children("symbol"):
        yield SymbolInstance.from_sexp(sym_sexp), sym_sexp


def _find_property_node(sym_sexp: SExp, name: str) -> SExp | None:
    """Find a direct-child property node by field name."""
    for prop in sym_sexp.find_children("property"):
        if prop.get_string(0) == name:
            return prop
    return None


def _is_hidden(prop_sexp: SExp) -> bool:
    """Check whether a property is hidden.

    Handles both the KiCad 8+ ``(effects ... (hide yes))`` list form and the
    older bare-atom ``(effects ... hide)`` form.
    """
    effects = prop_sexp.find_child("effects")
    if effects is None:
        return False
    if effects.find_child("hide") is not None:
        return True
    return any(c.is_atom and c.value == "hide" for c in effects.children)


def plan_tidy(
    schematic: Schematic,
    threshold: float = 0.0,
    refs: list[str] | None = None,
) -> TidyResult:
    """Compute the field position changes tidy would make.

    Pure planning -- does not modify the schematic.

    Args:
        schematic: The loaded schematic.
        threshold: Only plan changes for fields whose current distance from
            the body bbox exceeds this many mm (0 = all in-scope fields).
        refs: If given, restrict to these reference designators. Explicitly
            listed references are tidied even if they are power/virtual
            symbols (``#``-prefixed), which are otherwise skipped.
    """
    result = TidyResult()
    refs_set = set(refs) if refs else None

    for inst, sym_sexp in _iter_placed_symbols(schematic):
        reference = inst.reference

        if refs_set is not None:
            if reference not in refs_set:
                continue
        elif reference.startswith("#"):
            # Power/virtual symbols: skip unless explicitly named.
            continue

        lib_sym = schematic.get_lib_symbol_resolved(inst.lib_id)
        if lib_sym is None:
            result.warnings.append(
                f"{reference}: lib_id '{inst.lib_id}' not found in embedded lib_symbols; skipped"
            )
            continue

        bbox = placed_body_bbox(
            lib_sym,
            inst.position,
            rotation=inst.rotation,
            mirror=inst.mirror,
            unit=inst.unit,
        )
        defaults = default_field_positions(bbox)

        plan = SymbolTidyPlan(reference=reference, lib_id=inst.lib_id, unit=inst.unit)
        for field_name in TIDY_FIELDS:
            prop = _find_property_node(sym_sexp, field_name)
            if prop is None or _is_hidden(prop):
                continue
            at_node = prop.find_child("at")
            if at_node is None:
                continue

            old_x = at_node.get_float(0) or 0.0
            old_y = at_node.get_float(1) or 0.0
            old_angle = at_node.get_float(2) or 0.0
            old_offset = field_offset_mm((old_x, old_y), bbox)

            if threshold > 0 and old_offset <= threshold:
                continue

            new_x, new_y, new_angle = defaults[field_name]
            if (
                math.isclose(new_x, old_x, abs_tol=_POSITION_EPSILON)
                and math.isclose(new_y, old_y, abs_tol=_POSITION_EPSILON)
                and math.isclose(new_angle, old_angle, abs_tol=_POSITION_EPSILON)
            ):
                continue  # Already in place -- keep the file byte-identical.

            plan.changes.append(
                FieldChange(
                    field=field_name,
                    old_position=(old_x, old_y),
                    new_position=(new_x, new_y),
                    old_angle=old_angle,
                    new_angle=new_angle,
                    old_offset=old_offset,
                    new_offset=field_offset_mm((new_x, new_y), bbox),
                    prop_sexp=prop,
                )
            )

        if plan.changes:
            result.symbols.append(plan)

    return result


def tidy_fields(
    schematic: Schematic,
    threshold: float = 0.0,
    refs: list[str] | None = None,
) -> TidyResult:
    """Apply bbox-relative default positions to Reference/Value fields.

    Only the ``(at x y angle)`` of the affected property nodes is modified.
    Returns the applied :class:`TidyResult`.
    """
    result = plan_tidy(schematic, threshold=threshold, refs=refs)

    for plan in result.symbols:
        for change in plan.changes:
            if change.prop_sexp is None:  # pragma: no cover - set during planning
                continue
            at_node = change.prop_sexp.find_child("at")
            if at_node is None:  # pragma: no cover - guarded during planning
                continue
            at_node.set_value(0, change.new_position[0])
            at_node.set_value(1, change.new_position[1])
            at_node.set_value(2, 0)

    if result.fields_changed:
        schematic.invalidate_cache()

    return result


def _result_to_dict(result: TidyResult, dry_run: bool) -> dict:
    return {
        "dry_run": dry_run,
        "modified": (not dry_run) and result.fields_changed > 0,
        "symbols_changed": len(result.symbols),
        "fields_changed": result.fields_changed,
        "symbols": [s.to_dict() for s in result.symbols],
        "warnings": result.warnings,
    }


def _print_text(result: TidyResult, dry_run: bool) -> None:
    if dry_run:
        print("DRY RUN - No changes will be made")
        print("=" * 60)

    for plan in result.symbols:
        unit_note = f" unit {plan.unit}" if plan.unit != 1 else ""
        print(f"{plan.reference}{unit_note} ({plan.lib_id}):")
        for c in plan.changes:
            print(
                f"  {c.field}: ({c.old_position[0]:.2f}, {c.old_position[1]:.2f})"
                f" [{c.old_offset:.1f} mm]"
                f" -> ({c.new_position[0]:.2f}, {c.new_position[1]:.2f})"
                f" [{c.new_offset:.1f} mm]"
            )

    for w in result.warnings:
        print(f"Warning: {w}", file=sys.stderr)

    verb = "would change" if dry_run else "changed"
    print(f"{len(result.symbols)} symbol(s), {result.fields_changed} field(s) {verb}")


def run_tidy(args) -> int:
    """Execute the tidy command."""
    schematic_path = Path(args.schematic)

    try:
        sch = Schematic.load(schematic_path)
    except (FileNotFoundError, KiCadFileNotFoundError):
        print(f"Error: Schematic not found: {schematic_path}", file=sys.stderr)
        return 1

    refs: list[str] | None = None
    if args.refs:
        refs = [r.strip() for r in args.refs.split(",") if r.strip()]

    if args.dry_run:
        result = plan_tidy(sch, threshold=args.threshold, refs=refs)
        if args.format == "json":
            print(json.dumps(_result_to_dict(result, dry_run=True), indent=2))
        else:
            _print_text(result, dry_run=True)
        return 0

    if args.backup:
        backup_path = f"{schematic_path}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(schematic_path, backup_path)
        if args.format != "json":
            print(f"Backup created: {backup_path}")

    result = tidy_fields(sch, threshold=args.threshold, refs=refs)

    if result.fields_changed:
        sch.save()

    if args.format == "json":
        print(json.dumps(_result_to_dict(result, dry_run=False), indent=2))
    else:
        _print_text(result, dry_run=False)

    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Reset Reference/Value field positions to bbox-relative defaults "
            "(headless field autoplace; deterministic, not Eeschema-parity)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.0,
        metavar="MM",
        help=(
            "Only touch fields farther than this many mm from the symbol "
            "body bbox (default 0 = tidy all in-scope fields)"
        ),
    )
    parser.add_argument(
        "--refs",
        help="Comma-separated reference designators to tidy (e.g., C13,C15)",
    )
    parser.add_argument("--dry-run", "-n", action="store_true", help="Preview without modifying")
    parser.add_argument("--backup", action="store_true", help="Create backup before modifying")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format")

    args = parser.parse_args(argv)
    return run_tidy(args)


if __name__ == "__main__":
    sys.exit(main())
