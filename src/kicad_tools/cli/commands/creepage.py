"""``kct creepage`` command handler (Issue #4327, phase 1 MVP).

Measures per-pair surface-path (creepage) distance between an HV net group
and every other conductor / board edge, honoring milled Edge.Cuts slots, and
reports a **census** (all pairs + margin, not just violations) with clearance
(through-air) and creepage (over-surface) reported as distinct values.

The required minimum is supplied by the operator via ``--min`` (phase 1 does
not know IEC table values -- that is #4332).  Exit is non-zero iff any pair's
creepage falls below ``--min``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kicad_tools.creepage.engine import CreepageReport


def run_creepage_command(args) -> int:
    """Handle the ``creepage`` command.  Returns the process exit code."""
    from kicad_tools._shapely import has_shapely
    from kicad_tools.creepage.engine import compute_creepage_census, resolve_hv_nets
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
    min_mm = float(args.min)

    hv_nets = resolve_hv_nets(pcb, net_class, net_class_map)
    report = compute_creepage_census(
        pcb,
        hv_nets,
        min_mm,
        net_class=net_class,
        board=str(pcb_path),
    )

    if fmt == "json":
        print(json.dumps(report.to_dict(), indent=2))
    else:
        _render_table(report)

    # Non-zero exit iff any pair's creepage < --min.  An empty census
    # (no HV nets, or HV nets with no neighbors) is a clean exit 0.
    return 0 if report.passed else 1


def _render_table(report: CreepageReport) -> None:
    """Print the human-readable census table."""
    if not report.has_hv_nets:
        print(
            f"No '{report.net_class}' nets found "
            "(supply --net-class-map to classify HV nets, or check --net-class)."
        )
        print(f"Board: {report.board}")
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
