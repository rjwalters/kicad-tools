"""``kct export-formats`` -- PDF/SVG/STEP/IPC-2581/ODB++ via kicad-cli (Issue #8017).

Usage:
    kct export-formats board.kicad_pcb --formats pdf,svg,step,ipc2581,odb -o out/
    kct export-formats board.kicad_pcb --format json

Exits 0 only if every requested format produced a non-empty file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..export.kicad_cli_exports import (
    DEFAULT_LAYERS,
    FORMAT_SUFFIX,
    export_formats,
    parse_formats,
)
from .format_options import add_format_flag, wants_json


def build_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    p = parser or argparse.ArgumentParser(prog="kct export-formats")
    p.add_argument("pcb", help="Path to .kicad_pcb file")
    p.add_argument(
        "--formats",
        dest="formats",
        default=",".join(FORMAT_SUFFIX),
        help=f"Comma-separated formats ({', '.join(FORMAT_SUFFIX)}; default: all)",
    )
    p.add_argument(
        "-o", "--output", default=None, help="Output directory (default: <pcb-dir>/formats)"
    )
    p.add_argument(
        "--layers",
        default=DEFAULT_LAYERS,
        help=f"Layers for PDF/SVG (default: {DEFAULT_LAYERS})",
    )
    add_format_flag(p, help_text="Output format; json emits a summary of what was produced")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(args)


def run(args: argparse.Namespace) -> int:
    pcb = Path(args.pcb)
    if not pcb.is_file():
        print(f"Error: PCB file not found: {pcb}", file=sys.stderr)
        return 2
    try:
        formats = parse_formats(args.formats)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    out_dir = Path(args.output) if args.output else pcb.resolve().parent / "formats"

    results = export_formats(pcb, out_dir, formats, layers=args.layers)
    ok = all(r.status == "ok" for r in results)

    if wants_json(args):
        print(
            json.dumps(
                {
                    "pcb": str(pcb),
                    "output_dir": str(out_dir),
                    "success": ok,
                    "formats": [r.to_dict() for r in results],
                },
                indent=2,
            )
        )
    else:
        for r in results:
            if r.status == "ok":
                print(f"  ok      {r.format:8} {r.path} ({r.size_bytes} bytes)")
            else:
                print(f"  FAILED  {r.format:8} {r.error}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
