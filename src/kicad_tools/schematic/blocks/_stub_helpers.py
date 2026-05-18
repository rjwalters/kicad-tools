"""Shared helpers for ``pin_nets`` stub-and-label emission across blocks.

Several block classes (``LDOBlock``, ``BuckConverter``, ``GateDriverBlock``,
``HalfBridge``, ``DebugHeader``) emit a short horizontal stub-wire away from
each annotated pin and place a KiCad net-label on the stub endpoint, so the
label is anchored to a wire endpoint (required by KiCad's label-only
connectivity rules; see issue #2980).

That stub-and-label pattern has a latent failure mode: if the stub endpoint
coordinate happens to lie on the interior of an unrelated foreign wire on
the sheet, KiCad treats the label as electrically welded to that wire and
silently bridges two distinct nets together.  PR #3014 added a
``BuckConverter``-local collision check (``_fb_stub_would_collide``) plus a
FB-pin-specific divert, but the four other emit sites still have the bug.

This module promotes the collision-detection primitive to a module-level
helper (``_stub_endpoint_would_collide``) and adds an auto-shifting emitter
(``_emit_pin_net_stub``) that:

  1. Tries the symbol-center-aware primary side first (the existing
     heuristic: stub left if the pin is left-of-center, otherwise right).
  2. Falls back to the opposite side if the primary endpoint would collide
     with a foreign wire.
  3. Raises ``ValueError`` if *both* sides would collide.  Silent net
     bridging is unrecoverable at netlist time, so we surface the failure
     loudly with both candidate coordinates in the message.

Public API:
    _stub_endpoint_would_collide(sch, x, y) -> bool
    _emit_pin_net_stub(sch, pin_pos, x_center, net_name, ports, *, block_label="")

See issues #3011 and #3015, and PR #3014 for the original FB-pin reference
implementation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_sch_helper import Schematic


# Default stub length: one KiCad grid (2.54 mm).  Matches the value used at
# every emit site this helper replaces.
_STUB = 2.54


def _stub_endpoint_would_collide(sch: "Schematic", x: float, y: float) -> bool:
    """Check whether a point ``(x, y)`` lands on an existing wire interior.

    Used by ``_emit_pin_net_stub`` to decide whether the symbol-center
    stub-direction heuristic would silently bridge the about-to-be-placed
    label into a foreign net.  Robust against mock ``Schematic`` objects
    used in unit tests: returns ``False`` if ``sch.wires`` is missing or
    not a real list, or if ``_find_wire_collisions_for_point`` is
    unavailable.

    Promoted from ``BuckConverter._fb_stub_would_collide`` in PR #3014.
    See issue #3015 for context.
    """
    wires = getattr(sch, "wires", None)
    if not isinstance(wires, list) or not wires:
        return False
    finder = getattr(sch, "_find_wire_collisions_for_point", None)
    if not callable(finder):
        return False
    try:
        collisions = finder(x, y)
    except Exception:  # noqa: BLE001 - defensive: mocks can raise
        return False
    return isinstance(collisions, list) and len(collisions) > 0


def _emit_pin_net_stub(
    sch: "Schematic",
    pin_pos: tuple[float, float],
    x_center: float,
    net_name: str,
    ports: dict[str, tuple[float, float]] | None,
    *,
    block_label: str = "",
) -> tuple[float, float]:
    """Emit a stub wire + net-label for a single ``pin_nets`` entry.

    Tries the symbol-center heuristic first (stub left if the pin is
    left-of-``x_center``, otherwise right).  If the resulting stub endpoint
    would land on a foreign wire (where the label would silently bridge two
    nets), tries the opposite side.  Raises ``ValueError`` if both sides
    would collide.

    Args:
        sch: Schematic to add the wire and label to.
        pin_pos: ``(x, y)`` of the pin to anchor.
        x_center: X coordinate used to pick the primary stub direction.
            Pins left of ``x_center`` stub left; everything else stubs
            right.  ``HalfBridge`` callers pass ``pin_pos[0] + 1.0`` to
            force a leftward primary side.
        net_name: Label text and (optional) port name to register.
        ports: Optional ``{net_name: pin_pos}`` registry to update with the
            pin's real coordinate (so external callers can reach the pin
            via ``block.ports[net_name]``).  Existing entries are
            preserved (back-compat).  Pass ``None`` to skip registration.
        block_label: Optional prefix used in the ``ValueError`` message
            (e.g. ``"LDOBlock "``) to ease debugging.

    Returns:
        ``(label_x, label_y)`` of the emitted label (always equal to
        ``pin_pos[1]`` for ``y``).

    Raises:
        ValueError: If both the primary and fallback stub endpoints would
            land on existing foreign wires.  Silent net bridging would
            otherwise result; the caller must move the block or split the
            colliding rail.
    """
    if pin_pos[0] < x_center:
        primary_x = pin_pos[0] - _STUB
        fallback_x = pin_pos[0] + _STUB
    else:
        primary_x = pin_pos[0] + _STUB
        fallback_x = pin_pos[0] - _STUB

    for candidate_x in (primary_x, fallback_x):
        if not _stub_endpoint_would_collide(sch, candidate_x, pin_pos[1]):
            sch.add_wire(pin_pos, (candidate_x, pin_pos[1]), warn_on_collision=False)
            sch.add_label(net_name, candidate_x, pin_pos[1], rotation=0)
            if ports is not None and net_name not in ports:
                ports[net_name] = pin_pos
            return (candidate_x, pin_pos[1])

    raise ValueError(
        f"{block_label}pin_nets stub for net {net_name!r} at pin "
        f"{pin_pos} would land on an existing wire on both sides "
        f"(stub_x candidates: {primary_x}, {fallback_x}). "
        f"Silent net-bridging would result. Move the block or split "
        f"the colliding rail."
    )
