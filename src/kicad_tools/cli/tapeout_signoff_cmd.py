"""``kct tapeout-sign-off``: record the explicit human sign-offs ``submit-jlcpcb`` requires (#8537, #8818).

Two attestations, in the order the work happens:

* ``--stage release`` (default): "the gates pass; these bytes may be sent to the
  fab for preview". Enough to upload and preview; never enough to save a cart.
* ``--stage fab-review``: "I looked at the fab's rendering of the uploaded
  bytes (gerber viewer / DFM result)". Required before the cart is saved.
"""

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
        "--stage",
        choices=["release", "fab-review"],
        default="release",
        help=(
            "release (default): gates pass, these bytes may be sent to the fab for preview "
            "(does not allow saving a cart). fab-review: you reviewed the fab's gerber "
            "viewer / DFM rendering of the uploaded bytes; required before the cart is saved"
        ),
    )
    parser.add_argument(
        "--readiness",
        help=(
            "Required for --stage release. Passing `kct readiness` receipt covering --board and the bundle: "
            "<board>/output/readiness.json or the `--verify` receipt at "
            "<board>/output/readiness-verification/readiness.json"
        ),
    )
    parser.add_argument(
        "--reviewer", required=True, help="Your name; recorded as the human sign-off for --stage"
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text); json emits one document on stdout",
    )


def run(args: argparse.Namespace) -> int:
    stage = getattr(args, "stage", "release")
    try:
        record = sign_off_bundle(
            Path(args.bundle),
            Path(args.board),
            args.reviewer,
            readiness=Path(args.readiness) if args.readiness else None,
            stage=stage,
        )
    except WorkflowError as exc:
        if args.format == "json":
            print(json.dumps({"error": str(exc)}, indent=2))
        else:
            print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    signoff = record["fab_review_signoff" if stage == "fab-review" else "human_signoff"]
    if args.format == "json":
        print(
            json.dumps(
                {
                    "stage": stage,
                    "reviewer": signoff["reviewer"],
                    "signed_at": signoff["signed_at"],
                    "board_sha256": record["board_sha256"],
                },
                indent=2,
            )
        )
        return 0
    print(f"Signed off ({stage}) by {signoff['reviewer']} at {signoff['signed_at']}")
    print(f"board sha256: {record['board_sha256']}")
    return 0
