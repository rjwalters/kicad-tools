"""Stable finding keys and local-evidence hashes for ``kct check`` (Issue #5946).

Two identities are attached to every :class:`~kicad_tools.validate.violations.DRCViolation`:

``key``
    *What* the finding is about: the rule id, the sorted item references, the
    sorted net names and the layer, joined as
    ``rule_id|item,item|net,net|layer``.  The key deliberately excludes the
    location and measured values, so it survives a board edit that moves the
    finding a little or changes its message wording.  ``kct check --diff`` pairs
    findings across two revisions by key, and evidence-bound waivers name the
    finding they acknowledge by key.  Tracks, arcs and vias appear in
    ``items`` under geometry descriptors (``Trace@F.Cu:w0.25:1/2~3/2``,
    Issue #6088; see :mod:`kicad_tools.validate.copper_refs`), not their
    UUIDs, so a re-route that reproduces the same copper keeps its keys.

``evidence_hash``
    *The local evidence* that produced the finding: its rounded location and
    closest points, its measured and required values, the placement and pad
    geometry (with pad nets) of every footprint it names, and the pad
    membership of every net it names (and, when several findings share one
    key, how many do -- see :func:`evidence_payload`).  For a finding with a
    location, a net's membership is limited to the pads *near the finding*
    (Issue #6011, see :func:`net_evidence_radius`), so adding a ``GND`` pad
    across the board does not re-stale every waiver that names ``GND``;
    whole-net rules and findings without a location keep the full
    membership.  When the copper or nets under a finding
    change, the hash changes -- that is what makes an evidence-bound waiver go
    **stale** instead of silently suppressing a finding whose geometry nobody
    has reviewed.

Both are pure functions of the finding (plus the loaded board, for the
footprint / net part of the evidence), so the same board always yields the
same keys and hashes.  Values are rounded (positions to 1 um, measurements to
0.1 um) so float noise from re-serialization cannot flip a hash.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

    from .violations import DRCResults, DRCViolation

# Field separator inside a finding key.  ``|`` never appears in a KiCad
# reference designator and is vanishingly rare in a net name.
KEY_SEPARATOR = "|"

# Hash prefix so a reader can tell the algorithm at a glance, and so a future
# change to the evidence recipe can be versioned without ambiguity.
#
# ``ev1`` (Issue #5946): every named net contributed its full pad membership.
# ``ev2`` (Issue #6011): a located finding's nets contribute only the pads
# within :func:`net_evidence_radius` of it, with their positions in sheet
# (file) coordinates -- the frame ``kct check`` reports locations in
# (Issue #6055 fixed ev2 in place, before any release carried it: pads used
# to be compared in the board-relative frame, so on a board whose outline
# does not start at (0, 0) the nearby-pad set came out empty).
# Waivers recorded against an older version go stale exactly once after an
# upgrade, and are reported as such (see :func:`is_outdated_evidence_hash`).
EVIDENCE_HASH_VERSION = "ev2"
EVIDENCE_HASH_PREFIX = EVIDENCE_HASH_VERSION + ":"

# ``kct detect-mistakes`` hashes (Issue #6006) add the check's structured
# measurements to the recipe above, so they carry a compound version
# ``<evidence version>.<measurement recipe>`` (``ev2.m1``).  Bumping either
# part makes mistake waivers report ``outdated_evidence_version``; bumping
# only the ``m`` part leaves every ``kct check`` waiver untouched.
#
# ``m1``: the structured ``Mistake.measurements`` mapping, thresholds excluded.
MISTAKE_MEASUREMENT_RECIPE = "m1"
MISTAKE_EVIDENCE_HASH_VERSION = f"{EVIDENCE_HASH_VERSION}.{MISTAKE_MEASUREMENT_RECIPE}"
MISTAKE_EVIDENCE_HASH_PREFIX = MISTAKE_EVIDENCE_HASH_VERSION + ":"

# Default radius (mm) around a located finding within
# which a named net's pads count as evidence (Issue #6011).
DEFAULT_NET_EVIDENCE_RADIUS_MM = 5.0

# Per-rule radius overrides.  Clearance / width / dimension style findings
# are about the copper right at the finding, so a few mm is plenty.
#
# Exact rule ids first (Issue #6041): the bare ids below would otherwise miss
# every prefix (``clearance`` is not ``clearance_*``) and fall through to the
# 5 mm default, contradicting the documented 3 mm for these rules.
NET_EVIDENCE_RADIUS_BY_RULE: dict[str, float] = {
    "clearance": 3.0,
    "dimensions": 3.0,
    "mask_to_copper": 3.0,
    "width_consistency": 3.0,
}

# Then by rule-id prefix (first match wins).
NET_EVIDENCE_RADIUS_BY_PREFIX: tuple[tuple[str, float], ...] = (
    ("clearance_", 3.0),
    ("edge_clearance_", 3.0),
    ("dimension_", 3.0),
    ("netclass_", 3.0),
    ("hole_to_hole", 3.0),
    ("pth_", 3.0),
    ("solder_mask", 3.0),
)

# Rules whose verdict depends on the topology of the whole net: their
# findings keep the full pad membership of every named net, wherever the
# finding is located (Issue #6011).
GLOBAL_NET_EVIDENCE_RULES = frozenset(
    {
        "ampacity",
        "connectivity",
        "dangling_copper",
        "diffpair_length_skew",
        "diffpair_routing_continuity",
        "impedance",
        "isolated_copper",
        "match_group_length_skew",
        "net_undeclared",
        "path_ampacity",
        "single_pad_net",
        "track_dangling",
        "via_dangling",
        "zone_no_net",
    }
)
GLOBAL_NET_EVIDENCE_PREFIXES: tuple[str, ...] = ("unconnected",)

_POS_DIGITS = 3  # 1 um
_VALUE_DIGITS = 4  # 0.1 um

# Separators that split an item descriptor such as ``U1.3`` / ``U1-pad3`` /
# ``U1:3`` / ``U1 pad 3`` into its leading reference designator.
_REF_SPLIT = re.compile(r"[.\-:/ ]")


def finding_key(
    rule_id: str,
    items: tuple[str, ...] | list[str] = (),
    nets: tuple[str, ...] | list[str] = (),
    layer: str | None = None,
) -> str:
    """Build the stable key ``rule_id|items|nets|layer`` for a finding."""
    return KEY_SEPARATOR.join(
        (
            rule_id,
            ",".join(sorted(items)),
            ",".join(sorted(nets)),
            layer or "",
        )
    )


def net_evidence_radius(rule_id: str) -> float | None:
    """Radius (mm) of the net-membership evidence for ``rule_id``.

    ``None`` means the rule depends on whole-net topology and keeps the full
    membership of every net it names.
    """
    if rule_id in GLOBAL_NET_EVIDENCE_RULES or rule_id.startswith(GLOBAL_NET_EVIDENCE_PREFIXES):
        return None
    exact = NET_EVIDENCE_RADIUS_BY_RULE.get(rule_id)
    if exact is not None:
        return exact
    for prefix, radius in NET_EVIDENCE_RADIUS_BY_PREFIX:
        if rule_id.startswith(prefix):
            return radius
    return DEFAULT_NET_EVIDENCE_RADIUS_MM


def evidence_hash_version(evidence_hash: str | None) -> str | None:
    """Return the version tag (``"ev1"``, ``"ev2"``, ...) of an evidence hash."""
    if not evidence_hash or ":" not in evidence_hash:
        return None
    return evidence_hash.split(":", 1)[0]


def current_evidence_hash_version(evidence_hash: str | None) -> str:
    """The version this kct computes for hashes of ``evidence_hash``'s family.

    A compound version (``ev1.m1``, ``ev2.m1``) is a ``kct detect-mistakes``
    hash, compared against :data:`MISTAKE_EVIDENCE_HASH_VERSION`; anything
    else is a ``kct check`` hash, compared against
    :data:`EVIDENCE_HASH_VERSION`.
    """
    version = evidence_hash_version(evidence_hash)
    if version is not None and "." in version:
        return MISTAKE_EVIDENCE_HASH_VERSION
    return EVIDENCE_HASH_VERSION


def is_outdated_evidence_hash(evidence_hash: str | None) -> bool:
    """True when ``evidence_hash`` was computed by an older evidence recipe.

    Such a hash can never match a hash computed today, whatever the board
    looks like, so a waiver carrying it is stale *because kct was upgraded*,
    not (necessarily) because the board changed.
    """
    version = evidence_hash_version(evidence_hash)
    return version is not None and version != current_evidence_hash_version(evidence_hash)


def _round(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    rounded = round(float(value), digits)
    # Normalize -0.0 so the canonical JSON is stable.
    return 0.0 if rounded == 0 else rounded


def _point(point: tuple[float, float] | None) -> list[float | None] | None:
    if point is None:
        return None
    return [_round(point[0], _POS_DIGITS), _round(point[1], _POS_DIGITS)]


def _footprint_index(pcb: PCB) -> dict[str, Any]:
    index: dict[str, Any] = {}
    for fp in pcb.footprints:
        ref = getattr(fp, "reference", "")
        if ref and ref not in index:
            index[ref] = fp
    return index


def _resolve_ref(item: str, footprints: dict[str, Any]) -> str | None:
    """Map an item descriptor (``U1``, ``U1.3``, ``U1-pad3``) to a footprint ref."""
    if item in footprints:
        return item
    head = _REF_SPLIT.split(item, maxsplit=1)[0]
    if head in footprints:
        return head
    return None


def _footprint_evidence(fp: Any) -> dict[str, Any]:
    pads = []
    for pad in getattr(fp, "pads", []) or []:
        pads.append(
            [
                str(pad.number),
                pad.net_name or "",
                _round(pad.position[0], _POS_DIGITS),
                _round(pad.position[1], _POS_DIGITS),
                _round(pad.size[0], _POS_DIGITS),
                _round(pad.size[1], _POS_DIGITS),
                _round(getattr(pad, "rotation", 0.0), _VALUE_DIGITS),
            ]
        )
    pads.sort(key=lambda row: (row[0], row[1], str(row[2:])))
    return {
        "at": _point(fp.position),
        "rot": _round(fp.rotation, _VALUE_DIGITS),
        "layer": fp.layer,
        "footprint": getattr(fp, "name", ""),
        "pads": pads,
    }


def _net_members(pcb: PCB) -> dict[str, list[str]]:
    members: dict[str, list[str]] = {}
    for fp in pcb.footprints:
        ref = getattr(fp, "reference", "")
        for pad in getattr(fp, "pads", []) or []:
            if pad.net_name:
                members.setdefault(pad.net_name, []).append(f"{ref}.{pad.number}")
    for names in members.values():
        names.sort()
    return members


# Copper outline of one pad, in the pad's own frame, as a (possibly
# degenerate) axis-aligned core rectangle inflated by a radius:
# (board_rotation_deg, core_half_w, core_half_h, inflate_radius).
#
# * ``rect`` / ``roundrect`` / ``trapezoid`` / ``custom`` / unknown: the full
#   rectangle, radius 0.  A roundrect's copper lies inside it, so this is
#   exact for rect and conservative (never excludes copper) for the rest.
# * ``circle``: a point core inflated by the radius.
# * ``oval``: a segment core along the major axis inflated by half the minor
#   axis (a stadium), exactly KiCad's oval.
_PadShape = tuple[float, float, float, float]

# (ident, sheet_x, sheet_y, shape) for one pad of a net.
_NetPad = tuple[str, float, float, _PadShape]


def _pad_shape(pad: Any) -> _PadShape:
    size = getattr(pad, "size", None) or (0.0, 0.0)
    w = max(float(size[0]), 0.0)
    h = max(float(size[1]), 0.0)
    rot = float(getattr(pad, "rotation", 0.0) or 0.0)
    shape = str(getattr(pad, "shape", "") or "").lower()
    if shape == "circle":
        return (rot, 0.0, 0.0, max(w, h) / 2.0)
    if shape in ("oval", "obround"):
        r = min(w, h) / 2.0
        return (rot, w / 2.0 - r, h / 2.0 - r, r)
    return (rot, w / 2.0, h / 2.0, 0.0)


def _board_origin(pcb: PCB) -> tuple[float, float]:
    """``pcb.board_origin`` as floats, ``(0, 0)`` when absent or malformed."""
    origin = getattr(pcb, "board_origin", None)
    try:
        return (float(origin[0]), float(origin[1])) if origin else (0.0, 0.0)
    except (TypeError, ValueError, IndexError):
        return (0.0, 0.0)


def _net_pads(pcb: PCB) -> dict[str, list[_NetPad]]:
    """Every net's pads in **sheet** coordinates (footprint position + rotation).

    ``PCB.load()`` stores footprint positions *board-relative* (the Edge.Cuts
    minimum corner is subtracted at load time), while ``kct check`` reports
    finding locations in sheet coordinates -- the literal ``(at ...)`` values
    of the file, see ``DRCChecker._absolutize`` -- and ``kct detect-mistakes``
    shifts its locations to the same frame.  The board origin is added back
    here so pads and findings are compared in one frame (Issue #6055).
    """
    from kicad_tools.core.geometry import rotate_pad_offset

    origin_x, origin_y = _board_origin(pcb)
    pads_by_net: dict[str, list[_NetPad]] = {}
    for fp in pcb.footprints:
        ref = getattr(fp, "reference", "")
        fx = fp.position[0] + origin_x
        fy = fp.position[1] + origin_y
        for pad in getattr(fp, "pads", []) or []:
            if not pad.net_name:
                continue
            ox, oy = rotate_pad_offset(pad.position[0], pad.position[1], fp.rotation or 0.0)
            pads_by_net.setdefault(pad.net_name, []).append(
                (f"{ref}.{pad.number}", fx + ox, fy + oy, _pad_shape(pad))
            )
    return pads_by_net


def _point_rect_distance(x: float, y: float, hx: float, hy: float) -> float:
    """Distance from ``(x, y)`` to the solid rectangle ``[-hx, hx] x [-hy, hy]``."""
    return math.hypot(max(abs(x) - hx, 0.0), max(abs(y) - hy, 0.0))


def _point_segment_distance(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> float:
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0.0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _segment_hits_rect(ax: float, ay: float, bx: float, by: float, hx: float, hy: float) -> bool:
    """Liang-Barsky: does segment ``a-b`` touch ``[-hx, hx] x [-hy, hy]``?"""
    dx, dy = bx - ax, by - ay
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, ax + hx), (dx, hx - ax), (-dy, ay + hy), (dy, hy - ay)):
        if p == 0.0:
            if q < 0.0:
                return False
            continue
        t = q / p
        if p < 0.0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return True


def _segment_rect_distance(
    ax: float, ay: float, bx: float, by: float, hx: float, hy: float
) -> float:
    """Distance from segment ``a-b`` to the solid rectangle ``[-hx, hx] x [-hy, hy]``."""
    if _segment_hits_rect(ax, ay, bx, by, hx, hy):
        return 0.0
    # Disjoint convex sets: the closest pair involves a vertex of one of them.
    best = min(_point_rect_distance(ax, ay, hx, hy), _point_rect_distance(bx, by, hx, hy))
    for cx, cy in ((-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)):
        best = min(best, _point_segment_distance(cx, cy, ax, ay, bx, by))
    return best


def _pad_bbox_distance(
    x: float, y: float, shape: _PadShape, bbox: tuple[float, float, float, float]
) -> float:
    """Exact distance from a pad's copper outline to the anchor ``bbox`` (0 if they touch).

    The bbox (a point, segment or rectangle in sheet coordinates) is moved
    into the pad's frame -- undoing the pad's absolute board rotation with
    the same KiCad convention used to place it -- and measured against the
    pad's core rectangle, less the shape's inflate radius.
    """
    from kicad_tools.core.geometry import rotate_pad_offset

    rot, hx, hy, inflate = shape
    x0, y0, x1, y1 = bbox
    if x0 <= x <= x1 and y0 <= y <= y1:
        return 0.0  # pad centre inside the bbox
    corners = [
        rotate_pad_offset(cx - x, cy - y, -rot) if rot else (cx - x, cy - y)
        for cx, cy in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    ]
    dist = min(
        _segment_rect_distance(*a, *b, hx, hy)
        for a, b in zip(corners, corners[1:] + corners[:1], strict=True)
    )
    return max(dist - inflate, 0.0)


class EvidenceContext:
    """Per-board lookup tables shared by every hash computed for one run."""

    def __init__(self, pcb: PCB | None) -> None:
        self.footprints: dict[str, Any] = _footprint_index(pcb) if pcb is not None else {}
        self.net_members: dict[str, list[str]] = _net_members(pcb) if pcb is not None else {}
        self.net_pads: dict[str, list[_NetPad]] = _net_pads(pcb) if pcb is not None else {}
        self.has_board = pcb is not None


def _anchor_bbox(violation: DRCViolation) -> tuple[float, float, float, float] | None:
    """Bounding box of the finding's ``location`` and ``closest_locations``."""
    points = [p for p in (violation.location, *violation.closest_locations) if p is not None]
    if not points:
        return None
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _local_net_pads(
    pads: list[_NetPad], bbox: tuple[float, float, float, float], radius: float
) -> list[list[Any]]:
    """Pads whose copper comes within ``radius`` of ``bbox``, with sheet positions."""
    x0, y0, x1, y1 = bbox
    local: list[list[Any]] = []
    for ident, x, y, shape in pads:
        # Cheap reject on the pad's enclosing circle, then the exact outline
        # distance (Issue #6011 review: a rectangle's corner reaches further
        # than ``max(w, h) / 2``).
        dx = max(x0 - x, 0.0, x - x1)
        dy = max(y0 - y, 0.0, y - y1)
        _rot, hx, hy, inflate = shape
        if math.hypot(dx, dy) - (math.hypot(hx, hy) + inflate) > radius:
            continue
        if _pad_bbox_distance(x, y, shape, bbox) <= radius:
            local.append([ident, _round(x, _POS_DIGITS), _round(y, _POS_DIGITS)])
    local.sort(key=lambda row: (row[0], str(row[1:])))
    return local


def evidence_payload(
    violation: DRCViolation,
    context: EvidenceContext | None = None,
    *,
    multiplicity: int = 1,
) -> dict:
    """Return the canonical evidence document hashed by :func:`compute_evidence_hash`.

    ``multiplicity`` is how many findings on the board share this finding's
    key.  Findings that share a key cannot be told apart by it, so when there
    is more than one the count joins the evidence: adding (or removing) a
    look-alike finding then changes every hash under that key, and a waiver
    reviewed against the old population goes stale instead of silently
    covering the newcomer.  A unique key (the common case) leaves the payload
    -- and so the hash -- exactly as it was.
    """
    ctx = context or EvidenceContext(None)
    payload: dict[str, Any] = {
        "key": violation.key,
        "location": _point(violation.location),
        "closest": [_point(p) for p in violation.closest_locations],
        "actual": _round(violation.actual_value, _VALUE_DIGITS),
        "required": _round(violation.required_value, _VALUE_DIGITS),
    }
    if multiplicity > 1:
        payload["multiplicity"] = multiplicity
    if ctx.has_board:
        footprints: dict[str, Any] = {}
        for item in violation.items:
            ref = _resolve_ref(item, ctx.footprints)
            if ref is not None and ref not in footprints:
                footprints[ref] = _footprint_evidence(ctx.footprints[ref])
        payload["footprints"] = dict(sorted(footprints.items()))
        nets = sorted(set(violation.nets))
        radius = net_evidence_radius(violation.rule_id)
        bbox = _anchor_bbox(violation)
        if radius is None or bbox is None or not nets:
            # Whole-net rules and unlocated findings: full membership (ev1 recipe).
            payload["nets"] = {net: ctx.net_members.get(net, []) for net in nets}
        else:
            # Issue #6011: only the pads near the finding, with positions, so a
            # far-away pad on the same net leaves the hash alone while a
            # nearby pad that is added, removed or moved still changes it.
            payload["nets"] = {
                net: _local_net_pads(ctx.net_pads.get(net, []), bbox, radius) for net in nets
            }
            payload["net_radius"] = _round(radius, _VALUE_DIGITS)
    return payload


def compute_evidence_hash(
    violation: DRCViolation,
    context: EvidenceContext | None = None,
    *,
    multiplicity: int = 1,
) -> str:
    """Hash the local evidence of ``violation`` (see module docstring)."""
    canonical = json.dumps(
        evidence_payload(violation, context, multiplicity=multiplicity),
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return EVIDENCE_HASH_PREFIX + digest


def annotate_evidence(results: DRCResults, pcb: PCB | None) -> None:
    """Attach an ``evidence_hash`` to every finding in ``results`` that lacks one.

    Only :class:`~kicad_tools.validate.violations.DRCViolation` instances are
    annotated; any other finding object that reached the list (e.g. a test
    double or a foreign model) is left untouched.
    """
    from .violations import DRCViolation

    def _needs(v: object) -> bool:
        return isinstance(v, DRCViolation) and not v.evidence_hash

    if not any(_needs(v) for v in results.violations):
        return
    context = EvidenceContext(pcb)
    counts = Counter(v.key for v in results.violations if isinstance(v, DRCViolation))
    results.violations = [
        replace(v, evidence_hash=compute_evidence_hash(v, context, multiplicity=counts[v.key]))
        if _needs(v)
        else v
        for v in results.violations
    ]
