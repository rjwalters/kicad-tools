"""Fleet command handler (fleet-wide PCB status surveys)."""

__all__ = ["run_fleet_command"]


def run_fleet_command(args) -> int:
    """Dispatch to ``fleet_cmd.main`` after re-serializing args back into argv.

    Mirrors the pattern used by ``run_decisions_command``: the unified parser
    captures top-level args and we re-package them as a sub-argv list for the
    standalone command module so behavior stays in sync between
    ``kct fleet status`` and ``python -m kicad_tools.cli.fleet_cmd status``.
    """
    from ..fleet_cmd import main as fleet_main

    sub_argv: list[str] = []

    fleet_command = getattr(args, "fleet_command", None)
    if not fleet_command:
        # No sub-action: let the standalone parser show its help.
        return fleet_main([])

    sub_argv.append(fleet_command)

    if fleet_command == "status":
        boards_dir = getattr(args, "fleet_boards_dir", None)
        if boards_dir:
            sub_argv.extend(["--boards-dir", boards_dir])

        fmt = getattr(args, "fleet_format", None)
        if fmt:
            sub_argv.extend(["--format", fmt])

        if getattr(args, "fleet_ship_only", False):
            sub_argv.append("--ship-only")

        if getattr(args, "fleet_include_stale", False):
            sub_argv.append("--include-stale")

        pattern = getattr(args, "fleet_pattern", None)
        if pattern:
            sub_argv.extend(["--pattern", pattern])

    return fleet_main(sub_argv)
