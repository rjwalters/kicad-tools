#!/usr/bin/env python3
"""
Export Gerber files for Seeed Fusion PCB manufacturing.

Generates:
- Gerber files (copper, mask, silk, drill)
- Drill files (Excellon format)
- Pick-and-place file (for PCBA)
- ZIP archive ready for upload

Usage:
    python3 scripts/kicad/export-gerbers.py hardware/chorus-hat-reva/kicad/chorus-hat-reva.kicad_pcb
    python3 scripts/kicad/export-gerbers.py --preview  # Show what would be generated
"""

import argparse
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Seeed Fusion layer naming convention
SEEED_LAYER_NAMES = {
    "F.Cu": "GTL",  # Top copper
    "B.Cu": "GBL",  # Bottom copper
    "In1.Cu": "G1",  # Inner layer 1
    "In2.Cu": "G2",  # Inner layer 2
    "F.Paste": "GTP",  # Top paste
    "B.Paste": "GBP",  # Bottom paste
    "F.SilkS": "GTO",  # Top silkscreen
    "B.SilkS": "GBO",  # Bottom silkscreen
    "F.Mask": "GTS",  # Top soldermask
    "B.Mask": "GBS",  # Bottom soldermask
    "Edge.Cuts": "GKO",  # Board outline
}

# KiCad layer IDs for 4-layer board
FOUR_LAYER_STACK = [
    "F.Cu",
    "In1.Cu",
    "In2.Cu",
    "B.Cu",
    "F.Paste",
    "B.Paste",
    "F.SilkS",
    "B.SilkS",
    "F.Mask",
    "B.Mask",
    "Edge.Cuts",
]


def find_kicad_cli() -> Path | None:
    """Find kicad-cli executable."""
    locations = [
        "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
        "/usr/local/bin/kicad-cli",
        "/opt/homebrew/bin/kicad-cli",
    ]

    for loc in locations:
        if Path(loc).exists():
            return Path(loc)

    try:
        result = subprocess.run(["which", "kicad-cli"], capture_output=True, text=True)
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except Exception:
        pass

    return None


def export_gerbers(pcb_path: Path, output_dir: Path, kicad_cli: Path) -> bool:
    """Export Gerber files using kicad-cli."""
    print(f"Exporting Gerbers from: {pcb_path}")

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    # Build layer list for export
    layers = ",".join(FOUR_LAYER_STACK)

    try:
        # Export Gerbers
        subprocess.run(
            [
                str(kicad_cli),
                "pcb",
                "export",
                "gerbers",
                "--output",
                str(output_dir) + "/",
                "--layers",
                layers,
                "--subtract-soldermask",
                "--no-x2",  # Use legacy format for compatibility
                "--no-netlist",
                "--precision",
                "6",
                str(pcb_path),
            ],
            check=True,
        )

        # Export drill files
        subprocess.run(
            [
                str(kicad_cli),
                "pcb",
                "export",
                "drill",
                "--output",
                str(output_dir) + "/",
                "--format",
                "excellon",
                "--drill-origin",
                "absolute",
                "--excellon-zeros-format",
                "decimal",
                "--excellon-units",
                "mm",
                "--generate-map",
                "--map-format",
                "gerberx2",
                str(pcb_path),
            ],
            check=True,
        )

        return True

    except subprocess.CalledProcessError as e:
        print(f"Error exporting Gerbers: {e}")
        return False


def export_position_file(pcb_path: Path, output_dir: Path, kicad_cli: Path) -> bool:
    """Export pick-and-place position file."""
    print("Exporting position file...")

    try:
        subprocess.run(
            [
                str(kicad_cli),
                "pcb",
                "export",
                "pos",
                "--output",
                str(output_dir / "positions.csv"),
                "--format",
                "csv",
                "--units",
                "mm",
                "--side",
                "both",
                "--smd-only",
                str(pcb_path),
            ],
            check=True,
        )
        return True

    except subprocess.CalledProcessError as e:
        print(f"Error exporting position file: {e}")
        return False


def rename_for_seeed(output_dir: Path, project_name: str):
    """Rename Gerber files to Seeed's expected naming convention."""
    print("Renaming files for Seeed Fusion...")

    for kicad_layer, seeed_ext in SEEED_LAYER_NAMES.items():
        # KiCad generates files like: project-F_Cu.gbr
        kicad_pattern = kicad_layer.replace(".", "_")
        for f in output_dir.glob(f"*{kicad_pattern}*"):
            new_name = f"{project_name}.{seeed_ext}"
            new_path = output_dir / new_name
            print(f"  {f.name} → {new_name}")
            f.rename(new_path)

    # Rename drill files
    for f in output_dir.glob("*.drl"):
        new_name = f"{project_name}.XLN"  # Excellon drill
        new_path = output_dir / new_name
        print(f"  {f.name} → {new_name}")
        f.rename(new_path)


def create_zip(output_dir: Path, project_name: str) -> Path:
    """Create ZIP file for upload to Seeed Fusion."""
    timestamp = datetime.now().strftime("%Y%m%d")
    zip_name = f"{project_name}_gerbers_{timestamp}.zip"
    zip_path = output_dir.parent / zip_name

    print(f"Creating ZIP: {zip_path}")

    with ZipFile(zip_path, "w") as zf:
        for f in output_dir.iterdir():
            if f.is_file() and not f.name.startswith("."):
                zf.write(f, f.name)
                print(f"  Added: {f.name}")

    return zip_path


def generate_fab_notes(output_dir: Path, project_name: str):
    """Generate fabrication notes file."""
    notes_path = output_dir / f"{project_name}_fab_notes.txt"

    notes = f"""
================================================================================
                        FABRICATION NOTES
                     {project_name} Rev A
                   Generated: {datetime.now().isoformat()}
================================================================================

BOARD SPECIFICATIONS
--------------------
Layers:          4-layer FR-4
Thickness:       1.6 mm
Copper:          1 oz (35 µm) outer, 0.5 oz (17.5 µm) inner
Finish:          HASL Lead-Free (or ENIG if available)
Soldermask:      Green (both sides)
Silkscreen:      White (both sides)
Min trace/space: 6 mil / 6 mil (0.1524 mm)
Min via:         0.3 mm drill / 0.6 mm pad
Board outline:   Defined in GKO layer

STACKUP (4-layer)
-----------------
L1 (Top):     Signal + components
L2 (GND):     Solid ground plane
L3 (Power):   Power + signal
L4 (Bottom):  Signal + components

SPECIAL NOTES
-------------
- This is a Raspberry Pi HAT form factor
- 40-pin header must align with Pi GPIO
- Mounting holes per HAT specification
- Clock distribution traces require impedance control (optional for Rev-A)

FILE INVENTORY
--------------
{project_name}.GTL    - Top copper
{project_name}.GBL    - Bottom copper
{project_name}.G1     - Inner layer 1 (GND)
{project_name}.G2     - Inner layer 2 (Power)
{project_name}.GTS    - Top soldermask
{project_name}.GBS    - Bottom soldermask
{project_name}.GTO    - Top silkscreen
{project_name}.GBO    - Bottom silkscreen
{project_name}.GTP    - Top paste
{project_name}.GBP    - Bottom paste
{project_name}.GKO    - Board outline
{project_name}.XLN    - Drill file (Excellon)
positions.csv         - Pick-and-place positions

CONTACT
-------
See repository for design files and support.

================================================================================
"""

    with open(notes_path, "w") as f:
        f.write(notes.strip())

    print(f"Fab notes written to: {notes_path}")


def preview_export(pcb_path: Path, output_dir: Path):
    """Preview what would be generated without actually exporting."""
    project_name = pcb_path.stem

    print(f"\n{'=' * 60}")
    print("GERBER EXPORT PREVIEW")
    print(f"{'=' * 60}")
    print(f"\nSource:  {pcb_path}")
    print(f"Output:  {output_dir}")
    print(f"Project: {project_name}")

    print("\nFiles that would be generated:")
    for kicad_layer, seeed_ext in SEEED_LAYER_NAMES.items():
        print(f"  {project_name}.{seeed_ext} ({kicad_layer})")
    print(f"  {project_name}.XLN (drill)")
    print("  positions.csv (pick-and-place)")
    print(f"  {project_name}_fab_notes.txt")

    print("\nZIP file:")
    print(f"  {project_name}_gerbers_YYYYMMDD.zip")

    print(f"\n{'=' * 60}")


def main():
    parser = argparse.ArgumentParser(description="Export Gerbers for Seeed Fusion")
    parser.add_argument(
        "pcb",
        nargs="?",
        type=Path,
        default=REPO_ROOT / "hardware/chorus-hat-reva/kicad/chorus-hat-reva.kicad_pcb",
        help="Path to KiCad PCB file",
    )
    parser.add_argument("--output-dir", "-o", type=Path, help="Output directory")
    parser.add_argument("--preview", action="store_true", help="Preview without exporting")
    parser.add_argument("--no-zip", action="store_true", help="Don't create ZIP file")
    parser.add_argument(
        "--no-rename", action="store_true", help="Keep KiCad naming (don't rename for Seeed)"
    )

    args = parser.parse_args()

    output_dir = args.output_dir
    if not output_dir:
        output_dir = args.pcb.parent.parent / "manufacturing" / "gerbers"

    project_name = args.pcb.stem

    if args.preview:
        preview_export(args.pcb, output_dir)
        return

    if not args.pcb.exists():
        print(f"Error: PCB file not found: {args.pcb}")
        print("Create the PCB layout in KiCad first.")
        sys.exit(1)

    kicad_cli = find_kicad_cli()
    if not kicad_cli:
        print("Error: kicad-cli not found")
        print("Install KiCad 8 from: https://www.kicad.org/download/")
        print("\nmacOS: brew install --cask kicad")
        sys.exit(1)

    # Clean output directory
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    # Export Gerbers
    if not export_gerbers(args.pcb, output_dir, kicad_cli):
        sys.exit(1)

    # Export position file
    export_position_file(args.pcb, output_dir, kicad_cli)

    # Rename for Seeed
    if not args.no_rename:
        rename_for_seeed(output_dir, project_name)

    # Generate fab notes
    generate_fab_notes(output_dir, project_name)

    # Create ZIP
    if not args.no_zip:
        zip_path = create_zip(output_dir, project_name)
        print(f"\n✓ Ready for upload to Seeed Fusion: {zip_path}")


if __name__ == "__main__":
    main()
