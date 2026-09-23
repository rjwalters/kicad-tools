"""Router primitives -> clearance-kernel shapes (Epic #5509, Phase 3).

The kernel (:mod:`kicad_tools.router.clearance_kernel`) deliberately knows
nothing about the router: it speaks :class:`~clearance_kernel.KSegment` /
:class:`~clearance_kernel.KVia` / :class:`~clearance_kernel.KPad` value
objects and answers "how far apart is this copper, edge to edge".  Every
consumer the epic migrates therefore needs the *same* translation from
:mod:`kicad_tools.router.primitives` objects into those shapes -- and it has
to be the same one, or two migrated consumers would disagree about the copper
rather than about the clearance.

This module is that single translation.  It is the production sibling of the
conformance harness's ``tests/conformance/adapters/_support.py``: one
translation, cited from both sides, so a difference between two migrated
consumers is a difference between two *rule resolutions*, never between two
readings of the same geometry.

**Rule resolution stays with the caller.**  Nothing here decides what
``required_mm`` is -- that is the consumer's own (unchanged) logic, and
Epic #5509's scope guard #1 forbids this phase from touching any rule value.
The helpers below answer *gaps* and take a requirement the caller resolved.

Layer convention
----------------
Layers are compared by :class:`kicad_tools.core.types.CopperLayer` ``value``,
not by routing-stack index.  Both are consistent integer namings of the same
layer and the kernel only ever tests them for equality, but mixing the two
inside one query would be a silent bug -- so every helper here uses ``value``
and nothing else.  Copper present on every layer (a via barrel, a
through-hole pad) carries :data:`~clearance_kernel.ALL_LAYERS`.
"""

from __future__ import annotations

from kicad_tools.router.clearance_kernel import (
    ALL_LAYERS,
    CLEARANCE_EPSILON_MM,
    NO_INTERACTION,
    KPad,
    KSegment,
    KShape,
    KVia,
    clear,
    copper_gap,
    make_pad,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Segment, Via

__all__ = [
    "ALL_LAYERS",
    "CLEARANCE_EPSILON_MM",
    "NO_INTERACTION",
    "KPad",
    "KSegment",
    "KShape",
    "KVia",
    "clear",
    "copper_gap",
    "gap_deficit",
    "overlaps",
    "pad_shape",
    "segment_shape",
    "shapes_clear",
    "via_shape",
]

#: KiCad's default roundrect corner ratio, used when a pad carries no explicit
#: one.  The router's :class:`~kicad_tools.router.primitives.Pad` has no
#: ``roundrect_rratio`` field, so this is the only value available -- and it is
#: the same default ``clearance_kernel.make_pad`` documents.
DEFAULT_ROUNDRECT_RRATIO = 0.25


def segment_shape(seg: Segment) -> KSegment:
    """A routed :class:`~primitives.Segment` as kernel copper."""
    return KSegment(
        x1=seg.x1,
        y1=seg.y1,
        x2=seg.x2,
        y2=seg.y2,
        width=seg.width,
        layer=_layer_value(seg.layer),
    )


def via_shape(via: Via) -> KVia:
    """A :class:`~primitives.Via` as kernel copper.

    A barrel is copper on every layer it spans.  The kernel's ``KVia`` is
    implicitly ``ALL_LAYERS``, which is the right model for the through vias
    the router emits; a caller that needs a blind/buried barrel's layer span
    must gate on ``via.layers`` itself before asking (the coupled search's
    ``rail_clear`` does exactly that, mirroring
    ``Grid3D::trace_stored_vias_clear``).
    """
    return KVia(x=via.x, y=via.y, diameter=via.diameter, drill=via.drill)


def pad_shape(pad: Pad) -> KPad:
    """A :class:`~primitives.Pad` as kernel copper.

    ``pad.rotation`` is the *residual* board-space angle (issue #4910): the
    ``io.py`` parsers already axis-swap ``width``/``height`` for a pad within
    1 degree of 90/270, and ``rotation`` carries only what that swap does not
    absorb.  That is exactly the angle ``make_pad`` wants, because the swapped
    dimensions plus the residual angle reconstruct the same board-frame
    rectangle the full angle plus the unswapped dimensions would.

    A through-hole pad is copper on every layer, and its hole is modelled from
    ``pad.drill`` so hole queries have something to read.
    """
    return make_pad(
        pad.shape,
        pad.width,
        pad.height,
        DEFAULT_ROUNDRECT_RRATIO,
        pad.rotation,
        pad.x,
        pad.y,
        ALL_LAYERS if pad.through_hole else _layer_value(pad.layer),
        pad.drill if pad.through_hole else 0.0,
    )


def shapes_clear(a: KShape, b: KShape, required_mm: float) -> bool:
    """``clear`` under the kernel's own epsilon -- re-exported for callers.

    A thin alias so a migrated consumer imports one module rather than two,
    and so the epsilon it compares under is provably the kernel's.
    """
    return clear(a, b, required_mm)


def gap_deficit(a: KShape, b: KShape, required_mm: float) -> float:
    """How far short of ``required_mm`` this pair falls; ``<= 0`` is clear.

    The "deficit" idiom several diff-pair gates already speak
    (``worst_segment_pad_deficit``, ``segment_via_deficit``), expressed on
    the kernel's exact gap.  A pair that cannot interact at all (different
    layers) has an infinite gap and therefore a deficit of ``0.0`` rather
    than ``-inf``, so callers can keep taking a plain ``max()`` over pads.
    """
    gap = copper_gap(a, b)
    if gap == NO_INTERACTION:
        return 0.0
    return required_mm - gap


def overlaps(a: KShape, b: KShape) -> bool:
    """True when the two coppers physically intersect (gap strictly below 0).

    The clearance-free half of the kernel: no requirement, no epsilon, just
    "does this metal touch that metal".  Used by the coupled constructor's
    physical-overlap self-check, which asks a short-detection question rather
    than a clearance question.
    """
    return copper_gap(a, b) < 0.0


def _layer_value(layer: Layer) -> int:
    """The integer layer id of a router :class:`~router.layers.Layer`.

    A one-line function on purpose: it is the *only* place this module turns
    a layer into the integer the kernel compares, so the module docstring's
    "``value``, never a stack index" convention has a single enforcement
    point rather than three call sites that each remembered.
    """
    return layer.value
