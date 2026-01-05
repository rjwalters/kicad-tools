"""MCP server command handlers."""

__all__ = ["run_mcp_command"]


def run_mcp_command(args) -> int:
    """Handle mcp command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success)
    """
    mcp_subcommand = getattr(args, "mcp_command", None)

    if mcp_subcommand == "serve":
        return _run_serve(args)
    else:
        # Default to showing help
        print("Usage: kct mcp serve [OPTIONS]")
        print()
        print("Commands:")
        print("  serve    Start the MCP server")
        return 0


def _run_serve(args) -> int:
    """Run the MCP server.

    Args:
        args: Parsed command line arguments with transport options

    Returns:
        Exit code (0 for success)
    """
    from kicad_tools.mcp.server import run_server

    transport = getattr(args, "transport", "stdio")
    host = getattr(args, "host", "localhost")
    port = getattr(args, "port", 8080)

    try:
        run_server(transport=transport, host=host, port=port)
        return 0
    except ImportError as e:
        print(f"Error: {e}")
        print()
        print("To use HTTP transport, install the MCP dependencies:")
        print("  pip install 'kicad-tools[mcp]'")
        return 1
    except KeyboardInterrupt:
        print("\nServer stopped.")
        return 0
    except Exception as e:
        print(f"Error starting server: {e}")
        return 1
