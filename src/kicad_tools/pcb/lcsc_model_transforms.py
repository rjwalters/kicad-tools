"""Curated per-part 3D-body transforms for the LCSC/EasyEDA model tier.

The three installed-library ``add-3d-models`` tiers *derive* a body's offset
and rotation geometrically, by comparing the target footprint's pad field
against the source ``.kicad_mod`` (``models3d._pad_anchor`` /
``models3d._pad_field_orientation``).  The fourth tier — LCSC/EasyEDA
fetch-on-demand (:mod:`kicad_tools.pcb.lcsc_models`) — has **no source
footprint at all**: it synthesizes a ``(model ...)`` node from nothing but a
C-number, so there is nothing to derive a rotation from.  EasyEDA STEP bodies
are authored in whatever orientation the part vendor chose, so the identity
rotation that tier emits by default is a *guess*, and for many parts it is
wrong by a quarter turn.

This module is where the fleet's **authored** corrections live: an explicit,
curated ``C-number -> transform`` table, mirroring the shape of the adjacent
:mod:`kicad_tools.pcb.model_substitutions` (policy docstring, dict constant,
single accessor).

**Why keyed by C-number.**  An LCSC C-number is a stable, global,
vendor-issued identifier for a physical part, and a body's native orientation
is a property of *that part*, not of any board.  ``lib_id``s in this fleet are
not stable — ``docs/guides/lcsc-3d-models.md`` notes each synthetic lib id is
unique per board — so a lib_id-keyed table would have a reuse rate of ~zero.
Keyed by C-number, one calibration is correct for every board and every
downstream consumer that installs this package.

**Placement-angle independence.**  A footprint's own ``(at x y angle)``
rotates the whole footprint *including* its 3D body, so a per-part transform
authored here stays correct at any board placement angle.  Calibrate once per
part; never per instance.

**Frame semantics.**  KiCad applies a model's transforms in the order
**scale -> rotate -> offset**, in the *model frame*: X matches the footprint 2D
X, **Y is negated** relative to the footprint 2D frame, and Z is up from the
board.  Because ``offset`` is applied *after* ``rotate``, the ``offset`` here
is a post-rotation translation — so when calibrating a part, set ``rotate``
first, render, and only then measure ``offset``.

**Precedence** (resolved in ``models3d._resolve_lcsc``, first non-``None``
wins, merged per field):

1. A per-board ``lcsc_models.json`` sidecar entry in object form
   (``{"lib_id": {"lcsc": "C...", "rotate": [...], "offset": [...]}}``).
2. This packaged table.
3. Identity (``rotate (xyz 0 0 0)`` / ``offset (xyz 0 0 0)``) — unchanged
   behavior for every part with no entry anywhere.

**Provenance is mandatory, and enforced.**  Every entry MUST carry a
:class:`TransformProvenance` recording the board, refdes, ISO date, and the
exact command used to verify it *by rendering*.  ``tests/test_lcsc_models.py``
walks this table and fails on any entry whose provenance is missing or
malformed.  This is a structural rule, not a documentation nicety: **only a
render can tell you whether a number is right**, CI has no KiCad to render
with, so the only defensible gate is "a human must have rendered this."  An
uncalibrated guess cannot land green.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from math import isfinite

__all__ = [
    "LCSC_MODEL_TRANSFORMS",
    "LcscModelTransform",
    "TransformProvenance",
    "entry_problems",
    "lookup_transform",
    "table_problems",
]

Triple = tuple[float, float, float]

_ISO_DATE_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

# Mirrors ``lcsc_models._LCSC_ID_RE``; duplicated (rather than imported) to
# keep this curated data module free of any dependency on the network client.
_LCSC_C_NUMBER_RE = re.compile(r"^C\d+$")


@dataclass(frozen=True)
class TransformProvenance:
    """Evidence that a table entry was verified by an actual render.

    Attributes:
        board: Repo-relative board directory the calibration was rendered
            from (e.g. ``"boards/06-diffpair-test"``).
        refdes: The reference designator inspected (e.g. ``"J3"``).
        verified: ISO ``YYYY-MM-DD`` date the render was inspected.
        command: The literal command sequence used, so the check is
            reproducible by the next person.
        notes: Optional free text — what the render showed, which candidate
            signs were compared, and why this one won.
    """

    board: str
    refdes: str
    verified: str
    command: str
    notes: str = ""


@dataclass(frozen=True)
class LcscModelTransform:
    """A curated ``(rotate, offset)`` correction for one LCSC part.

    Both triples are in the **model frame** (see the module docstring): X as
    the footprint 2D X, Y negated versus footprint 2D Y, Z up from the board.
    ``rotate`` is written verbatim into the emitted ``(rotate (xyz ...))``;
    ``offset`` is *added to* the pad-centroid delta the shared offset
    machinery already injects, and its Z component passes straight through
    (the knob for a STEP whose origin is not on the board plane).

    ``provenance`` is typed optional only so that a forgetful author gets a
    **test failure** rather than a ``TypeError`` at import — see
    :func:`entry_problems`.  A ``None`` provenance is never acceptable in the
    committed table.
    """

    rotate: Triple | None = None
    offset: Triple | None = None
    provenance: TransformProvenance | None = None


# --------------------------------------------------------------------------
# The curated table
# --------------------------------------------------------------------------
# Every entry below was verified by rendering the board named in its
# provenance and *looking at the body*.  Do not add an entry you have not
# rendered: the emission tests verify plumbing (composition, sign, frame),
# never the correctness of a number.  See "Calibrating a per-part transform"
# in docs/guides/lcsc-3d-models.md for the recipe.
LCSC_MODEL_TRANSFORMS: dict[str, LcscModelTransform] = {
    # Mini-PCIe / M.2-style edge socket.  The EasyEDA body is authored with
    # its 52-position card slot running along the model X axis (native XY
    # extents ~29.9 x 9.7 mm), while the footprint's pad column runs along
    # footprint Y -- so the identity rotation lays the socket across the
    # board edge at 90 degrees to its own pads (issue #4584).
    "C444929": LcscModelTransform(
        rotate=(0.0, 0.0, -90.0),
        provenance=TransformProvenance(
            board="boards/06-diffpair-test",
            refdes="J3",
            verified="2026-08-04",
            command=(
                "kct pcb add-3d-models /tmp/cal.kicad_pcb --lcsc-models "
                "/tmp/sidecar.json && kct render /tmp/cal.kicad_pcb && "
                "kicad-cli pcb render /tmp/cal.kicad_pcb --side top --zoom 5 "
                "--pivot 4.6,-0.2,0 -o /tmp/edge.png"
            ),
            notes=(
                "All three candidates (0, +90, -90) were rendered on a "
                "scratch copy of diffpair_test_routed.kicad_pcb with J3's "
                "stale model node stripped.  At rotate 0 the body lies "
                "east-west and hangs ~10 mm off the right board edge -- the "
                "reported symptom.  Both +/-90 put the long axis north-south, "
                "parallel to the 12-pad column at x=193.5.  -90 wins on the "
                "board-edge criterion: measured on the top orthographic "
                "render, the body spans board X 187.9..197.6 (0.9 mm clear of "
                "the Edge.Cuts right edge at x=198.5), while +90 spans "
                "189.4..199.1 and pokes 0.6 mm past it.  -90 also points the "
                "card-entry slot off-board (+X); +90 points it inboard, at "
                "U3.  A front-side render confirms the body seats on the "
                "board surface, so no offset is needed."
            ),
        ),
    ),
}


# --------------------------------------------------------------------------
# Lookup + validation
# --------------------------------------------------------------------------


def lookup_transform(c_number: str) -> LcscModelTransform | None:
    """Return the curated transform for *c_number*, or ``None`` if unlisted.

    Consulted by the LCSC resolver after a per-board sidecar override, and
    before falling back to identity.
    """
    return LCSC_MODEL_TRANSFORMS.get(c_number)


def _triple_problems(label: str, value: object) -> list[str]:
    """Return reasons *value* is not a usable 3-tuple of finite numbers."""
    if value is None:
        return []
    if not isinstance(value, tuple) or len(value) != 3:
        return [f"{label} must be a 3-tuple, got {value!r}"]
    problems: list[str] = []
    for axis, component in zip("xyz", value, strict=True):
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            problems.append(f"{label}.{axis} must be a number, got {component!r}")
        elif not isfinite(float(component)):
            problems.append(f"{label}.{axis} must be finite, got {component!r}")
    return problems


def entry_problems(c_number: str, entry: LcscModelTransform) -> list[str]:
    """Return every reason *entry* is not fit to ship, or ``[]`` when it is.

    Enforced by a test over :data:`LCSC_MODEL_TRANSFORMS`.  The provenance
    rules are the load-bearing part: an entry with no recorded render is an
    uncalibrated guess, and this repo has already shipped one of those green
    (#4457).  CI cannot render, so "a human rendered it and said so" is the
    strongest gate available.
    """
    problems: list[str] = []
    if not _LCSC_C_NUMBER_RE.match(c_number):
        problems.append(f"key {c_number!r} is not a C-number ('C' followed by digits)")
    if entry.rotate is None and entry.offset is None:
        problems.append("entry specifies neither rotate nor offset (it would be a no-op)")
    problems.extend(_triple_problems("rotate", entry.rotate))
    problems.extend(_triple_problems("offset", entry.offset))

    prov = entry.provenance
    if prov is None:
        problems.append(
            "missing provenance: every entry must record the render that "
            "verified it (board, refdes, ISO date, command)"
        )
        return problems
    for field_name in ("board", "refdes", "command"):
        value = getattr(prov, field_name)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"provenance.{field_name} must be a non-empty string")
    if not isinstance(prov.verified, str) or not _ISO_DATE_RE.match(prov.verified):
        problems.append(
            f"provenance.verified must be an ISO YYYY-MM-DD date, got {prov.verified!r}"
        )
    return problems


def table_problems() -> dict[str, list[str]]:
    """Return ``{c_number: problems}`` for every unfit entry in the table."""
    found: dict[str, list[str]] = {}
    for c_number, entry in LCSC_MODEL_TRANSFORMS.items():
        problems = entry_problems(c_number, entry)
        if problems:
            found[c_number] = problems
    return found
