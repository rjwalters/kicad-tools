#!/usr/bin/env python3
"""Measure how much of the JLCPCB (jlcparts) catalog already has a stock 3D body.

Issue #7686, part 1.  For every in-stock ``jlc_components`` row, map the JLCPCB
``package`` string to a KiCad footprint with the same matcher ``kct parts
import`` uses (``PartImporter._match_footprint``), then ask whether that
footprint resolves to an *existing* stock body through the ``add-3d-models``
ladder (``make_library_resolver``: exact name, same-library variant, curated
substitution).  Rows with no such body are the share the LCSC tier would have
to cover.  Results are broken down by Basic/Preferred vs Extended and by category.

"Generatable family" (parametric generation, #7687) is reported as not
measured: there is no generator registry to ask yet.

Offline and read-only: it opens the SQLite catalog read-only and never touches
the network.  When the catalog is not cached it exits 2 with "not measured"
instead of inventing numbers -- download it with ``kct parts sync-catalog``
(large) and re-run.

Usage::

    uv run python scripts/research/measure_3d_catalog_coverage.py [--db PATH] [--json]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

Matcher = Callable[[str], str | None]
HasBody = Callable[[str], bool]

#: ``_match_footprint`` returns the bare package string at 0.3 confidence when it
#: found no real footprint; only confidence >= this counts as a match.
MIN_MATCH_CONFIDENCE = 0.5


def build_matcher() -> Matcher:
    """``package string -> KiCad lib_id`` using ``PartImporter._match_footprint``."""
    from kicad_tools.parts.importer import ImportOptions, PartImporter

    importer = PartImporter.__new__(PartImporter)  # the matcher reads no instance state
    cache: dict[str, str | None] = {}

    def match(package: str) -> str | None:
        if package not in cache:
            table = SimpleNamespace(package=package, pins=[])
            footprint, confidence = importer._match_footprint(
                package, table, ImportOptions(package=package)
            )
            cache[package] = footprint if confidence >= MIN_MATCH_CONFIDENCE else None
        return cache[package]

    return match


def build_body_check() -> HasBody:
    """``lib_id -> True`` when the installed KiCad libraries give an existing body."""
    from kicad_tools.footprints.library_path import detect_kicad_library_path
    from kicad_tools.pcb.models3d import ResolvedModels, make_library_resolver

    paths = detect_kicad_library_path()
    if not paths.found:
        raise SystemExit("KiCad footprint libraries not found: cannot test for stock bodies")
    resolver = make_library_resolver(paths, allow_variants=True, allow_substitutions=True)
    cache: dict[str, bool] = {}

    def has_body(lib_id: str) -> bool:
        if lib_id not in cache:
            resolved = resolver(lib_id)
            models = resolved.models if isinstance(resolved, ResolvedModels) else resolved
            cache[lib_id] = bool(models)
        return cache[lib_id]

    return has_body


def classify(rows: Iterable[sqlite3.Row], match: Matcher, has_body: HasBody) -> dict[str, Any]:
    """Count rows by ``(library_type, category) -> {stock_body, needs_lcsc, no_footprint}``."""
    keys = None
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    total: Counter[str] = Counter()
    for row in rows:
        if keys is None:
            keys = set(row.keys())
        library_type = (
            "basic"
            if str(row["library_type"] or "").lower()
            in {
                "basic",
                "preferred",
            }
            else "extended"
        )
        category = str(row["category"] if "category" in keys else "") or "(uncategorised)"
        lib_id = match(str(row["package"] or ""))
        if lib_id is None:
            outcome = "no_footprint"
        elif has_body(lib_id):
            outcome = "stock_body"
        else:
            outcome = "needs_lcsc"
        for bucket in (f"type:{library_type}", f"category:{category}", "all"):
            counts[bucket][outcome] += 1
        total[outcome] += 1
    return {"total": dict(total), "buckets": {k: dict(v) for k, v in sorted(counts.items())}}


def _percent(part: int, whole: int) -> str:
    return f"{100.0 * part / whole:5.1f}%" if whole else "   n/a"


def render_text(result: dict[str, Any]) -> str:
    lines = ["bucket | parts | stock body | needs LCSC (footprint, no body) | no footprint match"]
    for bucket, c in result["buckets"].items():
        n = sum(c.values())
        lines.append(
            f"{bucket} | {n} | {_percent(c.get('stock_body', 0), n)} | "
            f"{_percent(c.get('needs_lcsc', 0), n)} | {_percent(c.get('no_footprint', 0), n)}"
        )
    lines.append("generatable family (#7687): not measured (no generator registry yet)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", type=Path, help="jlcparts SQLite (default: kct's cached catalog)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--in-stock-only", action="store_true", help="only rows with stock > 0")
    args = parser.parse_args(argv)

    from kicad_tools.parts.jlcparts_catalog import get_catalog_path

    db = args.db or get_catalog_path()
    if not db.is_file():
        print(f"not measured: jlcparts catalog not found at {db}", file=sys.stderr)
        return 2
    where = " WHERE stock > 0" if args.in_stock_only else ""
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"SELECT * FROM jlc_components{where}")  # noqa: S608
        result = classify(rows, build_matcher(), build_body_check())
    print(json.dumps(result, indent=2) if args.json else render_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
