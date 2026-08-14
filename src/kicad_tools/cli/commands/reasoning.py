"""Reasoning command handler (LLM-driven PCB layout).

Machine output (``--format json``, issue #4674) is forwarded to the inner
parser like every other flag; the document shape is described in
``reason_cmd`` and ``docs/reference/machine-output.md``.
"""

__all__ = ["run_reason_command"]


def run_reason_command(args) -> int:
    """Handle reason command."""
    from ..reason_cmd import main as reason_main

    sub_argv = [args.pcb]
    if args.output:
        sub_argv.extend(["-o", args.output])
    if args.export_state:
        sub_argv.append("--export-state")
    if args.state_output:
        sub_argv.extend(["--state-output", args.state_output])
    if args.interactive:
        sub_argv.append("--interactive")
    if args.analyze:
        sub_argv.append("--analyze")
    if args.auto_route:
        sub_argv.append("--auto-route")
    if args.max_nets != 10:
        sub_argv.extend(["--max-nets", str(args.max_nets)])
    if args.drc:
        sub_argv.extend(["--drc", args.drc])
    if args.verbose:
        sub_argv.append("--verbose")
    if args.dry_run:
        sub_argv.append("--dry-run")
    # Machine output (#4674): forward the canonical flag to the inner parser.
    if getattr(args, "format", "text") != "text":
        sub_argv.extend(["--format", args.format])
    return reason_main(sub_argv)
