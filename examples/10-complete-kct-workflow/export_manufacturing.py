#!/usr/bin/env python3
"""
Complete KCT Workflow Example: Manufacturing Export

This script demonstrates exporting manufacturing files from a PCB design.
It shows how to generate:

1. Gerber files (copper layers, solder mask, silkscreen, drill)
2. Bill of Materials (BOM) in JLCPCB format
3. Pick-and-Place (CPL) file for assembly

These files are ready to upload to PCB fabrication services like JLCPCB,
PCBWay, or OSH Park.

Usage:
    # First, run generate_design.py to create the PCB
    python generate_design.py

    # Then export manufacturing files
    python export_manufacturing.py

    # Or specify custom paths
    python export_manufacturing.py output/led_indicator_routed.kicad_pcb

This example pairs with generate_design.py to show the complete
workflow from specification to manufacturing files.
"""

from __future__ import annotations

import sys
from pathlib import Path


def export_gerbers(pcb_path: Path, output_dir: Path, manufacturer: str = "jlcpcb") -> Path | None:
    """
    Export Gerber files for PCB fabrication.

    Args:
        pcb_path: Path to the routed PCB file
        output_dir: Directory for output files
        manufacturer: Target manufacturer preset

    Returns:
        Path to the gerber zip file, or None if export fails
    """
    from kicad_tools.export import GerberExporter

    print("=" * 60)
    print("Exporting Gerber Files")
    print("=" * 60)

    print(f"\n1. Loading PCB: {pcb_path.name}")
    print(f"   Manufacturer preset: {manufacturer}")

    try:
        exporter = GerberExporter(pcb_path)

        gerber_dir = output_dir / "gerbers"
        gerber_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n2. Exporting to: {gerber_dir}")

        gerber_path = exporter.export_for_manufacturer(manufacturer, gerber_dir)

        print(f"\n3. Gerbers exported: {gerber_path}")
        print("\n   Included layers:")
        print("   - F.Cu (front copper)")
        print("   - B.Cu (back copper)")
        print("   - F.SilkS (front silkscreen)")
        print("   - B.SilkS (back silkscreen)")
        print("   - F.Mask (front solder mask)")
        print("   - B.Mask (back solder mask)")
        print("   - Edge.Cuts (board outline)")
        print("   - Drill files (PTH and NPTH)")

        return gerber_path

    except Exception as e:
        print(f"\n   Error exporting Gerbers: {e}")
        return None


def export_bom(schematic_path: Path, output_dir: Path, manufacturer: str = "jlcpcb") -> Path | None:
    """
    Export Bill of Materials for assembly.

    Args:
        schematic_path: Path to the schematic file
        output_dir: Directory for output files
        manufacturer: Target manufacturer format

    Returns:
        Path to the BOM CSV file, or None if export fails
    """
    from kicad_tools.export import BOMExportConfig, export_bom
    from kicad_tools.schema.bom import extract_bom

    print("\n" + "=" * 60)
    print("Exporting Bill of Materials")
    print("=" * 60)

    print(f"\n1. Loading schematic: {schematic_path.name}")
    print(f"   Format: {manufacturer}")

    try:
        # Extract BOM from schematic
        bom = extract_bom(str(schematic_path))

        print(f"\n2. Found {len(bom.items)} components")

        # Configure BOM export
        bom_config = BOMExportConfig(
            include_dnp=False,  # Exclude "Do Not Populate" items
            group_by_value=True,  # Group identical components
            include_lcsc=True,  # Include LCSC part numbers
            include_mfr=True,  # Include manufacturer info
        )

        # Export BOM
        bom_csv = export_bom(bom.items, manufacturer=manufacturer, config=bom_config)

        bom_path = output_dir / "bom.csv"
        bom_path.write_text(bom_csv)

        print(f"\n3. BOM exported: {bom_path}")
        print("\n   BOM preview:")
        for line in bom_csv.strip().split("\n")[:6]:
            print(f"   {line}")

        return bom_path

    except Exception as e:
        print(f"\n   Error exporting BOM: {e}")
        return None


def export_pnp(pcb_path: Path, output_dir: Path, manufacturer: str = "jlcpcb") -> Path | None:
    """
    Export Pick-and-Place file for assembly.

    Args:
        pcb_path: Path to the PCB file
        output_dir: Directory for output files
        manufacturer: Target manufacturer format

    Returns:
        Path to the CPL CSV file, or None if export fails
    """
    from kicad_tools.export import PnPExportConfig, export_pnp
    from kicad_tools.schema.pcb import PCB

    print("\n" + "=" * 60)
    print("Exporting Pick-and-Place File")
    print("=" * 60)

    print(f"\n1. Loading PCB: {pcb_path.name}")
    print(f"   Format: {manufacturer}")

    try:
        # Load PCB
        pcb = PCB.load(str(pcb_path))

        print(f"\n2. Found {len(list(pcb.footprints))} footprints")

        # Configure PnP export
        pnp_config = PnPExportConfig(
            use_aux_origin=True,  # Use KiCad's auxiliary axis origin
            include_dnp=False,  # Exclude DNP components
        )

        # Export PnP
        pnp_csv = export_pnp(list(pcb.footprints), manufacturer=manufacturer, config=pnp_config)

        pnp_path = output_dir / "cpl.csv"
        pnp_path.write_text(pnp_csv)

        print(f"\n3. CPL exported: {pnp_path}")
        print("\n   CPL preview:")
        for line in pnp_csv.strip().split("\n")[:6]:
            print(f"   {line}")

        return pnp_path

    except Exception as e:
        print(f"\n   Error exporting CPL: {e}")
        return None


def export_assembly_package(
    pcb_path: Path,
    schematic_path: Path,
    output_dir: Path,
    manufacturer: str = "jlcpcb",
) -> bool:
    """
    Export complete assembly package (Gerbers + BOM + CPL).

    This is the recommended one-liner approach for simple projects.

    Args:
        pcb_path: Path to the routed PCB file
        schematic_path: Path to the schematic file
        output_dir: Directory for output files
        manufacturer: Target manufacturer

    Returns:
        True if all exports succeeded
    """
    from kicad_tools.export import AssemblyPackage

    print("\n" + "=" * 60)
    print("Quick Export: Complete Assembly Package")
    print("=" * 60)

    print(f"\n   PCB: {pcb_path.name}")
    print(f"   Schematic: {schematic_path.name}")
    print(f"   Manufacturer: {manufacturer}")
    print(f"   Output: {output_dir}")

    try:
        pkg = AssemblyPackage.create(
            pcb=pcb_path,
            schematic=schematic_path,
            manufacturer=manufacturer,
            output_dir=output_dir,
        )
        result = pkg.export()

        print("\n   Assembly package created:")
        print(result)

        if result.success:
            print("\n   All files ready for upload!")
            return True
        else:
            print(f"\n   Warnings: {result.errors}")
            return False

    except Exception as e:
        print(f"\n   Error creating assembly package: {e}")
        return False


def show_manufacturer_info(manufacturer: str = "jlcpcb") -> None:
    """Show information about the target manufacturer."""
    from kicad_tools.manufacturers import get_profile

    print("\n" + "=" * 60)
    print(f"Manufacturer Profile: {manufacturer.upper()}")
    print("=" * 60)

    try:
        profile = get_profile(manufacturer)
        rules = profile.get_design_rules(layers=2)

        print(f"\n   Name: {profile.name}")
        print(f"   Website: {profile.website}")
        print(f"   Assembly: {'Yes' if profile.supports_assembly() else 'No'}")

        print("\n   Design Rules (2-layer):")
        print(f"   - Min trace: {rules.min_trace_width_mm}mm")
        print(f"   - Min clearance: {rules.min_clearance_mm}mm")
        print(f"   - Min via drill: {rules.min_via_drill_mm}mm")
        print(f"   - Min via diameter: {rules.min_via_diameter_mm}mm")

    except Exception as e:
        print(f"\n   Error loading profile: {e}")


def main() -> int:
    """Main entry point."""
    # Determine file paths
    project_dir = Path(__file__).parent
    output_dir = project_dir / "output"

    if len(sys.argv) > 1:
        pcb_path = Path(sys.argv[1])
        schematic_path = pcb_path.with_name(pcb_path.stem.replace("_routed", "") + ".kicad_sch")
    else:
        pcb_path = output_dir / "led_indicator_routed.kicad_pcb"
        schematic_path = output_dir / "led_indicator.kicad_sch"

    # Check if files exist
    if not pcb_path.exists():
        print(f"Error: PCB file not found: {pcb_path}")
        print("\nRun generate_design.py first to create the PCB.")
        return 1

    manufacturer = "jlcpcb"
    mfg_output_dir = output_dir / manufacturer

    print("=" * 60)
    print("Manufacturing Export Example")
    print("=" * 60)
    print("\nSource files:")
    print(f"  PCB: {pcb_path}")
    print(f"  Schematic: {schematic_path}")
    print(f"\nTarget: {manufacturer.upper()}")
    print(f"Output: {mfg_output_dir}")

    # Show manufacturer info
    show_manufacturer_info(manufacturer)

    # Method 1: Quick export with AssemblyPackage (recommended)
    print("\n" + "-" * 60)
    print("METHOD 1: Quick Export (AssemblyPackage)")
    print("-" * 60)

    if schematic_path.exists():
        export_assembly_package(
            pcb_path,
            schematic_path,
            mfg_output_dir / "quick_export",
            manufacturer,
        )

    # Method 2: Individual exports (for more control)
    print("\n" + "-" * 60)
    print("METHOD 2: Individual Exports")
    print("-" * 60)

    individual_dir = mfg_output_dir / "individual"
    individual_dir.mkdir(parents=True, exist_ok=True)

    gerber_path = export_gerbers(pcb_path, individual_dir, manufacturer)
    bom_path = None
    pnp_path = None

    if schematic_path.exists():
        bom_path = export_bom(schematic_path, individual_dir, manufacturer)

    pnp_path = export_pnp(pcb_path, individual_dir, manufacturer)

    # Summary
    print("\n" + "=" * 60)
    print("MANUFACTURING EXPORT SUMMARY")
    print("=" * 60)

    print(f"\nOutput directory: {mfg_output_dir}")

    print("\nGenerated files:")
    if gerber_path:
        print(f"  Gerbers: {gerber_path}")
    if bom_path:
        print(f"  BOM: {bom_path}")
    if pnp_path:
        print(f"  CPL: {pnp_path}")

    print("\nNext steps:")
    print(f"  1. Go to {manufacturer}.com")
    print("  2. Upload gerbers.zip")
    print("  3. If assembly needed, upload bom.csv and cpl.csv")
    print("  4. Review quote and place order")

    print("\nCLI alternative:")
    print(f"  kct export --manufacturer {manufacturer} \\")
    print(f"      {pcb_path} {schematic_path} \\")
    print(f"      -o {mfg_output_dir}")

    success = gerber_path is not None
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
