"""Net connectivity status CLI command.

Report net connectivity status showing which nets are complete, incomplete,
or unrouted, with details on what's missing.

Usage:
    kicad-net-status board.kicad_pcb
    kicad-net-status board.kicad_pcb --incomplete
    kicad-net-status board.kicad_pcb --net GND
    kicad-net-status board.kicad_pcb --by-class
    kicad-net-status board.kicad_pcb --format json
    kicad-net-status board.kicad_pcb --strict
    kicad-net-status board.kicad_pcb --legacy-proximity

Connectivity model:
    By default (since issue #4557), connectivity is decided by real geometric
    copper contact (shapely polygon intersection), matching the semantics of
    ``kicad-cli pcb drc`` (issue #4176). ``--strict`` selects the same model
    explicitly (kept as a no-op for script compatibility).
    Pass ``--legacy-proximity`` to opt into the legacy 0.01mm
    endpoint-proximity tolerance: a segment endpoint is unioned with a pad /
    via / other segment whenever their reference points land within 0.01mm,
    without testing whether the real copper (segment width, pad shape)
    actually touches. That model diverges from KiCad in both directions -- it
    can report proximity-unioned copper "complete" that KiCad reports open
    (issue #4176), and it reports false opens when a trace endpoint lands
    inside pad copper but away from the pad center (issue #4557).

Exit Codes:
    0 - No incomplete nets
    1 - Error loading file or other error
    2 - Incomplete nets found

    The exit code for the normal status path is board-wide even under
    ``--net`` (exit 2 when ANY net on the board is incomplete, regardless of
    the selected net's status) -- kept unchanged for script compatibility
    (issue #4682). The ``--why`` path scopes its exit code to the selected
    net when ``--net`` is given.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kicad_tools.analysis.net_status import NetStatus, NetStatusAnalyzer, NetStatusResult

if TYPE_CHECKING:
    from kicad_tools.router.stuck_classifier import StuckNetDiagnosis


def main(argv: list[str] | None = None) -> int:
    """Main entry point for kicad-net-status command."""
    parser = argparse.ArgumentParser(
        prog="kicad-net-status",
        description="Report net connectivity status for a PCB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--incomplete",
        action="store_true",
        help="Show only incomplete nets",
    )
    parser.add_argument(
        "--net",
        help=(
            "Show status for a specific net by name. Scopes the text "
            "summary/banner (and --why diagnoses) to the selected net. "
            "Note: the exit code stays BOARD-WIDE for the normal status "
            "path -- exit 2 when any net on the board is incomplete, even "
            "if the selected net is complete (issue #4682)."
        ),
    )
    parser.add_argument(
        "--by-class",
        action="store_true",
        dest="by_class",
        help="Group output by net class",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show all pads with coordinates",
    )
    model_group = parser.add_mutually_exclusive_group()
    model_group.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Decide connectivity by REAL geometric copper contact (shapely "
            "polygon intersection), matching KiCad's connectivity semantics "
            "(issue #4176). This has been the DEFAULT since issue #4557; the "
            "flag is kept as an explicit no-op for script compatibility."
        ),
    )
    model_group.add_argument(
        "--legacy-proximity",
        action="store_true",
        dest="legacy_proximity",
        help=(
            "Opt into the legacy 0.01mm endpoint-proximity connectivity "
            "model: a segment endpoint is unioned with a pad/via/segment "
            "whenever their reference points land within 0.01mm, even if the "
            "actual copper (segment width, pad shape) does not touch. "
            "Faster (~1.5-2x) but diverges from KiCad in both directions "
            "(issues #4176, #4557). Mutually exclusive with --strict."
        ),
    )
    parser.add_argument(
        "--why",
        action="store_true",
        help=(
            "Classify each incomplete signal net by WHY it is stuck "
            "(ESCAPE_BLOCKED / CONGESTION_SATURATED / BUDGET_STARVED / "
            "PLACEMENT_BOUND) with supporting evidence. Read-only diagnostic "
            "(issue #3863)."
        ),
    )

    args = parser.parse_args(argv)

    # Validate PCB path
    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: PCB not found: {pcb_path}", file=sys.stderr)
        return 1

    # Connectivity model selection (issue #4557): strict (real copper
    # geometry) is the default; --strict is an explicit no-op kept for script
    # compatibility; --legacy-proximity opts back into the endpoint-proximity
    # model. The two flags are mutually exclusive (argparse-enforced).
    strict = not args.legacy_proximity

    # Run analysis
    try:
        analyzer = NetStatusAnalyzer(pcb_path, strict=strict)
        result = analyzer.analyze()
    except Exception as e:
        print(f"Error during analysis: {e}", file=sys.stderr)
        return 1

    # --net: validate the selected net exists before dispatching to either
    # output path. This must run ahead of the --why branch -- previously
    # --why short-circuited first and silently ignored --net entirely
    # (issue #4682).
    net_status: NetStatus | None = None
    if args.net:
        net_status = result.get_net(args.net)
        if not net_status:
            print(f"Error: Net '{args.net}' not found", file=sys.stderr)
            print("\nAvailable nets:", file=sys.stderr)
            for net in sorted(result.nets, key=lambda n: n.net_name)[:20]:
                print(f"  {net.net_name}", file=sys.stderr)
            if len(result.nets) > 20:
                print(f"  ... and {len(result.nets) - 20} more", file=sys.stderr)
            return 1

    # --why: stuck-net classifier (issue #3863). Read-only diagnostic that
    # labels each incomplete signal net by WHY it is stuck. Handled before the
    # normal filter/output path because it has its own output shape. Respects
    # the strict/legacy selection (issue #4557) and the --net filter
    # (issue #4682).
    if args.why:
        return output_why(pcb_path, args.format, strict=strict, net=args.net)

    # Filter results if needed
    if net_status is not None:
        # Create a result with just this net (--net validated above)
        filtered = NetStatusResult(nets=[net_status], total_nets=1)
    elif args.incomplete:
        filtered = NetStatusResult(
            nets=result.incomplete + result.unrouted,
            total_nets=result.total_nets,
        )
    else:
        filtered = result

    # Output
    if args.format == "json":
        output_json(filtered, pcb_path, args.by_class, strict=strict)
    else:
        # For --incomplete the summary header must describe the unfiltered
        # board so its counts stay internally consistent (Complete +
        # Incomplete + Unrouted == total). The filtered set only drives the
        # net *list* below the header. For --net the summary is instead
        # scoped to the selection (issue #4682) so the banner and summary
        # cannot contradict each other; ``net_filter`` selects that path.
        output_text(
            filtered,
            pcb_path,
            args.verbose,
            args.by_class,
            args.incomplete,
            summary_result=result,
            strict=strict,
            net_filter=args.net,
        )

    # Exit code. Deliberately BOARD-WIDE even under --net (issue #4682 kept
    # this unchanged to avoid breaking script consumers; documented in the
    # --net help text).
    if result.incomplete_count > 0 or result.unrouted_count > 0:
        return 2
    return 0


def _model_label(strict: bool) -> str:
    """Human-readable name of the connectivity model in use (issue #4557)."""
    if strict:
        return "strict (real copper geometry, matches kicad-cli)"
    return "legacy endpoint-proximity (0.01mm tolerance)"


def output_text(
    result: NetStatusResult,
    pcb_path: Path,
    verbose: bool = False,
    by_class: bool = False,
    incomplete_only: bool = False,
    summary_result: NetStatusResult | None = None,
    strict: bool = True,
    net_filter: str | None = None,
) -> None:
    """Output results as formatted text.

    Args:
        result: The (possibly filtered) result used to render the net list.
        summary_result: The unfiltered result used for the Summary header. When
            ``--incomplete`` filters complete nets out of ``result``, the header
            must still describe the full board so its counts stay consistent
            (Complete + Incomplete + Unrouted == total). Defaults to ``result``.
        strict: Which connectivity model produced ``result`` (drives the
            model line in the header, issue #4557).
        net_filter: Name of the net selected with ``--net``, if any. When set,
            the Summary block and the connectivity banner are BOTH scoped to
            the selection (with the board-wide total named for context) so no
            rendered line contradicts another (issue #4682). Note the exit
            code computed by ``main()`` stays board-wide regardless.
    """
    summary = summary_result if summary_result is not None else result
    print(f"Net Status: {pcb_path.name}")
    print("=" * 60)
    print(f"Connectivity model: {_model_label(strict)}")
    print()
    if net_filter is not None:
        # --net: scope the counts to the selection (issue #4682). Before
        # this, a board-wide "Incomplete: 1" rendered directly above an
        # "All nets are fully connected!" banner derived from the filtered
        # set -- a direct contradiction.
        print(f"Summary: 1 net selected (of {summary.total_nets} on board)")
        counted = result
    else:
        print(f"Summary: {summary.total_nets} nets total")
        counted = summary
    print(f"  Complete:   {counted.complete_count} (all pads connected)")
    print(f"  Incomplete: {counted.incomplete_count} (partially connected)")
    print(f"  Unrouted:   {counted.unrouted_count} (no pads connected)")
    print()

    if by_class:
        _output_by_class(result, verbose)
    else:
        _output_flat(result, verbose, incomplete_only, net_filter=net_filter)


def _output_flat(
    result: NetStatusResult,
    verbose: bool,
    incomplete_only: bool,
    net_filter: str | None = None,
) -> None:
    """Output nets in flat list format.

    ``net_filter`` names the ``--net`` selection when set; the fully-connected
    banner is then worded for that scope instead of claiming board-wide
    completeness (issue #4682).
    """
    # Show incomplete nets with details
    if result.incomplete or result.unrouted:
        print("Incomplete Nets:")
        print("=" * 60)

        for net in result.incomplete + result.unrouted:
            _print_net_status(net, verbose)

    elif not incomplete_only:
        if net_filter is not None:
            print(f"Selected net '{net_filter}' is fully connected.")
        else:
            print("All nets are fully connected!")

    # Show complete nets if not filtering
    if not incomplete_only and result.complete:
        print()
        print("Complete Nets:")
        print("-" * 60)
        for net in result.complete[:20]:
            print(f"  {net.net_name} ({net.total_pads} pads)")
        if len(result.complete) > 20:
            print(f"  ... and {len(result.complete) - 20} more")


def _output_by_class(result: NetStatusResult, verbose: bool) -> None:
    """Output nets grouped by net class."""
    by_class = result.by_net_class()

    for class_name, nets in sorted(by_class.items()):
        incomplete = [n for n in nets if n.status != "complete"]
        complete = [n for n in nets if n.status == "complete"]

        print(f"\nNet Class: {class_name}")
        print("-" * 60)
        print(f"  Complete: {len(complete)}, Incomplete: {len(incomplete)}")

        if incomplete:
            print()
            for net in incomplete:
                _print_net_status(net, verbose, indent="  ")


def _print_net_status(net: NetStatus, verbose: bool, indent: str = "") -> None:
    """Print status for a single net."""
    # Status indicator
    if net.status == "complete":
        status_char = "+"
    elif net.status == "incomplete":
        status_char = "!"
    else:  # unrouted
        status_char = "X"

    # Net type indicator
    type_info = ""
    if net.is_plane_net:
        layers = (
            net.plane_layers if net.plane_layers else ([net.plane_layer] if net.plane_layer else [])
        )
        layers_str = ", ".join(layers) if layers else "unknown"
        type_info = f" -- Plane net on {layers_str}"

    print()
    print(
        f"{indent}[{status_char}] {net.net_name} ({net.unconnected_count} pads unconnected){type_info}"
    )

    # Show unconnected pads
    pads_to_show = net.unconnected_pads[:5] if not verbose else net.unconnected_pads
    for pad in pads_to_show:
        fix_hint = "needs via to plane" if net.is_plane_net else "needs routing"
        print(
            f"{indent}  {pad.full_name:<8} @ ({pad.position[0]:.2f}, {pad.position[1]:.2f}) -- {fix_hint}"
        )

    if not verbose and len(net.unconnected_pads) > 5:
        print(f"{indent}  ... ({len(net.unconnected_pads) - 5} more)")

    # Suggested fix
    print(f"{indent}  -> Fix: {net.suggested_fix}")


def output_json(
    result: NetStatusResult,
    pcb_path: Path,
    by_class: bool = False,
    strict: bool = True,
) -> None:
    """Output results as JSON."""
    data: dict[str, Any] = {
        "pcb": str(pcb_path),
        "connectivity_model": "strict" if strict else "legacy_proximity",
        "summary": {
            "total_nets": result.total_nets,
            "complete": result.complete_count,
            "incomplete": result.incomplete_count,
            "unrouted": result.unrouted_count,
            "total_unconnected_pads": result.total_unconnected_pads,
        },
    }

    if by_class:
        data["by_class"] = {
            class_name: [n.to_dict() for n in nets]
            for class_name, nets in result.by_net_class().items()
        }
    else:
        data["nets"] = [n.to_dict() for n in result.nets]

    print(json.dumps(data, indent=2))


_ACTION_LABELS = {
    "de_reverse_bundle": "de-reverse the bundle",
    "reorder_pins": "re-order pins",
    "widen_channel": "widen the channel",
    "move_part": "move a part",
    "accept_plateau": "accept plateau",
}


def _print_recommendation(diag: StuckNetDiagnosis) -> None:
    """Render the additive ranked fix ladder for one stuck net (issue #4261).

    Only called when ``diag`` has a non-empty ``recommendation``.
    """
    confidence = diag.recommendation[0].confidence.value.upper()
    if diag.topology == "self_crossing_bundle":
        group = diag.match_group or "bundle"
        headline = f"self-crossing {group} bundle"
    elif diag.topology == "co_oriented_bundle":
        # Issue #4286: same-group congestion with a MEASURED co-oriented pin
        # order -- the bundle is planar, so de-reversal is never suggested.
        group = diag.match_group or "bundle"
        headline = f"co-oriented {group} bundle (saturated corridor)"
    else:
        headline = "foreign-cluster congestion"
    print(f"  recommendation:   [{confidence}] {headline}")
    for i, ranked in enumerate(diag.recommendation, start=1):
        label = _ACTION_LABELS.get(ranked.action.value, ranked.action.value)
        preferred = "  [preferred]" if i == 1 else ""
        print(f"                    {i}. {label} ({ranked.rationale}){preferred}")


def output_why(pcb_path: Path, fmt: str, strict: bool = True, net: str | None = None) -> int:
    """Classify incomplete signal nets by why they are stuck and print them.

    ``strict`` selects the connectivity model the classifier's internal
    ``NetStatusAnalyzer`` runs use (issue #4557) -- before this parameter,
    ``--strict --why`` silently ignored ``--strict``.

    ``net`` scopes the output to a single net (the ``--net`` selection,
    issue #4682): classification still runs whole-board (acceptable cost for
    a read-only diagnostic), then the diagnoses are post-filtered to the
    selected net. The caller (``main()``) has already validated that the net
    exists on the board. Before this parameter, ``--net X --why`` silently
    printed every stuck net EXCEPT the one asked about.

    Returns 2 if any (selected) stuck nets were found (matching the rest of
    net-status' exit-code convention), 0 if there are none -- so under
    ``--net`` the exit code reflects the selected net only.
    """
    from kicad_tools.router.stuck_classifier import StuckClassifierResult, classify_stuck_nets

    result = classify_stuck_nets(pcb_path, strict=strict)

    # --net filter (issue #4682): restrict diagnoses (and every derived
    # count) to the selected net.
    if net is not None:
        result = StuckClassifierResult(diagnoses=[d for d in result.diagnoses if d.net_name == net])

    if fmt == "json":
        data = {
            "pcb": str(pcb_path),
            "connectivity_model": "strict" if strict else "legacy_proximity",
            **result.to_dict(),
        }
        if net is not None:
            data["net_filter"] = net
        print(json.dumps(data, indent=2))
        return 2 if result.diagnoses else 0

    print(f"Stuck-net classification: {pcb_path.name}")
    print("=" * 70)
    print(f"Connectivity model: {_model_label(strict)}")
    if net is not None:
        print(f"Selected net: {net}")
    print()
    counts = result.counts
    print(f"Stuck signal nets: {len(result.diagnoses)}")
    print(f"  ESCAPE_BLOCKED:       {counts['escape_blocked']}")
    print(f"  CONGESTION_SATURATED: {counts['congestion_saturated']}")
    print(f"  BUDGET_STARVED:       {counts['budget_starved']}")
    print(f"  PLACEMENT_BOUND:      {counts['placement_bound']}")
    print(f"  POUR_DISCONTINUOUS:   {counts['pour_discontinuous']}")
    print()

    if not result.diagnoses:
        if net is not None:
            print(f"Net '{net}' is not stuck (complete, or only advisory plane/pour residuals).")
        else:
            print("No stuck signal nets -- board is fully routed (or only advisory")
            print("plane/pour residuals remain).")
        return 0

    # Group for readability, escape-blocked first (most upstream).
    order = {
        "escape_blocked": 0,
        "congestion_saturated": 1,
        "budget_starved": 2,
        "placement_bound": 3,
        "pour_discontinuous": 4,
    }
    for diag in sorted(result.diagnoses, key=lambda d: order[d.classification_value]):
        pads = ", ".join(diag.unconnected_pads) or "(none)"
        print(f"[{diag.classification.value.upper()}] {diag.net_name}")
        print(f"  unconnected pads: {pads}")
        if diag.blocking_nets:
            # Defect 2 (#4261): flag which blockers are the net's OWN match-group
            # siblings rather than foreign copper.
            note = ""
            if diag.same_group_blockers and diag.match_group:
                note = (
                    f"        (same match-group {diag.match_group}: "
                    f"{', '.join(diag.same_group_blockers)})"
                )
            print(f"  blocking nets:    {', '.join(diag.blocking_nets)}{note}")
        print(f"  evidence:         {diag.evidence}")
        if diag.recommendation:
            _print_recommendation(diag)
        print()

    return 2


if __name__ == "__main__":
    sys.exit(main())
