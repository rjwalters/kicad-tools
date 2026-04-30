"""IPC command handlers for live KiCad instance interaction."""

from __future__ import annotations

__all__ = ["run_ipc_command"]


def run_ipc_command(args) -> int:
    """Handle ipc command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success)
    """
    ipc_subcommand = getattr(args, "ipc_command", None)

    if ipc_subcommand == "status":
        return _run_status(args)
    elif ipc_subcommand == "connect":
        return _run_connect(args)
    elif ipc_subcommand == "push-routes":
        return _run_push_routes(args)
    else:
        print("Usage: kct ipc <command> [OPTIONS]")
        print()
        print("Commands:")
        print("  status       Show KiCad IPC connection status")
        print("  connect      Test connection to a running KiCad instance")
        print("  push-routes  Push routed tracks from a PCB file to KiCad")
        return 0


def _run_status(args) -> int:
    """Report KiCad IPC connection status.

    Discovers running KiCad instances and reports their status.
    """
    try:
        from kicad_tools.ipc.discovery import discover_instances, discover_socket
    except ImportError:
        print("Error: IPC dependencies not installed.")
        print("Install with: pip install 'kicad-tools[ipc]'")
        return 1

    socket_path = getattr(args, "socket", None)

    if socket_path:
        resolved = discover_socket(explicit_path=socket_path)
        if resolved:
            print(f"Socket found: {resolved}")
            return _try_connect_and_report(str(resolved))
        else:
            print(f"Socket not found at: {socket_path}")
            return 1

    # Auto-discover
    instances = discover_instances()
    if not instances:
        auto_socket = discover_socket()
        if auto_socket:
            print(f"Socket found: {auto_socket}")
            return _try_connect_and_report(str(auto_socket))
        print("No running KiCad instances found.")
        print()
        print("To start KiCad with IPC enabled:")
        print("  kicad-cli api-server --socket /tmp/kicad/kicad.sock")
        print()
        print("Or specify a socket path:")
        print("  kct ipc status --socket /path/to/socket")
        return 1

    print(f"Found {len(instances)} KiCad instance(s):")
    for inst in instances:
        print(f"  {inst}")
    print()

    # Try connecting to the first instance
    return _try_connect_and_report(str(instances[0].socket_path))


def _try_connect_and_report(socket_path: str) -> int:
    """Try connecting to KiCad and report status."""
    try:
        from kicad_tools.ipc.client import IPCClient, IPCError
    except ImportError:
        print("Error: pynng not installed.")
        print("Install with: pip install 'kicad-tools[ipc]'")
        return 1

    print(f"Connecting to {socket_path}...")
    try:
        with IPCClient(socket_path) as client:
            if client.ping():
                version = client.get_version()
                print(f"Connected to KiCad {version}")
                docs = client.get_open_documents()
                if docs:
                    print(f"Open documents: {len(docs)}")
                    for doc in docs:
                        print(f"  {doc.get('path', 'unknown')}")
                return 0
            else:
                print("Connected but KiCad is not responding to health checks.")
                return 1
    except IPCError as exc:
        print(f"Connection failed: {exc}")
        return 1
    except ImportError:
        print("Error: pynng not installed.")
        print("Install with: pip install 'kicad-tools[ipc]'")
        return 1


def _run_connect(args) -> int:
    """Test connection to a running KiCad instance."""
    socket_path = getattr(args, "socket", None)

    try:
        from kicad_tools.ipc.client import IPCClient, IPCError
        from kicad_tools.ipc.discovery import discover_socket
    except ImportError:
        print("Error: IPC dependencies not installed.")
        print("Install with: pip install 'kicad-tools[ipc]'")
        return 1

    resolved = discover_socket(explicit_path=socket_path)
    if not resolved:
        print("No KiCad IPC socket found.")
        if socket_path:
            print(f"  Checked: {socket_path}")
        print("  Set KICAD_IPC_SOCKET or use --socket")
        return 1

    try:
        with IPCClient(str(resolved)) as client:
            version = client.get_version()
            print(f"Successfully connected to KiCad {version}")
            print(f"Socket: {resolved}")
            return 0
    except IPCError as exc:
        print(f"Connection failed: {exc}")
        return 1


def _run_push_routes(args) -> int:
    """Push routed tracks from a PCB file to a running KiCad instance."""
    pcb_path = getattr(args, "pcb", None)
    socket_path = getattr(args, "socket", None)
    net_filter = getattr(args, "net", None)
    dry_run = getattr(args, "dry_run", False)

    if not pcb_path:
        print("Error: PCB file path required.")
        print("Usage: kct ipc push-routes <pcb_file> [--socket PATH] [--net NAME]")
        return 1

    try:
        from kicad_tools.ipc.client import IPCClient, IPCError
        from kicad_tools.ipc.discovery import discover_socket
    except ImportError:
        print("Error: IPC dependencies not installed.")
        print("Install with: pip install 'kicad-tools[ipc]'")
        return 1

    from pathlib import Path

    pcb_file = Path(pcb_path)
    if not pcb_file.exists():
        print(f"Error: PCB file not found: {pcb_file}")
        return 1

    # Read tracks from the PCB file using existing kicad-tools parsing
    try:
        from kicad_tools.pcb.parser import parse_pcb
    except ImportError:
        print("Error: PCB parser not available.")
        return 1

    board = parse_pcb(pcb_file)
    tracks = board.tracks if hasattr(board, "tracks") else []
    vias = board.vias if hasattr(board, "vias") else []

    if net_filter:
        # Filter to specific net
        net_code = None
        for net in board.nets if hasattr(board, "nets") else []:
            if hasattr(net, "name") and net.name == net_filter:
                net_code = net.number if hasattr(net, "number") else None
                break
        if net_code is not None:
            tracks = [t for t in tracks if hasattr(t, "net") and t.net == net_code]
            vias = [v for v in vias if hasattr(v, "net") and v.net == net_code]

    print(f"Found {len(tracks)} tracks and {len(vias)} vias in {pcb_file.name}")

    if net_filter:
        print(f"  Filtered to net: {net_filter}")

    if dry_run:
        print("Dry run -- no changes pushed to KiCad.")
        return 0

    if not tracks and not vias:
        print("No tracks or vias to push.")
        return 0

    resolved = discover_socket(explicit_path=socket_path)
    if not resolved:
        print("No KiCad IPC socket found.")
        return 1

    try:
        from kicad_tools.ipc.board import BoardOperations
        from kicad_tools.ipc.proto.messages import TrackSegment as IPCTrack
        from kicad_tools.ipc.proto.messages import Vector2
        from kicad_tools.ipc.proto.messages import Via as IPCVia

        with IPCClient(str(resolved)) as client:
            board_ops = BoardOperations(client)

            # Convert kicad-tools track format to IPC format
            ipc_tracks = []
            for t in tracks:
                ipc_tracks.append(
                    IPCTrack(
                        start=Vector2(
                            x_nm=int(getattr(t, "start_x", 0) * 1e6),
                            y_nm=int(getattr(t, "start_y", 0) * 1e6),
                        ),
                        end=Vector2(
                            x_nm=int(getattr(t, "end_x", 0) * 1e6),
                            y_nm=int(getattr(t, "end_y", 0) * 1e6),
                        ),
                        width_nm=int(getattr(t, "width", 0.25) * 1e6),
                        layer=getattr(t, "layer", "F.Cu"),
                        net=getattr(t, "net", 0),
                    )
                )

            ipc_vias = []
            for v in vias:
                ipc_vias.append(
                    IPCVia(
                        position=Vector2(
                            x_nm=int(getattr(v, "x", 0) * 1e6),
                            y_nm=int(getattr(v, "y", 0) * 1e6),
                        ),
                        diameter_nm=int(getattr(v, "size", 0.8) * 1e6),
                        drill_nm=int(getattr(v, "drill", 0.4) * 1e6),
                        net=getattr(v, "net", 0),
                    )
                )

            created = board_ops.push_routes(
                tracks=ipc_tracks,
                vias=ipc_vias,
                description=f"Push routes from {pcb_file.name}",
            )
            print(f"Pushed {len(created)} items to KiCad.")
            return 0

    except IPCError as exc:
        print(f"Error: {exc}")
        return 1
