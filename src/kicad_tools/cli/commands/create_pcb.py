"""Create-PCB command handler for generating PCBs from schematics."""

__all__ = ["run_create_pcb_command"]


def run_create_pcb_command(args) -> int:
    """Handle create-pcb command."""
    from ..create_pcb_cmd import main as create_pcb_main

    sub_argv = [args.create_pcb_schematic]

    if getattr(args, "create_pcb_output", None):
        sub_argv.extend(["-o", args.create_pcb_output])

    if getattr(args, "create_pcb_width", 100.0) != 100.0:
        sub_argv.extend(["--width", str(args.create_pcb_width)])

    if getattr(args, "create_pcb_height", 100.0) != 100.0:
        sub_argv.extend(["--height", str(args.create_pcb_height)])

    if getattr(args, "create_pcb_layers", 2) != 2:
        sub_argv.extend(["--layers", str(args.create_pcb_layers)])

    if getattr(args, "create_pcb_title", ""):
        sub_argv.extend(["--title", args.create_pcb_title])

    if getattr(args, "create_pcb_revision", "1.0") != "1.0":
        sub_argv.extend(["--revision", args.create_pcb_revision])

    if getattr(args, "create_pcb_company", ""):
        sub_argv.extend(["--company", args.create_pcb_company])

    if getattr(args, "create_pcb_no_place", False):
        sub_argv.append("--no-place")

    if getattr(args, "create_pcb_spacing", 15.0) != 15.0:
        sub_argv.extend(["--spacing", str(args.create_pcb_spacing)])

    if getattr(args, "create_pcb_columns", 10) != 10:
        sub_argv.extend(["--columns", str(args.create_pcb_columns)])

    if getattr(args, "create_pcb_dry_run", False):
        sub_argv.append("--dry-run")

    return create_pcb_main(sub_argv)
