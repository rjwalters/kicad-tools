"""45-degree quantization helpers for segment-emitting/mutating passes.

Issue #3532: 192+ arbitrary-angle segments (0.8-22.5 degrees off the
0/45/90/135 set) shipped on softstart's committed routed artifact, and
every fleet board carries the same class.  A* on-grid output is
naturally 45-quantized; the off-angle copper comes from passes that
drag endpoints to raw coordinates:

* the pad-tail emitters -- the C++ ``reconstruct_path`` (split in
  float64 by ``cpp_backend._convert_result_to_route``) and the Python
  ``Router._convert_path_to_route`` connect the last grid cell to the
  EXACT (off-grid) pad centre with a single straight segment;
* optimizer mutation passes -- ``convert_corners_45``'s terminal
  endpoint restoration and ``pull_tight_pass`` skew chain neighbours;
* file-level repair passes -- the softstart recipe's step-10c/10d
  drill-to-drill and cross-net clearance repairs regex-drag every
  segment endpoint that referenced a moved via/segment endpoint.

The fix shape shared by all emitters: when a displacement vector is not
on the 8-direction set, emit a two-leg DOGLEG (one exact 45-degree leg
covering ``min(|dx|, |dy|)`` plus one axis-aligned leg) instead of a
single skewed segment.  Both legs are constructed so the equality
``|dx| == |dy|`` (diagonal leg) or ``delta == 0`` (axis leg) holds
EXACTLY in IEEE float / decimal arithmetic -- see :func:`dogleg_points`.

45-only routing is a manufacturability/quality convention: acute-angle
copper junctions can etch poorly (acid traps).
"""

from __future__ import annotations

import math
import re
import uuid as _uuid
from decimal import Decimal
from pathlib import Path

__all__ = [
    "ANGLE_TOL_DEG",
    "off_angle_degrees",
    "is_45_aligned",
    "snap_direction_8",
    "dogleg_points",
    "segment_angle_census",
    "quantize_pcb_file",
]

#: Tolerance (degrees off the nearest multiple of 45) below which a
#: segment counts as 45-aligned.  Float round-trips through KiCad
#: S-expression text land well under this.
ANGLE_TOL_DEG = 0.01

_DIAG = math.sqrt(0.5)

#: The 8 legal routing directions (unit vectors).
EIGHT_DIRECTIONS: tuple[tuple[float, float], ...] = (
    (1.0, 0.0),
    (_DIAG, _DIAG),
    (0.0, 1.0),
    (-_DIAG, _DIAG),
    (-1.0, 0.0),
    (-_DIAG, -_DIAG),
    (0.0, -1.0),
    (_DIAG, -_DIAG),
)


def off_angle_degrees(dx: float, dy: float) -> float:
    """Degrees off the nearest multiple of 45 for displacement (dx, dy).

    Returns 0.0 for a zero-length displacement (nothing to quantize).
    """
    if dx == 0 and dy == 0:
        return 0.0
    ang = math.degrees(math.atan2(dy, dx)) % 45.0
    return min(ang, 45.0 - ang)


def is_45_aligned(dx: float, dy: float, tol_deg: float = ANGLE_TOL_DEG) -> bool:
    """True if displacement (dx, dy) lies on the 0/45/90/135 angle set.

    A zero-length displacement counts as aligned.
    """
    return off_angle_degrees(dx, dy) <= tol_deg


def snap_direction_8(dx: float, dy: float) -> tuple[float, float]:
    """Snap displacement (dx, dy) to the nearest of the 8 routing directions.

    Returns a unit vector from :data:`EIGHT_DIRECTIONS`.  A zero-length
    input returns ``(1.0, 0.0)`` (callers should guard degenerate input
    when the direction matters).
    """
    if dx == 0 and dy == 0:
        return EIGHT_DIRECTIONS[0]
    ang = math.atan2(dy, dx)
    idx = round(ang / (math.pi / 4.0)) % 8
    return EIGHT_DIRECTIONS[idx]


def dogleg_points(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    axis_first: bool = False,
    tol_deg: float = ANGLE_TOL_DEG,
) -> list[tuple[float, float]]:
    """Return the polyline vertices connecting (x1, y1) to (x2, y2) with
    only 45-aligned legs.

    For an already-aligned displacement this is ``[(x1, y1), (x2, y2)]``.
    Otherwise a single intermediate vertex is inserted producing one
    exact diagonal leg plus one exact axis-aligned leg:

    * ``axis_first=False`` (default): diagonal leg leaves the START
      point, axis leg lands on the end point.
    * ``axis_first=True``: axis leg leaves the start point, diagonal leg
      lands on the END point.  Useful when the default variant's bulge
      side collides with nearby copper.

    Exactness: the intermediate vertex shares one coordinate with an
    endpoint, so the axis leg is EXACTLY axis-aligned.  The diagonal
    leg's ``|dx| == |dy|`` holds up to one rounding step of
    ``x1 + copysign(|dy|, dx)`` (float64 ulp at board scale is
    ~3e-14 mm: the residual angle is below 1e-9 degrees, far inside
    :data:`ANGLE_TOL_DEG`).  The file-level pass
    (:func:`quantize_pcb_file`) uses :class:`~decimal.Decimal`
    arithmetic instead, which is exact in the serialized text.

    The maximum perpendicular deviation of the dogleg from the straight
    chord is bounded by ``min(|dx|, |dy|)`` -- callers that mutate
    committed copper must re-verify clearances afterwards.
    """
    dx = x2 - x1
    dy = y2 - y1
    if is_45_aligned(dx, dy, tol_deg):
        return [(x1, y1), (x2, y2)]

    adx = abs(dx)
    ady = abs(dy)
    if not axis_first:
        # Diagonal leg first (from the start point), axis leg second.
        if adx >= ady:
            mid = (x1 + math.copysign(ady, dx), y2)
        else:
            mid = (x2, y1 + math.copysign(adx, dy))
    else:
        # Axis leg first, diagonal leg lands on the end point.
        if adx >= ady:
            mid = (x2 - math.copysign(ady, dx), y1)
        else:
            mid = (x1, y2 - math.copysign(adx, dy))
    return [(x1, y1), mid, (x2, y2)]


# ---------------------------------------------------------------------------
# File-level quantization pass (for repair recipes / committed artifacts)
# ---------------------------------------------------------------------------

#: Matches a complete KiCad ``(segment ...)`` block in both the tab- and
#: 2-space-indented serializations, with ``(uuid ...)`` either before or
#: after ``(net ...)`` (hand-patched artifacts use the latter order).
#: Group 1 is the leading indentation.
_SEGMENT_BLOCK_RE = re.compile(
    r"^([ \t]+)\(segment\s*\n"
    r"\s*\(start ([-\d.]+) ([-\d.]+)\)\s*\n"
    r"\s*\(end ([-\d.]+) ([-\d.]+)\)\s*\n"
    r"\s*\(width ([-\d.]+)\)\s*\n"
    r"\s*\(layer \"([^\"]+)\"\)\s*\n"
    r"(?:\s*\(uuid \"([^\"]+)\"\)\s*\n)?"
    r"\s*\(net (\d+)\)\s*\n"
    r"(?:\s*\(uuid \"([^\"]+)\"\)\s*\n)?"
    r"[ \t]*\)",
    re.MULTILINE,
)


def _fmt_decimal(value: Decimal) -> str:
    """Format a Decimal coordinate the way KiCad S-expressions store it."""
    s = format(value.normalize(), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s and s != "-0" else "0"


#: Matches any ``(uuid "...")`` attribute -- used to collect every uuid
#: already present in a PCB file so derived dogleg uuids never collide.
_UUID_ATTR_RE = re.compile(r'\(uuid "([^"]+)"\)')


def _derive_dogleg_uuid(parent_uuid: str, seen_uuids: set[str]) -> str:
    """Deterministic, collision-free uuid for a dogleg second leg.

    The base derivation is ``uuid5(NAMESPACE_OID, parent + ":dogleg")``.
    If that uuid is already present in the file (e.g. the parent was
    quantized by a PREVIOUS pipeline run and its old sibling leg
    survives -- the repair->quantize fixpoint loop makes this a
    recurring pattern), the suffix is extended (``:dogleg:2``,
    ``:dogleg:3``, ...) until the candidate is unused.  The probe order
    is fixed, so the result depends only on the parent uuid and the
    file's existing uuid population: identical input files yield
    byte-identical output.

    The chosen uuid is added to *seen_uuids* so later derivations in
    the same pass cannot collide with it either.
    """
    candidate = str(_uuid.uuid5(_uuid.NAMESPACE_OID, parent_uuid + ":dogleg"))
    n = 1
    while candidate in seen_uuids:
        n += 1
        candidate = str(
            _uuid.uuid5(_uuid.NAMESPACE_OID, f"{parent_uuid}:dogleg:{n}")
        )
    seen_uuids.add(candidate)
    return candidate


def _decimal_dogleg_mid(
    x1: Decimal, y1: Decimal, x2: Decimal, y2: Decimal, axis_first: bool
) -> tuple[Decimal, Decimal]:
    """Exact-decimal intermediate vertex (mirrors :func:`dogleg_points`)."""
    dx = x2 - x1
    dy = y2 - y1
    adx = abs(dx)
    ady = abs(dy)

    def _signed(mag: Decimal, like: Decimal) -> Decimal:
        return mag if like >= 0 else -mag

    if not axis_first:
        if adx >= ady:
            return (x1 + _signed(ady, dx), y2)
        return (x2, y1 + _signed(adx, dy))
    if adx >= ady:
        return (x2 - _signed(ady, dx), y1)
    return (x1, y2 - _signed(adx, dy))


def segment_angle_census(
    pcb_path: Path | str, tol_deg: float = ANGLE_TOL_DEG
) -> tuple[int, list[dict]]:
    """Angle census over every ``(segment ...)`` in a PCB file.

    Returns ``(total_segments, off_angle)`` where *off_angle* is a list
    of dicts (``start``, ``end``, ``layer``, ``net``, ``uuid``,
    ``off_deg``) for each segment off the 0/45/90/135 set by more than
    *tol_deg*.  Zero-length segments are never reported.
    """
    text = Path(pcb_path).read_text()
    total = 0
    bad: list[dict] = []
    for m in _SEGMENT_BLOCK_RE.finditer(text):
        total += 1
        x1, y1, x2, y2 = (float(m.group(i)) for i in (2, 3, 4, 5))
        off = off_angle_degrees(x2 - x1, y2 - y1)
        if off > tol_deg:
            bad.append(
                {
                    "start": (x1, y1),
                    "end": (x2, y2),
                    "layer": m.group(7),
                    "net": int(m.group(9)),
                    "uuid": m.group(8) or m.group(10),
                    "off_deg": off,
                }
            )
    return total, bad


def quantize_pcb_file(
    pcb_path: Path | str,
    *,
    tol_deg: float = ANGLE_TOL_DEG,
    axis_first_uuids: frozenset[str] | set[str] = frozenset(),
    skip_uuids: frozenset[str] | set[str] = frozenset(),
    dry_run: bool = False,
) -> list[str]:
    """Replace every off-angle ``(segment ...)`` in *pcb_path* with an
    exact two-leg dogleg (45-degree leg + axis-aligned leg).

    The dogleg shares the original segment's endpoints, width, layer and
    net, so connectivity is bit-for-bit preserved; only the path between
    the endpoints changes (bounded perpendicular bulge of
    ``min(|dx|, |dy|)``).  The first leg keeps the original segment's
    uuid; the second leg gets a deterministic uuid5 derived from the
    original uuid (so repeated runs are byte-identical).  The derived
    uuid is checked against every uuid already in the file -- when a
    previously-quantized parent is re-quantized while its old sibling
    leg survives (the repair->quantize fixpoint pattern), the
    derivation suffix is extended deterministically instead of
    duplicating the sibling's uuid (see :func:`_derive_dogleg_uuid`).

    Coordinates are manipulated as :class:`~decimal.Decimal` so the
    emitted legs are EXACTLY 45-aligned in the serialized text.

    Args:
        pcb_path: PCB file to rewrite in place.
        tol_deg: Angle tolerance for the off-angle test.
        axis_first_uuids: Segment uuids that should use the axis-first
            dogleg variant (bulge on the opposite side of the chord) --
            the escape hatch when the default variant's bulge introduces
            a clearance violation.
        skip_uuids: Segment uuids to leave UNTOUCHED (when neither
            dogleg variant clears the surrounding copper -- e.g. a
            skewed chord threading a corridor both of whose sides are
            occupied).  Skipped segments remain off-angle; callers must
            document them (fleet census baseline) and fix by re-routing.
        dry_run: When True, report without writing.

    Returns:
        List of uuids (or ``"start-end"`` keys for uuid-less segments)
        of the segments that were (or would be) replaced.
    """
    path = Path(pcb_path)
    text = path.read_text()
    flips = set(axis_first_uuids)
    skips = set(skip_uuids)
    replaced: list[str] = []
    # Every uuid already in the file, plus uuids emitted during this
    # pass -- derived dogleg uuids must never collide with either.
    seen_uuids: set[str] = set(_UUID_ATTR_RE.findall(text))

    def _rewrite(m: re.Match[str]) -> str:
        indent = m.group(1)
        x1, y1 = Decimal(m.group(2)), Decimal(m.group(3))
        x2, y2 = Decimal(m.group(4)), Decimal(m.group(5))
        width, layer, net = m.group(6), m.group(7), m.group(9)
        seg_uuid = m.group(8) or m.group(10)
        dx = float(x2 - x1)
        dy = float(y2 - y1)
        if (dx == 0 and dy == 0) or is_45_aligned(dx, dy, tol_deg):
            return m.group(0)

        key = seg_uuid or f"{m.group(2)},{m.group(3)}-{m.group(4)},{m.group(5)}"
        if key in skips:
            return m.group(0)
        replaced.append(key)
        if dry_run:
            return m.group(0)

        mx, my = _decimal_dogleg_mid(x1, y1, x2, y2, axis_first=key in flips)

        # Preserve the file's indentation style (tabs vs spaces).
        inner = indent * 2 if indent in ("\t", "  ") else indent + "\t"

        def _block(
            sx: Decimal, sy: Decimal, ex: Decimal, ey: Decimal, block_uuid: str | None
        ) -> str:
            lines = [
                f"{indent}(segment",
                f"{inner}(start {_fmt_decimal(sx)} {_fmt_decimal(sy)})",
                f"{inner}(end {_fmt_decimal(ex)} {_fmt_decimal(ey)})",
                f"{inner}(width {width})",
                f'{inner}(layer "{layer}")',
            ]
            if block_uuid is not None:
                lines.append(f'{inner}(uuid "{block_uuid}")')
            lines.append(f"{inner}(net {net})")
            lines.append(f"{indent})")
            return "\n".join(lines)

        # Deterministic uuid for the second leg (uuid5 of the original)
        # so repeated runs of the pass produce byte-identical artifacts.
        # Collision-proof: if the base derivation already exists in the
        # file (stale sibling from a previous quantization of the same
        # parent), the suffix is extended deterministically.
        second_uuid = (
            _derive_dogleg_uuid(seg_uuid, seen_uuids)
            if seg_uuid is not None
            else None
        )
        return (
            _block(x1, y1, mx, my, seg_uuid)
            + "\n"
            + _block(mx, my, x2, y2, second_uuid)
        )

    new_text = _SEGMENT_BLOCK_RE.sub(_rewrite, text)
    if replaced and not dry_run:
        path.write_text(new_text)
    return replaced
