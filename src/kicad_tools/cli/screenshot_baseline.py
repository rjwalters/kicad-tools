"""Visual baseline record/compare helpers for ``kct screenshot`` (issue #6912).

A baseline is a rendered PNG plus a ``<name>.baseline.json`` sidecar that
records the *renderer identity* (kicad-cli and cairosvg versions), the render
options and the image dimensions.  Comparing against a baseline made by a
different renderer would report rasterizer noise as design drift, so
:func:`compare_identity` surfaces every differing field and the caller reports
``renderer_mismatch`` instead of computing a drift number.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SIDECAR_SUFFIX = ".baseline.json"
SCHEMA_VERSION = 1

# A pixel counts as changed when any channel differs by more than this
# (0-255 scale).  Fixed on purpose: tolerance flags are out of scope.
CHANNEL_TOLERANCE = 8


class BaselineError(Exception):
    """A baseline is missing, unreadable or corrupt."""


def baseline_paths(directory: str | Path, input_path: str | Path) -> tuple[Path, Path]:
    """Return ``(png_path, sidecar_path)`` for ``input_path`` inside ``directory``."""
    name = Path(input_path).stem
    base = Path(directory)
    return base / f"{name}.png", base / f"{name}{SIDECAR_SUFFIX}"


def renderer_identity() -> dict[str, str | None]:
    """Versions of the tools that turn a design into pixels."""
    from kicad_tools.cli.runner import find_kicad_cli
    from kicad_tools.export.gerber import get_kicad_cli_version

    kicad_cli = find_kicad_cli()
    kicad_version = get_kicad_cli_version(kicad_cli) if kicad_cli is not None else None
    try:
        import cairosvg  # type: ignore[import-untyped]

        cairosvg_version: str | None = str(cairosvg.__version__)
    except Exception:  # missing or broken native cairo
        cairosvg_version = None
    return {"kicad_cli": kicad_version, "cairosvg": cairosvg_version}


def build_sidecar(
    renderer: dict[str, Any], options: dict[str, Any], width_px: int, height_px: int
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "renderer": dict(renderer),
        "options": dict(options),
        "width_px": int(width_px),
        "height_px": int(height_px),
    }


def write_sidecar(path: Path, sidecar: dict[str, Any]) -> None:
    path.write_text(json.dumps(sidecar, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_sidecar(png_path: Path, sidecar_path: Path) -> dict[str, Any]:
    """Load and validate a baseline; raise :class:`BaselineError` if unusable."""
    if not sidecar_path.is_file() or not png_path.is_file():
        missing = sidecar_path if not sidecar_path.is_file() else png_path
        raise BaselineError(f"Baseline not found: {missing}")
    try:
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BaselineError(f"Corrupt baseline sidecar {sidecar_path}: {exc}") from exc
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("renderer"), dict)
        or not isinstance(data.get("options"), dict)
        or not isinstance(data.get("width_px"), int)
        or not isinstance(data.get("height_px"), int)
    ):
        raise BaselineError(f"Corrupt baseline sidecar {sidecar_path}: missing required fields")
    return data


def compare_identity(baseline: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    """List fields whose baseline and current values differ (empty = comparable)."""
    diffs: list[dict[str, Any]] = []
    for section in ("renderer", "options"):
        old, new = baseline.get(section, {}), current.get(section, {})
        for key in sorted(set(old) | set(new)):
            if old.get(key) != new.get(key):
                diffs.append(
                    {"field": f"{section}.{key}", "baseline": old.get(key), "current": new.get(key)}
                )
    for key in ("width_px", "height_px"):
        if baseline.get(key) != current.get(key):
            diffs.append({"field": key, "baseline": baseline.get(key), "current": current.get(key)})
    return diffs


def pixel_drift(baseline_png: Path, current_png: Path) -> dict[str, Any]:
    """Fraction of pixels whose any channel differs by more than the tolerance."""
    import numpy as np
    from PIL import Image

    try:
        with Image.open(baseline_png) as a_img, Image.open(current_png) as b_img:
            a = np.asarray(a_img.convert("RGBA"), dtype=np.int16)
            b = np.asarray(b_img.convert("RGBA"), dtype=np.int16)
    except Exception as exc:
        raise BaselineError(f"Cannot read image for comparison: {exc}") from exc
    if a.shape != b.shape:
        raise BaselineError(
            f"Image dimensions differ: {a.shape[1]}x{a.shape[0]} vs {b.shape[1]}x{b.shape[0]}"
        )
    changed = int((np.abs(a - b).max(axis=2) > CHANNEL_TOLERANCE).sum())
    total = int(a.shape[0] * a.shape[1])
    return {
        "status": "identical" if changed == 0 else "changed",
        "drift_fraction": (changed / total) if total else 0.0,
        "changed_pixels": changed,
        "total_pixels": total,
    }
