"""Gerber export types for MCP tools.

Provides dataclasses and utilities for Gerber file export operations.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GerberFile:
    """Information about a generated Gerber file."""

    filename: str
    """Name of the generated file."""

    layer: str
    """KiCad layer name (e.g., 'F.Cu', 'B.Mask')."""

    file_type: str
    """File category: 'copper', 'soldermask', 'silkscreen', 'paste', 'outline', 'drill'."""

    size_bytes: int
    """File size in bytes."""


@dataclass
class GerberExportResult:
    """Result of a Gerber export operation."""

    success: bool
    """Whether the export completed successfully."""

    output_dir: str
    """Directory containing the exported files."""

    zip_file: str | None = None
    """Path to zip archive if created, None otherwise."""

    files: list[GerberFile] = field(default_factory=list)
    """List of generated files with metadata."""

    layer_count: int = 0
    """Number of copper layers in the board."""

    warnings: list[str] = field(default_factory=list)
    """Any warnings encountered during export."""

    error: str | None = None
    """Error message if success is False."""

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "success": self.success,
            "output_dir": self.output_dir,
            "zip_file": self.zip_file,
            "files": [
                {
                    "filename": f.filename,
                    "layer": f.layer,
                    "file_type": f.file_type,
                    "size_bytes": f.size_bytes,
                }
                for f in self.files
            ],
            "layer_count": self.layer_count,
            "warnings": self.warnings,
            "error": self.error,
        }


# Layer name to file type mapping
LAYER_FILE_TYPES: dict[str, str] = {
    "F.Cu": "copper",
    "B.Cu": "copper",
    "In1.Cu": "copper",
    "In2.Cu": "copper",
    "In3.Cu": "copper",
    "In4.Cu": "copper",
    "In5.Cu": "copper",
    "In6.Cu": "copper",
    "F.Mask": "soldermask",
    "B.Mask": "soldermask",
    "F.SilkS": "silkscreen",
    "B.SilkS": "silkscreen",
    "F.Paste": "paste",
    "B.Paste": "paste",
    "Edge.Cuts": "outline",
}


def get_file_type(layer: str) -> str:
    """Get the file type for a given layer name."""
    return LAYER_FILE_TYPES.get(layer, "other")
