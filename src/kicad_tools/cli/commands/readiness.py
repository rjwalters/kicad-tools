"""Readiness command handler (``kct readiness``, issue #4977).

Thin shim that re-serializes the unified-parser args back into an argv list for
the standalone :mod:`kicad_tools.cli.readiness_cmd` module, mirroring the
pattern used by :func:`run_board_metrics_command` and :func:`run_fleet_command`.
This keeps behavior identical between ``kct readiness`` and
``python -m kicad_tools.cli.readiness_cmd``.
"""

__all__ = ["run_readiness_command"]


def run_readiness_command(args) -> int:
    """Dispatch to ``readiness_cmd.main`` after rebuilding sub-argv."""
    from ..readiness_cmd import main as readiness_main

    sub_argv: list[str] = [args.readiness_board]

    manufacturer = getattr(args, "readiness_manufacturer", None)
    if manufacturer:
        sub_argv.extend(["--mfr", manufacturer])

    if getattr(args, "readiness_generate", False):
        sub_argv.append("--generate")
    elif getattr(args, "readiness_verify", False):
        sub_argv.append("--verify")

    if getattr(args, "readiness_pcb_only", False):
        sub_argv.append("--pcb-only")
    elif getattr(args, "readiness_assembly", False):
        sub_argv.append("--assembly")

    output = getattr(args, "readiness_output", None)
    if output:
        sub_argv.extend(["--output", output])

    schematic = getattr(args, "readiness_schematic", None)
    if schematic:
        sub_argv.extend(["--sch", schematic])

    net_class_map = getattr(args, "readiness_net_class_map", None)
    if net_class_map:
        sub_argv.extend(["--net-class-map", net_class_map])

    project_root = getattr(args, "readiness_project_root", None)
    if project_root:
        sub_argv.extend(["--project-root", project_root])

    ack = getattr(args, "readiness_ack_warnings", "")
    if ack:
        sub_argv.extend(["--ack-warnings", ack])

    if getattr(args, "readiness_include_tht", False):
        sub_argv.append("--include-tht")

    if getattr(args, "readiness_no_archive", False):
        sub_argv.append("--no-archive")

    hv_net_class = getattr(args, "readiness_hv_net_class", None)
    if hv_net_class and hv_net_class != "HV":
        sub_argv.extend(["--hv-net-class", hv_net_class])

    hv_min = getattr(args, "readiness_hv_min", None)
    if hv_min is not None:
        sub_argv.extend(["--hv-min", str(hv_min)])

    hv_standard = getattr(args, "readiness_hv_standard", None)
    if hv_standard:
        sub_argv.extend(["--hv-standard", hv_standard])

    hv_working_voltage = getattr(args, "readiness_hv_working_voltage", None)
    if hv_working_voltage is not None:
        sub_argv.extend(["--hv-working-voltage", str(hv_working_voltage)])

    hv_pollution_degree = getattr(args, "readiness_hv_pollution_degree", None)
    if hv_pollution_degree is not None:
        sub_argv.extend(["--hv-pollution-degree", str(hv_pollution_degree)])

    hv_material_group = getattr(args, "readiness_hv_material_group", None)
    if hv_material_group and hv_material_group != "IIIa":
        sub_argv.extend(["--hv-material-group", hv_material_group])

    fill_tolerance = getattr(args, "readiness_fill_tolerance", None)
    if fill_tolerance is not None:
        sub_argv.extend(["--fill-tolerance", str(fill_tolerance)])

    if getattr(args, "format", "text") != "text":
        sub_argv.extend(["--format", args.format])

    return readiness_main(sub_argv)
