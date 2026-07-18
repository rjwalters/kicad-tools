"""Route-time layer-selection advisories (Issue #4314).

Warn-floor guards that surface two silent footguns in ``kct route``. Both
are *advisory* -- they print to stderr and never change the exit code or
the routed copper. They are the reporter's "or at minimum warn" floor for
tiers 1 & 2 of Issue #4314; the deeper structural alternatives (reserving
pour-net layers, deriving ``allowed_layers`` from ``target_ampacity``,
making :meth:`LayerDefinition.is_routable` pour-aware) are deliberately
deferred.

Tier 1 -- pour-net-blind ``--layers auto``
    :func:`kicad_tools.router.io.detect_layer_stack` infers the stack
    solely from ``(zone ...)`` s-expressions physically drawn in the input
    PCB and is never handed the loaded net-class-map. When a map declares
    ``is_pour_net`` classes (the common workflow of adding GND/PWR planes
    *post-route*), the input has no inner-layer zones, so auto silently
    picks a signal-on-inner configuration and the A* engine is free to
    route signal -- including high-current nets -- onto the layers the user
    intended to reserve for planes. :func:`pour_net_blind_auto_warning`
    detects this and recommends ``--layers 4``.

Tier 2 -- ampacity-vs-inner-layer conflict
    The route-time layer assignment never evaluates a net's
    ``target_ampacity`` against the candidate inner layer's copper weight,
    yet the post-route ampacity DRC classifies any non-``F.Cu``/``B.Cu``
    layer as internal (IPC-2221 ``k = 0.024``) and can flag the router's
    own inner-layer copper as impossibly under-rated (the reporter's
    "requires 65.5 mm" self-contradiction).
    :func:`ampacity_inner_layer_conflicts` predicts that DRC failure at
    route time, reusing the *exact* ``width_for_current`` call shape the
    DRC uses (``inner_copper_oz`` / ``layer="internal"``) so the route-time
    number and the DRC number agree to the last digit.

Drift-prevention contract
    When the net-class-map declares no ``is_pour_net`` classes and no
    ``target_ampacity``, every function here is a pure no-op (empty
    result / ``None``), so a board without those declarations sees
    byte-identical behavior and no new warnings -- mirroring the
    declarative drift-prevention contract of the sibling ampacity DRC.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from kicad_tools.physics.ampacity import width_for_current
from kicad_tools.router.layers import LayerType

if TYPE_CHECKING:
    from kicad_tools.manufacturers.base import DesignRules
    from kicad_tools.router.layers import LayerStack
    from kicad_tools.router.rules import NetClassRouting

# Mirrors ``validate/rules/ampacity.py::_EXTERNAL_LAYERS`` /
# ``AmpacityRule._is_external_layer`` exactly: any copper layer that is not
# ``F.Cu`` / ``B.Cu`` is internal for IPC-2221's k constant. Keeping this
# list identical is what makes the route-time advisory and the post-route
# DRC bucket every layer the same way (Issue #4314 acceptance criterion).
_EXTERNAL_LAYERS = ("F.Cu", "B.Cu")


def is_external_layer(layer_name: str) -> bool:
    """Return True for outer copper (``F.Cu`` / ``B.Cu``), False for internal.

    Byte-identical to
    :meth:`kicad_tools.validate.rules.ampacity.AmpacityRule._is_external_layer`
    so the route-time advisory and the post-route DRC classify every layer
    into the same internal/external bucket.
    """
    return layer_name in _EXTERNAL_LAYERS


def declared_pour_net_names(
    net_class_map: dict[str, NetClassRouting] | None,
) -> list[str]:
    """Return the names of net-class entries that declare ``is_pour_net``.

    Args:
        net_class_map: The ``{net_name: NetClassRouting}`` map loaded from a
            ``--net-class-map`` sidecar (``args._loaded_net_class_map``).
            ``None`` or empty yields ``[]``.

    Returns:
        Sorted names of entries whose class sets ``is_pour_net=True`` -- the
        nets the user intends to serve with copper pours / plane layers.
        Empty when the map is absent or declares no pour nets (the
        drift-prevention no-op).
    """
    if not net_class_map:
        return []
    return sorted(name for name, nc in net_class_map.items() if getattr(nc, "is_pour_net", False))


def stack_routes_signal_on_inner(layer_stack: LayerStack) -> bool:
    """Return True if the stack places signal on any inner (non-outer) layer.

    An inner signal layer is any layer that is not an outer (``F.Cu`` /
    ``B.Cu``) layer and whose :class:`~kicad_tools.router.layers.LayerType`
    is ``SIGNAL`` or ``MIXED`` (a split plane that still carries signal).
    Plane layers do not count -- the plane-aware ``--layers 4`` stack
    (``SIG-GND-PWR-SIG``) returns ``False`` here, which is exactly why
    passing ``--layers 4`` silences both advisories.

    Args:
        layer_stack: The resolved stack for this route.

    Returns:
        True when signal may be routed onto an inner layer.
    """
    return any(
        (not layer.is_outer) and layer.layer_type in (LayerType.SIGNAL, LayerType.MIXED)
        for layer in layer_stack.layers
    )


def pour_net_blind_auto_warning(
    layer_stack: LayerStack,
    net_class_map: dict[str, NetClassRouting] | None,
) -> str | None:
    """Build the Tier-1 pour-net-blind-``auto`` warning, or ``None``.

    Fires only when the auto-resolved ``layer_stack`` routes signal on inner
    layers **and** the loaded net-class-map declares one or more
    ``is_pour_net`` classes -- the exact combination in which
    ``detect_layer_stack`` (which never sees the map) silently strands
    plane intent.

    Args:
        layer_stack: The stack ``--layers auto`` selected.
        net_class_map: The loaded ``{net_name: NetClassRouting}`` map.

    Returns:
        A loud, multi-clause warning string recommending ``--layers 4``, or
        ``None`` when the condition does not hold (no pour nets, or the
        stack already reserves its inner layers for planes).
    """
    if not stack_routes_signal_on_inner(layer_stack):
        return None
    pour_nets = declared_pour_net_names(net_class_map)
    if not pour_nets:
        return None
    joined = ", ".join(pour_nets)
    return (
        f"WARNING: --layers auto selected '{layer_stack.name}', which routes "
        f"signal on inner layers, but the net-class-map declares "
        f"{len(pour_nets)} pour-net class(es): {joined}. Auto layer selection "
        "infers planes only from zones already drawn in the input PCB, so "
        "pour/plane nets you add post-route are invisible to it -- signal "
        "(including high-current nets) may be routed onto your intended "
        "plane layers, where it can never be manufactured to spec. "
        "Pass --layers 4 to reserve the inner layers for GND/PWR planes."
    )


@dataclass(frozen=True)
class AmpacityLayerConflict:
    """A route-time ampacity-vs-inner-layer conflict for one net-class.

    Attributes:
        net_name: The net (net-class map key) that carries the target.
        current_a: The class's ``target_ampacity`` in amps.
        required_internal_width_mm: IPC-2221 minimum internal-copper width
            for ``current_a`` -- identical to the value the post-route
            ampacity DRC computes for an inner-layer segment of this net.
        max_routable_width_mm: The widest trace the router will lay for the
            class (its ``trace_width``) -- the "largest routable width the
            engine can place on that layer".
        inner_copper_oz: The internal copper weight used for the derivation
            (from the resolved manufacturer profile), surfaced in the
            message so it matches the DRC's ``(IPC-2221, <oz>oz internal)``
            annotation.
    """

    net_name: str
    current_a: float
    required_internal_width_mm: float
    max_routable_width_mm: float
    inner_copper_oz: float

    @property
    def message(self) -> str:
        """The loud, DRC-consistent route-time warning line."""
        return (
            f"WARNING: net '{self.net_name}' targets {self.current_a:.1f}A but "
            f"the layer stack routes signal on inner layers. On internal copper "
            f"this needs a {self.required_internal_width_mm:.3f}mm trace "
            f"(IPC-2221, {self.inner_copper_oz}oz internal), far above the "
            f"{self.max_routable_width_mm:.3f}mm the router will lay -- the "
            "post-route ampacity DRC will flag any inner-layer segment as "
            "impossibly under-rated (and `kct pcb reinforce` cannot rescue an "
            "inner-layer high-current net). Pass --layers 4 to reserve the "
            f'inner layers for planes, or set allowed_layers=["F.Cu","B.Cu"] '
            f"for the '{self.net_name}' class."
        )


def ampacity_inner_layer_conflicts(
    net_class_map: dict[str, NetClassRouting] | None,
    design_rules: DesignRules,
    layer_stack: LayerStack,
) -> list[AmpacityLayerConflict]:
    """Return the Tier-2 ampacity-vs-inner-layer conflicts for this route.

    For each net-class that sets ``target_ampacity`` (and is not itself a
    pour net -- pour nets become plane fills, not routed signal), computes
    the IPC-2221 internal-copper required width via the *exact*
    ``width_for_current`` call shape the ampacity DRC uses
    (``copper_weight_oz=design_rules.inner_copper_oz``, ``layer="internal"``).
    A conflict is reported when the stack routes signal on inner layers and
    that required width exceeds the widest trace the router will lay for the
    class (its ``trace_width``) -- i.e. the router would produce inner-layer
    copper its own post-route ampacity DRC flags as impossible.

    Args:
        net_class_map: The loaded ``{net_name: NetClassRouting}`` map.
        design_rules: The resolved manufacturer :class:`DesignRules`
            (supplies ``inner_copper_oz``). Resolve it the same way the
            post-route DRC does so the numbers agree.
        layer_stack: The resolved stack for this route.

    Returns:
        One :class:`AmpacityLayerConflict` per unsatisfiable net-class,
        empty when no class sets ``target_ampacity`` or the stack reserves
        its inner layers for planes (the drift-prevention no-op).
    """
    if not net_class_map or not stack_routes_signal_on_inner(layer_stack):
        return []

    conflicts: list[AmpacityLayerConflict] = []
    for name, nc in net_class_map.items():
        current = getattr(nc, "target_ampacity", None)
        if current is None:
            continue
        # Pour nets are auto-skipped by the router (they become plane
        # fills, not routed signal), so an ampacity-on-inner-layer warning
        # for them would be a false positive.
        if getattr(nc, "is_pour_net", False):
            continue

        required_internal = width_for_current(
            float(current),
            copper_weight_oz=design_rules.inner_copper_oz,
            layer="internal",
        )
        max_width = float(getattr(nc, "trace_width", 0.2))
        if required_internal > max_width:
            conflicts.append(
                AmpacityLayerConflict(
                    net_name=name,
                    current_a=float(current),
                    required_internal_width_mm=required_internal,
                    max_routable_width_mm=max_width,
                    inner_copper_oz=design_rules.inner_copper_oz,
                )
            )
    return conflicts
