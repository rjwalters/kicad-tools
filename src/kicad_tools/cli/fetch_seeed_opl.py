#!/usr/bin/env python3
"""
Fetch Seeed Studio OPL (Open Parts Library) for KiCad.

The Seeed OPL provides pre-verified components for Seeed Fusion PCBA,
with guaranteed availability and competitive pricing.

Usage:
    python3 scripts/kicad/fetch-seeed-opl.py --list           # List available categories
    python3 scripts/kicad/fetch-seeed-opl.py --update         # Update local cache
    python3 scripts/kicad/fetch-seeed-opl.py --search "0402"  # Search components
"""

import argparse
import subprocess
import sys
from pathlib import Path

# Repository root
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HARDWARE_DIR = REPO_ROOT / "hardware"
LIB_DIR = HARDWARE_DIR / "chorus-hat-reva" / "lib"

# Seeed OPL sources
SEEED_OPL_KICAD_URL = "https://github.com/Seeed-Studio/OPL_Kicad_Library.git"
SEEED_OPL_CSV_URL = "https://statics3.seeedstudio.com/fusion/opl/opl_smt.csv"

# Keystone components for Chorus HAT (from engineering plan)
KEYSTONE_COMPONENTS = {
    "TCXO": {
        "part": "ASTX-H11-24.576MHZ-T",
        "package": "SMD-3225-4",
        "function": "24.576 MHz TCXO, 3.3V CMOS",
        "opl_verified": True,
    },
    "MCU": {
        "part": "STM32C011F4P6TR",
        "package": "TSSOP-20",
        "function": "Cortex-M0+ timebase MCU",
        "opl_verified": True,
    },
    "DAC": {
        "part": "PCM5122PWR",
        "package": "TSSOP-28",
        "function": "Stereo audio DAC, external MCLK",
        "opl_verified": True,
    },
}

# Standard OPL passive values we'll likely need
OPL_PASSIVES = {
    "resistors_0402": [
        "0R",
        "10R",
        "22R",
        "33R",
        "47R",
        "100R",
        "220R",
        "330R",
        "470R",
        "1K",
        "2.2K",
        "3.3K",
        "4.7K",
        "10K",
        "22K",
        "33K",
        "47K",
        "100K",
    ],
    "capacitors_0402": [
        "100pF",
        "1nF",
        "10nF",
        "100nF",
        "1uF",
        "2.2uF",
        "4.7uF",
        "10uF",
    ],
    "capacitors_0805": [
        "10uF",
        "22uF",
        "47uF",
        "100uF",
    ],
}


def check_git():
    """Verify git is available."""
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def clone_opl_library(force: bool = False):
    """Clone or update the Seeed OPL KiCad library."""
    opl_dir = LIB_DIR / "seeed-opl"

    if opl_dir.exists() and not force:
        print(f"OPL library exists at {opl_dir}")
        print("Use --force to re-download")
        return opl_dir

    if opl_dir.exists():
        print("Removing existing OPL library...")
        subprocess.run(["rm", "-rf", str(opl_dir)], check=True)

    print("Cloning Seeed OPL KiCad library...")
    subprocess.run(["git", "clone", "--depth=1", SEEED_OPL_KICAD_URL, str(opl_dir)], check=True)

    print(f"OPL library cloned to {opl_dir}")
    return opl_dir


def fetch_opl_csv():
    """Fetch the current OPL component list CSV."""
    csv_path = LIB_DIR / "seeed-opl" / "opl_smt.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    print("Fetching OPL component list...")
    try:
        subprocess.run(["curl", "-sL", "-o", str(csv_path), SEEED_OPL_CSV_URL], check=True)
        print(f"OPL CSV saved to {csv_path}")
        return csv_path
    except subprocess.CalledProcessError as e:
        print(f"Warning: Could not fetch OPL CSV: {e}")
        return None


def search_opl(query: str):
    """Search the OPL CSV for components matching query."""
    csv_path = LIB_DIR / "seeed-opl" / "opl_smt.csv"

    if not csv_path.exists():
        print("OPL CSV not found. Run with --update first.")
        return []

    import csv

    results = []
    with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Search across all fields
            row_str = " ".join(str(v) for v in row.values()).lower()
            if query.lower() in row_str:
                results.append(row)

    return results


def list_categories():
    """List available component categories in OPL."""
    csv_path = LIB_DIR / "seeed-opl" / "opl_smt.csv"

    if not csv_path.exists():
        print("OPL CSV not found. Run with --update first.")
        return

    import csv

    categories = set()
    with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if "Category" in row:
                categories.add(row["Category"])

    print("OPL Component Categories:")
    for cat in sorted(categories):
        if cat:
            print(f"  - {cat}")


def show_keystone_status():
    """Show status of keystone components for Chorus HAT."""
    print("\nChorus HAT Keystone Components:")
    print("=" * 60)
    for name, info in KEYSTONE_COMPONENTS.items():
        status = "✓ OPL" if info["opl_verified"] else "⚠ Non-OPL"
        print(f"\n{name}:")
        print(f"  Part:     {info['part']}")
        print(f"  Package:  {info['package']}")
        print(f"  Function: {info['function']}")
        print(f"  Status:   {status}")


def generate_lib_table():
    """Generate KiCad library table entries for OPL."""
    opl_dir = LIB_DIR / "seeed-opl"

    if not opl_dir.exists():
        print("OPL library not found. Run with --update first.")
        return

    # Find all symbol libraries
    sym_libs = list(opl_dir.glob("**/*.kicad_sym"))

    print("\n# Add to sym-lib-table (in KiCad or project):")
    for lib in sorted(sym_libs):
        name = lib.stem
        rel_path = lib.relative_to(REPO_ROOT)
        print(
            f'(lib (name "OPL_{name}")(type "KiCad")(uri "${{KIPRJMOD}}/../../{rel_path}")(options "")(descr "Seeed OPL"))'
        )

    # Find all footprint libraries
    fp_libs = list(opl_dir.glob("**/*.pretty"))

    print("\n# Add to fp-lib-table:")
    for lib in sorted(fp_libs):
        name = lib.stem
        rel_path = lib.relative_to(REPO_ROOT)
        print(
            f'(lib (name "OPL_{name}")(type "KiCad")(uri "${{KIPRJMOD}}/../../{rel_path}")(options "")(descr "Seeed OPL"))'
        )


def main():
    parser = argparse.ArgumentParser(description="Manage Seeed OPL library for KiCad")
    parser.add_argument("--update", action="store_true", help="Download/update OPL library")
    parser.add_argument("--force", action="store_true", help="Force re-download of library")
    parser.add_argument("--list", action="store_true", help="List OPL categories")
    parser.add_argument("--search", type=str, help="Search OPL for components")
    parser.add_argument("--keystone", action="store_true", help="Show keystone component status")
    parser.add_argument(
        "--lib-table", action="store_true", help="Generate KiCad library table entries"
    )

    args = parser.parse_args()

    if not any([args.update, args.list, args.search, args.keystone, args.lib_table]):
        parser.print_help()
        print("\n")
        show_keystone_status()
        return

    if args.update:
        if not check_git():
            print("Error: git is required")
            sys.exit(1)
        clone_opl_library(force=args.force)
        fetch_opl_csv()

    if args.list:
        list_categories()

    if args.search:
        results = search_opl(args.search)
        print(f"\nFound {len(results)} matches for '{args.search}':")
        for r in results[:20]:  # Limit output
            print(f"  - {r.get('Part Number', 'N/A')}: {r.get('Description', 'N/A')}")
        if len(results) > 20:
            print(f"  ... and {len(results) - 20} more")

    if args.keystone:
        show_keystone_status()

    if args.lib_table:
        generate_lib_table()


if __name__ == "__main__":
    main()
