"""Attempt-local placement policy for the route CLI."""

from __future__ import annotations

import json
from pathlib import Path

from kicad_tools.placement.routing import analyze_routing_placement
from kicad_tools.router.reporting import RoutingPlacementReport


def prepare(args, source: Path) -> None:
    """Resolve whole-board invalid nets before complete/region can hide terminals."""
    requested = [n.strip() for n in args.nets.split(",") if n.strip()] if args.nets else None
    skipped = [n.strip() for n in args.skip_nets.split(",") if n.strip()] if args.skip_nets else []
    disposition = analyze_routing_placement(
        source,
        requested_nets=requested,
        user_excluded_nets=skipped,
        allow_offboard=getattr(args, "allow_offboard", False),
    )
    if getattr(args, "differential_pairs", False):
        from kicad_tools.router.diffpair import detect_differential_pairs

        pairs = detect_differential_pairs(dict(enumerate(sorted(disposition.all_nets), 1)))
        disposition = analyze_routing_placement(
            source,
            requested_nets=requested,
            user_excluded_nets=skipped,
            allow_offboard=getattr(args, "allow_offboard", False),
            coupled_groups=[(p.positive.net_name, p.negative.net_name) for p in pairs],
        )
    args._placement_disposition = disposition
    output = Path(args.output) if args.output else source.with_stem(source.stem + "_routed")
    args._placement_output = output
    args._placement_output_before = output.stat() if output.exists() else None
    report = Path(args.complete_report) if getattr(args, "complete_report", None) else None
    args._placement_report_before = (
        report.stat() if report is not None and report.exists() else None
    )
    if disposition.invalid_nets and not args.quiet:
        print("Placement-invalid, not attempted: " + ", ".join(sorted(disposition.invalid_nets)))


def finish(args, exit_code: int) -> int:
    """Report physically observed completion without counting blocked nets as attempts."""
    disposition = getattr(args, "_placement_disposition", None)
    if disposition is None or not disposition.invalid_nets:
        return exit_code
    completed = frozenset()
    output = args._placement_output
    fresh = output.exists() and output.stat() != args._placement_output_before
    if fresh:
        from kicad_tools.analysis.net_status import NetStatusAnalyzer

        result = NetStatusAnalyzer(output, strict=True).analyze()
        completed = frozenset(net.net_name for net in result.complete)
    report = RoutingPlacementReport(disposition, completed).to_dict()
    report_path = getattr(args, "complete_report", None)
    if report_path:
        from kicad_tools.core.atomic_write import atomic_write_text

        # The current attempt supplies placement metadata even when no board
        # was produced. Never infer it from an old output board or report.
        path = Path(report_path)
        payload = {}
        if path.exists() and path.stat() != args._placement_report_before:
            payload = json.loads(path.read_text())
        payload["placement_disposition"] = report
        atomic_write_text(Path(report_path), json.dumps(payload, indent=2) + "\n")
    if disposition.requested_invalid_nets and exit_code == 0:
        return 2
    return exit_code
