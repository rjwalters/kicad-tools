"""KiCad CLI runner utility.

Provides functions to locate and run kicad-cli commands for
ERC validation, DRC validation, netlist export, and more.
"""

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def find_kicad_cli() -> Optional[Path]:
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
    output_path: Optional[Path] = None
    stdout: str = ""
    stderr: str = ""
    return_code: int = 0


def run_erc(
    schematic_path: Path,
    output_path: Optional[Path] = None,
    format: str = "json",
    severity_all: bool = True,
    kicad_cli: Optional[Path] = None,
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
    output_path: Optional[Path] = None,
    format: str = "json",
    schematic_parity: bool = True,
    kicad_cli: Optional[Path] = None,
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
    output_path: Optional[Path] = None,
    format: str = "kicad",
    kicad_cli: Optional[Path] = None,
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


def get_kicad_version(kicad_cli: Optional[Path] = None) -> Optional[str]:
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
