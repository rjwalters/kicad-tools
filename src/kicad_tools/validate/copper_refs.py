"""Geometry-based item references for copper findings (Issue #6088).

``kct check`` used to name a track or via in a finding's ``items`` by its
UUID (``Trace-1a2b3c4d`` / ``Via-1a2b3c4d``).  Those UUIDs are random unless
the router runs with ``--seed``, so two routes with byte-identical copper
shared almost none of them, and a keyed waiver (see
:mod:`kicad_tools.validate.evidence`) recorded against one route never matched
the next.  As #5946 / #6015 did for silkscreen primitives, the descriptors
below name the copper by **what it is**, not by its identity:

* **Track** -- ``Trace@<layer>:w<width>:<x1>/<y1>~<x2>/<y2>``
* **Arc** -- ``Arc@<layer>:w<width>:<start>~<mid>~<end>``
* **Run** (``width_consistency``) -- ``Run@<layer>:w<width>:<end>~<end>:n<tracks>:h<digest>``
* **Via** -- ``Via@<x>/<y>:<top>-<bottom>:d<drill>/s<size>``

The net is not repeated in the descriptor: every finding already carries its
nets in the key's ``nets`` field.

Coordinates are **sheet** coordinates -- the literal ``(at ...)`` values in
the ``.kicad_pcb`` file, the frame ``kct check`` reports locations in.
:class:`~kicad_tools.schema.pcb.PCB` hands rules board-relative geometry (it
subtracts ``board_origin`` on load), so callers pass ``origin`` (see
:func:`board_origin`) to add it back.  Keying in the sheet frame keeps a key
unchanged when only the board outline -- and so the board origin -- moves.

Coordinates and sizes are quantised to 1 um (the same resolution the
evidence hash uses), so float noise from re-serialisation cannot flip a key,
while copper that moves by more than the quantum gets a new one.  Endpoints
are ordered canonically, so a segment written end-to-start gets the same key
as one written start-to-end.  Two pieces of copper with *identical*
descriptors (an exact duplicate segment, a doubled via) produce findings that
share a key; the evidence hash already folds the number of findings sharing
a key into its payload (``multiplicity``), so a waiver reviewed against one
cannot silently cover a newly added twin.

Every descriptor avoids ``,`` and ``|`` (the finding-key separators).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import Segment, Via

# Quantum: 1 um, expressed as the number of decimal places of a mm value.
_QUANTUM_PER_MM = 1000

_INNER_LAYER = re.compile(r"^In(\d+)\.Cu$")


_NM_PER_MM = 1_000_000


def _q(value: float) -> int:
    """Quantise a mm value to an integer number of micrometres.

    Snaps to whole nanometres (KiCad's internal unit) first, then rounds
    half-up in integer arithmetic, so the float noise left by the origin
    subtract/add round trip cannot flip a value that sits on a half-um boundary.
    """
    nanometres = round(float(value) * _NM_PER_MM)
    return (nanometres + 500) // 1000


def _mm(quantised: int) -> str:
    """Format a quantised value as a short mm string (``1.5``, ``100.123``, ``0``)."""
    text = f"{quantised / _QUANTUM_PER_MM:.3f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


Origin = tuple[float, float]
_NO_ORIGIN: Origin = (0.0, 0.0)


def board_origin(pcb: object) -> Origin:
    """``pcb.board_origin`` as floats; ``(0, 0)`` when absent or malformed."""
    origin = getattr(pcb, "board_origin", None)
    try:
        return (float(origin[0]), float(origin[1])) if origin else _NO_ORIGIN
    except (TypeError, ValueError, IndexError):
        return _NO_ORIGIN


def _qpoint(point: tuple[float, float], origin: Origin = _NO_ORIGIN) -> tuple[int, int]:
    return (_q(float(point[0]) + origin[0]), _q(float(point[1]) + origin[1]))


def _fmt_point(point: tuple[int, int]) -> str:
    return f"{_mm(point[0])}/{_mm(point[1])}"


def _clean(text: str) -> str:
    """Strip the finding-key separators from a free-form field (layer names)."""
    return text.replace(",", "_").replace("|", "_")


def _layer_order(layer: str) -> tuple[int, str]:
    """Physical stack order: ``F.Cu`` < ``In1.Cu`` < ... < ``B.Cu`` < anything else."""
    if layer == "F.Cu":
        return (0, layer)
    match = _INNER_LAYER.match(layer)
    if match:
        return (int(match.group(1)), layer)
    if layer == "B.Cu":
        return (1_000_000, layer)
    return (2_000_000, layer)


def segment_ref(seg: Segment, origin: Origin = _NO_ORIGIN) -> str:
    """Geometry descriptor of a track segment (or arc) for a finding's ``items``."""
    from kicad_tools.schema.pcb import Arc

    layer = _clean(seg.layer or "")
    width = _mm(_q(seg.width))
    a, b = sorted((_qpoint(seg.start, origin), _qpoint(seg.end, origin)))
    if isinstance(seg, Arc):
        # Reversing an arc swaps start/end but keeps its midpoint.
        mid = _fmt_point(_qpoint(seg.mid, origin))
        return f"Arc@{layer}:w{width}:{_fmt_point(a)}~{mid}~{_fmt_point(b)}"
    return f"Trace@{layer}:w{width}:{_fmt_point(a)}~{_fmt_point(b)}"


def run_ref(
    layer: str,
    width: float,
    start: tuple[float, float],
    end: tuple[float, float],
    count: int,
    origin: Origin = _NO_ORIGIN,
    vertices: Sequence[tuple[float, float]] | None = None,
) -> str:
    """Descriptor of a chain of ``count`` same-width tracks from ``start`` to ``end``.

    Used by ``width_consistency``, whose findings are about a whole run of
    tracks: naming each track would make the key grow with the run, so the
    run is named by its (canonically ordered) end points, width and length in
    tracks.  End points and count alone do not pin a run's shape -- two runs
    can bend through different intermediate points -- so when ``vertices``
    (the run's polyline, in order) is given, a short digest of its quantised
    vertices, canonically oriented, is appended as ``:h<digest>``.  Moving an
    interior vertex by more than the 1 um quantum therefore changes the key.
    """
    a, b = sorted((_qpoint(start, origin), _qpoint(end, origin)))
    ref = f"Run@{_clean(layer)}:w{_mm(_q(width))}:{_fmt_point(a)}~{_fmt_point(b)}:n{count}"
    if vertices:
        path: list[tuple[int, int]] = []
        for vertex in vertices:
            point = _qpoint(vertex, origin)
            if not path or path[-1] != point:
                path.append(point)
        canonical = min(path, path[::-1])
        digest = hashlib.sha256(repr(canonical).encode()).hexdigest()[:12]
        ref += f":h{digest}"
    return ref


def via_ref(via: Via, origin: Origin = _NO_ORIGIN) -> str:
    """Geometry descriptor of a via for a finding's ``items``."""
    # ``getattr``: rule tests drive these with duck-typed via stubs.
    raw_layers = getattr(via, "layers", None) or []
    layers = sorted((_clean(layer) for layer in raw_layers), key=_layer_order)
    if len(layers) >= 2:
        span = f"{layers[0]}-{layers[-1]}"
    elif layers:
        span = layers[0]
    else:
        span = "?"
    pos = _fmt_point(_qpoint(via.position, origin))
    drill = _mm(_q(getattr(via, "drill", 0.0) or 0.0))
    size = _mm(_q(getattr(via, "size", 0.0) or 0.0))
    return f"Via@{pos}:{span}:d{drill}/s{size}"
