"""Screenshot generation for report figures.

Generates PCB renders (front, back, copper, assembly presets) and
per-sheet schematic screenshots for inclusion in design review reports.

Reuses the screenshot infrastructure from ``kicad_tools.mcp.tools.screenshot``
(``screenshot_board``, ``_svg_to_png``, layer presets).
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.mcp.tools.screenshot import (
    _check_cairosvg,
    _svg_to_png,
    screenshot_board,
)

logger = logging.getLogger(__name__)

#: Default maximum image dimension (px) for report figures.
#: Higher than the vision-API cap (1568 px) because report images are
#: embedded in documents, not sent to a vision model.
REPORT_MAX_SIZE_PX = 3000

#: PCB presets to render, in order.
#: Each tuple is (preset_name, output_filename, caption).
_PCB_PRESETS: list[tuple[str, str, str]] = [
    ("front", "pcb_front.png", "PCB Front"),
    ("back", "pcb_back.png", "PCB Back"),
    ("copper", "pcb_copper.png", "PCB Copper Layers"),
    ("assembly", "assembly.png", "Assembly View"),
]


@dataclass
class FigureEntry:
    """Manifest entry describing a single generated report figure."""

    filename: str
    """Filename relative to the output directory (e.g. ``pcb_front.png``)."""

    caption: str
    """Human-readable label (e.g. ``"PCB Front"``)."""

    figure_type: str
    """One of ``pcb_front``, ``pcb_back``, ``pcb_copper``, ``assembly``, or ``schematic``."""


class ReportFigureGenerator:
    """Generates report figures for PCB boards and schematics.

    Renders four PCB layer presets (front, back, copper, assembly) and
    one PNG per schematic sheet.  Returns a manifest of
    :class:`FigureEntry` objects describing the generated files.

    Parameters
    ----------
    max_size_px:
        Maximum image dimension in pixels.  Defaults to
        :data:`REPORT_MAX_SIZE_PX` (3000).
    """

    def __init__(self, max_size_px: int = REPORT_MAX_SIZE_PX) -> None:
        self.max_size_px = max_size_px

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_all(
        self,
        pcb_path: str | Path,
        sch_path: str | Path,
        output_dir: str | Path,
    ) -> list[FigureEntry]:
        """Generate all report figures and return a manifest.

        Parameters
        ----------
        pcb_path:
            Path to the ``.kicad_pcb`` file.
        sch_path:
            Path to the root ``.kicad_sch`` file.
        output_dir:
            Directory where PNG files will be written.  Created if it
            does not exist.

        Returns
        -------
        list[FigureEntry]
            Manifest entries for every successfully generated figure.
            Figures that fail to render are excluded (a warning is
            logged) so that partial results are still usable.

        Raises
        ------
        RuntimeError
            If ``kicad-cli`` or ``cairosvg`` is not available.
        """
        self._check_dependencies()

        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        entries: list[FigureEntry] = []
        entries.extend(self._generate_pcb_figures(Path(pcb_path), out))
        entries.extend(self._generate_schematic_figures(Path(sch_path), out))
        return entries

    # ------------------------------------------------------------------
    # Dependency checks
    # ------------------------------------------------------------------

    @staticmethod
    def _check_dependencies() -> None:
        """Raise :class:`RuntimeError` if required tools are missing."""
        kicad_cli = find_kicad_cli()
        if kicad_cli is None:
            raise RuntimeError(
                "kicad-cli not found. Install KiCad 8+ from https://www.kicad.org/download/"
            )

        if not _check_cairosvg():
            raise RuntimeError(
                "cairosvg is required for report figure generation. "
                "Install with: pip install 'kicad-tools[screenshot]'"
            )

    # ------------------------------------------------------------------
    # PCB rendering
    # ------------------------------------------------------------------

    def _generate_pcb_figures(
        self,
        pcb_path: Path,
        output_dir: Path,
    ) -> list[FigureEntry]:
        """Render the four PCB layer presets."""
        entries: list[FigureEntry] = []
        for preset, filename, caption in _PCB_PRESETS:
            out_path = output_dir / filename
            try:
                result = screenshot_board(
                    pcb_path=str(pcb_path),
                    layers=preset,
                    max_size_px=self.max_size_px,
                    output_path=str(out_path),
                )
                if result.get("success"):
                    figure_type = preset if preset != "assembly" else "assembly"
                    # Normalise preset name to figure_type enum
                    if preset in ("front", "back", "copper"):
                        figure_type = f"pcb_{preset}"
                    entries.append(
                        FigureEntry(
                            filename=filename,
                            caption=caption,
                            figure_type=figure_type,
                        )
                    )
                else:
                    logger.warning(
                        "Failed to render PCB preset '%s': %s",
                        preset,
                        result.get("error_message", "unknown error"),
                    )
            except Exception:
                logger.warning(
                    "Exception while rendering PCB preset '%s'",
                    preset,
                    exc_info=True,
                )
        return entries

    # ------------------------------------------------------------------
    # Schematic rendering (all sheets)
    # ------------------------------------------------------------------

    def _generate_schematic_figures(
        self,
        sch_path: Path,
        output_dir: Path,
    ) -> list[FigureEntry]:
        """Export every schematic sheet to PNG.

        Uses ``kicad-cli sch export svg`` which emits one SVG per sheet
        into a temporary directory.  Each SVG is then converted to PNG
        via ``_svg_to_png``.
        """
        kicad_cli = find_kicad_cli()
        if kicad_cli is None:
            logger.warning("kicad-cli not found; skipping schematic figures")
            return []

        entries: list[FigureEntry] = []
        try:
            with tempfile.TemporaryDirectory(prefix="kicad_sch_") as tmpdir:
                tmp = Path(tmpdir)
                cmd = [
                    str(kicad_cli),
                    "sch",
                    "export",
                    "svg",
                    "--output",
                    str(tmp),
                    str(sch_path),
                ]
                subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )

                svg_files: Sequence[Path] = sorted(tmp.glob("*.svg"))
                if not svg_files:
                    logger.warning(
                        "kicad-cli produced no SVG output for %s",
                        sch_path,
                    )
                    return []

                for svg_file in svg_files:
                    png_name = f"schematic_{svg_file.stem}.png"
                    png_path = output_dir / png_name
                    try:
                        ok, err, _w, _h = _svg_to_png(svg_file, png_path, self.max_size_px)
                        if ok:
                            entries.append(
                                FigureEntry(
                                    filename=png_name,
                                    caption=f"Schematic: {svg_file.stem}",
                                    figure_type="schematic",
                                )
                            )
                        else:
                            logger.warning(
                                "Failed to render schematic sheet '%s': %s",
                                svg_file.name,
                                err,
                            )
                    except Exception:
                        logger.warning(
                            "Exception while converting schematic SVG '%s'",
                            svg_file.name,
                            exc_info=True,
                        )
        except subprocess.TimeoutExpired:
            logger.warning(
                "kicad-cli schematic SVG export timed out for %s",
                sch_path,
            )
        except FileNotFoundError:
            logger.warning(
                "kicad-cli not found at %s",
                kicad_cli,
            )
        except Exception:
            logger.warning(
                "Unexpected error during schematic export",
                exc_info=True,
            )

        return entries
