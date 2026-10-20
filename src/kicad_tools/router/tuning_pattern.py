"""KiCad tuning-pattern (``generated``) objects for skew-compensation bumps.

Issue #7521 (operator ruling Q16 on #6858), step (2): skew-compensation
bumps are written as KiCad 10 tuning-pattern generator objects instead of
anonymous track segments, so KiCad's own DRC can tell a deliberate bump from
an accidental uncoupled run.

File format
===========

The format was learned black-box, from saved boards only (never KiCad
source):

* public third-party boards that contain saved tuning patterns
  (``mawoka-myblock/td-free`` ``KiCad/td-free.kicad_pcb`` holds two
  ``diff_pair_skew`` patterns; ``shanteacontrols/OpenDeck``
  ``sch/dubfocus/16t/USB/USB.kicad_pcb`` and ``ElectronicCats/flipper-addons``
  ``FLIPPER_MOD_BUS/FLIPPER_MOD_BUS.kicad_pcb`` hold ``single`` ones); and
* what pinned ``kicad-cli`` 10.0.6 writes back when it re-saves such a board
  (``kicad-cli pcb upgrade``), which fixes the KiCad 10 field set and order
  emitted here (``is_time_domain``, ``last_tuning_length`` and the
  ``target_delay*`` triple are KiCad 10 additions).

A pattern is a top-level ``(generated ...)`` block whose ``members`` list the
uuids of the track objects it owns.  Every member is a track of the tuned
net.  Members are plain ``(segment ...)`` blocks elsewhere in the file;
pinned kicad-cli drops a member uuid that names no track when it loads the
board, so a stale reference degrades to a smaller pattern, never a load
failure.

What pinned kicad-cli 10.0.6 does with one (measured, Issue #7521)
=================================================================

* Member tracks are left out of the diff-pair *uncoupled length* measure on
  both sides: they are not uncoupled copper of their net, and the other net
  does not couple to them.  They are also out of the *gap out of range*
  check.
* So the partner leg's copper opposite the bump, which is now beside
  nothing, counts as uncoupled: about the bump's base-line length.  Under
  min-only gap rules (all kct writes) that can exceed the plain bump's own
  figure: a 45-degree bump with a 1.8 mm base reads 1.13 mm as plain copper
  and 1.8 mm as a pattern.  ``kct check`` measures the same way (operator
  ruling Q16).  The skew tuner judges each bump's pattern form against the
  uncoupled limit and rejects one that fails; it never commits the bump as
  plain copper instead, because kct under-reads KiCad on plain
  perpendicular-leg trombones.  Only the segments that
  leave the host line are members: making the coupled lead-in and lead-out
  runs members too would uncouple the partner opposite them as well.

Emission
========

:func:`tag_skew_bump` attaches one :class:`TuningPattern` to the segments of
a committed bump (``Segment.tuning_pattern``).  The tag rides along through
``dataclasses.replace`` copies; :meth:`Route.to_sexp
<kicad_tools.router.primitives.Route.to_sexp>` gives each tagged segment a
uuid and writes one ``(generated ...)`` block per pattern naming exactly the
segments it emitted.  A segment rebuilt by a later pass loses the tag and is
simply written as ordinary copper.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.sexp import SExp

    from .primitives import Route, Segment

#: ``tuning_mode`` of a skew pattern (lengthens one leg of a diff pair).
MODE_DIFF_PAIR_SKEW = "diff_pair_skew"

#: ``last_status`` values.  ``tuned`` and ``too_long`` occur in saved
#: third-party boards; ``too_short`` round-trips through pinned kicad-cli.
STATUS_TUNED = "tuned"
STATUS_TOO_SHORT = "too_short"
STATUS_TOO_LONG = "too_long"

#: Value KiCad writes for an unset length/delay target (an "infinite" max).
_UNSET_TARGET = 1000000

#: A segment endpoint farther than this from the host line is off the line.
_ON_LINE_EPS_MM = 1e-4


def _num(value: float) -> str:
    """Format a millimetre value the way the router writes coordinates."""
    rounded = round(float(value), 4)
    if rounded == int(rounded):
        return str(int(rounded))
    return repr(rounded)


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass(frozen=True)
class TuningPattern:
    """One KiCad tuning-pattern object (everything but uuid and members).

    Coordinates are in mm.  ``base_line`` is the stretch of the host line the
    bump replaces, ``base_line_coupled`` the matching stretch of the partner
    leg (``None`` omits it, as KiCad does for single-net patterns).
    """

    layer: str
    net_name: str
    base_line: tuple[tuple[float, float], tuple[float, float]]
    base_line_coupled: tuple[tuple[float, float], tuple[float, float]] | None
    track_width: float
    diff_pair_gap: float
    min_amplitude: float
    max_amplitude: float
    min_spacing: float
    initial_side: str = "right"
    single_sided: bool = True
    rounded: bool = False
    corner_radius_percent: int = 0
    tuning_mode: str = MODE_DIFF_PAIR_SKEW
    target_skew: float = 0.0
    target_skew_min: float = 0.0
    target_skew_max: float = 0.0
    last_status: str = STATUS_TUNED
    last_tuning_length: float = 0.0

    def to_sexp(self, members: Sequence[str], uuid: str) -> str:
        """Return the ``(generated ...)`` block, indented for a board body.

        The field order is the one pinned kicad-cli 10.0.6 writes, so a
        KiCad open/save round trip does not churn the block.
        """
        (ox, oy), (ex, ey) = self.base_line

        def line(points: tuple[tuple[float, float], tuple[float, float]]) -> str:
            pts = " ".join(f"(xy {_num(x)} {_num(y)})" for x, y in points)
            return f"\t\t\t(pts\n\t\t\t\t{pts}\n\t\t\t)\n"

        yes_no = {True: "yes", False: "no"}
        out = [
            "(generated\n",
            f"\t\t(uuid {_quote(uuid)})\n",
            "\t\t(type tuning_pattern)\n",
            '\t\t(name "Tuning Pattern")\n',
            f"\t\t(layer {_quote(self.layer)})\n",
            "\t\t(base_line\n",
            line(self.base_line),
            "\t\t)\n",
        ]
        if self.base_line_coupled is not None:
            out += ["\t\t(base_line_coupled\n", line(self.base_line_coupled), "\t\t)\n"]
        out += [
            f"\t\t(corner_radius_percent {int(self.corner_radius_percent)})\n",
            f"\t\t(end\n\t\t\t(xy {_num(ex)} {_num(ey)})\n\t\t)\n",
            f"\t\t(initial_side {_quote(self.initial_side)})\n",
            "\t\t(is_time_domain no)\n",
            f"\t\t(last_diff_pair_gap {_num(self.diff_pair_gap)})\n",
            f"\t\t(last_netname {_quote(self.net_name)})\n",
            f"\t\t(last_status {_quote(self.last_status)})\n",
            f"\t\t(last_track_width {_num(self.track_width)})\n",
            f"\t\t(last_tuning_length {_num(self.last_tuning_length)})\n",
            f"\t\t(max_amplitude {_num(self.max_amplitude)})\n",
            f"\t\t(min_amplitude {_num(self.min_amplitude)})\n",
            f"\t\t(min_spacing {_num(self.min_spacing)})\n",
            f"\t\t(origin\n\t\t\t(xy {_num(ox)} {_num(oy)})\n\t\t)\n",
            "\t\t(override_custom_rules no)\n",
            f"\t\t(rounded {yes_no[self.rounded]})\n",
            f"\t\t(single_sided {yes_no[self.single_sided]})\n",
            f"\t\t(target_delay {_UNSET_TARGET})\n",
            f"\t\t(target_delay_max {_UNSET_TARGET})\n",
            "\t\t(target_delay_min 0)\n",
            f"\t\t(target_length {_UNSET_TARGET})\n",
            f"\t\t(target_length_max {_UNSET_TARGET})\n",
            "\t\t(target_length_min 0)\n",
            f"\t\t(target_skew {_num(self.target_skew)})\n",
            f"\t\t(target_skew_max {_num(self.target_skew_max)})\n",
            f"\t\t(target_skew_min {_num(self.target_skew_min)})\n",
            f"\t\t(tuning_mode {_quote(self.tuning_mode)})\n",
            _members_sexp(members),
            "\t)",
        ]
        return "".join(out)


def _members_sexp(members: Sequence[str]) -> str:
    """``(members ...)`` sorted and wrapped two per line, as KiCad writes it."""
    if not members:
        return "\t\t(members)\n"
    quoted = [_quote(m) for m in sorted(members)]
    if len(quoted) <= 2:
        return f"\t\t(members {' '.join(quoted)})\n"
    rows = [" ".join(quoted[i : i + 2]) for i in range(0, len(quoted), 2)]
    return "\t\t(members " + "\n\t\t\t".join(rows) + "\n\t\t)\n"


# ---------------------------------------------------------------------------
# Building a pattern for a committed skew bump
# ---------------------------------------------------------------------------


def _distance_to_line(px: float, py: float, ax: float, ay: float, ux: float, uy: float) -> float:
    return abs((px - ax) * uy - (py - ay) * ux)


def bump_members(segments: Sequence[Segment]) -> list[Segment]:
    """The segments of an inserted serpentine that leave its host line.

    ``segments`` replace one straight host segment, so the host line runs
    from the first segment's start to the last segment's end.  Lead-in,
    lead-out and between-loop runs lying on that line stay coupled to the
    partner and are not bump copper.
    """
    if not segments:
        return []
    ax, ay = segments[0].x1, segments[0].y1
    bx, by = segments[-1].x2, segments[-1].y2
    length = math.hypot(bx - ax, by - ay)
    if length <= _ON_LINE_EPS_MM:
        return list(segments)
    ux, uy = (bx - ax) / length, (by - ay) / length
    return [
        s
        for s in segments
        if _distance_to_line(s.x1, s.y1, ax, ay, ux, uy) > _ON_LINE_EPS_MM
        or _distance_to_line(s.x2, s.y2, ax, ay, ux, uy) > _ON_LINE_EPS_MM
    ]


def _closest_point(px: float, py: float, segments: Iterable[Segment]) -> tuple[float, float] | None:
    best: tuple[float, float] | None = None
    best_d2 = math.inf
    for s in segments:
        dx, dy = s.x2 - s.x1, s.y2 - s.y1
        den = dx * dx + dy * dy
        t = 0.0 if den == 0.0 else max(0.0, min(1.0, ((px - s.x1) * dx + (py - s.y1) * dy) / den))
        cx, cy = s.x1 + t * dx, s.y1 + t * dy
        d2 = (cx - px) ** 2 + (cy - py) ** 2
        if d2 < best_d2:
            best, best_d2 = (cx, cy), d2
    return best


def skew_bump_pattern(
    segments: Sequence[Segment],
    partner: Route | None,
    *,
    diff_pair_gap: float,
    min_spacing: float,
    tolerance_mm: float,
    last_status: str = STATUS_TUNED,
) -> tuple[TuningPattern, list[Segment]] | None:
    """Describe the bump inside an inserted serpentine as a skew pattern.

    Args:
        segments: The serpentine's segments, in order, replacing one host
            segment of the tuned leg.
        partner: The other leg of the pair (for ``base_line_coupled``).
        diff_pair_gap: The pair's edge-to-edge gap (mm).
        min_spacing: Spacing between the bump's loops (mm).
        tolerance_mm: The skew window; written as ``target_skew_min/max``.
        last_status: :data:`STATUS_TUNED`, :data:`STATUS_TOO_SHORT` or
            :data:`STATUS_TOO_LONG`.

    Returns:
        ``(pattern, members)``, or ``None`` when no segment leaves the host
        line (nothing to describe).
    """
    members = bump_members(segments)
    if not members:
        return None
    ax, ay = segments[0].x1, segments[0].y1
    bx, by = segments[-1].x2, segments[-1].y2
    length = math.hypot(bx - ax, by - ay)
    ux, uy = ((bx - ax) / length, (by - ay) / length) if length > 0 else (1.0, 0.0)

    # Base line: the host-line stretch from where the bump leaves the line to
    # where it rejoins it (projections of the member extremes).
    ts = [((x - ax) * ux + (y - ay) * uy) for s in members for x, y in ((s.x1, s.y1), (s.x2, s.y2))]
    t0, t1 = min(ts), max(ts)
    start = (ax + t0 * ux, ay + t0 * uy)
    end = (ax + t1 * ux, ay + t1 * uy)

    # Side and amplitude, relative to the base line (board y grows down, so a
    # positive cross product puts the bump on the right of travel).
    offsets = [
        ux * (y - ay) - uy * (x - ax) for s in members for x, y in ((s.x1, s.y1), (s.x2, s.y2))
    ]
    far = max(offsets, key=abs)
    amplitude = abs(far)
    side = "right" if far > 0 else "left"

    coupled = None
    if partner is not None:
        partner_segments = [s for s in partner.segments if s.layer == segments[0].layer]
        cs = _closest_point(start[0], start[1], partner_segments)
        ce = _closest_point(end[0], end[1], partner_segments)
        if cs is not None and ce is not None:
            coupled = (cs, ce)

    first = segments[0]
    pattern = TuningPattern(
        layer=first.layer.kicad_name,
        net_name=first.net_name,
        base_line=(start, end),
        base_line_coupled=coupled,
        track_width=first.width,
        diff_pair_gap=diff_pair_gap,
        min_amplitude=amplitude,
        max_amplitude=amplitude,
        min_spacing=min_spacing,
        initial_side=side,
        target_skew=0.0,
        target_skew_min=-abs(tolerance_mm),
        target_skew_max=abs(tolerance_mm),
        last_status=last_status,
    )
    return pattern, members


def tag_skew_bump(members: Iterable[Segment], pattern: TuningPattern) -> None:
    """Mark ``members`` as owned by ``pattern`` (see module docstring)."""
    for seg in members:
        seg.tuning_pattern = pattern


def retag(members: Iterable[Segment], pattern: TuningPattern, **changes: object) -> TuningPattern:
    """Re-tag ``members`` with a copy of ``pattern`` carrying ``changes``."""
    updated = replace(pattern, **changes)  # type: ignore[arg-type]
    tag_skew_bump(members, updated)
    return updated


# ---------------------------------------------------------------------------
# Reading patterns back
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TuningPatternRecord:
    """A tuning pattern as read from a board file."""

    uuid: str
    tuning_mode: str
    net_name: str
    layer: str
    members: tuple[str, ...]


_GENERATED_RE = re.compile(r"\(generated\b")


def _record(node: SExp) -> TuningPatternRecord | None:
    """The record of one ``(generated ...)`` node, ``None`` if not a tuning pattern."""

    def atom(tag: str) -> str:
        child = node.find_child(tag)
        value = child.get_first_atom() if child is not None else None
        return "" if value is None else str(value)

    if atom("type") != "tuning_pattern":
        return None
    members_node = node.find_child("members")
    members = tuple(str(v) for v in members_node.get_atoms()) if members_node else ()
    return TuningPatternRecord(
        uuid=atom("uuid"),
        tuning_mode=atom("tuning_mode"),
        net_name=atom("last_netname"),
        layer=atom("layer"),
        members=members,
    )


def board_tuning_patterns(root: SExp) -> list[TuningPatternRecord]:
    """Every tuning pattern among the top-level items of a parsed board."""
    records = (_record(c) for c in root.children if c.name == "generated")
    return [r for r in records if r is not None]


def tuning_pattern_member_uuids(root: SExp) -> frozenset[str]:
    """Uuids of every track owned by a tuning pattern of a parsed board.

    Issue #7521 (operator ruling Q16): pinned kicad-cli 10.0.6 leaves these
    tracks out of the diff-pair uncoupled-length measure entirely, as the
    measured net *and* as the partner beside which the other net couples.
    """
    return frozenset(m for r in board_tuning_patterns(root) for m in r.members)


def read_tuning_patterns(text: str) -> list[TuningPatternRecord]:
    """Return every ``tuning_pattern`` generator object in ``text``."""
    from kicad_tools.sexp import parse_string

    records: list[TuningPatternRecord] = []
    for match in _GENERATED_RE.finditer(text):
        depth, i, in_str = 0, match.start(), False
        while i < len(text):
            c = text[i]
            if in_str:
                if c == "\\":
                    i += 1
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        record = _record(parse_string(text[match.start() : i + 1]))
        if record is not None:
            records.append(record)
    return records


__all__ = [
    "MODE_DIFF_PAIR_SKEW",
    "STATUS_TOO_LONG",
    "STATUS_TOO_SHORT",
    "STATUS_TUNED",
    "TuningPattern",
    "TuningPatternRecord",
    "board_tuning_patterns",
    "bump_members",
    "read_tuning_patterns",
    "retag",
    "skew_bump_pattern",
    "tag_skew_bump",
    "tuning_pattern_member_uuids",
]
