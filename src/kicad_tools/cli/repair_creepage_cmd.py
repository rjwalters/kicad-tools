"""``kct repair-creepage`` -- translate wide HV copper to clear a creepage shortfall (Issue #8545).

Consumes the ``kct creepage`` census (run in-process with the same flags) and
nudges the movable copper (track segments, then vias) on one side of each
failing, unwaived pair along the census witness normal.  Widths and joints are
kept; a move is kept only when the pair passes, no other pair gets worse and
the kicad-cli ``--refill-zones`` DRC does not regress.  See
:mod:`kicad_tools.creepage.repair`.

Exit codes: 0 = every failing pair repaired (or none failed); 1 = pairs remain
unrepaired (listed); 2 = usage / input error.

.. warning::
   An engineering aid, NOT a certification.
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

EXIT_USAGE = 2


def add_repair_creepage_args(parser) -> None:
    """Register ``repair-creepage`` arguments (mirrors ``kct creepage``)."""
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument("--net-class", dest="net_class", default="HV")
    parser.add_argument("--net-class-map", dest="net_class_map", default=None)
    parser.add_argument("--voltage-map", dest="voltage_map", default=None)
    parser.add_argument("--min", type=float, default=None, help="Manual required creepage (mm)")
    parser.add_argument("--standard", choices=["iec60664", "iec62368"], default=None)
    parser.add_argument("--pollution-degree", dest="pollution_degree", type=int, choices=[1, 2, 3])
    parser.add_argument("--working-voltage", dest="working_voltage", type=float, default=None)
    parser.add_argument(
        "--overvoltage-category",
        dest="overvoltage_category",
        choices=["I", "II", "III", "IV"],
        default="II",
    )
    parser.add_argument(
        "--insulation", choices=["basic", "supplementary", "reinforced"], default="basic"
    )
    parser.add_argument(
        "--material-group",
        dest="material_group",
        choices=["I", "II", "IIIa", "IIIb"],
        default="IIIa",
    )
    parser.add_argument("--census-threshold", dest="census_threshold", type=float, default=30.0)
    parser.add_argument(
        "--waive-same-footprint",
        dest="waive_same_footprint",
        action="store_true",
        help="Do not move same-footprint pairs",
    )
    parser.add_argument(
        "--waive-attach-zone",
        dest="waive_attach_zone",
        action="store_true",
        help="Do not move waived attach-zone pairs",
    )
    parser.add_argument(
        "--from-report",
        dest="from_report",
        default=None,
        help="kct creepage --format json file selecting the failing pairs to repair",
    )
    parser.add_argument(
        "--max-displacement",
        dest="max_displacement",
        type=float,
        default=0.5,
        help="Largest move per pair in mm (default 0.5)",
    )
    parser.add_argument(
        "--margin",
        type=float,
        default=0.05,
        help="Extra distance beyond the shortfall in mm (default 0.05)",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="List proposed moves with witness coordinates; write nothing",
    )
    parser.add_argument(
        "-o", "--output", default=None, help="Output .kicad_pcb (default: overwrite the input)"
    )
    parser.add_argument("--format", choices=["text", "json"], default="text")


def build_census_fn(args) -> Callable[[Any], Any]:
    """Census callable honoring ``kct creepage``'s threshold / waiver flags.

    Raises ``ValueError`` with a user-facing message on invalid input.
    """
    from kicad_tools.creepage.engine import (
        compute_creepage_census,
        pair_bounds_from_dict,
        resolve_hv_nets,
        voltage_map_from_dict,
    )
    from kicad_tools.creepage.standards import (
        DEFAULT_INSULATION,
        StandardLookupError,
        get_standard,
    )

    pcb_path = Path(args.pcb)
    min_mm = float(args.min) if args.min is not None else None
    standard_id = args.standard
    if standard_id is None and min_mm is None:
        raise ValueError("provide either --standard (with --pollution-degree) or --min")

    voltage_map = None
    pair_bounds: dict = {}
    edge_voltage = 0.0
    if args.voltage_map:
        if standard_id is None:
            raise ValueError("--voltage-map requires --standard (and --pollution-degree)")
        raw = json.loads(Path(args.voltage_map).read_text())
        voltage_map, edge_voltage = voltage_map_from_dict(raw)
        pair_bounds = pair_bounds_from_dict(raw)

    std = None
    required_creepage = required_clearance = None
    creep_prov = clear_prov = None
    edition = None
    insulation = args.insulation or DEFAULT_INSULATION
    if standard_id is not None:
        if args.pollution_degree is None:
            raise ValueError("--standard requires --pollution-degree")
        if voltage_map is None and args.working_voltage is None:
            raise ValueError("--standard requires --working-voltage (or --voltage-map)")
        try:
            std = get_standard(standard_id)
            edition = std.edition
            if voltage_map is None:
                wv, pd = float(args.working_voltage), int(args.pollution_degree)
                required_creepage, creep_prov = std.required_creepage(
                    wv, pd, args.material_group, insulation
                )
                required_clearance, clear_prov = std.required_clearance(
                    wv, pd, args.overvoltage_category, insulation
                )
        except StandardLookupError as e:
            raise ValueError(f"standard-table lookup failed: {e}") from e

    net_class_map = None
    if args.net_class_map:
        from kicad_tools.router.rules import net_class_map_from_path

        net_class_map = net_class_map_from_path(Path(args.net_class_map), pcb_path=pcb_path)

    def census(pcb):
        hv_nets = resolve_hv_nets(
            pcb,
            args.net_class,
            net_class_map,
            voltage_map=voltage_map,
            edge_voltage=edge_voltage,
            census_threshold=args.census_threshold if voltage_map is not None else None,
        )
        attach_zones = None
        if args.waive_attach_zone:
            from kicad_tools.router.pairwise_clearance import build_attach_zones

            attach_zones = build_attach_zones(pcb.footprints)
        try:
            report = compute_creepage_census(
                pcb,
                hv_nets,
                min_mm,
                net_class=args.net_class,
                board=str(pcb_path),
                required_creepage_mm=required_creepage,
                required_clearance_mm=required_clearance,
                standard=standard_id,
                standard_edition=edition,
                working_voltage=None if voltage_map is not None else args.working_voltage,
                pollution_degree=args.pollution_degree,
                material_group=args.material_group if standard_id is not None else None,
                creepage_provenance=creep_prov,
                clearance_provenance=clear_prov,
                voltage_map=voltage_map,
                standard_obj=std if voltage_map is not None else None,
                edge_voltage=edge_voltage,
                overvoltage_category=args.overvoltage_category,
                insulation=insulation,
                attach_zones=attach_zones,
                pair_bounds=pair_bounds or None,
            )
        except StandardLookupError as e:
            raise ValueError(f"standard-table lookup failed: {e}") from e
        if args.waive_same_footprint:
            for p in report.pairs:
                if p.relationship == "same_footprint":
                    p.waived = True
        for p in report.pairs:
            if p.relationship == "attach_zone" and p.residual_passed:
                p.waived = True
        return report

    return census


def build_drc_fn(pcb_path: Path) -> Callable[[str], int | None] | None:
    """DRC error counter running ``kicad-cli pcb drc --refill-zones``, or ``None``."""
    from kicad_tools.cli.runner import find_kicad_cli, run_drc
    from kicad_tools.drc.report import DRCReport

    if not find_kicad_cli():
        return None

    def count(text: str) -> int | None:
        with tempfile.TemporaryDirectory(prefix="kct-repair-creepage-") as tmp:
            board = Path(tmp) / pcb_path.name
            board.write_text(text, encoding="utf-8")
            res = run_drc(board, schematic_parity=False, refill_zones=True)
            if not res.success or res.output_path is None:
                return None
            try:
                return DRCReport.load(res.output_path).error_count
            except Exception:
                return None
            finally:
                Path(res.output_path).unlink(missing_ok=True)

    return count


def run_repair_creepage_command(args, *, drc_fn=...) -> int:
    """Handle ``repair-creepage``.  ``drc_fn`` is injectable for tests."""
    from kicad_tools._shapely import has_shapely
    from kicad_tools.creepage.repair import CreepageRepairer, load_report_rows

    if not has_shapely():
        print("Error: creepage analysis requires shapely.", file=sys.stderr)
        return EXIT_USAGE
    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: PCB file not found: {pcb_path}", file=sys.stderr)
        return EXIT_USAGE
    try:
        census_fn = build_census_fn(args)
        only = load_report_rows(args.from_report) if args.from_report else None
    except (ValueError, OSError, json.JSONDecodeError, TypeError, KeyError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return EXIT_USAGE

    if drc_fn is ...:
        drc_fn = build_drc_fn(pcb_path)

    repairer = CreepageRepairer(pcb_path)
    result = repairer.repair(
        census_fn,
        only=only,
        margin=args.margin,
        max_displacement=args.max_displacement,
        dry_run=args.dry_run,
        drc_fn=drc_fn,
    )

    if not args.dry_run and result.moves:
        out = Path(args.output) if args.output else pcb_path
        repairer.save(out)
        result.notes.append(f"wrote {out}")
    elif args.output and not args.dry_run:
        # Nothing moved: still honor -o so pipelines always find the file.
        repairer.save(Path(args.output))

    if args.format == "json":
        print(json.dumps(result.to_dict(), indent=2))
    else:
        _print_text(result)
    return 0 if result.success else 1


def _print_text(result) -> None:
    verb = "Proposed" if result.dry_run else "Applied"
    print(
        f"Creepage repair: {result.failing_before} failing pair(s) before, "
        f"{result.failing_after} after."
    )
    for m in result.moves:
        print(f"  {verb}: {m}")
    for r in result.rejected:
        print(f"  Rolled back: {r}")
    for u in result.unrepaired:
        print(f"  UNREPAIRED: {u}")
    for n in result.notes:
        print(f"  Note: {n}")
    if not result.success and result.moves and not result.dry_run:
        print(
            f"PARTIALLY REPAIRED: {len(result.moves)} move(s) were kept and written, but "
            f"{len(result.unrepaired)} pair(s) could not be repaired."
        )
    if result.success:
        print("OK" if result.moves or not result.failing_before else "OK (nothing to repair)")
    else:
        print(f"FAIL: {len(result.unrepaired)} pair(s) could not be repaired.")
