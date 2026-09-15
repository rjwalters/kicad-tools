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
        value = encoded.get(field.name, field.default)
        if field.name == "check_available":
            values[field.name] = value
        elif field.name in {"pad_net_identities", "physical_pad_net_identities"}:
            values[field.name] = tuple(tuple(identity) for identity in value)
        else:
            values[field.name] = frozenset(value)
    return RoutingPlacementDisposition(**values)


def _active_coupled_groups(args, names: frozenset[str]) -> list[tuple[str, str]]:
    """Resolve coupling on the whole board before exclusions remove either half."""
    from kicad_tools.router.diffpair import should_engage_coupled
    from kicad_tools.router.diffpair_detection import detect_diff_pairs
    from kicad_tools.router.net_class import classify_and_apply_rules
    from kicad_tools.router.net_names import resolve_net_class_map_keys
    from kicad_tools.router.rules import DEFAULT_NET_CLASS_MAP

    net_names = dict(enumerate(sorted(names), 1))
    classes = dict(DEFAULT_NET_CLASS_MAP)
    for name, auto_routing in classify_and_apply_rules(net_names).items():
        classes.setdefault(name, auto_routing)
    loaded = getattr(args, "_loaded_net_class_map", None)
    if loaded:
        resolution = resolve_net_class_map_keys(loaded.keys(), names)
        for name, key in resolution.resolved.items():
            classes[name] = loaded[key]
    # Match DifferentialPairRouter's net-name/class-name detection context.
    routing = dict(classes)
    net_to_class = {}
    for name, cls in classes.items():
        net_to_class[name] = cls.name
        routing.setdefault(cls.name, cls)
    detected = detect_diff_pairs(net_names, net_class_routing=routing, net_to_class=net_to_class)
    return [
        (item.pair.positive.net_name, item.pair.negative.net_name)
        for item in detected
        if should_engage_coupled(item.pair, routing, net_to_class)[0]
    ]


def prepare(args, source: Path) -> None:
    """Resolve whole-board invalid nets before complete/region can hide terminals."""
    requested = [n.strip() for n in args.nets.split(",") if n.strip()] if args.nets else None
    skipped = [n.strip() for n in args.skip_nets.split(",") if n.strip()] if args.skip_nets else []
    plane = (
        [
            n.strip()
            for n in (getattr(args, "complete_exclude_nets", "") or "").split(",")
            if n.strip()
        ]
        if getattr(args, "complete", False)
        else []
    )
    disposition = analyze_routing_placement(
        source,
        requested_nets=requested,
        user_excluded_nets=skipped,
        plane_excluded_nets=plane,
        allow_offboard=getattr(args, "allow_offboard", False),
    )
    groups = _active_coupled_groups(args, disposition.all_nets)
    if groups:
        disposition = analyze_routing_placement(
            source,
            requested_nets=requested,
            user_excluded_nets=skipped,
            plane_excluded_nets=plane,
            allow_offboard=getattr(args, "allow_offboard", False),
            coupled_groups=groups,
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
    failed = Path(args.export_failed_nets) if getattr(args, "export_failed_nets", None) else None
    args._placement_failed_before = (
        failed.stat() if failed is not None and failed.exists() else None
    )
    if disposition.invalid_nets and not args.quiet:
        print("Placement-invalid, not attempted: " + ", ".join(sorted(disposition.invalid_nets)))


def for_attempt(args, skip_nets) -> RoutingPlacementDisposition | None:
    """Derive plane intent from this trial without changing the initial selection."""
    initial: RoutingPlacementDisposition | None = getattr(
        args, "_initial_placement_disposition", None
    )
    if initial is None:
        return None
    selected = getattr(args, "_route_only_nets", None)
    # --complete synthesizes a route-only selection after the whole-board
    # placement check. Already-connected nets stay in the completion request;
    # their absence from this trial is not evidence of a copper-pour plan.
    selection_skips = (
        initial.all_nets - frozenset(selected) if selected is not None else frozenset()
    )
    plane = initial.plane_excluded_nets | (
        frozenset(skip_nets or ())
        & initial.all_nets
        - initial.user_excluded_nets
        - initial.unrequested_nets
        - selection_skips
        - initial.invalid_nets
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


def _finish_failed_export(args, disposition: RoutingPlacementDisposition) -> None:
    """Add blocked requests even when no router was needed or eligible nets completed."""
    target = getattr(args, "export_failed_nets", None)
    if not target:
        return
    from kicad_tools.core.atomic_write import atomic_write_text

    path = Path(target)
    fresh = path.exists() and path.stat() != args._placement_failed_before
    if path.suffix.lower() == ".json":
        entries = json.loads(path.read_text()) if fresh else []
        # The legacy exporter may have considered excluded IDs unrouted.
        # Placement is authoritative for those nets, including unrequested ones.
        entries = [entry for entry in entries if entry["net"] not in disposition.invalid_nets]
        entries.extend(
            {
                "net": name,
                "status": "placement-invalid, not attempted",
                "attempted": False,
                "pads": [
                    f"{ref}.{pad}"
                    for ref, pad, _authored, effective in disposition.pad_net_identities
                    if effective == name
                ],
            }
            for name in sorted(disposition.requested_invalid_nets)
        )
        content = json.dumps(entries, indent=2) + "\n"
    else:
        names = path.read_text().splitlines() if fresh else []
        names = [name for name in names if name not in disposition.invalid_nets]
        if disposition.requested_invalid_nets:
            names.append("# placement-invalid, not attempted")
            names.extend(sorted(disposition.requested_invalid_nets))
        content = "\n".join(names) + ("\n" if names else "")
    atomic_write_text(path, content)


def finish(args, exit_code: int) -> int:
    """Report physically observed completion without counting blocked nets as attempts."""
    disposition = getattr(args, "_placement_disposition", None)
    if disposition is None or not disposition.invalid_nets:
        return exit_code
    _finish_failed_export(args, disposition)
    completed: frozenset[str] = frozenset()
    output = args._placement_output
    fresh = output.exists() and output.stat() != args._placement_output_before
    if fresh:
        from kicad_tools.analysis.net_status import NetStatusAnalyzer

        result = NetStatusAnalyzer(output, strict=True).analyze()
        completed = frozenset(net.net_name for net in result.complete)
    report = RoutingPlacementReport(disposition, completed).to_dict()
    fill_error = getattr(args, "_placement_fill_error", None)
    if fill_error:
        report["zone_fill_status"] = "failed"
        report["zone_fill_error"] = fill_error
        report["clean_success"] = False
    repair_error = getattr(args, "_placement_repair_error", None)
    if repair_error:
        report["auto_fix_status"] = "rejected"
        report["auto_fix_error"] = repair_error
        report["clean_success"] = False
    if (fill_error or repair_error) and exit_code == 0:
        exit_code = 3
    if disposition.requested_invalid_nets and exit_code == 0:
        exit_code = 2
    if exit_code != 0:
        report["clean_success"] = False
    if getattr(args, "format", None) == "json":
        # The normal diagnostics branch is bypassed by placement-only partials
        # and early all-invalid exits. Publish an attempt summary on every such
        # path, including --quiet and runs without --complete-report.
        print(
            json.dumps(
                {
                    "exit_code": exit_code,
                    "output_written": fresh,
                    "placement_disposition": report,
                },
                indent=2,
            )
        )
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
    return exit_code
