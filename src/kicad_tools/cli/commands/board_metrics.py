"""Board-metrics command handler (emit normalized board.json per board).

Thin shim that re-serializes the unified-parser args back into an argv list for
the standalone :mod:`kicad_tools.cli.board_metrics_cmd` module, mirroring the
pattern used by :func:`run_fleet_command`. This keeps behavior in sync between
``kct board-metrics`` and ``python -m kicad_tools.cli.board_metrics_cmd``.
"""

__all__ = ["run_board_metrics_command"]


def run_board_metrics_command(args) -> int:
    """Dispatch to ``board_metrics_cmd.main`` after rebuilding sub-argv."""
    from ..board_metrics_cmd import main as board_metrics_main

    sub_argv: list[str] = []

    if getattr(args, "board_metrics_board", None):
        sub_argv.append(args.board_metrics_board)

    if getattr(args, "board_metrics_all", False):
        sub_argv.append("--all")

    boards_dir = getattr(args, "board_metrics_boards_dir", None)
    if boards_dir:
        sub_argv.extend(["--boards-dir", boards_dir])

    output = getattr(args, "board_metrics_output", None)
    if output:
        sub_argv.extend(["--output", output])

    if getattr(args, "board_metrics_dry_run", False):
        sub_argv.append("--dry-run")

    return board_metrics_main(sub_argv)
