"""``kct creepage`` command handler (Issue #4327 phase 1, #4332 phase 2).

Measures per-pair surface-path (creepage) distance between an HV net group
and every other conductor / board edge, honoring milled Edge.Cuts slots, and
reports a **census** (all pairs + margin, not just violations) with clearance
(through-air) and creepage (over-surface) reported as distinct values.

The required threshold comes from one of two sources:

* ``--min`` -- the operator supplies the required creepage directly (phase 1).
* ``--standard`` -- the required creepage AND clearance are *derived* from an
  IEC 60664-1 / 62368-1 table for a ``(working voltage, pollution degree,
  material group)`` triple (phase 2, #4332).

When both are supplied the stricter (larger) creepage bound governs per pair.
Exit is non-zero iff any pair fails its governing creepage bound or (in
standard mode) its derived clearance bound.

.. warning::
   The derived values are an engineering aid, NOT a certification.  The
   governing standard and a qualified engineer are authoritative.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kicad_tools.creepage.engine import CreepageReport

# Distinct exit code for "could not classify any HV net on a board that looks
# like mains" (issue #4354).  Kept separate from 1 ("a measured pair failed its
# bound") so CI / agent gates can tell an unclassifiable-HV vacuity from a real
# creepage failure.
EXIT_HV_UNCLASSIFIED = 2


def run_creepage_command(args) -> int:
    """Handle the ``creepage`` command.  Returns the process exit code."""
    from kicad_tools._shapely import has_shapely
    from kicad_tools.creepage.engine import (
        SELV_WORKING_VOLTAGE_V,
        compute_creepage_census,
        mains_suspect_nets,
        resolve_hv_nets,
        voltage_map_from_dict,
    )
    from kicad_tools.creepage.standards import (
        RMS_TO_PEAK,
        StandardLookupError,
        get_standard,
    )
    from kicad_tools.schema.pcb import PCB

    fmt = getattr(args, "format", "table") or "table"

    if not has_shapely():
        # Consistent with segment_copper_polygon returning None: fail loud.
        print(
            "Error: creepage analysis requires shapely (a core dependency); "
            "it is not importable in this environment.",
            file=sys.stderr,
        )
        return 1

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: PCB file not found: {pcb_path}", file=sys.stderr)
        return 1

    # --- Threshold-source resolution (phase 1 --min vs phase 2 --standard) ---
    min_arg = getattr(args, "min", None)
    min_mm = float(min_arg) if min_arg is not None else None
    standard_id = getattr(args, "standard", None)

    if standard_id is None and min_mm is None:
        print(
            "Error: provide either --standard (with --working-voltage and "
            "--pollution-degree) or --min.",
            file=sys.stderr,
        )
        return 1

    # --- Optional per-net voltage map (#4371) --------------------------------
    # When supplied, the required creepage/clearance is derived PER PAIR from
    # |V_a - V_b| instead of a single global --working-voltage.  It requires a
    # --standard (the table is the only source of a derived requirement) but
    # makes --working-voltage optional (unmapped nets default to 0 V).
    voltage_map: dict | None = None
    edge_voltage = 0.0
    vmap_arg = getattr(args, "voltage_map", None)
    if vmap_arg:
        if standard_id is None:
            print(
                "Error: --voltage-map requires --standard (and --pollution-degree); "
                "the per-pair requirement is derived from an IEC table.",
                file=sys.stderr,
            )
            return 1
        vmap_path = Path(vmap_arg)
        if not vmap_path.exists():
            print(f"Error: voltage-map file not found: {vmap_path}", file=sys.stderr)
            return 1
        try:
            voltage_map, edge_voltage = voltage_map_from_dict(json.loads(vmap_path.read_text()))
        except json.JSONDecodeError as e:
            print(f"Error: parsing voltage-map JSON: {e}", file=sys.stderr)
            return 1
        except (TypeError, ValueError) as e:
            print(f"Error: invalid voltage-map structure: {e}", file=sys.stderr)
            return 1

    # Phase-2 derived requirements (None in phase-1 mode).
    required_creepage_mm: float | None = None
    required_clearance_mm: float | None = None
    creepage_prov: dict | None = None
    clearance_prov: dict | None = None
    standard_edition: str | None = None
    std = None
    working_voltage = getattr(args, "working_voltage", None)
    pollution_degree = getattr(args, "pollution_degree", None)
    material_group = getattr(args, "material_group", "IIIa")

    if standard_id is not None:
        if voltage_map is None:
            # Single-voltage mode: preserve the exact phase-2 requirement/message.
            if working_voltage is None or pollution_degree is None:
                print(
                    "Error: --standard requires both --working-voltage and --pollution-degree.",
                    file=sys.stderr,
                )
                return 1
        else:
            # Per-net voltage mode (#4371): --working-voltage is optional (each
            # pair keys on its own |ΔV|), but --pollution-degree is still needed.
            if pollution_degree is None:
                print(
                    "Error: --standard (with --voltage-map) requires --pollution-degree.",
                    file=sys.stderr,
                )
                return 1
        try:
            std = get_standard(standard_id)
            standard_edition = std.edition
            if voltage_map is None:
                # Single-voltage mode: derive the one global requirement up front.
                # (working_voltage / pollution_degree were validated non-None above.)
                assert working_voltage is not None and pollution_degree is not None
                required_creepage_mm, creepage_prov = std.required_creepage(
                    float(working_voltage), int(pollution_degree), material_group
                )
                peak_voltage = float(working_voltage) * RMS_TO_PEAK
                required_clearance_mm, clearance_prov = std.required_clearance(
                    peak_voltage, int(pollution_degree)
                )
        except StandardLookupError as e:
            # Safety-critical: fail LOUD, never emit a guessed number.
            print(f"Error: standard-table lookup failed: {e}", file=sys.stderr)
            return 1

    # Load the optional net-class map sidecar (reuses net_class_map_from_dict).
    net_class_map = None
    ncm_arg = getattr(args, "net_class_map", None)
    if ncm_arg:
        from kicad_tools.router.rules import net_class_map_from_dict

        ncm_path = Path(ncm_arg)
        if not ncm_path.exists():
            print(f"Error: net-class-map file not found: {ncm_path}", file=sys.stderr)
            return 1
        try:
            net_class_map = net_class_map_from_dict(json.loads(ncm_path.read_text()))
        except json.JSONDecodeError as e:
            print(f"Error: parsing net-class-map JSON: {e}", file=sys.stderr)
            return 1
        except (TypeError, ValueError) as e:
            print(f"Error: invalid net-class-map structure: {e}", file=sys.stderr)
            return 1

    pcb = PCB.load(pcb_path)
    net_class = getattr(args, "net_class", "HV") or "HV"

    hv_nets = resolve_hv_nets(pcb, net_class, net_class_map)
    try:
        report = compute_creepage_census(
            pcb,
            hv_nets,
            min_mm,
            net_class=net_class,
            board=str(pcb_path),
            # In map mode the requirement varies per pair, so the report-level
            # scalar requirement / working voltage are null (per-pair).
            required_creepage_mm=required_creepage_mm,
            required_clearance_mm=required_clearance_mm,
            standard=standard_id,
            standard_edition=standard_edition,
            working_voltage=None if voltage_map is not None else working_voltage,
            pollution_degree=pollution_degree,
            material_group=material_group if standard_id is not None else None,
            creepage_provenance=creepage_prov,
            clearance_provenance=clearance_prov,
            voltage_map=voltage_map,
            standard_obj=std if voltage_map is not None else None,
            edge_voltage=edge_voltage,
        )
    except StandardLookupError as e:
        # A per-pair |ΔV| out of the table's range (map mode): fail LOUD, never
        # silently pass a safety-critical audit.
        print(f"Error: standard-table lookup failed: {e}", file=sys.stderr)
        return 1

    # Vacuity guard (issue #4354): a census that resolves ZERO HV nets is only
    # legitimately "nothing to audit" on a genuinely low-voltage board.  When
    # the board carries mains-named copper OR a mains-level working voltage was
    # supplied, an empty HV group means the HV path was never evaluated -- a
    # safety-gate FALSE PASS.  Fire loud and exit non-zero (distinct code) so
    # this is never mistaken for a clean board.
    mains_suspects: list[str] = []
    guard_triggered = False
    if not report.has_hv_nets:
        mains_suspects = mains_suspect_nets(pcb)
        high_working_voltage = (
            working_voltage is not None and float(working_voltage) >= SELV_WORKING_VOLTAGE_V
        )
        guard_triggered = bool(mains_suspects) or high_working_voltage

    if fmt == "json":
        print(json.dumps(report.to_dict(), indent=2))
    elif report.uses_standard:
        _render_table_standard(report, guard_triggered=guard_triggered)
    else:
        _render_table(report, guard_triggered=guard_triggered)

    if guard_triggered:
        _warn_hv_unclassified(report, mains_suspects, working_voltage)
        return EXIT_HV_UNCLASSIFIED

    # Non-zero exit iff any pair fails its governing bound(s).  An empty census
    # on a genuinely low-voltage board (no mains names, no HV working voltage)
    # is a clean exit 0.
    return 0 if report.passed else 1


def _warn_hv_unclassified(
    report: CreepageReport,
    mains_suspects: list[str],
    working_voltage: float | None,
) -> None:
    """Loud stderr warning when the HV group is empty on a mains-looking board."""
    from kicad_tools.creepage.engine import SELV_WORKING_VOLTAGE_V

    print(
        "WARNING: creepage resolved 0 HV nets, but this board looks like a "
        "mains/HV design -- the HV insulation path was NOT evaluated.",
        file=sys.stderr,
    )
    if mains_suspects:
        shown = ", ".join(mains_suspects[:12])
        more = "" if len(mains_suspects) <= 12 else f" (+{len(mains_suspects) - 12} more)"
        print(f"  Mains/HV-suspect nets on the board: {shown}{more}", file=sys.stderr)
    if working_voltage is not None and float(working_voltage) >= SELV_WORKING_VOLTAGE_V:
        print(
            f"  Working voltage {float(working_voltage):g} V is at/above the "
            f"{SELV_WORKING_VOLTAGE_V:g} V SELV boundary -- an HV path is implied.",
            file=sys.stderr,
        )
    print(
        f"  None of these nets were classified as '{report.net_class}'.  Supply "
        "--net-class-map (mapping the mains nets to the HV class) or --net-class "
        "so the census can actually audit them.",
        file=sys.stderr,
    )
    print(
        f"  Exiting {EXIT_HV_UNCLASSIFIED} (HV-unclassified) rather than 0 to "
        "avoid a safety-gate false pass.",
        file=sys.stderr,
    )


def _render_table(report: CreepageReport, *, guard_triggered: bool = False) -> None:
    """Print the human-readable census table (phase-1 / manual --min mode)."""
    if not report.has_hv_nets:
        print(
            f"No '{report.net_class}' nets found "
            "(supply --net-class-map to classify HV nets, or check --net-class)."
        )
        print(f"Board: {report.board}")
        if guard_triggered:
            # #4354: mains-looking board with an empty HV group -- do NOT claim
            # a clean exit; the loud WARNING + non-zero exit follow.
            print(
                "Census: 0 pairs, but the board looks like mains/HV -- "
                "HV classification FAILED (see WARNING below)."
            )
        else:
            print("Census: 0 pairs.  Nothing to audit -- exit 0.")
        return

    print(f"HV creepage/clearance census  (net-class '{report.net_class}')")
    print(f"Board: {report.board}")
    print(f"Required minimum creepage (--min): {report.min_mm:.3f} mm")
    print(f"HV nets ({len(report.hv_nets)}): {', '.join(report.hv_nets)}")
    print()

    if not report.pairs:
        print("Census: 0 pairs (HV nets present but no other conductors / edge found).")
        return

    header = (
        f"{'HV net':<16} {'Against':<16} {'Layer':<7} "
        f"{'Clearance':>10} {'Creepage':>10} {'Margin':>10} {'Result':>7}"
    )
    print(header)
    print("-" * len(header))
    for p in report.pairs:
        result = "PASS" if p.passed else "FAIL"
        print(
            f"{p.net_a:<16} {p.net_b:<16} {p.layer:<7} "
            f"{p.clearance_mm:>9.3f}  {p.creepage_mm:>9.3f}  "
            f"{p.margin_mm:>9.3f}  {result:>7}"
        )

    print()
    failures = [p for p in report.pairs if not p.passed]
    total = len(report.pairs)
    if failures:
        print(f"FAIL: {len(failures)}/{total} pair(s) below {report.min_mm:.3f} mm creepage.")
    else:
        print(f"PASS: all {total} pair(s) clear {report.min_mm:.3f} mm creepage.")


def _render_table_standard(report: CreepageReport, *, guard_triggered: bool = False) -> None:
    """Print the census table with IEC-derived requirements (phase-2 mode)."""
    from kicad_tools.creepage.standards import DISCLAIMER

    if not report.has_hv_nets:
        print(
            f"No '{report.net_class}' nets found "
            "(supply --net-class-map to classify HV nets, or check --net-class)."
        )
        print(f"Board: {report.board}")
        if guard_triggered:
            # #4354: mains-looking board with an empty HV group -- do NOT claim
            # a clean exit; the loud WARNING + non-zero exit follow.
            print(
                "Census: 0 pairs, but the board looks like mains/HV -- "
                "HV classification FAILED (see WARNING below)."
            )
        else:
            print("Census: 0 pairs.  Nothing to audit -- exit 0.")
        return

    cp = report.creepage_provenance or {}
    clp = report.clearance_provenance or {}
    std_name = cp.get("standard", report.standard)
    print(f"HV creepage/clearance census  (net-class '{report.net_class}')")
    print(f"Board: {report.board}")
    print(f"Standard: {std_name} {report.standard_edition}  (--standard {report.standard})")
    if report.uses_voltage_map:
        # Per-net voltage mode (#4371): the requirement is derived per pair from
        # |ΔV|, so there is no single working voltage / derived requirement.
        n_mapped = len(report.voltage_map or {})
        print(
            f"Voltage source: per-pair |dV| (voltage-map, {n_mapped} net(s) mapped, "
            f"edge/earth = {report.edge_voltage:g} V)  |  "
            f"Pollution degree: {report.pollution_degree}  |  "
            f"Material group: {report.material_group}"
        )
        print(
            "Derived requirement: per pair (see the ReqCr / ReqCl columns; nets at "
            "equal potential require ~0)."
        )
    else:
        print(
            f"Working voltage: {report.working_voltage:g} V RMS  "
            f"(peak ~ {clp.get('peak_voltage_v', 0.0):.0f} V)  |  "
            f"Pollution degree: {report.pollution_degree}  |  "
            f"Material group: {report.material_group}"
        )
        if report.required_creepage_mm is not None:
            print(
                f"Derived required creepage: {report.required_creepage_mm:.3f} mm  "
                f"[{cp.get('table_id', '?')}, {cp.get('voltage_row_used_v', '?')} V row, "
                f"step-up]"
            )
        if report.required_clearance_mm is not None:
            print(
                f"Derived required clearance: {report.required_clearance_mm:.3f} mm  "
                f"[{clp.get('table_id', '?')}, {clp.get('governing_component', '?')}, "
                f"altitude {clp.get('altitude_assumption', '?')}]"
            )
    if report.min_mm is not None:
        print(f"Manual override (--min): {report.min_mm:.3f} mm  (stricter governs)")
    print(f"HV nets ({len(report.hv_nets)}): {', '.join(report.hv_nets)}")
    print(f"NOTE: {DISCLAIMER}")
    print()

    if not report.pairs:
        print("Census: 0 pairs (HV nets present but no other conductors / edge found).")
        return

    header = (
        f"{'HV net':<14} {'Against':<14} {'Layer':<6} "
        f"{'Clnce':>8} {'ReqCl':>8} {'Creep':>8} {'ReqCr':>8} "
        f"{'Margin':>8} {'Govern':>8} {'Result':>7}"
    )
    print(header)
    print("-" * len(header))
    for p in report.pairs:
        result = "PASS" if p.passed else "FAIL"
        req_cl = p.required_clearance_mm if p.required_clearance_mm is not None else 0.0
        req_cr = p.required_creepage_mm if p.required_creepage_mm is not None else 0.0
        print(
            f"{p.net_a:<14} {p.net_b:<14} {p.layer:<6} "
            f"{p.clearance_mm:>8.3f} {req_cl:>8.3f} "
            f"{p.creepage_mm:>8.3f} {req_cr:>8.3f} "
            f"{p.margin_mm:>8.3f} {p.governing_bound:>8} {result:>7}"
        )

    print()
    failures = [p for p in report.pairs if not p.passed]
    total = len(report.pairs)
    if failures:
        print(
            f"FAIL: {len(failures)}/{total} pair(s) fail the derived creepage/clearance "
            "requirement."
        )
    else:
        print(f"PASS: all {total} pair(s) clear the derived creepage/clearance requirement.")
