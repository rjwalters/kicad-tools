"""Attempt-local placement policy for the route CLI."""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import fields, replace
from pathlib import Path

from kicad_tools.placement.routing import RoutingPlacementDisposition, analyze_routing_placement
from kicad_tools.router.reporting import RoutingPlacementReport

_attempt_disposition: ContextVar[list[RoutingPlacementDisposition] | None] = ContextVar(
    "route_attempt_placement", default=None
)


@contextmanager
def capture_disposition():
    """A nested or subsequent invocation gets its own result channel."""
    values: list[RoutingPlacementDisposition] = []
    token = _attempt_disposition.set(values)
    try:
        yield values
    finally:
        _attempt_disposition.reset(token)


def publish_disposition(disposition: RoutingPlacementDisposition) -> None:
    from .route_deadline import current_stage, record_stage

    values = _attempt_disposition.get()
    if values is not None:
        values[:] = [disposition]
    encoded = {
        field.name: (
            disposition.check_available
            if field.name == "check_available"
            else sorted(getattr(disposition, field.name))
        )
        for field in fields(disposition)
    }
    record_stage(current_stage() or "placement", placement_disposition=encoded)


def disposition_from_control(state: dict) -> RoutingPlacementDisposition | None:
    encoded = state.get("placement_disposition")
    if encoded is None:
        return None
    values = {}
    for field in fields(RoutingPlacementDisposition):
        value = encoded[field.name]
        if field.name == "check_available":
            values[field.name] = value
        elif field.name == "pad_net_identities":
            values[field.name] = tuple(tuple(identity) for identity in value)
        else:
            values[field.name] = frozenset(value)
    return RoutingPlacementDisposition(**values)


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
    args._initial_placement_disposition = disposition
    args._placement_disposition = disposition
    publish_disposition(disposition)
    output = Path(args.output) if args.output else source.with_stem(source.stem + "_routed")
    args._placement_output = output
    args._placement_output_before = output.stat() if output.exists() else None
    report = Path(args.complete_report) if getattr(args, "complete_report", None) else None
    args._placement_report_before = (
        report.stat() if report is not None and report.exists() else None
    )
    if disposition.invalid_nets and not args.quiet:
        print("Placement-invalid, not attempted: " + ", ".join(sorted(disposition.invalid_nets)))


def for_attempt(args, skip_nets) -> RoutingPlacementDisposition | None:
    """Derive plane intent from this trial without changing the initial selection."""
    initial = getattr(args, "_initial_placement_disposition", None)
    if initial is None:
        return None
    plane = (
        frozenset(skip_nets or ())
        & initial.all_nets - initial.user_excluded_nets - initial.unrequested_nets
    )
    disposition = replace(
        initial,
        plane_excluded_nets=plane,
        requested_nets=initial.requested_nets - plane,
    )
    args._placement_disposition = disposition
    publish_disposition(disposition)
    return disposition


def select_result(args, router) -> None:
    """Publish the selected router's policy after an escalation compares trials."""
    disposition = getattr(router, "placement_disposition", None)
    if disposition is not None:
        args._placement_disposition = disposition
        publish_disposition(disposition)


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
