#!/usr/bin/env python3
"""
Example: End-to-End Manufacturing Export

Demonstrates how to generate complete manufacturing packages for PCB assembly
services like JLCPCB, PCBWay, and OSH Park using the kicad-tools Python API.

This example shows:
1. Quick one-liner export using AssemblyPackage
2. Individual exporter usage for fine-grained control
3. Manufacturer profile comparison
4. How LCSC part numbers flow into the BOM

Usage:
    python export_manufacturing.py [pcb_file] [schematic_file]

If no files specified, uses the voltage divider board from ../boards/.
"""

from __future__ import annotations

import sys
from pathlib import Path


def example_quick_export(pcb_path: Path, schematic_path: Path, output_dir: Path) -> None:
    """
    Quick one-liner export using AssemblyPackage.

    This is the simplest way to generate manufacturing files.
    """
    from kicad_tools.export import AssemblyPackage

    print("=" * 70)
    print("EXAMPLE 1: Quick Export with AssemblyPackage")
    print("=" * 70)

    # One-liner: Create complete assembly package for JLCPCB
    pkg = AssemblyPackage.create(
        pcb=pcb_path,
        schematic=schematic_path,
        manufacturer="jlcpcb",
        output_dir=output_dir / "jlcpcb",
    )
    result = pkg.export()

    print("\nAssembly package created:")
    print(result)

    if result.success:
        print("\nGenerated files ready for upload to JLCPCB!")
    else:
        print(f"\nWarnings: {result.errors}")


def example_individual_exporters(pcb_path: Path, schematic_path: Path, output_dir: Path) -> None:
    """
    Use individual exporters for fine-grained control.

    This approach lets you customize each export step.
    """
    from kicad_tools.export import (
        BOMExportConfig,
        GerberExporter,
        PnPExportConfig,
        export_bom,
        export_pnp,
    )
    from kicad_tools.schema.bom import extract_bom
    from kicad_tools.schema.pcb import PCB

    print("\n" + "=" * 70)
    print("EXAMPLE 2: Individual Exporters")
    print("=" * 70)

    # --- Gerber Export ---
    print("\n--- Gerber Export ---")
    exporter = GerberExporter(pcb_path)

    # Export with manufacturer preset
    gerber_dir = output_dir / "gerbers_custom"
    gerber_path = exporter.export_for_manufacturer("jlcpcb", gerber_dir)
    print(f"Gerbers exported to: {gerber_path}")

    # --- BOM Export ---
    print("\n--- BOM Export ---")

    # Extract BOM from schematic
    bom = extract_bom(str(schematic_path))

    # Configure BOM export
    bom_config = BOMExportConfig(
        include_dnp=False,  # Exclude "Do Not Populate" items
        group_by_value=True,  # Group identical components
        include_lcsc=True,  # Include LCSC part numbers
        include_mfr=True,  # Include manufacturer info
    )

    # Export for JLCPCB
    bom_csv = export_bom(bom.items, manufacturer="jlcpcb", config=bom_config)
    bom_path = output_dir / "custom_bom.csv"
    bom_path.write_text(bom_csv)
    print(f"BOM exported to: {bom_path}")
    print("BOM preview (first 5 lines):")
    for line in bom_csv.strip().split("\n")[:5]:
        print(f"  {line}")

    # --- Pick-and-Place Export ---
    print("\n--- Pick-and-Place Export ---")

    # Load PCB for placement data
    pcb = PCB.load(str(pcb_path))

    # Configure PnP export
    pnp_config = PnPExportConfig(
        use_aux_origin=True,  # Use KiCad's auxiliary axis origin
        include_dnp=False,  # Exclude DNP components
    )

    # Export placement file
    pnp_csv = export_pnp(list(pcb.footprints), manufacturer="jlcpcb", config=pnp_config)
    pnp_path = output_dir / "custom_cpl.csv"
    pnp_path.write_text(pnp_csv)
    print(f"CPL exported to: {pnp_path}")
    print("CPL preview (first 5 lines):")
    for line in pnp_csv.strip().split("\n")[:5]:
        print(f"  {line}")


def example_manufacturer_comparison() -> None:
    """
    Compare manufacturer capabilities and design rules.

    Useful for deciding which manufacturer to use.
    """
    from kicad_tools.export import BOM_FORMATTERS, MANUFACTURER_PRESETS, PNP_FORMATTERS
    from kicad_tools.manufacturers import (
        compare_design_rules,
        find_compatible_manufacturers,
        get_manufacturer_ids,
        get_profile,
    )

    print("\n" + "=" * 70)
    print("EXAMPLE 3: Manufacturer Comparison")
    print("=" * 70)

    # --- Available Manufacturers ---
    print("\n--- Available Manufacturer Profiles ---")
    for mfr_id in get_manufacturer_ids():
        profile = get_profile(mfr_id)
        print(f"  {mfr_id}: {profile.name}")
        print(f"    Website: {profile.website}")
        print(f"    Assembly: {'Yes' if profile.supports_assembly() else 'No'}")
        print(f"    Layers: {profile.supported_layers}")

    # --- Export Format Support ---
    print("\n--- Export Format Support ---")
    print("\nBOM formatters available:")
    for mfr_id in BOM_FORMATTERS:
        print(f"  - {mfr_id}")

    print("\nPick-and-Place formatters available:")
    for mfr_id in PNP_FORMATTERS:
        print(f"  - {mfr_id}")

    print("\nGerber presets available:")
    for mfr_id, preset in MANUFACTURER_PRESETS.items():
        print(f"  - {mfr_id}: {preset.name}")

    # --- Design Rules Comparison ---
    print("\n--- Design Rules Comparison (4-layer, 1oz copper) ---")
    rules = compare_design_rules(layers=4, copper_oz=1.0)
    print(f"{'Manufacturer':<12} {'Min Trace':<12} {'Min Clear':<12} {'Min Via':<10}")
    print("-" * 50)
    for mfr_id, rule in rules.items():
        print(
            f"{mfr_id:<12} "
            f"{rule.min_trace_width_mm:.3f}mm     "
            f"{rule.min_clearance_mm:.3f}mm     "
            f"{rule.min_via_drill_mm:.2f}mm"
        )

    # --- Find Compatible Manufacturers ---
    print("\n--- Find Compatible Manufacturers ---")
    print("For design with: 0.15mm traces, 0.15mm clearance, 0.3mm via")
    compatible = find_compatible_manufacturers(
        trace_width_mm=0.15,
        clearance_mm=0.15,
        via_drill_mm=0.3,
        layers=4,
        needs_assembly=True,
    )
    print(f"Compatible manufacturers: {[m.id for m in compatible]}")


def example_lcsc_parts() -> None:
    """
    How LCSC part numbers get into the BOM.

    LCSC part numbers are stored as component properties in KiCad.
    """
    print("\n" + "=" * 70)
    print("EXAMPLE 4: LCSC Part Number Workflow")
    print("=" * 70)

    print("""
LCSC part numbers flow from schematic symbols to BOM:

1. In KiCad Symbol Editor or Schematic:
   - Add a field named "LCSC" to your symbol
   - Set the value to the LCSC part number (e.g., "C123456")

2. Or use the kicad-tools parts importer:
   ```python
   from kicad_tools.parts import import_lcsc_part

   # Import a specific LCSC part
   import_lcsc_part("C123456", library="MyParts.kicad_sym")
   ```

3. When extracting BOM, LCSC numbers are automatically included:
   ```python
   from kicad_tools import extract_bom

   bom = extract_bom("design.kicad_sch")
   for item in bom.items:
       print(f"{item.reference}: LCSC={item.lcsc}")
   ```

4. JLCPCB BOM formatter uses the "LCSC Part #" column:
   ```csv
   Comment,Designator,Footprint,LCSC Part #
   100nF,C1,C_0603,C123456
   10k,R1,R_0603,C654321
   ```

5. For bulk assignment, use the parts lookup API:
   ```python
   from kicad_tools.parts import lookup_lcsc_by_value

   # Find LCSC parts matching component value
   results = lookup_lcsc_by_value("10k", footprint="0603")
   for part in results:
       print(f"{part.lcsc}: {part.description} - ${part.price}")
   ```
""")


def example_cli_commands() -> None:
    """
    CLI commands for manufacturing export.

    For users who prefer command-line workflows.
    """
    print("\n" + "=" * 70)
    print("EXAMPLE 5: CLI Commands")
    print("=" * 70)

    print("""
Manufacturing export is also available via the command line:

# Generate complete assembly package
kct export --manufacturer jlcpcb design.kicad_pcb design.kicad_sch -o output/

# Export Gerbers only
kct export gerbers design.kicad_pcb --manufacturer jlcpcb -o gerbers/

# Export BOM in specific format
kct bom design.kicad_sch --format jlcpcb -o bom_jlcpcb.csv

# Export pick-and-place file
kct export pnp design.kicad_pcb --manufacturer jlcpcb -o positions.csv

# Compare manufacturer capabilities
kct mfr compare --layers 4

# Check design against manufacturer rules
kct mfr check design.kicad_pcb --manufacturer jlcpcb

# Show manufacturer info
kct mfr info jlcpcb
""")


def main() -> int:
    """Main entry point."""
    # Find sample board files
    if len(sys.argv) > 2:
        pcb_path = Path(sys.argv[1])
        schematic_path = Path(sys.argv[2])
    else:
        # Use the voltage divider board from boards/
        boards_dir = Path(__file__).parent.parent.parent / "boards" / "01-voltage-divider"
        pcb_path = boards_dir / "output" / "voltage_divider.kicad_pcb"
        schematic_path = boards_dir / "output" / "voltage_divider.kicad_sch"

        if not pcb_path.exists():
            print("Note: Sample board files not found. Running examples that don't need files.")
            example_manufacturer_comparison()
            example_lcsc_parts()
            example_cli_commands()
            return 0

    # Create output directory
    output_dir = Path(__file__).parent / "output"
    output_dir.mkdir(exist_ok=True)

    print(f"PCB: {pcb_path}")
    print(f"Schematic: {schematic_path}")
    print(f"Output: {output_dir}")

    try:
        # Run all examples
        example_quick_export(pcb_path, schematic_path, output_dir)
        example_individual_exporters(pcb_path, schematic_path, output_dir)
        example_manufacturer_comparison()
        example_lcsc_parts()
        example_cli_commands()

        print("\n" + "=" * 70)
        print("SUCCESS: All manufacturing files generated!")
        print("=" * 70)
        print(f"\nOutput directory: {output_dir}")
        print("\nNext steps:")
        print("1. Review the generated files")
        print("2. Upload to your manufacturer's website")
        print("3. For JLCPCB: Upload gerbers.zip, bom.csv, and cpl.csv")

        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
