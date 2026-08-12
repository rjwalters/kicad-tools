"""IPC command handlers for live KiCad instance interaction.

Machine output (``--format json``, issue #4674): ``ipc status``,
``ipc connect`` and ``ipc push-routes`` each print exactly one JSON document
on stdout, keyed by ``command`` plus that command's own summary.  Failures
carry an ``error`` string with the exit code unchanged, so a caller can tell
"no KiCad running" from "IPC extra not installed" without scraping prose.
See ``docs/reference/machine-output.md``.
"""

from __future__ import annotations

from typing import Any

from ..format_options import FORMAT_JSON, emit_json

__all__ = ["run_ipc_command"]

_IPC_MISSING_MSG = "IPC dependencies not installed. Install with: pip install 'kicad-tools[ipc]'"
_PYNNG_MISSING_MSG = "pynng not installed. Install with: pip install 'kicad-tools[ipc]'"


def _fmt(args) -> str:
    """Return the requested output format for an ``ipc`` invocation."""
    return getattr(args, "format", "text") or "text"


def _emit(command: str, payload: dict[str, Any]) -> None:
    """Print the single JSON document for *command* (JSON mode only)."""
    document: dict[str, Any] = {"command": command}
    document.update(payload)
    emit_json(document)


def _fail(
    command: str,
    output_format: str,
    message: str,
    *,
    text_lines: list[str] | None = None,
    **fields: Any,
) -> int:
    """Report a failure in the requested format and return exit code 1.

    ``text_lines`` preserves the command's original prose verbatim (several
    of these paths print without an ``Error:`` prefix); it defaults to the
    conventional ``Error: <message>`` single line.
    """
    if output_format == FORMAT_JSON:
        payload: dict[str, Any] = {"error": message, "success": False}
        payload.update(fields)
        _emit(command, payload)
        return 1
    for line in text_lines if text_lines is not None else [f"Error: {message}"]:
        print(line)
    return 1


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
    output_format = _fmt(args)

    try:
        from kicad_tools.ipc.discovery import discover_instances, discover_socket
    except ImportError:
        if output_format == FORMAT_JSON:
            return _fail("status", output_format, _IPC_MISSING_MSG, ipc_available=False)
        print("Error: IPC dependencies not installed.")
        print("Install with: pip install 'kicad-tools[ipc]'")
        return 1

    socket_path = getattr(args, "socket", None)

    if socket_path:
        resolved = discover_socket(explicit_path=socket_path)
        if resolved:
            if output_format != FORMAT_JSON:
                print(f"Socket found: {resolved}")
            return _try_connect_and_report(str(resolved), output_format, instances=[])
        if output_format == FORMAT_JSON:
            return _fail(
                "status",
                output_format,
                f"Socket not found at: {socket_path}",
                socket=str(socket_path),
                connected=False,
                instances=[],
            )
        print(f"Socket not found at: {socket_path}")
        return 1

    # Auto-discover
    instances = discover_instances()
    if not instances:
        auto_socket = discover_socket()
        if auto_socket:
            if output_format != FORMAT_JSON:
                print(f"Socket found: {auto_socket}")
            return _try_connect_and_report(str(auto_socket), output_format, instances=[])
        if output_format == FORMAT_JSON:
            return _fail(
                "status",
                output_format,
                "No running KiCad instances found.",
                socket=None,
                connected=False,
                instances=[],
            )
        print("No running KiCad instances found.")
        print()
        print("To start KiCad with IPC enabled:")
        print("  kicad-cli api-server --socket /tmp/kicad/kicad.sock")
        print()
        print("Or specify a socket path:")
        print("  kct ipc status --socket /path/to/socket")
        return 1

    instance_paths = sorted(str(inst.socket_path) for inst in instances)

    if output_format != FORMAT_JSON:
        print(f"Found {len(instances)} KiCad instance(s):")
        for inst in instances:
            print(f"  {inst}")
        print()

    # Try connecting to the first instance
    return _try_connect_and_report(
        str(instances[0].socket_path), output_format, instances=instance_paths
    )


def _try_connect_and_report(
    socket_path: str, output_format: str = "text", instances: list[str] | None = None
) -> int:
    """Try connecting to KiCad and report status."""
    instances = instances or []

    try:
        from kicad_tools.ipc.client import IPCClient, IPCError
    except ImportError:
        if output_format == FORMAT_JSON:
            return _fail(
                "status",
                output_format,
                _PYNNG_MISSING_MSG,
                socket=socket_path,
                connected=False,
                instances=instances,
            )
        print("Error: pynng not installed.")
        print("Install with: pip install 'kicad-tools[ipc]'")
        return 1

    if output_format != FORMAT_JSON:
        print(f"Connecting to {socket_path}...")
    try:
        with IPCClient(socket_path) as client:
            if client.ping():
                version = client.get_version()
                docs = client.get_open_documents()
                if output_format == FORMAT_JSON:
                    _emit(
                        "status",
                        {
                            "socket": socket_path,
                            "connected": True,
                            "kicad_version": version,
                            "instances": instances,
                            "open_documents": sorted(
                                str(doc.get("path", "unknown")) for doc in docs
                            ),
                            "success": True,
                        },
                    )
                    return 0
                print(f"Connected to KiCad {version}")
                if docs:
                    print(f"Open documents: {len(docs)}")
                    for doc in docs:
                        print(f"  {doc.get('path', 'unknown')}")
                return 0
            return _fail(
                "status",
                output_format,
                "Connected but KiCad is not responding to health checks.",
                text_lines=["Connected but KiCad is not responding to health checks."],
                socket=socket_path,
                connected=False,
                instances=instances,
            )
    except IPCError as exc:
        return _fail(
            "status",
            output_format,
            f"Connection failed: {exc}",
            text_lines=[f"Connection failed: {exc}"],
            socket=socket_path,
            connected=False,
            instances=instances,
        )
    except ImportError:
        if output_format == FORMAT_JSON:
            return _fail(
                "status",
                output_format,
                _PYNNG_MISSING_MSG,
                socket=socket_path,
                connected=False,
                instances=instances,
            )
        print("Error: pynng not installed.")
        print("Install with: pip install 'kicad-tools[ipc]'")
        return 1


def _run_connect(args) -> int:
    """Test connection to a running KiCad instance."""
    socket_path = getattr(args, "socket", None)
    output_format = _fmt(args)

    try:
        from kicad_tools.ipc.client import IPCClient, IPCError
        from kicad_tools.ipc.discovery import discover_socket
    except ImportError:
        if output_format == FORMAT_JSON:
            return _fail("connect", output_format, _IPC_MISSING_MSG, connected=False)
        print("Error: IPC dependencies not installed.")
        print("Install with: pip install 'kicad-tools[ipc]'")
        return 1

    resolved = discover_socket(explicit_path=socket_path)
    if not resolved:
        if output_format == FORMAT_JSON:
            return _fail(
                "connect",
                output_format,
                "No KiCad IPC socket found.",
                socket=str(socket_path) if socket_path else None,
                connected=False,
            )
        print("No KiCad IPC socket found.")
        if socket_path:
            print(f"  Checked: {socket_path}")
        print("  Set KICAD_IPC_SOCKET or use --socket")
        return 1

    try:
        with IPCClient(str(resolved)) as client:
            version = client.get_version()
            if output_format == FORMAT_JSON:
                _emit(
                    "connect",
                    {
                        "socket": str(resolved),
                        "connected": True,
                        "kicad_version": version,
                        "success": True,
                    },
                )
                return 0
            print(f"Successfully connected to KiCad {version}")
            print(f"Socket: {resolved}")
            return 0
    except IPCError as exc:
        return _fail(
            "connect",
            output_format,
            f"Connection failed: {exc}",
            text_lines=[f"Connection failed: {exc}"],
            socket=str(resolved),
            connected=False,
        )


def _run_push_routes(args) -> int:
    """Push routed tracks from a PCB file to a running KiCad instance."""
    pcb_path = getattr(args, "pcb", None)
    socket_path = getattr(args, "socket", None)
    net_filter = getattr(args, "net", None)
    dry_run = getattr(args, "dry_run", False)
    output_format = _fmt(args)

    if not pcb_path:
        if output_format == FORMAT_JSON:
            return _fail("push-routes", output_format, "PCB file path required.", pushed=0)
        print("Error: PCB file path required.")
        print("Usage: kct ipc push-routes <pcb_file> [--socket PATH] [--net NAME]")
        return 1

    try:
        from kicad_tools.ipc.client import IPCClient, IPCError
        from kicad_tools.ipc.discovery import discover_socket
    except ImportError:
        if output_format == FORMAT_JSON:
            return _fail(
                "push-routes", output_format, _IPC_MISSING_MSG, pcb=str(pcb_path), pushed=0
            )
        print("Error: IPC dependencies not installed.")
        print("Install with: pip install 'kicad-tools[ipc]'")
        return 1

    from pathlib import Path

    pcb_file = Path(pcb_path)
    if not pcb_file.exists():
        if output_format == FORMAT_JSON:
            return _fail(
                "push-routes",
                output_format,
                f"PCB file not found: {pcb_file}",
                pcb=str(pcb_file),
                pushed=0,
            )
        print(f"Error: PCB file not found: {pcb_file}")
        return 1

    # Read tracks from the PCB file using existing kicad-tools parsing
    try:
        from kicad_tools.pcb.parser import parse_pcb
    except ImportError:
        if output_format == FORMAT_JSON:
            return _fail(
                "push-routes",
                output_format,
                "PCB parser not available.",
                pcb=str(pcb_file),
                pushed=0,
            )
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

    # Summary fields shared by every terminal state below.
    summary: dict[str, Any] = {
        "pcb": str(pcb_file),
        "net_filter": net_filter,
        "tracks": len(tracks),
        "vias": len(vias),
        "dry_run": bool(dry_run),
    }

    if output_format != FORMAT_JSON:
        print(f"Found {len(tracks)} tracks and {len(vias)} vias in {pcb_file.name}")

        if net_filter:
            print(f"  Filtered to net: {net_filter}")

    if dry_run:
        if output_format == FORMAT_JSON:
            _emit("push-routes", {**summary, "pushed": 0, "success": True})
            return 0
        print("Dry run -- no changes pushed to KiCad.")
        return 0

    if not tracks and not vias:
        if output_format == FORMAT_JSON:
            _emit("push-routes", {**summary, "pushed": 0, "success": True})
            return 0
        print("No tracks or vias to push.")
        return 0

    resolved = discover_socket(explicit_path=socket_path)
    if not resolved:
        return _fail(
            "push-routes",
            output_format,
            "No KiCad IPC socket found.",
            text_lines=["No KiCad IPC socket found."],
            **summary,
            pushed=0,
        )

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
            if output_format == FORMAT_JSON:
                _emit(
                    "push-routes",
                    {
                        **summary,
                        "socket": str(resolved),
                        "pushed": len(created),
                        "success": True,
                    },
                )
                return 0
            print(f"Pushed {len(created)} items to KiCad.")
            return 0

    except IPCError as exc:
        return _fail(
            "push-routes",
            output_format,
            str(exc),
            **summary,
            socket=str(resolved),
            pushed=0,
        )
