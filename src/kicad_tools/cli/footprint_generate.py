#!/usr/bin/env python3
"""
Footprint generation CLI commands.

Usage:
    kct footprint generate <type> [options]

Examples:
    # Generate SOIC footprint
    kct footprint generate soic --pins 8 --pitch 1.27 --output MyPart.kicad_mod

    # Generate QFP footprint
    kct footprint generate qfp --pins 48 --pitch 0.5 --output LQFP48.kicad_mod

    # Generate chip (passive) footprint
    kct footprint generate chip --size 0402 --output R_0402.kicad_mod

    # Generate SOT footprint
    kct footprint generate sot --variant SOT-23 --output SOT23.kicad_mod

    # Generate through-hole DIP
    kct footprint generate dip --pins 8 --pitch 2.54 --output DIP8.kicad_mod

    # List available generators
    kct footprint generate --list
"""

import argparse
import json
import sys
from pathlib import Path

from kicad_tools.library.generators import (
    create_chip,
    create_dip,
    create_pin_header,
    create_qfn,
    create_qfp,
    create_soic,
    create_sot,
)
from kicad_tools.library.generators.standards import CHIP_SIZES, SOT_STANDARDS
from kicad_tools.utils import ensure_parent_dir

# Generator info for --list and help
GENERATORS = {
    "soic": {
        "description": "SOIC (Small Outline Integrated Circuit) packages",
        "function": create_soic,
        "params": ["pins", "pitch", "body_width", "body_length"],
        "example": "kct footprint generate soic --pins 8",
    },
    "qfp": {
        "description": "LQFP/TQFP (Quad Flat Package) packages",
        "function": create_qfp,
        "params": ["pins", "pitch", "body_size"],
        "example": "kct footprint generate qfp --pins 48 --pitch 0.5",
    },
    "qfn": {
        "description": "QFN (Quad Flat No-lead) packages with optional exposed pad",
        "function": create_qfn,
        "params": ["pins", "pitch", "body_size", "exposed_pad"],
        "example": "kct footprint generate qfn --pins 16 --body-size 3.0 --exposed-pad 1.7",
    },
    "chip": {
        "description": "Chip passives (0201, 0402, 0603, 0805, 1206, etc.)",
        "function": create_chip,
        "params": ["size", "prefix"],
        "example": "kct footprint generate chip --size 0402 --prefix R",
        "sizes": list(CHIP_SIZES.keys()),
    },
    "sot": {
        "description": "SOT (Small Outline Transistor) packages",
        "function": create_sot,
        "params": ["variant"],
        "example": "kct footprint generate sot --variant SOT-23",
        "variants": list(SOT_STANDARDS.keys()),
    },
    "dip": {
        "description": "DIP (Dual In-line Package) through-hole packages",
        "function": create_dip,
        "params": ["pins", "pitch", "row_spacing"],
        "example": "kct footprint generate dip --pins 8",
    },
    "pin-header": {
        "description": "Pin header through-hole connectors",
        "function": create_pin_header,
        "params": ["pins", "rows", "pitch"],
        "example": "kct footprint generate pin-header --pins 10 --rows 2",
    },
}


def list_generators(output_format: str = "text") -> int:
    """List available footprint generators."""
    if output_format == "json":
        data = {
            name: {
                "description": info["description"],
                "parameters": info["params"],
                "example": info["example"],
                **({"sizes": info["sizes"]} if "sizes" in info else {}),
                **({"variants": info["variants"]} if "variants" in info else {}),
            }
            for name, info in GENERATORS.items()
        }
        print(json.dumps(data, indent=2))
    else:
        print("\nAvailable footprint generators:\n")
        for name, info in GENERATORS.items():
            print(f"  {name:<12} - {info['description']}")
            if "sizes" in info:
                print(f"               Sizes: {', '.join(info['sizes'])}")
            if "variants" in info:
                print(f"               Variants: {', '.join(info['variants'])}")
            print(f"               Example: {info['example']}")
            print()
    return 0


def generate_soic(args) -> int:
    """Generate SOIC footprint."""
    if args.pins is None:
        print("Error: --pins is required for SOIC", file=sys.stderr)
        return 1

    try:
        fp = create_soic(
            pins=args.pins,
            pitch=args.pitch,
            body_width=args.body_width,
            body_length=args.body_length,
            name=args.name,
        )
        return _output_footprint(fp, args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def generate_qfp(args) -> int:
    """Generate QFP/LQFP footprint."""
    if args.pins is None:
        print("Error: --pins is required for QFP", file=sys.stderr)
        return 1

    try:
        fp = create_qfp(
            pins=args.pins,
            pitch=args.pitch,
            body_size=args.body_size,
            name=args.name,
        )
        return _output_footprint(fp, args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def generate_qfn(args) -> int:
    """Generate QFN footprint."""
    if args.pins is None:
        print("Error: --pins is required for QFN", file=sys.stderr)
        return 1

    try:
        fp = create_qfn(
            pins=args.pins,
            pitch=args.pitch,
            body_size=args.body_size,
            exposed_pad=args.exposed_pad,
            name=args.name,
        )
        return _output_footprint(fp, args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def generate_chip(args) -> int:
    """Generate chip passive footprint."""
    if args.size is None:
        print("Error: --size is required for chip footprints", file=sys.stderr)
        print(f"Valid sizes: {', '.join(CHIP_SIZES.keys())}", file=sys.stderr)
        return 1

    try:
        fp = create_chip(
            size=args.size,
            prefix=args.prefix or "",
            metric=args.metric,
            name=args.name,
        )
        return _output_footprint(fp, args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def generate_sot(args) -> int:
    """Generate SOT footprint."""
    if args.variant is None:
        print("Error: --variant is required for SOT footprints", file=sys.stderr)
        print(f"Valid variants: {', '.join(SOT_STANDARDS.keys())}", file=sys.stderr)
        return 1

    try:
        fp = create_sot(
            variant=args.variant,
            name=args.name,
        )
        return _output_footprint(fp, args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def generate_dip(args) -> int:
    """Generate DIP footprint."""
    if args.pins is None:
        print("Error: --pins is required for DIP", file=sys.stderr)
        return 1

    try:
        fp = create_dip(
            pins=args.pins,
            pitch=args.pitch,
            row_spacing=args.row_spacing,
            name=args.name,
        )
        return _output_footprint(fp, args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def generate_pin_header(args) -> int:
    """Generate pin header footprint."""
    if args.pins is None:
        print("Error: --pins is required for pin-header", file=sys.stderr)
        return 1

    try:
        fp = create_pin_header(
            pins=args.pins,
            rows=args.rows or 1,
            pitch=args.pitch or 2.54,
            name=args.name,
        )
        return _output_footprint(fp, args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def _output_footprint(fp, args) -> int:
    """Output the generated footprint."""
    if args.json:
        # Output footprint data as JSON
        data = {
            "name": fp.name,
            "description": fp.description,
            "tags": fp.tags,
            "attr": fp.attr,
            "pads": [
                {
                    "name": p.name,
                    "x": p.x,
                    "y": p.y,
                    "width": p.width,
                    "height": p.height,
                    "shape": p.shape,
                    "type": p.pad_type,
                }
                for p in fp.pads
            ],
        }
        print(json.dumps(data, indent=2))
        return 0

    if args.output:
        # Write to file
        output_path = Path(args.output)

        # Ensure .kicad_mod extension
        if not output_path.suffix:
            output_path = output_path.with_suffix(".kicad_mod")

        ensure_parent_dir(output_path)

        try:
            fp.save(str(output_path))
            print(f"Saved: {output_path}")
            return 0
        except Exception as e:
            print(f"Error saving footprint: {e}", file=sys.stderr)
            return 1
    else:
        # Output to stdout
        print(fp.to_sexp())
        return 0


def main(argv: list[str] | None = None) -> int:
    """Main entry point for footprint generate command."""
    parser = argparse.ArgumentParser(
        prog="kct footprint generate",
        description="Generate parametric KiCad footprints",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # List option at top level
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available generators",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output footprint data as JSON (instead of .kicad_mod format)",
    )

    subparsers = parser.add_subparsers(dest="generator_type", help="Footprint type")

    # SOIC subcommand
    soic_parser = subparsers.add_parser("soic", help="Generate SOIC footprint")
    soic_parser.add_argument("--pins", type=int, required=True, help="Number of pins (4-32, even)")
    soic_parser.add_argument("--pitch", type=float, help="Pin pitch in mm (default: 1.27)")
    soic_parser.add_argument("--body-width", type=float, dest="body_width", help="Body width in mm")
    soic_parser.add_argument(
        "--body-length", type=float, dest="body_length", help="Body length in mm"
    )
    soic_parser.add_argument("--name", help="Custom footprint name")
    soic_parser.add_argument("-o", "--output", help="Output file path")
    soic_parser.add_argument("--json", action="store_true", help="Output as JSON")
    soic_parser.set_defaults(func=generate_soic)

    # QFP subcommand
    qfp_parser = subparsers.add_parser("qfp", help="Generate LQFP/TQFP footprint")
    qfp_parser.add_argument(
        "--pins", type=int, required=True, help="Number of pins (divisible by 4)"
    )
    qfp_parser.add_argument("--pitch", type=float, help="Pin pitch in mm (default: 0.5)")
    qfp_parser.add_argument(
        "--body-size", type=float, dest="body_size", help="Body size in mm (square)"
    )
    qfp_parser.add_argument("--name", help="Custom footprint name")
    qfp_parser.add_argument("-o", "--output", help="Output file path")
    qfp_parser.add_argument("--json", action="store_true", help="Output as JSON")
    qfp_parser.set_defaults(func=generate_qfp)

    # QFN subcommand
    qfn_parser = subparsers.add_parser("qfn", help="Generate QFN footprint")
    qfn_parser.add_argument(
        "--pins", type=int, required=True, help="Number of pins (divisible by 4)"
    )
    qfn_parser.add_argument("--pitch", type=float, help="Pin pitch in mm (default: 0.5)")
    qfn_parser.add_argument(
        "--body-size", type=float, dest="body_size", help="Body size in mm (square)"
    )
    qfn_parser.add_argument(
        "--exposed-pad", type=float, dest="exposed_pad", help="Exposed pad size in mm"
    )
    qfn_parser.add_argument("--name", help="Custom footprint name")
    qfn_parser.add_argument("-o", "--output", help="Output file path")
    qfn_parser.add_argument("--json", action="store_true", help="Output as JSON")
    qfn_parser.set_defaults(func=generate_qfn)

    # Chip subcommand
    chip_parser = subparsers.add_parser("chip", help="Generate chip passive footprint")
    chip_parser.add_argument(
        "--size",
        required=True,
        choices=list(CHIP_SIZES.keys()),
        help="Imperial size code (0201, 0402, 0603, 0805, 1206, etc.)",
    )
    chip_parser.add_argument("--prefix", help="Component prefix (R, C, L, etc.)")
    chip_parser.add_argument("--metric", action="store_true", help="Use metric naming convention")
    chip_parser.add_argument("--name", help="Custom footprint name")
    chip_parser.add_argument("-o", "--output", help="Output file path")
    chip_parser.add_argument("--json", action="store_true", help="Output as JSON")
    chip_parser.set_defaults(func=generate_chip)

    # SOT subcommand
    sot_parser = subparsers.add_parser("sot", help="Generate SOT footprint")
    sot_parser.add_argument(
        "--variant",
        required=True,
        choices=list(SOT_STANDARDS.keys()),
        help="SOT variant (SOT-23, SOT-23-5, SOT-23-6, SOT-223, SOT-89)",
    )
    sot_parser.add_argument("--name", help="Custom footprint name")
    sot_parser.add_argument("-o", "--output", help="Output file path")
    sot_parser.add_argument("--json", action="store_true", help="Output as JSON")
    sot_parser.set_defaults(func=generate_sot)

    # DIP subcommand
    dip_parser = subparsers.add_parser("dip", help="Generate DIP footprint")
    dip_parser.add_argument("--pins", type=int, required=True, help="Number of pins (even)")
    dip_parser.add_argument("--pitch", type=float, help="Pin pitch in mm (default: 2.54)")
    dip_parser.add_argument(
        "--row-spacing", type=float, dest="row_spacing", help="Row spacing in mm"
    )
    dip_parser.add_argument("--name", help="Custom footprint name")
    dip_parser.add_argument("-o", "--output", help="Output file path")
    dip_parser.add_argument("--json", action="store_true", help="Output as JSON")
    dip_parser.set_defaults(func=generate_dip)

    # Pin header subcommand
    header_parser = subparsers.add_parser("pin-header", help="Generate pin header footprint")
    header_parser.add_argument("--pins", type=int, required=True, help="Total number of pins")
    header_parser.add_argument("--rows", type=int, choices=[1, 2], help="Number of rows (1 or 2)")
    header_parser.add_argument("--pitch", type=float, help="Pin pitch in mm (default: 2.54)")
    header_parser.add_argument("--name", help="Custom footprint name")
    header_parser.add_argument("-o", "--output", help="Output file path")
    header_parser.add_argument("--json", action="store_true", help="Output as JSON")
    header_parser.set_defaults(func=generate_pin_header)

    args = parser.parse_args(argv)

    # Handle --list at top level
    if args.list:
        return list_generators("json" if args.json else "text")

    # No subcommand selected
    if not args.generator_type:
        parser.print_help()
        return 0

    # Call the appropriate generator function
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
