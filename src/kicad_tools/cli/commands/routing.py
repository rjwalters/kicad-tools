"""Routing command handlers (route, route-auto, zones, optimize-traces).

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
    "run_route_command",
    "run_route_auto_command",
    "run_zones_command",
    "run_optimize_command",
]


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


def run_route_command(args) -> int:
    """Handle route command."""
    from ..route_cmd import _flag_passed_explicitly
    from ..route_cmd import main as route_main

    # Issue #4875: the inner command now derives its clearance from a board's
    # own declared ``(net_class …)`` rules when nothing explicit outranks
    # them, so "the operator typed the default value" and "the operator typed
    # nothing" are no longer equivalent.  The value-comparison forwarding
    # below would drop a deliberate ``--clearance 0.15`` / ``--manufacturer
    # jlcpcb``, silently handing the decision back to the board.
    _explicit_clearance = _flag_passed_explicitly(None, ("--clearance",))
    _explicit_manufacturer = _flag_passed_explicitly(None, ("--manufacturer", "--mfr"))

    sub_argv = [args.pcb]
    if args.output:
        sub_argv.extend(["-o", args.output])
    if args.strategy != "negotiated":
        sub_argv.extend(["--strategy", args.strategy])
    if args.skip_nets:
        sub_argv.extend(["--skip-nets", args.skip_nets])
    # Issue #4322: forward --nets (route ONLY the listed nets).  The inner
    # route_cmd handler validates it, enforces mutual exclusion with
    # --skip-nets, and inverts it into the skip machinery.  Declared on BOTH
    # parsers + here so tests/test_cli_parser_drift.py stays green.
    if getattr(args, "nets", None):
        sub_argv.extend(["--nets", args.nets])
    # Issue #3155: forward --preserve-existing (incremental routing).  Both
    # outer (parser.py) and inner (route_cmd.py) parsers declare it as a
    # store_true defaulting to False, so only forward when the user set it.
    if getattr(args, "preserve_existing", False):
        sub_argv.append("--preserve-existing")
    # Issue #4471 (epic #4465): forward --complete (auto-detect + route only
    # the currently-unconnected signal links on the lattice fixed-obstacle
    # path).  Declared on BOTH parsers as store_true defaulting to False, so
    # only forward when the user set it -- flag-off argv stays byte-identical
    # and tests/test_cli_parser_drift.py stays green.
    if getattr(args, "complete", False):
        sub_argv.append("--complete")
    # Issue #4476: forward --complete-exclude-nets (pour/plane nets --complete
    # must not route -- their connectivity comes from a filled zone the trace
    # checker cannot credit).  Declared on BOTH parsers with an empty-string
    # default, so an unset flag forwards nothing and the argv stays
    # byte-identical (tests/test_cli_parser_drift.py).
    if getattr(args, "complete_exclude_nets", ""):
        sub_argv.extend(["--complete-exclude-nets", args.complete_exclude_nets])
    # Issue #4477 (epic #4465, Phase 4): forward --complete-report (the
    # structured unroutable-link report path).  Both parsers declare it; see
    # tests/test_cli_parser_drift.py.
    if getattr(args, "complete_report", None):
        sub_argv.extend(["--complete-report", args.complete_report])
    grid_val = str(args.grid)
    if grid_val.lower() != "auto":
        sub_argv.extend(["--grid", grid_val])
    # Issue #4242: forward --max-cells (auto-grid memory budget override) only
    # when the user changed it from the shared 500,000 default, keeping
    # flag-off argv byte-identical.  Both parsers declare it; see
    # tests/test_cli_parser_drift.py.
    if getattr(args, "max_cells", 500_000) != 500_000:
        sub_argv.extend(["--max-cells", str(args.max_cells)])
    # Issue #4700: --trace-width now carries a ``None`` sentinel (unset) so the
    # inner command can raise the default to the manufacturer's own minimum
    # trace width.  Forward it only when the user actually passed a value --
    # forwarding the sentinel would both crash the float parse and defeat the
    # auto-fill.  Flag-off argv stays byte-identical.
    if getattr(args, "trace_width", None) is not None:
        sub_argv.extend(["--trace-width", str(args.trace_width)])
    if args.clearance != 0.15 or _explicit_clearance:
        sub_argv.extend(["--clearance", str(args.clearance)])
    # Issue #6280: ``None`` sentinel = resolve from the board's own rules;
    # forward only an explicit value so flag-off argv stays byte-identical.
    if getattr(args, "via_clearance", None) is not None:
        sub_argv.extend(["--via-clearance", str(args.via_clearance)])
    if args.via_drill != 0.3:
        sub_argv.extend(["--via-drill", str(args.via_drill)])
    if args.via_diameter != 0.6:
        sub_argv.extend(["--via-diameter", str(args.via_diameter)])
    if args.mc_trials != 10:
        sub_argv.extend(["--mc-trials", str(args.mc_trials)])
    # Issue #6267: forward the evolutionary GA size.  The outer parser accepted
    # --pop-size/--generations but never handed them to the inner route
    # command, so every ``kct route --strategy evolutionary`` ran the default
    # 20 x 10 GA regardless of the flags.
    if getattr(args, "pop_size", 20) != 20:
        sub_argv.extend(["--pop-size", str(args.pop_size)])
    if getattr(args, "generations", 10) != 10:
        sub_argv.extend(["--generations", str(args.generations)])
    if args.iterations != 15:
        sub_argv.extend(["--iterations", str(args.iterations)])
    # Issue #3101: forward --early-stop-patience.  Both outer and inner
    # default to 2; only forward when the user passed a non-default value.
    early_stop_val = getattr(args, "early_stop_patience", 2)
    if early_stop_val != 2:
        sub_argv.extend(["--early-stop-patience", str(early_stop_val)])
    # Issue #3438 / #3414: forward --targeted-ripup / --max-ripups-per-net.
    # Both outer and inner parsers declare --targeted-ripup as store_true
    # defaulting to False and --max-ripups-per-net defaulting to None
    # (Issue #3470: None = use each flow's own default), so only forward
    # explicitly-provided values (matches the early-stop-patience pattern).
    if getattr(args, "targeted_ripup", False):
        sub_argv.append("--targeted-ripup")
    # Issue #4053: forward --bundle-river-planner.  Both parsers declare it
    # as store_true defaulting to False, so only forward when the user set
    # it (byte-identical when absent).
    if getattr(args, "bundle_river_planner", False):
        sub_argv.append("--bundle-river-planner")
    # Issue #4094 (epic #4049): forward the three constructor-only routing
    # flags (#4089/#4090/#4092).  Both parsers declare each as store_true
    # defaulting to False, so only forward when the user set it — this is
    # what makes flag-off byte-identical (sub_argv unchanged when absent).
    if getattr(args, "monotone_certificate_order", False):
        sub_argv.append("--monotone-certificate-order")
    # Issue #4159: forward --no-rescue-pass.  Both parsers declare it as
    # store_true defaulting to False (rescue sweep ON by default), so only
    # forward when the user set it (byte-identical when absent).
    if getattr(args, "no_rescue_pass", False):
        sub_argv.append("--no-rescue-pass")
    # Issue #5520 (Epic #5510, Phase 1b): forward --no-routing-plan.  Both
    # parsers declare it as ``store_false`` onto dest ``routing_plan``
    # (default True, i.e. the report-only plan stage is ON), so only
    # forward when the user turned it OFF -- flag-off argv stays
    # byte-identical (tests/test_cli_parser_drift.py).
    if not getattr(args, "routing_plan", True):
        sub_argv.append("--no-routing-plan")
    # Issue #5521 (Epic #5510, Phase 1c): forward --plan-gate.  Both parsers
    # declare it as ``store_true`` defaulting to False, so only forward when
    # the user set it -- flag-off argv stays byte-identical.  The override
    # (--force) is an existing flag and is forwarded by its own block.
    if getattr(args, "plan_gate", False):
        sub_argv.append("--plan-gate")
    if getattr(args, "cross_package_pair_corridor", False):
        sub_argv.append("--cross-package-pair-corridor")
    if getattr(args, "slack_corridor_widening", False):
        sub_argv.append("--slack-corridor-widening")
    # Issue #4474 (epic #4410): forward --escape-corridor-reservation.  Both
    # parsers declare it as store_true defaulting to False, so only forward
    # when the user set it (byte-identical when absent).
    if getattr(args, "escape_corridor_reservation", False):
        sub_argv.append("--escape-corridor-reservation")
    # Issue #5891 (epic #5508 Phase 2): forward --no-pad-access-invariant.  Both
    # parsers declare it as store_true defaulting to False (the invariant itself
    # is ON by default), so only forward the OPT-OUT when the user set it --
    # argv is byte-identical for a run that did not pass it.
    if getattr(args, "no_pad_access_invariant", False):
        sub_argv.append("--no-pad-access-invariant")
    max_ripups_val = getattr(args, "max_ripups_per_net", None)
    if max_ripups_val is not None:
        sub_argv.extend(["--max-ripups-per-net", str(max_ripups_val)])
    timeout_val = getattr(args, "timeout", None)
    if timeout_val is not None:
        sub_argv.extend(["--timeout", str(timeout_val)])
    # Issue #5266: forward the per-search-stage allocation.  Both parsers
    # default it to None (= "use --timeout as the per-stage cap"), so a run
    # that never passed it stays byte-identical to the pre-#5266 sub-invocation.
    search_timeout_val = getattr(args, "search_timeout", None)
    if search_timeout_val is not None:
        sub_argv.extend(["--search-timeout", str(search_timeout_val)])
    per_net_timeout_val = getattr(args, "per_net_timeout", 30.0)
    if per_net_timeout_val != 30.0:
        sub_argv.extend(["--per-net-timeout", str(per_net_timeout_val)])
    # Issue #5785: forward the oracle completion-loop controls.  Both parsers
    # default --oracle-rounds to None (= env var / built-in default 3) and
    # --allow-stranded-pour-pads to False, so only forward explicit values;
    # an unset run stays byte-identical to the pre-#5785 sub-invocation.
    oracle_rounds_val = getattr(args, "oracle_rounds", None)
    if oracle_rounds_val is not None:
        sub_argv.extend(["--oracle-rounds", str(oracle_rounds_val)])
    if getattr(args, "allow_stranded_pour_pads", False):
        sub_argv.append("--allow-stranded-pour-pads")
    # Issue #2817: forward --checkpoint-interval to the inner parser.  The
    # inner default also lives at 30.0, so only forward when the user passed
    # a non-default value (matches the per-net-timeout pattern above).
    checkpoint_interval_val = getattr(args, "checkpoint_interval", 30.0)
    if checkpoint_interval_val != 30.0:
        sub_argv.extend(["--checkpoint-interval", str(checkpoint_interval_val)])
    # Issue #5945: forward --checkpoint / --resume (both default None).
    if getattr(args, "checkpoint", None):
        sub_argv.extend(["--checkpoint", args.checkpoint])
    if getattr(args, "resume", None):
        sub_argv.extend(["--resume", args.resume])
    # Issue #6054: forward --lint-gate / --lint-gate-waivers / --lint-gate-strict.
    if getattr(args, "lint_gate", False):
        sub_argv.append("--lint-gate")
    if getattr(args, "lint_gate_waivers", None):
        sub_argv.extend(["--lint-gate-waivers", args.lint_gate_waivers])
    if getattr(args, "lint_gate_strict", False):
        sub_argv.append("--lint-gate-strict")
    # Issue #2819: forward --max-search-iterations to the inner parser.
    # Both defaults are 0 (= use cols*rows*4 heuristic), so only forward
    # when the user passed a non-default value (matches the per-net-timeout
    # and checkpoint-interval patterns above).
    max_iter_val = getattr(args, "max_search_iterations", 0) or 0
    if max_iter_val:
        sub_argv.extend(["--max-search-iterations", str(max_iter_val)])
    # Issue #3881: forward --per-net-iterations to the inner parser.  Default
    # is 0 (= unset), so only forward a non-default value (matches the
    # --max-search-iterations pattern above).  Under --deterministic-budget the
    # inner normalization defaults it, so an unset value still becomes the tuned
    # cap inside route_cmd.
    per_net_iter_val = getattr(args, "per_net_iterations", 0) or 0
    if per_net_iter_val:
        sub_argv.extend(["--per-net-iterations", str(per_net_iter_val)])
    # Issue #3538: forward --deterministic-budget to the inner parser.  Both
    # sites declare it as store_true defaulting to False, so only forward when
    # the user passed it (matches the --targeted-ripup pattern above).  The
    # inner ``route_cmd._normalize_deterministic_budget`` then disables the
    # per-net wall-clock cutoff and pins the iteration backstop.
    if getattr(args, "deterministic_budget", False):
        sub_argv.append("--deterministic-budget")
    # Issue #5870: forward the relief-rescue opt-in the same way.
    if getattr(args, "deterministic_rescue", False):
        sub_argv.append("--deterministic-rescue")
    if args.verbose:
        sub_argv.append("--verbose")
    if args.dry_run:
        sub_argv.append("--dry-run")
    # Use command-level quiet or global quiet
    if getattr(args, "quiet", False) or getattr(args, "global_quiet", False):
        sub_argv.append("--quiet")
    if getattr(args, "power_nets", None):
        sub_argv.extend(["--power-nets", args.power_nets])
    if getattr(args, "auto_pour", None) is not None:
        sub_argv.append("--auto-pour" if args.auto_pour else "--no-auto-pour")
    if getattr(args, "strict_pad_clearance", False):
        sub_argv.append("--strict-pad-clearance")
    if getattr(args, "layers", "auto") != "auto":
        sub_argv.extend(["--layers", args.layers])
    if getattr(args, "force", False):
        sub_argv.append("--force")
    if getattr(args, "allow_unsafe_grid", False):
        sub_argv.append("--allow-unsafe-grid")
    if getattr(args, "no_optimize", False):
        sub_argv.append("--no-optimize")
    # Issue #4732: advisory per-stage routing-quality instrumentation.
    if getattr(args, "report_stage_quality", False):
        sub_argv.append("--report-stage-quality")
    # Issue #2388: --auto-layers is now enabled by default.  Forward only
    # the user's explicit choice (so the default takes effect when neither
    # is passed and --no-auto-layers is honored when disabled).
    # Issue #4502: the outer parser is tri-state (default=None), so an
    # explicit ``--auto-layers`` is now distinguishable from "unset" and
    # must be forwarded literally -- the inner argv-sniffing sites
    # (_apply_complete_mode_defaults, and the --auto-layers/--layers
    # conflict check) key off the token being present in this sub_argv.
    auto_layers_attr = getattr(args, "auto_layers", None)
    if auto_layers_attr is False:
        sub_argv.append("--no-auto-layers")
    elif auto_layers_attr is True:
        sub_argv.append("--auto-layers")
    # None (unset): forward nothing; the inner parser's own default=True applies.
    if getattr(args, "max_layers", 6) != 6:
        sub_argv.extend(["--max-layers", str(args.max_layers)])
    # Issue #3400: forward --starting-layers when explicitly supplied.
    # The outer parser stores ``None`` when the user did not pass the flag;
    # only forward a concrete value so the inner dispatcher's CLI > spec >
    # default precedence is preserved.
    if getattr(args, "starting_layers", None) is not None:
        sub_argv.extend(["--starting-layers", str(args.starting_layers)])
    if getattr(args, "min_completion", 0.95) != 0.95:
        sub_argv.extend(["--min-completion", str(args.min_completion)])
    if getattr(args, "adaptive_rules", False):
        sub_argv.append("--adaptive-rules")
    # Issue #2881: --auto-mfr-tier / --mfr-tier-ladder forwarding.  Both
    # are opt-in (default off / None) so we forward only when set.
    if getattr(args, "auto_mfr_tier", False):
        sub_argv.append("--auto-mfr-tier")
    if getattr(args, "mfr_tier_ladder", None):
        sub_argv.extend(["--mfr-tier-ladder", args.mfr_tier_ladder])
    # Issue #3352 (P_AS5): forward --auto-pcb-size to the inner parser.
    # Opt-in (default off); when set the inner dispatcher implies
    # --auto-layers per Q5 of the architect proposal.
    if getattr(args, "auto_pcb_size", False):
        sub_argv.append("--auto-pcb-size")
    # Issue #3403: forward --packing-overhead when set (None means "use
    # policy default", so we only forward an explicit override).
    packing_overhead_attr = getattr(args, "packing_overhead", None)
    if packing_overhead_attr is not None:
        sub_argv.extend(["--packing-overhead", str(packing_overhead_attr)])
    if getattr(args, "min_trace", None) is not None:
        sub_argv.extend(["--min-trace", str(args.min_trace)])
    if getattr(args, "min_clearance_floor", None) is not None:
        sub_argv.extend(["--min-clearance-floor", str(args.min_clearance_floor)])
    if getattr(args, "manufacturer", "jlcpcb") != "jlcpcb" or _explicit_manufacturer:
        sub_argv.extend(["--manufacturer", args.manufacturer])
    # Issue #4700: the copper weight selects the manufacturer design-rule row
    # (``<layers>layer_<oz>oz``) the trace-width floor is resolved from, so it
    # must reach the inner command verbatim.
    if getattr(args, "copper", None) is not None:
        sub_argv.extend(["--copper", str(args.copper)])
    if getattr(args, "high_performance", False):
        sub_argv.append("--high-performance")
    if getattr(args, "skip_drc", False):
        sub_argv.append("--skip-drc")
    # Issue #4178: forward --strict-drc so a native DRC that did not run
    # becomes a hard failure.  Defaults off; forward only when set so the
    # flag-off path stays byte-identical.
    if getattr(args, "strict_drc", False):
        sub_argv.append("--strict-drc")
    # Issue #4433: forward --strict-layers so per-net avoid_layers is enforced
    # as a hard constraint.  Defaults off; forward only when set so the flag-off
    # path stays byte-identical.
    if getattr(args, "strict_layers", False):
        sub_argv.append("--strict-layers")
    # Issue #5014: forward --reserve-plane-layers so a controlled-impedance
    # recipe's declared reference planes are hard-excluded from the routable
    # set. Issue #5789: the flag now defaults to True on BOTH the outer and
    # inner parsers, so the "unset" path already matches (no forwarding
    # needed); only an explicit opt-out (--no-reserve-plane-layers) needs to
    # cross the shim, mirroring the --no-sync-check forwarding pattern below.
    if getattr(args, "reserve_plane_layers", True) is False:
        sub_argv.append("--no-reserve-plane-layers")
    # Issue #3154: forward the advisory drift-banner flags.  --sync-check
    # defaults on; forward --no-sync-check only when explicitly disabled.
    if getattr(args, "sync_check", True) is False:
        sub_argv.append("--no-sync-check")
    if getattr(args, "schematic", None):
        sub_argv.extend(["--schematic", args.schematic])
    # Issue #4156: forward the off-board placement preflight escape hatch.
    # --allow-offboard defaults off; forward only when the user set it so the
    # flag-off path stays byte-identical.
    if getattr(args, "allow_offboard", False):
        sub_argv.append("--allow-offboard")
    # Issue #4799: forward the pre-route census advisory report path.  Defaults
    # to None (the inner preflight then falls back to
    # KCT_CROSSTAIL_CENSUS_ADVISORY); forward only when the user set it so the
    # flag-off path stays byte-identical.
    if getattr(args, "census_advisory", None):
        sub_argv.extend(["--census-advisory", str(args.census_advisory)])
    # Issue #4799: forward the opt-in go/no-go gate and its threshold.  Both
    # default off/None; forwarded only when set so the flag-off path stays
    # byte-identical to the advisory-only behaviour.
    if getattr(args, "census_advisory_gate", False):
        sub_argv.append("--census-advisory-gate")
    if getattr(args, "census_advisory_gate_pct", None) is not None:
        sub_argv.extend(["--census-advisory-gate-pct", str(args.census_advisory_gate_pct)])
    # Issue #4799: forward the placement-only escape-capacity forecast.  Both
    # default off/None; forwarded only when set so an un-flagged route is
    # byte-identical.
    if getattr(args, "capacity_forecast", False):
        sub_argv.append("--capacity-forecast")
    if getattr(args, "capacity_forecast_json", None):
        sub_argv.extend(["--capacity-forecast-json", str(args.capacity_forecast_json)])
    if getattr(args, "auto_fix", False):
        sub_argv.append("--auto-fix")
    if getattr(args, "auto_fix_passes", None) is not None:
        sub_argv.extend(["--auto-fix-passes", str(args.auto_fix_passes)])
    # Issue #2595: forward placement-feedback flags.
    if getattr(args, "placement_feedback", False):
        sub_argv.append("--placement-feedback")
    if getattr(args, "placement_feedback_budget", 3) != 3:
        sub_argv.extend(["--placement-feedback-budget", str(args.placement_feedback_budget)])
    if getattr(args, "placement_feedback_max_movement", 5.0) != 5.0:
        sub_argv.extend(
            [
                "--placement-feedback-max-movement",
                str(args.placement_feedback_max_movement),
            ]
        )
    if getattr(args, "placement_feedback_anchor", None):
        sub_argv.extend(["--placement-feedback-anchor", args.placement_feedback_anchor])
    if getattr(args, "placement_feedback_no_anchor", None):
        sub_argv.extend(
            [
                "--placement-feedback-no-anchor",
                args.placement_feedback_no_anchor,
            ]
        )
    # Issue #2606: forward stagnation + outer-timeout flags only when
    # set to a non-default value so the "boards 01-05 produce identical
    # routes" invariant holds when --placement-feedback is off.
    if getattr(args, "placement_feedback_stagnation_patience", 3) != 3:
        sub_argv.extend(
            [
                "--placement-feedback-stagnation-patience",
                str(args.placement_feedback_stagnation_patience),
            ]
        )
    if getattr(args, "placement_feedback_outer_timeout", None) is not None:
        sub_argv.extend(
            [
                "--placement-feedback-outer-timeout",
                str(args.placement_feedback_outer_timeout),
            ]
        )
    # Issue #4468: forward the classifier-driven placement-DELTA feedback
    # flags.  Budget is forwarded only when non-default so a run that never
    # asked for the loop stays byte-identical to the pre-#4468 sub-invocation.
    #
    # Issue #5890: the toggle is now TRI-state -- ``None`` means "auto, decided
    # by the routing plan's feasibility".  Forward only an EXPLICIT choice, in
    # whichever direction it was made; ``None`` forwards nothing so the
    # sub-invocation reaches the same auto default this one did.
    _delta_feedback_choice = getattr(args, "placement_delta_feedback", None)
    if _delta_feedback_choice is True:
        sub_argv.append("--placement-delta-feedback")
    elif _delta_feedback_choice is False:
        sub_argv.append("--no-placement-delta-feedback")
    if getattr(args, "placement_delta_feedback_budget", 3) != 3:
        sub_argv.extend(
            [
                "--placement-delta-feedback-budget",
                str(args.placement_delta_feedback_budget),
            ]
        )
    if getattr(args, "placement_delta_feedback_timeout", None) is not None:
        sub_argv.extend(
            [
                "--placement-delta-feedback-timeout",
                str(args.placement_delta_feedback_timeout),
            ]
        )
    if getattr(args, "export_failed_nets", None):
        sub_argv.extend(["--export-failed-nets", args.export_failed_nets])
    if getattr(args, "no_cache", False):
        sub_argv.append("--no-cache")
    if getattr(args, "backend", "auto") != "auto":
        sub_argv.extend(["--backend", args.backend])
    # Issue #4268: forward --route-engine only when non-default so a default
    # (grid) run stays byte-identical to the pre-mesh sub-invocation.
    if getattr(args, "route_engine", "grid") != "grid":
        sub_argv.extend(["--route-engine", args.route_engine])
    # Issue #4318: forward --lattice-optimize (opt-in geometric post-passes on
    # non-grid copper).  Both parsers declare it as store_true defaulting to
    # False, so only forward when the user set it -- flag-off is byte-identical.
    if getattr(args, "lattice_optimize", False):
        sub_argv.append("--lattice-optimize")
    # Issue #2589: forward --seed for deterministic runs.  Default is None
    # (router uses os.urandom-derived state, existing behaviour).
    if getattr(args, "seed", None) is not None:
        sub_argv.extend(["--seed", str(args.seed)])
    # Issue #3897: forward --order-method to the inner parser.  Default is
    # None (use the internal priority-based net ordering, existing behaviour),
    # so only forward an explicitly-provided value (matches the --seed pattern).
    if getattr(args, "order_method", None) is not None:
        sub_argv.extend(["--order-method", str(args.order_method)])
    # Issue #5894: forward --ripup-strategy to the inner parser.  Default is
    # "negotiated" (existing whole-set loop, unchanged), so only forward an
    # explicitly-provided non-default value (matches the --order-method
    # pattern above -- this outer->inner forwarding list is the THIRD site
    # (besides parser.py and route_cmd.py's _route_parser) that must know
    # about a new route flag, per tests/test_cli_parser_drift.py).
    if getattr(args, "ripup_strategy", "negotiated") != "negotiated":
        sub_argv.extend(["--ripup-strategy", str(args.ripup_strategy)])
    # Issue #5944: forward --diagnose-unrouted-budget only when it differs
    # from the default (20 s), matching the --ripup-strategy pattern above.
    if getattr(args, "diagnose_unrouted_budget", 20.0) != 20.0:
        sub_argv.extend(["--diagnose-unrouted-budget", str(args.diagnose_unrouted_budget)])
    # Issue #3054 (Phase 2 of #3045): forward --region-parallel and partition
    # tuning flags to the inner parser.  All four flags are opt-in and only
    # forwarded when set to non-default values, so existing scripts using
    # ``kct route`` without these flags produce byte-identical output.
    # Issue #4148: forward --region (SPATIAL routing bound).  Distinct from
    # --region-parallel above.  Default None; only forward when the user set
    # it so byte-identical output is preserved when absent.
    if getattr(args, "region", None) is not None:
        sub_argv.extend(["--region", str(args.region)])
    if getattr(args, "region_parallel", False):
        sub_argv.append("--region-parallel")
    if getattr(args, "partition_rows", 2) != 2:
        sub_argv.extend(["--partition-rows", str(args.partition_rows)])
    if getattr(args, "partition_cols", 2) != 2:
        sub_argv.extend(["--partition-cols", str(args.partition_cols)])
    if getattr(args, "max_parallel_workers", 4) != 4:
        sub_argv.extend(["--max-parallel-workers", str(args.max_parallel_workers)])
    if getattr(args, "strict", False):
        sub_argv.append("--strict")
    # Issue #3033 / #3062: Forward --strict-in-pad-clearance opt-in to the
    # inner route command.  The inner main() sets the
    # KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE env var so EscapeRouter consumes
    # it on lazy construction.  Outer parser uses the
    # ``route_strict_in_pad_clearance`` dest, inner uses
    # ``strict_in_pad_clearance``; check both for forward-compat.
    if getattr(args, "route_strict_in_pad_clearance", False) or getattr(
        args, "strict_in_pad_clearance", False
    ):
        sub_argv.append("--strict-in-pad-clearance")
    # Issue #3118: Forward the --micro-via-in-pad-fallback triplet to the
    # inner route command.  The inner main() sets the
    # KICAD_TOOLS_MICRO_VIA_IN_PAD_FALLBACK env var (plus size/drill) so
    # EscapeRouter consumes them on lazy construction.  Outer parser uses
    # ``route_micro_via_in_pad_fallback`` dest, inner uses
    # ``micro_via_in_pad_fallback``; check both for forward-compat.
    if getattr(args, "route_micro_via_in_pad_fallback", False) or getattr(
        args, "micro_via_in_pad_fallback", False
    ):
        sub_argv.append("--micro-via-in-pad-fallback")
        sub_argv.extend(
            [
                "--micro-via-size",
                str(
                    getattr(
                        args,
                        "route_micro_via_size",
                        getattr(args, "micro_via_size", 0.3),
                    )
                ),
            ]
        )
        sub_argv.extend(
            [
                "--micro-via-drill",
                str(
                    getattr(
                        args,
                        "route_micro_via_drill",
                        getattr(args, "micro_via_drill", 0.15),
                    )
                ),
            ]
        )
    # Issue #4475 (epic #4465 Phase 3): forward --via-in-pad-last-resort to
    # the inner route command.  The inner main() sets
    # DesignRules.via_in_pad_last_resort so the lattice engine stages a
    # same-net via-in-pad attach as a last resort instead of the #4284
    # opportunistic tier-only gate.  Outer parser uses the
    # ``route_via_in_pad_last_resort`` dest, inner uses
    # ``via_in_pad_last_resort``; check both for forward-compat.
    if getattr(args, "route_via_in_pad_last_resort", False) or getattr(
        args, "via_in_pad_last_resort", False
    ):
        sub_argv.append("--via-in-pad-last-resort")
    # Issue #2464: Forward differential pair routing flags
    if getattr(args, "differential_pairs", False):
        sub_argv.append("--differential-pairs")
    if getattr(args, "diffpair_spacing", None) is not None:
        sub_argv.extend(["--diffpair-spacing", str(args.diffpair_spacing)])
    if getattr(args, "diffpair_max_delta", None) is not None:
        sub_argv.extend(["--diffpair-max-delta", str(args.diffpair_max_delta)])
    # Issue #3275: forward --diffpair-per-pair-timeout (Issue #3089
    # per-pair budget) so callers can bound the CoupledPathfinder's
    # coupled A* search on dense BGA/QFN escape geometry.  Both outer
    # and inner default to None; only forward when the user set it.
    if getattr(args, "diffpair_per_pair_timeout", None) is not None:
        sub_argv.extend(["--diffpair-per-pair-timeout", str(args.diffpair_per_pair_timeout)])
    # Issue #2648 (Epic #2556 Phase 3I): forward the length-match-diffpairs flag
    if getattr(args, "length_match_diffpairs", False):
        sub_argv.append("--length-match-diffpairs")
    # Issue #2723 (Epic #2661 Phase 3H): forward the length-match-groups flag
    if getattr(args, "length_match_groups", False):
        sub_argv.append("--length-match-groups")
    # Issue #2996: forward --net-class-map sidecar path so rich
    # NetClassRouting fields (intra_pair_clearance, etc.) merge into the
    # autorouter's net_class_map at routing time.
    if getattr(args, "net_class_map", None) is not None:
        sub_argv.extend(["--net-class-map", args.net_class_map])
    # Issue #4980: forward the --current-paths sidecar (and its suppression
    # flag) so the inner route_cmd loads the declared branch current-path
    # intent, runs path_ampacity in the post-route DRC, and re-emits the
    # declarations next to the routed board for kct check auto-discovery.
    # Both parsers declare both flags (tests/test_cli_parser_drift.py); only
    # forward when set so the flag-off argv stays byte-identical.
    if getattr(args, "current_paths", None) is not None:
        sub_argv.extend(["--current-paths", args.current_paths])
    if getattr(args, "no_current_paths", False):
        sub_argv.append("--no-current-paths")
    # Issue #3171 (Phase 3): forward the analog-aware routing flags so the
    # inner route_cmd injects the boosted analog NetClassRouting class.
    if getattr(args, "analog_nets", None):
        sub_argv.extend(["--analog-nets", args.analog_nets])
    if getattr(args, "auto_analog", False):
        sub_argv.append("--auto-analog")
    # Issue #4431 (Phase 1): forward --voltage-map + the creepage lookup knobs
    # so the inner route_cmd builds the HV pairwise-clearance table.  Both
    # parsers declare each; the creepage knobs share the inner defaults
    # (iec60664 / 2 / IIIa / 30.0), so only forward non-defaults to keep
    # flag-off argv byte-identical (enforced by tests/test_cli_parser_drift.py).
    if getattr(args, "voltage_map", None) is not None:
        sub_argv.extend(["--voltage-map", args.voltage_map])
        if getattr(args, "creepage_standard", "iec60664") != "iec60664":
            sub_argv.extend(["--creepage-standard", args.creepage_standard])
        if getattr(args, "pollution_degree", 2) != 2:
            sub_argv.extend(["--pollution-degree", str(args.pollution_degree)])
        if getattr(args, "material_group", "IIIa") != "IIIa":
            sub_argv.extend(["--material-group", args.material_group])
        if getattr(args, "hv_threshold", 30.0) != 30.0:
            sub_argv.extend(["--hv-threshold", str(args.hv_threshold)])
    if getattr(args, "format", "text") != "text":
        sub_argv.extend(["--format", args.format])
    return route_main(sub_argv)


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
