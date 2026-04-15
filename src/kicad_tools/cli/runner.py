"""KiCad CLI runner utility.

Provides functions to locate and run kicad-cli commands for
ERC validation, DRC validation, netlist export, and more.
"""

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


def find_kicad_cli() -> Path | None:
    """Find kicad-cli executable.

    Searches common installation locations for KiCad 8+.

    Returns:
        Path to kicad-cli if found, None otherwise
    """
    # Check PATH first
    if path := shutil.which("kicad-cli"):
        return Path(path)

    # Common installation locations
    locations = [
        # macOS
        "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
        "/opt/homebrew/bin/kicad-cli",
        "/usr/local/bin/kicad-cli",
        # Linux
        "/usr/bin/kicad-cli",
        "/usr/local/bin/kicad-cli",
        # Windows (common paths)
        "C:/Program Files/KiCad/8.0/bin/kicad-cli.exe",
        "C:/Program Files/KiCad/bin/kicad-cli.exe",
    ]

    for loc in locations:
        path = Path(loc)
        if path.exists():
            return path

    return None


@dataclass
class KiCadCLIResult:
    """Result from running kicad-cli."""

    success: bool
    output_path: Path | None = None
    stdout: str = ""
    stderr: str = ""
    return_code: int = 0


def run_erc(
    schematic_path: Path,
    output_path: Path | None = None,
    format: str = "json",
    severity_all: bool = True,
    kicad_cli: Path | None = None,
) -> KiCadCLIResult:
    """Run KiCad ERC on a schematic.

    Args:
        schematic_path: Path to .kicad_sch file
        output_path: Where to save the report (default: temp file)
        format: Output format - "json" or "report"
        severity_all: Include all severity levels
        kicad_cli: Path to kicad-cli (auto-detected if not provided)

    Returns:
        KiCadCLIResult with success status and output path
    """
    if kicad_cli is None:
        kicad_cli = find_kicad_cli()
        if kicad_cli is None:
            return KiCadCLIResult(
                success=False,
                stderr="kicad-cli not found. Install KiCad 8 from https://www.kicad.org/download/",
            )

    # Determine output path
    if output_path is None:
        suffix = ".json" if format == "json" else ".rpt"
        fd, temp_path = tempfile.mkstemp(suffix=suffix, prefix="erc_")
        output_path = Path(temp_path)
        # Close the file descriptor - kicad-cli will write to it
        import os

        os.close(fd)

    # Build command
    cmd = [
        str(kicad_cli),
        "sch",
        "erc",
        "--output",
        str(output_path),
        "--format",
        format,
        "--units",
        "mm",
    ]

    if severity_all:
        cmd.append("--severity-all")

    cmd.append(str(schematic_path))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)

        # ERC returns non-zero if there are violations, but still produces output
        if output_path.exists():
            return KiCadCLIResult(
                success=True,
                output_path=output_path,
                stdout=result.stdout,
                stderr=result.stderr,
                return_code=result.returncode,
            )
        else:
            return KiCadCLIResult(
                success=False,
                stderr=result.stderr or "ERC produced no output",
                return_code=result.returncode,
            )

    except FileNotFoundError as e:
        return KiCadCLIResult(success=False, stderr=f"kicad-cli not found: {e}")
    except subprocess.SubprocessError as e:
        return KiCadCLIResult(success=False, stderr=f"Failed to run ERC: {e}")


def run_drc(
    pcb_path: Path,
    output_path: Path | None = None,
    format: str = "json",
    schematic_parity: bool = True,
    kicad_cli: Path | None = None,
) -> KiCadCLIResult:
    """Run KiCad DRC on a PCB.

    Args:
        pcb_path: Path to .kicad_pcb file
        output_path: Where to save the report (default: temp file)
        format: Output format - "json" or "report"
        schematic_parity: Check schematic parity
        kicad_cli: Path to kicad-cli (auto-detected if not provided)

    Returns:
        KiCadCLIResult with success status and output path
    """
    if kicad_cli is None:
        kicad_cli = find_kicad_cli()
        if kicad_cli is None:
            return KiCadCLIResult(
                success=False,
                stderr="kicad-cli not found. Install KiCad 8 from https://www.kicad.org/download/",
            )

    # Determine output path
    if output_path is None:
        suffix = ".json" if format == "json" else ".rpt"
        fd, temp_path = tempfile.mkstemp(suffix=suffix, prefix="drc_")
        output_path = Path(temp_path)
        import os

        os.close(fd)

    # Build command
    cmd = [
        str(kicad_cli),
        "pcb",
        "drc",
        "--output",
        str(output_path),
        "--format",
        format,
        "--units",
        "mm",
    ]

    if schematic_parity:
        cmd.append("--schematic-parity")

    cmd.append(str(pcb_path))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)

        if output_path.exists():
            return KiCadCLIResult(
                success=True,
                output_path=output_path,
                stdout=result.stdout,
                stderr=result.stderr,
                return_code=result.returncode,
            )
        else:
            return KiCadCLIResult(
                success=False,
                stderr=result.stderr or "DRC produced no output",
                return_code=result.returncode,
            )

    except FileNotFoundError as e:
        return KiCadCLIResult(success=False, stderr=f"kicad-cli not found: {e}")
    except subprocess.SubprocessError as e:
        return KiCadCLIResult(success=False, stderr=f"Failed to run DRC: {e}")


def run_netlist_export(
    schematic_path: Path,
    output_path: Path | None = None,
    format: str = "kicad",
    kicad_cli: Path | None = None,
) -> KiCadCLIResult:
    """Export netlist from schematic.

    Args:
        schematic_path: Path to .kicad_sch file
        output_path: Where to save the netlist
        format: Output format - "kicad", "cadstar", "orcadpcb2", "spice", "spice-model"
        kicad_cli: Path to kicad-cli

    Returns:
        KiCadCLIResult with success status and output path
    """
    if kicad_cli is None:
        kicad_cli = find_kicad_cli()
        if kicad_cli is None:
            return KiCadCLIResult(success=False, stderr="kicad-cli not found")

    # Determine output path
    if output_path is None:
        suffix = ".net" if format == "kicad" else f".{format}"
        fd, temp_path = tempfile.mkstemp(suffix=suffix, prefix="netlist_")
        output_path = Path(temp_path)
        import os

        os.close(fd)

    cmd = [
        str(kicad_cli),
        "sch",
        "export",
        "netlist",
        "--output",
        str(output_path),
        "--format",
        format,
        str(schematic_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode == 0 and output_path.exists():
            return KiCadCLIResult(
                success=True,
                output_path=output_path,
                stdout=result.stdout,
                stderr=result.stderr,
                return_code=result.returncode,
            )
        else:
            return KiCadCLIResult(
                success=False,
                stderr=result.stderr or "Netlist export failed",
                return_code=result.returncode,
            )

    except FileNotFoundError as e:
        return KiCadCLIResult(success=False, stderr=f"kicad-cli not found: {e}")
    except subprocess.SubprocessError as e:
        return KiCadCLIResult(success=False, stderr=f"Failed to export netlist: {e}")


def _kicad_cli_has_fill_zones(kicad_cli: Path) -> bool:
    """Check whether the installed kicad-cli supports 'pcb fill-zones'.

    This subcommand does not exist in KiCad 8, 9, or 10 but may be
    added in a future release.
    """
    try:
        result = subprocess.run(
            [str(kicad_cli), "pcb", "fill-zones", "--help"],
            capture_output=True,
            text=True,
        )
        # kicad-cli returns 0 and prints the parent help when a subcommand
        # is unknown, so we check stdout for "fill-zones" to confirm support.
        return result.returncode == 0 and "fill-zones" in result.stdout
    except Exception:
        return False


def run_fill_zones(
    pcb_path: Path,
    output_path: Path | None = None,
    kicad_cli: Path | None = None,
) -> KiCadCLIResult:
    """Fill all copper zones in a PCB using kicad-cli.

    Attempts ``kicad-cli pcb fill-zones`` first (for future KiCad versions
    that may add it).  When that subcommand is unavailable (KiCad 8/9/10),
    falls back to ``kicad-cli pcb drc`` which fills all zones as a side
    effect before running design-rule checks.

    With the DRC fallback a non-zero exit code caused by DRC *violations*
    (not a fill failure) is treated as success — the zones were still filled.

    Args:
        pcb_path: Path to .kicad_pcb file
        output_path: Where to save the filled PCB (default: overwrites input)
        kicad_cli: Path to kicad-cli (auto-detected if not provided)

    Returns:
        KiCadCLIResult with success status and output path
    """
    if kicad_cli is None:
        kicad_cli = find_kicad_cli()
        if kicad_cli is None:
            return KiCadCLIResult(
                success=False,
                stderr="kicad-cli not found. Install KiCad 8 from https://www.kicad.org/download/",
            )

    # If a future KiCad ships a native fill-zones subcommand, prefer it.
    if _kicad_cli_has_fill_zones(kicad_cli):
        return _run_fill_zones_native(pcb_path, output_path, kicad_cli)

    # Fallback: use DRC which fills zones as a side effect.
    return _run_fill_zones_via_drc(pcb_path, output_path, kicad_cli)


def _run_fill_zones_native(
    pcb_path: Path,
    output_path: Path | None,
    kicad_cli: Path,
) -> KiCadCLIResult:
    """Fill zones using the native ``kicad-cli pcb fill-zones`` subcommand."""
    cmd = [str(kicad_cli), "pcb", "fill-zones"]

    if output_path is not None:
        cmd.extend(["--output", str(output_path)])

    cmd.append(str(pcb_path))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)

        expected_path = output_path if output_path is not None else pcb_path

        if result.returncode == 0:
            return KiCadCLIResult(
                success=True,
                output_path=expected_path,
                stdout=result.stdout,
                stderr=result.stderr,
                return_code=result.returncode,
            )
        else:
            return KiCadCLIResult(
                success=False,
                stdout=result.stdout,
                stderr=result.stderr or "Zone fill failed",
                return_code=result.returncode,
            )

    except FileNotFoundError as e:
        return KiCadCLIResult(success=False, stderr=f"kicad-cli not found: {e}")
    except subprocess.SubprocessError as e:
        return KiCadCLIResult(success=False, stderr=f"Failed to fill zones: {e}")


def _kicad_drc_supports_refill(kicad_cli: Path) -> bool:
    """Check whether ``kicad-cli pcb drc`` supports ``--refill-zones``.

    KiCad 10+ added explicit ``--refill-zones`` and ``--save-board`` flags.
    Earlier versions (8, 9) always refill zones as part of DRC.
    """
    try:
        result = subprocess.run(
            [str(kicad_cli), "pcb", "drc", "--help"],
            capture_output=True,
            text=True,
        )
        return "--refill-zones" in result.stdout
    except Exception:
        return False


def _snapshot_net_declarations(pcb_path: Path) -> list:
    """Snapshot net declaration S-expression nodes from a PCB file.

    Returns a list of ``(net N "name")`` SExp nodes extracted from the
    PCB's top-level children.  These can be used later by
    :func:`_restore_net_declarations` to repair a PCB whose net table
    was stripped by kicad-cli.

    Returns an empty list if the file cannot be read (e.g. it does not
    exist).  In that scenario the subsequent DRC subprocess call will
    also fail, so skipping the snapshot is harmless.
    """
    from kicad_tools.core.sexp_file import load_pcb

    try:
        sexp = load_pcb(str(pcb_path))
    except Exception:
        return []
    return [child for child in sexp.children if child.name == "net"]


def _get_fp_reference(fp_node) -> str:
    """Extract the reference designator from a footprint S-expression node.

    Checks both KiCad 8+ ``(property "Reference" "U1" ...)`` format and
    KiCad 7 ``(fp_text reference "U1" ...)`` format.

    Returns an empty string if no reference is found.
    """
    # KiCad 8+ format: (property "Reference" "U1" ...)
    for child in fp_node.children:
        if child.name == "property":
            prop_name = child.get_string(0)
            if prop_name == "Reference":
                return child.get_string(1) or ""
    # KiCad 7 format: (fp_text reference "U1" ...)
    for child in fp_node.children:
        if child.name == "fp_text":
            text_type = child.get_string(0)
            if text_type == "reference":
                return child.get_string(1) or ""
    return ""


def _has_nonzero_net(net_node) -> bool:
    """Check whether a ``(net ...)`` S-expression carries a real net assignment.

    Returns True for:
    - ``(net 1 "GND")`` — KiCad 8/9 format with nonzero net number
    - ``(net "GND")`` — KiCad 10 format (name-only, no number)

    Returns False for:
    - ``(net 0)`` or ``(net 0 "")`` — unconnected pad
    - None
    """
    if net_node is None:
        return False
    net_num = net_node.get_int(0)
    if net_num is not None:
        # Traditional format: (net N "name") — nonzero means connected
        return net_num != 0
    # KiCad 10 name-only format: (net "name") — presence of a name means connected
    net_name = net_node.get_string(0)
    return bool(net_name)


def _make_segment_via_key(child) -> str | None:
    """Build a geometry-based key for a segment or via S-expression node.

    Uses ``(start, end, layer)`` for segments and ``(position, size, layers)``
    for vias, which are stable across kicad-cli UUID regeneration.

    Returns None if required geometry fields are missing.
    """
    if child.name == "segment":
        start_node = child.get("start")
        end_node = child.get("end")
        layer_node = child.get("layer")
        if start_node is None or end_node is None or layer_node is None:
            return None
        sx = start_node.get_float(0) or 0.0
        sy = start_node.get_float(1) or 0.0
        ex = end_node.get_float(0) or 0.0
        ey = end_node.get_float(1) or 0.0
        layer = layer_node.get_string(0) or ""
        return f"seg:{sx},{sy}:{ex},{ey}:{layer}"
    elif child.name == "via":
        at_node = child.get("at")
        size_node = child.get("size")
        layers_node = child.get("layers")
        if at_node is None or size_node is None:
            return None
        ax = at_node.get_float(0) or 0.0
        ay = at_node.get_float(1) or 0.0
        sz = size_node.get_float(0) or 0.0
        layer_strs = []
        if layers_node is not None:
            for c in layers_node.children:
                if c.is_atom and isinstance(c.value, str):
                    layer_strs.append(c.value)
        return f"via:{ax},{ay}:{sz}:{','.join(layer_strs)}"
    return None


def _canonicalize_net_node(net_node, name_to_number: dict[str, int]):
    """Canonicalize a ``(net ...)`` S-expression to ``(net N "name")`` format.

    KiCad 10 may emit inline net references as ``(net "name")`` without a
    numeric ID.  If the node is in name-only format and the name appears in
    *name_to_number*, return a new ``(net N "name")`` node.  Otherwise
    return the original node unchanged.
    """
    from kicad_tools.sexp import SExp

    if net_node is None:
        return net_node
    # Already has a numeric first value — nothing to fix
    if net_node.get_int(0) is not None:
        return net_node
    # Name-only format: (net "name")
    net_name = net_node.get_string(0) or ""
    if net_name and net_name in name_to_number:
        return SExp.list("net", name_to_number[net_name], net_name)
    return net_node


def _snapshot_element_nets(pcb_path: Path) -> dict[str, list]:
    """Snapshot per-element inline ``(net ...)`` assignments from a PCB.

    Captures the ``(net ...)`` child S-expression for every pad, segment,
    and via, keyed by a stable identifier:

    - **Pads**: keyed by ``"<reference>:<pad_number>"`` using the footprint's
      reference designator, which is stable across kicad-cli UUID regeneration.
    - **Segments**: keyed by ``"seg:<sx>,<sy>:<ex>,<ey>:<layer>"`` (geometry).
    - **Vias**: keyed by ``"via:<x>,<y>:<size>:<layers>"`` (geometry).

    Net nodes are canonicalized to ``(net N "name")`` format using the PCB
    header net declarations, so that restoring always writes the full format
    even when the original used KiCad 10 name-only ``(net "name")`` syntax.

    Returns a dict mapping key strings to a list ``[net_sexp_node]``
    containing the (potentially canonicalized) ``(net ...)`` S-expression.
    An empty dict is returned if the file cannot be read.
    """
    from kicad_tools.core.sexp_file import load_pcb

    try:
        sexp = load_pcb(str(pcb_path))
    except Exception:
        return {}

    # Build name -> number lookup from header (net N "name") declarations
    name_to_number: dict[str, int] = {}
    for child in sexp.children:
        if child.name == "net":
            net_num = child.get_int(0)
            net_name = child.get_string(1) or ""
            if net_num is not None and net_num != 0 and net_name:
                name_to_number[net_name] = net_num

    snapshot: dict[str, list] = {}

    # Snapshot pads inside footprints, keyed by reference + pad number
    for fp_node in (c for c in sexp.children if c.name == "footprint"):
        fp_ref = _get_fp_reference(fp_node)
        if not fp_ref:
            continue

        for pad_node in (c for c in fp_node.children if c.name == "pad"):
            net_node = pad_node.get("net")
            if not _has_nonzero_net(net_node):
                continue
            # Use the pad's first atom (its number) as sub-key
            pad_number = pad_node.get_first_atom()
            if pad_number is None:
                continue
            key = f"{fp_ref}:{pad_number}"
            snapshot[key] = [_canonicalize_net_node(net_node, name_to_number)]

    # Snapshot segments and vias at the top level, keyed by geometry
    for child in sexp.children:
        if child.name in ("segment", "via"):
            net_node = child.get("net")
            if not _has_nonzero_net(net_node):
                continue
            geo_key = _make_segment_via_key(child)
            if geo_key:
                snapshot[geo_key] = [_canonicalize_net_node(net_node, name_to_number)]

    return snapshot


def _restore_net_declarations(
    target_pcb: Path,
    net_nodes: list,
    element_nets: dict[str, list] | None = None,
) -> None:
    """Restore net declarations and per-element net assignments in *target_pcb*.

    kicad-cli may strip ``(net N "name")`` header declarations and/or
    inline ``(net N)`` assignments inside pads, segments, and vias when
    re-serializing a PCB.  This function restores both from snapshots
    captured before the DRC run.

    The net table restoration is a no-op when the output already has at
    least as many net declarations as the snapshot.

    The per-element restoration is a no-op when no element_nets snapshot
    was provided or when no elements had their nets zeroed out.
    """
    from kicad_tools.core.sexp_file import load_pcb, save_pcb

    output_sexp = load_pcb(str(target_pcb))

    modified = False

    # --- Restore net table headers ---
    output_net_nodes = [child for child in output_sexp.children if child.name == "net"]

    if len(output_net_nodes) < len(net_nodes):
        # Remove whatever net declarations remain in the output.
        for node in output_net_nodes:
            output_sexp.children.remove(node)

        # Find insertion point: nets go after ``setup`` / ``title_block`` and
        # before ``footprint`` / ``segment`` / ``via`` / ``zone`` / ``gr_*``.
        insert_index = len(output_sexp.children)
        content_tags = {
            "footprint",
            "segment",
            "via",
            "zone",
            "gr_line",
            "gr_arc",
            "gr_text",
            "gr_rect",
            "gr_circle",
            "gr_poly",
        }
        for i, child in enumerate(output_sexp.children):
            if child.name in content_tags:
                insert_index = i
                break

        for offset, net_node in enumerate(net_nodes):
            output_sexp.children.insert(insert_index + offset, net_node)
        modified = True

    # --- Restore per-element inline net assignments ---
    if element_nets:
        # Restore pad nets inside footprints (keyed by reference:pad_number)
        for fp_node in (c for c in output_sexp.children if c.name == "footprint"):
            fp_ref = _get_fp_reference(fp_node)
            if not fp_ref:
                continue

            for pad_node in (c for c in fp_node.children if c.name == "pad"):
                pad_number = pad_node.get_first_atom()
                if pad_number is None:
                    continue
                key = f"{fp_ref}:{pad_number}"
                if key not in element_nets:
                    continue
                current_net = pad_node.get("net")
                if not _has_nonzero_net(current_net):
                    if current_net is not None:
                        pad_node.remove(current_net)
                    pad_node.append(element_nets[key][0])
                    modified = True

        # Restore segment and via nets at the top level (keyed by geometry)
        for child in output_sexp.children:
            if child.name in ("segment", "via"):
                geo_key = _make_segment_via_key(child)
                if geo_key and geo_key in element_nets:
                    current_net = child.get("net")
                    if not _has_nonzero_net(current_net):
                        if current_net is not None:
                            child.remove(current_net)
                        child.append(element_nets[geo_key][0])
                        modified = True

    if modified:
        save_pcb(output_sexp, target_pcb)


def _run_fill_zones_via_drc(
    pcb_path: Path,
    output_path: Path | None,
    kicad_cli: Path,
) -> KiCadCLIResult:
    """Fill zones by running ``kicad-cli pcb drc`` as a side-effect.

    ``kicad-cli pcb drc`` fills all zones before performing design-rule
    checks.  In KiCad 8/9 this happens automatically; in KiCad 10+ the
    ``--refill-zones`` and ``--save-board`` flags must be passed explicitly.

    A non-zero exit code caused by DRC *violations* does **not** indicate
    a fill failure.

    After a successful DRC run the output PCB's net declarations are
    verified against the input.  If kicad-cli stripped the net table
    (a known issue when the PCB was serialized by kicad-tools rather
    than KiCad itself), the original net declarations are restored so
    that segments and vias retain their net assignments.
    """
    import os

    # Snapshot input net declarations and per-element net assignments
    # *before* DRC runs — the DRC may modify the file in-place when
    # no output_path is given.
    input_net_nodes = _snapshot_net_declarations(pcb_path)
    input_element_nets = _snapshot_element_nets(pcb_path)

    # DRC modifies the input file in-place.  If the caller requested a
    # separate output file, copy the source first and run DRC on the copy.
    if output_path is not None:
        shutil.copy2(pcb_path, output_path)
        target_pcb = output_path
    else:
        target_pcb = pcb_path

    # Create a temp file for the DRC report (we don't need it).
    fd, drc_report_path = tempfile.mkstemp(suffix=".json", prefix="drc_fill_")
    os.close(fd)
    drc_report = Path(drc_report_path)

    cmd = [
        str(kicad_cli),
        "pcb",
        "drc",
        "--output",
        str(drc_report),
        "--format",
        "json",
    ]

    # KiCad 10+ requires explicit flags to refill zones and persist changes.
    if _kicad_drc_supports_refill(kicad_cli):
        cmd.extend(["--refill-zones", "--save-board"])

    cmd.append(str(target_pcb))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)

        # DRC returns non-zero when there are violations, but the zones
        # are still filled.  We treat it as success when the DRC report
        # was actually produced (meaning the command ran to completion).
        if drc_report.exists() and drc_report.stat().st_size > 0:
            # Restore net declarations and per-element net assignments
            # if kicad-cli stripped them.
            _restore_net_declarations(target_pcb, input_net_nodes, input_element_nets)

            return KiCadCLIResult(
                success=True,
                output_path=target_pcb,
                stdout=result.stdout,
                stderr=result.stderr,
                return_code=result.returncode,
            )
        else:
            return KiCadCLIResult(
                success=False,
                stderr=result.stderr or "Zone fill via DRC failed — no report produced",
                return_code=result.returncode,
            )

    except FileNotFoundError as e:
        return KiCadCLIResult(success=False, stderr=f"kicad-cli not found: {e}")
    except subprocess.SubprocessError as e:
        return KiCadCLIResult(success=False, stderr=f"Failed to fill zones: {e}")
    finally:
        # Clean up the temporary DRC report.
        drc_report.unlink(missing_ok=True)


def get_kicad_version(kicad_cli: Path | None = None) -> str | None:
    """Get KiCad version string.

    Returns:
        Version string like "8.0.6" or None if kicad-cli not found
    """
    if kicad_cli is None:
        kicad_cli = find_kicad_cli()
        if kicad_cli is None:
            return None

    try:
        result = subprocess.run(
            [str(kicad_cli), "version"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass

    return None
