"""
Gerber export using kicad-cli.

Wraps kicad-cli for generating Gerber files with manufacturer presets.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.progress import ProgressCallback

from kicad_tools.exceptions import (
    ConfigurationError,
    ExportError,
)
from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.cli.runner import find_kicad_cli

logger = logging.getLogger(__name__)


def _pcb_has_unfilled_zones(pcb_path: Path) -> bool:
    """Return True if the PCB defines copper-pour zones with no fill data.

    Cheap text scan that avoids parsing the full S-expression: a properly
    filled zone contains ``(filled_polygon ...)`` children; an unfilled zone
    has only the outline definition.  Used by :meth:`GerberExporter._export_gerbers`
    to decide whether to invoke the safety-net fill pass before exporting
    Gerbers (issue #2516).

    The serializer wraps zones in two forms -- ``(zone (net ...)`` on a
    single line, or ``(zone\\n  (net ...)`` with a newline after the
    opening token -- so we accept either.
    """
    try:
        text = pcb_path.read_text()
    except OSError:
        return False
    has_zone = "(zone " in text or "(zone\n" in text or "(zone\t" in text
    if not has_zone:
        return False
    # If we have zones but no filled_polygon, the zones are unfilled and
    # the resulting Gerbers would lack G36..G37 polygon-fill regions.
    return "filled_polygon" not in text


@dataclass
class GerberConfig:
    """Configuration for Gerber export."""

    # Output settings
    output_dir: Path | None = None
    create_zip: bool = True
    zip_name: str = "gerbers.zip"

    # Layer selection
    layers: list[str] = field(default_factory=list)  # Empty = all copper + required
    include_edge_cuts: bool = True
    include_silkscreen: bool = True
    include_soldermask: bool = True
    include_solderpaste: bool = False

    # Format options
    use_protel_extensions: bool = True  # .GTL/.GBL vs .gbr
    use_aux_origin: bool = True
    subtract_soldermask: bool = False
    disable_aperture_macros: bool = False

    # Drill options
    generate_drill: bool = True
    drill_format: str = "excellon"  # excellon or gerber_x2
    merge_pth_npth: bool = False
    minimal_header: bool = False

    # Post-zip cleanup: when True (default), remove individual gerber and
    # drill files after creating the zip archive so only the zip remains.
    clean_after_zip: bool = True


@dataclass
class GerberManufacturerPreset:
    """Preset configuration for a specific manufacturer."""

    name: str
    config: GerberConfig
    layer_rename: dict[str, str] = field(default_factory=dict)


# Manufacturer presets
JLCPCB_PRESET = GerberManufacturerPreset(
    name="JLCPCB",
    config=GerberConfig(
        use_protel_extensions=True,
        use_aux_origin=True,
        include_solderpaste=False,
        generate_drill=True,
        merge_pth_npth=False,
        minimal_header=False,
    ),
    layer_rename={
        # JLCPCB preferred naming
        "F.Cu": "F_Cu",
        "B.Cu": "B_Cu",
        "F.SilkS": "F_Silkscreen",
        "B.SilkS": "B_Silkscreen",
        "F.Mask": "F_Mask",
        "B.Mask": "B_Mask",
        "Edge.Cuts": "Edge_Cuts",
    },
)

PCBWAY_PRESET = GerberManufacturerPreset(
    name="PCBWay",
    config=GerberConfig(
        use_protel_extensions=True,
        use_aux_origin=True,
        include_solderpaste=True,
        generate_drill=True,
        merge_pth_npth=False,
    ),
)

OSHPARK_PRESET = GerberManufacturerPreset(
    name="OSH Park",
    config=GerberConfig(
        use_protel_extensions=True,
        use_aux_origin=False,
        include_solderpaste=False,
        generate_drill=True,
        merge_pth_npth=True,  # OSH Park prefers merged drill
    ),
)

SEEED_PRESET = GerberManufacturerPreset(
    name="Seeed Fusion",
    config=GerberConfig(
        use_protel_extensions=True,
        use_aux_origin=True,
        include_solderpaste=True,
        generate_drill=True,
        merge_pth_npth=False,
        minimal_header=False,
    ),
    layer_rename={
        # Seeed Fusion preferred naming (Protel extensions)
        "F.Cu": "GTL",
        "B.Cu": "GBL",
        "In1.Cu": "G1",
        "In2.Cu": "G2",
        "F.SilkS": "GTO",
        "B.SilkS": "GBO",
        "F.Mask": "GTS",
        "B.Mask": "GBS",
        "F.Paste": "GTP",
        "B.Paste": "GBP",
        "Edge.Cuts": "GKO",
    },
)

MANUFACTURER_PRESETS: dict[str, GerberManufacturerPreset] = {
    "jlcpcb": JLCPCB_PRESET,
    "pcbway": PCBWAY_PRESET,
    "oshpark": OSHPARK_PRESET,
    "seeed": SEEED_PRESET,
}



def get_kicad_cli_version(kicad_cli: Path) -> str | None:
    """Get the version string from kicad-cli.

    Args:
        kicad_cli: Path to the kicad-cli executable.

    Returns:
        Version string like ``"10.0.1"`` or ``None`` if the version
        could not be determined.
    """
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


def get_drill_origin_value(kicad_cli: Path) -> str:
    """Return the correct ``--drill-origin`` value for the installed kicad-cli.

    KiCad 10 renamed the auxiliary-axis origin flag value from ``aux``
    to ``plot``.  Passing the wrong value causes kicad-cli to exit with
    *"Invalid origin mode specified"*.

    Returns:
        ``"plot"`` for KiCad 10+ or when the version cannot be
        determined (safe default), ``"aux"`` for KiCad 9 and earlier.
    """
    version_str = get_kicad_cli_version(kicad_cli)
    if version_str is None:
        # Cannot determine version; default to the modern value.
        return "plot"

    try:
        major = int(version_str.split(".")[0])
    except (ValueError, IndexError):
        return "plot"

    return "plot" if major >= 10 else "aux"


class GerberExporter:
    """
    Export Gerbers using kicad-cli.

    Example::

        exporter = GerberExporter("board.kicad_pcb")
        exporter.export_for_manufacturer("jlcpcb", "output/")

        # Or with custom config
        config = GerberConfig(include_solderpaste=True)
        exporter.export(config, "output/")
    """

    def __init__(self, pcb_path: str | Path):
        """
        Initialize the exporter.

        Args:
            pcb_path: Path to KiCad PCB file

        Raises:
            FileNotFoundError: If PCB file doesn't exist
            RuntimeError: If kicad-cli is not found
        """
        self.pcb_path = Path(pcb_path)
        if not self.pcb_path.exists():
            raise KiCadFileNotFoundError(
                "PCB file not found",
                context={"file": str(pcb_path)},
                suggestions=["Check that the file path is correct"],
            )

        self.kicad_cli = find_kicad_cli()
        if not self.kicad_cli:
            raise ConfigurationError(
                "kicad-cli not found",
                context={"searched": ["PATH", "/Applications/KiCad/KiCad.app/Contents/MacOS"]},
                suggestions=[
                    "Install KiCad 7.0 or later",
                    "Add kicad-cli to your PATH",
                ],
            )

    def export(
        self,
        config: GerberConfig | None = None,
        output_dir: str | Path | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> Path:
        """
        Export Gerbers with given configuration.

        Args:
            config: Export configuration
            output_dir: Output directory (overrides config.output_dir)
            progress_callback: Optional callback for progress reporting.
                Signature: (progress: float, message: str, cancelable: bool) -> bool
                Returns False to cancel, True to continue.

        Returns:
            Path to output (directory or zip file)
        """
        config = config or GerberConfig()
        out_dir = Path(output_dir) if output_dir else config.output_dir
        if out_dir is None:
            out_dir = self.pcb_path.parent / "gerbers"

        out_dir.mkdir(parents=True, exist_ok=True)

        # Calculate total steps for progress
        total_steps = 1  # Gerbers
        if config.generate_drill:
            total_steps += 1
        if config.create_zip:
            total_steps += 1
        current_step = 0

        # Export Gerbers
        if progress_callback is not None:
            if not progress_callback(current_step / total_steps, "Exporting Gerber files", True):
                return out_dir
        self._export_gerbers(config, out_dir)
        current_step += 1

        # Export drill files
        if config.generate_drill:
            if progress_callback is not None:
                if not progress_callback(current_step / total_steps, "Exporting drill files", True):
                    return out_dir
            self._export_drill(config, out_dir)
            current_step += 1

        # Create zip if requested
        if config.create_zip:
            if progress_callback is not None:
                if not progress_callback(current_step / total_steps, "Creating zip archive", True):
                    return out_dir
            zip_path = out_dir / config.zip_name
            self._create_zip(out_dir, zip_path)

            # Clean up individual files after zipping when requested
            if config.clean_after_zip:
                self._clean_after_zip(out_dir, zip_path)

            if progress_callback is not None:
                progress_callback(1.0, f"Export complete: {zip_path.name}", False)
            return zip_path

        if progress_callback is not None:
            progress_callback(1.0, "Export complete", False)
        return out_dir

    def export_for_manufacturer(
        self,
        manufacturer: str,
        output_dir: str | Path | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> Path:
        """
        Export Gerbers using manufacturer preset.

        Args:
            manufacturer: Manufacturer ID (jlcpcb, pcbway, oshpark)
            output_dir: Output directory
            progress_callback: Optional callback for progress reporting.

        Returns:
            Path to output (directory or zip file)

        Raises:
            ValueError: If manufacturer is not supported
        """
        preset = MANUFACTURER_PRESETS.get(manufacturer.lower())
        if preset is None:
            available = list(MANUFACTURER_PRESETS.keys())
            raise ConfigurationError(
                f"Unknown manufacturer: {manufacturer}",
                context={"manufacturer": manufacturer, "available": available},
                suggestions=[f"Use one of: {', '.join(available)}"],
            )

        logger.info(f"Exporting Gerbers for {preset.name}")
        return self.export(preset.config, output_dir, progress_callback=progress_callback)

    def _export_gerbers(self, config: GerberConfig, output_dir: Path) -> None:
        """Export Gerber files using kicad-cli."""
        # Safety-net fill (issue #2516): if the PCB has zone definitions
        # but no fill polygons, the resulting Gerbers would contain zero
        # G36..G37 polygon-fill regions and the manufactured board would
        # lack plane copper.  Fill zones into a temp PCB so we never
        # silently mutate the user's file, then export Gerbers from the
        # temp file.
        pcb_for_export = self.pcb_path
        tmpdir: tempfile.TemporaryDirectory[str] | None = None
        try:
            if _pcb_has_unfilled_zones(self.pcb_path):
                from kicad_tools.cli.runner import run_fill_zones

                tmpdir = tempfile.TemporaryDirectory(prefix="kct_gerber_fill_")
                # Reuse the original filename so kicad-cli's per-layer
                # output naming is unaffected.
                filled_pcb = Path(tmpdir.name) / self.pcb_path.name
                logger.info(
                    "Gerber export: PCB has unfilled zones; filling to temp file %s",
                    filled_pcb,
                )
                fill_result = run_fill_zones(
                    self.pcb_path,
                    output_path=filled_pcb,
                    kicad_cli=self.kicad_cli,
                )
                if fill_result.success and filled_pcb.exists():
                    pcb_for_export = filled_pcb
                else:
                    # Non-fatal: fall back to the unfilled PCB and let the
                    # downstream Gerber export proceed.  The user will see
                    # missing plane copper in the Gerbers but the export
                    # itself will still succeed.
                    logger.warning(
                        "Gerber export: zone fill failed (%s); Gerbers may lack plane copper",
                        fill_result.stderr or "(no stderr)",
                    )

            self._export_gerbers_impl(config, output_dir, pcb_for_export)
        finally:
            if tmpdir is not None:
                tmpdir.cleanup()

    def _export_gerbers_impl(
        self,
        config: GerberConfig,
        output_dir: Path,
        pcb_path: Path,
    ) -> None:
        """Invoke ``kicad-cli pcb export gerbers`` against ``pcb_path``."""
        cmd = [
            str(self.kicad_cli),
            "pcb",
            "export",
            "gerbers",
            str(pcb_path),
            "--output",
            str(output_dir) + "/",
        ]

        # Add options
        if not config.use_protel_extensions:
            cmd.append("--no-protel-ext")

        if config.use_aux_origin:
            cmd.append("--use-drill-file-origin")

        if config.subtract_soldermask:
            cmd.append("--subtract-soldermask")

        if config.disable_aperture_macros:
            cmd.append("--disable-aperture-macros")

        # Layer selection - kicad-cli 9.x requires comma-separated list
        layers = config.layers if config.layers else self._get_default_layers(config)
        if layers:
            cmd.extend(["--layers", ",".join(layers)])

        logger.debug(f"Running: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
            )
            logger.debug(f"kicad-cli output: {result.stdout}")
        except subprocess.CalledProcessError as e:
            # Capture both stdout and stderr - kicad-cli may output errors to either
            error_output = e.stderr.strip() if e.stderr else ""
            stdout_output = e.stdout.strip() if e.stdout else ""
            combined_output = error_output or stdout_output or "No error output captured"

            logger.error(f"kicad-cli failed (exit code {e.returncode}): {combined_output}")
            raise ExportError(
                "Gerber export failed",
                context={
                    "pcb": str(self.pcb_path),
                    "exit_code": e.returncode,
                    "stderr": error_output or "(empty)",
                    "stdout": stdout_output or "(empty)",
                },
                suggestions=[
                    "Check the KiCad log for details",
                    "Verify the PCB file is valid and can be opened in KiCad",
                    "Ensure all layers referenced exist in the PCB",
                ],
            )

    def _export_drill(self, config: GerberConfig, output_dir: Path) -> None:
        """Export drill files using kicad-cli."""
        cmd = [
            str(self.kicad_cli),
            "pcb",
            "export",
            "drill",
            str(self.pcb_path),
            "--output",
            str(output_dir) + "/",
            "--format",
            config.drill_format,
        ]

        if config.merge_pth_npth:
            cmd.append("--merge-npth")

        if config.minimal_header:
            cmd.append("--minimal-header")

        if config.use_aux_origin:
            origin_value = get_drill_origin_value(self.kicad_cli)
            cmd.append("--drill-origin")
            cmd.append(origin_value)

        logger.debug(f"Running: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
            )
            logger.debug(f"kicad-cli output: {result.stdout}")
        except subprocess.CalledProcessError as e:
            # Capture both stdout and stderr - kicad-cli may output errors to either
            error_output = e.stderr.strip() if e.stderr else ""
            stdout_output = e.stdout.strip() if e.stdout else ""
            combined_output = error_output or stdout_output or "No error output captured"

            logger.error(f"kicad-cli failed (exit code {e.returncode}): {combined_output}")
            raise ExportError(
                "Drill export failed",
                context={
                    "pcb": str(self.pcb_path),
                    "exit_code": e.returncode,
                    "stderr": error_output or "(empty)",
                    "stdout": stdout_output or "(empty)",
                },
                suggestions=[
                    "Check the KiCad log for details",
                    "Verify the PCB file is valid and can be opened in KiCad",
                ],
            )

    def _get_default_layers(self, config: GerberConfig) -> list[str]:
        """Get default layers to export based on config."""
        layers = ["F.Cu", "B.Cu"]  # Always include copper

        # Add inner copper layers if present (detected from PCB)
        # For now, assume 2-layer. Could parse PCB to detect actual layers.

        if config.include_silkscreen:
            layers.extend(["F.SilkS", "B.SilkS"])

        if config.include_soldermask:
            layers.extend(["F.Mask", "B.Mask"])

        if config.include_solderpaste:
            layers.extend(["F.Paste", "B.Paste"])

        if config.include_edge_cuts:
            layers.append("Edge.Cuts")

        return layers

    @staticmethod
    def _clean_after_zip(source_dir: Path, zip_path: Path) -> None:
        """Remove individual gerber/drill files after creating the zip.

        Removes all files in *source_dir* except the zip archive itself,
        leaving only the zip in the output directory.
        """
        for file in list(source_dir.iterdir()):
            if file.is_file() and file != zip_path:
                file.unlink()
                logger.debug(f"Cleaned up: {file.name}")
        logger.info(f"Cleaned individual gerber files, kept {zip_path.name}")

    def _create_zip(self, source_dir: Path, zip_path: Path) -> None:
        """Create zip file from directory contents."""
        # Remove existing zip
        if zip_path.exists():
            zip_path.unlink()

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in source_dir.iterdir():
                if file.is_file() and file != zip_path:
                    zf.write(file, file.name)

        logger.info(f"Created {zip_path}")


def export_gerbers(
    pcb_path: str | Path,
    manufacturer: str = "jlcpcb",
    output_dir: str | Path | None = None,
    progress_callback: ProgressCallback | None = None,
) -> Path:
    """
    Convenience function to export Gerbers.

    Args:
        pcb_path: Path to KiCad PCB file
        manufacturer: Manufacturer ID or "generic"
        output_dir: Output directory
        progress_callback: Optional callback for progress reporting.
            Signature: (progress: float, message: str, cancelable: bool) -> bool
            Returns False to cancel, True to continue.

    Returns:
        Path to output (directory or zip file)
    """
    exporter = GerberExporter(pcb_path)

    if manufacturer.lower() in MANUFACTURER_PRESETS:
        return exporter.export_for_manufacturer(
            manufacturer, output_dir, progress_callback=progress_callback
        )
    else:
        return exporter.export(output_dir=output_dir, progress_callback=progress_callback)
