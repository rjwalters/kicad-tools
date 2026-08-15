"""report CLI command: generate a Markdown design report from data snapshots.

Usage:
    kct report generate project.kicad_pro --mfr jlcpcb -o reports/
    kct report generate board.kicad_pcb --mfr jlcpcb --data-dir data/
    kct report generate board.kicad_pcb --mfr jlcpcb --no-figures
    kct report generate board.kicad_pcb --mfr jlcpcb --sch path/to/root.kicad_sch

Machine output (``--format json``, issue #4674): ``report generate`` prints one
document naming the ``report_path`` it wrote, the ``pdf_path`` when PDF
rendering succeeded, the ``data_source`` it used (``auto-collect`` /
``data-dir`` / ``skeleton``) and a ``figures`` block recording whether figures
were generated or why they were skipped.  Progress prose is suppressed and
third-party stdout chatter is diverted to stderr so stdout carries exactly one
document; warnings keep going to stderr either way.  See
``docs/reference/machine-output.md``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .format_options import FORMAT_JSON, add_format_flag, emit_json, stdout_to_stderr_when

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
    gen_parser.add_argument(
        "--quantity",
        type=int,
        default=5,
        help="Quantity for cost estimation (default: 5)",
    )
    gen_parser.add_argument(
        "--skip-erc",
        action="store_true",
        help="Skip ERC during auto-collection",
    )
    gen_parser.add_argument(
        "--skip-collect",
        action="store_true",
        help="Skip auto-collection; generate skeleton report (legacy behavior)",
    )
    add_format_flag(gen_parser)

    # --- Interactive DRC report sub-command ---
    int_parser = sub.add_parser(
        "interactive",
        help="Generate an interactive HTML DRC report with PCB visualization",
    )
    int_parser.add_argument(
        "input",
        help="Path to .kicad_pcb file",
    )
    int_parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output HTML file path (default: <input_stem>_interactive.html)",
    )
    int_parser.add_argument(
        "--project-name",
        default=None,
        help="Project display name (default: PCB filename stem)",
    )

    args = parser.parse_args(argv)

    if not args.report_subcommand:
        parser.print_help()
        return 0

    if args.report_subcommand == "generate":
        return _run_generate(args)

    if args.report_subcommand == "interactive":
        return _run_interactive(args)

    return 0


def _run_generate(args: argparse.Namespace) -> int:
    """Execute the ``generate`` sub-command."""
    as_json = getattr(args, "format", "text") == FORMAT_JSON

    def say(message: str) -> None:
        """Print progress prose unless JSON mode owns stdout."""
        if not as_json:
            print(message)

    def fail(message: str) -> int:
        """Report a failure as a document (JSON) or on stderr (text)."""
        if as_json:
            emit_json(
                {
                    "command": "generate",
                    "input": str(args.input),
                    "error": message,
                    "success": False,
                }
            )
        else:
            print(message, file=sys.stderr)
        return 1

    try:
        from kicad_tools.report import ReportData, ReportGenerator
    except ImportError as exc:
        return fail(str(exc))

    input_path = Path(args.input)
    project_name = input_path.stem

    # Resolve .kicad_pro to .kicad_pcb by stem matching so the S-expression
    # parser receives a PCB file rather than JSON project metadata.
    if input_path.suffix == ".kicad_pro":
        pcb_path = input_path.with_suffix(".kicad_pcb")
        if not pcb_path.exists():
            return fail(f"Error: PCB file not found: {pcb_path}")
        input_path = pcb_path

    # Determine data source: explicit --data-dir, auto-collection, or skeleton
    version_dir: Path | None = None
    data_source = "auto-collect"
    if args.data_dir:
        data_source = "data-dir"
        data_kwargs = _load_data_dir(args.data_dir)
    elif args.skip_collect:
        data_source = "skeleton"
        data_kwargs = {}
    else:
        # Auto-collect: write snapshots into the versioned output directory.
        # This pre-determines the version directory so figures land in the same vN/.
        with stdout_to_stderr_when(as_json):
            version_dir, data_kwargs = _auto_collect(
                pcb_path=input_path,
                output_dir=Path(args.output),
                manufacturer=args.mfr,
                quantity=getattr(args, "quantity", 5),
                skip_erc=getattr(args, "skip_erc", False),
            )

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
    if args.no_figures:
        figures: dict = {"generated": False, "skipped_reason": "disabled by --no-figures"}
    elif args.data_dir:
        figures = {"generated": False, "skipped_reason": "pre-collected via --data-dir"}
    elif input_path.suffix != ".kicad_pcb":
        figures = {
            "generated": False,
            "skipped_reason": f"unsupported input type {input_path.suffix}",
        }
    else:
        if version_dir is None:
            version_dir = generator.next_version_dir(Path(args.output))
        figures_dir = version_dir / "figures"
        with stdout_to_stderr_when(as_json):
            figures = _generate_figures(args, input_path, figures_dir, data)

    try:
        report_path = generator.generate(data, Path(args.output), version_dir=version_dir)
    except FileExistsError as exc:
        return fail(f"Error: {exc}")

    say(f"Report written to {report_path}")

    # Attempt to render Markdown to HTML then PDF.
    pdf_path: Path | None = None
    with stdout_to_stderr_when(as_json):
        try:
            from kicad_tools.report.renderers import render_html, render_pdf

            md_content = report_path.read_text(encoding="utf-8")
            figures_dir = version_dir / "figures" if version_dir is not None else None
            html_content = render_html(
                md_content,
                figures_dir=(
                    figures_dir if figures_dir is not None and figures_dir.is_dir() else None
                ),
            )
            pdf_path = report_path.with_suffix(".pdf")
            render_pdf(html_content, pdf_path)
            say(f"PDF report written to {pdf_path}")
        except ImportError:
            pdf_path = None
            print(
                "Hint: install 'kicad-tools[report]' for automatic PDF generation",
                file=sys.stderr,
            )
        except Exception as exc:
            pdf_path = None
            print(f"Warning: PDF rendering failed: {exc}", file=sys.stderr)

    if as_json:
        emit_json(
            {
                "command": "generate",
                "input": str(input_path),
                "manufacturer": args.mfr,
                "output_dir": str(args.output),
                "project_name": project_name,
                "report_path": str(report_path),
                "pdf_path": str(pdf_path) if pdf_path is not None else None,
                "data_source": data_source,
                "figures": figures,
                "success": True,
            }
        )

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
) -> dict:
    """Attempt figure generation, populating *data* in place.

    Handles graceful degradation: prints a warning to stderr and
    continues without figures if dependencies (kicad-cli / cairosvg)
    are absent.

    Returns the ``figures`` block of the ``--format json`` document
    (``{"generated": bool, "skipped_reason": str | None}``).  The caller
    diverts stdout in JSON mode (see :func:`~.format_options.stdout_to_stderr_when`), so the
    progress line needs no special handling here.
    """

    def skipped(reason: str) -> dict:
        print(f"Warning: figure generation skipped — {reason}", file=sys.stderr)
        return {"generated": False, "skipped_reason": reason}

    try:
        from kicad_tools.report import ReportFigureGenerator
    except ImportError as exc:
        return skipped(str(exc))

    if args.sch:
        sch_path: Path | None = Path(args.sch)
    else:
        from kicad_tools.report.utils import find_schematic

        sch_path = find_schematic(input_path)

    if sch_path is None:
        return skipped("no schematic found. Use --sch to specify explicitly.")

    try:
        fig_gen = ReportFigureGenerator()
        print("Generating figures...")
        entries = fig_gen.generate_all(input_path, sch_path, figures_dir)
        data.pcb_figures = _entries_to_pcb_figures(entries)
        data.pcb_layer_figures = _entries_to_layer_figures(entries)
        data.schematic_sheets = _entries_to_schematic_sheets(entries)
    except (RuntimeError, OSError) as exc:
        hint = ""
        if isinstance(exc, OSError) and "cairo" in str(exc).lower():
            hint = (
                " (hint: auto-detection of Homebrew libcairo was attempted"
                " but failed — try: DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib"
                " kct report generate ...)"
            )
        return skipped(f"{exc}{hint}")

    return {"generated": True, "skipped_reason": None}


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


def _entries_to_layer_figures(entries: list[FigureEntry]) -> list[dict] | None:
    """Convert :class:`FigureEntry` items of type ``pcb_layer`` to the list
    shape expected by :attr:`ReportData.pcb_layer_figures`.

    Returns ``None`` when no per-layer entries are present.
    """
    layers = [
        {
            # Caption is "Copper Layer F.Cu" -- strip the prefix for the name.
            "name": entry.caption.removeprefix("Copper Layer "),
            "figure_path": f"figures/{entry.filename}",
        }
        for entry in entries
        if entry.figure_type == "pcb_layer"
    ]
    return layers or None


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


def _auto_collect(
    pcb_path: Path,
    output_dir: Path,
    manufacturer: str,
    quantity: int,
    skip_erc: bool,
) -> tuple[Path, dict]:
    """Run ReportDataCollector and return (version_dir, data_kwargs).

    The version directory is pre-determined so that collected data and the
    generated report land in the same ``vN/`` directory, avoiding a race
    where auto-versioning would bump to ``vN+1``.
    """
    from kicad_tools.report import ReportDataCollector
    from kicad_tools.report.generator import ReportGenerator

    # Pre-determine the version directory
    version_dir = ReportGenerator.next_version_dir(output_dir)
    data_dir = version_dir / "data"

    collector = ReportDataCollector(
        pcb_path=pcb_path,
        manufacturer=manufacturer,
        quantity=quantity,
        skip_erc=skip_erc,
    )
    print(f"Collecting design data into {data_dir} ...")
    collector.collect_all(data_dir)
    print("Collection complete.")

    data_kwargs = _load_data_dir(str(data_dir))
    return version_dir, data_kwargs


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
        "erc_summary.json": "erc",
        "audit.json": "audit",
        "net_status.json": "net_status",
        "cost.json": "cost",
        "schematic_sheets.json": "schematic_sheets",
        "pcb_figures.json": "pcb_figures",
        "pcb_layer_figures.json": "pcb_layer_figures",
        "analog_components.json": "analog_components",
        "narrative.json": "_narrative",
        "stackup.json": "stackup",
        "off_board.json": "off_board",
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

    # Analog components: the collector nests the list under ``components``;
    # ReportData.analog_components expects a plain list[dict].
    if "analog_components" in result and isinstance(result["analog_components"], dict):
        result["analog_components"] = result["analog_components"].get("components", [])

    # Narrative: the collector writes a single dict with sub-keys;
    # unpack into individual ReportData fields.
    if "_narrative" in result and isinstance(result["_narrative"], dict):
        narrative = result.pop("_narrative")
        for key in (
            "design_narrative",
            "functional_blocks",
            "interfaces",
            "power_architecture",
            "assembly_notes",
        ):
            val = narrative.get(key)
            if val is not None:
                result[key] = val
    else:
        result.pop("_narrative", None)

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


def _run_interactive(args: argparse.Namespace) -> int:
    """Execute the ``interactive`` sub-command."""
    input_path = Path(args.input)

    # Resolve .kicad_pro to .kicad_pcb
    if input_path.suffix == ".kicad_pro":
        input_path = input_path.with_suffix(".kicad_pcb")

    if not input_path.exists():
        print(f"Error: PCB file not found: {input_path}", file=sys.stderr)
        return 1

    if input_path.suffix != ".kicad_pcb":
        print(
            f"Error: expected .kicad_pcb file, got {input_path.suffix}",
            file=sys.stderr,
        )
        return 1

    output_path = (
        Path(args.output)
        if args.output
        else input_path.with_name(f"{input_path.stem}_interactive.html")
    )

    try:
        from kicad_tools.report.interactive import render_interactive_html

        print(f"Generating interactive report for {input_path} ...")
        html = render_interactive_html(
            pcb_path=input_path,
            project_name=args.project_name,
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(html, encoding="utf-8")
        print(f"Interactive report written to {output_path}")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
