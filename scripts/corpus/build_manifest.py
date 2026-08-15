#!/usr/bin/env python3
"""Build the committed curated-sample manifest (issue #4830, slice 2).

Slice 1 (``probe_open_schematics.py``) answered *"does the parser survive random
real-world KiCad files?"* — 239/239 sampled artifacts parsed. This script turns
that one-off sample into a **standing, reproducible sample**: it scans a seeded
pool of dataset records, stratifies the parseable artifacts by kind, KiCad
file-format version era and payload size, and writes a deterministic manifest
that pins each chosen artifact by ``sha256`` + fetch URL.

The manifest is committed; the payloads are not. ``check_manifest.py`` fetches
them on demand into the gitignored ``scripts/corpus/.cache/`` and scores the
parsers against them, so the corpus stays referenced by URL rather than mirrored
into a repo that has no license to redistribute 87k third-party designs.

This is a **local, opt-in dev tool**: it does network I/O, is never wired into
CI, and is never imported from ``tests/``. Re-running it is only necessary when
you want a bigger / differently-stratified sample — the committed manifest is
the artifact everyone else consumes.

Usage::

    # rebuild the default manifest (scans 90 records, keeps 30 entries)
    uv run python scripts/corpus/build_manifest.py

    # a wider, deeper sample written somewhere else
    uv run python scripts/corpus/build_manifest.py --n 50 --scan 150 \\
        --out scripts/corpus/manifests/open-schematics-wide.json

Exit status is 0 when a manifest was written, 1 otherwise.
"""

from __future__ import annotations

import argparse
import math
import random
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from corpus_manifest import (
    ATTRIBUTION,
    DEFAULT_MANIFEST,
    MANIFEST_SCHEMA_VERSION,
    CorpusManifest,
    DatasetRef,
    ManifestEntry,
    dump_manifest,
    metrics_for,
    sha256_of_text,
)
from hf_hub import (
    DATASET_LICENSE,
    DEFAULT_DATASET,
    HubError,
    dataset_revision,
    dataset_row_count,
    fetch_row,
    row_url,
)
from parse_taxonomy import (
    FORMAT_FOR_KIND,
    OK,
    SUFFIX_FOR_KIND,
    declared_version,
    detect_format,
    parse_payload,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# A sample is "diverse" along three axes we can measure cheaply and that
# actually drive parser behaviour: the artifact kind, the KiCad file-format
# era (the version token's year), and the payload size decade (a 20 kB
# two-footprint board and a 4 MB backplane exercise very different code paths).
SIZE_BUCKET_LABELS = {3: "<10kB", 4: "10-100kB", 5: "100kB-1MB", 6: "1-10MB", 7: ">10MB"}


def size_bucket(size_bytes: int) -> str:
    """Coarse order-of-magnitude bucket for a payload size."""
    decade = max(3, min(7, int(math.log10(max(size_bytes, 1)))))
    return SIZE_BUCKET_LABELS[decade]


def version_era(version: str | None) -> str:
    """Group KiCad file-format versions by year (``20211014`` -> ``2021``)."""
    if not version:
        return "unknown"
    return version[:4] if len(version) >= 8 else f"legacy-{version}"


def stratum_of(kind: str, version: str | None, size_bytes: int) -> str:
    return f"{kind}|{version_era(version)}|{size_bucket(size_bytes)}"


def collect_candidates(
    args: argparse.Namespace,
    offsets: list[int],
    scratch_dir: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch the scan pool and return parse-verified candidate artifacts.

    Every candidate is parsed *before* it can enter the manifest, so each entry
    carries a real observed outcome (``expected_outcome``) rather than an
    assumption. Records the runner could never reproduce (truncated cells,
    oversize payloads, non-KiCad formats) are dropped here, not pinned.
    """
    candidates: list[dict[str, Any]] = []
    notes: list[str] = []
    for position, offset in enumerate(offsets, start=1):
        if not args.quiet:
            print(f"[{position}/{len(offsets)}] scanning record @offset {offset}", file=sys.stderr)
        try:
            fetched = fetch_row(
                args.dataset, args.config, args.split, offset, args.timeout, args.retries
            )
        except HubError as exc:
            notes.append(f"offset {offset}: fetch failed ({exc})")
            continue

        row = fetched["row"]
        truncated = set(fetched["truncated_cells"])
        record_name = str(row.get("name") or f"offset-{offset}")

        payloads: list[tuple[str, str, int, str]] = []
        if args.kinds in ("both", "schematic") and "schematic" not in truncated:
            payloads.append(("schematic", "schematic", 0, str(row.get("schematic") or "")))
        if args.kinds in ("both", "pcb") and "pcb_files" not in truncated:
            for slot, pcb_text in enumerate(row.get("pcb_files") or []):
                payloads.append(("pcb", "pcb_files", slot, str(pcb_text or "")))

        for kind, cell, slot, text in payloads:
            if not text.strip():
                continue
            fmt = detect_format(text)
            if fmt != FORMAT_FOR_KIND[kind]:
                continue  # legacy Eeschema / Altium / other: no parser, not a sample
            size = len(text.encode("utf-8"))
            if size < args.min_bytes or size > args.max_bytes:
                continue
            stem = f"cand{offset:07d}-{kind}{slot}"
            outcome, parse_exc, _elapsed = parse_payload(
                text, kind, scratch_dir, stem, args.max_bytes
            )
            if outcome != OK and not args.include_failures:
                notes.append(
                    f"offset {offset} {kind}{slot}: dropped, outcome={outcome} ({parse_exc})"
                )
                continue
            path = scratch_dir / f"{stem}{SUFFIX_FOR_KIND[kind]}"
            path.write_text(text, encoding="utf-8")
            metrics = metrics_for(path, kind) if outcome == OK else {}
            path.unlink(missing_ok=True)
            candidates.append(
                {
                    "id": f"os-{offset:07d}-{kind}{slot}",
                    "kind": kind,
                    "row_offset": offset,
                    "cell": cell,
                    "slot": slot,
                    "record_name": record_name[:120],
                    "source_url": row_url(args.dataset, args.config, args.split, offset),
                    "sha256": sha256_of_text(text),
                    "size_bytes": size,
                    "detected_format": fmt,
                    "declared_version": declared_version(text),
                    "expected_outcome": outcome,
                    "metrics": metrics,
                    "stratum": stratum_of(kind, declared_version(text), size),
                }
            )
    return candidates, notes


def select_diverse(
    candidates: list[dict[str, Any]],
    n: int,
    seed: int,
    max_per_record: int,
) -> list[dict[str, Any]]:
    """Pick ``n`` candidates spread across strata, deterministically.

    Round-robin over the strata (sorted by name, so the order does not depend on
    fetch timing) taking one candidate at a time: a corpus where 80% of the
    entries are 2021-era 100 kB boards would tell us nothing new about the
    parser that one board does not.
    """
    rng = random.Random(seed)
    buckets: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        buckets.setdefault(candidate["stratum"], []).append(candidate)
    for stratum in buckets:
        buckets[stratum].sort(key=lambda c: c["id"])
        rng.shuffle(buckets[stratum])

    chosen: list[dict[str, Any]] = []
    per_record: dict[int, int] = {}
    order = sorted(buckets)
    while len(chosen) < n and any(buckets[s] for s in order):
        for stratum in order:
            if len(chosen) >= n:
                break
            while buckets[stratum]:
                candidate = buckets[stratum].pop(0)
                offset = candidate["row_offset"]
                if per_record.get(offset, 0) >= max_per_record:
                    continue
                per_record[offset] = per_record.get(offset, 0) + 1
                chosen.append(candidate)
                break
    chosen.sort(key=lambda c: c["id"])
    return chosen


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_manifest.py",
        description=(
            "Scan a seeded pool of bshada/open-schematics records and write a "
            "deterministic, committed manifest pinning a diverse subset by "
            "sha256 + URL. Local/opt-in: does network I/O, never run in CI."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "The manifest is committed; the payloads never are. Run\n"
            "check_manifest.py to fetch and score against it."
        ),
    )
    parser.add_argument("--n", type=int, default=30, help="entries to keep (default: 30)")
    parser.add_argument(
        "--scan", type=int, default=90, help="records to fetch and evaluate (default: 90)"
    )
    parser.add_argument(
        "--seed", type=int, default=4830, help="sampling/selection seed (default: 4830)"
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"manifest path (default: {DEFAULT_MANIFEST.relative_to(REPO_ROOT)})",
    )
    parser.add_argument(
        "--manifest-id",
        default="open-schematics-sample",
        help="identifier recorded in the manifest (default: open-schematics-sample)",
    )
    parser.add_argument("--dataset", default=DEFAULT_DATASET, help="Hugging Face dataset id")
    parser.add_argument("--config", default="default", help="dataset config (default: default)")
    parser.add_argument("--split", default="train", help="dataset split (default: train)")
    parser.add_argument(
        "--kinds",
        choices=("both", "schematic", "pcb"),
        default="both",
        help="artifact kinds eligible for the manifest (default: both)",
    )
    parser.add_argument(
        "--min-bytes",
        type=int,
        default=2_000,
        help="skip payloads smaller than this (default: 2000)",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=4_000_000,
        help="skip payloads larger than this (default: 4000000 -- entries must stay "
        "cheap enough that a full re-fetch is a coffee break, not an afternoon)",
    )
    parser.add_argument(
        "--max-per-record",
        type=int,
        default=1,
        help="cap artifacts pinned from any single design (default: 1)",
    )
    parser.add_argument(
        "--include-failures",
        action="store_true",
        help="also pin artifacts that fail to parse today, with their observed "
        "failure class as expected_outcome (turns them into regression pins)",
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="per-request timeout seconds")
    parser.add_argument("--retries", type=int, default=3, help="retries per HTTP request")
    parser.add_argument("--quiet", action="store_true", help="suppress per-record progress")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.n <= 0 or args.scan <= 0:
        print("error: --n and --scan must be positive", file=sys.stderr)
        return 2

    rng = random.Random(args.seed)
    scratch_dir = Path(tempfile.mkdtemp(prefix="kct-corpus-manifest-"))
    try:
        revision = dataset_revision(args.dataset, args.timeout, args.retries)
        row_count = dataset_row_count(
            args.dataset, args.config, args.split, args.timeout, args.retries
        )
        offsets = sorted(rng.sample(range(row_count), min(args.scan, row_count)))
        candidates, notes = collect_candidates(args, offsets, scratch_dir)
    except HubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)

    if not candidates:
        print("error: no usable candidates in the scanned pool", file=sys.stderr)
        return 1

    chosen = select_diverse(candidates, args.n, args.seed, args.max_per_record)
    entries = [
        ManifestEntry(**{k: v for k, v in candidate.items() if k != "stratum"})
        for candidate in chosen
    ]
    strata = sorted({c["stratum"] for c in chosen})
    manifest = CorpusManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        manifest_id=args.manifest_id,
        generated_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        generator="scripts/corpus/build_manifest.py",
        dataset=DatasetRef(
            id=args.dataset,
            url=f"https://huggingface.co/datasets/{args.dataset}",
            license=DATASET_LICENSE,
            attribution=ATTRIBUTION,
            config=args.config,
            split=args.split,
            revision=revision,
            row_count=row_count,
        ),
        selection={
            "seed": args.seed,
            "scanned_offsets": offsets,
            "records_scanned": len(offsets),
            "candidates_found": len(candidates),
            "entries_kept": len(entries),
            "kinds": args.kinds,
            "min_bytes": args.min_bytes,
            "max_bytes": args.max_bytes,
            "max_per_record": args.max_per_record,
            "include_failures": bool(args.include_failures),
            "strategy": (
                "round-robin over (kind, KiCad version era, payload size decade) strata, "
                "seeded shuffle within each stratum"
            ),
            "strata": strata,
            "scan_notes": notes[:50],
        },
        entries=entries,
    )
    dump_manifest(manifest, args.out)

    print(f"manifest    : {args.out}")
    print(f"dataset     : {args.dataset} @ {revision or 'unknown revision'} ({row_count} rows)")
    print(f"scanned     : {len(offsets)} records -> {len(candidates)} candidates")
    print(f"kept        : {len(entries)} entries across {len(strata)} strata")
    for stratum in strata:
        count = sum(1 for c in chosen if c["stratum"] == stratum)
        print(f"  {stratum:<34} {count}")
    total_mb = sum(e.size_bytes for e in entries) / 1e6
    print(f"payload size: {total_mb:.1f} MB total (fetched on demand, never committed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
