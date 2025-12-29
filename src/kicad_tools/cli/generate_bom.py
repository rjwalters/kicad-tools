#!/usr/bin/env python3
"""
Generate Bill of Materials (BOM) from KiCad schematics.

Supports multiple output formats including JLCPCB and Seeed Fusion.
Can extract components directly from schematics or via kicad-cli netlist.

Usage:
    # Generate BOM from schematic (includes all sub-sheets)
    python3 scripts/kicad/generate-bom.py project.kicad_sch

    # Output as JLCPCB-compatible CSV
    python3 scripts/kicad/generate-bom.py project.kicad_sch --format jlcpcb -o bom.csv

    # Human-readable table
    python3 scripts/kicad/generate-bom.py project.kicad_sch --format table

    # Group by value and footprint
    python3 scripts/kicad/generate-bom.py project.kicad_sch --group

    # Use kicad-cli netlist (more accurate for complex projects)
    python3 scripts/kicad/generate-bom.py project.kicad_sch --use-netlist

    # Exclude power symbols
    python3 scripts/kicad/generate-bom.py project.kicad_sch --exclude-refs "#PWR,#FLG"
"""

import argparse
import csv
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

KICAD_SCRIPTS = Path(__file__).resolve().parent


# Try to import sexp parser for direct schematic reading
try:
    from kicad_tools.core.sexp import parse_sexp

    HAS_SEXP_PARSER = True
except ImportError:
    HAS_SEXP_PARSER = False


@dataclass
class Component:
    """Represents a BOM component."""

    reference: str
    value: str
    footprint: str
    description: str = ""
    manufacturer: str = ""
    mpn: str = ""  # Manufacturer Part Number
    lcsc: str = ""  # LCSC Part Number
    opl_sku: str = ""  # Seeed OPL SKU
    datasheet: str = ""
    sheet: str = ""  # Source sheet name
    lib_id: str = ""  # Library ID
    dnp: bool = False  # Do Not Place
    properties: dict = field(default_factory=dict)

    @property
    def quantity(self) -> int:
        """Get quantity from reference designators."""
        return len(self.reference_list)

    @property
    def reference_list(self) -> list[str]:
        """Parse reference designators."""
        return [r.strip() for r in self.reference.split(",")]

    @property
    def ref_prefix(self) -> str:
        """Extract reference prefix (e.g., 'R' from 'R1')."""
        match = re.match(r"^([A-Za-z_#]+)", self.reference)
        return match.group(1) if match else self.reference

    @property
    def ref_number(self) -> int:
        """Extract reference number for sorting."""
        match = re.search(r"(\d+)", self.reference)
        return int(match.group(1)) if match else 0

    def get_field(self, name: str) -> str:
        """Get a custom field value."""
        # Check direct attributes first
        if hasattr(self, name.lower()):
            return getattr(self, name.lower(), "")
        return self.properties.get(name, "")


@dataclass
class BOMLine:
    """Grouped BOM line (components with same value/footprint)."""

    components: list[Component] = field(default_factory=list)

    @property
    def quantity(self) -> int:
        return len(self.components)

    @property
    def designators(self) -> str:
        refs = []
        for c in self.components:
            refs.extend(c.reference_list)
        return ", ".join(
            sorted(
                refs,
                key=lambda x: (
                    re.match(r"([A-Za-z_#]+)", x).group(1) if re.match(r"([A-Za-z_#]+)", x) else "",
                    int(re.search(r"(\d+)", x).group(1)) if re.search(r"(\d+)", x) else 0,
                ),
            )
        )

    @property
    def value(self) -> str:
        return self.components[0].value if self.components else ""

    @property
    def footprint(self) -> str:
        return self.components[0].footprint if self.components else ""

    @property
    def mpn(self) -> str:
        return self.components[0].mpn if self.components else ""

    @property
    def lcsc(self) -> str:
        return self.components[0].lcsc if self.components else ""

    @property
    def manufacturer(self) -> str:
        return self.components[0].manufacturer if self.components else ""

    @property
    def opl_sku(self) -> str:
        return self.components[0].opl_sku if self.components else ""

    @property
    def description(self) -> str:
        return self.components[0].description if self.components else ""

    @property
    def datasheet(self) -> str:
        return self.components[0].datasheet if self.components else ""

    def get_field(self, name: str) -> str:
        """Get a custom field value from first component."""
        if self.components:
            return self.components[0].get_field(name)
        return ""


def find_kicad_cli() -> Optional[Path]:
    """Find kicad-cli executable."""
    # Check common locations on macOS
    locations = [
        "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
        "/usr/local/bin/kicad-cli",
        "/opt/homebrew/bin/kicad-cli",
    ]

    for loc in locations:
        if Path(loc).exists():
            return Path(loc)

    # Try PATH
    try:
        result = subprocess.run(["which", "kicad-cli"], capture_output=True, text=True)
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except Exception:
        pass

    return None


def extract_components_from_schematic(sch_path: Path, sheet_name: str = "") -> list[Component]:
    """Extract components directly from a schematic file using sexp parser."""
    if not HAS_SEXP_PARSER:
        raise RuntimeError("sexp parser not available - use --use-netlist instead")

    text = sch_path.read_text(encoding="utf-8")
    sexp = parse_sexp(text)

    if sexp.tag != "kicad_sch":
        raise ValueError(f"Not a schematic: {sch_path}")

    components = []

    # Find symbol instances
    for sym in sexp.find_all("symbol"):
        # Skip lib_symbols section (they don't have lib_id as first child)
        lib_id_node = sym.find("lib_id")
        if not lib_id_node:
            continue

        lib_id = lib_id_node.get_string(0) or ""

        # Extract properties
        props = {}
        reference = ""
        value = ""
        footprint = ""
        datasheet = ""
        description = ""

        for prop in sym.find_all("property"):
            prop_name = prop.get_string(0) or ""
            prop_value = prop.get_string(1) or ""

            if prop_name == "Reference":
                reference = prop_value
            elif prop_name == "Value":
                value = prop_value
            elif prop_name == "Footprint":
                footprint = prop_value
            elif prop_name == "Datasheet":
                datasheet = prop_value
            elif prop_name == "Description":
                description = prop_value
            elif not prop_name.startswith("ki_"):
                # Store custom properties (LCSC, MPN, etc.)
                props[prop_name] = prop_value

        if reference:
            components.append(
                Component(
                    reference=reference,
                    value=value,
                    footprint=footprint,
                    datasheet=datasheet,
                    description=description,
                    sheet=sheet_name or sch_path.stem,
                    lib_id=lib_id,
                    mpn=props.get("MPN", props.get("Manufacturer_PN", "")),
                    manufacturer=props.get("Manufacturer", props.get("MFR", "")),
                    lcsc=props.get("LCSC", props.get("LCSC Part #", "")),
                    opl_sku=props.get("OPL", props.get("OPL_SKU", "")),
                    dnp=props.get("DNP", "").lower() in ("yes", "true", "1", "dnp"),
                    properties=props,
                )
            )

    return components


def extract_components_hierarchical(main_sch: Path) -> list[Component]:
    """Extract components from main schematic and all sub-sheets."""
    if not HAS_SEXP_PARSER:
        raise RuntimeError("sexp parser not available - use --use-netlist instead")

    all_components = []
    processed = set()

    def process_schematic(sch_path: Path, sheet_name: str = ""):
        if sch_path in processed:
            return
        processed.add(sch_path)

        if not sch_path.exists():
            print(f"Warning: Schematic not found: {sch_path}", file=sys.stderr)
            return

        # Extract components from this sheet
        components = extract_components_from_schematic(sch_path, sheet_name)
        all_components.extend(components)

        # Find sub-sheets
        text = sch_path.read_text(encoding="utf-8")
        sexp = parse_sexp(text)

        for sheet in sexp.find_all("sheet"):
            # Get sheet file property
            for prop in sheet.find_all("property"):
                prop_name = prop.get_string(0) or ""
                if prop_name == "Sheetfile":
                    sheet_file = prop.get_string(1) or ""
                    if sheet_file:
                        sub_path = sch_path.parent / sheet_file
                        # Get sheet name
                        sub_name = ""
                        for p in sheet.find_all("property"):
                            if p.get_string(0) == "Sheetname":
                                sub_name = p.get_string(1) or ""
                                break
                        process_schematic(sub_path, sub_name or sheet_file)

    process_schematic(main_sch)
    return all_components


def filter_components(
    components: list[Component],
    exclude_refs: list[str] = None,
    exclude_values: list[str] = None,
    include_refs: list[str] = None,
) -> list[Component]:
    """Filter components based on criteria."""
    filtered = []

    # Default exclusions
    if exclude_refs is None:
        exclude_refs = ["#PWR", "#FLG", "#SYM"]

    for comp in components:
        # Skip DNP
        if comp.dnp:
            continue

        # Check exclusions
        excluded = False

        for prefix in exclude_refs:
            if comp.reference.startswith(prefix):
                excluded = True
                break

        if exclude_values:
            for val in exclude_values:
                if val.lower() in comp.value.lower():
                    excluded = True
                    break

        if include_refs:
            included = False
            for prefix in include_refs:
                if comp.reference.startswith(prefix):
                    included = True
                    break
            if not included:
                excluded = True

        if not excluded:
            filtered.append(comp)

    return filtered


def extract_bom_from_netlist(netlist_path: Path) -> list[Component]:
    """Extract component info from KiCad netlist XML."""
    import xml.etree.ElementTree as ET

    components = []
    tree = ET.parse(netlist_path)
    root = tree.getroot()

    for comp in root.findall(".//comp"):
        ref = comp.get("ref", "")
        value = comp.findtext("value", "")
        footprint = comp.findtext("footprint", "")

        # Get fields
        fields = {}
        for fld in comp.findall(".//field"):
            name = fld.get("name", "")
            fields[name.lower()] = fld.text or ""

        component = Component(
            reference=ref,
            value=value,
            footprint=footprint,
            description=fields.get("description", ""),
            manufacturer=fields.get("manufacturer", fields.get("mfr", "")),
            mpn=fields.get("mpn", fields.get("mfr_pn", fields.get("manufacturer_pn", ""))),
            opl_sku=fields.get("opl_sku", fields.get("seeed_opl", fields.get("opl", ""))),
            dnp=fields.get("dnp", "").lower() in ("yes", "true", "1", "dnp"),
        )
        components.append(component)

    return components


def group_components(components: list[Component]) -> list[BOMLine]:
    """Group components by value and footprint."""
    groups: dict[tuple, BOMLine] = {}

    for comp in components:
        if comp.dnp:
            continue  # Skip DNP components

        key = (comp.value, comp.footprint, comp.mpn)
        if key not in groups:
            groups[key] = BOMLine()
        groups[key].components.append(comp)

    return list(groups.values())


def export_seeed_format(bom_lines: list[BOMLine], output_path: Path):
    """Export BOM in Seeed Fusion format."""
    # Seeed requires: Part Number, Description, Quantity, Designators, Footprint, Remarks
    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "Part Number",
                "Description",
                "Quantity",
                "Designators",
                "Footprint",
                "OPL SKU",
                "Remarks",
            ]
        )

        for line in sorted(bom_lines, key=lambda x: x.designators):
            remarks = ""
            if line.opl_sku:
                remarks = "OPL"
            elif line.mpn:
                remarks = "Customer Supplied" if not line.opl_sku else ""

            writer.writerow(
                [
                    line.mpn or line.value,
                    line.description or line.value,
                    line.quantity,
                    line.designators,
                    line.footprint,
                    line.opl_sku,
                    remarks,
                ]
            )

    print(f"Seeed BOM written to: {output_path}")


def export_jlcpcb_format(bom_lines: list[BOMLine], output_path: Path):
    """Export BOM in JLCPCB format."""
    # JLCPCB requires: Comment, Designator, Footprint, LCSC Part Number
    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])

        for line in sorted(bom_lines, key=lambda x: x.designators):
            # Use lcsc field, fall back to opl_sku
            lcsc_part = line.lcsc or line.opl_sku
            writer.writerow(
                [
                    line.value,
                    line.designators,
                    line.footprint,
                    lcsc_part,
                ]
            )

    print(f"JLCPCB BOM written to: {output_path}")


def export_generic_csv(bom_lines: list[BOMLine], output_path: Path):
    """Export generic CSV BOM."""
    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "Qty",
                "Value",
                "Designators",
                "Footprint",
                "Manufacturer",
                "MPN",
                "Description",
            ]
        )

        for line in sorted(bom_lines, key=lambda x: x.designators):
            writer.writerow(
                [
                    line.quantity,
                    line.value,
                    line.designators,
                    line.footprint,
                    line.manufacturer,
                    line.mpn,
                    line.description,
                ]
            )

    print(f"Generic BOM written to: {output_path}")


def format_table(bom_lines: list[BOMLine], show_lcsc: bool = True) -> str:
    """Format BOM as human-readable table."""
    fields = ["Designators", "Qty", "Value", "Footprint"]
    if show_lcsc:
        fields.append("LCSC")

    # Build rows
    rows = []
    for line in sorted(bom_lines, key=lambda x: x.designators):
        row = {
            "Designators": line.designators[:40] + ("..." if len(line.designators) > 40 else ""),
            "Qty": str(line.quantity),
            "Value": line.value[:20],
            "Footprint": line.footprint.split(":")[-1][:25] if line.footprint else "",
            "LCSC": line.lcsc or line.opl_sku or "",
        }
        rows.append(row)

    # Calculate column widths
    widths = {f: len(f) for f in fields}
    for row in rows:
        for f in fields:
            widths[f] = max(widths[f], len(row.get(f, "")))

    # Format header
    header = " | ".join(f.ljust(widths[f]) for f in fields)
    separator = "-+-".join("-" * widths[f] for f in fields)

    # Format rows
    output = [header, separator]
    for row in rows:
        line = " | ".join(row.get(f, "").ljust(widths[f]) for f in fields)
        output.append(line)

    # Add summary
    total_qty = sum(line.quantity for line in bom_lines)
    lcsc_count = sum(1 for line in bom_lines if line.lcsc or line.opl_sku)
    output.append(separator)
    output.append(
        f"Total: {len(bom_lines)} unique parts, {total_qty} components, {lcsc_count} with LCSC"
    )

    return "\n".join(output)


def format_json(bom_lines: list[BOMLine]) -> str:
    """Format BOM as JSON."""
    data = {
        "bom": [],
        "summary": {
            "unique_parts": len(bom_lines),
            "total_components": sum(line.quantity for line in bom_lines),
            "with_lcsc": sum(1 for line in bom_lines if line.lcsc or line.opl_sku),
        },
    }

    for line in sorted(bom_lines, key=lambda x: x.designators):
        entry = {
            "references": line.designators.split(", "),
            "quantity": line.quantity,
            "value": line.value,
            "footprint": line.footprint,
        }
        if line.description:
            entry["description"] = line.description
        if line.mpn:
            entry["mpn"] = line.mpn
        if line.manufacturer:
            entry["manufacturer"] = line.manufacturer
        if line.lcsc:
            entry["lcsc"] = line.lcsc
        elif line.opl_sku:
            entry["lcsc"] = line.opl_sku
        if line.datasheet:
            entry["datasheet"] = line.datasheet
        data["bom"].append(entry)

    return json.dumps(data, indent=2)


def generate_bom_from_schematic(schematic_path: Path, output_dir: Path, format: str = "seeed"):
    """Generate BOM from KiCad schematic."""
    kicad_cli = find_kicad_cli()

    if kicad_cli:
        # Use kicad-cli to generate netlist
        netlist_path = output_dir / "temp_netlist.xml"
        print("Generating netlist with kicad-cli...")
        try:
            subprocess.run(
                [
                    str(kicad_cli),
                    "sch",
                    "export",
                    "netlist",
                    "--output",
                    str(netlist_path),
                    str(schematic_path),
                ],
                check=True,
            )
            components = extract_bom_from_netlist(netlist_path)
            netlist_path.unlink()  # Clean up
        except subprocess.CalledProcessError as e:
            print(f"Error generating netlist: {e}")
            print("Falling back to placeholder BOM...")
            components = generate_placeholder_bom()
    else:
        print("kicad-cli not found. Install KiCad 8 to generate real BOM.")
        print("Generating placeholder BOM from engineering plan...")
        components = generate_placeholder_bom()

    bom_lines = group_components(components)

    # Generate output
    project_name = schematic_path.stem
    if format == "seeed":
        output_path = output_dir / f"{project_name}_bom_seeed.csv"
        export_seeed_format(bom_lines, output_path)
    elif format == "jlcpcb":
        output_path = output_dir / f"{project_name}_bom_jlcpcb.csv"
        export_jlcpcb_format(bom_lines, output_path)
    else:
        output_path = output_dir / f"{project_name}_bom.csv"
        export_generic_csv(bom_lines, output_path)

    # Summary
    total_parts = sum(line.quantity for line in bom_lines)
    unique_parts = len(bom_lines)
    opl_parts = len([bom_line for bom_line in bom_lines if bom_line.opl_sku])

    print("\nBOM Summary:")
    print(f"  Total parts:  {total_parts}")
    print(f"  Unique parts: {unique_parts}")
    print(
        f"  OPL parts:    {opl_parts} ({100 * opl_parts // unique_parts if unique_parts else 0}%)"
    )


def generate_placeholder_bom() -> list[Component]:
    """Generate placeholder BOM from engineering plan keystone components."""
    # This represents the expected BOM from the engineering plan
    components = [
        # Keystone components
        Component(
            reference="U1",
            value="ASTX-H11-24.576MHZ-T",
            footprint="Oscillator_SMD:Oscillator_SMD_Abracon_ASE-4Pin_3.2x2.5mm",
            description="24.576 MHz TCXO, 3.3V CMOS",
            manufacturer="Abracon",
            mpn="ASTX-H11-24.576MHZ-T",
            opl_sku="",  # To be verified
        ),
        Component(
            reference="U2",
            value="STM32C011F4P6",
            footprint="Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm",
            description="Cortex-M0+ 48MHz MCU, 16KB Flash",
            manufacturer="STMicroelectronics",
            mpn="STM32C011F4P6TR",
            opl_sku="",
        ),
        Component(
            reference="U3",
            value="PCM5122",
            footprint="Package_SO:TSSOP-28_4.4x9.7mm_P0.65mm",
            description="Stereo Audio DAC, 112dB SNR",
            manufacturer="Texas Instruments",
            mpn="PCM5122PWR",
            opl_sku="",
        ),
        # Power - LDOs
        Component(
            reference="U4",
            value="AMS1117-3.3",
            footprint="Package_TO_SOT_SMD:SOT-223-3_TabPin2",
            description="3.3V LDO, 1A, Digital",
            manufacturer="AMS",
            mpn="AMS1117-3.3",
            opl_sku="C6186",
        ),
        Component(
            reference="U5",
            value="AMS1117-3.3",
            footprint="Package_TO_SOT_SMD:SOT-223-3_TabPin2",
            description="3.3V LDO, 1A, Analog",
            manufacturer="AMS",
            mpn="AMS1117-3.3",
            opl_sku="C6186",
        ),
        # Clock damping resistors
        Component(
            reference="R1",
            value="33R",
            footprint="Resistor_SMD:R_0402_1005Metric",
            description="TCXO→DAC damping",
            manufacturer="",
            mpn="",
            opl_sku="C25104",
        ),
        Component(
            reference="R2",
            value="33R",
            footprint="Resistor_SMD:R_0402_1005Metric",
            description="TCXO→MCU damping",
            manufacturer="",
            mpn="",
            opl_sku="C25104",
        ),
        # Decoupling capacitors (example subset)
        Component(
            reference="C1,C2,C3,C4",
            value="100nF",
            footprint="Capacitor_SMD:C_0402_1005Metric",
            description="Decoupling",
            manufacturer="",
            mpn="",
            opl_sku="C1525",
        ),
        Component(
            reference="C5,C6",
            value="10uF",
            footprint="Capacitor_SMD:C_0805_2012Metric",
            description="Bulk decoupling",
            manufacturer="",
            mpn="",
            opl_sku="C15850",
        ),
        # Connectors
        Component(
            reference="J1",
            value="Raspberry_Pi_40pin",
            footprint="Connector_PinHeader_2.54mm:PinHeader_2x20_P2.54mm_Vertical",
            description="40-pin HAT header",
            manufacturer="",
            mpn="",
            opl_sku="",
        ),
        Component(
            reference="J2",
            value="AudioJack3_StereoSwitch",
            footprint="Connector_Audio:Jack_3.5mm_Ledino_1117_Horizontal",
            description="3.5mm TRS audio jack",
            manufacturer="",
            mpn="",
            opl_sku="",
        ),
        Component(
            reference="J3",
            value="Tag-Connect_TC2030",
            footprint="Connector:Tag-Connect_TC2030-IDC-NL_2x03_P1.27mm_Vertical",
            description="SWD debug connector",
            manufacturer="Tag-Connect",
            mpn="TC2030-IDC-NL",
            opl_sku="",
        ),
    ]

    return components


def main():
    parser = argparse.ArgumentParser(
        description="Generate Bill of Materials from KiCad schematics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", type=Path, help="Path to KiCad schematic (.kicad_sch)")

    # Output options
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output file (default: stdout for table/json, auto-named for csv)",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=["table", "csv", "jlcpcb", "seeed", "json"],
        default="table",
        help="Output format (default: table)",
    )

    # Extraction options
    parser.add_argument(
        "--use-netlist",
        action="store_true",
        help="Use kicad-cli netlist export (more accurate for complex projects)",
    )
    parser.add_argument(
        "--single-sheet",
        action="store_true",
        help="Only process the specified schematic, not sub-sheets",
    )

    # Filter options
    parser.add_argument(
        "--exclude-refs",
        type=str,
        default="#PWR,#FLG,#SYM",
        help="Reference prefixes to exclude (comma-separated)",
    )
    parser.add_argument("--exclude-values", type=str, help="Values to exclude (comma-separated)")
    parser.add_argument(
        "--include-refs", type=str, help="Only include these reference prefixes (comma-separated)"
    )

    # Grouping
    parser.add_argument(
        "--no-group", action="store_true", help="Don't group components (list each individually)"
    )

    args = parser.parse_args()

    # Validate input
    if not args.schematic.exists():
        print(f"Error: Schematic not found: {args.schematic}", file=sys.stderr)
        return 1

    if args.schematic.suffix != ".kicad_sch":
        print(f"Error: Not a schematic file: {args.schematic}", file=sys.stderr)
        return 1

    # Extract components
    components = []

    if args.use_netlist:
        # Use kicad-cli netlist export
        kicad_cli = find_kicad_cli()
        if not kicad_cli:
            print(
                "Error: kicad-cli not found. Install KiCad 8 or omit --use-netlist", file=sys.stderr
            )
            return 1

        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as f:
            netlist_path = Path(f.name)

        try:
            subprocess.run(
                [
                    str(kicad_cli),
                    "sch",
                    "export",
                    "netlist",
                    "--output",
                    str(netlist_path),
                    str(args.schematic),
                ],
                check=True,
                capture_output=True,
            )
            components = extract_bom_from_netlist(netlist_path)
        except subprocess.CalledProcessError as e:
            print(
                f"Error generating netlist: {e.stderr.decode() if e.stderr else e}", file=sys.stderr
            )
            return 1
        finally:
            netlist_path.unlink(missing_ok=True)
    else:
        # Direct schematic parsing
        if not HAS_SEXP_PARSER:
            print("Error: sexp parser not available. Use --use-netlist instead.", file=sys.stderr)
            return 1

        try:
            if args.single_sheet:
                components = extract_components_from_schematic(args.schematic)
            else:
                components = extract_components_hierarchical(args.schematic)
        except Exception as e:
            print(f"Error reading schematic: {e}", file=sys.stderr)
            return 1

    if not components:
        print("No components found", file=sys.stderr)
        return 1

    # Filter components
    exclude_refs = args.exclude_refs.split(",") if args.exclude_refs else []
    exclude_values = args.exclude_values.split(",") if args.exclude_values else None
    include_refs = args.include_refs.split(",") if args.include_refs else None

    components = filter_components(
        components,
        exclude_refs=exclude_refs,
        exclude_values=exclude_values,
        include_refs=include_refs,
    )

    if not components:
        print("No components remaining after filtering", file=sys.stderr)
        return 1

    # Group components
    if args.no_group:
        # Create BOMLine per component
        bom_lines = [BOMLine(components=[c]) for c in components]
    else:
        bom_lines = group_components(components)

    # Generate output
    if args.format == "table":
        output = format_table(bom_lines)
        if args.output:
            args.output.write_text(output)
            print(f"Wrote BOM to: {args.output}")
        else:
            print(output)

    elif args.format == "json":
        output = format_json(bom_lines)
        if args.output:
            args.output.write_text(output)
            print(f"Wrote BOM to: {args.output}")
        else:
            print(output)

    elif args.format == "csv":
        output_path = args.output or (args.schematic.parent / f"{args.schematic.stem}_bom.csv")
        export_generic_csv(bom_lines, output_path)

    elif args.format == "jlcpcb":
        output_path = args.output or (
            args.schematic.parent / f"{args.schematic.stem}_bom_jlcpcb.csv"
        )
        export_jlcpcb_format(bom_lines, output_path)

    elif args.format == "seeed":
        output_path = args.output or (
            args.schematic.parent / f"{args.schematic.stem}_bom_seeed.csv"
        )
        export_seeed_format(bom_lines, output_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
