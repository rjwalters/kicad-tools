#!/usr/bin/env python3
"""Curated-sample manifest for external KiCad corpora (issue #4830, slice 2).

The manifest is a **committed, deterministic pointer file**: a list of records
sampled from the Hugging Face ``bshada/open-schematics`` dataset, each pinned by
``sha256`` + fetch URL, together with the shape metrics that made it worth
keeping (pad count, net count, copper layers, declared KiCad file version).

The payloads themselves are **never committed**. The corpus stays referenced by
URL: the runner (``check_manifest.py``) downloads what it needs into the
gitignored ``scripts/corpus/.cache/``, verifies each payload against the pinned
hash, and scores this repo's parsers against it. That keeps a real-world
regression set reproducible without mirroring 317 GB of third-party,
CC-BY-4.0-licensed design files into the tree.

This module is **pure and offline** -- schema, validation, hashing, scoring and
rendering only. No network, no CLI, no writes outside a caller-supplied path.
That is what makes it importable from ``tests/`` (the network scripts are not).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from parse_taxonomy import (
    FAILURE_CLASSES,
    OK,
    SKIP_CLASSES,
    SUFFIX_FOR_KIND,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_DIR = REPO_ROOT / "scripts" / "corpus" / "manifests"
DEFAULT_MANIFEST = MANIFEST_DIR / "open-schematics-sample.json"
DEFAULT_CACHE_DIR = REPO_ROOT / "scripts" / "corpus" / ".cache" / "payloads"

MANIFEST_SCHEMA_VERSION = 1
VALID_KINDS = ("schematic", "pcb")
ATTRIBUTION = (
    "Contains data from the Open Schematics dataset by bshada "
    "(https://huggingface.co/datasets/bshada/open-schematics), "
    "used under CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/)."
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Keys that must never appear in a manifest entry: they would mean the payload
# itself (or a chunk of it) got baked into the committed file, which is exactly
# what "reference by URL, do not vendor" forbids. Enforced by validation *and*
# by a test, because this is the constraint most likely to be broken by a
# well-meaning future edit.
FORBIDDEN_ENTRY_KEYS = frozenset({"text", "payload", "content", "source_text", "data", "body"})


class ManifestError(ValueError):
    """The manifest file is missing, unreadable, or fails schema validation."""


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
@dataclass
class ManifestEntry:
    """One pinned artifact: a single ``.kicad_sch`` or ``.kicad_pcb`` payload."""

    id: str  # stable, human-readable: "os-0041187-pcb0"
    kind: str  # "schematic" | "pcb"
    row_offset: int  # dataset row this artifact came from
    cell: str  # dataset column: "schematic" | "pcb_files"
    slot: int  # index within the column (schematics are always 0)
    record_name: str  # upstream project/design name, for attribution
    source_url: str  # exact datasets-server URL serving the record
    sha256: str  # hash of the payload text encoded as UTF-8
    size_bytes: int
    detected_format: str  # kicad_sch | kicad_pcb
    declared_version: str | None  # KiCad file-format version token
    expected_outcome: str = OK  # taxonomy class observed when the entry was pinned
    metrics: dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    @property
    def filename(self) -> str:
        """Stable local filename (hash-prefixed, so a re-pin never collides)."""
        return f"{self.sha256[:16]}-{self.id}{SUFFIX_FOR_KIND[self.kind]}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, obj: dict[str, Any]) -> ManifestEntry:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in obj.items() if k in known})


@dataclass
class DatasetRef:
    """Where the entries came from, pinned tightly enough to re-derive them."""

    id: str
    url: str
    license: str
    attribution: str
    config: str = "default"
    split: str = "train"
    revision: str | None = None  # dataset commit sha at build time
    row_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, obj: dict[str, Any]) -> DatasetRef:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in obj.items() if k in known})


@dataclass
class CorpusManifest:
    """A committed, deterministic sample of an external corpus."""

    manifest_id: str
    generated_utc: str
    generator: str
    dataset: DatasetRef
    selection: dict[str, Any]
    entries: list[ManifestEntry]
    schema_version: int = MANIFEST_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "manifest_id": self.manifest_id,
            "generated_utc": self.generated_utc,
            "generator": self.generator,
            "dataset": self.dataset.to_dict(),
            "selection": self.selection,
            "entries": [e.to_dict() for e in self.entries],
        }

    @classmethod
    def from_dict(cls, obj: dict[str, Any]) -> CorpusManifest:
        errors = validate_manifest_dict(obj)
        if errors:
            raise ManifestError("invalid manifest: " + "; ".join(errors[:10]))
        return cls(
            schema_version=int(obj["schema_version"]),
            manifest_id=str(obj["manifest_id"]),
            generated_utc=str(obj["generated_utc"]),
            generator=str(obj["generator"]),
            dataset=DatasetRef.from_dict(obj["dataset"]),
            selection=dict(obj.get("selection") or {}),
            entries=[ManifestEntry.from_dict(e) for e in obj["entries"]],
        )

    def by_kind(self, kind: str) -> list[ManifestEntry]:
        return [e for e in self.entries if e.kind == kind]


def validate_manifest_dict(obj: Any) -> list[str]:
    """Return a list of human-readable schema violations (empty == valid).

    Validation is deliberately explicit rather than schema-library driven: the
    manifest is a committed artifact, and the failure message a contributor sees
    when they hand-edit it should name the field and the entry.
    """
    errors: list[str] = []
    if not isinstance(obj, dict):
        return [f"top level must be an object, got {type(obj).__name__}"]

    for key in (
        "schema_version",
        "manifest_id",
        "generated_utc",
        "generator",
        "dataset",
        "entries",
    ):
        if key not in obj:
            errors.append(f"missing top-level key '{key}'")
    if errors:
        return errors

    if obj["schema_version"] != MANIFEST_SCHEMA_VERSION:
        errors.append(
            f"schema_version {obj['schema_version']!r} != supported {MANIFEST_SCHEMA_VERSION}"
        )

    dataset = obj["dataset"]
    if not isinstance(dataset, dict):
        errors.append("'dataset' must be an object")
    else:
        for key in ("id", "url", "license", "attribution"):
            if not dataset.get(key):
                errors.append(f"dataset.{key} is required and must be non-empty")

    entries = obj["entries"]
    if not isinstance(entries, list) or not entries:
        return errors + ["'entries' must be a non-empty list"]

    seen_ids: set[str] = set()
    seen_hashes: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"entries[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{where}: must be an object")
            continue
        leaked = sorted(FORBIDDEN_ENTRY_KEYS & set(entry))
        if leaked:
            errors.append(
                f"{where}: payload content must not be committed (forbidden keys: {leaked})"
            )
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not _ID_RE.match(entry_id):
            errors.append(f"{where}: 'id' must match {_ID_RE.pattern}")
        elif entry_id in seen_ids:
            errors.append(f"{where}: duplicate id {entry_id!r}")
        else:
            seen_ids.add(entry_id)

        if entry.get("kind") not in VALID_KINDS:
            errors.append(f"{where}: 'kind' must be one of {VALID_KINDS}")

        expected = entry.get("expected_outcome", OK)
        if expected != OK and expected not in FAILURE_CLASSES:
            errors.append(
                f"{where}: 'expected_outcome' must be '{OK}' or a parser-failure class, "
                f"got {expected!r} (skip/integrity classes are run-time states, not pins)"
            )

        sha = entry.get("sha256")
        if not isinstance(sha, str) or not _SHA256_RE.match(sha):
            errors.append(f"{where}: 'sha256' must be 64 lowercase hex chars")
        elif sha in seen_hashes:
            errors.append(f"{where}: duplicate sha256 {sha[:12]}... (same payload pinned twice)")
        else:
            seen_hashes.add(sha)

        url = entry.get("source_url")
        if not isinstance(url, str) or not url.startswith("https://"):
            errors.append(f"{where}: 'source_url' must be an https URL")

        size = entry.get("size_bytes")
        if not isinstance(size, int) or size <= 0:
            errors.append(f"{where}: 'size_bytes' must be a positive int")

        for key in ("row_offset", "slot"):
            value = entry.get(key)
            if not isinstance(value, int) or value < 0:
                errors.append(f"{where}: '{key}' must be a non-negative int")

    return errors


def load_manifest(path: Path = DEFAULT_MANIFEST) -> CorpusManifest:
    """Load and validate a manifest file."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{path}: not valid JSON: {exc}") from exc
    return CorpusManifest.from_dict(raw)


def dump_manifest(manifest: CorpusManifest, path: Path) -> None:
    """Write a manifest deterministically (stable key order, trailing newline)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest.to_dict(), indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Payload cache + integrity
# ---------------------------------------------------------------------------
def sha256_of_text(text: str) -> str:
    """Hash a payload the way the manifest pins it (UTF-8 bytes of the text)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cache_path_for(entry: ManifestEntry, cache_dir: Path = DEFAULT_CACHE_DIR) -> Path:
    """Where a fetched payload lives locally (always inside a gitignored dir)."""
    return cache_dir / entry.filename


def verify_cached(entry: ManifestEntry, cache_dir: Path = DEFAULT_CACHE_DIR) -> tuple[bool, str]:
    """Check a cached payload against its pinned hash.

    Returns ``(ok, detail)``. A mismatch is an *integrity* signal, not a parser
    signal: it means the upstream row changed (or the cache is corrupt), and
    scoring the parser on those bytes would be measuring the wrong thing.
    """
    path = cache_path_for(entry, cache_dir)
    if not path.is_file():
        return False, "not cached"
    actual = sha256_of_file(path)
    if actual != entry.sha256:
        return False, f"sha256 {actual[:12]}... != pinned {entry.sha256[:12]}..."
    return True, "ok"


def compare_metrics(pinned: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Diff re-derived shape metrics against the ones pinned in the manifest.

    "It parsed" is a weak assertion: a file can load cleanly and yield an empty
    object graph (the corpus contains KiCad-4-era ``(version 3)`` boards that do
    exactly that -- accepted by the s-expr reader, zero footprints extracted).
    Pinning ``footprints``/``pads``/``nets`` counts turns the manifest into a
    *semantic* regression check, so a refactor that quietly stops reading a node
    shows up as drift instead of a green run.
    """
    drift: list[str] = []
    for key, pinned_value in sorted(pinned.items()):
        if key not in actual:
            drift.append(f"{key}: pinned {pinned_value!r}, not re-derived")
            continue
        if actual[key] != pinned_value:
            drift.append(f"{key}: pinned {pinned_value!r} -> now {actual[key]!r}")
    return drift


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
@dataclass
class EntryResult:
    """Outcome of running one manifest entry through this repo's parser."""

    id: str
    kind: str
    outcome: str  # OK / a FAIL_* / a SKIP_* constant
    expected_outcome: str = OK  # what the manifest pinned
    record_name: str = ""
    declared_version: str | None = None
    size_bytes: int = 0
    elapsed_ms: float = 0.0
    fetched: bool = False  # payload was downloaded during this run
    exception_type: str | None = None
    message: str | None = None
    signature: str | None = None
    detail: str | None = None  # integrity/fetch detail, when not a parse outcome
    metrics_drift: list[str] = field(default_factory=list)  # pinned vs re-derived counts

    @property
    def failed(self) -> bool:
        return self.outcome in FAILURE_CLASSES

    @property
    def skipped(self) -> bool:
        return self.outcome in SKIP_CLASSES

    @property
    def regressed(self) -> bool:
        """Pinned as parseable, no longer parses -- the signal worth alerting on."""
        return self.expected_outcome == OK and self.failed

    @property
    def unexpected_pass(self) -> bool:
        """Pinned as a known parser failure, but it parses now -- re-pin it."""
        return self.expected_outcome != OK and self.outcome == OK


def score_entries(
    manifest: CorpusManifest,
    results: list[EntryResult],
    *,
    mode: str = "offline",
) -> dict[str, Any]:
    """Aggregate per-entry outcomes into a machine-readable scorecard.

    ``parse_failure_rate`` counts only entries that were actually parsed --
    entries that could not be fetched, or whose bytes failed the hash check, are
    reported separately so an outage never masquerades as a parser regression.
    """
    by_class = Counter(r.outcome for r in results)
    parsed = [r for r in results if r.outcome == OK or r.failed]
    failures = [r for r in results if r.failed]
    integrity = [r for r in results if r.skipped]
    regressions = [r for r in results if r.regressed]
    unexpected_passes = [r for r in results if r.unexpected_pass]
    drifted = [r for r in results if r.metrics_drift]
    signatures = Counter(r.signature for r in failures if r.signature)

    by_kind: dict[str, dict[str, int]] = {}
    for result in results:
        bucket = by_kind.setdefault(result.kind, {})
        bucket[result.outcome] = bucket.get(result.outcome, 0) + 1

    by_version: dict[str, dict[str, int]] = {}
    for result in parsed:
        bucket_v = by_version.setdefault(
            result.declared_version or "unknown", {"ok": 0, "failed": 0}
        )
        bucket_v["failed" if result.failed else "ok"] += 1

    return {
        "schema_version": 1,
        "manifest_id": manifest.manifest_id,
        "manifest_schema_version": manifest.schema_version,
        "dataset": manifest.dataset.to_dict(),
        "mode": mode,
        "totals": {
            "entries": len(manifest.entries),
            "evaluated": len(results),
            "parsed": len(parsed),
            "ok": len(parsed) - len(failures),
            "failed": len(failures),
            "regressions": len(regressions),
            "unexpected_passes": len(unexpected_passes),
            "metrics_drift": len(drifted),
            "unavailable": len(integrity),
            "fetched_this_run": sum(1 for r in results if r.fetched),
            "parse_failure_rate": (round(len(failures) / len(parsed), 4) if parsed else None),
            "coverage": (
                round(len(parsed) / len(manifest.entries), 4) if manifest.entries else 0.0
            ),
        },
        "by_class": dict(sorted(by_class.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_kind": by_kind,
        "by_declared_version": dict(sorted(by_version.items())),
        "top_signatures": [
            {
                "signature": sig,
                "count": count,
                "class": next(r.outcome for r in failures if r.signature == sig),
                "entries": [r.id for r in failures if r.signature == sig][:5],
            }
            for sig, count in signatures.most_common(20)
        ],
        "regressions": [
            {"id": r.id, "kind": r.kind, "outcome": r.outcome, "signature": r.signature}
            for r in regressions
        ],
        "unexpected_passes": [
            {"id": r.id, "kind": r.kind, "expected_outcome": r.expected_outcome}
            for r in unexpected_passes
        ],
        "metrics_drift": [{"id": r.id, "kind": r.kind, "drift": r.metrics_drift} for r in drifted],
        "unavailable_entries": [
            {"id": r.id, "outcome": r.outcome, "detail": r.detail} for r in integrity
        ],
        "entries": [asdict(r) for r in results],
    }


def render_scorecard(report: dict[str, Any]) -> str:
    """Human-readable rendering of :func:`score_entries` output."""
    totals = report["totals"]
    dataset = report["dataset"]
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append(f"corpus manifest scorecard -- {report['manifest_id']}")
    lines.append("=" * 78)
    lines.append(f"dataset      : {dataset['id']} ({dataset['license']})")
    lines.append(f"revision     : {dataset.get('revision') or 'unknown'}")
    lines.append(f"mode         : {report['mode']}")
    lines.append("")
    lines.append("-- totals " + "-" * 68)
    lines.append(f"manifest entries    : {totals['entries']}")
    lines.append(
        f"evaluated           : {totals['evaluated']} ({totals['fetched_this_run']} fetched)"
    )
    lines.append(f"parsed              : {totals['parsed']} (coverage {totals['coverage']:.1%})")
    lines.append(f"  parsed OK         : {totals['ok']}")
    rate = totals["parse_failure_rate"]
    lines.append(
        f"  parse failures    : {totals['failed']}"
        + (f" ({rate:.1%} of parsed)" if rate is not None else "")
    )
    lines.append(f"unavailable         : {totals['unavailable']} (not fetched / hash mismatch)")
    lines.append(
        f"vs pinned outcomes  : {totals['regressions']} regressions, "
        f"{totals['unexpected_passes']} unexpected passes, "
        f"{totals['metrics_drift']} metrics drift"
    )
    lines.append("")
    lines.append("-- outcomes by class " + "-" * 57)
    for name, count in report["by_class"].items():
        marker = "FAIL" if name in FAILURE_CLASSES else ("ok  " if name == OK else "skip")
        lines.append(f"  [{marker}] {name:<34} {count:>6}")
    lines.append("")
    lines.append("-- outcomes by kind " + "-" * 58)
    for kind, classes in sorted(report["by_kind"].items()):
        rendered = ", ".join(f"{k}={v}" for k, v in sorted(classes.items()))
        lines.append(f"  {kind:<12} {rendered}")
    lines.append("")
    lines.append("-- parsed entries by declared KiCad file version " + "-" * 29)
    for version, counts in report["by_declared_version"].items():
        lines.append(f"  version {version:<12} ok={counts['ok']:<6} failed={counts['failed']}")
    lines.append("")
    lines.append("-- failure signatures " + "-" * 56)
    if not report["top_signatures"]:
        lines.append("  (none -- every parsed entry loaded cleanly)")
    for sig in report["top_signatures"]:
        lines.append(f"  {sig['count']:>4}x [{sig['class']}] {sig['signature']}")
        lines.append(f"        e.g. {', '.join(sig['entries'])}")
    if report["regressions"]:
        lines.append("")
        lines.append("-- REGRESSIONS (pinned as parseable, now failing) " + "-" * 28)
        for item in report["regressions"]:
            lines.append(f"  {item['id']:<28} {item['outcome']:<26} {item['signature'] or ''}")
    if report["unexpected_passes"]:
        lines.append("")
        lines.append("-- unexpected passes (re-pin these entries) " + "-" * 34)
        for item in report["unexpected_passes"]:
            lines.append(f"  {item['id']:<28} pinned as {item['expected_outcome']}")
    if report["metrics_drift"]:
        lines.append("")
        lines.append("-- metrics drift (parsed, but the object graph changed) " + "-" * 22)
        for item in report["metrics_drift"]:
            lines.append(f"  {item['id']:<28} {'; '.join(item['drift'])[:120]}")
    if report["unavailable_entries"]:
        lines.append("")
        lines.append("-- unavailable entries " + "-" * 55)
        for item in report["unavailable_entries"][:20]:
            lines.append(f"  {item['id']:<28} {item['outcome']:<26} {item['detail'] or ''}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Shape metrics (what makes a sample "diverse")
# ---------------------------------------------------------------------------
def pcb_metrics(path: Path) -> dict[str, Any]:
    """Coarse shape metrics for a ``.kicad_pcb`` (best effort, never raises)."""
    from kicad_tools.schema import PCB

    board = PCB.load(path)
    metrics: dict[str, Any] = {
        "footprints": len(board.footprints),
        "pads": sum(len(fp.pads) for fp in board.footprints),
        "nets": len(board.nets),
        "copper_layers": len(board.copper_layers),
        "segments": board.segment_count,
        "vias": board.via_count,
        "zones": board.zone_count,
    }
    try:
        width, height = board.board_size
        metrics["board_mm"] = [round(float(width), 2), round(float(height), 2)]
    except Exception:  # board outline may be absent or degenerate -- not fatal
        metrics["board_mm"] = None
    return metrics


def schematic_metrics(path: Path) -> dict[str, Any]:
    """Coarse shape metrics for a ``.kicad_sch`` (best effort, never raises)."""
    from kicad_tools.schema import Schematic

    sch = Schematic.load(path)
    return {
        "symbols": len(sch.symbols),
        "wires": len(sch.wires),
        "labels": len(sch.labels) + len(sch.global_labels) + len(sch.hierarchical_labels),
        "junctions": len(sch.junctions),
        "sheets": len(sch.sheets),
    }


def metrics_for(path: Path, kind: str) -> dict[str, Any]:
    """Metrics for a payload of ``kind``; ``{}`` when extraction fails."""
    try:
        return pcb_metrics(path) if kind == "pcb" else schematic_metrics(path)
    except Exception:
        return {}
