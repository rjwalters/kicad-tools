"""
Gerber export using kicad-cli.

Wraps kicad-cli for generating Gerber files with manufacturer presets.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class GerberConfig:
    """Configuration for Gerber export."""

    # Output settings
    output_dir: Optional[Path] = None
    create_zip: bool = True
    zip_name: str = "gerbers.zip"

    # Layer selection
    layers: List[str] = field(default_factory=list)  # Empty = all copper + required
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


@dataclass
class ManufacturerPreset:
    """Preset configuration for a specific manufacturer."""

    name: str
    config: GerberConfig
    layer_rename: Dict[str, str] = field(default_factory=dict)


# Manufacturer presets
JLCPCB_PRESET = ManufacturerPreset(
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

PCBWAY_PRESET = ManufacturerPreset(
    name="PCBWay",
    config=GerberConfig(
        use_protel_extensions=True,
        use_aux_origin=True,
        include_solderpaste=True,
        generate_drill=True,
        merge_pth_npth=False,
    ),
)

OSHPARK_PRESET = ManufacturerPreset(
    name="OSH Park",
    config=GerberConfig(
        use_protel_extensions=True,
        use_aux_origin=False,
        include_solderpaste=False,
        generate_drill=True,
        merge_pth_npth=True,  # OSH Park prefers merged drill
    ),
)

MANUFACTURER_PRESETS: Dict[str, ManufacturerPreset] = {
    "jlcpcb": JLCPCB_PRESET,
    "pcbway": PCBWAY_PRESET,
    "oshpark": OSHPARK_PRESET,
}


def find_kicad_cli() -> Optional[Path]:
    """Find kicad-cli executable."""
    # Check PATH first
    cli = shutil.which("kicad-cli")
    if cli:
        return Path(cli)

    # Check common locations
    common_paths = [
        # macOS
        Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"),
        # Linux
        Path("/usr/bin/kicad-cli"),
        Path("/usr/local/bin/kicad-cli"),
        # Windows
        Path("C:/Program Files/KiCad/8.0/bin/kicad-cli.exe"),
        Path("C:/Program Files/KiCad/7.0/bin/kicad-cli.exe"),
    ]

    for path in common_paths:
        if path.exists():
            return path

    return None


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
            raise FileNotFoundError(f"PCB file not found: {pcb_path}")

        self.kicad_cli = find_kicad_cli()
        if not self.kicad_cli:
            raise RuntimeError(
                "kicad-cli not found. Please install KiCad 7.0+ or add kicad-cli to PATH."
            )

    def export(
        self,
        config: Optional[GerberConfig] = None,
        output_dir: Optional[str | Path] = None,
    ) -> Path:
        """
        Export Gerbers with given configuration.

        Args:
            config: Export configuration
            output_dir: Output directory (overrides config.output_dir)

        Returns:
            Path to output (directory or zip file)
        """
        config = config or GerberConfig()
        out_dir = Path(output_dir) if output_dir else config.output_dir
        if out_dir is None:
            out_dir = self.pcb_path.parent / "gerbers"

        out_dir.mkdir(parents=True, exist_ok=True)

        # Export Gerbers
        self._export_gerbers(config, out_dir)

        # Export drill files
        if config.generate_drill:
            self._export_drill(config, out_dir)

        # Create zip if requested
        if config.create_zip:
            zip_path = out_dir / config.zip_name
            self._create_zip(out_dir, zip_path)
            return zip_path

        return out_dir

    def export_for_manufacturer(
        self,
        manufacturer: str,
        output_dir: Optional[str | Path] = None,
    ) -> Path:
        """
        Export Gerbers using manufacturer preset.

        Args:
            manufacturer: Manufacturer ID (jlcpcb, pcbway, oshpark)
            output_dir: Output directory

        Returns:
            Path to output (directory or zip file)

        Raises:
            ValueError: If manufacturer is not supported
        """
        preset = MANUFACTURER_PRESETS.get(manufacturer.lower())
        if preset is None:
            available = ", ".join(MANUFACTURER_PRESETS.keys())
            raise ValueError(f"Unknown manufacturer: {manufacturer}. Available: {available}")

        logger.info(f"Exporting Gerbers for {preset.name}")
        return self.export(preset.config, output_dir)

    def _export_gerbers(self, config: GerberConfig, output_dir: Path) -> None:
        """Export Gerber files using kicad-cli."""
        cmd = [
            str(self.kicad_cli),
            "pcb",
            "export",
            "gerbers",
            str(self.pcb_path),
            "--output", str(output_dir) + "/",
        ]

        # Add options
        if config.use_protel_extensions:
            cmd.append("--use-drill-file-origin")

        if config.use_aux_origin:
            cmd.append("--use-drill-file-origin")

        if config.subtract_soldermask:
            cmd.append("--subtract-soldermask")

        if config.disable_aperture_macros:
            cmd.append("--disable-aperture-macros")

        # Layer selection
        layers = config.layers if config.layers else self._get_default_layers(config)
        for layer in layers:
            cmd.extend(["--layers", layer])

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
            logger.error(f"kicad-cli failed: {e.stderr}")
            raise RuntimeError(f"Gerber export failed: {e.stderr}")

    def _export_drill(self, config: GerberConfig, output_dir: Path) -> None:
        """Export drill files using kicad-cli."""
        cmd = [
            str(self.kicad_cli),
            "pcb",
            "export",
            "drill",
            str(self.pcb_path),
            "--output", str(output_dir) + "/",
            "--format", config.drill_format,
        ]

        if config.merge_pth_npth:
            cmd.append("--merge-npth")

        if config.minimal_header:
            cmd.append("--minimal-header")

        if config.use_aux_origin:
            cmd.append("--drill-origin")
            cmd.append("aux")

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
            logger.error(f"kicad-cli failed: {e.stderr}")
            raise RuntimeError(f"Drill export failed: {e.stderr}")

    def _get_default_layers(self, config: GerberConfig) -> List[str]:
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
    output_dir: Optional[str | Path] = None,
) -> Path:
    """
    Convenience function to export Gerbers.

    Args:
        pcb_path: Path to KiCad PCB file
        manufacturer: Manufacturer ID or "generic"
        output_dir: Output directory

    Returns:
        Path to output (directory or zip file)
    """
    exporter = GerberExporter(pcb_path)

    if manufacturer.lower() in MANUFACTURER_PRESETS:
        return exporter.export_for_manufacturer(manufacturer, output_dir)
    else:
        return exporter.export(output_dir=output_dir)
