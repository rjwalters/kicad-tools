"""``kct tapeout-sign-off``: record the explicit human sign-off ``submit-jlcpcb`` requires (#8537)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..export.submission_workflow import WorkflowError, sign_off_bundle


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("bundle", help="Tapeout bundle directory (contains manifest.json)")
    parser.add_argument("--board", required=True, help="Board .kicad_pcb the bundle was cut from")
    parser.add_argument(
        "--readiness",
        required=True,
        help="Passing `kct readiness` receipt (readiness.json) covering --board and the bundle",
    )
    parser.add_argument(
        "--reviewer", required=True, help="Your name; recorded as the human sign-off"
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text); json emits one document on stdout",
    )


def run(args: argparse.Namespace) -> int:
    try:
        record = sign_off_bundle(
            Path(args.bundle), Path(args.board), args.reviewer, readiness=Path(args.readiness)
        )
    except WorkflowError as exc:
        if args.format == "json":
            print(json.dumps({"error": str(exc)}, indent=2))
        else:
            print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    signoff = record["human_signoff"]
    if args.format == "json":
        print(
            json.dumps(
                {
                    "reviewer": signoff["reviewer"],
                    "signed_at": signoff["signed_at"],
                    "board_sha256": record["board_sha256"],
                },
                indent=2,
            )
        )
        return 0
    print(f"Signed off by {signoff['reviewer']} at {signoff['signed_at']}")
    print(f"board sha256: {record['board_sha256']}")
    return 0
