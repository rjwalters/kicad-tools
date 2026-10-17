"""Routing command handlers (route-auto, retune-lengths, zones, optimize-traces).

``kct route`` has no handler here: ``cli.main`` hands its argv straight to
``route_cmd.main`` (issue #7017).

Machine output (``--format json``, issue #4674): ``route-auto`` emits exactly
one document describing the run -- the resolved board/output paths and a
``nets`` array with one entry per requested net (its strategy, metrics,
written copper and warnings, or the failure that stopped it) -- and
``optimize-traces`` forwards the canonical flag to its inner parser.  Exit
codes are unchanged in both cases.  See ``docs/reference/machine-output.md``.
"""

import contextlib
import sys
from typing import TYPE_CHECKING

from ..format_options import FORMAT_JSON, emit_json, stdout_to_stderr_when

if TYPE_CHECKING:
    from kicad_tools.router.checkpoint import BestCheckpointWriter, RouteScore

__all__ = [
    "run_route_auto_command",
    "run_retune_lengths_command",
    "run_zones_command",
    "run_optimize_command",
]


def run_retune_lengths_command(args) -> int:
    """Handle ``retune-lengths`` (Issue #6885): exit 0 only when an output was written."""
    from kicad_tools.router.pcb_retune import RetuneError, retune_pcb
    from kicad_tools.router.rules import net_class_map_from_path

    as_json = getattr(args, "format", "text") == FORMAT_JSON
    try:
        try:
            net_class_map = net_class_map_from_path(args.net_class_map, pcb_path=args.pcb)
        except (OSError, ValueError, TypeError) as exc:
            raise RetuneError("missing_rules", f"cannot load net-class map: {exc}") from exc
        with stdout_to_stderr_when(as_json):
            report = retune_pcb(
                args.pcb,
                args.output,
                groups=args.groups,
                diffpairs=[tuple(pair) for pair in args.diffpairs],
                net_class_map=net_class_map,
                provenance_path=args.provenance,
                timeout_s=args.timeout,
            )
    except RetuneError as exc:
        if as_json:
            emit_json(
                {"command": "retune-lengths", "ok": False, "code": exc.code, "error": exc.message}
            )
        else:
            print(f"retune-lengths FAILED ({exc.code}): {exc.message}", file=sys.stderr)
            print("No output was written; the input board is unchanged.", file=sys.stderr)
        return 1

    if as_json:
        emit_json({"command": "retune-lengths", "ok": True, **report.to_dict()})
        return 0
    print(f"retune-lengths: {report.status} -> {report.output_path}")
    for t in report.targets:
        line = (
            f"  {t.kind} {t.name}: skew {t.before_skew_mm:.4f} -> {t.after_skew_mm:.4f} mm "
            f"(limit {t.tolerance_mm:g} mm, {t.tolerance_source}; {t.status})"
        )
        if t.stretch_target_mm is not None:
            line += (
                f"; stretch target {t.stretch_target_mm:g} mm "
                f"{'met' if t.stretch_achieved else 'NOT met'}"
            )
        print(line)
    print(f"  changed nets: {', '.join(report.changed_nets) or 'none'}")
    print(f"  provenance: {report.provenance_path}")
    return 0


def run_zones_command(args) -> int:
    """Handle zones command."""
    if not args.zones_command:
        print("Usage: kicad-tools zones <command> [options] <file>")
        print("Commands: add, list, batch, hv-keepout, fill")
        return 1

    from ..zones_cmd import main as zones_main

    if args.zones_command == "add":
        sub_argv = ["add", args.pcb]
        if args.output:
            sub_argv.extend(["-o", args.output])
        sub_argv.extend(["--net", args.net])
        sub_argv.extend(["--layer", args.layer])
        if args.priority != 0:
            sub_argv.extend(["--priority", str(args.priority)])
        if args.clearance != 0.3:
            sub_argv.extend(["--clearance", str(args.clearance)])
        if getattr(args, "thermal_gap", 0.3) != 0.3:
            sub_argv.extend(["--thermal-gap", str(args.thermal_gap)])
        if getattr(args, "thermal_bridge", 0.4) != 0.4:
            sub_argv.extend(["--thermal-bridge", str(args.thermal_bridge)])
        if getattr(args, "min_thickness", 0.25) != 0.25:
            sub_argv.extend(["--min-thickness", str(args.min_thickness)])
        if args.verbose:
            sub_argv.append("--verbose")
        if args.dry_run:
            sub_argv.append("--dry-run")
        # Use global quiet flag
        if getattr(args, "global_quiet", False):
            sub_argv.append("--quiet")
        if getattr(args, "format", "text") != "text":
            sub_argv.extend(["--format", args.format])
        return zones_main(sub_argv) or 0

    elif args.zones_command == "list":
        sub_argv = ["list", args.pcb]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        return zones_main(sub_argv) or 0

    elif args.zones_command == "batch":
        sub_argv = ["batch", args.pcb]
        if args.output:
            sub_argv.extend(["-o", args.output])
        sub_argv.extend(["--power-nets", args.power_nets])
        if args.clearance != 0.3:
            sub_argv.extend(["--clearance", str(args.clearance)])
        if args.verbose:
            sub_argv.append("--verbose")
        if args.dry_run:
            sub_argv.append("--dry-run")
        # Use global quiet flag
        if getattr(args, "global_quiet", False):
            sub_argv.append("--quiet")
        if getattr(args, "format", "text") != "text":
            sub_argv.extend(["--format", args.format])
        return zones_main(sub_argv) or 0

    elif args.zones_command == "hv-keepout":
        sub_argv = ["hv-keepout", args.pcb]
        if args.output:
            sub_argv.extend(["-o", args.output])
        sub_argv.extend(["--net-class", args.net_class])
        if getattr(args, "net_class_map", None):
            sub_argv.extend(["--net-class-map", args.net_class_map])
        sub_argv.extend(["--clearance", str(args.clearance)])
        if getattr(args, "plane_layers", None):
            sub_argv.extend(["--plane-layers", args.plane_layers])
        if getattr(args, "refill", False):
            sub_argv.append("--refill")
        if args.verbose:
            sub_argv.append("--verbose")
        if args.dry_run:
            sub_argv.append("--dry-run")
        if getattr(args, "global_quiet", False):
            sub_argv.append("--quiet")
        if getattr(args, "format", "text") != "text":
            sub_argv.extend(["--format", args.format])
        return zones_main(sub_argv) or 0

    elif args.zones_command == "fill":
        sub_argv = ["fill", args.pcb]
        if args.output:
            sub_argv.extend(["-o", args.output])
        if getattr(args, "net", None):
            sub_argv.extend(["--net", args.net])
        if args.verbose:
            sub_argv.append("--verbose")
        if args.dry_run:
            sub_argv.append("--dry-run")
        # Use global quiet flag
        if getattr(args, "global_quiet", False):
            sub_argv.append("--quiet")
        if getattr(args, "format", "text") != "text":
            sub_argv.extend(["--format", args.format])
        return zones_main(sub_argv) or 0

    return 1


def _effective_via_geometry(
    pcb_path: str,
    via_drill: float | None,
    via_diameter: float | None,
) -> tuple[float | None, float | None]:
    """Resolve the via drill/diameter route-auto would actually use.

    An explicit CLI override wins; otherwise the value is derived from the
    board's net-class via constraints (Issue #4247).  Returns ``(None, None)``
    for any field that can be neither overridden nor derived (e.g. the board
    file is unreadable during a dry-run preview).
    """
    eff_drill = via_drill
    eff_diameter = via_diameter
    if eff_drill is not None and eff_diameter is not None:
        return eff_drill, eff_diameter
    try:
        from pathlib import Path

        from kicad_tools.router.io import parse_pcb_design_rules

        pcb_rules = parse_pcb_design_rules(Path(pcb_path).read_text())
        if eff_drill is None:
            eff_drill = pcb_rules.min_via_drill
        if eff_diameter is None:
            eff_diameter = pcb_rules.min_via_diameter
    except Exception:
        # Dry-run preview must never fail on a missing/unreadable board file;
        # fall back to reporting only the explicit overrides (possibly None).
        pass
    return eff_drill, eff_diameter


def _print_effective_via_geometry(
    eff_drill: float | None,
    override_drill: float | None,
    eff_diameter: float | None,
    override_diameter: float | None,
) -> None:
    """Print the effective via geometry for the dry-run preview (Issue #4247)."""

    def _fmt(value: float | None, override: float | None) -> str:
        if value is None:
            return "board-derived (unavailable)"
        source = "explicit override" if override is not None else "board-derived"
        return f"{value:.4g}mm ({source})"

    print(f"  Via drill: {_fmt(eff_drill, override_drill)}")
    print(f"  Via diameter: {_fmt(eff_diameter, override_diameter)}")


def _route_auto_one(
    args,
    net_name: str,
    pcb_path: str,
    output_path: str | None,
    *,
    as_json: bool = False,
) -> tuple[int, dict]:
    """Route a single net via RoutingOrchestrator and report the result.

    Extracted from ``run_route_auto_command`` (Issue #4322) so the ``--net``
    single-net path and each iteration of the ``--nets`` multi-net loop share
    identical routing + reporting.  ``pcb_path`` / ``output_path`` are passed
    explicitly so the loop can chain outputs (route net N+1 from net N's
    output, accumulating copper).

    Returns ``(exit_code, document)``.  The document is the per-net entry of
    the ``--format json`` payload (issue #4674); prose is printed only when
    *as_json* is false, so text output stays byte-identical.
    """

    via_drill = getattr(args, "via_drill", None)
    via_diameter = getattr(args, "via_diameter", None)

    from kicad_tools.mcp.tools.routing import route_net_auto

    def _error_doc(message: str) -> dict:
        return {
            "net": net_name,
            "success": False,
            "partial": False,
            "error": message,
            "source": pcb_path,
        }

    try:
        # The negotiated router logs its per-iteration progress on stdout;
        # under --format json that chatter is replayed on stderr instead.
        with stdout_to_stderr_when(as_json):
            result = route_net_auto(
                pcb_path=pcb_path,
                net_name=net_name,
                output_path=output_path,
                strategy=args.strategy,
                enable_repair=not args.no_repair,
                enable_via_resolution=not args.no_via_resolution,
                # Issue #4148: spatial routing bound.  Confines the routed net to
                # the board-relative box and fails if the net has an endpoint
                # outside it.
                region=getattr(args, "region", None),
                # Issue #4165: persist partial multi-pad copper only when opted in.
                allow_partial=getattr(args, "allow_partial", False),
                # Issue #4247: explicit via-geometry overrides (None => board-derived).
                via_drill=via_drill,
                via_diameter=via_diameter,
                # Issue #6001: classify what a partial / failed net left
                # unrouted -- JSON only, like ``kct route`` (#5944).
                diagnose_unrouted_budget=(
                    getattr(args, "diagnose_unrouted_budget", 20.0) if as_json else None
                ),
            )
    except FileNotFoundError as e:
        if not as_json:
            print(f"Error: {e}", file=sys.stderr)
        return 1, _error_doc(str(e))
    except ValueError as e:
        if not as_json:
            print(f"Error: {e}", file=sys.stderr)
        return 1, _error_doc(str(e))
    except Exception as e:
        if not as_json:
            print(f"Error: {e}", file=sys.stderr)
        if getattr(args, "verbose", False):
            import traceback

            traceback.print_exc()
        return 1, _error_doc(str(e))

    doc = {
        "net": result.get("net_name", net_name),
        "success": bool(result["success"]),
        "partial": bool(result.get("partial", False)),
        "strategy_used": result.get("strategy_used"),
        "metrics": result.get("metrics", {}),
        "segments_written": result.get("segments_written"),
        "vias_written": result.get("vias_written"),
        "warnings": list(result.get("warnings", [])),
        "output_path": result.get("output_path"),
        "pads_connected": result.get("pads_connected"),
        "pads_total": result.get("pads_total"),
        "error": result.get("error_message"),
        "alternative_strategies": list(result.get("alternative_strategies", [])),
        "source": pcb_path,
    }
    if "unrouted" in result:
        # Issue #6001: same entries / summary ``kct route`` emits (#5944).
        doc["unrouted"] = result["unrouted"]
        doc["unrouted_diagnosis"] = result.get("unrouted_diagnosis")

    if as_json:
        # The caller prints the single document; per-net prose is suppressed.
        return (0 if result["success"] else 1), doc

    # Print result
    if result["success"]:
        metrics = result.get("metrics", {})
        print(f"Routed net '{result['net_name']}' successfully")
        print(f"  Strategy: {result.get('strategy_used', 'unknown')}")
        if metrics:
            length = metrics.get("total_length_mm", 0.0)
            vias = metrics.get("via_count", 0)
            repairs = metrics.get("repair_actions", 0)
            if length:
                print(f"  Total length: {length:.2f}mm")
            if vias:
                print(f"  Vias: {vias}")
            if repairs:
                print(f"  Repairs applied: {repairs}")
        # Issue #2913: surface physical track count delta so silent
        # data loss (success=True with no tracks written) is visible.
        segs_written = result.get("segments_written")
        vias_written = result.get("vias_written")
        if segs_written is not None:
            print(f"  Segments written: {segs_written}")
        if vias_written:
            print(f"  Vias written: {vias_written}")
        for warning in result.get("warnings", []):
            print(f"  Warning: {warning}")
        if result.get("output_path"):
            print(f"  Saved to: {result['output_path']}")
        return 0, doc
    elif result.get("partial"):
        # Issue #4165: routing produced copper but left some pads of a
        # multi-pad net unconnected.  Report the honest k/n and exit non-zero;
        # the copper is only saved when --allow-partial was given.
        pads_connected = result.get("pads_connected")
        pads_total = result.get("pads_total")
        kn = (
            f"{pads_connected}/{pads_total}"
            if pads_connected is not None and pads_total is not None
            else "some"
        )
        print(
            f"partially routed net '{result['net_name']}': {kn} pads connected",
            file=sys.stderr,
        )
        print(f"  Strategy: {result.get('strategy_used', 'unknown')}", file=sys.stderr)
        print(
            "  A single two-terminal corridor left pad(s) unconnected. "
            "Try --strategy hierarchical (or --strategy auto, which attempts a "
            "hierarchical fallback) to improve completion; note that congested "
            "or geometrically hard nets may remain partial even then.",
            file=sys.stderr,
        )
        segs_written = result.get("segments_written")
        if segs_written is not None:
            print(f"  Partial copper saved: {segs_written} segments", file=sys.stderr)
        elif not getattr(args, "allow_partial", False):
            print(
                "  Partial copper NOT saved (pass --allow-partial to persist it).",
                file=sys.stderr,
            )
        for warning in result.get("warnings", []):
            print(f"  Warning: {warning}", file=sys.stderr)
        return 1, doc
    else:
        print(f"Routing failed for net '{result['net_name']}'", file=sys.stderr)
        if result.get("error_message"):
            print(f"  Error: {result['error_message']}", file=sys.stderr)
        for alt in result.get("alternative_strategies", []):
            strategy_name = alt.get("strategy", "unknown")
            reason = alt.get("reason", "")
            print(f"  Try: {strategy_name} - {reason}", file=sys.stderr)
        return 1, doc


def _route_auto_dry_run(args, net_name: str, *, as_json: bool = False) -> dict:
    """Preview the strategy selection for one net (no routing).

    Prints the human preview unless *as_json*, and returns the same content as
    the per-net entry of the ``--format json`` document (issue #4674).
    """
    via_drill = getattr(args, "via_drill", None)
    via_diameter = getattr(args, "via_diameter", None)
    # Report the effective via geometry that would be used (Issue #4247):
    # an explicit CLI override, or the value derived from the board's
    # net-class via constraints.
    eff_drill, eff_diameter = _effective_via_geometry(args.pcb, via_drill, via_diameter)

    if not as_json:
        print(f"[dry-run] Would route net '{net_name}' on '{args.pcb}' using RoutingOrchestrator")
        print(f"  Strategy override: {args.strategy}")
        print(f"  Repair enabled: {not args.no_repair}")
        print(f"  Via resolution enabled: {not args.no_via_resolution}")
        _print_effective_via_geometry(eff_drill, via_drill, eff_diameter, via_diameter)
        if args.output:
            print(f"  Output: {args.output}")

    def _source(value: float | None, override: float | None) -> str | None:
        if value is None:
            return None
        return "explicit override" if override is not None else "board-derived"

    return {
        "net": net_name,
        "would_route": True,
        "strategy": args.strategy,
        "repair_enabled": not args.no_repair,
        "via_resolution_enabled": not args.no_via_resolution,
        "via_drill_mm": eff_drill,
        "via_drill_source": _source(eff_drill, via_drill),
        "via_diameter_mm": eff_diameter,
        "via_diameter_source": _source(eff_diameter, via_diameter),
        "output_path": args.output,
    }


def _parse_route_auto_targets(args, *, as_json: bool = False) -> tuple[list[str] | None, int]:
    """Resolve the net(s) route-auto should route -- Issue #4322.

    Exactly one of ``--net`` (single) / ``--nets`` (comma-separated list) must
    be given.  Returns ``(net_list, rc)``: on success ``net_list`` is the
    ordered, de-duplicated, whitespace-trimmed list of nets and ``rc`` is 0; on
    a usage error ``net_list`` is ``None`` and ``rc`` is a non-zero exit code
    (the error has already been reported -- as prose, or as the single
    ``{"error": ...}`` document under *as_json*, issue #4674).
    """

    def _usage_error(message: str) -> tuple[None, int]:
        if as_json:
            emit_json(
                {
                    "command": "route-auto",
                    "pcb": getattr(args, "pcb", None),
                    "error": message,
                    "nets": [],
                    "success": False,
                }
            )
        else:
            print(f"Error: {message}", file=sys.stderr)
        return None, 2

    net = getattr(args, "net", None)
    nets_raw = getattr(args, "nets", None)

    if net and nets_raw:
        return _usage_error(
            "--net and --nets are mutually exclusive (use --net for a "
            "single net or --nets for a comma-separated list)."
        )
    if not net and not nets_raw:
        return _usage_error("route-auto requires --net NAME or --nets NAME[,NAME...].")

    if net:
        return [net], 0

    # Reachable only when nets_raw is truthy (the guards above returned for the
    # net / neither cases); ``or ""`` keeps the type checker happy.
    parsed = [n.strip() for n in (nets_raw or "").split(",") if n.strip()]
    if not parsed:
        return _usage_error(
            "--nets was given but lists no net names "
            '(expected a comma-separated list, e.g. --nets "/A,/B").'
        )
    # De-duplicate while preserving first-seen order.
    seen: set[str] = set()
    ordered: list[str] = []
    for n in parsed:
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    return ordered, 0


def run_route_auto_command(args) -> int:
    """Handle route-auto command using RoutingOrchestrator.

    Routes a single net (``--net``) or several nets in sequence (``--nets``,
    Issue #4322).  For the multi-net path each net is routed independently and
    the exit code is non-zero if ANY net fails or is left partial; when an
    output path is given, copper accumulates (net N+1 routes from net N's
    output).  The single-net ``--net`` path is unchanged.

    Under ``--format json`` (issue #4674) the per-net prose is replaced by a
    single document whose ``nets`` array carries one entry per requested net,
    in request order; the exit code is unchanged.
    """
    as_json = getattr(args, "format", "text") == FORMAT_JSON

    net_list, rc = _parse_route_auto_targets(args, as_json=as_json)
    if rc != 0:
        return rc
    assert net_list is not None  # rc == 0 guarantees a list

    def _emit(nets: list[dict], overall_rc: int, lint_gate: dict | None = None) -> None:
        doc = {
            "command": "route-auto",
            "pcb": args.pcb,
            "output": args.output,
            "strategy": args.strategy,
            "dry_run": bool(args.dry_run),
            "nets": nets,
            "nets_requested": len(net_list),
            "nets_routed": sum(1 for entry in nets if entry.get("success")),
            "success": overall_rc == 0,
        }
        if lint_gate is not None:
            # Issue #6054: present only under --lint-gate.
            doc["lint_gate"] = lint_gate
        emit_json(doc)

    # Dry-run: preview strategy selection without routing (per net).
    if args.dry_run:
        previews = [_route_auto_dry_run(args, net_name, as_json=as_json) for net_name in net_list]
        if as_json:
            # A preview routes nothing, so nets_routed stays 0 while success
            # reports that the preview itself completed.
            _emit(previews, 0)
        return 0

    overall_rc = 0
    net_docs: list[dict] = []

    # Issue #6054: --lint-gate baselines the input board BEFORE any pass (and
    # before _RouteAutoPasses seeds --output), and judges --output after the
    # last one -- one whole-run verdict over every net.
    gate = None
    requested_output = args.output
    if getattr(args, "lint_gate", False):
        if not args.output:
            print(
                "Warning: --lint-gate has no effect without --output "
                "(route-auto persists nothing to gate).",
                file=sys.stderr,
            )
        else:
            from ..route_lint_gate import LintGate

            gate = LintGate.from_route_auto_args(args)
            gate_error = gate.begin()
            if gate_error is not None:
                print(gate_error, file=sys.stderr)
                if as_json:
                    _emit([], 1, gate.baseline_error_outcome().to_dict())
                return 1

    def _unstage_args() -> None:
        if gate is not None:
            args.output = requested_output

    # Issue #5945: --resume / --checkpoint / regressing-pass rollback.
    try:
        if gate is not None:
            # Issue #6090: every pass writes a staging file next to --output;
            # only the judged final board is promoted onto it.  Staged inside
            # the guard so a SIGTERM right after it still cleans up.
            staged = gate.stage()
            if staged is not None:
                args.output = str(staged)
        passes, rc = _RouteAutoPasses.create(args, net_list, as_json=as_json)
    except BaseException:
        if gate is not None:
            gate.abort()
            _unstage_args()
        raise
    if rc != 0:
        if gate is not None:
            gate.abort()
            _unstage_args()
        return rc
    base_pcb = passes.base_pcb
    working = passes.working

    routed_so_far = 0
    try:
        for net_name in net_list:
            skip_doc = passes.resumed_skip(net_name)
            if skip_doc is not None:
                net_docs.append(skip_doc)
                continue
            # Chain outputs so multi-net copper accumulates: the first net routes
            # from the original board; subsequent nets route from the prior output
            # (only possible when an output path was given -- otherwise nothing is
            # persisted and each net routes independently against the input).
            source = base_pcb
            if working and (routed_so_far > 0 or passes.seeded_working):
                # Later nets chain from the accumulated output; a private
                # --checkpoint working copy already holds the base board.
                source = working
            routed_so_far += 1
            net_rc, net_doc = _route_auto_one(args, net_name, source, working, as_json=as_json)
            if passes.after_pass(routed_so_far, net_name, net_doc):
                # The pass made the board worse and was undone -- its copper is
                # not in the output, so it cannot count as routed.
                net_rc = 1
            net_docs.append(net_doc)
            if net_rc != 0:
                overall_rc = 1
        passes.finish()
    except BaseException:
        if gate is not None:
            gate.abort()
            _unstage_args()
        raise
    gate_doc = None
    if gate is not None:
        try:
            outcome = gate.finish()
        finally:
            _unstage_args()
        net_docs = gate.relocate(net_docs)
        overall_rc = outcome.exit_code(overall_rc)
        gate_doc = outcome.to_dict()
        if outcome.rolled_back:
            # None of this run's copper shipped.
            for entry in net_docs:
                if entry.get("success") and not entry.get("resumed"):
                    entry["success"] = False
                    entry["lint_gate_rolled_back"] = True
    if as_json:
        _emit(net_docs, overall_rc, gate_doc)
    return overall_rc


class _RouteAutoPasses:
    """Per-net pass bookkeeping for ``route-auto`` (Issue #5945).

    Each routed net is one *pass* over the accumulating board.  When active
    (``--checkpoint``, ``--resume``, or a multi-net ``--nets`` run with
    ``--output``), every pass that wrote copper is scored with
    :meth:`RouteScore.from_board`:

    * a pass that LOWERS the number of complete nets is rolled back -- the
      previous accepted board is restored atomically before the next net
      routes, so a regression can never be what ships;
    * a pass that strictly beats the best score is written to
      ``--checkpoint`` (board + ``.checkpoint.json`` sidecar).

    ``--resume PATH`` routes the first net from the checkpoint board instead
    of the input, and skips requested nets the checkpoint already completed.
    Without ``--output``, ``--checkpoint`` accumulates copper in a private
    working file next to the checkpoint (removed at the end); with
    ``--output``, the output is seeded with the starting board up front, so
    it is always written and never a stale leftover (Issue #6010).
    ``--no-rollback`` keeps regressing passes (scoring and checkpoints still
    run).  A rolled-back net reports ``success: False``.
    """

    def __init__(self) -> None:
        self.active = False
        self.base_pcb: str = ""
        self.working: str | None = None
        self._tmp_working: str | None = None
        self.writer: BestCheckpointWriter | None = None
        self.accepted_text: str | None = None
        self.accepted_score: RouteScore | None = None
        self.accepted_pass = 0
        self.resumed_complete: set[str] = set()
        self._seeded_output = False
        self.rollback = True
        self.quiet = False
        self.as_json = False

    @classmethod
    def create(cls, args, net_list: list[str], *, as_json: bool) -> "tuple[_RouteAutoPasses, int]":
        from pathlib import Path

        self = cls()
        self.as_json = as_json
        self.quiet = bool(getattr(args, "quiet", False) or getattr(args, "global_quiet", False))
        self.base_pcb = args.pcb
        checkpoint = getattr(args, "checkpoint", None)
        resume = getattr(args, "resume", None)
        output_path = args.output

        if resume:
            from kicad_tools.router.checkpoint import checkpoint_identity_mismatch
            from kicad_tools.router.preserve_existing import fully_connected_nets

            if not Path(resume).exists():
                self._err(f"Error: --resume checkpoint not found: {resume}")
                return self, 1
            try:
                mismatch = checkpoint_identity_mismatch(args.pcb, resume)
            except OSError as e:
                self._err(f"Error: cannot read --resume checkpoint: {e}")
                return self, 1
            if mismatch:
                self._err(
                    f"Error: --resume checkpoint {resume} does not match {args.pcb} ({mismatch})."
                )
                return self, 2
            self.base_pcb = str(resume)
            try:
                self.resumed_complete = set(fully_connected_nets(Path(resume)))
            except Exception:  # noqa: BLE001 - nothing known complete => route all
                self.resumed_complete = set()

        if checkpoint:
            from kicad_tools.router.checkpoint import BestCheckpointWriter, RouteScore

            ck = Path(checkpoint)
            if ck.suffix != ".kicad_pcb":
                self._err(f"Error: --checkpoint must be a .kicad_pcb path, got {ck}")
                return self, 1
            writer = self.writer = BestCheckpointWriter(
                ck,
                command="route-auto",
                source_pcb=resume or args.pcb,
                quiet=self.quiet or as_json,
                printer=self._info,
            )
            if resume and ck.exists() and ck.resolve() == Path(resume).resolve():
                with contextlib.suppress(OSError):
                    writer.seed(RouteScore.from_board(ck), 0, label="resumed checkpoint")
            if not output_path:
                import os
                import tempfile

                ck.parent.mkdir(parents=True, exist_ok=True)
                fd, name = tempfile.mkstemp(
                    prefix=ck.stem + "_working_", suffix=".kicad_pcb", dir=ck.parent
                )
                os.close(fd)
                # Seed the working copy with the starting board (and its
                # project rules) so every pass -- even after a failed first
                # net -- has a board to chain from.
                from kicad_tools.core.atomic_write import atomic_write_text

                base = Path(self.base_pcb)
                atomic_write_text(name, base.read_text(encoding="utf-8"))
                for suffix in (".kicad_pro", ".kicad_dru"):
                    if base.with_suffix(suffix).exists():
                        atomic_write_text(
                            Path(name).with_suffix(suffix),
                            base.with_suffix(suffix).read_text(encoding="utf-8"),
                        )
                self._tmp_working = name
                self.working = name

        if self.working is None:
            self.working = output_path
        self.rollback = not getattr(args, "no_rollback", False)
        self.active = bool(self.working and (checkpoint or resume or len(net_list) > 1))
        if self.active and self._tmp_working is None and output_path:
            # Seed --output with the starting board (the --resume checkpoint,
            # or the input) exactly like the private working copy above, so
            # (a) the first routed net chains from it, (b) a run in which
            # every requested net is already complete -- or every pass
            # writes nothing -- still leaves the right board at --output
            # instead of a missing or stale file from an earlier run
            # (Issues #5945, #6010), and (c) after_pass never scores a stale
            # leftover as a net's pass.
            try:
                self._seed_output(Path(output_path))
            except OSError as e:
                self._err(f"Error: cannot write --output {output_path}: {e}")
                return self, 1
        if self.active:
            self._score_baseline()
        return self, 0

    def _seed_output(self, dest) -> None:
        from pathlib import Path

        from kicad_tools.core.atomic_write import atomic_write_text

        base = Path(self.base_pcb)
        if not base.exists():
            return
        if not (dest.exists() and dest.resolve() == base.resolve()):
            dest.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(dest, base.read_text(encoding="utf-8"))
            for suffix in (".kicad_pro", ".kicad_dru"):
                src, dst = base.with_suffix(suffix), dest.with_suffix(suffix)
                if src.exists() and not dst.exists():
                    atomic_write_text(dst, src.read_text(encoding="utf-8"))
        self._seeded_output = True

    # -- reporting -------------------------------------------------------
    def _err(self, msg: str) -> None:
        # stderr is never part of the --format json document.
        print(msg, file=sys.stderr)

    def _info(self, msg: str) -> None:
        if not (self.quiet or self.as_json):
            print(msg)

    # -- passes ----------------------------------------------------------
    def _score_baseline(self) -> None:
        from pathlib import Path

        from kicad_tools.router.checkpoint import RouteScore

        base = Path(self.base_pcb)
        if not base.exists():
            return
        try:
            text = base.read_text(encoding="utf-8")
            score = RouteScore.from_board(base)
        except (OSError, ValueError):
            return
        self.accepted_text, self.accepted_score, self.accepted_pass = text, score, 0
        if self.writer is not None:
            self._offer(
                score,
                0,
                text,
                "pass 0 (resumed checkpoint)" if self.resumed_complete else "pass 0 (input)",
            )

    def _offer(self, score, pass_index: int, text: str, label: str) -> None:
        from pathlib import Path

        from kicad_tools.core.atomic_write import atomic_write_text

        def _write(dest: Path) -> None:
            atomic_write_text(dest, text)
            for suffix in (".kicad_pro", ".kicad_dru"):
                src = Path(self.base_pcb).with_suffix(suffix)
                dst = dest.with_suffix(suffix)
                if src.exists() and not dst.exists():
                    atomic_write_text(dst, src.read_text(encoding="utf-8"))

        try:
            if self.writer is not None:
                self.writer.offer(score, pass_index, _write, label=label)
        except OSError as e:  # a checkpoint failure must never fail the route
            self._err(f"  checkpoint: write failed ({e}); continuing")

    @property
    def seeded_working(self) -> bool:
        return self._tmp_working is not None or self._seeded_output

    def resumed_skip(self, net_name: str) -> dict | None:
        if net_name not in self.resumed_complete:
            return None
        self._info(f"Net '{net_name}' already complete in --resume checkpoint; kept as-is")
        return {
            "net": net_name,
            "success": True,
            "partial": False,
            "resumed": True,
            "source": self.base_pcb,
        }

    def after_pass(self, pass_index: int, net_name: str, doc: dict) -> bool:
        """Score the board after a pass; return True when it was rolled back."""
        if not self.active:
            return False
        from pathlib import Path

        from kicad_tools.core.atomic_write import atomic_write_text
        from kicad_tools.router.checkpoint import RouteScore

        target = self.working
        if not target or not Path(target).exists():
            return False
        try:
            text = Path(target).read_text(encoding="utf-8")
        except OSError:
            return False
        if text == self.accepted_text:
            return False  # the pass wrote nothing new
        score = RouteScore.from_board(Path(target))
        label = f"pass {pass_index} (net {net_name})"
        if (
            self.rollback
            and self.accepted_score is not None
            and self.accepted_text is not None
            and score.nets_complete < self.accepted_score.nets_complete
        ):
            atomic_write_text(Path(target), self.accepted_text)
            # The pass's copper is not in the output: report it as not
            # successful, consistent with the exit code and nets_routed.
            doc["success"] = False
            doc["rolled_back"] = True
            doc["rolled_back_to_pass"] = self.accepted_pass
            msg = (
                f"  rollback: {label} lowered complete nets "
                f"{self.accepted_score.nets_complete} -> {score.nets_complete}; "
                f"restored pass {self.accepted_pass} (--no-rollback keeps it)"
            )
            self._err(msg)
            return True
        self.accepted_text, self.accepted_score, self.accepted_pass = text, score, pass_index
        if self.writer is not None:
            self._offer(score, pass_index, text, label)
        return False

    def finish(self) -> None:
        import os

        if self.active and self.accepted_score is not None:
            self._info(
                f"Emitted result: pass {self.accepted_pass} "
                f"({self.accepted_score.nets_complete} net(s) complete)"
            )
        if self._tmp_working:
            for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
                with contextlib.suppress(OSError):
                    os.unlink(os.path.splitext(self._tmp_working)[0] + suffix)


def run_optimize_command(args) -> int:
    """Handle optimize-traces command."""
    from ..optimize_cmd import main as optimize_main

    sub_argv = [args.pcb]
    if args.output:
        sub_argv.extend(["-o", args.output])
    if args.net:
        sub_argv.extend(["--net", args.net])
    if args.no_merge:
        sub_argv.append("--no-merge")
    if args.no_zigzag:
        sub_argv.append("--no-zigzag")
    if args.no_45:
        sub_argv.append("--no-45")
    if args.chamfer_size != 0.5:
        sub_argv.extend(["--chamfer-size", str(args.chamfer_size)])
    if args.verbose:
        sub_argv.append("--verbose")
    if args.dry_run:
        sub_argv.append("--dry-run")
    # Use command-level quiet or global quiet
    if getattr(args, "quiet", False) or getattr(args, "global_quiet", False):
        sub_argv.append("--quiet")
    # DRC-aware mode arguments
    if getattr(args, "drc_aware", False):
        sub_argv.append("--drc-aware")
    if getattr(args, "mfr", None):
        sub_argv.extend(["--mfr", args.mfr])
    if getattr(args, "layers", 2) != 2:
        sub_argv.extend(["--layers", str(args.layers)])
    if getattr(args, "copper", 1.0) != 1.0:
        sub_argv.extend(["--copper", str(args.copper)])
    # Machine output (#4674): forward the canonical flag to the inner parser.
    if getattr(args, "format", "text") != "text":
        sub_argv.extend(["--format", args.format])
    return optimize_main(sub_argv)
