"""Board metrics extractor — emit a normalized ``board.json`` per demo board.

This module aggregates already-computed manufacturing artifacts into a single,
stable ``board.json`` data contract consumed by the kicad-tools.org demo gallery
(Epic #3674, Phase 1, issue #3676).

It does **not** recompute anything from KiCad. All metrics are parsed from
artifacts that already exist under a board's ``output/manufacturing/`` directory:

* ``report.md``      -> routing %, DRC errors, layer count, board size, part
                        count, description, cost estimate.
* ``manifest.json``  -> board name and ``generated_at`` timestamp.
* ``bom_jlcpcb.csv`` -> fallback part count (row count minus header).
* ``kicad_project.zip`` -> downloadable manufacturing package path.
* ``../renders/*.png`` -> render image paths (written by ``kct render``, #3675).

Output is written to ``boards/<id>/output/board.json``.

board.json schema (v1)
----------------------

All fields except ``schema_version``, ``generated_at``, ``slug`` and ``status``
are OPTIONAL — they are omitted (never ``null``) when the source artifact is
absent or unparseable.

::

    {
      "$schema": "https://kicad-tools.org/schemas/board/v1.json",
      "schema_version": 1,
      "generated_at": "<ISO-8601 UTC timestamp>",
      "slug": "05-bldc-motor-controller",
      "name": "bldc_controller_routed",
      "description": "3-Phase Brushless DC Motor Driver",
      "layer_count": 4,
      "board_size_mm": {"width": 80.0, "height": 100.0},
      "part_count": 55,
      "nets_routed_pct": 82.1,
      "drc_violations": 14,
      "cost": {"per_board_usd": 9.16, "batch_qty": 5, "batch_total_usd": 45.78},
      "renders": {
        "pcb_front": "renders/pcb-front.svg",
        "pcb_back": "renders/pcb-back.svg",
        "3d_front": "renders/3d-front.png",
        "3d_back": "renders/3d-back.png"
      },
      "manufacturing_package": "manufacturing/kicad_project.zip",
      "manifest_generated_at": "2026-06-12T05:03:41.535120+00:00",
      "status": "ok"
    }

``status`` is one of:

* ``"ok"``           — ``output/manufacturing/`` exists and ``report.md`` parsed.
* ``"partial"``      — ``output/manufacturing/`` exists but ``report.md`` is
                        absent/unparseable (only identity + whatever fields we
                        could recover).
* ``"no_artifacts"`` — no ``output/manufacturing/`` directory at all.

Schema versioning policy: this schema is the Phase 2 (Astro site) data contract.
Field additions must be additive — no renames, no type changes. Bump
``schema_version`` only for breaking changes. See ``docs/board-json-schema.md``.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "extract_board_metrics",
    "emit_board_json",
    "main",
]

SCHEMA_VERSION = 1
SCHEMA_URL = "https://kicad-tools.org/schemas/board/v1.json"

# Render images written by `kct render` (#3675), relative to output/.
# Keys are the board.json field names; values are paths relative to output/.
RENDER_FILES = {
    "pcb_front": "renders/pcb-front.svg",
    "pcb_back": "renders/pcb-back.svg",
    "3d_front": "renders/3d-front.png",
    "3d_back": "renders/3d-back.png",
}

# --- report.md field regexes (machine-generated, stable format) -------------
_LAYERS_RE = re.compile(r"\|\s*Layers\s*\|\s*(\d+)\s*copper", re.IGNORECASE)
_BOARD_SIZE_RE = re.compile(r"\|\s*Board Size\s*\|\s*([\d.]+)\s*x\s*([\d.]+)\s*mm", re.IGNORECASE)
_FOOTPRINTS_RE = re.compile(r"\|\s*Footprints\s*\|\s*(\d+)", re.IGNORECASE)
_NETS_ROUTED_RE = re.compile(r"\|\s*Signal Net Completion\s*\|\s*([\d.]+)\s*%", re.IGNORECASE)
_DRC_ERRORS_RE = re.compile(
    r"##\s*DRC Status.*?\|\s*Errors\s*\|\s*(\d+)", re.IGNORECASE | re.DOTALL
)
_COST_TOTAL_RE = re.compile(r"Total \(estimated\)\D*~([\d.]+)\s*USD", re.IGNORECASE)
_COST_BATCH_QTY_RE = re.compile(r"\|\s*Batch Quantity\s*\|\s*(\d+)", re.IGNORECASE)
_COST_BATCH_TOTAL_RE = re.compile(r"Batch Total \(estimated\)\D*~([\d.]+)\s*USD", re.IGNORECASE)
# Front-matter title: lives between the leading `---` fences.
_TITLE_RE = re.compile(r'^title:\s*"?([^"\n]+)"?\s*$', re.MULTILINE)


def _parse_report_md(text: str, slug: str) -> dict:
    """Parse the machine-generated ``report.md`` into a partial metrics dict.

    Each field is parsed independently; a miss logs a warning (naming the slug
    and field) and is omitted rather than raising.
    """
    out: dict = {}

    m = _LAYERS_RE.search(text)
    if m:
        out["layer_count"] = int(m.group(1))
    else:
        logger.warning("board %s: could not parse layer_count from report.md", slug)

    m = _BOARD_SIZE_RE.search(text)
    if m:
        out["board_size_mm"] = {
            "width": float(m.group(1)),
            "height": float(m.group(2)),
        }
    else:
        logger.warning("board %s: could not parse board_size_mm from report.md", slug)

    m = _FOOTPRINTS_RE.search(text)
    if m:
        out["part_count"] = int(m.group(1))
    else:
        logger.warning("board %s: could not parse part_count from report.md", slug)

    m = _NETS_ROUTED_RE.search(text)
    if m:
        out["nets_routed_pct"] = float(m.group(1))
    else:
        logger.warning("board %s: could not parse nets_routed_pct from report.md", slug)

    m = _DRC_ERRORS_RE.search(text)
    if m:
        out["drc_violations"] = int(m.group(1))
    else:
        logger.warning("board %s: could not parse drc_violations from report.md", slug)

    # Description: prefer the Theory of Operation section's first non-empty
    # paragraph after the heading, falling back to the front-matter title.
    description = _parse_description(text)
    if description:
        out["description"] = description

    # Cost is optional — omit the whole block if the section is absent.
    cost = _parse_cost(text)
    if cost:
        out["cost"] = cost

    return out


def _parse_description(text: str) -> str | None:
    """Extract a human-readable board description from report.md.

    Uses the first non-empty line(s) under ``### Theory of Operation``; falls
    back to the front-matter ``title``.
    """
    theory_idx = text.find("### Theory of Operation")
    if theory_idx != -1:
        section = text[theory_idx + len("### Theory of Operation") :]
        # Stop at the next markdown heading.
        end = re.search(r"\n#{1,6}\s", section)
        if end:
            section = section[: end.start()]
        lines = [ln.strip() for ln in section.splitlines() if ln.strip()]
        if lines:
            # Join the leading description lines (skip the board-name line if a
            # richer sentence follows) into a single space-joined string.
            return " ".join(lines)

    m = _TITLE_RE.search(text)
    if m:
        return m.group(1).strip()
    return None


def _parse_cost(text: str) -> dict | None:
    """Parse the ``## Cost Estimate`` block; return None when absent."""
    if "## Cost Estimate" not in text:
        return None
    cost: dict = {}
    m = _COST_TOTAL_RE.search(text)
    if m:
        cost["per_board_usd"] = float(m.group(1))
    m = _COST_BATCH_QTY_RE.search(text)
    if m:
        cost["batch_qty"] = int(m.group(1))
    m = _COST_BATCH_TOTAL_RE.search(text)
    if m:
        cost["batch_total_usd"] = float(m.group(1))
    return cost or None


def _parse_manifest(manifest_path: Path, slug: str) -> dict:
    """Parse identity fields from ``manifest.json`` (board name + timestamp)."""
    out: dict = {}
    try:
        data = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("board %s: could not read manifest.json (%s)", slug, exc)
        return out

    board = data.get("board") or {}
    name = board.get("name")
    if name:
        out["name"] = name

    generated_at = data.get("generated_at")
    if generated_at:
        out["manifest_generated_at"] = generated_at

    return out


def _count_bom_parts(bom_path: Path, slug: str) -> int | None:
    """Count assembly parts as ``bom_jlcpcb.csv`` data rows (minus header)."""
    try:
        with bom_path.open(newline="") as fh:
            rows = list(csv.reader(fh))
    except OSError as exc:
        logger.warning("board %s: could not read %s (%s)", slug, bom_path.name, exc)
        return None
    # Drop the header row; ignore trailing blank lines.
    data_rows = [r for r in rows[1:] if any(cell.strip() for cell in r)]
    return len(data_rows)


def extract_board_metrics(board_dir: Path) -> dict:
    """Read existing artifacts under ``board_dir`` and return a board.json dict.

    Never raises on missing/partial artifacts — fields are omitted and the
    ``status`` enum reflects how much was recovered.

    Args:
        board_dir: A board directory, e.g. ``boards/05-bldc-motor-controller``.

    Returns:
        A schema-conforming ``board.json`` dict.
    """
    board_dir = Path(board_dir)
    slug = board_dir.name

    metrics: dict = {
        "$schema": SCHEMA_URL,
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "slug": slug,
    }

    output_dir = board_dir / "output"
    mfg_dir = output_dir / "manufacturing"

    if not mfg_dir.is_dir():
        # No manufacturing artifacts at all — identity-only board.json.
        logger.info("board %s: no output/manufacturing directory; status=no_artifacts", slug)
        metrics["status"] = "no_artifacts"
        _attach_render_paths(metrics, output_dir)
        return metrics

    report_parsed = False

    # report.md is the primary metrics source.
    report_path = mfg_dir / "report.md"
    if report_path.is_file():
        try:
            text = report_path.read_text()
            metrics.update(_parse_report_md(text, slug))
            report_parsed = True
        except OSError as exc:
            logger.warning("board %s: could not read report.md (%s)", slug, exc)
    else:
        logger.warning("board %s: report.md missing under output/manufacturing", slug)

    # manifest.json supplies board identity (name + timestamp).
    manifest_path = mfg_dir / "manifest.json"
    if manifest_path.is_file():
        metrics.update(_parse_manifest(manifest_path, slug))

    # Fall back to BOM row count when report.md did not yield a part_count.
    if "part_count" not in metrics:
        bom_path = mfg_dir / "bom_jlcpcb.csv"
        if bom_path.is_file():
            count = _count_bom_parts(bom_path, slug)
            if count is not None:
                metrics["part_count"] = count

    # Downloadable manufacturing package (relative to board.json location).
    package_path = mfg_dir / "kicad_project.zip"
    if package_path.is_file():
        metrics["manufacturing_package"] = "manufacturing/kicad_project.zip"

    _attach_render_paths(metrics, output_dir)

    metrics["status"] = "ok" if report_parsed else "partial"
    return metrics


def _attach_render_paths(metrics: dict, output_dir: Path) -> None:
    """Attach a ``renders`` dict for render images that exist on disk.

    Paths are relative to ``board.json``'s location (``output/``), so they
    resolve as ``output/<value>``. Missing files are omitted; the ``renders``
    key is added only when at least one render exists.
    """
    renders: dict = {}
    for field, rel_path in RENDER_FILES.items():
        if (output_dir / rel_path).is_file():
            renders[field] = rel_path
    if renders:
        metrics["renders"] = renders


def emit_board_json(board_dir: Path, output_path: Path | None = None) -> Path:
    """Extract metrics and write ``board.json``; return the written path.

    Args:
        board_dir: Board directory (e.g. ``boards/05-bldc-motor-controller``).
        output_path: Override output path. Defaults to
            ``<board_dir>/output/board.json``.

    Returns:
        The path the ``board.json`` was written to.
    """
    board_dir = Path(board_dir)
    metrics = extract_board_metrics(board_dir)

    if output_path is None:
        output_path = board_dir / "output" / "board.json"
    output_path = Path(output_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(metrics, indent=2) + "\n")
    return output_path


def _iter_board_dirs(boards_dir: Path):
    """Yield candidate board sub-directories of ``boards_dir`` in sorted order.

    A board dir is any immediate sub-directory that is not hidden. The
    ``external/`` directory is descended into one level (it groups external
    boards), mirroring the ``boards/`` layout.
    """
    if not boards_dir.is_dir():
        return
    for entry in sorted(boards_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith(".") or entry.name.startswith("_"):
            continue
        if entry.name == "external":
            for sub in sorted(entry.iterdir()):
                if sub.is_dir() and not sub.name.startswith("."):
                    yield sub
            continue
        yield entry


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point for ``kct board-metrics``.

    Usage::

        kct board-metrics boards/05-bldc-motor-controller
        kct board-metrics --all --boards-dir boards/
        kct board-metrics boards/05-... --output path/board.json
        kct board-metrics boards/05-... --dry-run
    """
    parser = argparse.ArgumentParser(
        prog="kct board-metrics",
        description="Emit a normalized board.json per demo board from existing artifacts.",
    )
    parser.add_argument(
        "board",
        nargs="?",
        help="Path to a board directory (e.g. boards/05-bldc-motor-controller)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Process every board under --boards-dir instead of a single board",
    )
    parser.add_argument(
        "--boards-dir",
        default="boards",
        help="Root directory containing per-board subdirs (default: boards)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Override output path (single-board mode only)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print board.json to stdout without writing any file",
    )
    args = parser.parse_args(argv)

    if args.all:
        return _run_all(Path(args.boards_dir), dry_run=args.dry_run)

    if not args.board:
        parser.error("a board directory is required (or use --all)")

    board_dir = Path(args.board)
    if not board_dir.is_dir():
        print(f"error: board directory not found: {board_dir}")
        return 1

    if args.dry_run:
        metrics = extract_board_metrics(board_dir)
        print(json.dumps(metrics, indent=2))
        return 0

    output_path = Path(args.output) if args.output else None
    written = emit_board_json(board_dir, output_path)
    metrics = json.loads(written.read_text())
    print(f"{metrics['slug']:30s} {metrics['status']:13s} -> {written}")
    return 0


def _run_all(boards_dir: Path, dry_run: bool) -> int:
    """Process every board under ``boards_dir`` in sorted order."""
    if not boards_dir.is_dir():
        print(f"error: boards directory not found: {boards_dir}")
        return 1

    any_board = False
    for board_dir in _iter_board_dirs(boards_dir):
        any_board = True
        if dry_run:
            metrics = extract_board_metrics(board_dir)
            print(json.dumps(metrics, indent=2))
            continue
        written = emit_board_json(board_dir)
        metrics = json.loads(written.read_text())
        print(f"{metrics['slug']:30s} {metrics['status']:13s} -> {written}")

    if not any_board:
        print(f"error: no board subdirectories found under {boards_dir}")
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
