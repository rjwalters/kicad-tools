"""Pipeline command handler for end-to-end PCB repair workflow.

Machine output (``--format json``, issue #4674): the canonical flag is
forwarded to ``pipeline_cmd``'s inner parser, which emits one document
describing the run.  See ``docs/reference/machine-output.md``.
"""

__all__ = ["run_pipeline_command"]


def run_pipeline_command(args) -> int:
    """Handle pipeline command for existing PCB repair."""
    from ..pipeline_cmd import main as pipeline_main

    sub_argv = []

    # Positional input argument
    if getattr(args, "pipeline_input", None):
        sub_argv.append(args.pipeline_input)

    # Step selection
    if getattr(args, "pipeline_step", None):
        sub_argv.extend(["--step", args.pipeline_step])

    # Manufacturer
    if getattr(args, "pipeline_mfr", "jlcpcb") != "jlcpcb":
        sub_argv.extend(["--mfr", args.pipeline_mfr])

    # Layers (string value, e.g. "4", "4-sig", "4-all")
    if getattr(args, "pipeline_layers", None) is not None:
        sub_argv.extend(["--layers", args.pipeline_layers])

    # Flags
    if getattr(args, "pipeline_dry_run", False):
        sub_argv.append("--dry-run")

    if getattr(args, "pipeline_verbose", False):
        sub_argv.append("--verbose")

    if getattr(args, "pipeline_force", False):
        sub_argv.append("--force")

    # Commit
    if getattr(args, "pipeline_commit", False):
        sub_argv.append("--commit")

    # Max displacement for fix-drc step
    max_disp = getattr(args, "pipeline_max_displacement", None)
    if max_disp is not None and max_disp != 2.0:
        sub_argv.extend(["--max-displacement", str(max_disp)])

    # Route skip threshold (signal-net completion percent gating the route skip)
    route_skip_threshold = getattr(args, "pipeline_route_skip_threshold", None)
    if route_skip_threshold is not None and route_skip_threshold != 95.0:
        sub_argv.extend(["--route-skip-threshold", str(route_skip_threshold)])

    # Best-effort mode
    if getattr(args, "pipeline_best_effort", False):
        sub_argv.append("--best-effort")

    # Cache control
    if getattr(args, "pipeline_no_cache", False):
        sub_argv.append("--no-cache")
    if getattr(args, "pipeline_clear_cache", False):
        sub_argv.append("--clear-cache")

    # Schematic override
    if getattr(args, "pipeline_sch", None):
        sub_argv.extend(["--sch", args.pipeline_sch])

    # Apply sync drift in the sync step
    if getattr(args, "pipeline_apply_sync", False):
        sub_argv.append("--apply-sync")

    # HV pairwise-clearance passthrough (issue #4607).  Forward each flag
    # only when it differs from its parser default so an invocation without
    # the HV flags builds a byte-identical inner argv, while an explicitly
    # supplied flag is never silently dropped at this hop (the historical
    # unguarded-shim bug class -- see tests/test_pipeline_cli_args.py).
    pipeline_voltage_map = getattr(args, "pipeline_voltage_map", None)
    if pipeline_voltage_map is not None:
        sub_argv.extend(["--voltage-map", pipeline_voltage_map])

    pipeline_creepage_standard = getattr(args, "pipeline_creepage_standard", "iec60664")
    if pipeline_creepage_standard != "iec60664":
        sub_argv.extend(["--creepage-standard", pipeline_creepage_standard])

    pipeline_pollution_degree = getattr(args, "pipeline_pollution_degree", 2)
    if pipeline_pollution_degree != 2:
        sub_argv.extend(["--pollution-degree", str(pipeline_pollution_degree)])

    pipeline_material_group = getattr(args, "pipeline_material_group", "IIIa")
    if pipeline_material_group != "IIIa":
        sub_argv.extend(["--material-group", pipeline_material_group])

    pipeline_hv_threshold = getattr(args, "pipeline_hv_threshold", 30.0)
    if pipeline_hv_threshold is not None and pipeline_hv_threshold != 30.0:
        sub_argv.extend(["--hv-threshold", str(pipeline_hv_threshold)])

    # Use global quiet or command-level quiet
    if getattr(args, "global_quiet", False):
        sub_argv.append("--quiet")

    # Machine output (#4674): forward the canonical flag only when it asks for
    # JSON, so a default invocation still builds a byte-identical inner argv.
    if getattr(args, "format", "text") == "json":
        sub_argv.extend(["--format", "json"])

    return pipeline_main(sub_argv)
