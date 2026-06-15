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
        str(Path.home() / "Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"),
        "/opt/homebrew/bin/kicad-cli",
        "/usr/local/bin/kicad-cli",
        # Linux
        "/usr/bin/kicad-cli",
        "/usr/local/bin/kicad-cli",
        # Windows (common paths)
        "C:/Program Files/KiCad/8.0/bin/kicad-cli.exe",
        "C:/Program Files/KiCad/7.0/bin/kicad-cli.exe",
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

        # ERC returns non-zero when violations are found but still produces a
        # report.  However, when kicad-cli fails to LOAD the schematic it
        # also returns non-zero (exit 3) while writing nothing — yet the
        # tempfile pre-created above continues to exist with zero bytes.
        # Treat a load failure (recognised by either explicit stderr text or
        # an empty output file paired with a non-zero exit code) as a
        # genuine failure so the caller doesn't false-pass a broken
        # schematic.  See issue #2780.
        load_failed = "Failed to load schematic" in (result.stderr or "") or "Failed to load" in (
            result.stderr or ""
        )
        empty_output = not output_path.exists() or output_path.stat().st_size == 0
        kicad_cli_errored = result.returncode != 0 and empty_output

        if load_failed or kicad_cli_errored:
            return KiCadCLIResult(
                success=False,
                stderr=result.stderr or "ERC failed to load schematic",
                return_code=result.returncode,
            )

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
    """Fill zones using the native ``kicad-cli pcb fill-zones`` subcommand.

    Snapshots net declarations and per-element net assignments before
    running the external command, then restores them afterward — mirroring
    the protection in :func:`_run_fill_zones_via_drc`.
    """
    # Snapshot net state before invoking kicad-cli.
    input_net_nodes = _snapshot_net_declarations(pcb_path)
    input_element_nets = _snapshot_element_nets(pcb_path)

    cmd = [str(kicad_cli), "pcb", "fill-zones"]

    if output_path is not None:
        cmd.extend(["--output", str(output_path)])

    cmd.append(str(pcb_path))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)

        expected_path = output_path if output_path is not None else pcb_path

        if result.returncode == 0:
            # Restore net declarations and per-element nets if kicad-cli
            # rewrote them.  Guard on file existence since the output may
            # be at a different path than the input.
            if expected_path.exists():
                _restore_net_declarations(expected_path, input_net_nodes, input_element_nets)

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


def _has_canonical_net(net_node) -> bool:
    """Check whether a ``(net ...)`` node is in canonical ``(net N ...)`` format.

    Returns True only when the net has a numeric ID as its first child AND
    the ID is nonzero.  Name-only format ``(net "name")`` — which kicad-cli
    may emit as a corruption artefact — returns False so that the restore
    logic will overwrite it with the snapshotted canonical form.

    Returns True for:
    - ``(net 1 "GND")`` — canonical format with nonzero net number
    - ``(net 18)`` — numeric-only (valid, no name)

    Returns False for:
    - ``(net "SYNC_R")`` — name-only, missing numeric ID (corruption)
    - ``(net "")`` — empty name-only (corruption)
    - ``(net 0)`` or ``(net 0 "")`` — unconnected
    - None
    """
    if net_node is None:
        return False
    net_num = net_node.get_int(0)
    if net_num is not None:
        return net_num != 0
    # No numeric first child — this is name-only format, treat as needing restore
    return False


def _make_segment_via_key(child, *, precision: int = 4) -> str | None:
    """Build a geometry-based key for a segment or via S-expression node.

    Uses ``(start, end, layer)`` for segments and ``(position, size, layers)``
    for vias, which are stable across kicad-cli UUID regeneration.

    Coordinates are rounded to *precision* decimal places (default 4) so that
    minor floating-point drift introduced by kicad-cli re-serialization still
    produces identical keys.

    Returns None if required geometry fields are missing.
    """
    if child.name == "segment":
        start_node = child.get("start")
        end_node = child.get("end")
        layer_node = child.get("layer")
        if start_node is None or end_node is None or layer_node is None:
            return None
        sx = round(start_node.get_float(0) or 0.0, precision)
        sy = round(start_node.get_float(1) or 0.0, precision)
        ex = round(end_node.get_float(0) or 0.0, precision)
        ey = round(end_node.get_float(1) or 0.0, precision)
        layer = layer_node.get_string(0) or ""
        return f"seg:{sx},{sy}:{ex},{ey}:{layer}"
    elif child.name == "via":
        at_node = child.get("at")
        size_node = child.get("size")
        layers_node = child.get("layers")
        if at_node is None or size_node is None:
            return None
        ax = round(at_node.get_float(0) or 0.0, precision)
        ay = round(at_node.get_float(1) or 0.0, precision)
        sz = round(size_node.get_float(0) or 0.0, precision)
        layer_strs = []
        if layers_node is not None:
            for c in layers_node.children:
                if c.is_atom and isinstance(c.value, str):
                    layer_strs.append(c.value)
        return f"via:{ax},{ay}:{sz}:{','.join(layer_strs)}"
    return None


def _get_element_uuid(child) -> str | None:
    """Extract the UUID string from a segment or via S-expression node.

    Returns the UUID value if a ``(uuid ...)`` child exists, else None.
    """
    uuid_node = child.get("uuid")
    if uuid_node is None:
        return None
    return uuid_node.get_string(0)


def _canonicalize_net_node(net_node, name_to_number: dict[str, int], *, numeric_only: bool = False):
    """Canonicalize a ``(net ...)`` S-expression to canonical format.

    KiCad 10 may emit inline net references as ``(net "name")`` without a
    numeric ID.  If the node is in name-only format and the name appears in
    *name_to_number*, return a corrected node.  Otherwise return the original
    node unchanged.

    Args:
        net_node: The S-expression node to canonicalize.
        name_to_number: Mapping from net name to net number.
        numeric_only: When *True*, produce ``(net N)`` (required for
            segments and vias in KiCad 9+).  When *False* (default),
            produce ``(net N "name")`` (valid for pads and net
            declarations).
    """
    from kicad_tools.sexp import SExp

    if net_node is None:
        return net_node
    # Already has a numeric first value
    if net_node.get_int(0) is not None:
        # When numeric_only is requested, strip any trailing name string.
        # kicad-cli DRC may rewrite (net N) → (net N "name") on segments;
        # KiCad 9 rejects the dual-atom format on segments/vias.
        if numeric_only and len(net_node.children) > 1:
            return SExp.list("net", net_node.get_int(0))
        return net_node
    # Name-only format: (net "name")
    net_name = net_node.get_string(0) or ""
    if net_name and net_name in name_to_number:
        num = name_to_number[net_name]
        if numeric_only:
            return SExp.list("net", num)
        return SExp.list("net", num, net_name)
    return net_node


@dataclass
class NetFormatReport:
    """Result of :func:`validate_net_format`."""

    valid: bool
    """True when every checked element has canonical numeric net format."""
    name_only_segments: int = 0
    """Count of segments with name-only ``(net "name")`` format."""
    name_only_vias: int = 0
    """Count of vias with name-only ``(net "name")`` format."""
    name_only_pads: int = 0
    """Count of pads with name-only ``(net "name")`` format."""
    empty_net_segments: int = 0
    """Count of segments with empty ``(net "")`` format."""
    empty_net_vias: int = 0
    """Count of vias with empty ``(net "")`` format."""
    empty_net_pads: int = 0
    """Count of pads with empty ``(net "")`` format."""

    @property
    def total_corrupt(self) -> int:
        return (
            self.name_only_segments
            + self.name_only_vias
            + self.name_only_pads
            + self.empty_net_segments
            + self.empty_net_vias
            + self.empty_net_pads
        )


def validate_net_format(pcb_path: Path) -> NetFormatReport:
    """Validate that all segments, vias, and pads have canonical numeric net format.

    Canonical format is ``(net N)`` or ``(net N "name")`` where N is a nonzero
    integer.  Name-only format ``(net "name")`` and empty-net ``(net "")`` on
    elements that should be connected are flagged as corrupt.

    Elements with ``(net 0)`` (unconnected pads) are not flagged — those are
    legitimately unconnected.

    Args:
        pcb_path: Path to a ``.kicad_pcb`` file.

    Returns:
        A :class:`NetFormatReport` summarising any corruption found.
    """
    from kicad_tools.core.sexp_file import load_pcb

    try:
        sexp = load_pcb(str(pcb_path))
    except Exception:
        # If we can't load the file, report as valid — the caller will
        # surface the load failure through other channels.
        return NetFormatReport(valid=True)

    report = NetFormatReport(valid=True)

    def _check_net_node(net_node, element_kind: str) -> None:
        """Classify a ``(net ...)`` node and update *report* counters."""
        if net_node is None:
            return
        net_num = net_node.get_int(0)
        if net_num is not None:
            # Has numeric ID — canonical (even if 0, that's just unconnected)
            return
        # Name-only or empty-string format
        net_name = net_node.get_string(0)
        if net_name:
            # Name-only corruption: (net "SYNC_R")
            if element_kind == "segment":
                report.name_only_segments += 1
            elif element_kind == "via":
                report.name_only_vias += 1
            elif element_kind == "pad":
                report.name_only_pads += 1
            report.valid = False
        elif net_name == "":
            # Empty-string corruption: (net "")
            if element_kind == "segment":
                report.empty_net_segments += 1
            elif element_kind == "via":
                report.empty_net_vias += 1
            elif element_kind == "pad":
                report.empty_net_pads += 1
            report.valid = False

    # Check top-level segments and vias
    for child in sexp.children:
        if child.name == "segment":
            _check_net_node(child.get("net"), "segment")
        elif child.name == "via":
            _check_net_node(child.get("net"), "via")

    # Check pads inside footprints
    for fp_node in (c for c in sexp.children if c.name == "footprint"):
        for pad_node in (c for c in fp_node.children if c.name == "pad"):
            _check_net_node(pad_node.get("net"), "pad")

    return report


def _snapshot_element_nets(pcb_path: Path) -> dict[str, list]:
    """Snapshot per-element inline ``(net ...)`` assignments from a PCB.

    Captures the ``(net ...)`` child S-expression for every pad, segment,
    and via, keyed by a stable identifier:

    - **Pads**: keyed by ``"<reference>:<pad_number>"`` using the footprint's
      reference designator, which is stable across kicad-cli UUID regeneration.
    - **Segments/Vias (UUID)**: keyed by ``"uuid:<uuid>"`` when the element
      carries a ``(uuid ...)`` node.  UUID-based keys are immune to coordinate
      drift from DRC displacement (up to 0.5 mm for drill clearance repair).
    - **Segments (geometry)**: keyed by ``"seg:<sx>,<sy>:<ex>,<ey>:<layer>"``.
    - **Vias (geometry)**: keyed by ``"via:<x>,<y>:<size>:<layers>"``.

    Both UUID and geometry keys are stored for each element so that the
    restore pass can try UUID first (handles displacement) then fall back
    to geometry (handles UUID regeneration by kicad-cli).

    Net nodes are canonicalized to ``(net N "name")`` format using the PCB
    header net declarations, so that restoring always writes the full format
    even when the original used KiCad 10 name-only ``(net "name")`` syntax.

    Returns a dict mapping key strings to a list ``[net_sexp_node]``
    containing the (potentially canonicalized) ``(net ...)`` S-expression.
    An empty dict is returned if the file cannot be read.
    """
    from kicad_tools.core.sexp_file import load_pcb
    from kicad_tools.sexp import SExp

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

    # Snapshot segments and vias at the top level, keyed by both UUID and
    # geometry.  Include ALL segments/vias — even (net 0) — so that zone
    # fill corruption to (net "") can be restored to the original net.
    for child in sexp.children:
        if child.name in ("segment", "via"):
            net_node = child.get("net")
            if _has_nonzero_net(net_node):
                canonical = [_canonicalize_net_node(net_node, name_to_number, numeric_only=True)]
            else:
                # Preserve (net 0) assignment so restore can distinguish
                # "originally unconnected" from "newly created by fill".
                canonical = [SExp.list("net", 0)]

            # Store under geometry key (handles UUID regeneration).
            geo_key = _make_segment_via_key(child)
            if geo_key:
                snapshot[geo_key] = canonical

            # Store under UUID key (handles coordinate displacement).
            elem_uuid = _get_element_uuid(child)
            if elem_uuid:
                snapshot[f"uuid:{elem_uuid}"] = canonical

    return snapshot


def _fallback_restore_by_proximity(output_sexp, element_nets: dict[str, list]) -> bool:
    """Second-pass restore for segments/vias still carrying ``(net "")``.

    After the primary geometry-key restore, some elements may remain
    unmatched due to coordinate drift exceeding the rounding tolerance or
    new geometry created by kicad-cli during zone fill.

    This function builds per-layer spatial indices from the *element_nets*
    snapshot and assigns each remaining ``(net "")`` element the net of the
    nearest snapshotted element on the same layer, provided the distance is
    within a generous tolerance (0.5 mm -- matching the drill clearance max_displacement
    and still well below typical minimum trace clearance of 0.15 mm+).

    Returns True if any element was modified.
    """
    import math

    PROXIMITY_THRESHOLD = 0.5  # mm — must cover drill clearance max_displacement (0.5 mm)

    # Parse snapshot keys into spatial buckets by (element_type, layer_key).
    # segment keys: "seg:sx,sy:ex,ey:layer"
    # via keys:     "via:ax,ay:sz:layer1,layer2,..."
    spatial_index: dict[tuple[str, str], list[tuple[float, float, float, float, list]]] = {}
    for key, net_nodes in element_nets.items():
        parts = key.split(":")
        if len(parts) < 4:
            continue
        etype = parts[0]
        if etype == "seg":
            try:
                sx, sy = float(parts[1].split(",")[0]), float(parts[1].split(",")[1])
                ex, ey = float(parts[2].split(",")[0]), float(parts[2].split(",")[1])
            except (ValueError, IndexError):
                continue
            layer_key = parts[3]
            bucket = spatial_index.setdefault(("segment", layer_key), [])
            bucket.append((sx, sy, ex, ey, net_nodes))
        elif etype == "via":
            try:
                ax, ay = float(parts[1].split(",")[0]), float(parts[1].split(",")[1])
            except (ValueError, IndexError):
                continue
            layer_key = parts[3]  # comma-joined layers string
            bucket = spatial_index.setdefault(("via", layer_key), [])
            bucket.append((ax, ay, 0.0, 0.0, net_nodes))

    changed = False
    for child in output_sexp.children:
        if child.name not in ("segment", "via"):
            continue
        current_net = child.get("net")
        if _has_canonical_net(current_net):
            continue
        # This element still has a bad net -- attempt proximity match.
        if child.name == "segment":
            start_node = child.get("start")
            end_node = child.get("end")
            layer_node = child.get("layer")
            if start_node is None or end_node is None or layer_node is None:
                continue
            sx = start_node.get_float(0) or 0.0
            sy = start_node.get_float(1) or 0.0
            ex = end_node.get_float(0) or 0.0
            ey = end_node.get_float(1) or 0.0
            layer_key = layer_node.get_string(0) or ""
            bucket = spatial_index.get(("segment", layer_key), [])
            best_dist = PROXIMITY_THRESHOLD
            best_net = None
            for bsx, bsy, bex, bey, net_nodes in bucket:
                # Use the worst (max) single-endpoint distance rather
                # than the sum of both.  The combined metric is too
                # restrictive: when zone fill shifts both endpoints
                # slightly, the sum can exceed the threshold even though
                # each individual shift is small.  Using max() ensures
                # *both* endpoints are within the threshold individually.
                start_dist = math.hypot(sx - bsx, sy - bsy)
                end_dist = math.hypot(ex - bex, ey - bey)
                dist = max(start_dist, end_dist)
                if dist < best_dist:
                    best_dist = dist
                    best_net = net_nodes
        elif child.name == "via":
            at_node = child.get("at")
            if at_node is None:
                continue
            ax = at_node.get_float(0) or 0.0
            ay = at_node.get_float(1) or 0.0
            # Build the layers string to match the bucket key
            layers_node = child.get("layers")
            layer_strs = []
            if layers_node is not None:
                for c in layers_node.children:
                    if c.is_atom and isinstance(c.value, str):
                        layer_strs.append(c.value)
            layer_key = ",".join(layer_strs)
            bucket = spatial_index.get(("via", layer_key), [])
            best_dist = PROXIMITY_THRESHOLD
            best_net = None
            for bax, bay, _, _, net_nodes in bucket:
                dist = math.hypot(ax - bax, ay - bay)
                if dist < best_dist:
                    best_dist = dist
                    best_net = net_nodes
        else:
            continue

        if best_net is not None:
            if current_net is not None:
                child.remove(current_net)
            child.append(best_net[0])
            changed = True

    return changed


def _assign_empty_nets_to_zero(output_sexp) -> bool:
    """Replace remaining (net "") on segments/vias with (net 0).

    After key-based and proximity-based restore passes, any segments or
    vias still carrying (net "") are new geometry created by kicad-cli
    zone fill that had no snapshot entry.  Rather than leaving the empty
    string (which causes DRC errors), assign them (net 0) (the
    unconnected net) — a valid KiCad net that will not trigger DRC
    empty-net violations.

    Returns True if any element was modified.
    """
    from kicad_tools.sexp import SExp

    changed = False
    for child in output_sexp.children:
        if child.name not in ("segment", "via"):
            continue
        net_node = child.get("net")
        if net_node is None:
            continue
        # Check for (net "") — empty string net
        net_str = net_node.get_string(0)
        if net_str is not None and net_str == "":
            # Also check there is no numeric value (pure empty string)
            if net_node.get_int(0) is None:
                child.remove(net_node)
                child.append(SExp.list("net", 0))
                changed = True
    return changed


def _strip_dual_atom_nets(output_sexp) -> bool:
    """Strip trailing name from ``(net N "name")`` on segments and vias.

    KiCad 9 rejects dual-atom ``(net N "name")`` format on segments and
    vias — it expects ``(net N)`` only.  kicad-cli DRC may rewrite nets
    into dual-atom format.  This final pass ensures all segments/vias
    have numeric-only net nodes.

    Returns True if any element was modified.
    """
    from kicad_tools.sexp import SExp

    changed = False
    for child in output_sexp.children:
        if child.name not in ("segment", "via"):
            continue
        net_node = child.get("net")
        if net_node is None:
            continue
        net_num = net_node.get_int(0)
        if net_num is not None and len(net_node.children) > 1:
            child.remove(net_node)
            child.append(SExp.list("net", net_num))
            changed = True
    return changed


def _resolve_name_only_nets(output_sexp) -> bool:
    """Resolve name-only ``(net "NAME")`` on segments/vias to ``(net N)``.

    kicad-cli zone fill may create new segments or vias with name-only
    net references like ``(net "GND")`` instead of ``(net 2)``.  These
    won't match any snapshot entry, so the key-based and proximity
    restore passes leave them untouched.

    This pass builds a name→number lookup from the PCB's net declarations
    and resolves any remaining name-only references.

    Returns True if any element was modified.
    """
    from kicad_tools.sexp import SExp

    # Build name → number mapping from header net declarations.
    name_to_number: dict[str, int] = {}
    for child in output_sexp.children:
        if child.name == "net":
            net_num = child.get_int(0)
            net_name = child.get_string(1) or ""
            if net_num is not None and net_num != 0 and net_name:
                name_to_number[net_name] = net_num

    if not name_to_number:
        return False

    changed = False
    for child in output_sexp.children:
        if child.name not in ("segment", "via"):
            continue
        net_node = child.get("net")
        if net_node is None:
            continue
        # Skip if already has a numeric ID
        if net_node.get_int(0) is not None:
            continue
        # Name-only format: (net "GND")
        net_name = net_node.get_string(0)
        if net_name and net_name in name_to_number:
            child.remove(net_node)
            child.append(SExp.list("net", name_to_number[net_name]))
            changed = True
    return changed


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
                if not _has_canonical_net(current_net):
                    if current_net is not None:
                        pad_node.remove(current_net)
                    pad_node.append(element_nets[key][0])
                    modified = True

        # Restore segment and via nets at the top level.
        # Try UUID-based key first (immune to coordinate displacement),
        # then fall back to geometry-based key (handles UUID regeneration).
        for child in output_sexp.children:
            if child.name in ("segment", "via"):
                current_net = child.get("net")
                if _has_canonical_net(current_net):
                    continue

                # Try UUID-based lookup first.
                snapshot_net = None
                elem_uuid = _get_element_uuid(child)
                if elem_uuid:
                    uuid_key = f"uuid:{elem_uuid}"
                    if uuid_key in element_nets:
                        snapshot_net = element_nets[uuid_key]

                # Fall back to geometry-based lookup.
                if snapshot_net is None:
                    geo_key = _make_segment_via_key(child)
                    if geo_key and geo_key in element_nets:
                        snapshot_net = element_nets[geo_key]

                if snapshot_net is not None:
                    if current_net is not None:
                        child.remove(current_net)
                    child.append(snapshot_net[0])
                    modified = True

        # --- Fallback pass for remaining unmatched (net "") elements ---
        # After the primary key-based restore, some segments/vias may still
        # have (net "") due to coordinate drift beyond the rounding threshold
        # or geometry created by kicad-cli during fill.  Use spatial proximity
        # to find the nearest snapshotted element on the same layer.
        if _fallback_restore_by_proximity(output_sexp, element_nets):
            modified = True

        # --- Final pass: assign remaining (net "") to (net 0) ---
        # After key-based restore and proximity fallback, any segments/vias
        # still carrying (net "") are new geometry created by zone fill
        # with no snapshot entry.  Assign them (net 0) (unconnected) to
        # prevent DRC errors from empty-string net corruption.
        if _assign_empty_nets_to_zero(output_sexp):
            modified = True

    # --- Resolve name-only (net "NAME") on segments/vias ---
    # kicad-cli zone fill may create new segments/vias with name-only net
    # references that have no snapshot entry.  Resolve them using the
    # PCB's net declarations.
    if _resolve_name_only_nets(output_sexp):
        modified = True

    # --- Strip dual-atom (net N "name") from segments/vias ---
    # KiCad 9 rejects dual-atom format on segments/vias.  kicad-cli DRC
    # may rewrite (net N) → (net N "name"); this pass ensures only (net N).
    # Runs unconditionally (not just when element_nets is populated) since
    # the corruption can also come from the router output or other tools.
    if _strip_dual_atom_nets(output_sexp):
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


def run_pcb_export_svg(
    pcb_path: Path,
    output_path: Path,
    layers: list[str],
    black_and_white: bool = False,
    theme: str | None = None,
    timeout: int = 120,
    kicad_cli: Path | None = None,
) -> KiCadCLIResult:
    """Export a 2D layer plot of a PCB to an SVG using ``kicad-cli pcb export svg``.

    KiCad 10 removed the raster ``pcb export png`` subcommand entirely, so the
    2D layer plots are produced as scalable SVGs instead (a better web format
    for ``<img src>`` anyway). ``--mode-single`` writes the full output path as a
    single file (avoiding the KiCad-9 deprecation warning that flips the default
    to directory output); ``--page-size-mode 2`` and ``--fit-page-to-board`` keep
    the plot trimmed to the board content.

    Args:
        pcb_path: Path to the ``.kicad_pcb`` file to plot.
        output_path: Where to write the SVG.
        layers: Layer names to render (e.g. ``["F.Cu", "F.Silkscreen", "Edge.Cuts"]``).
        black_and_white: Render in black & white instead of color.
        theme: Optional KiCad color theme name.
        timeout: Subprocess timeout in seconds.
        kicad_cli: Path to kicad-cli (auto-detected when not provided).

    Returns:
        KiCadCLIResult with success status and output path.
    """
    if kicad_cli is None:
        kicad_cli = find_kicad_cli()
        if kicad_cli is None:
            return KiCadCLIResult(
                success=False,
                stderr="kicad-cli not found. Install KiCad 8 from https://www.kicad.org/download/",
            )

    cmd = [
        str(kicad_cli),
        "pcb",
        "export",
        "svg",
        "--mode-single",
        "--output",
        str(output_path),
        "--layers",
        ",".join(layers),
        "--page-size-mode",
        "2",  # fit page to board content
        "--fit-page-to-board",
    ]

    if black_and_white:
        cmd.append("--black-and-white")
    if theme:
        cmd.extend(["--theme", theme])

    cmd.append(str(pcb_path))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

        if output_path.exists() and output_path.stat().st_size > 0:
            return KiCadCLIResult(
                success=True,
                output_path=output_path,
                stdout=result.stdout,
                stderr=result.stderr,
                return_code=result.returncode,
            )
        return KiCadCLIResult(
            success=False,
            stderr=result.stderr or "SVG export produced no output",
            return_code=result.returncode,
        )
    except FileNotFoundError as e:
        return KiCadCLIResult(success=False, stderr=f"kicad-cli not found: {e}")
    except subprocess.TimeoutExpired:
        return KiCadCLIResult(success=False, stderr=f"SVG export timed out after {timeout} seconds")
    except subprocess.SubprocessError as e:
        return KiCadCLIResult(success=False, stderr=f"Failed to export SVG: {e}")


def run_pcb_render(
    pcb_path: Path,
    output_path: Path,
    side: str = "front",
    quality: str = "high",
    timeout: int = 300,
    kicad_cli: Path | None = None,
) -> KiCadCLIResult:
    """Ray-trace a 3D render of a PCB to a PNG using ``kicad-cli pcb render``.

    ``kicad-cli pcb render`` was added in KiCad 8.0.4 and requires a display
    (or a virtual framebuffer such as ``xvfb-run`` on headless CI).

    Args:
        pcb_path: Path to the ``.kicad_pcb`` file to render.
        output_path: Where to write the PNG.
        side: Camera side — ``"front"`` or ``"back"`` (also accepts the other
            ``kicad-cli`` presets like ``top``/``bottom``/``left``/``right``).
        quality: Render quality preset (``basic``/``high``/``user``).
        timeout: Subprocess timeout in seconds (ray-tracing is slow).
        kicad_cli: Path to kicad-cli (auto-detected when not provided).

    Returns:
        KiCadCLIResult with success status and output path.
    """
    if kicad_cli is None:
        kicad_cli = find_kicad_cli()
        if kicad_cli is None:
            return KiCadCLIResult(
                success=False,
                stderr="kicad-cli not found. Install KiCad 8.0.4+ from https://www.kicad.org/download/",
            )

    cmd = [
        str(kicad_cli),
        "pcb",
        "render",
        "--output",
        str(output_path),
        "--side",
        side,
        "--quality",
        quality,
        str(pcb_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

        if output_path.exists() and output_path.stat().st_size > 0:
            return KiCadCLIResult(
                success=True,
                output_path=output_path,
                stdout=result.stdout,
                stderr=result.stderr,
                return_code=result.returncode,
            )
        return KiCadCLIResult(
            success=False,
            stderr=result.stderr or "3D render produced no output",
            return_code=result.returncode,
        )
    except FileNotFoundError as e:
        return KiCadCLIResult(success=False, stderr=f"kicad-cli not found: {e}")
    except subprocess.TimeoutExpired:
        return KiCadCLIResult(success=False, stderr=f"3D render timed out after {timeout} seconds")
    except subprocess.SubprocessError as e:
        return KiCadCLIResult(success=False, stderr=f"Failed to render 3D: {e}")


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
