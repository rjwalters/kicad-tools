"""
MCP server CLI command.

Provides the `kct mcp serve` command for starting the MCP server.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    """Main entry point for MCP CLI command.

    Args:
        argv: Command-line arguments. If None, uses sys.argv[1:].

    Returns:
        Exit code (0 for success, non-zero for errors).
    """
    parser = argparse.ArgumentParser(
        prog="kct mcp",
        description="MCP server for AI agent integration",
    )

    subparsers = parser.add_subparsers(dest="mcp_command", help="MCP commands")

    # serve subcommand
    serve_parser = subparsers.add_parser(
        "serve",
        help="Start the MCP server",
    )
    serve_parser.add_argument(
        "--http",
        action="store_true",
        help="Use HTTP transport instead of stdio",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for HTTP transport (default: 8000)",
    )
    serve_parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )

    if argv is None:
        argv = sys.argv[1:]

    args = parser.parse_args(argv)

    if not args.mcp_command:
        parser.print_help()
        return 0

    if args.mcp_command == "serve":
        return run_serve(args)

    return 0


def run_serve(args: argparse.Namespace) -> int:
    """Run the MCP serve command.

    Args:
        args: Parsed arguments from argparse.

    Returns:
        Exit code (0 for success, non-zero for errors).
    """
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
            http=args.http,
            port=args.port,
            debug=args.debug,
        )
        return 0
    except KeyboardInterrupt:
        print("\nServer stopped.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
