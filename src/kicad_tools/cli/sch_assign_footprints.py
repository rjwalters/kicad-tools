#!/usr/bin/env python3
"""
Bulk-assign footprints to schematic symbols that are missing one.

Walks every sheet of a hierarchical schematic, finds the symbols whose
``Footprint`` property is empty or ``~``, and uses the ``suggest-footprint``
machinery (pin count + ``ki_fp_filters`` glob + project ``fp-lib-table``)
to propose a candidate. Symbols whose top candidate is **unambiguous**
get written via the same in-place edit path as ``set-footprint --map``.
Ambiguous and no-candidate symbols are reported and skipped — the user
either narrows them with ``suggest-footprint --ref <ref>`` or hand-feeds
a complete mapping to ``set-footprint --map``.

Ambiguity policy for ``--auto`` (the default and only mode):

    "Unambiguous" means the top candidate's rank tier (``ki_fp_filters``
    match, keyword match, hand-solder flag) is **strictly better** than
    the runner-up's. A single-candidate result is trivially unambiguous;
    a tie at the top tier is intentionally **ambiguous** — no confidence
    threshold, no "pick alphabetical first" fallback. This matches the
    behaviour of ``suggest-footprint`` for ref-only suggestion: if the
    library gives back two equally-ranked hits, the human decides.

Usage:
    # Assign every unambiguous missing footprint, write in place.
    kicad-tools sch assign-footprints board.kicad_sch --auto

    # Preview only — emit a JSON report, do not modify files.
    kicad-tools sch assign-footprints board.kicad_sch --auto \\
        --dry-run --format json

    # Re-assign even symbols that already have a footprint set.
    kicad-tools sch assign-footprints board.kicad_sch --auto --force
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from kicad_tools.cli.sch_footprint_common import (
    is_missing_footprint,
    iter_missing_footprint_symbols,
)
from kicad_tools.cli.sch_set_footprint import run_set_footprint
from kicad_tools.cli.sch_suggest_footprint import (
    _derive_keyword,
    _get_fp_filters,
    _rank_key,
    _resolve_target_pin_count,
    find_footprint_candidates,
)
from kicad_tools.footprints.fp_lib_table import find_project_fp_lib_table
from kicad_tools.footprints.library_path import detect_kicad_library_path


def _is_unambiguous(
    candidates: list[dict[str, Any]],
    fp_filters: list[str],
    keyword: str | None,
) -> bool:
    """Return ``True`` when the top candidate strictly out-ranks the runner-up.

    Compares the first four tiers of :func:`_rank_key` — filter match,
    keyword match, project-vs-global origin, and hand-solder flag — which
    are the signals that reflect "this candidate is a better fit for the
    symbol". The trailing ``(library, footprint_name)`` tiers are
    alphabetical tie-breakers and are deliberately excluded from the
    ambiguity decision: a sort-order tie-break is not real disambiguation.

    A single-candidate list is treated as unambiguous (there is nothing
    to be ambiguous *with*).
    """
    if not candidates:
        return False
    if len(candidates) == 1:
        return True
    top_tier = _rank_key(candidates[0], keyword, fp_filters)[:4]
    runner_tier = _rank_key(candidates[1], keyword, fp_filters)[:4]
    return top_tier != runner_tier


def _classify_symbol(
    sch: Any,
    sym: Any,
    *,
    schematic_path: Path,
    paths: Any,
    limit: int,
    use_project_table: bool,
) -> dict[str, Any]:
    """Compute the candidate list + auto-assign decision for one symbol.

    Returns a dict with the keys ``reference``, ``value``, ``lib_id``,
    ``current_footprint``, ``pin_count``, ``package_keyword``,
    ``fp_filters``, ``candidates`` (top-``limit``), ``status``
    (``"assigned" | "ambiguous" | "no_candidates"``) and ``assigned``
    (the chosen footprint string when ``status == "assigned"``).
    """
    target_pins = _resolve_target_pin_count(sch, sym)
    keyword = _derive_keyword(sym)
    fp_filters = _get_fp_filters(sch, sym)

    candidates = find_footprint_candidates(
        paths,
        target_pins,
        keyword,
        limit,
        fp_filters=fp_filters,
        schematic_path=schematic_path,
        use_project_table=use_project_table,
    )

    record: dict[str, Any] = {
        "reference": sym.reference,
        "value": sym.value,
        "lib_id": sym.lib_id,
        "current_footprint": sym.footprint or "",
        "pin_count": target_pins,
        "package_keyword": keyword,
        "fp_filters": fp_filters,
        "candidates": candidates,
        "status": "no_candidates",
        "assigned": None,
    }

    if not candidates:
        return record

    if _is_unambiguous(candidates, fp_filters, keyword):
        top = candidates[0]
        record["status"] = "assigned"
        record["assigned"] = f"{top['library']}:{top['footprint']}"
    else:
        record["status"] = "ambiguous"

    return record


def _emit_text_report(
    schematic_path: Path,
    records: list[dict[str, Any]],
    *,
    dry_run: bool,
    project_table: Path | None,
    library_source: str,
    duplicates_skipped: int,
) -> None:
    """Pretty-print a one-symbol-per-line report (text format)."""
    assigned = [r for r in records if r["status"] == "assigned"]
    ambiguous = [r for r in records if r["status"] == "ambiguous"]
    no_cand = [r for r in records if r["status"] == "no_candidates"]

    print(f"assign-footprints: {schematic_path}")
    print(f"  library source:  {library_source}")
    if project_table is not None:
        print(f"  project fp-lib-table: {project_table}")
    print(
        f"  scanned: {len(records)}  assigned: {len(assigned)}"
        f"  ambiguous: {len(ambiguous)}  no-candidate: {len(no_cand)}"
    )
    if duplicates_skipped:
        print(
            f"  duplicate-ref collisions skipped: {duplicates_skipped}"
            " (multi-sheet instance of same ref, first wins)"
        )

    if assigned:
        print()
        print(f"Assigned ({len(assigned)}):")
        for r in assigned:
            print(f"  {r['reference']:<8} -> {r['assigned']}  ({r['lib_id']})")

    if ambiguous:
        print()
        print(f"Ambiguous ({len(ambiguous)}):")
        for r in ambiguous:
            print(f"  {r['reference']:<8} ({r['lib_id']}, {r['pin_count']} pins)")
            for c in r["candidates"][:5]:
                origin_tag = " [project]" if c.get("origin") == "project" else ""
                print(f"      {c['library']}:{c['footprint']}{origin_tag}")
            if len(r["candidates"]) > 5:
                print(f"      ... and {len(r['candidates']) - 5} more")

    if no_cand:
        print()
        print(f"No candidates ({len(no_cand)}):")
        for r in no_cand:
            kw = f" keyword={r['package_keyword']!r}" if r["package_keyword"] else ""
            print(f"  {r['reference']:<8} ({r['lib_id']}, {r['pin_count']} pins){kw}")

    print()
    if dry_run:
        print(f"Dry run: {len(assigned)} footprint(s) would be assigned")
    elif not assigned:
        print("No unambiguous assignments to apply.")


def run_assign_footprints(
    schematic_path: Path,
    *,
    dry_run: bool = False,
    output_format: str = "text",
    limit: int = 20,
    backup: bool = True,
    validate: bool = True,
    include_power: bool = False,
    include_dnp: bool = False,
    force: bool = False,
    no_project_lib: bool = False,
    config_override: str | Path | None = None,
) -> int:
    """Bulk-assign unambiguous footprints to missing-footprint symbols.

    Returns 0 when at least one assignment succeeds OR there were no
    missing-footprint symbols to consider. Returns 1 when no library is
    available, every symbol was ambiguous / no-candidate, or any write
    error occurred.

    See the module docstring for the ambiguity policy.
    """
    if not schematic_path.exists():
        print(f"Error: File not found: {schematic_path}", file=sys.stderr)
        return 1

    paths = detect_kicad_library_path(config_override)
    project_table = (
        find_project_fp_lib_table(schematic_path) if not no_project_lib else None
    )
    if not paths.found and project_table is None:
        print(
            "Error: No KiCad footprint library found. "
            "assign-footprints requires the standard KiCad footprint libraries "
            "or a project fp-lib-table.\n"
            "Set the KICAD_FOOTPRINT_DIR environment variable to the directory "
            "containing your '*.pretty' footprint libraries, e.g.\n"
            "  export KICAD_FOOTPRINT_DIR="
            "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
            file=sys.stderr,
        )
        return 1

    # Walk the hierarchy and classify each symbol. ``--force`` widens the
    # iteration to every symbol; without it we only consider truly-empty
    # footprints (so we never silently overwrite a designer's choice).
    records: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    duplicates_skipped = 0
    for _node, sym, sch in iter_missing_footprint_symbols(
        schematic_path,
        include_power=include_power,
        include_dnp=include_dnp,
        include_assigned=force,
    ):
        # Without --force, only act on truly-empty footprints; --force lets
        # us reconsider already-assigned symbols too.
        if not force and not is_missing_footprint(sym):
            continue

        ref = sym.reference
        if not ref:
            continue
        if ref in seen_refs:
            # The same ref can show up under multiple sheet instances of
            # the same sub-sheet. The footprint property lives once per
            # symbol instance in the source schematic, so deduping on the
            # first occurrence is correct: ``set_footprint_text`` will
            # find that one occurrence regardless.
            duplicates_skipped += 1
            continue
        seen_refs.add(ref)

        record = _classify_symbol(
            sch,
            sym,
            schematic_path=schematic_path,
            paths=paths,
            limit=limit,
            use_project_table=not no_project_lib,
        )
        records.append(record)

    # Build the mapping for run_set_footprint.
    mapping: dict[str, str] = {
        r["reference"]: r["assigned"] for r in records if r["status"] == "assigned"
    }

    library_source = paths.source if paths.found else "project-only"

    if output_format == "json":
        report = {
            "schematic": str(schematic_path),
            "library_source": library_source,
            "project_lib_table": (
                str(project_table) if project_table is not None else None
            ),
            "scanned": len(records),
            "assigned": [
                {
                    "reference": r["reference"],
                    "lib_id": r["lib_id"],
                    "footprint": r["assigned"],
                }
                for r in records
                if r["status"] == "assigned"
            ],
            "ambiguous": [
                {
                    "reference": r["reference"],
                    "lib_id": r["lib_id"],
                    "pin_count": r["pin_count"],
                    "fp_filters": r["fp_filters"],
                    "candidates": r["candidates"],
                }
                for r in records
                if r["status"] == "ambiguous"
            ],
            "no_candidates": [
                {
                    "reference": r["reference"],
                    "lib_id": r["lib_id"],
                    "pin_count": r["pin_count"],
                    "package_keyword": r["package_keyword"],
                }
                for r in records
                if r["status"] == "no_candidates"
            ],
            "duplicates_skipped": duplicates_skipped,
            "dry_run": dry_run,
        }
        print(json.dumps(report, indent=2))
    else:
        _emit_text_report(
            schematic_path,
            records,
            dry_run=dry_run,
            project_table=project_table,
            library_source=library_source,
            duplicates_skipped=duplicates_skipped,
        )

    # No symbols at all -> success (nothing to do is not a failure).
    if not records:
        return 0

    # Symbols existed but nothing was assignable -> non-zero so CI fails loudly.
    if not mapping:
        return 1

    if dry_run:
        return 0

    # Hand the assignments off to the validated write path.
    write_rc = run_set_footprint(
        schematic_path=schematic_path,
        mapping=mapping,
        backup=backup,
        validate=validate,
        config_override=config_override,
    )
    return write_rc


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for standalone usage."""
    parser = argparse.ArgumentParser(
        description="Bulk-assign footprints to symbols missing one",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", type=Path, help="Path to .kicad_sch file")
    parser.add_argument(
        "--auto",
        action="store_true",
        default=True,
        help=(
            "Assign only unambiguous candidates (default; only mode currently "
            "supported). 'Unambiguous' = top candidate strictly out-ranks the "
            "runner-up on ki_fp_filters/keyword/hand-solder tier."
        ),
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Preview the proposed mapping without modifying files.",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text). JSON emits a full report.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max candidates considered per symbol (default: 20).",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip backup files on write.",
    )
    parser.add_argument(
        "--no-validate",
        dest="validate",
        action="store_false",
        default=True,
        help="Skip pin-count validation on the resolved mapping.",
    )
    parser.add_argument(
        "--include-power",
        action="store_true",
        help="Also consider power: symbols (default: skip).",
    )
    parser.add_argument(
        "--include-dnp",
        action="store_true",
        help="Also consider DNP symbols (default: skip).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Reconsider symbols that already have a non-empty footprint. "
            "Without --force, existing assignments are never overwritten."
        ),
    )
    parser.add_argument(
        "--no-project-lib",
        action="store_true",
        help=(
            "Ignore the project's fp-lib-table and only use global "
            "footprint libraries (CI / reproducibility opt-out)."
        ),
    )

    args = parser.parse_args(argv)

    return run_assign_footprints(
        schematic_path=args.schematic,
        dry_run=args.dry_run,
        output_format=args.format,
        limit=args.limit,
        backup=not args.no_backup,
        validate=args.validate,
        include_power=args.include_power,
        include_dnp=args.include_dnp,
        force=args.force,
        no_project_lib=args.no_project_lib,
    )


if __name__ == "__main__":
    sys.exit(main())
