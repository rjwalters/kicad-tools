"""Stable finding keys and local-evidence hashes for ``kct check`` (Issue #5946).

Two identities are attached to every :class:`~kicad_tools.validate.violations.DRCViolation`:

``key``
    *What* the finding is about: the rule id, the sorted item references, the
    sorted net names and the layer, joined as
    ``rule_id|item,item|net,net|layer``.  The key deliberately excludes the
    location and measured values, so it survives a board edit that moves the
    finding a little or changes its message wording.  ``kct check --diff`` pairs
    findings across two revisions by key, and evidence-bound waivers name the
    finding they acknowledge by key.

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
# within :func:`net_evidence_radius` of it, with their board positions.
# Waivers recorded against an older version go stale exactly once after an
# upgrade, and are reported as such (see :func:`is_outdated_evidence_hash`).
EVIDENCE_HASH_VERSION = "ev2"
EVIDENCE_HASH_PREFIX = EVIDENCE_HASH_VERSION + ":"

# Default radius (mm, board coordinates) around a located finding within
# which a named net's pads count as evidence (Issue #6011).
DEFAULT_NET_EVIDENCE_RADIUS_MM = 5.0

# Per-rule radius overrides, matched by rule-id prefix (first match wins).
# Clearance / width style findings are about the copper right at the
# finding, so a few mm is plenty.
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
    for prefix, radius in NET_EVIDENCE_RADIUS_BY_PREFIX:
        if rule_id.startswith(prefix):
            return radius
    return DEFAULT_NET_EVIDENCE_RADIUS_MM


def evidence_hash_version(evidence_hash: str | None) -> str | None:
    """Return the version tag (``"ev1"``, ``"ev2"``, ...) of an evidence hash."""
    if not evidence_hash or ":" not in evidence_hash:
        return None
    return evidence_hash.split(":", 1)[0]


def is_outdated_evidence_hash(evidence_hash: str | None) -> bool:
    """True when ``evidence_hash`` was computed by an older evidence recipe.

    Such a hash can never match a hash computed today, whatever the board
    looks like, so a waiver carrying it is stale *because kct was upgraded*,
    not (necessarily) because the board changed.
    """
    version = evidence_hash_version(evidence_hash)
    return version is not None and version != EVIDENCE_HASH_VERSION


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


# (ident, board_x, board_y, half_extent) for one pad of a net.
_NetPad = tuple[str, float, float, float]


def _net_pads(pcb: PCB) -> dict[str, list[_NetPad]]:
    """Every net's pads in board coordinates (footprint position + rotation)."""
    from kicad_tools.core.geometry import rotate_pad_offset

    pads_by_net: dict[str, list[_NetPad]] = {}
    for fp in pcb.footprints:
        ref = getattr(fp, "reference", "")
        fx, fy = fp.position
        for pad in getattr(fp, "pads", []) or []:
            if not pad.net_name:
                continue
            ox, oy = rotate_pad_offset(pad.position[0], pad.position[1], fp.rotation or 0.0)
            size = getattr(pad, "size", None) or (0.0, 0.0)
            half = max(float(size[0]), float(size[1])) / 2.0
            pads_by_net.setdefault(pad.net_name, []).append(
                (f"{ref}.{pad.number}", fx + ox, fy + oy, half)
            )
    return pads_by_net


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
    """Pads whose copper comes within ``radius`` of ``bbox``, with board positions."""
    x0, y0, x1, y1 = bbox
    local: list[list[Any]] = []
    for ident, x, y, half in pads:
        dx = max(x0 - x, 0.0, x - x1)
        dy = max(y0 - y, 0.0, y - y1)
        if (dx * dx + dy * dy) ** 0.5 - half <= radius:
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
