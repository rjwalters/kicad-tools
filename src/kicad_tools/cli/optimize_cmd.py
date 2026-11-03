"""
CLI command for trace optimization.

Usage:
    kct optimize-traces board.kicad_pcb
    kct optimize-traces board.kicad_pcb --net "NET8"
    kct optimize-traces board.kicad_pcb -o optimized.kicad_pcb
    kct optimize-traces board.kicad_pcb --drc-aware --mfr jlcpcb --layers 4

Machine output (``--format json``, issue #4674): one deterministic document
describing the run -- the resolved ``pcb``/``output`` paths, the enabled
``optimizations``, the before/after ``stats`` and whether the result was
``saved`` -- or ``{"error": ..., "success": false}`` on any failure, with the
exit code unchanged.  See ``docs/reference/machine-output.md``.
"""

import argparse
import sys
from pathlib import Path

from kicad_tools.cli.format_options import FORMAT_JSON, add_format_flag, emit_json


def _fail(as_json: bool, pcb: str, message: str, *, text: str | None = None) -> int:
    """Report an optimize-traces failure as a document (JSON) or prose (text).

    ``text`` overrides the conventional ``Error: <message>`` prose for the one
    failure path that deliberately words itself differently (``Error during
    optimization: ...``); text-mode output stays byte-identical.
    """
    if as_json:
        emit_json(
            {
                "command": "optimize-traces",
                "pcb": pcb,
                "error": message,
                "saved": False,
                "success": False,
            }
        )
    else:
        print(text if text is not None else f"Error: {message}", file=sys.stderr)
    return 1


def _refill_written_board(written: Path) -> tuple[bool, str | None]:
    """Refill zones in the written board; never raises (Issue #8286).

    Returns ``(refilled, error)``; ``error`` is None on success.
    """
    from kicad_tools.cli.runner import run_refill_zones

    try:
        result = run_refill_zones(written)
    except Exception as e:  # degrade like check_cmd: warn, exit code unchanged
        return False, str(e)
    if result.success:
        return True, None
    return False, result.stderr.strip() or "unknown error"


def _build_collision_checker(pcb_path: Path):
    """Build a collision checker plus a grid-synced optimize function factory.

    Loads the board (existing routes included, netclass / ``.kicad_dru``
    clearances honoured by the loader) and returns ``(router, checker)``: the
    shared checker the router uses, so each optimizer rewrite is checked
    before it is accepted (Issue #8214; one clearance kernel per #5509).  The
    loader's progress lines go to stderr because ``--format json`` owns stdout.
    """
    import contextlib

    from kicad_tools.router.io import load_pcb_for_routing
    from kicad_tools.router.optimizer import make_collision_checker
    from kicad_tools.router.optimizer.collision import CollisionChecker

    with contextlib.redirect_stdout(sys.stderr):
        router, _net_map = load_pcb_for_routing(
            str(pcb_path),
            rules=_declared_design_rules(pcb_path),
            router_class_floors=False,
            validate_drc=False,
            load_existing_routes=True,
        )
    # Issue #8250: rule-area keepouts are installed lazily by the route
    # flows, never at load, so install them before the checker reads the grid.
    router._install_grid_rule_area_keepouts()
    checker: CollisionChecker = make_collision_checker(router.grid)

    # Issue #8250: a bare NPTH registers no copper, so give the checker the
    # drilled holes explicitly.  0.25 mm is KiCad's default hole_clearance.
    holes = [
        (p.x, p.y, p.drill / 2.0)
        for p in router.pads.values()
        if p.drill > 0 and not p.through_hole
    ]
    if holes:
        from kicad_tools.router.optimizer.collision import HoleClearanceChecker

        clearance = max(float(getattr(router.rules, "trace_clearance", 0.0)), 0.25)
        checker = HoleClearanceChecker(checker, holes, clearance)
    return router, checker


def _declared_design_rules(pcb_path: Path):
    """The board's ``DesignRules`` with the clearance its own DRC enforces (#8269).

    ``load_pcb_for_routing`` reads clearance only from the ``.kicad_pcb``.  A
    KiCad 6+ board keeps its netclasses in the ``.kicad_pro`` (and custom rules
    in the ``.kicad_dru``), so the loader fell back to the 0.20 mm dataclass
    default: the checker then held every rewrite to 0.20 mm on a 0.1016 mm
    board, refusing moves that stay on existing copper.  The optimizer checks
    rewrites against the *requirement*, so the base is resolved with a zero
    target -- the project, DRU and ``project.kct`` fab floor, never raised by
    a router spacing preference.  The project's stricter named netclasses
    still become per-net floors in the loader (#6243); the router's own
    name-based classes do not (``router_class_floors=False``), because KiCad
    does not enforce them on a board whose files do not declare them.
    Returns ``None`` (the loader's own behaviour) when the board declares
    nothing.
    """
    from dataclasses import replace

    from kicad_tools.router.clearance_resolver import resolve_routing_clearances
    from kicad_tools.router.io import parse_pcb_design_rules

    resolved = resolve_routing_clearances(pcb_path, target_mm=0.0)
    trace_mm = resolved.trace.required_mm
    via_mm = resolved.via.required_mm
    if trace_mm <= 0 or via_mm <= 0:
        return None
    rules = parse_pcb_design_rules(Path(pcb_path).read_text()).to_design_rules()
    return replace(
        rules,
        trace_clearance=trace_mm,
        via_clearance=via_mm,
        grid_resolution=min(rules.grid_resolution, trace_mm / 2),
    )


def _seg_key(seg) -> tuple:
    return (seg.x1, seg.y1, seg.x2, seg.y2, seg.width, seg.layer, seg.net)


def _make_grid_synced_optimize_fn(router, optimizer):
    """Return ``(optimize, commit)`` wrapping ``optimizer.optimize_segments``.

    * ``optimize``: net identity (Issue #8214 review).  The file parser yields
      net id 0 for every segment on a KiCad-10 name-only board, while the grid
      carries the loader's dialect-normalized ids.  The optimizer excludes
      ``seg.net`` as "own net", so the segments are re-keyed to the grid id
      (looked up by net *name*) for the optimization and restored afterwards.
      It does **not** touch the grid.
    * ``commit(net_name, original, kept)``: static grid (Issue #8214 review).
      Nets are optimized in turn, so net B must be checked against net A's
      *kept* rewrite.  ``optimize_pcb`` calls this once per net after the
      Kelvin gate has accepted or vetoed branch rewrites, so the grid follows
      the copper that is actually written, not what ``optimize`` returned.
      The old copper is unmarked and the new copper marked (the transaction
      ``apply_route_transform_grid_synced`` uses).

    ``--drc-aware`` rollback runs after the per-net loop and is not reflected
    in the grid; its DRC re-check backstops that.
    """
    from dataclasses import replace

    grid = router.grid
    base_optimize = optimizer.optimize_segments
    by_name = {r.net_name: i for i, r in enumerate(router.existing_routes) if r.net > 0}

    def optimize(segs):
        if not segs:
            return base_optimize(segs)
        idx = by_name.get(segs[0].net_name)
        if idx is None:
            # Net has no copper in the grid (or neutralised ownership).
            return base_optimize(segs)
        gid = router.existing_routes[idx].net
        out = base_optimize([replace(s, net=gid) for s in segs])
        return [replace(s, net=segs[0].net) for s in out]

    def commit(net_name, original, kept):
        idx = by_name.get(net_name)
        if idx is None or not original:
            return
        old = router.existing_routes[idx]
        gid = old.net
        work = [replace(s, net=gid) for s in original]
        final = [replace(s, net=gid) for s in kept]
        if [_seg_key(s) for s in final] == [_seg_key(s) for s in work]:
            return
        gone = {_seg_key(s) for s in work}
        rest = [s for s in old.segments if _seg_key(s) not in gone]
        new = replace(old, segments=rest + final)
        grid.unmark_route(old)
        grid.mark_route(new)
        grid._mark_route_on_cpp_cells(new)
        grid.resync_route_occupancy([(old, new)])
        router.existing_routes[idx] = new

    return optimize, commit


def main(argv: list[str] | None = None) -> int:
    """Main entry point for trace optimization CLI."""
    parser = argparse.ArgumentParser(
        prog="kct optimize-traces",
        description="Optimize PCB traces to minimize bends and reduce segment count",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    kct optimize-traces board.kicad_pcb
    kct optimize-traces board.kicad_pcb --net USB_D+
    kct optimize-traces board.kicad_pcb -o optimized.kicad_pcb --no-45
    kct optimize-traces board.kicad_pcb --dry-run
    kct optimize-traces board.kicad_pcb --drc-aware --mfr jlcpcb --layers 4
""",
    )

    parser.add_argument(
        "pcb",
        help="Input PCB file (.kicad_pcb)",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output PCB file (default: modify in place)",
    )
    parser.add_argument(
        "--net",
        help="Only optimize traces for nets matching this pattern",
    )
    parser.add_argument(
        "--no-merge",
        action="store_true",
        help="Disable collinear segment merging",
    )
    parser.add_argument(
        "--no-zigzag",
        action="store_true",
        help="Disable zigzag elimination",
    )
    parser.add_argument(
        "--no-45",
        action="store_true",
        help="Disable 45-degree corner conversion",
    )
    parser.add_argument(
        "--no-jogs",
        action="store_true",
        help="Disable lateral jog removal (sliding short sideways steps to the next corner)",
    )
    parser.add_argument(
        "--chamfer-size",
        type=float,
        default=0.5,
        help="Size of 45-degree chamfer in mm (default: 0.5)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show optimization results without writing output",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed per-net statistics",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output (for scripting)",
    )

    # DRC-aware mode arguments
    parser.add_argument(
        "--drc-aware",
        action="store_true",
        help=(
            "Enable DRC-aware mode: roll back per-net optimizations that increase violations. "
            "Gated checks: clearances, dimensions, and trace/via/pad vs foreign-net zone fill; "
            "growth in pad or short categories forces rollback, and if baseline cannot be "
            "restored all changes are reverted"
        ),
    )
    parser.add_argument(
        "--mfr",
        help="Target manufacturer for DRC rules (e.g., jlcpcb, oshpark). Required with --drc-aware",
    )
    parser.add_argument(
        "--layers",
        type=int,
        default=2,
        help="Number of copper layers for DRC checks (default: 2)",
    )
    parser.add_argument(
        "--copper",
        type=float,
        default=1.0,
        help="Copper weight in oz for DRC checks (default: 1.0)",
    )

    # HV creepage inputs (Issue #8300): same flags/defaults as ``kct route``.
    parser.add_argument(
        "--voltage-map",
        dest="voltage_map",
        default=None,
        metavar="FILE",
        help=(
            "JSON per-net voltage map (same format and semantics as kct route). "
            "A candidate edit that would bring a net pair below its pairwise "
            "creepage requirement is rejected on its own; other edits on the "
            "net are still applied. Absent, behaviour is unchanged"
        ),
    )
    parser.add_argument(
        "--creepage-standard",
        dest="creepage_standard",
        choices=["iec60664", "iec62368"],
        default="iec60664",
        help="Creepage standard for --voltage-map (default: iec60664)",
    )
    parser.add_argument(
        "--pollution-degree",
        dest="pollution_degree",
        type=int,
        choices=[1, 2, 3],
        default=2,
        help="IEC pollution degree for --voltage-map (default: 2)",
    )
    parser.add_argument(
        "--material-group",
        dest="material_group",
        default="IIIa",
        help="Insulation material group I/II/IIIa/IIIb for --voltage-map (default: IIIa)",
    )
    parser.add_argument(
        "--hv-threshold",
        dest="hv_threshold",
        type=float,
        default=30.0,
        metavar="VOLTS",
        help="Minimum net-pair |dV| that triggers creepage widening (default: 30.0)",
    )
    add_format_flag(parser)

    args = parser.parse_args(argv)
    as_json = args.format == FORMAT_JSON

    # Validate DRC-aware arguments
    if args.drc_aware and not args.mfr:
        return _fail(
            as_json,
            args.pcb,
            "--drc-aware requires --mfr to specify the manufacturer profile (e.g., --mfr jlcpcb)",
        )

    # Check input file exists
    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        return _fail(as_json, str(pcb_path), f"PCB file not found: {pcb_path}")

    # Validate manufacturer ID if provided
    if args.mfr:
        from kicad_tools.manufacturers import get_manufacturer_ids

        valid_ids = get_manufacturer_ids()
        if args.mfr not in valid_ids:
            return _fail(
                as_json,
                str(pcb_path),
                f"Unknown manufacturer '{args.mfr}'. Valid options: {', '.join(valid_ids)}",
            )

    # Issue #8300: validate --voltage-map before any board work, with the same
    # messages and exit code as ``kct route``.  Its errors go to stderr as
    # prose; JSON mode additionally owns stdout, so report a document there.
    if args.voltage_map is not None:
        import contextlib
        import io

        from kicad_tools.cli.route_cmd import _preload_voltage_map

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = _preload_voltage_map(args)
        if rc != 0:
            msg = err.getvalue().strip()
            if as_json:
                return _fail(True, str(pcb_path), msg.removeprefix("Error: "))
            print(msg, file=sys.stderr)
            return rc
        if err.getvalue():  # warnings (e.g. unmapped pair-bound nets)
            print(err.getvalue(), end="", file=sys.stderr)
    else:
        args._pairwise_voltages = None
        args._pairwise_required = None

    # Import here to avoid circular imports
    from kicad_tools.cli.progress import spinner
    from kicad_tools.router.optimizer import (
        OptimizationConfig,
        TraceOptimizer,
    )

    # JSON mode owns stdout: the banner, the spinner and the results table are
    # all suppressed so exactly one document is printed.
    quiet = args.quiet or as_json

    # Configure optimizer
    config = OptimizationConfig(
        merge_collinear=not args.no_merge,
        eliminate_zigzags=not args.no_zigzag,
        convert_45_corners=not args.no_45,
        remove_jogs=not args.no_jogs,
        corner_chamfer_size=args.chamfer_size,
        drc_aware=args.drc_aware,
        drc_manufacturer=args.mfr,
        drc_layers=args.layers,
        drc_copper_oz=args.copper,
    )

    # Issue #8214: collision-check every rewrite against the board's own
    # copper instead of relying on whole-net ``--drc-aware`` rollback.  A grid
    # that cannot be built is reported, never silently downgraded to unchecked
    # rewrites.
    try:
        router, checker = _build_collision_checker(pcb_path)
    except Exception as e:
        return _fail(
            as_json,
            str(pcb_path),
            f"cannot build collision checker for {pcb_path}: {e}",
        )

    # Issue #8300: AND the --voltage-map pairwise requirement onto the checker
    # so each candidate edit is vetted individually.  Dormant (checker
    # untouched) without a voltage map or when it widens no pair.
    pairwise_checker = None
    if args.voltage_map is not None:
        from kicad_tools.cli.route_cmd import _apply_pairwise_clearance
        from kicad_tools.router.optimizer.collision import PairwiseCollisionChecker
        from kicad_tools.router.pairwise_clearance import PairwisePathChecker

        _apply_pairwise_clearance(router, args, quiet=True)
        path_checker = PairwisePathChecker.from_router(router)
        if path_checker is not None:
            pairwise_checker = PairwiseCollisionChecker(checker, path_checker)
            checker = pairwise_checker

    # Issue #8217: the detour pass needs every same-net pad/via of the loaded
    # board (the checker ignores same-net copper); length-matched nets are
    # left out of the map and so skipped.
    from kicad_tools.cli.route_cmd import _detour_net_anchors
    from kicad_tools.router.optimizer.kelvin_gate import KelvinBoardGate

    net_anchors = _detour_net_anchors(router, routes=router.existing_routes)
    # Kelvin nets are skipped like length-matched ones (#8217 AC2): the Kelvin
    # gate vets branch rewrites, but a whole-chain replacement is not its job.
    kelvin = KelvinBoardGate.from_pcb_text(pcb_path.read_text())
    if kelvin is not None:
        kelvin_names = {name for name, nid in kelvin.net_ids.items() if nid in kelvin.gate.kelvin}
        for net_id in [n for n in net_anchors if router.net_names.get(n) in kelvin_names]:
            del net_anchors[net_id]

    optimizer = TraceOptimizer(
        config,
        collision_checker=checker,
        net_anchors=net_anchors,
    )
    # Issue #8250: vias and pads are attachments the 45° pass must not leave
    # dangling, and the pad groups a rewrite must keep joined.
    from kicad_tools.router.optimizer.pcb import parse_vias

    pad_anchors: dict[str, list] = {}
    for pad in router.pads.values():
        if pad.net > 0 and pad.net_name and not pad.steiner_point:
            layers = () if pad.through_hole else (pad.layer,)
            pad_anchors.setdefault(pad.net_name, []).append((pad.x, pad.y, layers))
    attachments: dict[str, list[tuple[float, float]]] = {
        name: [(x, y) for x, y, _ in pts] for name, pts in pad_anchors.items()
    }
    for name, vias in parse_vias(pcb_path.read_text()).items():
        attachments.setdefault(name, []).extend((v.x, v.y) for v in vias)
    optimizer.set_attachment_points(attachments)

    # ``optimize_pcb`` calls ``self.optimize_segments`` per net; route that
    # through the grid-synced wrapper (see its docstring).  optimize_pcb has no
    # optimize_fn hook, so the instance method is deliberately rebound.
    optimizer.optimize_segments, commit_net = _make_grid_synced_optimize_fn(  # type: ignore[method-assign]
        router, optimizer
    )

    if not quiet:
        print("=" * 50)
        print("Trace Optimization")
        print("=" * 50)
        print(f"\nInput:  {pcb_path}")
        if args.output:
            print(f"Output: {args.output}")
        if args.net:
            print(f"Filter: nets matching '{args.net}'")
        if args.drc_aware:
            print(f"DRC:    aware (mfr={args.mfr}, layers={args.layers})")
        print()

        # Show enabled optimizations
        print("Optimizations enabled:")
        print(f"  - Collinear merge: {'yes' if config.merge_collinear else 'no'}")
        print(f"  - Zigzag elimination: {'yes' if config.eliminate_zigzags else 'no'}")
        print(f"  - 45 corners: {'yes' if config.convert_45_corners else 'no'}")
        if config.convert_45_corners:
            print(f"    (chamfer size: {config.corner_chamfer_size}mm)")
        print(f"  - Lateral jog removal: {'yes' if config.remove_jogs else 'no'}")
        if args.drc_aware:
            print(f"  - DRC-aware rollback: yes (mfr={args.mfr})")
        print()

    # Run optimization
    try:
        with spinner("Optimizing traces...", quiet=quiet):
            stats = optimizer.optimize_pcb(
                str(pcb_path),
                output_path=args.output,
                net_filter=args.net,
                dry_run=args.dry_run,
                on_net_optimized=commit_net,
                net_anchors_by_name=pad_anchors,
            )
    except Exception as e:
        return _fail(
            as_json,
            str(pcb_path),
            f"during optimization: {e}",
            text=f"Error during optimization: {e}",
        )

    if pairwise_checker is not None:
        stats.creepage_skips = dict(pairwise_checker.skips)

    # Issue #8286: the rewrite moved copper but the board still carries the
    # zone fills it was read with; ``kct check`` reads stored fills, so refill
    # once, after the final write, through the shared kicad-cli path.
    zones_refilled: bool | None = None
    refill_error: str | None = None
    if not args.dry_run:
        zones_refilled, refill_error = _refill_written_board(Path(args.output or pcb_path))
        # --quiet suppresses progress, not a warning that the written file is
        # inconsistent; JSON mode reports it via ``zones_refilled`` instead.
        if zones_refilled is False and not as_json:
            print(
                "WARNING: zones were NOT refilled "
                f"({refill_error}); the written board carries stale zone fills "
                "and `kct check` may report phantom clearance_segment_zone "
                "errors.  Fix with: kct check --refill-zones, or "
                "kicad-cli pcb drc --refill-zones --save-board <pcb> "
                "(issue #8286).",
                file=sys.stderr,
            )

    if as_json:
        document = {
            "command": "optimize-traces",
            "pcb": str(pcb_path),
            "output": args.output,
            "net_filter": args.net,
            "optimizations": {
                "merge_collinear": config.merge_collinear,
                "eliminate_zigzags": config.eliminate_zigzags,
                "convert_45_corners": config.convert_45_corners,
                "chamfer_size_mm": config.corner_chamfer_size,
            },
            "drc_aware": args.drc_aware,
            "manufacturer": args.mfr,
            "layers": args.layers,
            "copper_oz": args.copper,
            "stats": {
                "nets_optimized": stats.nets_optimized,
                "nets_rolled_back": stats.nets_rolled_back,
                "nets_attempted": stats.nets_optimized,
                "nets_kept": stats.nets_kept,
                "duplicates_removed": stats.duplicates_removed,
                "nets_connectivity_discarded": stats.nets_connectivity_discarded,
                "connectivity_discarded_nets": stats.connectivity_discarded_nets,
                "drc_failsafe_reverted": stats.drc_failsafe_reverted,
                "drc_failsafe_rules": stats.drc_failsafe_rules,
                "segments_before": stats.segments_before,
                "segments_after": stats.segments_after,
                "segment_reduction_pct": stats.segment_reduction,
                "corners_before": stats.corners_before,
                "corners_after": stats.corners_after,
                "length_before_mm": stats.length_before,
                "length_after_mm": stats.length_after,
                "length_reduction_pct": stats.length_reduction,
                "drc_errors_before": stats.drc_errors_before,
                "drc_errors_after": stats.drc_errors_after,
                "per_net": [
                    {
                        "net": n.net,
                        "segments_before": n.segments_before,
                        "segments_after": n.segments_after,
                        "length_before_mm": n.length_before,
                        "length_after_mm": n.length_after,
                        "rolled_back": n.rolled_back,
                        "new_violations": n.new_violations,
                    }
                    for n in stats.per_net
                ],
            },
            "dry_run": args.dry_run,
            "saved": not args.dry_run,
            "written_to": None if args.dry_run else (args.output or str(pcb_path)),
            "zones_refilled": zones_refilled,
            "zones_refill_error": refill_error,
            "success": True,
        }
        # Issue #8300: only present with --voltage-map, so the no-flag document
        # is unchanged.
        if args.voltage_map is not None:
            document["voltage_map"] = args.voltage_map
            document["creepage_skipped"] = [
                {"net_a": a, "net_b": b, "count": n}
                for (a, b), n in sorted(stats.creepage_skips.items())
            ]
        emit_json(document)
        return 0

    if not quiet:
        # Display results
        print("-" * 50)
        print("Results:")
        print("-" * 50)

        # Show DRC-aware net stats
        if args.drc_aware:
            print(
                f"  Nets attempted:  {stats.nets_optimized} "
                f"(kept: {stats.nets_kept}, rolled back: {stats.nets_rolled_back})"
            )
        else:
            print(f"  Nets optimized:  {stats.nets_optimized}")
        if stats.nets_connectivity_discarded:
            print(
                f"  Discarded (would disconnect pads): {stats.nets_connectivity_discarded} "
                f"({', '.join(stats.connectivity_discarded_nets[:5])}"
                f"{', ...' if stats.nets_connectivity_discarded > 5 else ''})"
            )
        print()

        print(
            f"  Segments:        {stats.segments_before:>6} -> {stats.segments_after:>6}  "
            f"({-stats.segment_reduction:+.1f}%)"
        )
        print(f"  Corners:         {stats.corners_before:>6} -> {stats.corners_after:>6}")
        print(f"  Duplicates removed: {stats.duplicates_removed}")
        print(
            f"  Total length:    {stats.length_before:>6.1f}mm -> {stats.length_after:>6.1f}mm  "
            f"({-stats.length_reduction:+.1f}%)"
        )

        if args.drc_aware:
            delta = stats.drc_errors_after - stats.drc_errors_before
            if delta > 0:
                suffix = f"({delta} new errors, {stats.nets_rolled_back} nets rolled back)"
            elif stats.nets_rolled_back > 0:
                suffix = f"(no regressions, {stats.nets_rolled_back} nets rolled back)"
            else:
                suffix = "(no regressions)"
            print(
                f"  DRC errors:      {stats.drc_errors_before:>6} -> "
                f"{stats.drc_errors_after:>6}  {suffix}"
            )
            if stats.drc_failsafe_reverted:
                print(
                    "  WARNING: per-net rollback could not restore the DRC baseline; "
                    "all changed nets were reverted"
                )
                if stats.drc_failsafe_rules:
                    rules = ", ".join(
                        f"{r} (+{c})" for r, c in sorted(stats.drc_failsafe_rules.items())
                    )
                    print(f"           rules above baseline: {rules}")
        if args.voltage_map is not None:
            total = sum(stats.creepage_skips.values())
            print(f"  Edits skipped for creepage: {total}")
            for (a, b), count in sorted(stats.creepage_skips.items()):
                print(f"    {a} / {b}: {count}")
        print()

        if args.verbose and stats.per_net:
            print("Per-net statistics:")
            width = max(len(n.net) for n in stats.per_net)
            for n in stats.per_net:
                note = "  (rolled back)" if n.rolled_back else ""
                if n.new_violations:
                    rules = ", ".join(f"{r}={c}" for r, c in sorted(n.new_violations.items()))
                    note += f"  [new violations: {rules}]"
                print(
                    f"  {n.net:<{width}}  segments {n.segments_before:>4} -> "
                    f"{n.segments_after:>4}  length {n.length_before:>7.2f}mm -> "
                    f"{n.length_after:>7.2f}mm{note}"
                )
            print()

        if args.dry_run:
            print("(Dry run - no changes written)")
        elif args.output:
            print(f"Saved to: {args.output}")
        else:
            print(f"Updated: {pcb_path}")

        if zones_refilled:
            print("Zones refilled via kicad-cli")
        elif zones_refilled is False:
            print("Zones NOT refilled (stale fill; see warning above)")

        print("=" * 50)

    return 0


if __name__ == "__main__":
    sys.exit(main())
