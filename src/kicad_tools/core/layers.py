"""Copper-layer stackup helpers shared across analysis and DRC code.

KiCad's layer *numbering* is not the physical stackup order (KiCad 9
numbers F.Cu=0, B.Cu=2, inner layers 4, 6, 8, ...), so code that needs
"which layers does this via barrel pass through?" must consult the
canonical front-to-back name ordering instead of layer indices.

The canonical ordering and the :func:`via_spans_layer` predicate were
originally private to ``analysis/net_status.py``; Issue #3487 promoted
them here so the DRC clearance rule can reuse the same semantics (a
through-via barrel is physical copper on EVERY layer it spans, not just
the two endpoint layers KiCad lists in ``(layers "F.Cu" "B.Cu")``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

# Canonical copper layer ordering from top (front) to bottom (back).
# KiCad supports up to 30 inner layers named In1.Cu .. In30.Cu.
COPPER_LAYER_ORDER: tuple[str, ...] = (
    "F.Cu",
    *(f"In{i}.Cu" for i in range(1, 31)),
    "B.Cu",
)

_COPPER_LAYER_INDEX: dict[str, int] = {name: idx for idx, name in enumerate(COPPER_LAYER_ORDER)}


def is_declared_copper_layer(name: str, layer_type: str) -> bool:
    """Recognize canonical copper declarations, including mixed/jumper types.

    KiCad layer types: https://dev-docs.kicad.org/en/file-formats/sexpr-pcb/#layers-section
    User/graphic declarations cannot establish enabled copper membership.
    """
    return name in COPPER_LAYER_ORDER and layer_type in ("signal", "power", "mixed", "jumper")


def validate_copper_layer(layer: str, available_layers: Iterable[str]) -> None:
    """Reject a layer that is not a real, board-enabled copper layer.

    Two independent checks (Issue #4907):

    1. *Canonical spelling* -- ``layer`` must be one of the well-known
       KiCad copper layer names (:data:`COPPER_LAYER_ORDER`: ``F.Cu``,
       ``B.Cu``, ``In1.Cu``..``In30.Cu``). This rejects typos as well as
       non-copper layers such as ``F.SilkS`` or ``Edge.Cuts``.
    2. *Board availability* -- ``layer`` must appear in
       ``available_layers``, the set of copper layers this specific board
       actually declares as enabled (e.g. a 2-layer board only declares
       ``F.Cu``/``B.Cu``, so ``In1.Cu`` is rejected even though it passes
       check 1). Callers pass the board's own declared copper layer names
       here (e.g. ``PCB.copper_layers``) rather than a hardcoded list, so
       this rejects layers that are merely undeclared/disabled on *this*
       board without hardcoding a stackup.

    Empty or missing layer evidence rejects every requested copper layer;
    canonical spelling alone cannot establish that a layer is enabled.

    Args:
        layer: Layer name to validate (e.g. ``"F.Cu"``, ``"In1.Cu"``).
        available_layers: Copper layer names declared as enabled
            (``signal``/``power``/``mixed``/``jumper`` type) in the board's own layer table.

    Raises:
        ValueError: If *layer* is not a recognised copper layer name, or
            is not among the board's declared/enabled copper layers.
    """
    if layer not in COPPER_LAYER_ORDER:
        raise ValueError(
            f"{layer!r} is not a valid copper layer name. Copper layers are "
            f"'F.Cu', 'B.Cu', or an inner layer like 'In1.Cu'..'In30.Cu'."
        )

    available = set(available_layers)
    if layer not in available:
        raise ValueError(
            f"Layer {layer!r} is not available on this board. "
            f"Declared copper layers: {', '.join(sorted(available, key=_COPPER_LAYER_INDEX.__getitem__))}."
        )


def via_spans_layer(via_layers: list[str] | tuple[str, ...], target_layer: str) -> bool:
    """Check whether a via barrel passes through a target copper layer.

    In KiCad, a via with layers ``["F.Cu", "B.Cu"]`` is a through-via
    whose barrel is physical copper on ALL intermediate copper layers
    (In1.Cu, In2.Cu, ...), not just the two listed endpoint layers.  A
    blind/buried/micro via such as ``["F.Cu", "In1.Cu"]`` spans only the
    layers between (and including) its declared endpoints.

    Args:
        via_layers: Layer names declared on the via
            (e.g., ``["F.Cu", "B.Cu"]``).
        target_layer: Copper layer name to test (e.g., ``"In1.Cu"``).

    Returns:
        True if the via barrel exists on (electrically connects to)
        the target layer.
    """
    # Direct match -- layer explicitly listed on the via.
    if target_layer in via_layers:
        return True

    # Determine the span of the via in the copper stack.
    indices = [_COPPER_LAYER_INDEX[layer] for layer in via_layers if layer in _COPPER_LAYER_INDEX]
    if len(indices) < 2:
        return False

    # Target must also be a recognised copper layer.
    target_idx = _COPPER_LAYER_INDEX.get(target_layer)
    if target_idx is None:
        return False

    # Via spans from min to max index; any layer in between is connected.
    return min(indices) <= target_idx <= max(indices)


# Issue #6485: the via-type tokens KiCad writes immediately after ``via``.
# A through via carries NO token; ``through`` is accepted from callers as an
# explicit spelling of that and never written.
VIA_TYPE_KEYWORDS: frozenset[str] = frozenset({"micro", "blind", "buried"})

_OUTER_COPPER_LAYERS: frozenset[str] = frozenset({"F.Cu", "B.Cu"})


def resolve_via_type(
    layers: tuple[str, ...] | list[str],
    via_type: str | None = None,
) -> tuple[str | None, tuple[str, ...]]:
    """Return the ``(keyword, layers)`` a new via node must be written with.

    Issue #6485.  KiCad (measured on kicad-cli 10.0.6) decides a via's drilled
    span from its type keyword, **not** from its ``(layers ...)`` pair: a
    ``(via ...)`` node with no ``micro`` / ``blind`` / ``buried`` token is a
    *through* via whatever the pair says.  KiCad rewrites such a node to
    ``(layers "F.Cu" "B.Cu")`` on save and puts its hole in the through-hole
    drill file.  A keyword-less partial-span via therefore says one thing to a
    reader of ``layers`` and builds another -- the barrel shorts whatever sits
    on the layers the file claimed it never reached.

    This is the single write-time rule that makes that node unwritable:

    * ``via_type`` of ``"micro"`` / ``"blind"`` / ``"buried"`` is kept, with
      the layers as given.
    * ``via_type="through"`` is an explicit through via: no keyword, and the
      layers are widened to the outer pair so the file states what is drilled.
    * ``via_type=None`` on the outer ``F.Cu``/``B.Cu`` pair is a through via.
    * ``via_type=None`` on any *other* pair of copper layers is a caller asking
      for a partial span, so the keyword is derived: ``blind`` when one end is
      an outer layer, ``buried`` when neither is.

    Layer pairs that are not two recognised copper layers are returned
    unchanged (nothing can be inferred from them).

    Args:
        layers: The via's layer pair, in the order the caller wants written.
        via_type: The requested via type, or ``None`` to derive it.

    Returns:
        ``(keyword, layers)`` -- ``keyword`` is ``None`` for a through via.

    Raises:
        ValueError: If ``via_type`` is not a via type KiCad knows.
    """
    layer_pair = tuple(layers)
    if via_type in VIA_TYPE_KEYWORDS:
        return via_type, layer_pair
    if via_type == "through":
        return None, ("F.Cu", "B.Cu")
    if via_type is not None:
        raise ValueError(
            f"Unknown via type {via_type!r}. Use 'micro', 'blind', 'buried', "
            f"'through', or None to derive it from the layer pair."
        )
    if len(layer_pair) != 2 or any(name not in _COPPER_LAYER_INDEX for name in layer_pair):
        return None, layer_pair
    outer_ends = sum(1 for name in layer_pair if name in _OUTER_COPPER_LAYERS)
    if outer_ends == 2 and layer_pair[0] != layer_pair[1]:
        return None, layer_pair
    return ("blind" if outer_ends else "buried"), layer_pair


def warn_derived_via_type(
    log: logging.Logger,
    writer: str,
    layers: tuple[str, ...] | list[str],
    x: float,
    y: float,
    keyword: str | None,
) -> None:
    """Log that *writer* derived a ``blind`` / ``buried`` keyword (Issue #6485).

    Called by every writer that hands :func:`resolve_via_type` a ``None``
    type.  A derived keyword changes the fab process the board needs, and
    neither ``kct check --mfr`` nor kicad-cli DRC reports a blind or buried
    via on a profile that cannot build one (Issue #6518), so this warning is
    the only signal.  A no-op when ``keyword`` is ``None`` (a through via).
    """
    if keyword is None:
        return
    log.warning(
        "%s: layers %s are a partial span, so the via at (%s, %s) is "
        "written as (via %s ...). It needs a fab process with %s vias; "
        "pass via_type='through' for an ordinary through via.",
        writer,
        tuple(layers),
        x,
        y,
        keyword,
        keyword,
    )


def kicad_read_via_layers(
    layers: tuple[str, ...] | list[str],
    via_type: str | None,
) -> tuple[tuple[str, ...], bool]:
    """Return the layer span KiCad *reads* for a parsed via node (Issue #6493).

    The reader half of :func:`resolve_via_type`.  KiCad (kicad-cli 10.0.6)
    decides a via's drilled span from its type keyword, not from its
    ``(layers ...)`` pair: a ``(via ...)`` node with no ``micro`` /
    ``blind`` / ``buried`` token is a through via whatever the pair says,
    and ``kicad-cli pcb upgrade --force`` rewrites it to
    ``(layers "F.Cu" "B.Cu")``.  Taking a keyword-less ``"In1.Cu" "B.Cu"``
    pair literally made kicad-tools model a blind via that KiCad never
    builds -- a false open where the barrel really joins F.Cu, and a false
    clean where it really shorts a track on a layer the pair omits.

    Every via parser (``schema.pcb.Via.from_sexp`` and the router's two
    regex parsers), and the raw-node readers that take a via's layers
    straight from the tree (zone-fill clearance, DSN export, the DRC local
    rerouter, the reasoning state and ``strip_traces``; Issue #6614, with
    :func:`via_node_type`), route the span through this one rule:

    * a ``micro`` / ``blind`` / ``buried`` via keeps its declared layers;
    * a keyword-less via whose two layers are recognised copper layers
      other than the outer ``F.Cu``/``B.Cu`` pair reads as
      ``("F.Cu", "B.Cu")``;
    * anything else (already the outer pair, or a layer list that is not
      two recognised copper names) is returned unchanged -- nothing can be
      inferred from it.

    Args:
        layers: The layer names from the node's ``(layers ...)`` child.
        via_type: The node's type keyword, or ``None`` when it has none.

    Returns:
        ``(layers, normalised)`` -- ``normalised`` is ``True`` when the span
        was widened to the outer pair, so callers can report the count.
    """
    layer_tuple = tuple(layers)
    if via_type is not None:
        return layer_tuple, False
    if len(layer_tuple) != 2 or any(name not in _COPPER_LAYER_INDEX for name in layer_tuple):
        return layer_tuple, False
    if set(layer_tuple) == _OUTER_COPPER_LAYERS:
        return layer_tuple, False
    return ("F.Cu", "B.Cu"), True


def via_node_type(node: Any) -> str | None:
    """Return a parsed ``(via ...)`` node's ``micro``/``blind``/``buried`` keyword.

    KiCad writes the type keyword as a bare atom straight after ``via``; a
    through via carries none.  ``node`` is a :class:`kicad_tools.sexp.SExp`
    (anything with a ``values`` list).  Returns ``None`` for a keyword-less
    via -- the ``via_type`` argument :func:`kicad_read_via_layers` expects
    from a raw-node reader (Issue #6614).
    """
    values = getattr(node, "values", None)
    if values and isinstance(values[0], str) and values[0] in VIA_TYPE_KEYWORDS:
        return values[0]
    return None


def warn_normalised_vias(log: logging.Logger, source: str, count: int) -> None:
    """Log ONE warning for *count* keyword-less partial-span vias (Issue #6493).

    A no-op when ``count`` is zero.  Callers aggregate per board instead of
    warning per via.
    """
    if count <= 0:
        return
    log.warning(
        "%s: %d via(s) have a partial (layers ...) span but no "
        "blind/buried/micro keyword. KiCad reads such a via as a through via "
        "(F.Cu to B.Cu), so it is read that way here; add the keyword if a "
        "blind or buried via was meant.",
        source,
        count,
    )
