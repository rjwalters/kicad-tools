"""PCB file parsing and optimization."""

from __future__ import annotations

import fnmatch
import logging
import math
import re
import tempfile
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ...core.layers import kicad_read_via_layers, warn_normalised_vias
from ..layers import Layer
from ..primitives import Segment, Via, segment_45_enforcement_disabled
from .config import NetOptimizationStats, OptimizationConfig, OptimizationStats
from .geometry import count_corners, total_length

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# Issue #3118 / #6493: the optional via-type token at the head of a
# ``(via ...)`` block.  Shared by every regex via parser so they agree on
# which blocks are keyword-less (and so read as through vias by KiCad).
VIA_TYPE_RE = re.compile(r"^\(via\s+(micro|blind|buried)\b")


def via_block_type(block: str) -> str | None:
    """Return the ``micro`` / ``blind`` / ``buried`` token of a via block, or ``None``."""
    m = VIA_TYPE_RE.match(block)
    return m.group(1) if m else None


def _decode_net_string(token: str) -> str:
    """Decode quoted KiCad names with the canonical S-expression decoder."""
    from kicad_tools.sexp import parse_string

    return str(parse_string(f"(name {token})").children[0].value)


def parse_net_names(pcb_text: str) -> dict[int, str]:
    """Parse net ID to name mapping from PCB file."""
    net_names: dict[int, str] = {}

    # Match net declarations: (net N "name")
    pattern = re.compile(r'\(net\s+(\d+)\s+("(?:\\.|[^"\\])*")\)')
    for match in pattern.finditer(pcb_text):
        net_id = int(match.group(1))
        net_name = _decode_net_string(match.group(2))
        if net_name:  # Skip empty net names
            net_names[net_id] = net_name

    return net_names


def _extract_balanced_blocks(text: str, keyword: str) -> list[tuple[int, int, str]]:
    """Extract balanced S-expression blocks starting with ``(keyword ...)``.

    Walks *text* looking for occurrences of ``(keyword`` followed by
    whitespace.  For each hit it counts balanced parentheses to find the
    matching close, returning a list of ``(start, end, block_text)``
    tuples.  This correctly handles nested sub-expressions like
    ``(uuid "...")``, ``(locked yes)``, or any other field that contains
    parentheses.
    """
    blocks: list[tuple[int, int, str]] = []
    opener = f"({keyword}"
    start: int | None = None
    depth = 0
    # Consume complete strings/comments/atoms so neither quoted parentheses
    # nor a literal "(segment ..." inside a property is treated as syntax.
    tokens = re.finditer(r'[#;][^\n]*|"(?:\\.|[^"\\])*"|[()]|[^\s()]+', text)
    for match in tokens:
        token = match.group()
        if start is None:
            pos = match.start()
            after = pos + len(opener)
            if (
                token == "("
                and text.startswith(opener, pos)
                and after < len(text)
                and (text[after].isspace() or text[after] == ")")
            ):
                start = pos
                depth = 1
        elif token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
            if depth == 0:
                end = match.end()
                blocks.append((start, end, text[start:end]))
                start = None
    if start is not None:
        raise ValueError(f"Unbalanced {keyword} S-expression")
    return blocks


# Field-extraction patterns used by parse_segments.  These match
# individual fields *anywhere* inside a balanced segment block so the
# parser is order-independent.
_RE_START = re.compile(r"\(start\s+([\d.eE+-]+)\s+([\d.eE+-]+)\)")
_RE_END = re.compile(r"\(end\s+([\d.eE+-]+)\s+([\d.eE+-]+)\)")
_RE_WIDTH = re.compile(r"\(width\s+([\d.eE+-]+)\)")
_RE_LAYER = re.compile(r'\(layer\s+"([^"]+)"\)')
_RE_NET = re.compile(r"\(net\s+(\d+)\)")
# Issue #4413: KiCad-10 name-based net dialect -- segments/vias reference
# their net by quoted name instead of a numeric id, e.g.
# ``(net "Net-(C11-Pad2)")``.  The numeric ``_RE_NET`` above matches
# nothing on such blocks, so before this pattern existed the parser
# skipped EVERY segment/via on a name-based board.  When those parsers
# feed ``--preserve-existing`` / ``--nets``, an empty preserved set let
# the dialect-blind strip pass delete all existing copper on save
# (silent, catastrophic data loss).
_RE_NET_NAME = re.compile(r'\(net\s+("(?:\\.|[^"\\])*")\)')


def _resolve_block_net(
    block: str,
    net_names: dict[int, str],
    name_to_id: dict[str, int],
) -> tuple[int, str] | None:
    """Resolve the net id + name referenced inside a segment/via *block*.

    Handles both KiCad net-reference dialects (issue #4413):

    * numeric  ``(net N)``        -> id ``N``, name from the header table.
    * name     ``(net "NAME")``   -> id from the ``name_to_id`` reverse map,
      name ``NAME``.

    Returns ``None`` only when the block carries no ``(net ...)`` field at
    all (a genuinely malformed block).  A name-only reference whose name is
    absent from the header table is deliberately **not** dropped: it is
    kept with a best-effort net id of ``0`` so pre-existing copper is never
    silently discarded -- dropping copper is exactly the failure mode this
    resolver fixes.
    """
    m_net = _RE_NET.search(block)
    if m_net:
        net = int(m_net.group(1))
        return net, net_names.get(net, f"Net{net}")

    m_name = _RE_NET_NAME.search(block)
    if m_name:
        net_name = _decode_net_string(m_name.group(1))
        # An empty inline name is the KiCad no-net (net 0) reference.
        if not net_name:
            return 0, "Net0"
        return name_to_id.get(net_name, 0), net_name

    return None


# Public alias so the other copper-block scanners
# (``partial_rescue.strip_net_copper``, ``io._remove_conflicting_vias``,
# ``quantize``) resolve both net-reference dialects through this single
# implementation instead of each re-deriving a numeric-only ``(net N)``
# regex -- the bug class fixed by issue #4529.  Kept as a thin alias so
# existing in-module callers of ``_resolve_block_net`` are unaffected.
resolve_block_net = _resolve_block_net


def parse_segments(pcb_text: str) -> dict[str, list[Segment]]:
    """Parse segments from PCB file text, grouped by net name.

    Uses a balanced-parentheses walker so that fields may appear in any
    order and extra fields (``uuid``, ``locked``, ``tstamp``, etc.) are
    silently ignored.
    """
    segments_by_net: dict[str, list[Segment]] = {}

    # First, build net ID to name mapping (and its reverse, so name-based
    # ``(net "NAME")`` references resolve to numeric ids -- issue #4413).
    net_names = parse_net_names(pcb_text)
    name_to_id = {name: nid for nid, name in net_names.items()}

    for _start, _end, block in _extract_balanced_blocks(pcb_text, "segment"):
        m_start = _RE_START.search(block)
        m_end = _RE_END.search(block)
        m_width = _RE_WIDTH.search(block)
        m_layer = _RE_LAYER.search(block)
        resolved_net = _resolve_block_net(block, net_names, name_to_id)

        # All five core fields are required; skip malformed blocks.  The
        # net resolver handles both the numeric ``(net N)`` and name-based
        # ``(net "NAME")`` dialects, so a name-only board no longer drops
        # every segment here (#4413).
        if not (m_start and m_end and m_width and m_layer and resolved_net):
            continue

        x1 = float(m_start.group(1))
        y1 = float(m_start.group(2))
        x2 = float(m_end.group(1))
        y2 = float(m_end.group(2))
        width = float(m_width.group(1))
        layer_name = m_layer.group(1)
        net, net_name = resolved_net

        # Convert layer name to Layer enum
        try:
            layer = Layer.from_kicad_name(layer_name)
        except ValueError:
            layer = Layer.F_CU  # Default for unknown layers

        seg = Segment(
            x1=x1,
            y1=y1,
            x2=x2,
            y2=y2,
            width=width,
            layer=layer,
            net=net,
            net_name=net_name,
        )

        if net_name not in segments_by_net:
            segments_by_net[net_name] = []
        segments_by_net[net_name].append(seg)

    return segments_by_net


def parse_vias(pcb_text: str) -> dict[str, list[Via]]:
    """Parse vias from PCB file text, grouped by net name.

    Parses ``(via ...)`` S-expressions and returns ``Via`` objects keyed
    by net name, mirroring the structure of ``parse_segments``.

    Args:
        pcb_text: Raw text content of a ``.kicad_pcb`` file.

    Returns:
        Dictionary mapping net names to lists of ``Via`` objects.
    """
    vias_by_net: dict[str, list[Via]] = {}

    # Reuse existing net-name mapping (plus its reverse for the name-based
    # ``(net "NAME")`` dialect -- issue #4413).
    net_names = parse_net_names(pcb_text)
    name_to_id = {name: nid for nid, name in net_names.items()}

    # Issue #3414 / #3155: use the same balanced-parentheses walker as
    # ``parse_segments`` so that fields may appear in any order and extra
    # fields are silently ignored.  The previous implementation used a
    # single ordered regex that required ``(net N)`` to immediately follow
    # ``(layers ...)``.  KiCad 8+ files -- including every PCB written by
    # ``kct route`` itself -- emit ``(uuid "...")`` between ``(layers)``
    # and ``(net)``, so the ordered pattern matched ZERO vias on modern
    # output.  Consequences before this fix:
    #
    #   * ``--preserve-existing`` (#3155) preserved segments but silently
    #     DROPPED every via: preserved multi-layer routes were re-emitted
    #     fragmented (inner/back-layer copper with no layer transitions)
    #     and the routing grid did not block the via barrels, so new
    #     routes could be laid straight through existing vias.
    #   * The "existing routes as obstacles" pre-pass in
    #     ``load_pcb_for_routing(load_existing_routes=True)`` had the
    #     same blind spot.
    #
    # Issue #3118's ``micro``/``blind``/``buried`` token handling is
    # preserved by matching the optional type token at the head of the
    # block body.
    _re_via_type = VIA_TYPE_RE
    _re_at = re.compile(r"\(at\s+([\d.eE+-]+)\s+([\d.eE+-]+)\)")
    _re_size = re.compile(r"\(size\s+([\d.eE+-]+)\)")
    _re_drill = re.compile(r"\(drill\s+([\d.eE+-]+)\)")
    _re_layers = re.compile(r'\(layers\s+"([^"]+)"\s+"([^"]+)"\)')

    vias_read_as_through = 0
    for _start, _end, block in _extract_balanced_blocks(pcb_text, "via"):
        m_at = _re_at.search(block)
        m_size = _re_size.search(block)
        m_drill = _re_drill.search(block)
        m_layers = _re_layers.search(block)
        resolved_net = _resolve_block_net(block, net_names, name_to_id)

        # All five core fields are required; skip malformed blocks.  The
        # net resolver handles both the numeric ``(net N)`` and name-based
        # ``(net "NAME")`` dialects, so a name-only board no longer drops
        # every via here (#4413).
        if not (m_at and m_size and m_drill and m_layers and resolved_net):
            continue

        m_type = _re_via_type.match(block)
        via_type_token = m_type.group(1) if m_type else None
        x = float(m_at.group(1))
        y = float(m_at.group(2))
        diameter = float(m_size.group(1))
        drill = float(m_drill.group(1))
        # Issue #6493: a keyword-less partial pair is a through via to KiCad.
        (layer_start_name, layer_end_name), normalised = kicad_read_via_layers(
            (m_layers.group(1), m_layers.group(2)), via_type_token
        )
        if normalised:
            vias_read_as_through += 1
        net, net_name = resolved_net

        # Convert layer names to Layer enum
        try:
            layer_start = Layer.from_kicad_name(layer_start_name)
        except ValueError:
            layer_start = Layer.F_CU
        try:
            layer_end = Layer.from_kicad_name(layer_end_name)
        except ValueError:
            layer_end = Layer.B_CU

        # Issue #3118: tag the primitive with ``is_micro=True`` when
        # the ``micro`` token was matched so the dimensions DRC
        # exemption fires when this PCB is re-parsed after a route or
        # stitch pass.
        is_micro = via_type_token == "micro"

        via = Via(
            x=x,
            y=y,
            drill=drill,
            diameter=diameter,
            layers=(layer_start, layer_end),
            net=net,
            net_name=net_name,
            is_micro=is_micro,
            # Issue #6662: a KiCad blind/buried via is drilled only through
            # its span; keep the keyword so the model and re-writes agree.
            span_type=via_type_token if via_type_token in ("blind", "buried") else None,
        )

        if net_name not in vias_by_net:
            vias_by_net[net_name] = []
        vias_by_net[net_name].append(via)

    warn_normalised_vias(logger, "parse_vias", vias_read_as_through)
    return vias_by_net


def replace_segments(
    pcb_text: str,
    original: dict[str, list[Segment]],
    optimized: dict[str, list[Segment]],
    *,
    scratch: bool = False,
) -> str:
    """Replace original segments with optimized ones in PCB text.

    Issue #8219: ``scratch=True`` marks text built only for an intermediate
    DRC trial (never written out); the off-angle 45-degree check is skipped
    so each segment is warned about once, on the final serialization.

    Uses a balanced-parentheses walker to identify complete ``(segment ...)``
    blocks, then checks each block's net.  This correctly handles segments
    that contain nested sub-expressions such as ``(uuid "...")``,
    ``(locked yes)``, or any other field that the fragile ``[^)]*``
    character class would trip over.

    Blocks are matched by resolved net *name*, through the same
    :func:`_resolve_block_net` that :func:`parse_segments` keyed ``original``
    with, so both net-reference dialects (issue #4413) are handled.  Matching
    by numeric id silently broke KiCad-10 name-only boards, which carry no
    ``(net N "name")`` header table: every segment resolved to id 0, no
    original segment was removed, and the optimized segments were appended
    as ``(net 0)`` no-net copper on top of the originals.
    """
    net_names = parse_net_names(pcb_text)
    name_to_id = {name: nid for nid, name in net_names.items()}
    name_only = bool(_RE_NET_NAME.search(pcb_text))

    # Collect net names whose segments should be replaced.
    net_names_to_remove = {
        net_name for net_name, segs in original.items() if net_name in optimized and segs
    }

    # Build the set of (start, end) byte-spans to remove by walking
    # balanced segment blocks and checking the net field inside each one.
    spans_to_remove: list[tuple[int, int]] = []
    for blk_start, blk_end, block_text in _extract_balanced_blocks(pcb_text, "segment"):
        resolved = _resolve_block_net(block_text, net_names, name_to_id)
        if resolved and resolved[1] in net_names_to_remove:
            # Also consume any trailing whitespace so blank lines don't
            # accumulate after removal.
            trail = blk_end
            while trail < len(pcb_text) and pcb_text[trail] in (" ", "\t", "\n", "\r"):
                trail += 1
            spans_to_remove.append((blk_start, trail))

    # Rebuild the text, skipping removed spans.
    if spans_to_remove:
        parts: list[str] = []
        prev = 0
        for rm_start, rm_end in spans_to_remove:
            parts.append(pcb_text[prev:rm_start])
            prev = rm_end
        parts.append(pcb_text[prev:])
        result = "".join(parts)
    else:
        result = pcb_text

    # Add optimized segments before the closing parenthesis
    new_segments_sexp: list[str] = []

    def _serialize_optimized() -> None:
        for net_name, segs in optimized.items():
            for seg in segs:
                if name_only and not seg.net_name:
                    seg = replace(seg, net_name=net_name)
                new_segments_sexp.append(seg.to_sexp(name_only=name_only))

    if scratch:
        with segment_45_enforcement_disabled():
            _serialize_optimized()
    else:
        _serialize_optimized()

    if new_segments_sexp:
        # Find the last ) and insert before it
        insert_pos = result.rfind(")")
        if insert_pos > 0:
            indent = "  "
            new_content = "\n" + indent + f"\n{indent}".join(new_segments_sexp) + "\n"
            result = result[:insert_pos] + new_content + result[insert_pos:]

    return result


def _run_drc_error_count(
    pcb_text: str,
    manufacturer: str,
    layers: int,
    copper_oz: float,
) -> int:
    """Run DRC on PCB text and return the error count.

    Writes text to a temporary file, loads it as a PCB object,
    runs clearance and dimension checks, and returns the error count.

    Args:
        pcb_text: Full PCB file text content.
        manufacturer: Manufacturer ID for design rules.
        layers: Number of copper layers.
        copper_oz: Copper weight in oz.

    Returns:
        Number of DRC errors found.
    """
    counts = _run_drc_error_count_by_category(
        pcb_text,
        manufacturer=manufacturer,
        layers=layers,
        copper_oz=copper_oz,
    )
    return counts["__total__"]


# Issue #3138: DRC rule categories that indicate routes crossing pads or
# pad-clearance violations.  When per-net optimization grows the count
# of any of these categories, the rollback gate must fire even if the
# total error count stayed flat (or decreased while pad-violations
# swapped in for other categories).  Without this, the optimizer can
# trade a ``clearance_segment_segment`` violation for a
# ``clearance_pad_segment`` one and silently leave a trace literally
# crossing a pad.
_PAD_VIOLATION_CATEGORIES = frozenset(
    {
        "clearance_pad_segment",
        "clearance_pad_via",
        # Issue #8052: short-class categories.  A trace overlapping foreign-net
        # zone fill, or an island bridging net 0, is a hard short that
        # kicad-cli reports as ``shorting_items``.  (``clearance_segment_zone``
        # is excluded, Issue #8250: it is measured against the STORED fills,
        # which go stale as soon as a trace moves; see ``_STALE_FILL_RULES``.)
        "clearance_via_zone",
        "clearance_pad_zone",
        "clearance_net0_bridge",
    }
)


# Issue #8250: rules measured against the board's stored zone fills.  Those
# fills are not recomputed when a trace moves, so a valid edit can "violate"
# them until zones are refilled; they must not drive a rollback.
_STALE_FILL_RULES = frozenset({"clearance_segment_zone"})


def _run_drc_error_count_by_category(
    pcb_text: str,
    manufacturer: str,
    layers: int,
    copper_oz: float,
    min_clearance_mm: float | None = None,
) -> dict[str, int]:
    """Run DRC and return a dict of error counts keyed by rule id.

    The returned dict also contains a ``__total__`` key with the
    aggregate error count so callers that only care about the total
    can avoid summing the per-category values.

    Issue #3138: introduced so the per-net rollback gate can detect
    category swaps that leave the total flat -- e.g., when the
    optimizer trades a ``clearance_segment_segment`` violation for a
    ``clearance_pad_segment`` one (a route crossing a pad), the total
    is unchanged but the pad-violation category went up.

    Issue #8367: ``min_clearance_mm`` is the clearance the board's own DRC
    enforces (``.kicad_pro`` / ``.kicad_dru`` / netclasses).  The scratch
    copy this function checks has no project files beside it, so without
    it the referee holds the board to the ``manufacturer`` preset, which is
    stricter than kicad-cli on a board authored to a tighter rule (+251
    clearance errors where kicad-cli counted +2).
    """
    from dataclasses import replace

    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate import DRCChecker

    with tempfile.NamedTemporaryFile(mode="w", suffix=".kicad_pcb", delete=False) as tmp:
        tmp.write(pcb_text)
        tmp_path = tmp.name

    try:
        pcb = PCB.load(tmp_path)
        checker = DRCChecker(
            pcb,
            manufacturer=manufacturer,
            layers=layers,
            copper_oz=copper_oz,
        )
        if min_clearance_mm is not None and min_clearance_mm > 0:
            checker.design_rules = replace(checker.design_rules, min_clearance_mm=min_clearance_mm)
        # Check clearances and dimensions (the DRC categories relevant to
        # trace optimization -- silkscreen and edge clearance are unaffected
        # by segment reshaping)
        results = checker.check_clearances()
        results.merge(checker.check_dimensions())
        # Issue #8052: also gate on foreign-net zone fill overlap.  These
        # are no-ops on boards without filled zones.
        results.merge(checker.check_segment_zone_clearances())
        results.merge(checker.check_via_zone_clearances())

        counts: dict[str, int] = {"__total__": 0}
        for v in results.errors:
            if v.rule_id in _STALE_FILL_RULES:
                continue
            counts[v.rule_id] = counts.get(v.rule_id, 0) + 1
            counts["__total__"] += 1
        return counts
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _pad_violations_grew(
    before: dict[str, int],
    after: dict[str, int],
) -> bool:
    """Return True if any pad-violation category grew between ``before`` and ``after``.

    Issue #3138 Approach B: the per-net rollback gate must fire on
    category swaps that introduce pad-clearance violations even when the
    aggregate error count is flat or improves.  Pad-overlap is a
    show-stopper (the route literally crosses a pad), so any growth in
    ``clearance_pad_segment`` or ``clearance_pad_via`` triggers
    rollback regardless of what other categories did.
    """
    for category in _PAD_VIOLATION_CATEGORIES:
        if after.get(category, 0) > before.get(category, 0):
            return True
    return False


def _segment_multiset(segs: list[Segment]) -> Counter:
    """Order- and orientation-insensitive multiset of ``segs`` (Issue #8299).

    Duplicates are counted (not collapsed), so a rewrite that only re-chains,
    reverses or reorders segments compares equal to its input, while one that
    drops or adds a segment -- even an exact duplicate -- does not.
    """
    keys: Counter = Counter()
    for seg in segs:
        p1 = (round(seg.x1, 6), round(seg.y1, 6))
        p2 = (round(seg.x2, 6), round(seg.y2, 6))
        a, b = (p1, p2) if p1 <= p2 else (p2, p1)
        keys[(seg.net, seg.layer, round(seg.width, 6), a, b)] += 1
    return keys


def _same_segments(a: list[Segment], b: list[Segment]) -> bool:
    """True when ``a`` and ``b`` hold the same segments in any order/orientation."""
    return a == b or (len(a) == len(b) and _segment_multiset(a) == _segment_multiset(b))


def dedupe_segments(segs: list[Segment]) -> tuple[list[Segment], int]:
    """Drop exact-duplicate segments, keeping the first occurrence (Issue #8271).

    Mirrors the key of ``kct pcb dedupe`` (``schema.pcb._segment_dedup_key``):
    net, layer, width and the *unordered* endpoint pair rounded to ~1 um.
    Returns ``(kept, removed_count)``.
    """
    seen: set[tuple] = set()
    kept: list[Segment] = []
    for seg in segs:
        p1 = (round(seg.x1, 3), round(seg.y1, 3))
        p2 = (round(seg.x2, 3), round(seg.y2, 3))
        a, b = (p1, p2) if p1 <= p2 else (p2, p1)
        key = (seg.net, seg.layer, round(seg.width, 3), a, b)
        if key in seen:
            continue
        seen.add(key)
        kept.append(seg)
    return kept, len(segs) - len(kept)


def _copper_order(layer: object) -> int:
    """Stack position of a copper layer: F.Cu first, In<n>.Cu by n, B.Cu last."""
    name = getattr(layer, "kicad_name", None) or str(layer)
    if name.startswith("F."):
        return 0
    if name.startswith("B."):
        return 10_000
    digits = "".join(ch for ch in name if ch.isdigit())
    return int(digits) if digits else 5_000


def _via_layers(via: Via, copper_layers: set) -> list:
    """Every copper layer in ``copper_layers`` that ``via`` electrically joins.

    ``Via.layers`` is only the (start, end) pair, so a through via
    ``(layers "F.Cu" "B.Cu")`` must be expanded to the inner layers it
    crosses (blind/buried vias to their span).  Issue #8250 review.
    """
    ends = [_copper_order(lyr) for lyr in via.layers]
    lo, hi = min(ends), max(ends)
    span = [lyr for lyr in copper_layers if lo <= _copper_order(lyr) <= hi]
    return span or list(via.layers)


def _anchor_extent(anchor: tuple) -> tuple[float, float, float, float]:
    """``(x, y, half_w, half_h)`` of an anchor; centre-only anchors have 0 extent.

    Anchors are ``(x, y, layers)`` or ``(x, y, layers, half_w, half_h)`` where
    the half-extents are the pad copper's board-space AABB (Issue #8419).
    """
    if len(anchor) >= 5:
        return anchor[0], anchor[1], anchor[3], anchor[4]
    return anchor[0], anchor[1], 0.0, 0.0


def _end_on_pad_copper(anchor: tuple, px: float, py: float, tolerance: float) -> bool:
    """True if point ``(px, py)`` lies on the anchor's pad copper (AABB + tolerance).

    A track that terminates at the pad *edge* (fine-pitch pads) is attached
    even though the pad centre is more than half a track width away.
    """
    ax, ay, hw, hh = _anchor_extent(anchor)
    return abs(px - ax) <= hw + tolerance and abs(py - ay) <= hh + tolerance


def _component_labels(
    segments: list[Segment],
    vias: list[Via],
    anchors: list[tuple[float, float, tuple[Layer, ...]]],
    tolerance: float,
    n_components: list[int] | None = None,
) -> list[int]:
    """Label each anchor with the id of the copper component containing it.

    Copper nodes are segments and vias; two touch when a segment endpoint lies
    on the other item (within ``tolerance``), a via joins every segment on its
    layers that reaches its position.  An anchor (pad) attaches to any segment
    on one of its layers passing within half the width of it, or to a via at
    it.  Anchors that attach to nothing get unique labels.  If
    ``n_components`` is given, the number of distinct copper components
    (segments and vias, pad or no pad) is appended to it.
    """
    import math

    n_seg = len(segments)
    parent = list(range(n_seg + len(vias)))
    copper_layers: set = {sg.layer for sg in segments}
    for _anchor in anchors:
        copper_layers.update(_anchor[2])
    for _v in vias:
        copper_layers.update(_v.layers)

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        parent[find(a)] = find(b)

    def dist_to_seg(px: float, py: float, s: Segment) -> float:
        dx, dy = s.x2 - s.x1, s.y2 - s.y1
        ll = dx * dx + dy * dy
        t = 0.0 if ll == 0 else max(0.0, min(1.0, ((px - s.x1) * dx + (py - s.y1) * dy) / ll))
        return math.hypot(px - (s.x1 + t * dx), py - (s.y1 + t * dy))

    # Spatial hash (1 mm cells) so big pour nets are not O(n^2).
    cell = 1.0
    buckets: dict[tuple[object, int, int], list[int]] = {}
    for i, sg in enumerate(segments):
        pad = tolerance + sg.width / 2
        for cx in range(
            int((min(sg.x1, sg.x2) - pad) // cell), int((max(sg.x1, sg.x2) + pad) // cell) + 1
        ):
            for cy in range(
                int((min(sg.y1, sg.y2) - pad) // cell), int((max(sg.y1, sg.y2) + pad) // cell) + 1
            ):
                buckets.setdefault((sg.layer, cx, cy), []).append(i)

    def candidates(layer: object, x: float, y: float) -> list[int]:
        return buckets.get((layer, int(x // cell), int(y // cell)), [])

    # segment endpoint on another segment (same layer)
    for i, a in enumerate(segments):
        for px, py in ((a.x1, a.y1), (a.x2, a.y2)):
            for j in candidates(a.layer, px, py):
                if j != i and dist_to_seg(px, py, segments[j]) <= tolerance:
                    union(i, j)
    for k, v in enumerate(vias):
        for lyr in _via_layers(v, copper_layers):
            for i in candidates(lyr, v.x, v.y):
                if dist_to_seg(v.x, v.y, segments[i]) <= tolerance:
                    union(n_seg + k, i)

    if n_components is not None:
        n_components.append(len({find(i) for i in range(len(parent))}))

    labels: list[int] = []
    for idx, anchor in enumerate(anchors):
        ax, ay, hw, hh = _anchor_extent(anchor)
        layers = anchor[2]
        hit: int | None = None
        # Cells covering the pad copper, so an edge-entering track found by
        # its end (not the pad centre) is a candidate (Issue #8419).
        cells = [
            (cx, cy)
            for cx in range(
                int((ax - hw - tolerance) // cell), int((ax + hw + tolerance) // cell) + 1
            )
            for cy in range(
                int((ay - hh - tolerance) // cell), int((ay + hh + tolerance) // cell) + 1
            )
        ]
        for lyr in layers or {sg.layer for sg in segments}:
            for cx, cy in cells:
                for i in buckets.get((lyr, cx, cy), []):
                    sg = segments[i]
                    if dist_to_seg(ax, ay, sg) <= max(tolerance, sg.width / 2) or (
                        (hw or hh)
                        and any(
                            _end_on_pad_copper(anchor, ex, ey, tolerance)
                            for ex, ey in ((sg.x1, sg.y1), (sg.x2, sg.y2))
                        )
                    ):
                        hit = i
                        break
                if hit is not None:
                    break
            if hit is not None:
                break
        if hit is None:
            for k, v in enumerate(vias):
                if math.hypot(v.x - ax, v.y - ay) <= max(tolerance, v.diameter / 2):
                    hit = n_seg + k
                    break
        labels.append(find(hit) if hit is not None else -1 - idx)
    return labels


def _free_end_count(
    segments: list[Segment],
    vias: list[Via],
    anchors: list[tuple[float, float, tuple[Layer, ...]]],
    tolerance: float,
) -> int:
    """Count segment ends that touch no other copper, via or pad.

    These are the ends KiCad flags as ``track_dangling``.  An exact duplicate
    (or reverse duplicate) of the segment does not count as something the end
    touches.  Issue #8250 review: a rewrite must not create new free ends.
    """
    import math

    cell = 1.0
    buckets: dict[tuple[object, int, int], list[int]] = {}
    for i, sg in enumerate(segments):
        pad = tolerance + sg.width / 2
        for cx in range(
            int((min(sg.x1, sg.x2) - pad) // cell), int((max(sg.x1, sg.x2) + pad) // cell) + 1
        ):
            for cy in range(
                int((min(sg.y1, sg.y2) - pad) // cell), int((max(sg.y1, sg.y2) + pad) // cell) + 1
            ):
                buckets.setdefault((sg.layer, cx, cy), []).append(i)

    def dist(px: float, py: float, s: Segment) -> float:
        dx, dy = s.x2 - s.x1, s.y2 - s.y1
        ll = dx * dx + dy * dy
        t = 0.0 if ll == 0 else max(0.0, min(1.0, ((px - s.x1) * dx + (py - s.y1) * dy) / ll))
        return math.hypot(px - (s.x1 + t * dx), py - (s.y1 + t * dy))

    def same(a: Segment, b: Segment) -> bool:
        bb = (b.x1, b.y1, b.x2, b.y2)
        return any(
            all(abs(u - v) <= tolerance for u, v in zip(pts, bb, strict=True))
            for pts in ((a.x1, a.y1, a.x2, a.y2), (a.x2, a.y2, a.x1, a.y1))
        )

    free = 0
    for i, sg in enumerate(segments):
        for px, py in ((sg.x1, sg.y1), (sg.x2, sg.y2)):
            # Same attach radius as ``_component_labels``: a pad is touched by
            # an end within ``max(tolerance, w/2)`` of its centre, i.e. one that
            # still lies on the pad copper (Issue #8325).
            half_w = sg.width / 2
            touched = any(
                (
                    (abs(a[0] - px) <= tolerance and abs(a[1] - py) <= tolerance)
                    or math.hypot(a[0] - px, a[1] - py) <= half_w
                    or _end_on_pad_copper(a, px, py, tolerance)
                )
                and (not a[2] or sg.layer in a[2])
                for a in anchors
            ) or any(
                abs(v.x - px) <= tolerance
                and abs(v.y - py) <= tolerance
                and sg.layer in _via_layers(v, {sg.layer, *v.layers})
                for v in vias
            )
            if not touched:
                for j in buckets.get((sg.layer, int(px // cell), int(py // cell)), []):
                    if (
                        j != i
                        and not same(sg, segments[j])
                        and dist(px, py, segments[j]) <= tolerance
                    ):
                        touched = True
                        break
            if not touched:
                free += 1
    return free


def connectivity_preserved(
    before: list[Segment],
    after: list[Segment],
    vias: list[Via],
    anchors: list[tuple[float, float, tuple[Layer, ...]]],
    tolerance: float,
) -> bool:
    """True if every pair of anchors joined by copper before is still joined after.

    Also rejects a rewrite that increases the number of copper components
    (a split that strands pad-less copper).  Issue #8250.  Pad-to-pad only: pad-to-zone connections are not modelled
    (a pad reached only through a pour is not required to stay joined by
    trace copper).
    """
    pre_n: list[int] = []
    post_n: list[int] = []
    pre = _component_labels(before, vias, anchors, tolerance, pre_n)
    post = _component_labels(after, vias, anchors, tolerance, post_n)
    # A rewrite that strands a fragment holding no pad (trace-to-trace
    # split) leaves every anchor label intact; the component count catches it.
    if post_n[0] > pre_n[0]:
        return False
    # ...and one that leaves a track end touching nothing (KiCad
    # ``track_dangling``) where the input had none.
    if _free_end_count(after, vias, anchors, tolerance) > _free_end_count(
        before, vias, anchors, tolerance
    ):
        return False
    first_post: dict[int, int] = {}
    for a, b in zip(pre, post, strict=True):
        if a < 0:
            continue
        if first_post.setdefault(a, b) != b:
            return False
    return True


def _seg_point_dist(px: float, py: float, s: Segment) -> float:
    dx, dy = s.x2 - s.x1, s.y2 - s.y1
    ll = dx * dx + dy * dy
    if ll == 0.0:
        return math.hypot(px - s.x1, py - s.y1)
    t = max(0.0, min(1.0, ((px - s.x1) * dx + (py - s.y1) * dy) / ll))
    return math.hypot(px - (s.x1 + t * dx), py - (s.y1 + t * dy))


def _changed_regions(
    before: list[Segment], after: list[Segment], tolerance: float
) -> list[tuple[list[int], list[Segment]]]:
    """Split a net rewrite into independent changed regions (Issue #8298).

    Returns ``(removed_indices_into_before, added_segments)`` per region.
    Changed items (segments only in ``before`` or only in ``after``) are
    grouped when they touch on the same layer; regions are ordered by their
    first removed segment's position in ``before`` (file order).
    """
    remaining = list(after)
    removed: list[int] = []
    for i, s in enumerate(before):
        if s in remaining:
            remaining.remove(s)
        else:
            removed.append(i)
    # ``remaining`` now holds the added segments (multiset difference).
    items: list[tuple[str, int, Segment]] = [("r", i, before[i]) for i in removed] + [
        ("a", j, s) for j, s in enumerate(remaining)
    ]
    parent = list(range(len(items)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def touches(a: Segment, b: Segment) -> bool:
        if a.layer != b.layer:
            return False
        return any(_seg_point_dist(px, py, b) <= tolerance for px, py in (a.start, a.end)) or any(
            _seg_point_dist(px, py, a) <= tolerance for px, py in (b.start, b.end)
        )

    for x in range(len(items)):
        for y in range(x + 1, len(items)):
            if touches(items[x][2], items[y][2]):
                parent[find(x)] = find(y)
    groups: dict[int, tuple[list[int], list[Segment]]] = {}
    for k, (kind, idx, seg) in enumerate(items):
        rem, add = groups.setdefault(find(k), ([], []))
        if kind == "r":
            rem.append(idx)
        else:
            add.append(seg)
    regions = list(groups.values())
    regions.sort(key=lambda r: (min(r[0]) if r[0] else len(before), len(r[1])))
    return regions


def keep_safe_regions(
    before: list[Segment],
    after: list[Segment],
    vias: list[Via],
    anchors: list[tuple[float, float, tuple[Layer, ...]]],
    tolerance: float,
) -> tuple[list[Segment], int, int]:
    """Greedily keep each independent changed region that is safe on its own.

    Returns ``(segments, kept_regions, total_regions)``.  Each region is
    applied on top of the already-kept result and must pass
    :func:`connectivity_preserved` against the *original* ``before``.
    """
    regions = _changed_regions(before, after, tolerance)
    current = list(before)
    kept = 0
    for rem_idx, added in regions:
        trial = list(current)
        for seg in (before[i] for i in rem_idx):
            if seg in trial:
                trial.remove(seg)
        trial.extend(added)
        if connectivity_preserved(before, trial, vias, anchors, tolerance):
            current = trial
            kept += 1
    return current, kept, len(regions)


def _arbitrate_changed_nets(
    pcb_text: str,
    segments_by_net: dict[str, list[Segment]],
    optimized_segments: dict[str, list[Segment]],
    changed_nets: list[str],
    arbiter: Callable[[str], int | None],
) -> set[str]:
    """Pick the largest clean subset of changed nets using an external checker.

    ``arbiter(board_text)`` returns the checker's clearance error count, or
    ``None`` when it cannot run.  A net set is clean when its count is at or
    below the input board's.  The set is bisected (O(k log n) runs), keeping
    each clean half cumulatively, so one bad net costs ~log n extra runs
    rather than n (Issue #8367).  Returns ``set()`` when the arbiter is
    unavailable or nothing is clean.
    """
    base = arbiter(pcb_text)
    if base is None:
        return set()
    kept: set[str] = set()

    def count(nets: list[str]) -> int | None:
        trial = {
            n: (optimized_segments[n] if (n in kept or n in nets) else segments_by_net[n])
            for n in optimized_segments
        }
        return arbiter(replace_segments(pcb_text, segments_by_net, trial, scratch=True))

    def solve(nets: list[str]) -> bool:
        c = count(nets)
        if c is None:
            return False
        if c <= base:
            kept.update(nets)
            return True
        if len(nets) == 1:
            return True
        mid = len(nets) // 2
        return solve(nets[:mid]) and solve(nets[mid:])

    if not solve(list(changed_nets)):
        return set()
    return kept


def optimize_pcb(
    pcb_path: str,
    output_path: str | None,
    optimize_fn: Callable[[list[Segment]], list[Segment]],
    config: OptimizationConfig,
    net_filter: str | None = None,
    net_pattern: str | None = None,
    dry_run: bool = False,
    on_net_optimized: Callable[[str, list[Segment], list[Segment]], None] | None = None,
    net_anchors_by_name: dict[str, list[tuple[float, float, tuple[Layer, ...]]]] | None = None,
) -> OptimizationStats:
    """Optimize traces in a PCB file.

    ``net_anchors_by_name`` (Issue #8250) maps a net name to the pad positions
    ``(x, y, layers)`` of that net.  When given, a net's rewrite that splits
    pads which were joined by trace copper in the input is discarded and
    counted in ``stats.nets_connectivity_discarded``.

    When ``config.drc_aware`` is True, the optimizer runs DRC before and
    after optimization. Any net whose optimized segments increase the
    total DRC error count is rolled back to its original segments.  The
    gate runs the clearance, dimension and segment/via-vs-zone-fill checks;
    growth in a pad- or short-class category also triggers rollback.  If
    the rollback loop cannot reach baseline, nets are re-admitted one at a time
    and only those that keep the board at baseline are kept (Issue #8367); if
    none qualifies, all changed nets are reverted.

    Args:
        pcb_path: Path to input .kicad_pcb file.
        output_path: Path for output file. If None, modifies in place.
        optimize_fn: Function to optimize a list of segments.
        config: Optimization configuration.
        net_filter: Only optimize the net with exactly this name (case-sensitive).
        net_pattern: Only optimize nets whose name matches this ``fnmatch`` glob
            (case-sensitive).  Mutually exclusive with ``net_filter`` (Issue #8299).
        dry_run: If True, calculate stats but don't write output.
        on_net_optimized: Optional per-net commit hook, called as
            ``(net_name, original_segments, kept_segments)`` once a net's
            result is final for the loop -- i.e. *after* the Kelvin gate has
            accepted or vetoed its branch rewrites -- so a caller can keep
            external state (e.g. a collision grid) following what is kept
            rather than what ``optimize_fn`` returned (Issue #8214).  The
            ``--drc-aware`` rollback below runs after the loop and does not
            call it; the rollback's DRC re-check backstops that.

    Returns:
        Statistics about the optimization.
    """
    pcb_text = Path(pcb_path).read_text()
    stats = OptimizationStats()

    # Parse existing segments
    segments_by_net = parse_segments(pcb_text)

    # Filter nets if requested
    if net_filter and net_pattern:
        raise ValueError("net_filter and net_pattern are mutually exclusive")
    if net_filter:
        segments_by_net = {net: segs for net, segs in segments_by_net.items() if net == net_filter}
    elif net_pattern:
        segments_by_net = {
            net: segs
            for net, segs in segments_by_net.items()
            if fnmatch.fnmatchcase(net, net_pattern)
        }

    # Calculate before stats
    for net, segs in segments_by_net.items():
        stats.segments_before += len(segs)
        stats.corners_before += count_corners(segs, config.tolerance)
        stats.length_before += total_length(segs)

    # DRC baseline (when drc_aware is enabled)
    baseline_counts: dict[str, int] = {"__total__": 0}
    if config.drc_aware and config.drc_manufacturer:
        baseline_counts = _run_drc_error_count_by_category(
            pcb_text,
            manufacturer=config.drc_manufacturer,
            layers=config.drc_layers,
            copper_oz=config.drc_copper_oz,
            min_clearance_mm=config.drc_clearance_mm,
        )
        stats.drc_errors_before = baseline_counts["__total__"]

    # Issue #6889: the rewrites below ignore same-net copper, so on a Kelvin
    # net they can join two branches of the star away from the shunt pad --
    # the #5398 defect, reached through ``kct optimize-traces``.  The board
    # gate optimizes a Kelvin net branch by branch and keeps any rewrite
    # that would touch a sibling.  ``None`` (no Kelvin net) leaves every net
    # on the path below, unchanged.
    from .kelvin_gate import KelvinBoardGate

    kelvin = KelvinBoardGate.from_pcb_text(pcb_text)

    vias_by_net = parse_vias(pcb_text) if net_anchors_by_name else {}

    # Optimize each net
    optimized_segments: dict[str, list[Segment]] = {}
    for net, segs in segments_by_net.items():
        optimized = kelvin.optimize_net(net, segs, optimize_fn) if kelvin else None
        if optimized is None:
            optimized = optimize_fn(segs)
        # Issue #8299: ``optimize_segments`` hands the chains back in
        # ``sort_into_chains`` order/orientation even when no pass fired.
        # A rewrite that is the same multiset of segments is *no* change:
        # keep the input verbatim so the net skips the connectivity guard and
        # dedupe below and is not reported as touched.
        if optimized != segs and _same_segments(optimized, segs):
            optimized = segs
        if (
            net_anchors_by_name
            and optimized != segs
            and not connectivity_preserved(
                segs,
                optimized,
                vias_by_net.get(net, []),
                net_anchors_by_name.get(net, []),
                config.tolerance,
            )
        ):
            # Issue #8298: one unsafe edit must not discard the safe ones.
            salvaged, n_kept, _n_regions = keep_safe_regions(
                segs,
                optimized,
                vias_by_net.get(net, []),
                net_anchors_by_name.get(net, []),
                config.tolerance,
            )
            if n_kept and salvaged != segs:
                optimized = salvaged
                stats.nets_connectivity_partial += 1
                stats.connectivity_partial_nets.append(net)
            else:
                optimized = list(segs)
                stats.nets_connectivity_discarded += 1
                stats.connectivity_discarded_nets.append(net)
        # Issue #8271: never emit duplicates the rewrite introduced.  Nets the
        # optimizer left unchanged (or whose rewrite was discarded above) are
        # written verbatim.
        if optimized != segs:
            optimized, n_dupes = dedupe_segments(optimized)
            stats.duplicates_removed += n_dupes
        optimized_segments[net] = optimized
        if on_net_optimized is not None:
            on_net_optimized(net, segs, optimized)
        stats.nets_optimized += 1
    if kelvin is not None:
        stats.kelvin_vetoes = kelvin.gate.vetoes
        kelvin.gate.report()

    rolled_back_nets: set[str] = set()
    net_violations: dict[str, dict[str, int]] = {}

    # DRC-aware per-net rollback
    if config.drc_aware and config.drc_manufacturer:
        # Build fully-optimized text and check DRC
        full_optimized_text = replace_segments(
            pcb_text, segments_by_net, optimized_segments, scratch=True
        )
        full_counts = _run_drc_error_count_by_category(
            full_optimized_text,
            manufacturer=config.drc_manufacturer,
            layers=config.drc_layers,
            copper_oz=config.drc_copper_oz,
            min_clearance_mm=config.drc_clearance_mm,
        )
        full_errors = full_counts["__total__"]
        baseline_errors = baseline_counts["__total__"]

        # Issue #3138: gate is now category-aware.  The old gate
        # ``if full_errors > baseline_errors`` silently allowed
        # optimization to *swap* category 5 ``clearance_segment_segment``
        # violations for category 5 ``clearance_pad_segment`` ones at a
        # constant total -- leaving traces literally crossing pads.
        # The new gate fires the rollback loop whenever any
        # pad-violation category grows, *or* the aggregate total grows.
        needs_rollback = full_errors > baseline_errors or _pad_violations_grew(
            baseline_counts, full_counts
        )

        if needs_rollback:
            # Some nets made things worse -- try rolling back one net at a time.
            # For each net, test keeping its original segments while all
            # other nets remain optimized.  If reverting a net reduces errors
            # back to (or below) baseline, mark it as rolled back.
            final_segments: dict[str, list[Segment]] = dict(optimized_segments)
            current_counts = dict(full_counts)

            for net in list(optimized_segments.keys()):
                # Skip nets whose optimization did not change the segments
                if optimized_segments[net] == segments_by_net[net]:
                    continue

                # Try reverting this single net
                trial = dict(final_segments)
                trial[net] = segments_by_net[net]
                trial_text = replace_segments(pcb_text, segments_by_net, trial, scratch=True)
                trial_counts = _run_drc_error_count_by_category(
                    trial_text,
                    manufacturer=config.drc_manufacturer,
                    layers=config.drc_layers,
                    copper_oz=config.drc_copper_oz,
                    min_clearance_mm=config.drc_clearance_mm,
                )

                # Issue #3138: a rollback is helpful if it lowers the total
                # error count *or* shrinks a pad-violation category (even
                # when the total is flat).  The previous gate
                # ``trial_errors < full_errors`` missed category swaps.
                trial_total = trial_counts["__total__"]
                pad_shrunk = any(
                    trial_counts.get(c, 0) < current_counts.get(c, 0)
                    for c in _PAD_VIOLATION_CATEGORIES
                )
                if trial_total < full_errors or pad_shrunk:
                    # Reverting this net helped -- keep original segments
                    final_segments[net] = segments_by_net[net]
                    stats.nets_rolled_back += 1
                    rolled_back_nets.add(net)
                    # Issue #8207: what disappeared by reverting this net is
                    # what its rewrite introduced.
                    net_violations[net] = {
                        c: n - trial_counts.get(c, 0)
                        for c, n in current_counts.items()
                        if c != "__total__" and n > trial_counts.get(c, 0)
                    }
                    full_errors = trial_total
                    current_counts = trial_counts

                    # If we are back at or below baseline AND no new
                    # pad-violations remain, stop rolling back.
                    if full_errors <= baseline_errors and not _pad_violations_grew(
                        baseline_counts, current_counts
                    ):
                        break

            # Issue #8052 fail safe, refined by Issue #8367.  If the greedy
            # loop could not get back to baseline (or a protected category is
            # still above baseline), do NOT discard every net: rebuild from
            # the input, admitting one changed net at a time and keeping it
            # only if the cumulative board stays at/below baseline with no
            # pad-class growth.  Nets whose own edits add errors are rejected
            # individually; the all-revert fail-safe fires only when no
            # changed net survives.
            if full_errors > baseline_errors or _pad_violations_grew(
                baseline_counts, current_counts
            ):
                above_rules = {
                    c: n - baseline_counts.get(c, 0)
                    for c, n in current_counts.items()
                    if c != "__total__" and n > baseline_counts.get(c, 0)
                }
                changed_nets = [
                    n for n in optimized_segments if optimized_segments[n] != segments_by_net[n]
                ]
                prev_rolled = set(rolled_back_nets)
                final_segments = {n: segments_by_net[n] for n in optimized_segments}
                kept_counts = dict(baseline_counts)
                for net in changed_nets:
                    trial = dict(final_segments)
                    trial[net] = optimized_segments[net]
                    trial_counts = _run_drc_error_count_by_category(
                        replace_segments(pcb_text, segments_by_net, trial, scratch=True),
                        manufacturer=config.drc_manufacturer,
                        layers=config.drc_layers,
                        copper_oz=config.drc_copper_oz,
                        min_clearance_mm=config.drc_clearance_mm,
                    )
                    if trial_counts["__total__"] <= baseline_errors and not _pad_violations_grew(
                        baseline_counts, trial_counts
                    ):
                        final_segments[net] = optimized_segments[net]
                        kept_counts = trial_counts
                        rolled_back_nets.discard(net)
                        net_violations.pop(net, None)
                    else:
                        rolled_back_nets.add(net)
                        net_violations[net] = {
                            c: n - baseline_counts.get(c, 0)
                            for c, n in trial_counts.items()
                            if c != "__total__" and n > baseline_counts.get(c, 0)
                        }
                # Recount nets_rolled_back from scratch (greedy phase counts
                # are superseded by the rebuild).
                stats.nets_rolled_back += len(rolled_back_nets - prev_rolled) - len(
                    prev_rolled - rolled_back_nets
                )
                full_errors = kept_counts["__total__"]
                if (
                    config.drc_arbiter is not None
                    and changed_nets
                    and not any(final_segments[n] != segments_by_net[n] for n in final_segments)
                ):
                    # Issue #8367: the in-process referee rejected every
                    # changed net, which is the signature of a rule-model gap
                    # (e.g. ``.kicad_dru`` pad exemptions it cannot see).
                    # Arbitrate with the external checker instead.
                    arb_kept = _arbitrate_changed_nets(
                        pcb_text,
                        segments_by_net,
                        optimized_segments,
                        changed_nets,
                        config.drc_arbiter,
                    )
                    if arb_kept:
                        before_arb = set(rolled_back_nets)
                        for n in changed_nets:
                            if n in arb_kept:
                                final_segments[n] = optimized_segments[n]
                                rolled_back_nets.discard(n)
                                net_violations.pop(n, None)
                        stats.nets_rolled_back -= len(before_arb - rolled_back_nets)
                        # Report the in-process count of what is actually kept.
                        full_errors = _run_drc_error_count_by_category(
                            replace_segments(
                                pcb_text, segments_by_net, final_segments, scratch=True
                            ),
                            manufacturer=config.drc_manufacturer,
                            layers=config.drc_layers,
                            copper_oz=config.drc_copper_oz,
                            min_clearance_mm=config.drc_clearance_mm,
                        )["__total__"]
                if not any(final_segments[n] != segments_by_net[n] for n in final_segments):
                    stats.drc_failsafe_reverted = True
                    stats.drc_failsafe_rules = above_rules
                    # Output is the untouched input text (see below), so the
                    # count *is* the baseline.
                    full_errors = baseline_errors

            optimized_segments = final_segments

        stats.drc_errors_after = full_errors

    # Recalculate after stats (may have changed due to rollbacks)
    stats.segments_after = 0
    stats.corners_after = 0
    stats.length_after = 0.0
    for net, segs in optimized_segments.items():
        stats.segments_after += len(segs)
        stats.corners_after += count_corners(segs, config.tolerance)
        stats.length_after += total_length(segs)

    # Issue #7065: per-net rows, built from the final (post-rollback) dict.
    for net, before_segs in segments_by_net.items():
        after_segs = optimized_segments.get(net, before_segs)
        stats.per_net.append(
            NetOptimizationStats(
                net=net,
                segments_before=len(before_segs),
                segments_after=len(after_segs),
                length_before=total_length(before_segs),
                length_after=total_length(after_segs),
                rolled_back=net in rolled_back_nets,
                new_violations=net_violations.get(net, {}),
                changed=not _same_segments(after_segs, before_segs),
            )
        )
        if net not in rolled_back_nets and after_segs != before_segs:
            stats.nets_kept += 1

    # Generate output (only if not dry run)
    if not dry_run:
        if all(optimized_segments.get(n) == segs for n, segs in segments_by_net.items()):
            # Nothing changed (e.g. the failsafe reverted everything): write
            # the input verbatim instead of re-serialising every segment.
            output_text = pcb_text
        else:
            output_text = replace_segments(pcb_text, segments_by_net, optimized_segments)
        out_path = output_path or pcb_path
        Path(out_path).write_text(output_text)

    return stats
