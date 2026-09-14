"""Board metrics extractor — emit a normalized ``board.json`` per demo board.

This module aggregates already-computed manufacturing artifacts into a single,
stable ``board.json`` data contract consumed by the kicad-tools.org demo gallery
(Epic #3674, Phase 1, issue #3676).

It does **not** invoke KiCad or run any manufacturing export. For a board with
an ``output/manufacturing/`` directory, all metrics are parsed from artifacts
that already exist there:

* ``report.md``      -> routing %, DRC errors, layer count, board size, part
                        count, description, cost estimate.
* ``manifest.json``  -> board name and ``generated_at`` timestamp.
* ``bom_jlcpcb.csv`` -> fallback part count (row count minus header).
* ``kicad_project.zip`` -> downloadable manufacturing package path.
* ``../renders/*.png`` -> render image paths (written by ``kct render``, #3675).
* ``../readiness.json`` -> current hash-bound manufacturing readiness.
* ``../lvs.json``    -> Layout-vs-Schematic verification (#3748, #3749);
                        sourced from ``output/lvs.json`` (NOT under
                        ``manufacturing/``).

For a board with no ``output/manufacturing/`` directory (issue #5055), static
identity/geometry metadata is instead read directly from a selected
``.kicad_pcb``/``.kicad_sch`` source (footprint/layer counts, Edge.Cuts bounds)
without altering readiness evidence or synthesizing manufacturing output — see
"Development boards without manufacturing export" in ``docs/board-json-schema.md``.

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
      "lvs_clean": true,
      "lvs_mismatches": 0,
      "status": "ok"
    }

``status`` is one of:

* ``"ok"``           — ``output/manufacturing/`` exists, ``report.md`` parsed,
                        ``drc_violations == 0`` (manufacturable), and (when
                        ``lvs.json`` is present) ``lvs_clean == True``.
* ``"partial"``      — ``output/manufacturing/`` exists but ``report.md`` is
                        absent/unparseable (only identity + whatever fields we
                        could recover), OR ``report.md`` parsed but the board
                        still has ``drc_violations > 0`` (not manufacturable),
                        OR an explicit ``lvs_clean == False`` LVS mismatch.
* ``"no_artifacts"`` — no manufacturing directory or usable development artifact.

Schema versioning policy: this schema is the Phase 2 (Astro site) data contract.
Field additions must be additive — no renames, no type changes. Bump
``schema_version`` only for breaking changes. See ``docs/board-json-schema.md``.

Machine output (``--format json``, issue #4674): the *command* prints one
envelope document describing the run — ``{"command": "board-metrics", "mode":
"single"|"all", "dry_run", "boards": [...], "success"}`` — where each entry in
``boards`` carries ``slug``/``status``/``output_path`` plus the board's full
``metrics`` (the board.json above).  That envelope is deliberately distinct
from the ``board.json`` artifact itself, which keeps its own schema.  See
``docs/reference/machine-output.md``.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .board_readiness import read_readiness
from .format_options import FORMAT_JSON, add_format_flag, emit_json

if TYPE_CHECKING:
    from kicad_tools.sexp import SExp

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


def _parse_report_md(text: str, slug: str, current_metrics: dict | None = None) -> dict:
    """Parse the machine-generated ``report.md`` into a partial metrics dict.

    Each field is parsed independently; a miss logs a warning (naming the slug
    and field) and is omitted rather than raising.
    """
    out: dict = {}
    current_metrics = current_metrics or {}

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
    if "nets_routed_pct" in current_metrics:
        out["nets_routed_pct"] = current_metrics["nets_routed_pct"]
    elif m:
        out["nets_routed_pct"] = float(m.group(1))
    else:
        logger.warning("board %s: could not parse nets_routed_pct from report.md", slug)

    m = _DRC_ERRORS_RE.search(text)
    if "drc_violations" in current_metrics:
        out["drc_violations"] = current_metrics["drc_violations"]
    elif m:
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


def _parse_lvs(lvs_path: Path, slug: str) -> dict:
    """Source LVS fields from ``output/lvs.json`` (#3748, #3749).

    Returns ``{"lvs_clean": bool, "lvs_mismatches": int}`` when the file is
    present and readable; returns ``{}`` when the file is absent or unparseable
    so the keys are *omitted* from the emitted ``board.json`` rather than set
    to ``null`` (the board.json contract: missing artifact → field omitted).

    A *vacuous* report (#4006: ``copper_vacuous: true`` or
    ``copper_bound_pad_count: 0`` — the schematic bound zero pins, so the
    comparator observed nothing) is also treated as "LVS not run" (``{}``):
    rendering it as either "clean" or "dirty" would claim evidence that does
    not exist.
    """
    if not lvs_path.is_file():
        return {}
    try:
        data = json.loads(lvs_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("board %s: could not read lvs.json (%s)", slug, exc)
        return {}
    if data.get("copper_vacuous") is True or data.get("copper_bound_pad_count") == 0:
        logger.info(
            "board %s: lvs.json is vacuous (0 schematic pins bound, #4006); "
            "treating as LVS-not-run",
            slug,
        )
        return {}
    return {
        "lvs_clean": bool(data.get("clean", False)),
        # Count BOTH comparator legs (#4012): ``mismatches`` is the
        # label-based list (historical board-00 schema) and
        # ``copper_mismatches`` the copper-extracted shorts/opens (#3762).
        # A board like 07 -- label-clean but with 5 real copper opens
        # (#3438) -- must render "5 mismatches", not a dishonest 0.
        "lvs_mismatches": (
            len(data.get("mismatches") or []) + len(data.get("copper_mismatches") or [])
        ),
    }


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


def _development_outline_bounds(pcb: SExp) -> tuple[float, float, float, float] | None:
    """Measure only fully validated straight Edge.Cuts contours.

    This is local metadata policy: no first-contour walk or bounds from
    unchecked leftovers. Curves and footprint-local geometry need other
    transforms/extrema and remain explicitly unsupported here.
    """
    import math
    from collections import Counter

    from shapely.errors import GEOSException  # type: ignore[import-untyped]
    from shapely.geometry import MultiLineString  # type: ignore[import-untyped]
    from shapely.ops import polygonize_full  # type: ignore[import-untyped]

    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []

    def point(node: SExp, tag: str) -> tuple[float, float]:
        matches = [child for child in node.iter_children() if child.tag == tag]
        if len(matches) != 1:
            raise ValueError("missing or duplicate outline coordinate")
        return xy(matches[0])

    def xy(node: SExp) -> tuple[float, float]:
        values = node.get_atoms()
        if (
            len(values) != 2
            or list(node.iter_children())
            or any(type(v) not in (int, float) for v in values)
            or any(not math.isfinite(float(v)) for v in values)
        ):
            raise ValueError("malformed outline coordinate")
        return float(values[0]), float(values[1])

    def visit(node: SExp, top_level: bool) -> None:
        layers = [child for child in node.iter_children() if child.tag == "layer"]
        if any(layer.get_string(0) == "Edge.Cuts" for layer in layers):
            if (
                not top_level
                or len(layers) != 1
                or layers[0].get_atoms() != ["Edge.Cuts"]
                or node.tag not in {"gr_line", "gr_rect", "gr_poly"}
            ):
                raise ValueError("unsupported outline geometry")
            if node.tag == "gr_line":
                points = [point(node, "start"), point(node, "end")]
            elif node.tag == "gr_rect":
                a, b = point(node, "start"), point(node, "end")
                points = [a, (b[0], a[1]), b, (a[0], b[1]), a]
            else:
                containers = [child for child in node.iter_children() if child.tag == "pts"]
                if len(containers) != 1:
                    raise ValueError("missing or duplicate outline polygon")
                children = list(containers[0].iter_children())
                if containers[0].get_atoms() or any(child.tag != "xy" for child in children):
                    raise ValueError("unsupported polygon edge")
                points = [xy(child) for child in children]
                if len(points) > 1 and points[-1] == points[0]:
                    points.pop()
                if len(points) < 3:
                    raise ValueError("degenerate outline polygon")
                points.append(points[0])
            segments.extend(zip(points, points[1:], strict=False))
        for child in node.iter_children():
            visit(child, False)

    try:
        for node in pcb.iter_children():
            visit(node, True)
        if not segments or any(a == b for a, b in segments):
            return None
        # Exact endpoints: do not silently close a physical gap by snapping.
        degrees = Counter(point for segment in segments for point in segment)
        if any(degree != 2 for degree in degrees.values()):
            return None
        lines = MultiLineString(segments)
        if not lines.is_simple:  # Reject crossings and overlapping edges.
            return None
        polygons, cuts, dangles, invalid = polygonize_full(lines)
        if polygons.is_empty or not all(g.is_empty for g in (cuts, dangles, invalid)):
            return None
        left, bottom, right, top = lines.bounds
        return float(left), float(bottom), float(right), float(top)
    except (ValueError, TypeError, OverflowError, GEOSException):
        return None


def _development_metrics(board_dir: Path, readiness: dict) -> dict:
    """Recover static development metadata without upgrading release evidence."""
    import hashlib
    import math

    from kicad_tools.exceptions import KiCadToolsError
    from kicad_tools.schema.pcb import FOOTPRINT_TAGS
    from kicad_tools.schema.schematic import Schematic
    from kicad_tools.sexp import parse_string
    from kicad_tools.spec.parser import load_spec

    result: dict = {"sources": {}, "diagnostics": []}
    diagnostics = result["diagnostics"]
    root = board_dir.resolve()
    project_path = board_dir / "project.kct"
    artifacts: dict = {}
    project_valid = True
    if project_path.exists():
        try:
            project = load_spec(project_path).project.model_dump(exclude_unset=True)
            artifacts = project.get("artifacts") or {}
            for field in ("name", "description"):
                if isinstance(project.get(field), str):
                    result[field] = project[field]
        except (OSError, ValueError) as exc:
            project_valid = False
            diagnostics.append(f"Cannot select artifacts from project.kct: {exc}")

    selected: dict[str, Path] = {}
    usable = False
    for kind, suffix in (("pcb", ".kicad_pcb"), ("schematic", ".kicad_sch")):
        if not project_valid:
            continue
        if kind in artifacts:
            value = artifacts[kind]
            if not isinstance(value, str) or not value:
                diagnostics.append(f"Invalid explicit {kind} path")
                continue
            path = board_dir / value
            selection = "project.artifacts"
        else:
            candidates = list((board_dir / "output").glob(f"*{suffix}"))
            if len(candidates) != 1:
                if candidates:
                    diagnostics.append(
                        f"Ambiguous {kind} candidates: "
                        + ", ".join(sorted(p.name for p in candidates))
                    )
                continue
            path = candidates[0]
            selection = "unambiguous_output"
        if not path.resolve().is_relative_to(root) or not path.is_file():
            diagnostics.append(f"Missing or outside-board {kind} source: {path}")
            continue
        try:
            raw = path.read_bytes()
            if kind == "pcb":
                # Static metadata does not require a valid outline/origin.
                # Parse the same captured bytes without PCB.load's geometry
                # normalization, then validate dimensions independently below.
                pcb = parse_string(raw.decode("utf-8"))
                tables = pcb.find_children("layers")
                if pcb.tag != "kicad_pcb" or len(tables) != 1:
                    raise ValueError("missing PCB root or copper layer definitions")
                layers = list(tables[0].iter_children())
                if any(
                    layer.tag is None
                    or not layer.tag.isdecimal()
                    or not layer.get_string(0)
                    or not layer.get_string(1)
                    for layer in layers
                ):
                    raise ValueError("malformed PCB layer definitions")
                # KiCad layer identity is numeric: 0 and 00 are the same ID.
                layer_ids = {int(layer.tag) for layer in layers if layer.tag is not None}
                if len(layer_ids) != len(layers):
                    raise ValueError("duplicate PCB layer definitions")
                copper_count = sum(layer.get_string(1) in {"signal", "power"} for layer in layers)
                if not copper_count:
                    raise ValueError("missing copper layer definitions")
                result["part_count"] = sum(
                    node.tag in FOOTPRINT_TAGS for node in pcb.iter_children()
                )
                result["layer_count"] = copper_count
                bounds = _development_outline_bounds(pcb)
                if bounds is not None:
                    width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
                    if all(math.isfinite(v) and v > 0 for v in (width, height)):
                        result["board_size_mm"] = {"width": width, "height": height}
                if "board_size_mm" not in result:
                    diagnostics.append(
                        "PCB outline is missing, open, malformed, or curved/unsupported; dimensions unknown"
                    )
            else:
                schematic = Schematic.load(path)
                if schematic._sexp.tag != "kicad_sch":
                    raise ValueError("missing schematic root")
            selected[kind] = path
            result["sources"][kind] = {
                "path": path.resolve().relative_to(root).as_posix(),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "selection": selection,
            }
            usable = True
        except (OSError, ValueError, TypeError, AttributeError, IndexError, KiCadToolsError) as exc:
            diagnostics.append(f"Cannot parse {kind} source {path.name}: {exc}")

    result["status"] = "partial" if usable else "no_artifacts"
    pcb_path = selected.get("pcb")
    if pcb_path is None:
        return result
    inputs = readiness.get("inputs", {})
    pcb_key = result["sources"]["pcb"]["path"]
    if (
        readiness.get("status") not in {"ready", "blocked"}
        or inputs.get(pcb_key) != result["sources"]["pcb"]["sha256"]
    ):
        diagnostics.append(
            "Native DRC measurements unavailable: no fresh readiness binding for selected PCB"
        )
        return result
    evidence = readiness.get("evidence", {})
    explicit_report = evidence.get("native_drc") if isinstance(evidence, dict) else None
    if explicit_report is not None:
        reports = [explicit_report] if isinstance(explicit_report, str) else []
    else:
        reports = [
            name for name in inputs if Path(name).name in {"native-drc.json", "placement-drc.json"}
        ]
    if len(reports) != 1 or reports[0] not in inputs:
        diagnostics.append(
            "Native DRC measurements unavailable: missing or ambiguous hash-bound report"
        )
        return result
    try:
        report_path = board_dir / reports[0]
        raw = report_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != inputs[reports[0]]:
            raise ValueError("report changed after readiness validation")
        report = json.loads(raw)
        violations, opens = report["violations"], report["unconnected_items"]
        if not isinstance(violations, list) or not isinstance(opens, list):
            raise ValueError("missing native finding arrays")
        if not all(
            isinstance(item, dict) and item.get("severity") in {"error", "warning", "ignore"}
            for item in violations
        ):
            raise ValueError("invalid native violation severity")
        if not all(isinstance(item, dict) for item in opens):
            raise ValueError("invalid native unconnected item")
        result["drc_violations"] = sum(item["severity"] == "error" for item in violations)
        result["native_drc_geometry_violations"] = len(violations)
        result["native_drc_unconnected_items"] = len(opens)
        result["sources"]["native_drc"] = {"path": reports[0], "sha256": inputs[reports[0]]}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        diagnostics.append(f"Native DRC measurements unavailable: {exc}")
    return result


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
    metrics["readiness"] = read_readiness(board_dir)

    if not mfg_dir.is_dir():
        # Development metadata is independent of manufacturing export.
        logger.info("board %s: extracting development artifacts", slug)
        metrics.update(_development_metrics(board_dir, metrics["readiness"]))
        _attach_render_paths(metrics, output_dir)
        return metrics

    current = (
        metrics["readiness"].get("metrics", {})
        if metrics["readiness"]["status"] in {"ready", "blocked"}
        else {}
    )

    # Markdown supplies descriptive/export metadata; current structured evidence
    # supplies checked metrics independently of report formatting.
    report_path = mfg_dir / "report.md"
    if report_path.is_file():
        try:
            text = report_path.read_text()
            metrics.update(_parse_report_md(text, slug, current))
        except OSError as exc:
            logger.warning("board %s: could not read report.md (%s)", slug, exc)
    else:
        logger.warning("board %s: report.md missing under output/manufacturing", slug)

    # manifest.json supplies board identity (name + timestamp).
    manifest_path = mfg_dir / "manifest.json"
    if manifest_path.is_file():
        metrics.update(_parse_manifest(manifest_path, slug))

    # lvs.json lives at output/lvs.json (NOT under output/manufacturing/) per
    # the #3748 producer. When absent the LVS fields are *omitted* — the site
    # renders a neutral "LVS not run" chip in that case rather than "Ready".
    metrics.update(_parse_lvs(output_dir / "lvs.json", slug))

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

    # Fresh readiness may carry newer measurements than the saved export report.
    if metrics["readiness"]["status"] in {"ready", "blocked"}:
        for name in ("drc_violations", "nets_routed_pct", "lvs_clean", "lvs_mismatches"):
            if name in current:
                metrics[name] = current[name]

    # A complete current verdict does not depend on a Markdown heading/table
    # or a cached board.json status. Missing or conflicting evidence stays partial.
    metrics["status"] = (
        "ok"
        if metrics["readiness"]["status"] == "ready"
        and metrics.get("drc_violations") == 0
        and metrics.get("lvs_clean") is True
        else "partial"
    )
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
    add_format_flag(parser)
    args = parser.parse_args(argv)
    as_json = args.format == FORMAT_JSON

    if args.all:
        return _run_all(Path(args.boards_dir), dry_run=args.dry_run, as_json=as_json)

    if not args.board:
        parser.error("a board directory is required (or use --all)")

    board_dir = Path(args.board)
    if not board_dir.is_dir():
        message = f"error: board directory not found: {board_dir}"
        if as_json:
            emit_json(_envelope("single", args.dry_run, [], error=message))
        else:
            print(message)
        return 1

    entry = _process_board(board_dir, Path(args.output) if args.output else None, args.dry_run)

    if as_json:
        emit_json(_envelope("single", args.dry_run, [entry]))
        return 0

    if args.dry_run:
        print(json.dumps(entry["metrics"], indent=2))
        return 0

    print(f"{entry['slug']:30s} {entry['status']:13s} -> {entry['output_path']}")
    return 0


def _envelope(
    mode: str,
    dry_run: bool,
    boards: list[dict],
    *,
    error: str | None = None,
) -> dict:
    """Build the ``--format json`` envelope for one ``board-metrics`` run."""
    payload: dict = {
        "command": "board-metrics",
        "mode": mode,
        "dry_run": dry_run,
        "boards": boards,
        "success": error is None,
    }
    if error is not None:
        payload["error"] = error
    return payload


def _process_board(board_dir: Path, output_path: Path | None, dry_run: bool) -> dict:
    """Extract (and, unless *dry_run*, write) one board's metrics.

    Returns the per-board entry used by both the prose and the JSON paths, so
    the two can never report different slugs/statuses for the same run.
    """
    if dry_run:
        metrics = extract_board_metrics(board_dir)
        written: Path | None = None
    else:
        written = emit_board_json(board_dir, output_path)
        metrics = json.loads(written.read_text())
    return {
        "slug": metrics.get("slug", board_dir.name),
        "status": metrics.get("status"),
        "output_path": str(written) if written is not None else None,
        "metrics": metrics,
    }


def _run_all(boards_dir: Path, dry_run: bool, as_json: bool = False) -> int:
    """Process every board under ``boards_dir`` in sorted order."""
    if not boards_dir.is_dir():
        message = f"error: boards directory not found: {boards_dir}"
        if as_json:
            emit_json(_envelope("all", dry_run, [], error=message))
        else:
            print(message)
        return 1

    entries: list[dict] = []
    for board_dir in _iter_board_dirs(boards_dir):
        entry = _process_board(board_dir, None, dry_run)
        entries.append(entry)
        if as_json:
            continue
        if dry_run:
            print(json.dumps(entry["metrics"], indent=2))
            continue
        print(f"{entry['slug']:30s} {entry['status']:13s} -> {entry['output_path']}")

    if not entries:
        message = f"error: no board subdirectories found under {boards_dir}"
        if as_json:
            emit_json(_envelope("all", dry_run, [], error=message))
        else:
            print(message)
        return 1

    if as_json:
        emit_json(_envelope("all", dry_run, entries))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
