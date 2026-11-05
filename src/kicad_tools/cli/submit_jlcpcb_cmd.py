"""``kct submit-jlcpcb``: agent-prepared JLCPCB PCBA submission (issue #8466).

Composes the existing submission plan / stock / upload / review pieces via
:mod:`kicad_tools.export.submission_workflow`. It never places an order.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..export.submission_plan import SubmissionError
from ..export.submission_workflow import (
    WorkflowError,
    format_stock_failures,
    run_submission,
)
from .format_options import add_format_flag


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("bundle", help="Tapeout bundle directory (contains manifest.json)")
    parser.add_argument("--board", required=True, help="Board .kicad_pcb the bundle was cut from")
    parser.add_argument("--schematic", required=True, help="Schematic .kicad_sch (under board dir)")
    parser.add_argument("--quantity", type=int, default=1, help="Boards to order (default: 1)")
    parser.add_argument("--output", "-o", required=True, help="New directory for plan and review")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preflight, prepare/verify, stock gate and review page only; no upload",
    )
    parser.add_argument(
        "--inventory-covered",
        metavar="C123,C456",
        default="",
        help="Catalog ids you explicitly hold in your JLC parts inventory (shown as covered)",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload the exact Gerber bytes via the API (needs JLC credentials and --reviewer)",
    )
    parser.add_argument("--reviewer", help="Your name; confirms the upload checklist")
    add_format_flag(parser)


def run(args: argparse.Namespace) -> int:
    from ..parts.jlcpcb_api import JLCCredentials, JLCOpenAPIClient

    credentials = JLCCredentials.from_env()
    upload = bool(args.upload)
    covered = [c for c in args.inventory_covered.split(",") if c.strip()]
    try:
        result = run_submission(
            bundle_root=Path(args.bundle),
            board=Path(args.board),
            schematic=Path(args.schematic),
            quantity=args.quantity,
            output_dir=Path(args.output),
            dry_run=bool(args.dry_run),
            inventory_source=JLCOpenAPIClient(credentials) if credentials else None,
            inventory_covered=covered,
            upload=upload,
            reviewer=args.reviewer,
            credentials=credentials,
        )
    except (WorkflowError, SubmissionError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    failures = list(format_stock_failures(result.stock))
    if getattr(args, "format", "text") == "json":
        print(
            json.dumps(
                {
                    "states": dict(result.states),
                    "stock_failures": failures,
                    "review_page": str(result.page.directory),
                    "stock_blocked": bool(result.stock.blocked),
                },
                indent=2,
            )
        )
        return 1 if result.stock.blocked else 0
    for key, value in result.states.items():
        print(f"{key}: {value}")
    for failure in failures:
        print(f"  STOCK: {failure}")
    print(f"review page: {result.page.directory}")
    return 1 if result.stock.blocked else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kct submit-jlcpcb", description=__doc__)
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
