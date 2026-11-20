"""``kct probe`` / ``kct apply --checked``: batch check-then-write for hand-scripted copper (Issue #9590).

Agents that place copper by script need to check a whole batch of proposed
tracks and vias against the board, and write nothing unless it all passes.
``kct route-link --probe`` checks a single router-routed link; this module
is the batch form for items the agent computed itself:

* ``kct probe <pcb> <items.json>`` -- read-only.  Appends the items to an
  in-memory copy of the board and runs the same ``kct check`` rule set on
  it, reporting only findings the batch *adds* (pre-existing board problems
  are subtracted via a baseline run on the unmodified board, so they never
  fail a probe).  Exit 1 on any added error finding or items-file problem.
* ``kct apply <pcb> <items.json> --checked`` -- probe first; write the board
  only when the probe is clean.  The write is atomic (sibling temp file +
  :func:`os.replace`, the ``route_cmd`` discipline), the previous bytes are
  kept at ``<pcb>.kicad_pcb.bak`` (override with ``--backup``), and a board
  that changed on disk between read and write is refused (exit 2, nothing
  written) by comparing ``st_mtime_ns`` and ``st_size`` immediately before
  the replace.  Items whose dedupe key is already on the board are skipped
  by ``add_trace`` / ``add_via`` dedupe (issue #4175; a segment's key is
  net, layer, unordered endpoints and width, a via's is net, position and
  layers); the report counts them separately
  (``items_written`` / ``items_skipped_as_duplicate``) instead of claiming
  every proposed item was written (issue #9674).  A via's size and drill
  are outside its key, so a via proposed at a position held by a via of a
  different size or drill is not a duplicate: it is not written, is counted
  under ``items_skipped_conflicting``, and the text output names the
  existing size/drill.  Arcs have no dedupe path; each successful apply
  appends them again.

The items file is JSON with absolute board millimetres (the file's literal
``(at ...)`` / ``(start ...)`` sheet coordinates, as KiCad shows them)::

    {"items": [
      {"type": "segment", "net": "GND", "layer": "F.Cu", "width": 0.2,
       "start": [x, y], "end": [x, y]},
      {"type": "arc", "net": "GND", "layer": "F.Cu", "width": 0.2,
       "start": [x, y], "mid": [x, y], "end": [x, y]},
      {"type": "via", "net": "GND", "at": [x, y], "size": 0.6, "drill": 0.3,
       "layers": ["F.Cu", "B.Cu"]}
    ]}

Rules come from ``kct check`` itself (:func:`.check_cmd.build_check_rule_set`
+ :func:`.check_cmd.run_selected_checks`), so a probe/check disagreement is a
bug -- there is no second clearance tolerance anywhere in this module.  The
categories run are :data:`PROBE_CHECK_CATEGORIES` (pairwise copper clearance
and shorts, the net-agnostic fab-view copper-gap pass, thin-copper slivers,
zone-fill clearance, board-edge clearance, trace/via dimensions, authored
net-class clearances on copper, via-in-pad / via-type / via-under-body,
zero-length segments, THT-to-SMD pad spacing).  The copper-gap pass is
net-agnostic: a same-net parallel track can fail a probe through it even
though no pairwise clearance rule objects.  The remaining default ``kct
check`` copper categories
are out of scope on purpose: ``sharp_trace_corner``, ``dangling_copper``,
``width_consistency``, ``isolated_copper``, ``redundant_copper`` and
``missing_plated_hole`` are routing-style / process advisories that report at
warning severity, so excluding them cannot produce the probe-clean /
check-failed disagreement this command exists to prevent, and
``netclass_floor`` grades the declared net-class sidecar rather than written
copper.  Arcs are checked through a 1 um-sagitta tessellation appended for
the check pass only (with the check width inflated by twice the sagitta
bound so the tessellated copper always covers the arc the board would
carry); the board itself is written with a real ``(arc ...)`` node.

Exit codes (both commands): ``0`` clean; ``1`` problems found, bad input, or
nothing to do; ``2`` refused -- the board changed under us, or a KiCad lock
is present under the ``error`` policy.  Nothing is ever written on ``1`` or
``2``.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .format_options import FORMAT_JSON, add_format_flag, emit_json, stdout_to_stderr_when

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

__all__ = [
    "ItemsFileError",
    "ProbeItem",
    "PROBE_CHECK_CATEGORIES",
    "parse_items",
    "load_items_file",
    "probe_items",
    "apply_items_checked",
    "add_probe_arguments",
    "add_apply_arguments",
    "run_probe_command",
    "run_apply_command",
]

#: ``kct check`` categories a probe runs (see module docstring).  Kept as a
#: tuple so the JSON report can carry it verbatim; every name must exist in
#: ``kicad_tools.cli.check_categories.CHECK_CATEGORIES``.
PROBE_CHECK_CATEGORIES: tuple[str, ...] = (
    "clearance",
    "physical_copper_gap",
    "copper_sliver",
    "segment_zone",
    "via_zone",
    "edge",
    "dimensions",
    "netclass_clearance_copper",
    "via_in_pad",
    "via_type",
    "via_under_body",
    "zero_length_segment",
    "tht_smd_spacing",
)

#: Sagitta bound for the check-pass tessellation of proposed arcs.  1 um is
#: the coordinate quantum :mod:`kicad_tools.validate.copper_refs` keys
#: findings on, so the tessellated copper is within one quantum of the arc
#: the board would actually carry.
_ARC_TESSELLATION_UM = 0.001

#: Fields every item accepts regardless of its ``type`` (#9618).
_COMMON_ITEM_FIELDS = frozenset({"type", "net"})

#: Per-type allowlists: a field meaningless for the item's ``type`` is
#: rejected at parse time instead of being silently dropped (#9618).
_ITEM_FIELDS: dict[str, frozenset[str]] = {
    "segment": frozenset({"layer", "width", "start", "end"}),
    "arc": frozenset({"layer", "width", "start", "end", "mid"}),
    "via": frozenset({"at", "size", "drill", "layers"}),
}


class ItemsFileError(ValueError):
    """The items file could not be parsed as a batch of proposed copper."""


@dataclass
class ProbeItem:
    """One proposed copper item, already coerced to typed fields.

    Coordinates are the items file's sheet-absolute millimetres; conversion
    to the board-relative frame happens at append time (``PCB`` geometry is
    board-relative; see ``PCB._detect_board_origin``).
    """

    kind: str  # "segment" | "arc" | "via"
    net: str
    index: int  # 1-based position in the file, for problem lines
    layer: str = ""
    width: float = 0.0
    start: tuple[float, float] = (0.0, 0.0)
    end: tuple[float, float] = (0.0, 0.0)
    mid: tuple[float, float] | None = None
    at: tuple[float, float] = (0.0, 0.0)
    size: float = 0.6
    drill: float = 0.3
    layers: tuple[str, ...] = ("F.Cu", "B.Cu")


@dataclass
class _ProbeOutcome:
    """Internal result shared by the CLI runners and the MCP handlers."""

    report: dict[str, Any]
    #: The board bytes as read before probing (apply's backup source).
    original_bytes: bytes = b""
    #: ``(st_mtime_ns, st_size)`` captured immediately before the read.
    stat_marker: tuple[int, int] = (0, 0)


def _as_point(value: Any, item: str, key: str) -> tuple[float, float]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
    ):
        raise ItemsFileError(f"item {item}: {key!r} must be [x, y] numbers")
    point = (float(value[0]), float(value[1]))
    if not all(math.isfinite(v) for v in point):
        raise ItemsFileError(f"item {item}: {key!r} must be finite")
    return point


def _as_positive(value: Any, item: str, key: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ItemsFileError(f"item {item}: {key!r} must be a number")
    number = float(value)
    if number <= 0:
        raise ItemsFileError(f"item {item}: {key!r} must be > 0")
    return number


def _parse_one(raw: Any, index: int) -> ProbeItem:
    item = f"{index}"
    if not isinstance(raw, dict):
        raise ItemsFileError(f"item {item}: each entry must be an object")
    kind = raw.get("type")
    if not isinstance(kind, str) or kind not in _ITEM_FIELDS:
        raise ItemsFileError(f"item {item}: 'type' must be segment, arc or via")
    unknown = sorted(set(raw) - _COMMON_ITEM_FIELDS - _ITEM_FIELDS[kind])
    if unknown:
        message = f"item {item}: field(s) {unknown} not valid for type '{kind}'"
        if kind == "segment" and "mid" in unknown:
            message += '; use "type": "arc" to keep the midpoint'
        raise ItemsFileError(message)
    net = raw.get("net")
    if not isinstance(net, str) or not net:
        raise ItemsFileError(f"item {item}: 'net' must be a non-empty string")

    if kind == "via":
        at = _as_point(raw.get("at"), item, "at")
        size = _as_positive(raw.get("size", 0.6), item, "size")
        drill = _as_positive(raw.get("drill", 0.3), item, "drill")
        if drill >= size:
            raise ItemsFileError(f"item {item}: drill ({drill}) must be smaller than size ({size})")
        layers = raw.get("layers", ["F.Cu", "B.Cu"])
        if (
            not isinstance(layers, list)
            or len(layers) != 2
            or not all(isinstance(v, str) and v for v in layers)
        ):
            raise ItemsFileError(
                f'item {item}: \'layers\' must be two layer names, e.g. ["F.Cu", "B.Cu"]'
            )
        return ProbeItem(
            kind="via",
            net=net,
            index=index,
            at=at,
            size=size,
            drill=drill,
            layers=(layers[0], layers[1]),
        )

    layer = raw.get("layer")
    if not isinstance(layer, str) or not layer:
        raise ItemsFileError(f"item {item}: 'layer' must be a copper layer name")
    width = _as_positive(raw.get("width"), item, "width")
    start = _as_point(raw.get("start"), item, "start")
    end = _as_point(raw.get("end"), item, "end")
    mid = _as_point(raw.get("mid"), item, "mid") if kind == "arc" else None
    return ProbeItem(
        kind=kind, net=net, index=index, layer=layer, width=width, start=start, end=end, mid=mid
    )


def parse_items(data: Any) -> list[ProbeItem]:
    """Parse an already-decoded items payload (the ``{"items": [...]}`` object)."""
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ItemsFileError('items file must be a JSON object {"items": [...]}')
    items = [_parse_one(raw, index) for index, raw in enumerate(data["items"], start=1)]
    if not items:
        raise ItemsFileError("items list is empty")
    return items


def load_items_file(path: str | Path) -> list[ProbeItem]:
    """Load and parse an items JSON file."""
    import json

    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ItemsFileError(f"cannot read items file {path}: {exc}") from exc
    except ValueError as exc:
        raise ItemsFileError(f"items file {path} is not valid JSON: {exc}") from exc
    return parse_items(data)


def _validate_against_board(items: list[ProbeItem], pcb: Any) -> list[dict[str, Any]]:
    """Reject items the board cannot carry; returns problem dicts (empty = ok)."""
    problems: list[dict[str, Any]] = []
    net_names = {net.name for net in pcb.nets.values() if getattr(net, "name", None)}
    layer_names = {layer.name for layer in pcb.copper_layers}

    def fail(item: ProbeItem, message: str) -> None:
        problems.append(
            {
                "rule_id": "items_file",
                "severity": "error",
                "message": f"item {item.index} ({item.kind} on net {item.net!r}): {message}",
                "location": None,
                "layer": None,
                "items": (),
                "nets": (item.net,),
                "actual_value": None,
                "required_value": None,
                "proposed_item": item.index,
            }
        )

    from kicad_tools.schema.pcb import Arc

    for item in items:
        if item.net not in net_names:
            sample = ", ".join(sorted(net_names)[:8])
            fail(item, f"unknown net (board has: {sample}...)")
            continue
        if item.kind == "via":
            bad = [name for name in item.layers if name not in layer_names]
            if bad:
                fail(item, f"unknown layer(s) {bad}")
            continue
        if item.layer not in layer_names:
            fail(item, f"unknown layer {item.layer!r}")
            continue
        if item.kind == "arc":
            assert item.mid is not None
            try:
                arc = Arc(
                    start=item.start,
                    mid=item.mid,
                    end=item.end,
                    width=item.width,
                    layer=item.layer,
                    net_number=0,
                )
                arc.circular_geometry()
                arc.centerline_points(max_error_mm=_ARC_TESSELLATION_UM)
            except ValueError as exc:
                fail(item, f"invalid arc geometry: {exc}")

    return problems


def _net_number(pcb: PCB, name: str) -> int:
    """The board's net number for *name* (0 when absent; validation ran first)."""
    for number, net in pcb.nets.items():
        if net.name == name:
            return number
    return 0


def _append_for_check(pcb: Any, items: list[ProbeItem]) -> dict[str, set[int]]:
    """Append every item to *pcb* for the check pass; returns descriptor -> item indices.

    Arcs are appended as a 1 um-sagitta tessellation of straight segments
    (the pairwise clearance rules walk ``pcb.segments``; ``(arc ...)`` copper
    is outside them today, so a real arc node would be silently unchecked).
    The chords lie on the arc's concave side, so the real copper extends up
    to one sagitta beyond the tessellation on the convex side; the check
    width is inflated by twice the sagitta bound so the tessellated copper
    always *covers* the arc the board would carry (the probe can only be
    stricter than ``kct check``, never looser -- Issue #9590 review).
    Everything goes through the public ``PCB.add_trace`` / ``add_via`` APIs.
    """
    from kicad_tools.schema.pcb import Arc, Segment, Via
    from kicad_tools.validate.copper_refs import segment_ref, via_ref

    ox, oy = pcb.board_origin

    def rel(point: tuple[float, float]) -> tuple[float, float]:
        return (point[0] - ox, point[1] - oy)

    descriptors: dict[str, set[int]] = {}
    origin = (ox, oy)  # refs are sheet-frame (see copper_refs); appended copper is not
    for item in items:
        if item.kind == "via":
            via = pcb.add_via(
                *rel(item.at), size=item.size, drill=item.drill, layers=item.layers, net=item.net
            )
            scratch = via or Via(
                position=rel(item.at),
                size=item.size,
                drill=item.drill,
                layers=list(item.layers),
                net_number=_net_number(pcb, item.net),
                net_name=item.net,
            )
            descriptors.setdefault(via_ref(scratch, origin), set()).add(item.index)
        elif item.kind == "arc":
            assert item.mid is not None
            arc = Arc(
                start=rel(item.start),
                mid=rel(item.mid),
                end=rel(item.end),
                width=item.width,
                layer=item.layer,
                net_number=0,
            )
            points = arc.centerline_points(max_error_mm=_ARC_TESSELLATION_UM)  # already relative
            check_width = item.width + 2 * _ARC_TESSELLATION_UM  # cover the sagitta (see docstring)
            for a, b in zip(points, points[1:], strict=False):
                appended = pcb.add_trace(a, b, width=check_width, layer=item.layer, net=item.net)
                for seg in appended:
                    descriptors.setdefault(segment_ref(seg, origin), set()).add(item.index)
        else:
            appended = pcb.add_trace(
                rel(item.start), rel(item.end), width=item.width, layer=item.layer, net=item.net
            )
            scratch = Segment(
                start=rel(item.start),
                end=rel(item.end),
                width=item.width,
                layer=item.layer,
                net_number=_net_number(pcb, item.net),
                net_name=item.net,
            )
            descriptors.setdefault(segment_ref(scratch, origin), set()).add(item.index)
    return descriptors


_VIA_DIM_QUANT = 3  # decimals compared for via size/drill; schema.pcb's dedupe quantum (~1 um)


def _append_for_write(pcb: Any, items: list[ProbeItem]) -> tuple[int, int, list[str]]:
    """Append every item to *pcb* as real board nodes (segments, vias, arcs).

    Returns ``(items_written, items_skipped_as_duplicate, conflicting_vias)``
    (issue #9674): ``add_trace`` / ``add_via`` dedupe (issue #4175) skips an
    item whose dedupe key -- net, layer, unordered endpoints and width for a
    segment; net, position and layers for a via -- is already on the board,
    and apply's report counts those separately from written items instead of
    claiming every proposed item was written.  A via's size and drill are
    outside its key, so a via proposed at a position held by a via of a
    different size or drill is not a duplicate: the copper the batch asked
    for is not on the board, the item is not written, and *conflicting_vias*
    carries one line per such via naming the existing and requested
    size/drill.  Arcs have no dedupe path, so every successful apply appends
    them and counts them as written.
    """
    ox, oy = pcb.board_origin

    def rel(point: tuple[float, float]) -> tuple[float, float]:
        return (point[0] - ox, point[1] - oy)

    written = 0
    skipped = 0
    conflicting: list[str] = []
    for item in items:
        if item.kind == "via":
            existing = pcb.find_via(*rel(item.at), layers=item.layers, net=item.net)
            if existing is not None and (
                round(existing.size, _VIA_DIM_QUANT) != round(item.size, _VIA_DIM_QUANT)
                or round(existing.drill, _VIA_DIM_QUANT) != round(item.drill, _VIA_DIM_QUANT)
            ):
                conflicting.append(
                    f"via for net {item.net} at ({item.at[0]:.3f}, {item.at[1]:.3f}) "
                    f"not written: board has size {existing.size:g} / drill "
                    f"{existing.drill:g} there, batch asked size {item.size:g} / "
                    f"drill {item.drill:g}"
                )
                continue
            via = pcb.add_via(
                *rel(item.at), size=item.size, drill=item.drill, layers=item.layers, net=item.net
            )
            if via is None:
                skipped += 1
            else:
                written += 1
        elif item.kind == "arc":
            assert item.mid is not None
            pcb.add_arc(
                rel(item.start),
                rel(item.mid),
                rel(item.end),
                width=item.width,
                layer=item.layer,
                net=item.net,
            )
            written += 1
        else:
            appended = pcb.add_trace(
                rel(item.start), rel(item.end), width=item.width, layer=item.layer, net=item.net
            )
            if appended:
                written += 1
            else:
                skipped += 1
    return written, skipped, conflicting


def _violation_dict(
    v: Any,
    descriptors: dict[str, set[int]],
    net_fallback: dict[str, set[int]],
) -> dict[str, Any]:
    """JSON shape of one finding, with the proposed item it involves.

    Attribution is by copper descriptor first (``Trace@...`` / ``Via@...`` /
    ``REF-PAD`` refs the clearance rules name).  Rules that name a net
    instead (``edge_clearance_trace``'s ``"Net 3"``, ``dimension_trace_width``
    's net name) fall back to net identity: the finding is already known to
    be NEW (baseline-subtracted), so a batch item on that net is the cause.
    """
    proposed: int | None = None
    for ref in v.items:
        indices = descriptors.get(ref) or net_fallback.get(ref)
        if indices:
            proposed = min(indices)
            break
    return {
        "rule_id": v.rule_id,
        "severity": v.severity,
        "message": v.message,
        "location": list(v.location) if v.location is not None else None,
        "layer": v.layer,
        "items": list(v.items),
        "nets": list(v.nets),
        "actual_value": v.actual_value,
        "required_value": v.required_value,
        "proposed_item": proposed,
    }


def _run_rule_set(pcb_path: Path, pcb: Any, manufacturer: str | None) -> Any:
    """``kct check``'s own rule-set builder, pointed at *pcb* (Issue #9590)."""
    from .check_cmd import build_check_rule_set

    # ``build_check_rule_set`` resolves the fab-view passes (gap policy,
    # copper oz, ``fab_view`` under jlcpcb) the same way the CLI does, so the
    # probe inherits them instead of hand-copying ``kct check``'s settings.
    return build_check_rule_set(pcb_path, manufacturer=manufacturer, pcb=pcb)


def _selected_findings(ruleset: Any, pcb_path: Path) -> Any:
    from .check_cmd import run_selected_checks

    results = run_selected_checks(
        ruleset.checker,
        set(PROBE_CHECK_CATEGORIES),
        set(),
        pcb_path=pcb_path,
        layers=ruleset.layers,
    )
    if ruleset.general_waivers is not None:
        from kicad_tools.validate.rules.waivers import apply_waivers

        apply_waivers(results, ruleset.general_waivers.for_check())
    return results


def probe_items(
    pcb_path: str | Path,
    items: list[ProbeItem],
    *,
    manufacturer: str | None = None,
    original_bytes: bytes | None = None,
    stat_marker: tuple[int, int] | None = None,
) -> _ProbeOutcome:
    """Check *items* against the board at *pcb_path*; write nothing.

    Returns the report dict (JSON shape of both commands) plus the board
    bytes / stat marker apply needs for its backup and write guard.
    """
    from kicad_tools.schema.pcb import PCB

    pcb_path = Path(pcb_path)
    if original_bytes is None:
        original_bytes = pcb_path.read_bytes()
    if stat_marker is None:
        stat_marker = _stat_marker(pcb_path)

    report: dict[str, Any] = {
        "categories": list(PROBE_CHECK_CATEGORIES),
        "items": [{"index": i.index, "type": i.kind, "net": i.net} for i in items],
        "problems": [],
        "warnings": [],
    }

    board = PCB.load(str(pcb_path))
    validation = _validate_against_board(items, board)
    if validation:
        # The batch cannot be appended as written; fail fast without running
        # the checker (nothing partial is ever checked or written).
        report["problems"] = validation
        report["error_count"] = len(validation)
        report["warning_count"] = 0
        report["ok"] = False
        return _ProbeOutcome(report, original_bytes, stat_marker)

    merged = PCB.load(str(pcb_path))
    descriptors = _append_for_check(merged, items)
    net_fallback: dict[str, set[int]] = {}
    for item in items:
        net_fallback.setdefault(item.net, set()).add(item.index)
        for number, net in board.nets.items():
            if net.name == item.net:
                net_fallback.setdefault(f"Net {number}", set()).add(item.index)
                break

    baseline_rs = _run_rule_set(pcb_path, board, manufacturer)
    merged_rs = _run_rule_set(pcb_path, merged, manufacturer)
    baseline = _selected_findings(baseline_rs, pcb_path)
    added = _selected_findings(merged_rs, pcb_path)

    # Findings the batch adds: a merged finding whose (key, location) is not
    # consumed by a baseline finding.  Counting (not set-diff) so a key that
    # already fired twice on the board and three times with the batch still
    # reports the one new occurrence.
    remaining = Counter((v.key, v.location) for v in baseline.violations)
    new = []
    for v in added.violations:
        marker = (v.key, v.location)
        if remaining.get(marker, 0) > 0:
            remaining[marker] -= 1
            continue
        new.append(v)

    def as_dict(v: Any) -> dict[str, Any]:
        return _violation_dict(v, descriptors, net_fallback)

    report["problems"] = [as_dict(v) for v in new if v.is_error]
    report["warnings"] = [as_dict(v) for v in new if not v.is_error and not v.is_info]
    report["error_count"] = len(report["problems"])
    report["warning_count"] = len(report["warnings"])
    report["ok"] = report["error_count"] == 0
    return _ProbeOutcome(report, original_bytes, stat_marker)


def _stat_marker(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_size)


def apply_items_checked(
    pcb_path: str | Path,
    items: list[ProbeItem],
    *,
    backup_path: str | Path | None = None,
    manufacturer: str | None = None,
) -> tuple[dict[str, Any], int]:
    """Probe, then write the items atomically or refuse; returns (report, exit).

    Exit ``0`` written; ``1`` probe problems (nothing written); ``2`` refused
    (board changed under us, or a KiCad lock under the ``error`` policy --
    nothing written either way).  On success the report carries
    ``items_written`` and ``items_skipped_as_duplicate`` (issue #9674): items
    whose dedupe key was already on the board are skipped by dedupe, and the
    report says so instead of counting them as written.  A via proposed at a
    position held by a via of a different size or drill is not a duplicate
    (size and drill are outside the via dedupe key): it is not written, and
    is counted under ``items_skipped_conflicting`` with one explanatory line
    per via in ``conflicting_via_details``.
    """
    from kicad_tools.schema.pcb import PCB

    pcb_path = Path(pcb_path)
    stat_marker = _stat_marker(pcb_path)
    original_bytes = pcb_path.read_bytes()

    outcome = probe_items(
        pcb_path,
        items,
        manufacturer=manufacturer,
        original_bytes=original_bytes,
        stat_marker=stat_marker,
    )
    report = outcome.report
    report["written"] = None
    report["backup"] = None
    report["refused"] = None
    report["items_written"] = None
    report["items_skipped_as_duplicate"] = None
    report["items_skipped_conflicting"] = None
    report["conflicting_via_details"] = None
    if not report["ok"]:
        return report, 1

    from kicad_tools.core.kicad_lock import check_kicad_lock

    try:
        check_kicad_lock(pcb_path)
    except PermissionError as exc:
        report["refused"] = str(exc)
        return report, 2

    board = PCB.load(str(pcb_path))
    items_written, items_skipped, conflicting = _append_for_write(board, items)

    backup = Path(backup_path) if backup_path else pcb_path.with_suffix(pcb_path.suffix + ".bak")
    tmp_path = pcb_path.with_suffix(pcb_path.suffix + ".probe-tmp")
    try:
        board.save(tmp_path)
        # Re-stat immediately before the replace: the board must be exactly
        # the bytes that were read and probed (Issue #9590, row 4 of #9592).
        if _stat_marker(pcb_path) != stat_marker:
            report["refused"] = (
                f"{pcb_path} changed on disk since it was read (mtime/size differ); nothing written"
            )
            return report, 2
        backup.write_bytes(original_bytes)
        os.replace(tmp_path, pcb_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    report["written"] = str(pcb_path)
    report["backup"] = str(backup)
    report["items_written"] = items_written
    report["items_skipped_as_duplicate"] = items_skipped
    report["items_skipped_conflicting"] = len(conflicting)
    report["conflicting_via_details"] = conflicting
    return report, 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def add_probe_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument("items", help="Items JSON file (see kct probe --help)")
    parser.add_argument(
        "--mfr",
        default=None,
        help="Manufacturer rules (default: resolved like kct check -- fab_profile.json > project.kct > jlcpcb)",
    )
    add_format_flag(parser)


def add_apply_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument("items", help="Items JSON file (see kct apply --help)")
    parser.add_argument(
        "--checked",
        action="store_true",
        required=True,
        help="Probe first and write only if every item passes (the only mode)",
    )
    parser.add_argument(
        "--backup",
        default=None,
        metavar="PATH",
        help="Backup path for the previous bytes (default: <pcb>.kicad_pcb.bak)",
    )
    parser.add_argument(
        "--mfr",
        default=None,
        help="Manufacturer rules (default: resolved like kct check -- fab_profile.json > project.kct > jlcpcb)",
    )
    add_format_flag(parser)


def _print_report_lines(report: dict[str, Any], out=sys.stderr) -> None:
    for problem in report["problems"]:
        _print_finding("problem", problem, out)
    for warning in report["warnings"]:
        _print_finding("warning", warning, out)


def _print_finding(label: str, finding: dict[str, Any], out) -> None:
    parts = [f"  {label}: [{finding['rule_id']}] {finding['message']}"]
    if finding.get("proposed_item") is not None:
        parts.append(f"(item {finding['proposed_item']})")
    if finding.get("location") is not None:
        x, y = finding["location"]
        parts.append(f"at {x:g},{y:g}")
    if finding.get("layer"):
        parts.append(f"on {finding['layer']}")
    print(" ".join(parts), file=out)


def _load_items_or_fail(args: argparse.Namespace, fail) -> list[ProbeItem] | None:
    pcb_path = Path(args.pcb)
    if not pcb_path.is_file():
        fail(f"board not found: {pcb_path}")
        return None
    try:
        return load_items_file(args.items)
    except ItemsFileError as exc:
        fail(str(exc))
        return None


def run_probe_command(args: argparse.Namespace) -> int:
    """Entry point for ``kct probe``: exit 1 on any problem, 2 never writes."""
    as_json = args.format == FORMAT_JSON

    def fail(message: str) -> int:
        if as_json:
            emit_json({"command": "probe", "ok": False, "error": message})
        else:
            print(f"probe: {message}", file=sys.stderr)
        return 1

    items = _load_items_or_fail(args, fail)
    if items is None:
        return 1

    with stdout_to_stderr_when(as_json):
        outcome = probe_items(Path(args.pcb), items, manufacturer=args.mfr)
    report = outcome.report
    report["command"] = "probe"
    report["written"] = None

    if as_json:
        emit_json(report)
    else:
        print(f"probe: {len(items)} item(s) against {args.pcb}")
        _print_report_lines(report, out=sys.stderr)
        if report["ok"]:
            print(
                f"probe: clean ({report['warning_count']} warning(s)); board not written"
                if report["warning_count"]
                else "probe: clean; board not written"
            )
        else:
            print(
                f"probe: {report['error_count']} problem(s); board not written",
                file=sys.stderr,
            )
    return 0 if report["ok"] else 1


def run_apply_command(args: argparse.Namespace) -> int:
    """Entry point for ``kct apply --checked``: 0 written, 1 problems, 2 refused."""
    as_json = args.format == FORMAT_JSON

    def fail(message: str) -> int:
        if as_json:
            emit_json({"command": "apply", "ok": False, "error": message})
        else:
            print(f"apply: {message}", file=sys.stderr)
        return 1

    items = _load_items_or_fail(args, fail)
    if items is None:
        return 1

    with stdout_to_stderr_when(as_json):
        report, code = apply_items_checked(
            Path(args.pcb), items, backup_path=args.backup, manufacturer=args.mfr
        )
    report["command"] = "apply"

    if as_json:
        emit_json(report)
        return code

    _print_report_lines(report, out=sys.stderr)
    if report["refused"] is not None:
        print(f"apply: refused: {report['refused']}", file=sys.stderr)
    elif report["written"] is None:
        print(
            f"apply: probe failed with {report['error_count']} problem(s); board left untouched",
            file=sys.stderr,
        )
    else:
        line = (
            f"apply: wrote {report['items_written']} item(s) to {report['written']} "
            f"(backup: {report['backup']})"
        )
        if report["items_skipped_as_duplicate"]:
            line += (
                f"; skipped {report['items_skipped_as_duplicate']} item(s) already "
                "on the board (duplicate)"
            )
        if report["items_skipped_conflicting"]:
            line += (
                f"; skipped {report['items_skipped_conflicting']} item(s) at a position "
                "held by a different via (not written)"
            )
        print(line)
        for detail in report["conflicting_via_details"] or []:
            print(f"apply: {detail}")
    return code
