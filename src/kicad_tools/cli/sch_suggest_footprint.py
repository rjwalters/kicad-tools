#!/usr/bin/env python3
"""
Suggest library footprints for a schematic symbol.

Given a component reference, this command looks up matching KiCad library
footprints based on the symbol's pin count, the symbol's ``ki_fp_filters``
glob patterns, and an optional package keyword hint. It reuses the existing
library-detection primitives in ``kicad_tools.footprints.library_path`` and
the pad-counting helper in ``kicad_tools.cli.lib_footprints``.

When the symbol carries a ``ki_fp_filters`` property (the canonical KiCad
footprint hint), those glob patterns are AND-combined with the pin-count
filter so a ref-only suggestion (no ``--package``) lands on the right
variant (e.g. a 5-pin 74LVC1G17 -> ``SOT-23-5``). An explicit ``--package``
keyword overrides the inferred filters.

The KiCad standard footprint library must be installed for this command to
return suggestions. When no library can be found (common on CI runners and
fresh checkouts), the command prints an actionable message suggesting the
``KICAD_FOOTPRINT_DIR`` environment variable and exits non-zero rather than
crashing.

Usage:
    kicad-tools sch suggest-footprint board.kicad_sch --ref U7 --package SOT-23

    # Limit candidate libraries searched and number of results
    kicad-tools sch suggest-footprint board.kicad_sch --ref R1 \\
        --package R_0603 --limit 10

    # JSON output
    kicad-tools sch suggest-footprint board.kicad_sch --ref U7 \\
        --package SOT-23 --format json
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from pathlib import Path
from typing import Any

from kicad_tools.cli.lib_footprints import _count_pads
from kicad_tools.core.sexp_file import load_footprint
from kicad_tools.footprints.library_path import (
    LibraryPaths,
    detect_kicad_library_path,
    guess_standard_library,
    list_available_libraries,
)
from kicad_tools.schema import Schematic

# Cap how many footprint files we read while ranking, so a no-keyword search
# across all 150+ libraries cannot blow up into reading tens of thousands of
# .kicad_mod files.
_MAX_SCANNED_FOOTPRINTS = 5000


def _resolve_target_pin_count(sch: Schematic, sym: Any) -> int | None:
    """Determine the expected pad count for a symbol.

    Prefers the resolved library-symbol pin count (handles ``extends``
    chains via :meth:`Schematic.get_lib_symbol_resolved`); falls back to the
    instance pin list. Returns ``None`` if neither yields a positive count.
    """
    try:
        lib_sym = sch.get_lib_symbol_resolved(sym.lib_id)
        if lib_sym is not None and lib_sym.pin_count > 0:
            return lib_sym.pin_count
    except Exception:
        pass

    instance_count = len(sym.pins)
    if instance_count > 0:
        return instance_count
    return None


def _derive_keyword(sym: Any) -> str | None:
    """Best-effort package keyword from the symbol's value or lib_id.

    Uses the same prefix conventions as :func:`guess_standard_library` (e.g.
    ``SOT-`` -> ``Package_TO_SOT_SMD``) applied to the footprint-name-ish
    fields available on a symbol. This is intentionally weak; for precise
    package inference the symbol's ``ki_fp_filters`` property is used instead
    (see :func:`_get_fp_filters`). Returns the standard library name if a
    prefix matches, else ``None``.
    """
    for candidate in (sym.value, sym.lib_id.split(":")[-1]):
        if not candidate:
            continue
        lib = guess_standard_library(candidate)
        if lib:
            return lib
    return None


def _get_fp_filters(sch: Schematic, sym: Any) -> list[str]:
    """Return the symbol's ``ki_fp_filters`` glob patterns, space-split.

    KiCad symbols carry a ``ki_fp_filters`` property (e.g.
    ``"SOT?23* SOT?553* Texas?R-PDSO-G5?DCK*"``) that is the canonical
    footprint-suggestion hint. ``LibrarySymbol.from_sexp`` already parses
    arbitrary ``(property ...)`` nodes into ``LibrarySymbol.properties``, so
    we simply read the key from the *directly-parsed* library symbol returned
    by :meth:`Schematic.get_lib_symbol_resolved`.

    Note: ``resolve_extends`` copies pins/graphics but NOT properties. For a
    symbol that defines its own pins (the common case) the directly-parsed
    symbol is returned, so ``ki_fp_filters`` is preserved. For ``extends``-only
    derived symbols with no own filters this returns ``[]`` and the caller
    falls back to Phase 1 behavior (graceful, no regression).

    Returns the list of space-separated patterns, or an empty list when the
    property is absent or empty/whitespace.
    """
    try:
        lib_sym = sch.get_lib_symbol_resolved(sym.lib_id)
    except Exception:
        return []
    if lib_sym is None:
        return []
    raw = lib_sym.properties.get("ki_fp_filters", "")
    return raw.split()


def _candidate_library_paths(paths: LibraryPaths, keyword: str | None) -> list[tuple[str, Path]]:
    """Return (library_name, library_dir) pairs to scan for candidates.

    When *keyword* is given, libraries whose name OR whose standard-library
    mapping matches the keyword are preferred. If nothing matches (or no
    keyword), all available libraries are returned.
    """
    all_libs = list_available_libraries(paths)
    result: list[tuple[str, Path]] = []

    if keyword:
        kw_lower = keyword.lower()
        # A keyword may itself be a footprint-name prefix (e.g. "SOT-23",
        # "R_0603"); map it to a standard library when possible.
        mapped_lib = guess_standard_library(keyword)
        for lib in all_libs:
            lib_lower = lib.lower()
            if kw_lower in lib_lower or (mapped_lib and lib == mapped_lib):
                lib_path = paths.get_library_path(lib)
                if lib_path is not None:
                    result.append((lib, lib_path))

    if not result:
        # No keyword, or keyword did not match any library name: scan all.
        for lib in all_libs:
            lib_path = paths.get_library_path(lib)
            if lib_path is not None:
                result.append((lib, lib_path))

    return result


def _matches_fp_filters(name: str, fp_filters: list[str] | None) -> bool:
    """Return ``True`` if *name* matches any ``ki_fp_filters`` glob pattern.

    Patterns are matched case-sensitively with :func:`fnmatch.fnmatchcase`
    against the footprint stem. An empty/``None`` filter list matches nothing
    (callers treat "no filters" as "do not constrain by filters").
    """
    if not fp_filters:
        return False
    return any(fnmatch.fnmatchcase(name, pat) for pat in fp_filters)


def _rank_key(
    candidate: dict[str, Any],
    keyword: str | None,
    fp_filters: list[str] | None = None,
) -> tuple:
    """Sort key: filter matches, then keyword matches, non-hand-solder, A-Z."""
    name = candidate["footprint"]
    name_lower = name.lower()
    # ki_fp_filters matches rank above everything else when filters are active.
    filter_match = 0
    if _matches_fp_filters(name, fp_filters):
        filter_match = -1  # sorts before non-matches
    kw_match = 0
    if keyword and keyword.lower() in name_lower:
        kw_match = -1  # sorts before non-matches
    hand = 1 if "handsolder" in name_lower.replace("_", "") else 0
    return (filter_match, kw_match, hand, candidate["library"], name)


def find_footprint_candidates(
    paths: LibraryPaths,
    target_pins: int | None,
    keyword: str | None,
    limit: int,
    fp_filters: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Find footprints whose pad count matches *target_pins*.

    Args:
        paths: Detected library paths (must be ``found``).
        target_pins: Required pad count, or ``None`` to accept any count.
        keyword: Optional package keyword used for library filtering + ranking.
        limit: Maximum number of candidates to return.
        fp_filters: Optional ``ki_fp_filters`` glob patterns. When non-empty,
            a footprint must match at least one pattern **in addition** to the
            pad-count check (AND-combined), and matching footprints rank first.
            The patterns are intentionally broad (e.g. ``SOT?23*`` matches
            SOT-23-5, SOT-23-6, and SOT-23), so the pad-count filter is what
            disambiguates the correct variant.

    Returns:
        Ranked list of dicts with ``library``, ``footprint``, ``pads`` keys.
    """
    candidates: list[dict[str, Any]] = []
    scanned = 0

    for lib_name, lib_dir in _candidate_library_paths(paths, keyword):
        for mod_file in sorted(lib_dir.glob("*.kicad_mod")):
            if scanned >= _MAX_SCANNED_FOOTPRINTS:
                break
            # AND-combine ki_fp_filters with the pad-count check: when filters
            # are present, a footprint must match at least one glob pattern.
            # The stem glob is cheap (no file read), so skip non-matches BEFORE
            # counting against the scan cap; the cap exists to bound expensive
            # ``.kicad_mod`` reads, not free string matches. This lets a
            # ki_fp_filters-driven search reach late-alphabet libraries (e.g.
            # ``Package_TO_SOT_SMD``) that sit past the raw 5000-file cap.
            if fp_filters and not _matches_fp_filters(mod_file.stem, fp_filters):
                continue
            scanned += 1
            try:
                sexp = load_footprint(mod_file)
                pad_count = _count_pads(sexp)
            except Exception:
                continue
            if target_pins is not None and pad_count != target_pins:
                continue
            candidates.append(
                {
                    "library": lib_name,
                    "footprint": mod_file.stem,
                    "pads": pad_count,
                }
            )
        if scanned >= _MAX_SCANNED_FOOTPRINTS:
            break

    candidates.sort(key=lambda c: _rank_key(c, keyword, fp_filters))
    return candidates[:limit]


def run_suggest_footprint(
    schematic_path: Path,
    ref: str,
    package: str | None = None,
    output_format: str = "text",
    limit: int = 20,
    config_override: str | Path | None = None,
) -> int:
    """Suggest library footprints for the symbol *ref*.

    Returns 0 when at least one candidate is found, 1 otherwise (including the
    no-library and symbol-not-found cases).
    """
    if not schematic_path.exists():
        print(f"Error: File not found: {schematic_path}", file=sys.stderr)
        return 1

    try:
        sch = Schematic.load(schematic_path)
    except Exception as e:  # pragma: no cover - defensive
        print(f"Error loading schematic: {e}", file=sys.stderr)
        return 1

    sym = sch.get_symbol(ref)
    if sym is None:
        print(f"Error: Symbol '{ref}' not found in {schematic_path.name}", file=sys.stderr)
        return 1

    target_pins = _resolve_target_pin_count(sch, sym)
    keyword = package or _derive_keyword(sym)

    # The symbol's ki_fp_filters are the canonical footprint hint. When an
    # explicit --package keyword is given it acts as a narrower/override and
    # we skip the (broad) inferred filters so the user's intent wins. With no
    # --package, the filters drive ref-only suggestion (AND-combined with the
    # pin-count filter to disambiguate). Absent/empty filters -> Phase 1.
    fp_filters: list[str] = [] if package else _get_fp_filters(sch, sym)

    # Detect KiCad library; degrade gracefully if absent.
    paths = detect_kicad_library_path(config_override)
    if not paths.found:
        print(
            "Error: No KiCad footprint library found. "
            "suggest-footprint requires the standard KiCad footprint libraries.\n"
            "Set the KICAD_FOOTPRINT_DIR environment variable to the directory "
            "containing your '*.pretty' footprint libraries, e.g.\n"
            "  export KICAD_FOOTPRINT_DIR="
            "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
            file=sys.stderr,
        )
        return 1

    candidates = find_footprint_candidates(
        paths, target_pins, keyword, limit, fp_filters=fp_filters
    )

    if output_format == "json":
        print(
            json.dumps(
                {
                    "reference": ref,
                    "value": sym.value,
                    "lib_id": sym.lib_id,
                    "pin_count": target_pins,
                    "package_keyword": keyword,
                    "fp_filters": fp_filters,
                    "library_source": paths.source,
                    "candidates": candidates,
                },
                indent=2,
            )
        )
    else:
        pin_desc = f"{target_pins} pins" if target_pins is not None else "unknown pin count"
        kw_desc = f", package hint '{keyword}'" if keyword else ""
        filt_desc = f", ki_fp_filters {fp_filters}" if fp_filters else ""
        print(f"Suggestions for {ref} ({sym.value or sym.lib_id}, {pin_desc}{kw_desc}{filt_desc}):")
        if not candidates:
            print("  (no matching footprints found)")
        else:
            for c in candidates:
                print(f"  {c['library']}:{c['footprint']} ({c['pads']} pads)")

    if not candidates:
        hint = ""
        if not package:
            hint = " Try narrowing with --package (e.g. --package SOT-23)."
        print(
            f"No matching footprints found for {ref}.{hint}",
            file=sys.stderr,
        )
        return 1

    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for standalone usage."""
    parser = argparse.ArgumentParser(
        description="Suggest library footprints for a schematic symbol",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", type=Path, help="Path to .kicad_sch file")
    parser.add_argument("--ref", required=True, help="Symbol reference (e.g., U7, R1)")
    parser.add_argument(
        "--package",
        help="Package keyword hint to filter/rank candidates (e.g., SOT-23, R_0603)",
    )
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format")
    parser.add_argument(
        "--limit", type=int, default=20, help="Maximum number of suggestions (default: 20)"
    )

    args = parser.parse_args(argv)

    return run_suggest_footprint(
        schematic_path=args.schematic,
        ref=args.ref,
        package=args.package,
        output_format=args.format,
        limit=args.limit,
    )


if __name__ == "__main__":
    sys.exit(main())
