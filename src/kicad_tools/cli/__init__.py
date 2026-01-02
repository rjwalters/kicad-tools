"""
Command-line interface tools for kicad-tools.

Provides CLI commands for common KiCad operations via the `kicad-tools` or `kct` command:

    kicad-tools symbols <schematic>    - List and query symbols
    kicad-tools nets <schematic>       - Trace and analyze nets
    kicad-tools erc <report>           - Parse ERC report
    kicad-tools drc <report>           - Parse DRC report
    kicad-tools bom <schematic>        - Generate bill of materials
    kicad-tools check <pcb>            - Pure Python DRC (no kicad-cli)
    kicad-tools validate --sync        - Check schematic-to-PCB netlist sync
    kicad-tools sch <command> <file>   - Schematic analysis tools
    kicad-tools pcb <command> <file>   - PCB query tools
    kicad-tools lib <command> <file>   - Symbol library tools
    kicad-tools footprint <command>    - Footprint generation tools
    kicad-tools mfr <command>          - Manufacturer tools
    kicad-tools parts <command>        - LCSC parts lookup and search
    kicad-tools datasheet <command>    - Datasheet search, download, and PDF parsing
    kicad-tools route <pcb>            - Autoroute a PCB
    kicad-tools zones <command>        - Add copper pour zones
    kicad-tools reason <pcb>           - LLM-driven PCB layout reasoning
    kicad-tools placement <command>    - Detect and fix placement conflicts
    kicad-tools optimize-traces <pcb>  - Optimize PCB traces
    kicad-tools validate-footprints    - Validate footprint pad spacing
    kicad-tools fix-footprints <pcb>   - Fix footprint pad spacing issues
    kicad-tools config                 - View/manage configuration
    kicad-tools interactive            - Launch interactive REPL mode

Schematic subcommands (kct sch <command>):
    summary      - Quick schematic overview
    hierarchy    - Show hierarchy tree
    labels       - List labels
    validate     - Run validation checks
    wires        - List wire segments and junctions
    info         - Show symbol details
    pins         - Show symbol pin positions
    connections  - Check pin connections using library positions
    unconnected  - Find unconnected pins and issues
    replace      - Replace a symbol's library ID

Examples:
    kct symbols design.kicad_sch --filter "U*"
    kct nets design.kicad_sch --net VCC
    kct erc design-erc.json --errors-only
    kct drc design-drc.rpt --mfr jlcpcb
    kct bom design.kicad_sch --format csv
    kct sch summary design.kicad_sch
    kct sch wires design.kicad_sch --stats
    kct sch info design.kicad_sch U1 --show-pins
    kct sch replace design.kicad_sch U1 "mylib:NewSymbol" --dry-run
    kct pcb summary board.kicad_pcb
    kct mfr compare
    kct parts lookup C123456
    kct parts search "100nF 0402" --in-stock
    kct datasheet convert STM32F103.pdf --output summary.md
    kct datasheet extract-images STM32F103.pdf --output images/
    kct route board.kicad_pcb --strategy negotiated
    kct reason board.kicad_pcb --export-state
    kct reason board.kicad_pcb --analyze
    kct validate-footprints board.kicad_pcb --min-pad-gap 0.15
    kct fix-footprints board.kicad_pcb --min-pad-gap 0.2 --dry-run
    kct footprint generate soic --pins 8 -o SOIC8.kicad_mod
    kct footprint generate chip --size 0402 --prefix R
    kct footprint generate qfp --pins 48 --pitch 0.5
    kct footprint generate sot --variant SOT-23
    kct footprint generate --list
    kct interactive
    kct interactive --project myboard.kicad_pro
"""

from __future__ import annotations

import argparse
import sys
import traceback

from kicad_tools import __version__
from kicad_tools.exceptions import KiCadToolsError

from .dispatch import dispatch_command
from .parsers import register_all_parsers

__all__ = [
    "main",
    "symbols_main",
    "nets_main",
    "erc_main",
    "drc_main",
    "bom_main",
    "format_error",
]


def format_error(e: Exception, verbose: bool = False) -> str:
    """
    Format an exception for user-friendly display.

    Args:
        e: The exception to format
        verbose: If True, include full stack trace

    Returns:
        Formatted error message string
    """
    if verbose:
        return traceback.format_exc()

    if isinstance(e, KiCadToolsError):
        return f"Error: {e}"

    # For other exceptions, show type and message
    return f"Error: {type(e).__name__}: {e}"


def main(argv: list[str] | None = None) -> int:
    """Main entry point for kicad-tools CLI."""
    parser = argparse.ArgumentParser(
        prog="kicad-tools",
        description="KiCad automation toolkit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=f"kicad-tools {__version__}")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show full stack traces on errors",
        dest="global_verbose",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output (for scripting)",
        dest="global_quiet",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Register all subparsers from the parsers module
    register_all_parsers(subparsers)

    # Handle footprint generate specially - it has its own subcommand parser
    if argv is None:
        argv = sys.argv[1:]
    if len(argv) >= 2 and argv[0] == "footprint" and argv[1] == "generate":
        from .footprint_generate import main as generate_main

        return generate_main(argv[2:]) or 0

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    # Get global verbose flag (use getattr for backwards compatibility)
    verbose = getattr(args, "global_verbose", False)

    try:
        return dispatch_command(args)
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        return 130
    except KiCadToolsError as e:
        print(format_error(e, verbose), file=sys.stderr)
        return 1
    except Exception as e:
        print(format_error(e, verbose), file=sys.stderr)
        return 1


# Legacy standalone entry points


def symbols_main() -> int:
    """Standalone entry point for kicad-symbols command."""
    from .symbols import main

    return main()


def nets_main() -> int:
    """Standalone entry point for kicad-nets command."""
    from .nets import main

    return main()


def erc_main() -> int:
    """Standalone entry point for kicad-erc command."""
    from .erc_cmd import main

    return main()


def drc_main() -> int:
    """Standalone entry point for kicad-drc command."""
    from .drc_cmd import main

    return main()


def bom_main() -> int:
    """Standalone entry point for kicad-bom command."""
    from .bom_cmd import main

    return main()


if __name__ == "__main__":
    sys.exit(main())
