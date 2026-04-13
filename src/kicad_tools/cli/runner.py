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
    """
    import os

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
