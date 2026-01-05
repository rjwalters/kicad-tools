"""MCP command handlers."""

import sys

__all__ = ["run_mcp_command"]


def run_mcp_command(args) -> int:
    """Handle mcp command and subcommands."""
    if not hasattr(args, "mcp_command") or not args.mcp_command:
        # No subcommand, show help
        from ..parser import create_parser

        parser = create_parser()
        parser.parse_args(["mcp", "--help"])
        return 0

    if args.mcp_command == "serve":
        return _run_serve(args)

    return 0


def _run_serve(args) -> int:
    """Run the MCP serve subcommand."""
    try:
        from kicad_tools.mcp.server import run_mcp
    except ImportError as e:
        print(
            "Error: MCP support requires the 'mcp' optional dependency.",
            file=sys.stderr,
        )
        print("Install with: pip install kicad-tools[mcp]", file=sys.stderr)
        print(f"\nDetails: {e}", file=sys.stderr)
        return 1

    try:
        run_mcp(
            http=getattr(args, "mcp_http", False),
            port=getattr(args, "mcp_port", 8000),
            debug=getattr(args, "mcp_debug", False),
        )
        return 0
    except KeyboardInterrupt:
        print("\nServer stopped.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
