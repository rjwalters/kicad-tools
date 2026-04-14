"""report CLI command: generate a Markdown design report from data snapshots.

Usage:
    kct report generate project.kicad_pro --mfr jlcpcb -o reports/
    kct report generate board.kicad_pcb --mfr jlcpcb --data-dir data/
    kct report generate board.kicad_pcb --mfr jlcpcb --no-figures
    kct report generate board.kicad_pcb --mfr jlcpcb --sch path/to/root.kicad_sch
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.report.figures import FigureEntry
    from kicad_tools.report.models import ReportData


def main(argv: list[str] | None = None) -> int:
    """Entry point for the report CLI command."""
    parser = argparse.ArgumentParser(
        prog="kct report",
        description="Generate a Markdown design report.",
    )
    sub = parser.add_subparsers(dest="report_subcommand")

    gen_parser = sub.add_parser("generate", help="Generate a design report")
    gen_parser.add_argument(
        "input",
        help="Path to .kicad_pro or .kicad_pcb file",
    )
    gen_parser.add_argument(
        "--mfr",
        default="unknown",
        help="Target manufacturer (default: unknown)",
    )
    gen_parser.add_argument(
        "-o",
        "--output",
        default="reports",
        help="Output directory for versioned reports (default: reports/)",
    )
    gen_parser.add_argument(
        "--data-dir",
        default=None,
        help="Directory containing pre-collected data/ and figures/ snapshots",
    )
    gen_parser.add_argument(
        "--template",
        default=None,
        help="Path to a custom Jinja2 template file",
    )
    gen_parser.add_argument(
        "--sch",
        default=None,
        help="Path to root .kicad_sch file (inferred from input if omitted)",
    )
    gen_parser.add_argument(
        "--no-figures",
        action="store_true",
        default=False,
        help="Skip figure generation (useful when kicad-cli/cairosvg are unavailable)",
    )

    args = parser.parse_args(argv)

    if not args.report_subcommand:
        parser.print_help()
        return 0

    if args.report_subcommand == "generate":
        return _run_generate(args)

    return 0


def _run_generate(args: argparse.Namespace) -> int:
    """Execute the ``generate`` sub-command."""
    try:
        from kicad_tools.report import ReportData, ReportGenerator
    except ImportError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    input_path = Path(args.input)
    project_name = input_path.stem

    # Build ReportData from data-dir JSON files if available
    data_kwargs = _load_data_dir(args.data_dir) if args.data_dir else {}

    data = ReportData(
        project_name=project_name,
        revision=data_kwargs.pop("revision", "1"),
        date=data_kwargs.pop(
            "date",
            __import__("datetime").date.today().isoformat(),
        ),
        manufacturer=args.mfr,
        **data_kwargs,
    )

    template_path = Path(args.template) if args.template else None
    generator = ReportGenerator(template_path=template_path)

    # --- Figure generation ---
    # Only attempt when: no --data-dir (pre-collected data path), no --no-figures,
    # and the input is a .kicad_pcb file.
    version_dir: Path | None = None
    if not args.no_figures and not args.data_dir and input_path.suffix == ".kicad_pcb":
        version_dir = generator.next_version_dir(Path(args.output))
        figures_dir = version_dir / "figures"
        _generate_figures(args, input_path, figures_dir, data)

    try:
        report_path = generator.generate(data, Path(args.output), version_dir=version_dir)
    except FileExistsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Report written to {report_path}")
    return 0


def _unwrap_envelope(payload: dict) -> dict | None:
    """Extract the ``data`` value from a collector envelope.

    The collector wraps every snapshot in
    ``{"schema_version": ..., "generated_at": ..., "data": <actual>}``.
    If *payload* looks like an envelope, return ``payload["data"]``;
    otherwise return *payload* unchanged so flat (non-enveloped) JSON
    files continue to work.
    """
    if isinstance(payload, dict) and "schema_version" in payload and "data" in payload:
        return payload["data"]
    return payload


def _generate_figures(
    args: argparse.Namespace,
    input_path: Path,
    figures_dir: Path,
    data: ReportData,
) -> None:
    """Attempt figure generation, populating *data* in place.

    Handles graceful degradation: prints a warning to stderr and
    continues without figures if dependencies (kicad-cli / cairosvg)
    are absent.
    """
    try:
        from kicad_tools.report import ReportFigureGenerator
    except ImportError as exc:
        print(
            f"Warning: figure generation skipped — {exc}",
            file=sys.stderr,
        )
        return

    sch_path = Path(args.sch) if args.sch else input_path.with_suffix(".kicad_sch")

    try:
        fig_gen = ReportFigureGenerator()
        print("Generating figures...")
        entries = fig_gen.generate_all(input_path, sch_path, figures_dir)
        data.pcb_figures = _entries_to_pcb_figures(entries)
        data.schematic_sheets = _entries_to_schematic_sheets(entries)
    except RuntimeError as exc:
        print(
            f"Warning: figure generation skipped — {exc}",
            file=sys.stderr,
        )


def _entries_to_pcb_figures(entries: list[FigureEntry]) -> dict | None:
    """Convert a list of :class:`FigureEntry` to the dict shape expected by
    :attr:`ReportData.pcb_figures`.

    Returns ``None`` when no PCB figure entries are present.
    """
    type_to_key = {
        "pcb_front": "front",
        "pcb_back": "back",
        "pcb_copper": "copper",
        "assembly": "assembly",
    }
    result: dict[str, str] = {}
    for entry in entries:
        key = type_to_key.get(entry.figure_type)
        if key is not None:
            result[key] = f"figures/{entry.filename}"
    return result or None


def _entries_to_schematic_sheets(entries: list[FigureEntry]) -> list[dict] | None:
    """Convert a list of :class:`FigureEntry` to the list shape expected by
    :attr:`ReportData.schematic_sheets`.

    Returns ``None`` when no schematic entries are present.
    """
    sheets = [
        {"name": entry.caption, "figure_path": f"figures/{entry.filename}"}
        for entry in entries
        if entry.figure_type == "schematic"
    ]
    return sheets or None


def _load_data_dir(data_dir_str: str) -> dict:
    """Load JSON files from a data directory into ReportData kwargs."""
    data_dir = Path(data_dir_str)
    result: dict = {}

    # Map of JSON file names to ReportData field names.
    # The collector writes ``board_summary.json`` and ``drc_summary.json``,
    # so the mapping must match those filenames.
    mappings = {
        "board_summary.json": "board_stats",
        "bom.json": "bom_groups",
        "drc_summary.json": "drc",
        "audit.json": "audit",
        "net_status.json": "net_status",
        "cost.json": "cost",
        "schematic_sheets.json": "schematic_sheets",
        "pcb_figures.json": "pcb_figures",
    }

    for filename, field_name in mappings.items():
        json_path = data_dir / filename
        if json_path.exists():
            with open(json_path, encoding="utf-8") as f:
                raw = json.load(f)
            data = _unwrap_envelope(raw)
            # Skip sections whose collector failed (data: null envelope).
            if data is None:
                continue
            result[field_name] = data

    # --- Post-load transformations ------------------------------------------

    # BOM: the collector nests the group list under a ``groups`` key;
    # ReportData.bom_groups expects a plain list[dict].
    if "bom_groups" in result and isinstance(result["bom_groups"], dict):
        result["bom_groups"] = result["bom_groups"].get("groups", [])

    # net_status: collector writes ``completion_pct`` but the template
    # reads ``completion_percent``.
    ns = result.get("net_status")
    if isinstance(ns, dict) and "completion_pct" in ns:
        ns["completion_percent"] = ns.pop("completion_pct")

    # board_stats: collector writes ``footprint_count`` but the template
    # checks ``component_count``.
    bs = result.get("board_stats")
    if isinstance(bs, dict) and "footprint_count" in bs:
        bs["component_count"] = bs.pop("footprint_count")

    # Load notes from text file
    notes_path = data_dir / "notes.txt"
    if notes_path.exists():
        result["notes"] = notes_path.read_text(encoding="utf-8").strip()

    # Load metadata fields
    meta_path = data_dir / "metadata.json"
    if meta_path.exists():
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
            if "revision" in meta:
                result["revision"] = meta["revision"]
            if "date" in meta:
                result["date"] = meta["date"]
            if "git_hash" in meta:
                result["git_hash"] = meta["git_hash"]

    return result
