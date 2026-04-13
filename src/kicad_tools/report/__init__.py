"""Report generation subpackage for kicad-tools.

Provides tools for generating report figures (PCB renders, schematic
screenshots) and structured manifests for design review documents.
"""

from kicad_tools.report.figures import FigureEntry, ReportFigureGenerator

__all__ = ["FigureEntry", "ReportFigureGenerator"]
