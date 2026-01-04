"""Impedance command handlers for transmission line calculations.

Provides CLI commands for:
- Stackup display
- Trace width calculation for target impedance
- Impedance calculation for given geometry
- Differential pair analysis
- Crosstalk estimation
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from argparse import Namespace

__all__ = ["run_impedance_command"]


def run_impedance_command(args: Namespace) -> int:
    """Handle impedance command and its subcommands."""
    if not args.impedance_command:
        print("Usage: kct impedance <command> [options] [board.kicad_pcb]")
        print("Commands: stackup, width, calculate, diffpair, crosstalk")
        return 1

    if args.impedance_command == "stackup":
        return _run_stackup_command(args)
    elif args.impedance_command == "width":
        return _run_width_command(args)
    elif args.impedance_command == "calculate":
        return _run_calculate_command(args)
    elif args.impedance_command == "diffpair":
        return _run_diffpair_command(args)
    elif args.impedance_command == "crosstalk":
        return _run_crosstalk_command(args)
    else:
        print(f"Unknown impedance subcommand: {args.impedance_command}")
        return 1


def _get_stackup(args: Namespace):
    """Get stackup from PCB file or preset."""
    from kicad_tools.physics import Stackup
    from kicad_tools.schema.pcb import PCB

    pcb_path = getattr(args, "pcb", None)
    preset = getattr(args, "impedance_preset", None)

    if pcb_path:
        pcb = PCB.load(Path(pcb_path))
        return Stackup.from_pcb(pcb)
    elif preset:
        preset_map = {
            "jlcpcb-4": Stackup.jlcpcb_4layer,
            "oshpark-4": Stackup.oshpark_4layer,
            "generic-2": Stackup.default_2layer,
            "generic-4": Stackup.jlcpcb_4layer,  # Use JLCPCB as generic 4-layer
            "generic-6": Stackup.default_6layer,
        }
        if preset in preset_map:
            return preset_map[preset]()
        else:
            print(f"Unknown preset: {preset}")
            print(f"Available presets: {', '.join(preset_map.keys())}")
            sys.exit(1)
    else:
        print("Error: Provide either BOARD path or --preset option")
        sys.exit(1)


def _run_stackup_command(args: Namespace) -> int:
    """Handle impedance stackup command."""
    from rich.console import Console
    from rich.table import Table

    stackup = _get_stackup(args)
    fmt = getattr(args, "impedance_format", "text")

    if fmt == "json":
        print(json.dumps(stackup.summary(), indent=2))
        return 0

    console = Console()

    # Board summary
    console.print(f"\n[bold]Board Stackup ({stackup.board_thickness_mm:.2f}mm total):[/bold]\n")

    # Create table
    table = Table(show_header=True, header_style="bold")
    table.add_column("Layer", style="cyan")
    table.add_column("Type")
    table.add_column("Thickness", justify="right")
    table.add_column("εr", justify="right")
    table.add_column("Material")

    for layer in stackup.layers:
        # Format thickness with appropriate unit
        if layer.thickness_mm < 0.1:
            thickness = f"{layer.thickness_mm * 1000:.1f}μm"
            if layer.copper_weight_oz:
                thickness += f" ({layer.copper_weight_oz:.1f}oz)"
        else:
            thickness = f"{layer.thickness_mm:.3f}mm"

        # Format epsilon_r
        epsilon_r = f"{layer.epsilon_r:.2f}" if layer.epsilon_r > 0 else "-"

        table.add_row(
            layer.name,
            layer.layer_type.value.capitalize(),
            thickness,
            epsilon_r,
            layer.material or "-",
        )

    console.print(table)

    if stackup.copper_finish:
        console.print(f"\nCopper finish: {stackup.copper_finish}")

    return 0


def _run_width_command(args: Namespace) -> int:
    """Handle impedance width command - calculate trace width for target impedance."""
    from rich.console import Console

    from kicad_tools.physics import TransmissionLine

    console = Console()
    stackup = _get_stackup(args)
    tl = TransmissionLine(stackup)

    target_z0 = args.impedance_target
    layer = args.impedance_layer
    mode = getattr(args, "impedance_mode", "auto")
    fmt = getattr(args, "impedance_format", "text")

    # Calculate width
    try:
        width_mm = tl.width_for_impedance(z0_target=target_z0, layer=layer, mode=mode)
    except ValueError as e:
        print(f"Error: {e}")
        return 1

    # Calculate verification
    is_outer = stackup.is_outer_layer(layer)
    if mode == "auto":
        calc_fn = tl.microstrip if is_outer else tl.stripline
        geometry = "microstrip" if is_outer else "stripline"
    elif mode == "microstrip":
        calc_fn = tl.microstrip
        geometry = "microstrip"
    elif mode == "stripline":
        calc_fn = tl.stripline
        geometry = "stripline"
    else:
        calc_fn = tl.microstrip if is_outer else tl.stripline
        geometry = "microstrip" if is_outer else "stripline"

    result = calc_fn(width_mm, layer)

    if fmt == "json":
        data = {
            "target_impedance_ohm": target_z0,
            "layer": layer,
            "geometry": geometry,
            "width_mm": round(width_mm, 4),
            "width_mil": round(width_mm / 0.0254, 2),
            "verification": {
                "z0_ohm": round(result.z0, 2),
                "epsilon_eff": round(result.epsilon_eff, 3),
                "delay_ps_per_mm": round(result.propagation_delay_ps_per_mm, 2),
            },
        }
        print(json.dumps(data, indent=2))
        return 0

    # Text format with rich
    console.print(f"\n[bold]Trace Width for {target_z0}Ω on {layer} ({geometry}):[/bold]\n")

    h = stackup.get_reference_plane_distance(layer)
    er = stackup.get_dielectric_constant(layer)

    console.print(f"  Target:     {target_z0}Ω")
    console.print(f"  Layer:      {layer} ({'outer' if is_outer else 'inner'} layer, {geometry})")
    console.print(f"  Stackup:    {h:.3f}mm to reference, εr={er:.2f}")
    console.print()
    console.print(
        f"  [bold green]Result:     {width_mm * 1000 / 25.4:.1f} mil ({width_mm:.3f} mm)[/bold green]"
    )
    console.print()
    console.print("  Verification:")
    console.print(f"    Z₀ = {result.z0:.1f}Ω")
    console.print(f"    εeff = {result.epsilon_eff:.2f}")
    console.print(f"    Delay = {result.propagation_delay_ps_per_mm:.1f} ps/mm")

    return 0


def _run_calculate_command(args: Namespace) -> int:
    """Handle impedance calculate command - forward impedance calculation."""
    from rich.console import Console

    from kicad_tools.physics import TransmissionLine

    console = Console()
    stackup = _get_stackup(args)
    tl = TransmissionLine(stackup)

    width_mm = args.impedance_width
    layer = args.impedance_layer
    mode = getattr(args, "impedance_mode", "auto")
    gap_mm = getattr(args, "impedance_gap", None)
    freq_ghz = getattr(args, "impedance_frequency", 1.0)
    fmt = getattr(args, "impedance_format", "text")

    # Determine geometry and calculate
    is_outer = stackup.is_outer_layer(layer)

    if mode == "cpwg" and gap_mm is not None:
        # CPWG mode
        result = tl.cpwg(width_mm=width_mm, gap_mm=gap_mm, layer=layer, frequency_ghz=freq_ghz)
        geometry = "CPWG"
    elif mode == "auto":
        if is_outer:
            result = tl.microstrip(width_mm=width_mm, layer=layer, frequency_ghz=freq_ghz)
            geometry = "Microstrip"
        else:
            result = tl.stripline(width_mm=width_mm, layer=layer, frequency_ghz=freq_ghz)
            geometry = "Stripline"
    elif mode == "microstrip":
        result = tl.microstrip(width_mm=width_mm, layer=layer, frequency_ghz=freq_ghz)
        geometry = "Microstrip"
    elif mode == "stripline":
        result = tl.stripline(width_mm=width_mm, layer=layer, frequency_ghz=freq_ghz)
        geometry = "Stripline"
    else:
        if is_outer:
            result = tl.microstrip(width_mm=width_mm, layer=layer, frequency_ghz=freq_ghz)
            geometry = "Microstrip"
        else:
            result = tl.stripline(width_mm=width_mm, layer=layer, frequency_ghz=freq_ghz)
            geometry = "Stripline"

    h = stackup.get_reference_plane_distance(layer)
    er = stackup.get_dielectric_constant(layer)

    if fmt == "json":
        data = {
            "geometry": geometry.lower(),
            "layer": layer,
            "width_mm": width_mm,
            "width_mil": round(width_mm / 0.0254, 2),
            "height_mm": round(h, 4),
            "epsilon_r": round(er, 2),
            "results": {
                "z0_ohm": round(result.z0, 2),
                "epsilon_eff": round(result.epsilon_eff, 3),
                "delay_ps_per_mm": round(result.propagation_delay_ps_per_mm, 2),
                "delay_ps_per_inch": round(result.propagation_delay_ns_per_inch * 1000, 1),
                "loss_db_per_m": round(result.loss_db_per_m, 3),
                "loss_db_per_inch": round(result.loss_db_per_m * 0.0254, 4),
            },
        }
        if gap_mm:
            data["gap_mm"] = gap_mm
        print(json.dumps(data, indent=2))
        return 0

    # Text format
    console.print("\n[bold]Impedance Calculation:[/bold]\n")
    console.print(f"  Geometry:   {geometry}")
    console.print(f"  Width:      {width_mm / 0.0254:.1f} mil ({width_mm:.3f} mm)")
    console.print(f"  Layer:      {layer}")
    console.print(f"  Height:     {h / 0.0254:.1f} mil ({h:.3f} mm) to reference")
    console.print(f"  εr:         {er:.2f}")
    if gap_mm:
        console.print(f"  Gap:        {gap_mm / 0.0254:.1f} mil ({gap_mm:.3f} mm)")
    console.print()
    console.print("  [bold]Results:[/bold]")
    console.print(f"    Z₀ = [bold green]{result.z0:.1f}Ω[/bold green]")
    console.print(f"    εeff = {result.epsilon_eff:.2f}")
    console.print(
        f"    Delay = {result.propagation_delay_ps_per_mm:.1f} ps/mm ({result.propagation_delay_ns_per_inch * 1000:.0f} ps/inch)"
    )
    console.print(f"    Loss = {result.loss_db_per_m * 0.0254:.3f} dB/inch @ {freq_ghz}GHz")

    return 0


def _run_diffpair_command(args: Namespace) -> int:
    """Handle impedance diffpair command - differential pair analysis."""
    from rich.console import Console

    from kicad_tools.physics import CoupledLines

    console = Console()
    stackup = _get_stackup(args)
    cl = CoupledLines(stackup)

    width_mm = args.impedance_width
    gap_mm = args.impedance_gap
    layer = args.impedance_layer
    target_zdiff = getattr(args, "impedance_target", None)
    fmt = getattr(args, "impedance_format", "text")

    is_outer = stackup.is_outer_layer(layer)

    if is_outer:
        result = cl.edge_coupled_microstrip(width_mm=width_mm, gap_mm=gap_mm, layer=layer)
        geometry = "Edge-coupled microstrip"
    else:
        result = cl.edge_coupled_stripline(width_mm=width_mm, gap_mm=gap_mm, layer=layer)
        geometry = "Edge-coupled stripline"

    # Check against target if provided
    target_check = None
    if target_zdiff:
        tolerance_pct = 5.0
        diff_pct = abs(result.zdiff - target_zdiff) / target_zdiff * 100
        if diff_pct <= tolerance_pct:
            target_check = ("pass", f"✓ Within ±{tolerance_pct:.0f}% of {target_zdiff}Ω")
        else:
            target_check = ("fail", f"✗ {diff_pct:.1f}% from {target_zdiff}Ω target")

    if fmt == "json":
        data = {
            "geometry": geometry.lower().replace(" ", "_"),
            "layer": layer,
            "width_mm": width_mm,
            "gap_mm": gap_mm,
            "results": {
                "zdiff_ohm": round(result.zdiff, 2),
                "zcommon_ohm": round(result.zcommon, 2),
                "z0_even_ohm": round(result.z0_even, 2),
                "z0_odd_ohm": round(result.z0_odd, 2),
                "coupling_coefficient": round(result.coupling_coefficient, 3),
            },
        }
        if target_zdiff:
            data["target_zdiff_ohm"] = target_zdiff
            data["target_met"] = target_check[0] == "pass"
        print(json.dumps(data, indent=2))
        return 0

    # Text format
    console.print("\n[bold]Differential Pair Analysis:[/bold]\n")
    console.print(f"  Geometry:   {geometry}")
    console.print(f"  Width:      {width_mm / 0.0254:.1f} mil ({width_mm:.3f} mm) each")
    console.print(f"  Gap:        {gap_mm / 0.0254:.1f} mil ({gap_mm:.3f} mm)")
    console.print(f"  Layer:      {layer}")
    console.print()
    console.print("  [bold]Results:[/bold]")
    console.print(f"    Zdiff = [bold green]{result.zdiff:.1f}Ω[/bold green]", end="")
    if target_check:
        color = "green" if target_check[0] == "pass" else "red"
        console.print(f" [{color}]({target_check[1]})[/{color}]")
    else:
        console.print()
    console.print(f"    Zcommon = {result.zcommon:.1f}Ω")
    console.print(f"    Z0_even = {result.z0_even:.1f}Ω")
    console.print(f"    Z0_odd = {result.z0_odd:.1f}Ω")
    console.print(f"    Coupling k = {result.coupling_coefficient:.2f}")

    # Recommendations
    console.print()
    console.print("  [bold]Recommendations:[/bold]")
    if 81 <= result.zdiff <= 99:
        console.print("    [green]✓ Good for USB 2.0 (90Ω ±10%)[/green]")
    if 90 <= result.zdiff <= 110:
        console.print("    [green]✓ Good for LVDS (100Ω ±10%)[/green]")
    if 85 <= result.zdiff <= 115:
        console.print("    [green]✓ Good for HDMI (100Ω ±15%)[/green]")

    return 0


def _run_crosstalk_command(args: Namespace) -> int:
    """Handle impedance crosstalk command - crosstalk estimation."""
    from rich.console import Console

    from kicad_tools.physics import CrosstalkAnalyzer

    console = Console()
    stackup = _get_stackup(args)
    xt = CrosstalkAnalyzer(stackup)

    layer = args.impedance_layer
    fmt = getattr(args, "impedance_format", "text")

    # Check if calculating spacing for budget
    max_percent = getattr(args, "impedance_max_percent", None)
    length_mm = getattr(args, "impedance_length", None)
    width_mm = getattr(args, "impedance_width", None)
    rise_time_ns = getattr(args, "impedance_rise_time", 1.0)

    if max_percent and length_mm and width_mm:
        # Calculate spacing for crosstalk budget
        spacing = xt.spacing_for_crosstalk_budget(
            max_crosstalk_percent=max_percent,
            width_mm=width_mm,
            parallel_length_mm=length_mm,
            layer=layer,
            rise_time_ns=rise_time_ns,
        )

        if fmt == "json":
            data = {
                "calculation": "spacing_for_budget",
                "max_crosstalk_percent": max_percent,
                "parallel_length_mm": length_mm,
                "width_mm": width_mm,
                "rise_time_ns": rise_time_ns,
                "layer": layer,
                "result": {
                    "minimum_spacing_mm": round(spacing, 3),
                    "minimum_spacing_mil": round(spacing / 0.0254, 1),
                },
            }
            print(json.dumps(data, indent=2))
            return 0

        console.print(f"\n[bold]Spacing for <{max_percent}% Crosstalk:[/bold]\n")
        console.print(f"  Parallel length: {length_mm} mm")
        console.print(f"  Trace width:     {width_mm / 0.0254:.1f} mil ({width_mm:.3f} mm)")
        console.print(f"  Max crosstalk:   {max_percent}%")
        console.print(f"  Rise time:       {rise_time_ns} ns")
        console.print()
        console.print(
            f"  [bold green]Minimum spacing: {spacing / 0.0254:.1f} mil ({spacing:.3f} mm)[/bold green]"
        )
        return 0

    # Standard crosstalk analysis
    spacing_mm = getattr(args, "impedance_spacing", None)
    if not spacing_mm or not length_mm or not width_mm:
        console.print(
            "[red]Error: For crosstalk analysis, provide --spacing, --length, and --width[/red]"
        )
        console.print(
            "[red]       Or use --max-percent, --length, and --width to calculate spacing[/red]"
        )
        return 1

    result = xt.analyze(
        aggressor_width_mm=width_mm,
        victim_width_mm=width_mm,
        spacing_mm=spacing_mm,
        parallel_length_mm=length_mm,
        layer=layer,
        rise_time_ns=rise_time_ns,
    )

    if fmt == "json":
        data = {
            "spacing_mm": spacing_mm,
            "parallel_length_mm": length_mm,
            "width_mm": width_mm,
            "layer": layer,
            "rise_time_ns": rise_time_ns,
            "results": {
                "next_percent": round(result.next_percent, 2),
                "next_db": round(result.next_db, 1),
                "fext_percent": round(result.fext_percent, 2),
                "fext_db": round(result.fext_db, 1),
                "saturation_length_mm": round(result.saturation_length_mm, 2),
            },
            "severity": result.severity,
            "recommendation": result.recommendation,
        }
        print(json.dumps(data, indent=2))
        return 0

    # Text format
    console.print("\n[bold]Crosstalk Analysis:[/bold]\n")
    console.print(f"  Spacing:        {spacing_mm / 0.0254:.1f} mil ({spacing_mm:.3f} mm)")
    console.print(f"  Parallel:       {length_mm} mm")
    console.print(f"  Width:          {width_mm / 0.0254:.1f} mil ({width_mm:.3f} mm) each")
    console.print(f"  Layer:          {layer}")
    console.print(f"  Rise time:      {rise_time_ns} ns")
    console.print()

    # Results with severity coloring
    severity_colors = {"acceptable": "green", "marginal": "yellow", "excessive": "red"}
    color = severity_colors.get(result.severity, "white")

    console.print("  [bold]Results:[/bold]")
    console.print(f"    NEXT = {result.next_percent:.1f}% ({result.next_db:.1f} dB)")
    console.print(f"    FEXT = {result.fext_percent:.1f}% ({result.fext_db:.1f} dB)")
    console.print(f"    Saturation length: {result.saturation_length_mm:.1f} mm")
    console.print()
    console.print(f"  [bold]Crosstalk Risk: [{color}]{result.severity.upper()}[/{color}][/bold]")

    if result.recommendation:
        console.print()
        console.print(f"  [bold]Recommendation:[/bold] {result.recommendation}")

    return 0
