"""
Pure Python DRC check command for KiCad PCBs.

Runs design rule checks against manufacturer specifications without
requiring kicad-cli to be installed. Suitable for CI/CD pipelines.

Usage:
    kct check board.kicad_pcb                      # Run all checks
    kct check board.kicad_pcb --mfr jlcpcb         # With manufacturer rules
    kct check board.kicad_pcb --format json        # JSON output for CI
    kct check board.kicad_pcb --only clearance     # Run specific checks
    kct check board.kicad_pcb --skip silkscreen    # Exclude checks

Exit Codes:
    0 - No errors (warnings may be present without --strict)
    1 - Command failure (file not found, parse error, etc.)
    2 - Errors found, or warnings found with --strict

Difference from `kct drc`:
    - kct drc: Uses kicad-cli to run DRC (requires KiCad)
    - kct check: Pure Python DRC (no external dependencies)
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from kicad_tools.manufacturers import get_manufacturer_ids
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate import DRCChecker, DRCResults, DRCViolation

# Issue #3750: meta-check status set.  ``NOT RUN`` is rendered with a space
# in human output and ``"NOT RUN"`` in JSON; we treat it as a single token
# so callers can compare against the literal.
SubCheckStatus = Literal["PASSED", "FAILED", "NOT RUN"]


@dataclass
class SubCheckResult:
    """Outcome of a single :mod:`kct check` sub-check (issue #3750).

    ``status`` is one of ``PASSED`` / ``FAILED`` / ``NOT RUN``.  ``detail``
    is the one-line human-readable summary that appears in parentheses on
    the human stanza and as the ``detail`` field in the JSON envelope.
    """

    status: SubCheckStatus
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status, "detail": self.detail}


@dataclass
class MetaCheckResult:
    """Aggregated meta-check rollup (issue #3750).

    Each of the four sub-checks (DRC, ERC, LVS, Manifest) has its own
    :class:`SubCheckResult`; ``overall`` is the rollup status that drives
    the exit code.
    """

    drc: SubCheckResult
    erc: SubCheckResult
    lvs: SubCheckResult
    manifest: SubCheckResult
    overall: Literal["PASSED", "FAILED", "INCOMPLETE"] = "PASSED"

    def _subs(self) -> tuple[SubCheckResult, ...]:
        return (self.drc, self.erc, self.lvs, self.manifest)

    def compute_overall(self, strict: bool = False) -> None:
        """Roll up the four sub-statuses into ``self.overall``.

        Rules (per issue #3750):

        * ``FAILED`` if any sub-check is ``FAILED``.
        * ``INCOMPLETE`` if any sub-check is ``NOT RUN`` (and none is
          ``FAILED``) -- unless ``strict`` is set, in which case the
          rollup is ``FAILED``.
        * ``PASSED`` only when every sub-check is ``PASSED``.
        """
        subs = self._subs()
        if any(s.status == "FAILED" for s in subs):
            self.overall = "FAILED"
        elif any(s.status == "NOT RUN" for s in subs):
            self.overall = "FAILED" if strict else "INCOMPLETE"
        else:
            self.overall = "PASSED"

    def to_dict(self) -> dict:
        return {
            "drc": self.drc.to_dict(),
            "erc": self.erc.to_dict(),
            "lvs": self.lvs.to_dict(),
            "manifest": self.manifest.to_dict(),
            "overall": self.overall,
        }


# Available check categories


def _find_pcb_file(directory: Path) -> Path | None:
    """Find a .kicad_pcb file in the given directory.

    Searches recursively and filters out routed/backup files to find
    the primary unrouted PCB file.

    Args:
        directory: Directory to search

    Returns:
        Path to PCB file if found, None otherwise
    """
    pcb_files = list(directory.glob("**/*.kicad_pcb"))
    # Filter out routed and backup files
    pcb_files = [
        f
        for f in pcb_files
        if not f.name.endswith("_routed.kicad_pcb") and not f.name.endswith("-bak.kicad_pcb")
    ]
    if pcb_files:
        return pcb_files[0]
    return None


def _emit_drift_banner(pcb_path: Path, schematic: str | None) -> None:
    """Print the advisory schematic/PCB drift banner (non-blocking).

    No-op when no schematic can be resolved or the PCB is in sync.  This is
    advisory only and never affects the caller's exit code (issue #3154).

    The banner is routed to stderr so it does not pollute the stdout JSON
    body produced by ``--format json`` consumers (the CI gate at
    ``scripts/ci/check_routed_drc.py`` parses stdout as a single JSON document
    and was choking on the leading WARNING line; routing to stderr keeps the
    advisory visible in human/log output while leaving the structured payload
    clean).
    """
    from kicad_tools.sync.drift import analyze_drift, format_drift_banner

    analysis, _resolved = analyze_drift(pcb_path, schematic)
    if analysis is None:
        return
    banner = format_drift_banner(analysis, pcb_path)
    if banner:
        print(banner, file=sys.stderr)


def run_netlist_sync_gate(
    pcb_path: Path,
    schematic: str | None = None,
    strict: bool = False,
) -> int:
    """Run the blocking schematic/PCB netlist-sync gate (issue #3154).

    Reuses :class:`kicad_tools.sync.reconciler.Reconciler` (via the shared
    drift helpers) to compare the schematic component set against the PCB
    footprint set, then prints a full add/drop/orphan report.

    Exit codes (mirroring ``kct check``'s convention):
        0 - in sync, or only PCB-only/value/footprint drift without --strict
        1 - no schematic could be resolved (cannot run the gate)
        2 - components present in the schematic are missing from the PCB
            (unbuildable BOM), or any drift with --strict

    Args:
        pcb_path: Path to the ``.kicad_pcb`` file.
        schematic: Optional explicit schematic path override.
        strict: When True, any drift (including PCB-only/value/footprint)
            yields exit code 2.
    """
    from kicad_tools.sync.drift import analyze_drift, has_drift, render_drift_report

    analysis, resolved = analyze_drift(pcb_path, schematic)
    if analysis is None or resolved is None:
        print(
            "Error: --netlist-sync requires a schematic, but none was found "
            f"for {Path(pcb_path).name}.",
            file=sys.stderr,
        )
        print(
            "Hint: pass --schematic <path>.kicad_sch, or place a sibling "
            "<basename>.kicad_sch next to the PCB.",
            file=sys.stderr,
        )
        return 1

    print(render_drift_report(analysis, pcb_path, resolved))

    # Schematic-only drift == unbuildable BOM == blocking (mirrors the
    # auditor's NOT_READY verdict rule).  PCB-only / value / footprint drift
    # is advisory unless --strict.
    if analysis.schematic_orphans:
        return 2
    if strict and has_drift(analysis):
        return 2
    return 0


def _erc_subcheck(sch_path: Path | None, strict: bool) -> SubCheckResult:
    """Run kicad-cli ERC against the discovered schematic (issue #3750).

    Returns ``NOT RUN`` when no schematic is found.  Returns ``FAILED``
    when kicad-cli is missing, the schematic fails to load, or the report
    contains any errors (and, under ``strict``, any warnings).
    """
    if sch_path is None:
        return SubCheckResult(
            status="NOT RUN",
            detail="no schematic discovered next to PCB",
        )

    from kicad_tools.cli.runner import find_kicad_cli, run_erc
    from kicad_tools.erc import ERCReport

    if find_kicad_cli() is None:
        return SubCheckResult(
            status="NOT RUN",
            detail="kicad-cli not found in PATH; install KiCad 8+ to enable ERC",
        )

    cli_result = run_erc(sch_path, format="json")
    if not cli_result.success or cli_result.output_path is None:
        return SubCheckResult(
            status="FAILED",
            detail=f"kicad-cli ERC failed: {(cli_result.stderr or '').strip().splitlines()[-1] if cli_result.stderr else 'unknown error'}",
        )

    try:
        report = ERCReport.load(cli_result.output_path)
    except Exception as e:
        return SubCheckResult(
            status="FAILED",
            detail=f"failed to parse ERC report: {e}",
        )

    err_count = report.error_count
    warn_count = report.warning_count
    detail = f"{err_count} error(s), {warn_count} warning(s)"
    if err_count > 0:
        return SubCheckResult(status="FAILED", detail=detail)
    if strict and warn_count > 0:
        return SubCheckResult(status="FAILED", detail=detail + " (strict)")
    return SubCheckResult(status="PASSED", detail=detail)


def _lvs_subcheck(sch_path: Path | None, pcb_path: Path) -> SubCheckResult:
    """Run live LVS via :func:`compare_netlists` (issue #3750).

    Always recomputes -- never reads ``output/lvs.json`` -- so a fresh
    PCB edit that breaks LVS is surfaced immediately.  Returns
    ``NOT RUN`` when no schematic is found.
    """
    if sch_path is None:
        return SubCheckResult(
            status="NOT RUN",
            detail="no schematic discovered; cannot compare",
        )

    try:
        from kicad_tools.lvs.board_lvs import compare_netlists

        result = compare_netlists(sch_path, pcb_path)
    except Exception as e:
        return SubCheckResult(
            status="FAILED",
            detail=f"LVS comparator raised {type(e).__name__}: {e}",
        )

    if result.clean:
        return SubCheckResult(
            status="PASSED",
            detail=f"{len(result.mismatches)} mismatch(es)",
        )

    # Show up to the first 3 mismatches in stable (ref, pad) order so
    # the detail line is bounded but informative.
    mismatches = sorted(result.mismatches, key=lambda m: (m.ref, m.pad))
    preview = ", ".join(
        f"{m.ref}.{m.pad} sch={m.schematic_net!r} pcb={m.pcb_net!r}" for m in mismatches[:3]
    )
    suffix = "" if len(mismatches) <= 3 else f" (+{len(mismatches) - 3} more)"
    return SubCheckResult(
        status="FAILED",
        detail=f"{len(mismatches)} mismatch(es): {preview}{suffix}",
    )


def _manifest_subcheck(pcb_path: Path) -> SubCheckResult:
    """Compare ``output/manufacturing/manifest.json`` mtime against the PCB.

    Resolution path (issue #3750):

    * Look for ``<pcb-dir>/manufacturing/manifest.json`` first (recipes
      that place the routed PCB next to a ``manufacturing/`` peer).
    * Then ``<pcb-dir>/../manufacturing/manifest.json`` for layouts where
      the PCB is one level deeper.

    Returns ``NOT RUN`` when neither manifest is present, ``FAILED``
    (rendered as ``STALE`` in human output) when the routed PCB is newer
    than the manifest, and ``PASSED`` otherwise.
    """
    candidates = [
        pcb_path.parent / "manufacturing" / "manifest.json",
        pcb_path.parent.parent / "manufacturing" / "manifest.json",
    ]
    manifest_path: Path | None = None
    for cand in candidates:
        if cand.exists():
            manifest_path = cand
            break

    if manifest_path is None:
        return SubCheckResult(
            status="NOT RUN",
            detail="no manufacturing bundle; run `kct export` first",
        )

    try:
        pcb_mtime = pcb_path.stat().st_mtime
        manifest_mtime = manifest_path.stat().st_mtime
    except OSError as e:
        return SubCheckResult(
            status="FAILED",
            detail=f"failed to stat manifest or PCB: {e}",
        )

    # Allow a small mtime tolerance so a fresh ``git checkout`` (which
    # writes files sequentially with sub-microsecond gaps) does not
    # spuriously flag the manifest as stale: the PCB and manifest are
    # written within milliseconds of each other by ``kct export``, while
    # a *real* stale manifest lags by minutes or longer (any rebuild of
    # the routed PCB that skipped ``kct export`` produces a multi-second
    # gap).  ``MANIFEST_FRESHNESS_TOLERANCE_S`` carves that gap.
    MANIFEST_FRESHNESS_TOLERANCE_S = 5.0
    delta = pcb_mtime - manifest_mtime
    if delta > MANIFEST_FRESHNESS_TOLERANCE_S:
        return SubCheckResult(
            status="FAILED",
            detail=f"STALE: routed PCB is {delta:.1f}s newer than manifest.json",
        )

    return SubCheckResult(
        status="PASSED",
        detail="manifest.json mtime within tolerance of routed PCB mtime",
    )


def run_meta_checks(
    pcb_path: Path,
    drc_status: SubCheckResult,
    schematic: str | None = None,
    strict: bool = False,
) -> MetaCheckResult:
    """Run the four meta sub-checks (DRC + ERC + LVS + Manifest).

    DRC is supplied by the caller (it has already run as part of the
    main check pipeline); this helper layers ERC, LVS, and manifest
    freshness on top and rolls them up into a single
    :class:`MetaCheckResult` (issue #3750).

    Args:
        pcb_path: Path to the routed ``.kicad_pcb`` under test.
        drc_status: Pre-computed DRC :class:`SubCheckResult` from the
            current invocation's DRC pipeline.  Folded in directly so the
            meta rollup doesn't redo the DRC work.
        schematic: Optional explicit ``.kicad_sch`` override.  When
            omitted, schematic discovery falls back to
            :func:`kicad_tools.sync.discover.resolve_schematic_for_pcb`
            (handles the ``_routed`` suffix strip used by recipes).
        strict: When True, ``NOT RUN`` rolls up to ``FAILED`` (instead of
            ``INCOMPLETE``) and ERC warnings become fatal.
    """
    from kicad_tools.sync.discover import resolve_schematic_for_pcb

    if schematic is not None:
        resolved_sch: Path | None = Path(schematic).resolve()
        if not resolved_sch.exists():
            resolved_sch = None
    else:
        resolved_sch = resolve_schematic_for_pcb(pcb_path)

    erc = _erc_subcheck(resolved_sch, strict)
    lvs = _lvs_subcheck(resolved_sch, pcb_path)
    manifest = _manifest_subcheck(pcb_path)

    result = MetaCheckResult(drc=drc_status, erc=erc, lvs=lvs, manifest=manifest)
    result.compute_overall(strict=strict)
    return result


def _format_meta_status_line(name: str, sub: SubCheckResult) -> str:
    """Render one human-output ``DRC: PASSED (...)`` line.

    ``STALE`` is rendered in place of ``FAILED`` for the Manifest
    sub-check when the detail starts with ``STALE:`` (issue #3750's
    human-clarity convention).  The JSON status is still ``FAILED``.
    """
    display_status = sub.status
    detail = sub.detail
    if name == "Manifest" and sub.status == "FAILED" and detail.startswith("STALE:"):
        display_status = "STALE"
        # Trim the "STALE: " prefix from the detail since the status
        # column already carries it.
        detail = detail[len("STALE: ") :]
    return f"{name + ':':10} {display_status:8} ({detail})"


def print_meta_check_stanza(result: MetaCheckResult) -> None:
    """Print the per-sub-check status block + overall rollup (issue #3750).

    Output goes to stdout in a stable column layout so humans can
    diff it across runs.  The ``Overall:`` line is the rollup that
    matches the exit-code decision.
    """
    print()
    print(_format_meta_status_line("DRC", result.drc))
    print(_format_meta_status_line("ERC", result.erc))
    print(_format_meta_status_line("LVS", result.lvs))
    print(_format_meta_status_line("Manifest", result.manifest))
    print(f"{'Overall:':10} {result.overall}")


CHECK_CATEGORIES = [
    "clearance",
    "connectivity",
    "segment_zone",
    "via_zone",
    "diffpair_clearance_intra",
    "diffpair_length_skew",
    "diffpair_routing_continuity",
    "dimensions",
    "edge",
    "impedance",
    "match_group_length_skew",
    "netlist",
    "pad_grid",
    "placement",
    "silkscreen",
    "single_pad_net",
    "solder_mask",
    "via_in_pad",
    "zones",
]


def main(argv: list[str] | None = None) -> int:
    """Main entry point for kct check command."""
    parser = argparse.ArgumentParser(
        prog="kct check",
        description="Pure Python DRC for PCBs (no kicad-cli required)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "pcb",
        help="Path to .kicad_pcb file or directory containing one",
    )
    parser.add_argument(
        "--format",
        choices=["table", "json", "summary"],
        default="table",
        help="Output format (default: table)",
    )
    parser.add_argument(
        "--errors-only",
        action="store_true",
        help="Show only errors, not warnings",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with error code 2 on warnings",
    )
    parser.add_argument(
        "--mfr",
        "-m",
        choices=get_manufacturer_ids(),
        default="jlcpcb",
        help="Target manufacturer for design rules (default: jlcpcb)",
    )
    parser.add_argument(
        "--layers",
        "-l",
        type=int,
        default=None,
        help="Number of copper layers (auto-detected from board if not specified)",
    )
    parser.add_argument(
        "--copper",
        "-c",
        type=float,
        default=1.0,
        help="Copper weight in oz (default: 1.0)",
    )
    parser.add_argument(
        "--only",
        dest="only_checks",
        help=f"Run only specific checks (comma-separated: {', '.join(CHECK_CATEGORIES)})",
    )
    parser.add_argument(
        "--skip",
        dest="skip_checks",
        help=f"Skip specific checks (comma-separated: {', '.join(CHECK_CATEGORIES)})",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Write JSON report to file (implies --format json for file output)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed violation information",
    )
    parser.add_argument(
        "--suppress-library",
        action="store_true",
        help="Suppress silkscreen warnings from standard KiCad library footprints",
    )
    parser.add_argument(
        "--drc-only",
        dest="drc_only",
        action="store_true",
        help=(
            "Legacy DRC-only mode (issue #3750).  Skips the ERC / LVS / "
            "Manifest meta sub-checks and preserves the pre-#3750 stdout "
            "and exit-code contract.  Intended for CI scripts and recipes "
            "that depend on the historical 'kct check' semantics (e.g. "
            "scripts/ci/check_routed_drc.py and the per-board allowlists "
            "in .github/routed-drc-tolerance.yml)."
        ),
    )
    parser.add_argument(
        "--netlist-sync",
        action="store_true",
        help=(
            "Run a blocking schematic/PCB netlist-sync gate (issue #3154). "
            "Compares the schematic component set against the PCB footprint set "
            "via the Reconciler and prints a full add/drop/orphan report. Exits "
            "with code 2 when components present in the schematic are missing "
            "from the PCB (unbuildable BOM). PCB-only/value/footprint drift is a "
            "warning unless --strict. Skips silently if no schematic is found."
        ),
    )
    parser.add_argument(
        "--schematic",
        default=None,
        help=(
            "Explicit path to the .kicad_sch file for the netlist-sync gate / "
            "advisory drift banner. When omitted, the schematic is "
            "auto-discovered from project.kct or the sibling <basename>.kicad_sch."
        ),
    )
    parser.add_argument(
        "--net-class-map",
        dest="net_class_map",
        default=None,
        help=(
            "Path to a JSON sidecar mapping net names to NetClassRouting "
            "fields (see kicad_tools.router.rules.NetClassRouting.to_dict). "
            "When supplied, enables the diff-pair routing_continuity and "
            "length_skew rules to fire on routed boards; without it those "
            "rules degrade to no-ops (Issue #2684)."
        ),
    )
    # Issue #3061: auto-derive the pad_grid tolerance from each board's
    # pad-offset histogram by default for the CLI.  Users can opt back into
    # the fixed-0.05mm behaviour with --pad-grid-strict, or pin a custom
    # value with --pad-grid-tolerance.
    pad_grid_group = parser.add_mutually_exclusive_group()
    pad_grid_group.add_argument(
        "--pad-grid-strict",
        action="store_true",
        help=(
            "Use the fixed 0.05mm pad_grid tolerance (PR #3057 default) "
            "instead of auto-deriving per-board from the pad-offset "
            "histogram (issue #3061).  Default: auto-derive."
        ),
    )
    pad_grid_group.add_argument(
        "--pad-grid-tolerance",
        type=float,
        default=None,
        metavar="MM",
        help=(
            "Override the pad_grid L2 tolerance with an explicit value "
            "in mm (e.g. ``--pad-grid-tolerance 0.02``).  Disables "
            "auto-derivation."
        ),
    )

    args = parser.parse_args(argv)

    # Parse and validate filter options
    only_set: set[str] | None = None
    skip_set: set[str] = set()

    if args.only_checks:
        only_set = set()
        for cat in args.only_checks.split(","):
            cat = cat.strip().lower()
            if cat not in CHECK_CATEGORIES:
                print(f"Error: Unknown check category: {cat!r}", file=sys.stderr)
                print(f"Available: {', '.join(CHECK_CATEGORIES)}", file=sys.stderr)
                return 1
            only_set.add(cat)

    if args.skip_checks:
        for cat in args.skip_checks.split(","):
            cat = cat.strip().lower()
            if cat not in CHECK_CATEGORIES:
                print(f"Error: Unknown check category: {cat!r}", file=sys.stderr)
                print(f"Available: {', '.join(CHECK_CATEGORIES)}", file=sys.stderr)
                return 1
            skip_set.add(cat)

    # Load PCB - resolve to absolute path for reliable file access
    # Handles both file paths and directory paths (like kct build)
    input_path = Path(args.pcb).resolve()

    if not input_path.exists():
        print(f"Error: Path not found: {input_path}", file=sys.stderr)
        return 1

    if input_path.is_dir():
        # Auto-discover PCB file in directory (consistent with kct build)
        pcb_path = _find_pcb_file(input_path)
        if pcb_path is None:
            print(f"Error: No .kicad_pcb file found in directory: {input_path}", file=sys.stderr)
            print(
                "Hint: Specify a .kicad_pcb file directly, or ensure the directory contains one.",
                file=sys.stderr,
            )
            return 1
    elif input_path.suffix != ".kicad_pcb":
        print(f"Error: Expected .kicad_pcb file, got: {input_path.name}", file=sys.stderr)
        print("Hint: Provide a .kicad_pcb file or a directory containing one.", file=sys.stderr)
        return 1
    else:
        pcb_path = input_path

    # Netlist-sync gate (issue #3154): a dedicated, blocking schematic/PCB
    # drift check that runs *instead of* the DRC pipeline and returns its own
    # exit code.  Reuses the Reconciler via the shared drift helpers.
    if getattr(args, "netlist_sync", False):
        return run_netlist_sync_gate(
            pcb_path,
            schematic=getattr(args, "schematic", None),
            strict=args.strict,
        )

    try:
        pcb = PCB.load(pcb_path)
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Advisory drift banner (issue #3154): when a schematic is discovered (or
    # passed via --schematic) and the component sets have drifted, print a
    # one-line, non-blocking warning before running DRC.  Never affects the
    # exit code on the default run -- the hard gate lives behind --netlist-sync.
    _emit_drift_banner(pcb_path, getattr(args, "schematic", None))

    # Auto-detect layer count from PCB if not explicitly provided
    if args.layers is not None:
        layers = args.layers
    else:
        detected = len(pcb.copper_layers)
        layers = detected if detected > 0 else 2

    # Load optional net-class-map sidecar (Issue #2684).  When supplied,
    # the diff-pair routing-continuity and length-skew rules can re-derive
    # engagement / skew state from the routed PCB and fire.  When omitted,
    # the rules degrade to no-ops (AC #3: graceful-degradation contract).
    net_class_map = None
    if args.net_class_map is not None:
        from kicad_tools.router.rules import net_class_map_from_dict

        ncm_path = Path(args.net_class_map).resolve()
        if not ncm_path.exists():
            print(f"Error: net-class-map file not found: {ncm_path}", file=sys.stderr)
            return 1
        try:
            ncm_data = json.loads(ncm_path.read_text())
        except json.JSONDecodeError as e:
            print(f"Error parsing net-class-map JSON: {e}", file=sys.stderr)
            return 1
        try:
            net_class_map = net_class_map_from_dict(ncm_data)
        except (TypeError, ValueError) as e:
            print(f"Error: invalid net-class-map structure: {e}", file=sys.stderr)
            return 1

    # Issue #3440: the skew rules (match_group_length_skew,
    # diffpair_length_skew, diffpair_routing_continuity) degrade to
    # silent no-ops without the --net-class-map sidecar -- "Rules
    # checked" excludes them and the check PASSES even with 15mm of
    # group skew on the board.  Warn LOUDLY when any of those rules is
    # selected but cannot engage, so a recipe that forgot the sidecar
    # doesn't sail through green.
    if net_class_map is None:
        _sidecar_dependent_rules = (
            "match_group_length_skew",
            "diffpair_length_skew",
            "diffpair_routing_continuity",
        )
        _inactive_rules = [
            rule
            for rule in _sidecar_dependent_rules
            if (only_set is None or rule in only_set) and rule not in skip_set
        ]
        if _inactive_rules:
            print(
                "WARNING: the following rules are INACTIVE without "
                "--net-class-map and will silently pass: "
                f"{', '.join(_inactive_rules)}.  Pass the routed board's "
                "sidecar (e.g. output/net_class_map.json) to validate "
                "length-match skew.",
                file=sys.stderr,
            )

    # Create checker with manufacturer rules
    try:
        checker = DRCChecker(
            pcb,
            manufacturer=args.mfr,
            layers=layers,
            copper_oz=args.copper,
            suppress_library=args.suppress_library,
            net_class_map=net_class_map,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Resolve pad_grid tolerance policy (issue #3061).
    # Precedence: explicit value > strict mode > auto-derive (CLI default).
    if args.pad_grid_tolerance is not None:
        pad_grid_threshold: float | None = args.pad_grid_tolerance
        pad_grid_auto_derive = False
    elif args.pad_grid_strict:
        pad_grid_threshold = None  # Falls through to DEFAULT_PAD_GRID_TOLERANCE_MM
        pad_grid_auto_derive = False
    else:
        pad_grid_threshold = None
        pad_grid_auto_derive = True

    # Run selected checks
    results = run_selected_checks(
        checker,
        only_set,
        skip_set,
        pad_grid_threshold=pad_grid_threshold,
        pad_grid_auto_derive=pad_grid_auto_derive,
    )

    # Apply errors-only filter
    violations = list(results.violations)
    if args.errors_only:
        violations = [v for v in violations if v.is_error]

    # Issue #3750: build the DRC SubCheckResult that will feed both the
    # exit-code computation and the meta-check rollup (when not in
    # --drc-only mode).  DRC status mirrors the legacy exit-code rule:
    # PASSED iff 0 errors and (0 warnings under --strict).
    error_count = sum(1 for v in violations if v.is_error)
    warning_count = sum(1 for v in violations if v.is_warning)
    drc_passed = error_count == 0 and not (warning_count > 0 and args.strict)
    drc_sub = SubCheckResult(
        status="PASSED" if drc_passed else "FAILED",
        detail=(
            f"{results.rules_checked} rules checked, "
            f"{error_count} error(s), {warning_count} warning(s)"
        ),
    )

    # Issue #3750: compute the meta-check rollup once and reuse it for
    # both the human stanza and the JSON envelope.  Skipped entirely
    # under --drc-only to preserve the legacy stdout/exit-code contract.
    drc_only = getattr(args, "drc_only", False)
    meta: MetaCheckResult | None = None
    if not drc_only:
        meta = run_meta_checks(
            pcb_path,
            drc_status=drc_sub,
            schematic=getattr(args, "schematic", None),
            strict=args.strict,
        )

    # Output results
    if args.format == "json":
        output_json(violations, results, pcb_path, args.mfr, layers, meta=meta)
    elif args.format == "summary":
        output_summary(violations, results, pcb_path)
        if meta is not None:
            print_meta_check_stanza(meta)
    else:
        output_table(violations, results, pcb_path, args.mfr, layers, args.verbose)
        if meta is not None:
            print_meta_check_stanza(meta)

    # Write JSON report to file if --output specified
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        write_json_report(violations, results, pcb_path, args.mfr, layers, output_path, meta=meta)

    # Determine exit code
    # Exit 2 = check ran successfully but found issues (errors, or warnings+strict)
    # Exit 1 = reserved for tool-level failures (file not found, parse error) above
    # Exit 0 = no errors (warnings may be present without --strict; infos
    #   never affect exit code -- they are advisory by definition).
    # Issue #3750: when the meta-check rollup is in play (default mode),
    # exit 2 also when any sub-check is FAILED (or, under --strict, any
    # sub-check is NOT RUN -- captured in ``meta.overall == 'FAILED'``).
    if drc_only:
        if error_count > 0 or (warning_count > 0 and args.strict):
            return 2
        return 0

    # Default (meta) mode: PASSED -> 0, FAILED -> 2, INCOMPLETE -> 0
    # (the user opted out of strict; INCOMPLETE is advisory-only).
    assert meta is not None  # guaranteed by the branch above
    if meta.overall == "FAILED":
        return 2
    return 0


def run_selected_checks(
    checker: DRCChecker,
    only_set: set[str] | None,
    skip_set: set[str],
    pad_grid_threshold: float | None = None,
    pad_grid_auto_derive: bool = True,
) -> DRCResults:
    """Run the selected DRC checks based on filters.

    Args:
        checker: The DRC checker pre-loaded with the PCB and rules.
        only_set: Optional whitelist of check category names.
        skip_set: Set of check category names to skip.
        pad_grid_threshold: Explicit pad_grid L2 tolerance in mm, or
            ``None`` to use the threshold-resolution policy below.
            Issue #3061.
        pad_grid_auto_derive: When ``True`` and ``pad_grid_threshold``
            is ``None``, the pad_grid check derives the threshold from
            the board's pad-offset histogram (issue #3061).  Defaults
            to ``True`` for the CLI; ``False`` preserves the PR #3057
            fixed-0.05mm behaviour.
    """
    results = DRCResults()

    # Build the pad_grid invocation as a thunk so the map below can
    # remain uniform (every value is a zero-arg callable).
    def _pad_grid_check() -> DRCResults:
        return checker.check_pad_grid_alignment(
            threshold=pad_grid_threshold,
            auto_derive_threshold=pad_grid_auto_derive,
        )

    # Map of category to check method.  This dict MUST stay a superset
    # of the methods invoked by ``DRCChecker.check_all`` (i.e., every
    # name in ``DRCChecker.CHECK_ALL_METHODS`` must be referenced as a
    # value here).  The regression test in
    # ``tests/test_check_cmd_coverage.py`` enforces the invariant for
    # Issue #3046.
    check_methods = {
        "clearance": checker.check_clearances,
        "connectivity": checker.check_connectivity,
        "segment_zone": checker.check_segment_zone_clearances,
        "via_zone": checker.check_via_zone_clearances,
        "diffpair_clearance_intra": checker.check_diffpair_clearance_intra,
        "diffpair_length_skew": checker.check_diffpair_length_skew,
        "diffpair_routing_continuity": checker.check_diffpair_routing_continuity,
        "dimensions": checker.check_dimensions,
        "edge": checker.check_edge_clearances,
        "impedance": checker.check_impedance,
        "match_group_length_skew": checker.check_match_group_length_skew,
        "netlist": checker.check_netlist,
        "pad_grid": _pad_grid_check,
        "placement": checker.check_footprint_placement,
        "silkscreen": checker.check_silkscreen,
        "single_pad_net": checker.check_single_pad_nets,
        "solder_mask": checker.check_solder_mask_pads,
        "via_in_pad": checker.check_via_in_pad,
        "zones": checker.check_zones,
    }

    for category, method in check_methods.items():
        # Skip if --only specified and this category not in it
        if only_set is not None and category not in only_set:
            continue

        # Skip if this category is in --skip
        if category in skip_set:
            continue

        # Run the check
        category_results = method()
        results.merge(category_results)

    return results


def output_table(
    violations: list[DRCViolation],
    results: DRCResults,
    pcb_path: Path,
    mfr: str,
    layers: int,
    verbose: bool = False,
) -> None:
    """Output violations as a formatted table."""
    error_count = sum(1 for v in violations if v.is_error)
    warning_count = sum(1 for v in violations if v.is_warning)
    info_count = sum(1 for v in violations if v.is_info)

    print(f"\n{'=' * 60}")
    print("PURE PYTHON DRC CHECK")
    print(f"{'=' * 60}")
    print(f"File: {pcb_path.name}")
    print(f"Manufacturer: {mfr.upper()}")
    print(f"Layers: {layers}")
    print(f"Rules checked: {results.rules_checked}")

    print("\nResults:")
    print(f"  Errors:     {error_count}")
    print(f"  Warnings:   {warning_count}")
    if info_count > 0:
        print(f"  Infos:      {info_count}")
    if results.suppressed_count > 0:
        print(f"  Suppressed: {results.suppressed_count} (standard library footprints)")

    if not violations:
        print(f"\n{'=' * 60}")
        print("DRC PASSED - No violations found")
        return

    # Group by rule_id summary
    by_rule: dict[str, dict[str, int]] = {}
    for v in violations:
        if v.rule_id not in by_rule:
            by_rule[v.rule_id] = {"errors": 0, "warnings": 0, "infos": 0}
        if v.is_error:
            by_rule[v.rule_id]["errors"] += 1
        elif v.is_info:
            by_rule[v.rule_id]["infos"] += 1
        else:
            by_rule[v.rule_id]["warnings"] += 1

    print(f"\n{'-' * 60}")
    print("BY RULE:")
    for rule_id, counts in sorted(
        by_rule.items(),
        key=lambda x: -(x[1]["errors"] + x[1]["warnings"] + x[1]["infos"]),
    ):
        parts = []
        if counts["errors"]:
            parts.append(f"{counts['errors']} error{'s' if counts['errors'] != 1 else ''}")
        if counts["warnings"]:
            parts.append(f"{counts['warnings']} warning{'s' if counts['warnings'] != 1 else ''}")
        if counts["infos"]:
            parts.append(f"{counts['infos']} info{'s' if counts['infos'] != 1 else ''}")
        print(f"  {rule_id}: {', '.join(parts)}")

    # Detailed output
    errors = [v for v in violations if v.is_error]
    warnings = [v for v in violations if v.is_warning]
    infos = [v for v in violations if v.is_info]

    if errors:
        print(f"\n{'-' * 60}")
        print("ERRORS (must fix):")
        for v in errors:
            _print_violation(v, verbose)

    if warnings:
        print(f"\n{'-' * 60}")
        print("WARNINGS (review recommended):")
        display_warnings = warnings if verbose else warnings[:10]
        for v in display_warnings:
            _print_violation(v, verbose)
        if len(warnings) > 10 and not verbose:
            print(f"\n  ... and {len(warnings) - 10} more warnings (use --verbose)")

    if infos:
        print(f"\n{'-' * 60}")
        print("INFOS (advisory only):")
        display_infos = infos if verbose else infos[:10]
        for v in display_infos:
            _print_violation(v, verbose)
        if len(infos) > 10 and not verbose:
            print(f"\n  ... and {len(infos) - 10} more infos (use --verbose)")

    print(f"\n{'=' * 60}")
    if errors:
        print("DRC FAILED - Fix errors before manufacturing")
    elif warnings:
        print("DRC WARNING - Review warnings")
    else:
        print("DRC PASSED - Advisory infos only")


def _print_violation(v: DRCViolation, verbose: bool, indent: str = "  ") -> None:
    """Print a single violation."""
    if v.is_error:
        symbol = "X"
    elif v.is_info:
        symbol = "i"
    else:
        symbol = "!"
    print(f"\n{indent}[{symbol}] {v.rule_id}")
    print(f"{indent}    {v.message}")

    if verbose:
        if v.location:
            print(f"{indent}    -> ({v.location[0]:.2f}, {v.location[1]:.2f}) mm")
        if v.layer:
            print(f"{indent}    Layer: {v.layer}")
        if v.actual_value is not None and v.required_value is not None:
            print(f"{indent}    Actual: {v.actual_value:.3f}mm, Required: {v.required_value:.3f}mm")
        if v.items:
            print(f"{indent}    Items: {', '.join(v.items)}")
        if v.nets:
            net_labels = [n if n else "<no net>" for n in v.nets]
            print(f"{indent}    Nets: {', '.join(net_labels)}")


def output_json(
    violations: list[DRCViolation],
    results: DRCResults,
    pcb_path: Path,
    mfr: str,
    layers: int,
    meta: MetaCheckResult | None = None,
) -> None:
    """Output violations as JSON.

    Issue #3750: when ``meta`` is provided (default mode), the envelope
    grows a top-level ``meta_checks`` field.  Legacy consumers that read
    ``summary.passed`` / ``summary.errors`` / ``violations`` are
    unaffected.  Under ``--drc-only`` the ``meta`` parameter is ``None``
    and the field is omitted (``OMIT-when-absent`` convention).
    """
    error_count = sum(1 for v in violations if v.is_error)
    warning_count = sum(1 for v in violations if v.is_warning)
    info_count = sum(1 for v in violations if v.is_info)

    summary_data: dict = {
        "errors": error_count,
        "warnings": warning_count,
        "infos": info_count,
        "rules_checked": results.rules_checked,
        # Issue #2660 / Epic #2556 Phase 4N: per-rule check counter.
        # The single ``rules_checked`` integer cannot tell a CI consumer
        # WHICH rules ran -- only the aggregate.  Without this map, a
        # diff-pair CI gate cannot distinguish "rule X ran and reported
        # 0 violations" from "rule X did not run at all" (e.g., the rule
        # short-circuited because no engaged pairs were detected, which
        # would be a silent regression in detection).  Always emitted
        # (even when empty) so downstream consumers can rely on the
        # field being present.
        "rules_checked_by_rule": dict(results.rules_checked_by_rule),
        "passed": error_count == 0,
    }
    if results.suppressed_count > 0:
        summary_data["suppressed"] = results.suppressed_count

    data: dict = {
        "file": str(pcb_path),
        "manufacturer": mfr,
        "layers": layers,
        "summary": summary_data,
        "violations": [v.to_dict() for v in violations],
    }
    if meta is not None:
        data["meta_checks"] = meta.to_dict()
    print(json.dumps(data, indent=2))


def write_json_report(
    violations: list[DRCViolation],
    results: DRCResults,
    pcb_path: Path,
    mfr: str,
    layers: int,
    output_path: Path,
    meta: MetaCheckResult | None = None,
) -> None:
    """Write DRC results as a JSON report file.

    Issue #3750: ``meta_checks`` is added to the envelope when meta-mode
    is active.  Omitted under ``--drc-only`` to preserve the legacy
    on-disk schema.
    """
    error_count = sum(1 for v in violations if v.is_error)
    warning_count = sum(1 for v in violations if v.is_warning)
    info_count = sum(1 for v in violations if v.is_info)

    summary_data: dict = {
        "errors": error_count,
        "warnings": warning_count,
        "infos": info_count,
        "rules_checked": results.rules_checked,
        # See ``output_json`` for the rationale on emitting this field
        # alongside the aggregate ``rules_checked`` integer.  Issue
        # #2660 / Epic #2556 Phase 4N.
        "rules_checked_by_rule": dict(results.rules_checked_by_rule),
        "passed": error_count == 0,
    }
    if results.suppressed_count > 0:
        summary_data["suppressed"] = results.suppressed_count

    data: dict = {
        "file": str(pcb_path),
        "manufacturer": mfr,
        "layers": layers,
        "summary": summary_data,
        "violations": [v.to_dict() for v in violations],
    }
    if meta is not None:
        data["meta_checks"] = meta.to_dict()
    output_path.write_text(json.dumps(data, indent=2) + "\n")


def output_summary(
    violations: list[DRCViolation],
    results: DRCResults,
    pcb_path: Path,
) -> None:
    """Output violation summary by rule."""
    if not violations:
        msg = f"  {results.rules_checked} rules checked, no violations found."
        if results.suppressed_count > 0:
            msg += (
                f"\n  ({results.suppressed_count} silkscreen warnings suppressed"
                f" -- standard library footprints)"
            )
        print(f"DRC PASSED: {pcb_path.name}")
        print(msg)
        return

    print(f"DRC Summary: {pcb_path.name}")
    print("=" * 50)

    # Group by rule_id
    by_rule: dict[str, dict[str, int]] = {}
    for v in violations:
        key = v.rule_id
        if key not in by_rule:
            by_rule[key] = {"errors": 0, "warnings": 0, "infos": 0}
        if v.is_error:
            by_rule[key]["errors"] += 1
        elif v.is_info:
            by_rule[key]["infos"] += 1
        else:
            by_rule[key]["warnings"] += 1

    print(f"{'Rule ID':<30} {'Errors':<8} {'Warnings':<10} {'Infos':<8}")
    print("-" * 60)

    for rule_id, counts in sorted(by_rule.items()):
        print(f"{rule_id:<30} {counts['errors']:<8} {counts['warnings']:<10} {counts['infos']:<8}")

    print("-" * 60)
    total_errors = sum(c["errors"] for c in by_rule.values())
    total_warnings = sum(c["warnings"] for c in by_rule.values())
    total_infos = sum(c["infos"] for c in by_rule.values())
    print(f"{'TOTAL':<30} {total_errors:<8} {total_warnings:<10} {total_infos:<8}")


if __name__ == "__main__":
    sys.exit(main())
