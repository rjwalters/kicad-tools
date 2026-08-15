#!/usr/bin/env python3
"""Score this repo's KiCad parsers against a committed corpus manifest (#4830).

The repeatable half of slice 2. ``build_manifest.py`` pins a diverse sample of
real-world designs by ``sha256`` + URL; this runner turns that pointer file back
into an actual measurement:

1. For each entry, use the locally cached payload if it is present **and** its
   hash matches the pin; otherwise fetch the record from the datasets-server
   (unless ``--offline``) and cache it under the gitignored
   ``scripts/corpus/.cache/payloads/``.
2. Parse it with ``kicad_tools.schema.Schematic.load`` / ``PCB.load`` and bucket
   the outcome with the shared taxonomy.
3. Compare each outcome against the ``expected_outcome`` the manifest pinned and
   report **regressions** (pinned parseable, now failing) and **unexpected
   passes** (pinned failing, now parses) separately from the raw rate.

Integrity failures (missing payload, hash mismatch) are reported as their own
classes and excluded from ``parse_failure_rate``: an upstream row change or a
flaky network is not a parser regression, and conflating them would make the
number untrustworthy exactly when it matters.

Local/opt-in, like everything else under ``scripts/corpus/``: it does network
I/O on a cold cache, is never wired into CI, and is never imported by ``tests/``
(the pure ``corpus_manifest`` / ``parse_taxonomy`` modules are the tested part).

Usage::

    # first run downloads the pinned payloads (~ tens of MB), then scores
    uv run python scripts/corpus/check_manifest.py

    # subsequent runs are offline and fast (cache hit + hash verify)
    uv run python scripts/corpus/check_manifest.py --offline

    # treat unavailable entries as failures too (nightly-style strictness)
    uv run python scripts/corpus/check_manifest.py --strict

Exit status: 0 clean, 1 regressions (or, with ``--strict``, unavailable
entries), 2 usage/manifest error.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from corpus_manifest import (
    DEFAULT_CACHE_DIR,
    DEFAULT_MANIFEST,
    CorpusManifest,
    EntryResult,
    ManifestEntry,
    ManifestError,
    cache_path_for,
    compare_metrics,
    load_manifest,
    metrics_for,
    render_scorecard,
    score_entries,
    sha256_of_text,
    verify_cached,
)
from hf_hub import HubError, fetch_row
from parse_taxonomy import (
    HASH_MISMATCH,
    MISSING_PAYLOAD,
    NET_ERROR,
    parse_file,
    signature_of,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT_DIR = REPO_ROOT / "scripts" / "corpus" / ".cache"


def payload_from_row(row: dict[str, object], entry: ManifestEntry) -> str:
    """Pull the exact cell/slot this entry pins out of a fetched dataset row."""
    if entry.cell == "schematic":
        return str(row.get("schematic") or "")
    values = row.get("pcb_files") or []
    if not isinstance(values, list) or entry.slot >= len(values):
        return ""
    return str(values[entry.slot] or "")


def ensure_payload(
    entry: ManifestEntry,
    manifest: CorpusManifest,
    cache_dir: Path,
    args: argparse.Namespace,
) -> tuple[Path | None, bool, str, str]:
    """Return ``(path, fetched, outcome_or_empty, detail)`` for one entry.

    ``outcome_or_empty`` is empty when the payload is available and verified;
    otherwise it is the integrity/network class that should be recorded instead
    of a parse outcome.
    """
    path = cache_path_for(entry, cache_dir)
    if not args.refetch:
        ok, detail = verify_cached(entry, cache_dir)
        if ok:
            return path, False, "", "cache hit"
        if path.is_file() and "sha256" in detail and args.offline:
            return None, False, HASH_MISMATCH, detail
    if args.offline:
        return None, False, MISSING_PAYLOAD, "not cached and --offline"

    try:
        fetched = fetch_row(
            manifest.dataset.id,
            manifest.dataset.config,
            manifest.dataset.split,
            entry.row_offset,
            args.timeout,
            args.retries,
        )
    except HubError as exc:
        return None, False, NET_ERROR, str(exc)

    if entry.cell in set(fetched["truncated_cells"]):
        return None, False, HASH_MISMATCH, f"datasets-server truncated cell '{entry.cell}'"

    text = payload_from_row(fetched["row"], entry)
    actual = sha256_of_text(text)
    if actual != entry.sha256:
        # Upstream changed the row (or the dataset was re-published). Pinning by
        # hash is what lets us say that out loud instead of silently scoring a
        # different file than the one the manifest describes.
        return (
            None,
            True,
            HASH_MISMATCH,
            f"fetched sha256 {actual[:12]}... != pinned {entry.sha256[:12]}...",
        )

    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path, True, "", "fetched"


def evaluate_entry(
    entry: ManifestEntry,
    manifest: CorpusManifest,
    cache_dir: Path,
    args: argparse.Namespace,
) -> EntryResult:
    result = EntryResult(
        id=entry.id,
        kind=entry.kind,
        outcome=MISSING_PAYLOAD,
        expected_outcome=entry.expected_outcome,
        record_name=entry.record_name,
        declared_version=entry.declared_version,
        size_bytes=entry.size_bytes,
    )
    try:
        path, fetched, blocked, detail = ensure_payload(entry, manifest, cache_dir, args)
    except OSError as exc:
        result.outcome = MISSING_PAYLOAD
        result.detail = f"{type(exc).__name__}: {exc}"
        return result

    result.fetched = fetched
    if blocked or path is None:
        result.outcome = blocked or MISSING_PAYLOAD
        result.detail = detail
        return result

    outcome, parse_exc, elapsed = parse_file(path, entry.kind)
    result.outcome = outcome
    result.elapsed_ms = round(elapsed, 2)
    result.detail = detail
    if parse_exc is not None:
        result.exception_type = type(parse_exc).__name__
        message = str(parse_exc).strip()
        result.message = message.splitlines()[0][:400] if message else None
        result.signature = signature_of(parse_exc)
    elif not args.no_metrics and entry.metrics:
        # A clean load is a weak assertion; compare the object graph too.
        result.metrics_drift = compare_metrics(entry.metrics, metrics_for(path, entry.kind))
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_manifest.py",
        description=(
            "Fetch (or reuse) the payloads pinned by a corpus manifest, verify "
            "them against their sha256, parse them with this repo's parsers, and "
            "report a parser-health scorecard including regressions vs the "
            "pinned outcomes."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"manifest to score (default: {DEFAULT_MANIFEST.relative_to(REPO_ROOT)})",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help=f"payload cache (default: {DEFAULT_CACHE_DIR.relative_to(REPO_ROOT)}, gitignored)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_REPORT_DIR,
        help=f"report directory (default: {DEFAULT_REPORT_DIR.relative_to(REPO_ROOT)})",
    )
    parser.add_argument(
        "--kinds",
        choices=("both", "schematic", "pcb"),
        default="both",
        help="restrict scoring to one artifact kind (default: both)",
    )
    parser.add_argument("--limit", type=int, default=0, help="score only the first N entries")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="never fetch: score only entries already cached and hash-verified",
    )
    parser.add_argument(
        "--refetch",
        action="store_true",
        help="ignore the cache and re-download every entry (re-validates the pins)",
    )
    parser.add_argument(
        "--no-metrics",
        action="store_true",
        help="skip the pinned-vs-re-derived shape-metric comparison (parse only)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero on metrics drift or unavailable entries too, "
        "not just on parse regressions",
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="per-request timeout seconds")
    parser.add_argument("--retries", type=int, default=3, help="retries per HTTP request")
    parser.add_argument("--quiet", action="store_true", help="suppress per-entry progress")
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="print the scorecard but do not write JSON/text report files",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    entries = [e for e in manifest.entries if args.kinds in ("both", e.kind)]
    if args.limit > 0:
        entries = entries[: args.limit]
    if not entries:
        print("error: manifest has no entries matching the filters", file=sys.stderr)
        return 2

    cache_dir: Path = args.cache_dir.expanduser().resolve()
    results: list[EntryResult] = []
    try:
        for position, entry in enumerate(entries, start=1):
            if not args.quiet:
                print(f"[{position}/{len(entries)}] {entry.id}", file=sys.stderr, flush=True)
            results.append(evaluate_entry(entry, manifest, cache_dir, args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    mode = "offline" if args.offline else ("refetch" if args.refetch else "cache-or-fetch")
    report = score_entries(manifest, results, mode=mode)
    report["generated_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report["manifest_path"] = str(args.manifest)
    summary = render_scorecard(report)
    print(summary)

    if not args.no_report:
        out_dir: Path = args.out.expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        json_path = out_dir / f"manifest-score-{stamp}.json"
        text_path = out_dir / f"manifest-score-{stamp}.txt"
        json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        text_path.write_text(summary + "\n", encoding="utf-8")
        print(f"JSON report : {json_path}")
        print(f"text report : {text_path}")

    totals = report["totals"]
    if totals["regressions"]:
        print(f"FAIL: {totals['regressions']} parse regression(s) vs the manifest", file=sys.stderr)
        return 1
    if args.strict and totals["metrics_drift"]:
        print(
            f"FAIL: {totals['metrics_drift']} entry/entries drifted vs pinned metrics (--strict)",
            file=sys.stderr,
        )
        return 1
    if args.strict and totals["unavailable"]:
        print(
            f"FAIL: {totals['unavailable']} entry/entries unavailable (--strict)", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
