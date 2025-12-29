#!/usr/bin/env python3
"""
Check BOM parts availability against manufacturer parts libraries.

Validates part numbers and generates search URLs for checking
availability with the target manufacturer.

Usage:
    python3 scripts/kicad/check-parts.py bom.csv --manufacturer jlcpcb
    python3 scripts/kicad/check-parts.py bom.csv --manufacturer seeed
    python3 scripts/kicad/check-parts.py bom.csv --urls  # Generate search URLs
"""

import argparse
import csv
import re
import sys
from pathlib import Path
from urllib.parse import quote

from kicad_tools.manufacturers import (
    ManufacturerProfile,
    get_manufacturer_ids,
    get_profile,
)


def read_bom(bom_path: Path) -> list[dict]:
    """Read BOM CSV file."""
    components = []

    with open(bom_path, newline="", encoding="utf-8-sig") as f:
        # Try to detect delimiter
        sample = f.read(1024)
        f.seek(0)

        # Check for common delimiters
        if "\t" in sample:
            delimiter = "\t"
        elif ";" in sample:
            delimiter = ";"
        else:
            delimiter = ","

        reader = csv.DictReader(f, delimiter=delimiter)

        # Normalize column names
        if reader.fieldnames:
            # Create a mapping of normalized names
            column_map = {}
            for field in reader.fieldnames:
                normalized = field.lower().strip()
                if "part" in normalized and "number" in normalized:
                    column_map[field] = "mpn"
                elif normalized in ("mpn", "mfr_pn", "mfr part", "manufacturer part"):
                    column_map[field] = "mpn"
                elif normalized in ("value", "comment"):
                    column_map[field] = "value"
                elif normalized in ("designator", "designators", "reference", "refs"):
                    column_map[field] = "designator"
                elif normalized in ("quantity", "qty"):
                    column_map[field] = "quantity"
                elif normalized in ("footprint", "package"):
                    column_map[field] = "footprint"
                elif "lcsc" in normalized or "opl" in normalized or "sku" in normalized:
                    column_map[field] = "library_id"
                elif normalized in ("description", "desc"):
                    column_map[field] = "description"
                else:
                    column_map[field] = normalized

        for row in reader:
            # Normalize row keys
            normalized_row = {column_map.get(k, k.lower()): v for k, v in row.items()}
            components.append(normalized_row)

    return components


def extract_part_number(component: dict) -> str | None:
    """Extract the best part number identifier from a component."""
    # Priority order for part identification
    for key in ["mpn", "library_id", "value", "part number"]:
        if key in component and component[key]:
            return component[key].strip()
    return None


def get_search_url(part_number: str, profile: ManufacturerProfile) -> str | None:
    """Get manufacturer-specific search URL for a part."""
    if profile.parts_library is None:
        return None

    # URL encode the part number
    encoded = quote(part_number, safe="")
    return profile.parts_library.search_url_template.format(part_number=encoded)


def validate_library_id(library_id: str, profile: ManufacturerProfile) -> dict:
    """Validate a library ID format for the manufacturer."""
    result = {
        "valid": False,
        "tier": "unknown",
        "message": "",
    }

    if not library_id:
        result["message"] = "No library ID provided"
        return result

    # JLCPCB/LCSC format: C followed by digits (e.g., C123456)
    if profile.id == "jlcpcb":
        if re.match(r"^C\d+$", library_id):
            result["valid"] = True
            result["tier"] = "lcsc"
            result["message"] = "Valid LCSC part number"
        else:
            result["message"] = "Invalid LCSC format (expected Cxxxxxx)"

    # Seeed OPL - similar format or Seeed-specific
    elif profile.id == "seeed":
        if re.match(r"^C\d+$", library_id) or re.match(r"^\d{5,}$", library_id):
            result["valid"] = True
            result["tier"] = "opl"
            result["message"] = "Valid OPL part number"
        else:
            result["message"] = "Invalid OPL format"

    # PCBWay - no fixed format, any MPN works
    elif profile.id == "pcbway":
        result["valid"] = True
        result["tier"] = "turnkey"
        result["message"] = "PCBWay accepts any MPN"

    # OSHPark - no parts library
    elif profile.id == "oshpark":
        result["valid"] = False
        result["message"] = "OSHPark is PCB-only (no assembly)"

    return result


def check_components(
    components: list[dict],
    profile: ManufacturerProfile,
    show_urls: bool = False,
) -> dict:
    """Check all components against manufacturer library."""
    results = {
        "valid": [],
        "invalid": [],
        "missing": [],
        "no_assembly": False,
    }

    if not profile.supports_assembly():
        results["no_assembly"] = True
        return results

    for comp in components:
        part_number = extract_part_number(comp)
        library_id = comp.get("library_id", "")
        designator = comp.get("designator", comp.get("reference", "?"))
        value = comp.get("value", "")
        quantity = comp.get("quantity", "1")

        entry = {
            "designator": designator,
            "value": value,
            "part_number": part_number,
            "library_id": library_id,
            "quantity": quantity,
        }

        if library_id:
            validation = validate_library_id(library_id, profile)
            entry["validation"] = validation

            if validation["valid"]:
                results["valid"].append(entry)
            else:
                results["invalid"].append(entry)
        else:
            results["missing"].append(entry)

        # Add search URL if requested
        if show_urls and part_number:
            entry["search_url"] = get_search_url(part_number, profile)

    return results


def print_results(results: dict, profile: ManufacturerProfile, show_urls: bool = False):
    """Print component availability check results."""
    print(f"\n{'=' * 60}")
    print(f"PARTS CHECK - {profile.name}")
    print(f"{'=' * 60}")

    if results["no_assembly"]:
        print(f"\n{profile.name} does not offer assembly services.")
        print("This manufacturer is PCB-only.")
        return

    total = len(results["valid"]) + len(results["invalid"]) + len(results["missing"])

    print("\nSummary:")
    print(f"  Total components: {total}")
    print(f"  ✓ Valid library IDs: {len(results['valid'])}")
    print(f"  ⚠ Invalid library IDs: {len(results['invalid'])}")
    print(f"  ? Missing library IDs: {len(results['missing'])}")

    if results["valid"]:
        print(f"\n{'─' * 60}")
        print(f"✓ VALID ({len(results['valid'])} parts):")
        for comp in results["valid"][:5]:
            print(f"  {comp['designator']}: {comp['value']} [{comp['library_id']}]")
        if len(results["valid"]) > 5:
            print(f"  ... and {len(results['valid']) - 5} more")

    if results["invalid"]:
        print(f"\n{'─' * 60}")
        print(f"⚠ INVALID LIBRARY IDs ({len(results['invalid'])} parts):")
        for comp in results["invalid"]:
            msg = comp["validation"]["message"]
            print(f"  {comp['designator']}: {comp['value']} [{comp['library_id']}] - {msg}")

    if results["missing"]:
        print(f"\n{'─' * 60}")
        print(f"? MISSING LIBRARY IDs ({len(results['missing'])} parts):")
        for comp in results["missing"][:10]:
            print(f"  {comp['designator']}: {comp['value']}")
            if show_urls and comp.get("search_url"):
                print(f"    Search: {comp['search_url']}")
        if len(results["missing"]) > 10:
            print(f"  ... and {len(results['missing']) - 10} more")

    if profile.parts_library:
        print(f"\n{'─' * 60}")
        print(f"Parts Library: {profile.parts_library.name}")
        if profile.parts_library.catalog_url:
            print(f"Catalog: {profile.parts_library.catalog_url}")

    print(f"\n{'=' * 60}")


def export_search_urls(
    components: list[dict],
    profile: ManufacturerProfile,
    output_path: Path,
):
    """Export search URLs to CSV."""
    if not profile.parts_library:
        print(f"Error: {profile.name} has no parts library")
        return

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Designator", "Value", "Part Number", "Search URL"])

        for comp in components:
            part_number = extract_part_number(comp)
            if part_number:
                url = get_search_url(part_number, profile)
                writer.writerow(
                    [
                        comp.get("designator", ""),
                        comp.get("value", ""),
                        part_number,
                        url or "",
                    ]
                )

    print(f"Search URLs exported to: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Check BOM parts against manufacturer libraries")
    parser.add_argument("bom", type=Path, help="Path to BOM CSV file")
    parser.add_argument(
        "-m",
        "--manufacturer",
        default="jlcpcb",
        choices=get_manufacturer_ids(),
        help="Target manufacturer (default: jlcpcb)",
    )
    parser.add_argument("--urls", action="store_true", help="Show search URLs for missing parts")
    parser.add_argument("--export", type=Path, help="Export search URLs to CSV")

    args = parser.parse_args()

    if not args.bom.exists():
        print(f"Error: BOM file not found: {args.bom}")
        sys.exit(1)

    # Get manufacturer profile
    try:
        profile = get_profile(args.manufacturer)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    # Read BOM
    try:
        components = read_bom(args.bom)
    except Exception as e:
        print(f"Error reading BOM: {e}")
        sys.exit(1)

    if not components:
        print("Error: No components found in BOM")
        sys.exit(1)

    print(f"Loaded {len(components)} components from {args.bom.name}")

    # Export mode
    if args.export:
        export_search_urls(components, profile, args.export)
        return

    # Check mode
    results = check_components(components, profile, show_urls=args.urls)
    print_results(results, profile, show_urls=args.urls)

    # Exit code based on missing/invalid parts
    if results["no_assembly"]:
        sys.exit(0)
    elif results["invalid"]:
        sys.exit(1)
    elif results["missing"]:
        sys.exit(0)  # Warning only
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
