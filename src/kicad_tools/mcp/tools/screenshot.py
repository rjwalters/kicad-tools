"""Screenshot/visualization pipeline for MCP AI feedback.

Provides tools to capture board (and optionally schematic) images
via kicad-cli SVG export, convert to PNG, and return base64-encoded
images suitable for vision API consumption.
"""

from __future__ import annotations

import base64
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from kicad_tools.cli.runner import find_kicad_cli

logger = logging.getLogger(__name__)

# Default layers for a useful composite board view
DEFAULT_LAYERS = ["F.Cu", "B.Cu", "Edge.Cuts", "F.SilkS", "B.SilkS", "F.Mask", "B.Mask"]

# Named layer presets
LAYER_PRESETS: dict[str, list[str]] = {
    "default": DEFAULT_LAYERS,
    "copper": ["F.Cu", "B.Cu", "Edge.Cuts"],
    "assembly": ["F.Cu", "B.Cu", "Edge.Cuts", "F.SilkS", "B.SilkS"],
    "front": ["F.Cu", "Edge.Cuts", "F.SilkS", "F.Mask"],
    "back": ["B.Cu", "Edge.Cuts", "B.SilkS", "B.Mask"],
}

# Maximum image dimension for Claude vision API
MAX_VISION_API_PX = 1568

KICAD_INSTALL_URL = "https://www.kicad.org/download/"


def _check_cairosvg() -> bool:
    """Check if cairosvg is available and the native cairo library is loadable.

    A bare ``import cairosvg`` succeeds even when the underlying C library
    (``libcairo``) is missing from the dynamic linker's search path.  The
    ``OSError`` only fires when ``cairosvg`` actually tries to call into the
    native library.  We therefore do a lightweight probe render to surface
    that failure early, before any real work begins.
    """
    try:
        import cairosvg

        # Probe: calling svg2png with a trivial SVG document exposes a
        # missing native cairo library (raises OSError on macOS/Linux when
        # libcairo is not on the dynamic linker path).
        cairosvg.svg2png(bytestring=b"<svg xmlns='http://www.w3.org/2000/svg'/>")
        return True
    except (ImportError, OSError):
        return False


def _resolve_layers(layers: list[str] | str | None) -> list[str]:
    """Resolve layer specification to a list of layer names.

    Args:
        layers: List of layer names, a preset name string, or None for defaults.

    Returns:
        List of KiCad layer names.
    """
    if layers is None:
        return DEFAULT_LAYERS

    if isinstance(layers, str):
        if layers in LAYER_PRESETS:
            return LAYER_PRESETS[layers]
        # Treat as comma-separated layer list
        return [layer.strip() for layer in layers.split(",")]

    return layers


def _export_svg(
    pcb_path: Path,
    svg_path: Path,
    layers: list[str],
    black_and_white: bool = False,
    theme: str | None = None,
    kicad_cli: Path | None = None,
) -> tuple[bool, str]:
    """Export PCB to SVG using kicad-cli.

    Args:
        pcb_path: Path to .kicad_pcb file
        svg_path: Path for output SVG
        layers: List of layers to render
        black_and_white: Use black and white mode
        theme: KiCad color theme name
        kicad_cli: Path to kicad-cli executable

    Returns:
        Tuple of (success, error_message)
    """
    if kicad_cli is None:
        kicad_cli = find_kicad_cli()
        if kicad_cli is None:
            return False, (f"kicad-cli not found. Install KiCad 8+ from {KICAD_INSTALL_URL}")

    cmd = [
        str(kicad_cli),
        "pcb",
        "export",
        "svg",
        "--output",
        str(svg_path),
        "--layers",
        ",".join(layers),
        "--page-size-mode",
        "2",  # fit page to board
        "--mode-single",
    ]

    if black_and_white:
        cmd.append("--black-and-white")

    if theme:
        cmd.extend(["--theme", theme])

    cmd.append(str(pcb_path))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )

        if svg_path.exists() and svg_path.stat().st_size > 0:
            return True, ""

        stderr = result.stderr.strip() if result.stderr else "SVG export produced no output"
        return False, f"kicad-cli SVG export failed: {stderr}"

    except FileNotFoundError:
        return False, (
            f"kicad-cli not found at {kicad_cli}. Install KiCad 8+ from {KICAD_INSTALL_URL}"
        )
    except subprocess.TimeoutExpired:
        return False, "kicad-cli SVG export timed out after 60 seconds"
    except subprocess.SubprocessError as e:
        return False, f"kicad-cli SVG export failed: {e}"


def _svg_to_png(
    svg_path: Path,
    png_path: Path,
    max_size_px: int = MAX_VISION_API_PX,
) -> tuple[bool, str, int, int]:
    """Convert SVG to PNG with size constraints.

    Args:
        svg_path: Path to input SVG file
        png_path: Path for output PNG file
        max_size_px: Maximum dimension in pixels

    Returns:
        Tuple of (success, error_message, width, height)
    """
    try:
        import cairosvg
    except ImportError:
        return (
            False,
            (
                "cairosvg is required for SVG to PNG conversion. "
                "Install with: pip install 'kicad-tools[screenshot]'"
            ),
            0,
            0,
        )

    try:
        # First pass: render at native resolution to determine dimensions
        # Use a reasonable default output width to get aspect ratio
        png_bytes = cairosvg.svg2png(
            url=str(svg_path),
        )

        # Determine actual dimensions using the PNG header
        width, height = _png_dimensions(png_bytes)

        if width == 0 or height == 0:
            return False, "SVG rendered to empty image", 0, 0

        # Calculate scale factor to fit within max_size_px
        scale = 1.0
        if width > max_size_px or height > max_size_px:
            scale = max_size_px / max(width, height)

        target_width = int(width * scale)
        target_height = int(height * scale)

        # Ensure at least 1px
        target_width = max(1, target_width)
        target_height = max(1, target_height)

        # Re-render at target size
        png_bytes = cairosvg.svg2png(
            url=str(svg_path),
            output_width=target_width,
            output_height=target_height,
        )

        png_path.write_bytes(png_bytes)

        return True, "", target_width, target_height

    except Exception as e:
        return False, f"SVG to PNG conversion failed: {e}", 0, 0


def _png_dimensions(png_bytes: bytes) -> tuple[int, int]:
    """Extract width and height from PNG header bytes.

    PNG format: first 8 bytes are signature, then IHDR chunk
    starting at byte 16 with 4-byte width and 4-byte height.

    Args:
        png_bytes: Raw PNG file bytes

    Returns:
        Tuple of (width, height)
    """
    if len(png_bytes) < 24:
        return 0, 0

    # PNG IHDR chunk: width at offset 16, height at offset 20 (big-endian)
    width = int.from_bytes(png_bytes[16:20], byteorder="big")
    height = int.from_bytes(png_bytes[20:24], byteorder="big")

    return width, height


def screenshot_board(
    pcb_path: str,
    layers: list[str] | str | None = None,
    max_size_px: int = MAX_VISION_API_PX,
    output_path: str | None = None,
    black_and_white: bool = False,
    theme: str | None = None,
) -> dict[str, Any]:
    """Capture a screenshot of a KiCad PCB board.

    Exports the board as SVG using kicad-cli, converts to PNG using cairosvg,
    resizes to fit vision API constraints, and returns base64-encoded image data.

    Args:
        pcb_path: Path to .kicad_pcb file
        layers: Layer specification - list of layer names, a preset name
                ("default", "copper", "assembly", "front", "back"), or None
                for the default composite view.
        max_size_px: Maximum dimension in pixels (default: 1568 for Claude vision)
        output_path: Optional path to save PNG file to disk
        black_and_white: Use black and white rendering (good for compact AI images)
        theme: KiCad color theme name (optional)

    Returns:
        Dictionary with keys:
            success: bool
            image_base64: str (base64-encoded PNG data) or None
            width_px: int
            height_px: int
            layers_rendered: list[str]
            output_path: str or None
            error_message: str or None
    """
    # Validate PCB path
    pcb = Path(pcb_path)
    if not pcb.exists():
        return {
            "success": False,
            "image_base64": None,
            "width_px": 0,
            "height_px": 0,
            "layers_rendered": [],
            "output_path": None,
            "error_message": f"PCB file not found: {pcb_path}",
        }

    if pcb.suffix != ".kicad_pcb":
        return {
            "success": False,
            "image_base64": None,
            "width_px": 0,
            "height_px": 0,
            "layers_rendered": [],
            "output_path": None,
            "error_message": (f"Invalid file extension: {pcb.suffix} (expected .kicad_pcb)"),
        }

    # Check dependencies
    kicad_cli = find_kicad_cli()
    if kicad_cli is None:
        return {
            "success": False,
            "image_base64": None,
            "width_px": 0,
            "height_px": 0,
            "layers_rendered": [],
            "output_path": None,
            "error_message": (f"kicad-cli not found. Install KiCad 8+ from {KICAD_INSTALL_URL}"),
        }

    if not _check_cairosvg():
        return {
            "success": False,
            "image_base64": None,
            "width_px": 0,
            "height_px": 0,
            "layers_rendered": [],
            "output_path": None,
            "error_message": (
                "cairosvg is required for screenshot support. "
                "Install with: pip install 'kicad-tools[screenshot]'"
            ),
        }

    # Resolve layers
    resolved_layers = _resolve_layers(layers)

    # Create temp directory for intermediate files
    with tempfile.TemporaryDirectory(prefix="kicad_screenshot_") as tmpdir:
        svg_path = Path(tmpdir) / "board.svg"
        png_path = Path(tmpdir) / "board.png"

        # Step 1: Export SVG
        svg_ok, svg_err = _export_svg(
            pcb_path=pcb,
            svg_path=svg_path,
            layers=resolved_layers,
            black_and_white=black_and_white,
            theme=theme,
            kicad_cli=kicad_cli,
        )
        if not svg_ok:
            return {
                "success": False,
                "image_base64": None,
                "width_px": 0,
                "height_px": 0,
                "layers_rendered": resolved_layers,
                "output_path": None,
                "error_message": svg_err,
            }

        # Step 2: Convert SVG to PNG
        png_ok, png_err, width, height = _svg_to_png(
            svg_path=svg_path,
            png_path=png_path,
            max_size_px=max_size_px,
        )
        if not png_ok:
            return {
                "success": False,
                "image_base64": None,
                "width_px": 0,
                "height_px": 0,
                "layers_rendered": resolved_layers,
                "output_path": None,
                "error_message": png_err,
            }

        # Step 3: Read PNG and base64 encode
        png_bytes = png_path.read_bytes()
        image_base64 = base64.b64encode(png_bytes).decode("ascii")

        # Step 4: Optionally save to output path
        saved_path = None
        if output_path:
            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(png_bytes)
            saved_path = str(out)

    return {
        "success": True,
        "image_base64": image_base64,
        "width_px": width,
        "height_px": height,
        "layers_rendered": resolved_layers,
        "output_path": saved_path,
        "error_message": None,
    }


def screenshot_schematic(
    sch_path: str,
    max_size_px: int = MAX_VISION_API_PX,
    output_path: str | None = None,
    black_and_white: bool = False,
    theme: str | None = None,
) -> dict[str, Any]:
    """Capture a screenshot of a KiCad schematic.

    Exports the schematic as SVG using kicad-cli, converts to PNG,
    and returns base64-encoded image data.

    Args:
        sch_path: Path to .kicad_sch file
        max_size_px: Maximum dimension in pixels (default: 1568 for Claude vision)
        output_path: Optional path to save PNG file to disk
        black_and_white: Use black and white rendering
        theme: KiCad color theme name (optional)

    Returns:
        Dictionary with same shape as screenshot_board result.
    """
    sch = Path(sch_path)
    if not sch.exists():
        return {
            "success": False,
            "image_base64": None,
            "width_px": 0,
            "height_px": 0,
            "layers_rendered": [],
            "output_path": None,
            "error_message": f"Schematic file not found: {sch_path}",
        }

    if sch.suffix != ".kicad_sch":
        return {
            "success": False,
            "image_base64": None,
            "width_px": 0,
            "height_px": 0,
            "layers_rendered": [],
            "output_path": None,
            "error_message": (f"Invalid file extension: {sch.suffix} (expected .kicad_sch)"),
        }

    kicad_cli = find_kicad_cli()
    if kicad_cli is None:
        return {
            "success": False,
            "image_base64": None,
            "width_px": 0,
            "height_px": 0,
            "layers_rendered": [],
            "output_path": None,
            "error_message": (f"kicad-cli not found. Install KiCad 8+ from {KICAD_INSTALL_URL}"),
        }

    if not _check_cairosvg():
        return {
            "success": False,
            "image_base64": None,
            "width_px": 0,
            "height_px": 0,
            "layers_rendered": [],
            "output_path": None,
            "error_message": (
                "cairosvg is required for screenshot support. "
                "Install with: pip install 'kicad-tools[screenshot]'"
            ),
        }

    with tempfile.TemporaryDirectory(prefix="kicad_screenshot_") as tmpdir:
        tmp_path = Path(tmpdir)
        png_path = tmp_path / "schematic.png"

        # Export schematic to SVG.
        # kicad-cli sch export svg --output expects a DIRECTORY, not a file
        # path. It auto-generates filenames based on sheet names inside tmpdir.
        cmd = [
            str(kicad_cli),
            "sch",
            "export",
            "svg",
            "--output",
            str(tmp_path),
        ]

        if black_and_white:
            cmd.append("--black-and-white")

        if theme:
            cmd.extend(["--theme", theme])

        cmd.append(str(sch))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )

            # Find the generated SVG file(s) in the output directory.
            # For single-sheet schematics there will be one file; for
            # multi-sheet there may be several — use the first (main sheet).
            svg_files = sorted(tmp_path.glob("*.svg"))
            if not svg_files:
                stderr = result.stderr.strip() if result.stderr else "SVG export produced no output"
                return {
                    "success": False,
                    "image_base64": None,
                    "width_px": 0,
                    "height_px": 0,
                    "layers_rendered": [],
                    "output_path": None,
                    "error_message": f"kicad-cli schematic SVG export failed: {stderr}",
                }
            svg_path = svg_files[0]

        except FileNotFoundError:
            return {
                "success": False,
                "image_base64": None,
                "width_px": 0,
                "height_px": 0,
                "layers_rendered": [],
                "output_path": None,
                "error_message": (
                    f"kicad-cli not found at {kicad_cli}. Install KiCad 8+ from {KICAD_INSTALL_URL}"
                ),
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "image_base64": None,
                "width_px": 0,
                "height_px": 0,
                "layers_rendered": [],
                "output_path": None,
                "error_message": "kicad-cli schematic SVG export timed out after 60 seconds",
            }
        except subprocess.SubprocessError as e:
            return {
                "success": False,
                "image_base64": None,
                "width_px": 0,
                "height_px": 0,
                "layers_rendered": [],
                "output_path": None,
                "error_message": f"kicad-cli schematic SVG export failed: {e}",
            }

        # Convert SVG to PNG
        png_ok, png_err, width, height = _svg_to_png(
            svg_path=svg_path,
            png_path=png_path,
            max_size_px=max_size_px,
        )
        if not png_ok:
            return {
                "success": False,
                "image_base64": None,
                "width_px": 0,
                "height_px": 0,
                "layers_rendered": [],
                "output_path": None,
                "error_message": png_err,
            }

        # Read and encode
        png_bytes = png_path.read_bytes()
        image_base64 = base64.b64encode(png_bytes).decode("ascii")

        saved_path = None
        if output_path:
            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(png_bytes)
            saved_path = str(out)

    return {
        "success": True,
        "image_base64": image_base64,
        "width_px": width,
        "height_px": height,
        "layers_rendered": [],
        "output_path": saved_path,
        "error_message": None,
    }
