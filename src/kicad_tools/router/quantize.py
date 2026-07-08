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
import os
import re
import uuid as _uuid
import warnings
from decimal import Decimal
from pathlib import Path

__all__ = [
    "ANGLE_TOL_DEG",
    "SERIALIZE_QUANTUM_MM",
    "off_angle_degrees",
    "is_45_aligned",
    "snap_direction_8",
    "dogleg_points",
    "segment_angle_census",
    "quantize_pcb_file",
    "OffAngleSegmentError",
    "OffAngleSegmentWarning",
    "verify_segment_45",
    "segment_45_strict_enabled",
]

#: Tolerance (degrees off the nearest multiple of 45) below which a
#: segment counts as 45-aligned.  Float round-trips through KiCad
#: S-expression text land well under this.
ANGLE_TOL_DEG = 0.01

#: One unit of the 4-decimal (0.1 um) serialization grid that
#: ``Segment.to_sexp`` rounds coordinates onto.  A diagonal produced by
#: the A* grid can have its two legs differ by a single quantum after
#: rounding (e.g. dx=0.1385, dy=0.1384): geometrically a legal ~45-degree
#: diagonal, but 0.02 deg off the exact 45-degree line -- over
#: :data:`ANGLE_TOL_DEG`.  :func:`verify_segment_45` treats a displacement
#: within one quantum of an exact 45/0/90/135 leg as legal so this benign
#: rounding jitter is not mistaken for a genuinely skewed emit.
SERIALIZE_QUANTUM_MM = 1e-4

#: Environment variable that flips the by-construction guard from
#: graceful-degradation (WARN + let the legacy quantize/repair fallback
#: handle it) into strict mode (raise :class:`OffAngleSegmentError`).  CI
#: enables this per-board as emission paths migrate onto obstacle-aware
#: doglegs; boards whose emitters are not yet migrated keep the default
#: WARN so a fresh re-route completes and the leak stays visible/migratable.
SEGMENT_45_STRICT_ENV = "KICAD_TOOLS_SEGMENT_45_STRICT"

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
# By-construction emission choke point (issue #3907)
# ---------------------------------------------------------------------------
#
# #3532 established the 45-only policy with an emit-then-repair
# architecture: passes emit whatever geometry they compute, and a
# post-hoc ``quantize_pcb_file`` sweep (plus the fleet census ratchet)
# catches the leaks.  That architecture keeps leaking -- every new
# emitter must independently remember to dogleg, and the post-hoc repair
# has no obstacle model (PR #3906's first ``quantize_pcb_file`` pass
# doglegged 9 board-05 segments straight into a 3-way short).
#
# #3907 moves legality to a single choke point: the point where a router
# segment is serialized to KiCad ``(segment ...)`` text.  Every
# router-emitted segment flows through ``Segment.to_sexp`` ->
# ``Route.to_sexp`` -> ``Autorouter.to_sexp``, so a by-construction guard
# here makes an off-angle leak impossible to serialize silently: it
# raises instead of writing arbitrary-angle copper that only the census
# would catch (in CI, after every local gate passed).
#
# The guard verifies the SERIALIZED displacement, not the analytic one:
# ``Segment.to_sexp`` rounds coordinates to 4 decimals (0.1 um), and it
# is the rounded text the census reads, so this is the population the
# policy actually governs.


class OffAngleSegmentError(ValueError):
    """A segment whose serialized displacement is off the 45-degree set.

    Raised by :func:`verify_segment_45` (and therefore by
    :meth:`kicad_tools.router.primitives.Segment.to_sexp`) **in strict mode
    only** when a router pass tries to serialize copper that is not on the
    {0, 45, 90, 135} angle set.  This is the by-construction backstop for
    issue #3907: every emitter must hand the serializer 45-legal geometry
    (dogleg off-axis pad tails / mutations via :func:`dogleg_points` BEFORE
    building the ``Segment``), so reaching this error means an emitter
    leaked -- fix the emitter, do not repair the artifact afterwards.

    In the default graceful-degradation mode the same off-angle copper
    surfaces as an :class:`OffAngleSegmentWarning` instead, so an
    un-migrated emitter does not hard-crash a fresh route (see
    :func:`segment_45_strict_enabled`).
    """

    def __init__(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        off_deg: float,
        *,
        context: str = "",
    ) -> None:
        self.x1, self.y1, self.x2, self.y2 = x1, y1, x2, y2
        self.off_deg = off_deg
        self.context = context
        where = f" [{context}]" if context else ""
        super().__init__(
            f"off-angle segment{where}: ({x1:.4f}, {y1:.4f}) -> "
            f"({x2:.4f}, {y2:.4f}) is {off_deg:.4f} deg off the "
            f"0/45/90/135 set (tol {ANGLE_TOL_DEG} deg).  Emit a dogleg "
            f"(kicad_tools.router.quantize.dogleg_points) at construction "
            f"time instead of serializing skewed copper (issue #3907)."
        )


class OffAngleSegmentWarning(UserWarning):
    """A serialized off-angle segment reported in graceful-degradation mode.

    Issue #3907.  When strict mode is OFF (the default -- see
    :func:`segment_45_strict_enabled`), :func:`verify_segment_45` emits
    this warning instead of raising :class:`OffAngleSegmentError`, naming
    the emitting net/layer so the leak stays visible and migratable while
    the offending emission path is ported onto obstacle-aware doglegs.
    The segment still serializes, so the legacy file-level
    :func:`quantize_pcb_file` repair pass a recipe runs afterward can fix
    it -- the fallback the hard raise would otherwise preempt.
    """


def _rounded_4(value: float) -> float:
    """Round like ``Segment.to_sexp`` serializes coordinates (4 dp)."""
    return round(value, 4)


def segment_45_strict_enabled() -> bool:
    """True when the by-construction guard should raise, not just warn.

    Issue #3907.  Controlled by the :data:`SEGMENT_45_STRICT_ENV`
    environment variable (any of ``1``/``true``/``yes``/``on``,
    case-insensitive).  Default is False: the guard degrades gracefully
    (WARN + serialize) so an un-migrated off-angle emitter does not
    hard-crash a fresh route, and the recipe's legacy
    :func:`quantize_pcb_file` fallback still gets to repair the artifact.
    CI opts individual boards into strict mode as their emission paths
    migrate.
    """
    val = os.environ.get(SEGMENT_45_STRICT_ENV, "")
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _is_quantum_aligned(dx: float, dy: float) -> bool:
    """True if (dx, dy) is on the 45-degree set to within one 4dp quantum.

    A diagonal emitted by the A* grid rounds to 4 decimals independently
    per axis, so its two legs can differ by a single 0.1 um quantum
    (e.g. |dx|=0.1385, |dy|=0.1384).  That is a legal ~45-degree diagonal
    for all manufacturing purposes -- the 0.1 um asymmetry is below the
    fabrication grid -- yet it sits ~0.02 deg off the exact 45-degree
    line, over :data:`ANGLE_TOL_DEG`.  This predicate accepts a
    displacement whose serialized legs are within one quantum of an exact
    axis-aligned (one leg ~ 0) or exact diagonal (|dx| ~ |dy|) leg, so the
    guard measures against the 4dp grid the census reads rather than raw
    degrees.
    """
    adx = abs(dx)
    ady = abs(dy)
    q = SERIALIZE_QUANTUM_MM * 1.5  # half-open guard against fp round noise
    # Axis-aligned (0 / 90 deg): the short leg is within a quantum of zero.
    if adx <= q or ady <= q:
        return True
    # Diagonal (45 / 135 deg): the two legs differ by at most one quantum.
    if abs(adx - ady) <= q:
        return True
    return False


def verify_segment_45(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    tol_deg: float = ANGLE_TOL_DEG,
    context: str = "",
    strict: bool | None = None,
) -> None:
    """Check that segment ``(x1, y1) -> (x2, y2)`` is 45-legal AS WRITTEN.

    The check runs on the 4-decimal *serialized* coordinates (matching
    :meth:`kicad_tools.router.primitives.Segment.to_sexp`), because it is
    the rounded text the fleet census governs.  A zero-length segment
    (both endpoints round to the same point) is legal -- it carries no
    direction.  A displacement within one 0.1 um serialization quantum of
    an exact 45/0/90/135 leg is also legal (see :func:`_is_quantum_aligned`):
    that is benign per-axis rounding jitter on an A* diagonal, not a skewed
    emit.

    Failure mode (issue #3907, graceful degradation):

    * **strict mode OFF (default)** -- an off-angle displacement emits an
      :class:`OffAngleSegmentWarning` naming the emitter and RETURNS.  The
      segment still serializes, so a recipe's legacy
      :func:`quantize_pcb_file` repair pass can dogleg it afterward.  This
      keeps un-migrated emission paths from hard-crashing a fresh route
      while the leak stays visible and migratable.
    * **strict mode ON** (``strict=True`` or :data:`SEGMENT_45_STRICT_ENV`)
      -- raises :class:`OffAngleSegmentError`.  CI enables this per-board
      as emitters migrate onto obstacle-aware doglegs; the fleet census
      remains the ratchet.

    Args:
        strict: force strict/non-strict; ``None`` (default) consults
            :func:`segment_45_strict_enabled` (the env-driven CI switch).

    Raises:
        OffAngleSegmentError: only in strict mode, when the serialized
            displacement is off the {0, 45, 90, 135} set by more than
            *tol_deg* (beyond one-quantum jitter).
    """
    sx1, sy1 = _rounded_4(x1), _rounded_4(y1)
    sx2, sy2 = _rounded_4(x2), _rounded_4(y2)
    dx = sx2 - sx1
    dy = sy2 - sy1
    if dx == 0 and dy == 0:
        return
    if _is_quantum_aligned(dx, dy):
        return
    off = off_angle_degrees(dx, dy)
    if off <= tol_deg:
        return
    if strict is None:
        strict = segment_45_strict_enabled()
    if strict:
        raise OffAngleSegmentError(sx1, sy1, sx2, sy2, off, context=context)
    where = f" [{context}]" if context else ""
    warnings.warn(
        f"off-angle segment{where}: ({sx1:.4f}, {sy1:.4f}) -> "
        f"({sx2:.4f}, {sy2:.4f}) is {off:.4f} deg off the 0/45/90/135 set "
        f"(tol {tol_deg} deg).  Serializing as-is for the legacy "
        f"quantize_pcb_file fallback to repair; migrate this emitter to a "
        f"by-construction dogleg (issue #3907).  Set "
        f"{SEGMENT_45_STRICT_ENV}=1 to make this a hard error.",
        OffAngleSegmentWarning,
        stacklevel=2,
    )


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
        candidate = str(_uuid.uuid5(_uuid.NAMESPACE_OID, f"{parent_uuid}:dogleg:{n}"))
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

    Issue #3907: a diagonal whose serialized legs differ by a single
    0.1 um quantum (a legal ~45-degree A* diagonal -- e.g. dx=0.1385,
    dy=0.1384) is NOT reported, matching :func:`verify_segment_45`.  The
    census and the by-construction guard must agree on what "legal" means
    so a fresh route that the guard passes also passes the census ratchet.
    """
    text = Path(pcb_path).read_text()
    total = 0
    bad: list[dict] = []
    for m in _SEGMENT_BLOCK_RE.finditer(text):
        total += 1
        x1, y1, x2, y2 = (float(m.group(i)) for i in (2, 3, 4, 5))
        if _is_quantum_aligned(x2 - x1, y2 - y1):
            continue
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
        second_uuid = _derive_dogleg_uuid(seg_uuid, seen_uuids) if seg_uuid is not None else None
        return _block(x1, y1, mx, my, seg_uuid) + "\n" + _block(mx, my, x2, y2, second_uuid)

    new_text = _SEGMENT_BLOCK_RE.sub(_rewrite, text)
    if replaced and not dry_run:
        path.write_text(new_text)
    return replaced
