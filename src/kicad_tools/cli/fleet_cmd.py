"""Fleet-wide PCB status CLI command.

Survey every board under a fleet root and report routing completion plus
manufacturing artifact readiness in a single shot. Designed to answer the
question "are all our boards manufacturing-ready?" without requiring agents or
humans to grep across multiple output directories.

Usage::

    kct fleet status
    kct fleet status --boards-dir boards/
    kct fleet status --format json
    kct fleet status --ship-only
    kct fleet status --include-stale
    kct fleet status --pattern '*_routed.kicad_pcb'

Exit Codes:
    0 - All surveyed boards are ship-ready
    1 - Argparse / IO error
    2 - One or more boards are not ship-ready (default semantics, matches
        ``net-status``)
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.analysis.net_status import NetStatusAnalyzer

SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class RoutingStatus:
    """Routing completion details for a single board."""

    total_pads: int = 0
    connected_pads: int = 0
    total_nets: int = 0
    complete_nets: int = 0
    incomplete_nets: int = 0
    unrouted_nets: int = 0
    error: str | None = None

    @property
    def completion_pct(self) -> float:
        if self.total_pads == 0:
            return 0.0
        return (self.connected_pads / self.total_pads) * 100.0

    @property
    def routing_complete(self) -> bool:
        if self.error is not None:
            return False
        # A board is routing-complete iff every multi-pad net is fully
        # connected. Single-pad nets are not counted by NetStatusAnalyzer.
        return (self.incomplete_nets + self.unrouted_nets) == 0 and self.total_nets > 0

    def to_dict(self) -> dict:
        data: dict = {
            "total_pads": self.total_pads,
            "connected_pads": self.connected_pads,
            "completion_pct": round(self.completion_pct, 2),
            "total_nets": self.total_nets,
            "complete_nets": self.complete_nets,
            "incomplete_nets": self.incomplete_nets,
            "unrouted_nets": self.unrouted_nets,
            "routing_complete": self.routing_complete,
        }
        if self.error is not None:
            data["error"] = self.error
        return data


@dataclass
class ManufacturingStatus:
    """Manufacturing artifact presence/freshness for a single board."""

    dir_exists: bool = False
    has_gerbers: bool = False
    has_bom: bool = False
    has_cpl: bool = False
    has_manifest: bool = False
    manifest_mtime: float | None = None
    stale: bool = False

    @property
    def has_any(self) -> bool:
        return self.has_gerbers or self.has_bom or self.has_cpl or self.has_manifest

    @property
    def has_all(self) -> bool:
        return self.has_gerbers and self.has_bom and self.has_cpl and self.has_manifest

    def to_dict(self) -> dict:
        return {
            "dir_exists": self.dir_exists,
            "has_gerbers": self.has_gerbers,
            "has_bom": self.has_bom,
            "has_cpl": self.has_cpl,
            "has_manifest": self.has_manifest,
            "manifest_mtime": _iso_or_none(self.manifest_mtime),
            "stale": self.stale,
        }


@dataclass
class BoardStatus:
    """Aggregated status for a single board."""

    name: str
    routed_pcb: Path | None
    routed_mtime: float | None
    routing: RoutingStatus
    manufacturing: ManufacturingStatus
    blockers: list[str] = field(default_factory=list)

    @property
    def ship_ready(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "routed_pcb": str(self.routed_pcb) if self.routed_pcb else None,
            "routed_mtime": _iso_or_none(self.routed_mtime),
            "routing": self.routing.to_dict(),
            "manufacturing": self.manufacturing.to_dict(),
            "ship_ready": self.ship_ready,
            "blockers": list(self.blockers),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso_or_none(mtime: float | None) -> str | None:
    if mtime is None:
        return None
    return (
        _dt.datetime.fromtimestamp(mtime, tz=_dt.timezone.utc)
        .isoformat(timespec="seconds")
    )


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _discover_routed_pcb(board_dir: Path, pattern: str) -> Path | None:
    """Find the first routed PCB inside a board's output/ directory."""
    output_dir = board_dir / "output"
    if not output_dir.is_dir():
        return None
    matches = sorted(output_dir.glob(pattern))
    if not matches:
        return None
    return matches[0]


def _detect_manufacturing(board_dir: Path) -> ManufacturingStatus:
    """Detect manufacturing artifacts for a single board.

    Prefer ``manifest.json``'s ``files`` keys when present; fall back to
    directory globs (so we tolerate manufacturer-name variations such as
    ``bom_jlcpcb.csv`` vs ``bom_pcbway.csv``).
    """
    mfg = ManufacturingStatus()
    mfg_dir = board_dir / "output" / "manufacturing"
    if not mfg_dir.is_dir():
        return mfg

    mfg.dir_exists = True
    manifest_path = mfg_dir / "manifest.json"

    if manifest_path.exists():
        mfg.has_manifest = True
        try:
            mfg.manifest_mtime = manifest_path.stat().st_mtime
        except OSError:
            mfg.manifest_mtime = None
        try:
            with manifest_path.open() as fh:
                manifest = json.load(fh)
            files = manifest.get("files") if isinstance(manifest, dict) else None
            if isinstance(files, dict):
                for name in files.keys():
                    lower = name.lower()
                    if lower.startswith("bom_") and lower.endswith(".csv"):
                        mfg.has_bom = True
                    elif lower.startswith("cpl_") and lower.endswith(".csv"):
                        mfg.has_cpl = True
                    elif lower == "gerbers.zip":
                        mfg.has_gerbers = True
        except (OSError, json.JSONDecodeError):
            # Manifest unreadable: fall back to directory scan below.
            pass

    # Directory-scan fallback (also fills gaps if manifest's files list is
    # incomplete).
    if not mfg.has_gerbers and (mfg_dir / "gerbers.zip").is_file():
        mfg.has_gerbers = True
    if not mfg.has_bom and any(mfg_dir.glob("bom_*.csv")):
        mfg.has_bom = True
    if not mfg.has_cpl and any(mfg_dir.glob("cpl_*.csv")):
        mfg.has_cpl = True

    return mfg


def _compute_routing(routed_pcb: Path) -> RoutingStatus:
    """Run NetStatusAnalyzer on a routed PCB and tally pads/nets."""
    status = RoutingStatus()
    try:
        analyzer = NetStatusAnalyzer(routed_pcb)
        result = analyzer.analyze()
    except Exception as exc:  # pragma: no cover - defensive
        status.error = f"{type(exc).__name__}: {exc}"
        return status

    status.total_pads = sum(n.total_pads for n in result.nets)
    status.connected_pads = sum(n.connected_count for n in result.nets)
    status.total_nets = result.total_nets
    status.complete_nets = result.complete_count
    status.incomplete_nets = result.incomplete_count
    status.unrouted_nets = result.unrouted_count
    return status


def _compute_blockers(
    routing: RoutingStatus,
    mfg: ManufacturingStatus,
    routed_pcb: Path | None,
) -> list[str]:
    """First-failing list of reasons a board cannot ship."""
    blockers: list[str] = []
    if routed_pcb is None:
        blockers.append("no routed PCB")
        return blockers
    if routing.error is not None:
        blockers.append(f"routing analysis failed: {routing.error}")
        return blockers
    if not routing.routing_complete:
        incomplete = routing.incomplete_nets + routing.unrouted_nets
        blockers.append(
            f"incomplete routing ({incomplete}/{routing.total_nets} nets)"
        )
    if not mfg.dir_exists:
        blockers.append("no manufacturing/ dir")
        return blockers
    if not mfg.has_gerbers:
        blockers.append("missing gerbers")
    if not mfg.has_bom:
        blockers.append("missing BOM")
    if not mfg.has_cpl:
        blockers.append("missing CPL")
    if not mfg.has_manifest:
        blockers.append("no manifest")
    if mfg.stale:
        blockers.append("artifacts stale")
    return blockers


def _survey_board(board_dir: Path, pattern: str) -> BoardStatus:
    """Build a BoardStatus for a single board directory."""
    routed_pcb = _discover_routed_pcb(board_dir, pattern)
    routed_mtime: float | None = None
    routing = RoutingStatus()
    mfg = ManufacturingStatus()

    if routed_pcb is not None:
        try:
            routed_mtime = routed_pcb.stat().st_mtime
        except OSError:
            routed_mtime = None
        routing = _compute_routing(routed_pcb)
        mfg = _detect_manufacturing(board_dir)
        if (
            routed_mtime is not None
            and mfg.manifest_mtime is not None
            and routed_mtime > mfg.manifest_mtime
        ):
            mfg.stale = True

    blockers = _compute_blockers(routing, mfg, routed_pcb)

    return BoardStatus(
        name=board_dir.name,
        routed_pcb=routed_pcb,
        routed_mtime=routed_mtime,
        routing=routing,
        manufacturing=mfg,
        blockers=blockers,
    )


def _discover_boards(boards_dir: Path, pattern: str) -> list[BoardStatus]:
    """Survey every board sub-directory of ``boards_dir``.

    A board is discovered if it contains a routed PCB matching ``pattern``
    under ``<board>/output/``. Boards without a routed PCB are silently
    skipped (they haven't reached the routing stage yet).
    """
    if not boards_dir.is_dir():
        return []
    boards: list[BoardStatus] = []
    for entry in sorted(boards_dir.iterdir()):
        if not entry.is_dir():
            continue
        # Skip dotfiles / hidden dirs and obvious non-board names.
        if entry.name.startswith("."):
            continue
        # A directory without an output/ sub-dir is not yet at routing
        # stage -- skip it silently.
        output_dir = entry / "output"
        if not output_dir.is_dir():
            continue
        # Skip directories with no routed PCB matching the pattern.
        if _discover_routed_pcb(entry, pattern) is None:
            continue
        boards.append(_survey_board(entry, pattern))
    return boards


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------


def _mfr_letters(mfg: ManufacturingStatus) -> str:
    if not mfg.dir_exists or not mfg.has_any:
        return "-"
    parts = []
    if mfg.has_bom:
        parts.append("B")
    if mfg.has_cpl:
        parts.append("C")
    if mfg.has_gerbers:
        parts.append("G")
    if mfg.has_manifest:
        parts.append("M")
    return "/".join(parts) if parts else "-"


def _stale_label(mfg: ManufacturingStatus) -> str:
    if not mfg.has_manifest:
        return "-"
    return "STALE" if mfg.stale else "fresh"


def _format_table(
    boards: list[BoardStatus],
    boards_dir: Path,
    *,
    ship_only: bool,
) -> str:
    """Format a fixed-width plain-ASCII table."""
    lines: list[str] = []
    header = f"{'Board':<28} {'Pads':>7} {'%':>5} {'Mfr':<8} {'Stale':<6} Ship?"
    sep = "-" * len(header)
    lines.append(header)
    lines.append(sep)

    visible = [b for b in boards if (not ship_only or b.ship_ready)]

    if not visible:
        if not boards:
            lines.append("No boards found")
        else:
            lines.append("(no ship-ready boards)")
    else:
        for b in visible:
            pads = f"{b.routing.connected_pads}/{b.routing.total_pads}"
            pct = f"{b.routing.completion_pct:.0f}%"
            mfr = _mfr_letters(b.manufacturing)
            stale = _stale_label(b.manufacturing)
            if b.ship_ready:
                ship = "YES"
            else:
                ship = f"NO  ({b.blockers[0]})"
            lines.append(
                f"{b.name[:28]:<28} {pads:>7} {pct:>5} {mfr:<8} {stale:<6} {ship}"
            )

    # Footer (always uses full board list, not filtered view).
    if boards:
        lines.append("")
        ship_ready = sum(1 for b in boards if b.ship_ready)
        incomplete = sum(
            1
            for b in boards
            if not b.routing.routing_complete and b.routing.error is None
        )
        stale_count = sum(1 for b in boards if b.manufacturing.stale)
        lines.append(
            f"{len(boards)} boards surveyed, {ship_ready} ship-ready, "
            f"{incomplete} incomplete, {stale_count} artifacts stale"
        )
        lines.append(f"boards-dir: {boards_dir}")

    return "\n".join(lines)


def _format_json(boards: list[BoardStatus], boards_dir: Path) -> str:
    summary = {
        "total": len(boards),
        "ship_ready": sum(1 for b in boards if b.ship_ready),
        "incomplete_routing": sum(
            1
            for b in boards
            if not b.routing.routing_complete and b.routing.error is None
        ),
        "stale_artifacts": sum(1 for b in boards if b.manufacturing.stale),
        "missing_artifacts": sum(
            1
            for b in boards
            if not b.manufacturing.has_all
        ),
    }
    doc = {
        "schema_version": SCHEMA_VERSION,
        "surveyed_at": _now_iso(),
        "boards_dir": str(boards_dir),
        "summary": summary,
        "boards": [b.to_dict() for b in boards],
    }
    return json.dumps(doc, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kicad-fleet",
        description="Fleet-wide PCB status and operations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    subparsers = parser.add_subparsers(dest="fleet_command", help="Fleet commands")

    status_parser = subparsers.add_parser(
        "status",
        help="Survey routing + manufacturing status for all boards",
    )
    status_parser.add_argument(
        "--boards-dir",
        default="boards",
        help="Root directory containing per-board subdirs (default: boards)",
    )
    status_parser.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )
    status_parser.add_argument(
        "--ship-only",
        action="store_true",
        help="Show only ship-ready boards (table only; JSON always lists all)",
    )
    status_parser.add_argument(
        "--include-stale",
        action="store_true",
        help=(
            "Reserved for future use: in the default behavior stale artifacts "
            "already demote ship-ready to NO. Currently a no-op flag retained "
            "for forward compatibility with --ship-only filter semantics."
        ),
    )
    status_parser.add_argument(
        "--pattern",
        default="*_routed.kicad_pcb",
        help="Glob to identify routed PCB inside output/ (default: *_routed.kicad_pcb)",
    )
    return parser


def run_status(args: argparse.Namespace) -> int:
    """Execute the ``fleet status`` sub-action."""
    boards_dir = Path(args.boards_dir)
    boards = _discover_boards(boards_dir, args.pattern)

    if args.format == "json":
        print(_format_json(boards, boards_dir))
    else:
        print(_format_table(boards, boards_dir, ship_only=args.ship_only))

    if not boards:
        return 2  # No boards found counts as not-ship-ready.
    if all(b.ship_ready for b in boards):
        return 0
    return 2


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the fleet command."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.fleet_command:
        parser.print_help()
        return 0

    if args.fleet_command == "status":
        return run_status(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
