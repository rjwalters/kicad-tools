"""``kct tapeout-sign-off``: record the explicit human sign-offs ``submit-jlcpcb`` requires (#8537, #8818).

Two attestations, in the order the work happens:

* ``--stage release`` (default): "the gates pass; these bytes may be sent to the
  fab for preview". Enough to upload and preview; never enough to save a cart.
* ``--stage fab-review``: "I looked at the fab's rendering of the uploaded
  bytes (gerber viewer / DFM result)". Required before the cart is saved.
* ``--stage fab-dfm`` (#8841, advisory): record the JLCDFM per-category counts you
  read off the fab's DFM tool, next to kct's per-rule counts. Lists every category
  where the fab reports danger and kct found nothing. Records only; never gates.

Order: release -> upload/preview at fab -> run JLCDFM, ``--stage fab-dfm`` ->
``--stage fab-review`` -> cart. fab-dfm is recommended before fab-review, not required.
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
        choices=["release", "fab-review", "fab-dfm"],
        default="release",
        help=(
            "release (default): gates pass, these bytes may be sent to the fab for preview "
            "(does not allow saving a cart). fab-review: you reviewed the fab's gerber "
            "viewer / DFM rendering of the uploaded bytes; required before the cart is saved. fab-dfm: record attested JLCDFM counts "
            "(--dfm-counts, --kct-report); advisory, recommended before fab-review"
        ),
    )
    parser.add_argument(
        "--dfm-counts",
        help='--stage fab-dfm: JSON file {"<category>": {"danger": n, "warning": n, "good": n}}',
    )
    parser.add_argument(
        "--kct-report",
        help="--stage fab-dfm: the `kct check --output` JSON report for this board "
        "(e.g. <board>/output/readiness-verification/kct-check.json)",
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


def _load_json(path: str | None, flag: str) -> object:
    if not path:
        raise WorkflowError(f"--stage fab-dfm needs {flag} <file.json>")
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkflowError(f"Cannot read {flag} {path}: {exc.__class__.__name__}") from exc


def run(args: argparse.Namespace) -> int:
    stage = getattr(args, "stage", "release")
    try:
        extra: dict = {}
        if stage == "fab-dfm":
            extra = {
                "dfm_counts": _load_json(getattr(args, "dfm_counts", None), "--dfm-counts"),
                "kct_report": _load_json(getattr(args, "kct_report", None), "--kct-report"),
            }
        record = sign_off_bundle(
            Path(args.bundle),
            Path(args.board),
            args.reviewer,
            readiness=Path(args.readiness) if args.readiness else None,
            stage=stage,
            **extra,
        )
    except WorkflowError as exc:
        if args.format == "json":
            print(json.dumps({"error": str(exc)}, indent=2))
        else:
            print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    if stage == "fab-dfm":
        dfm = record["fab_dfm"]
        if args.format == "json":
            print(json.dumps({"stage": stage, **dfm}, indent=2))
            return 0
        print(f"Recorded fab DFM attestation by {dfm['reviewer']} at {dfm['attested_at']}")
        if dfm["mismatches"]:
            print("MISMATCH: fab reports danger where kct reported nothing:")
            for m in dfm["mismatches"]:
                rules = ", ".join(m["kct_rules"]) or "no mapped kct rule"
                print(f"  - {m['category']}: fab danger={m['fab_danger']}, kct=0 ({rules})")
        else:
            print("No category where the fab reports danger and kct reported nothing.")
        return 0
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
