"""Build command handler for end-to-end workflow orchestration.

Machine output (``--format json``, issue #4674): the canonical flag is
forwarded to ``build_cmd``'s inner parser, which emits one document
describing the run.  See ``docs/reference/machine-output.md``.
"""

__all__ = ["run_build_command"]


def run_build_command(args) -> int:
    """Handle build command for end-to-end workflow."""
    from ..build_cmd import main as build_main

    sub_argv = []

    # Positional spec argument
    if getattr(args, "build_spec", None):
        sub_argv.append(args.build_spec)

    # Step selection
    if getattr(args, "build_step", "all") != "all":
        sub_argv.extend(["--step", args.build_step])

    # Manufacturer. The outer parser default is None so an explicit --mfr
    # (even "--mfr jlcpcb") is forwarded and treated as an override, while
    # omitting the flag lets build_cmd resolve the spec's target_fab
    # (issue #3920). Forwarding only-when-not-None preserves that signal.
    build_mfr = getattr(args, "build_mfr", None)
    if build_mfr is not None:
        sub_argv.extend(["--mfr", build_mfr])

    # Flags
    if getattr(args, "build_dry_run", False):
        sub_argv.append("--dry-run")

    if getattr(args, "build_verbose", False):
        sub_argv.append("--verbose")

    if getattr(args, "build_force", False):
        sub_argv.append("--force")

    # Quiet may come from the command-level --quiet/-q flag or the global flag
    if getattr(args, "build_quiet", False) or getattr(args, "global_quiet", False):
        sub_argv.append("--quiet")

    # Output directory
    build_output = getattr(args, "build_output", None)
    if build_output:
        sub_argv.extend(["--output", build_output])

    # Optimize placement (opt-in CMA-ES)
    if getattr(args, "build_optimize_placement", False):
        sub_argv.append("--optimize-placement")

    # Smoke-check opt-out
    if getattr(args, "build_no_smoke_check", False):
        sub_argv.append("--no-smoke-check")

    # Routing-completeness preflight escape hatch
    if getattr(args, "build_allow_incomplete", False):
        sub_argv.append("--allow-incomplete")

    # HV pairwise-clearance passthrough (issue #4607).  Forward each flag
    # only when it differs from its parser default so an invocation without
    # the HV flags builds a byte-identical inner argv, while an explicitly
    # supplied flag is never silently dropped at this hop (the historical
    # unguarded-shim bug class -- see tests/test_build_cmd_errors.py).
    build_voltage_map = getattr(args, "build_voltage_map", None)
    if build_voltage_map is not None:
        sub_argv.extend(["--voltage-map", build_voltage_map])

    build_creepage_standard = getattr(args, "build_creepage_standard", "iec60664")
    if build_creepage_standard != "iec60664":
        sub_argv.extend(["--creepage-standard", build_creepage_standard])

    build_pollution_degree = getattr(args, "build_pollution_degree", 2)
    if build_pollution_degree != 2:
        sub_argv.extend(["--pollution-degree", str(build_pollution_degree)])

    build_material_group = getattr(args, "build_material_group", "IIIa")
    if build_material_group != "IIIa":
        sub_argv.extend(["--material-group", build_material_group])

    build_hv_threshold = getattr(args, "build_hv_threshold", 30.0)
    if build_hv_threshold is not None and build_hv_threshold != 30.0:
        sub_argv.extend(["--hv-threshold", str(build_hv_threshold)])

    # Machine output (#4674): forward the canonical flag only when it asks for
    # JSON, so a default invocation still builds a byte-identical inner argv.
    if getattr(args, "format", "text") == "json":
        sub_argv.extend(["--format", "json"])

    return build_main(sub_argv)
