"""
Extracted image data model and utilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from kicad_tools.utils import ensure_parent_dir

if TYPE_CHECKING:
    pass


# Image classification types
IMAGE_CLASSIFICATIONS = [
    "pinout",  # Shows IC with pin labels
    "package",  # Mechanical dimensions
    "block_diagram",  # Internal architecture
    "schematic",  # Application circuit
    "graph",  # Electrical characteristics chart
    "timing",  # Timing diagram
    "other",  # Unclassified
]


@dataclass
class ExtractedImage:
    """
    Represents an image extracted from a PDF datasheet.

    Attributes:
        page: Page number where the image was found (1-indexed)
        index: Image index on the page (0-indexed)
        width: Image width in pixels
        height: Image height in pixels
        format: Image format (png, jpeg, etc.)
        data: Raw image bytes
        caption: Nearby text if detected (may be None)
        classification: Image type classification (pinout, package, etc.)
    """

    page: int
    index: int
    width: int
    height: int
    format: str
    data: bytes
    caption: str | None = None
    classification: str | None = None
    _xref: int = field(default=0, repr=False)  # Internal PDF reference

    @property
    def suggested_filename(self) -> str:
        """
        Generate a suggested filename for the image.

        Returns:
            A filename like 'page_1_img_0_pinout.png'
        """
        suffix = f"_{self.classification}" if self.classification else ""
        return f"page_{self.page}_img_{self.index}{suffix}.{self.format}"

    @property
    def size_kb(self) -> float:
        """Return image size in kilobytes."""
        return len(self.data) / 1024

    def save(self, path: str | Path) -> None:
        """
        Save the image to disk.

        Args:
            path: Output file path
        """
        path = Path(path)
        ensure_parent_dir(path).write_bytes(self.data)

    def __repr__(self) -> str:
        return (
            f"ExtractedImage(page={self.page}, index={self.index}, "
            f"{self.width}x{self.height} {self.format}, "
            f"classification={self.classification!r})"
        )


def classify_image(
    width: int,
    height: int,
    caption: str | None = None,
) -> str | None:
    """
    Attempt to classify an image based on its properties.

    This is a heuristic-based classification that looks at:
    - Image dimensions
    - Caption text (if available)

    Args:
        width: Image width in pixels
        height: Image height in pixels
        caption: Optional nearby caption text

    Returns:
        Classification string or None if unclassified
    """
    if caption:
        caption_lower = caption.lower()

        # Check for common keywords
        if any(kw in caption_lower for kw in ["pinout", "pin configuration", "pin assignment"]):
            return "pinout"
        if any(kw in caption_lower for kw in ["package", "dimension", "mechanical"]):
            return "package"
        if any(kw in caption_lower for kw in ["block diagram", "functional diagram"]):
            return "block_diagram"
        if any(kw in caption_lower for kw in ["application", "circuit", "schematic", "typical"]):
            return "schematic"
        if any(kw in caption_lower for kw in ["graph", "curve", "characteristic", "plot"]):
            return "graph"
        if any(kw in caption_lower for kw in ["timing", "waveform"]):
            return "timing"

    # Heuristics based on aspect ratio
    if width > 0 and height > 0:
        aspect = width / height

        # Package drawings tend to be square-ish
        if 0.8 < aspect < 1.2 and width > 300:
            # Could be pinout or package
            pass

        # Timing diagrams tend to be wide
        if aspect > 2.5:
            return "timing"

    return None
