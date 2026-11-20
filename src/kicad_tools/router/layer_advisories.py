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

Tier 3 -- plane-layer signal-reservation guardrail (Issue #5014)
    :meth:`kicad_tools.router.layers.LayerDefinition.is_routable`
    deliberately returns ``True`` for every copper layer, including layers
    a ``--layers 4``-style stack designates ``LayerType.PLANE`` (the
    continuous GND/PWR reference a controlled-impedance design depends
    on).  That is intentional -- KiCad zone fills flow around traces, so
    routing on a plane layer produces manufacturable copper -- but it
    means the CLI never surfaces the impedance-integrity consequence: the
    router is free to consume the reference plane with ordinary signal.
    Two independent guardrails close that gap:

    * :func:`plane_layer_reservation_advisory` -- a route-time warning
      (like Tiers 1 & 2, stderr-only, exit code unchanged) recommending
      ``--reserve-plane-layers`` whenever the resolved stack declares one
      or more ``PLANE`` layers and the caller has opted OUT of the hard
      restriction (``--no-reserve-plane-layers``).
    * :func:`reserve_plane_layers_allowed_layers` -- computes the
      ``DesignRules.allowed_layers`` value that makes the restriction
      HARD: every ``PLANE``-typed layer is dropped from the routable set,
      so the A* engine physically refuses to place signal there.  Wired
      to ``kct route --reserve-plane-layers``.  Issue #5789 flipped this
      on by default -- ``--no-reserve-plane-layers`` restores the
      historical mixed-layer-routing behaviour byte for byte, where PLANE
      metadata is advisory-only.

    :func:`plane_layer_signal_violations` closes the loop post-route: it
    scans the committed ``Autorouter.routes`` for any segment that landed
    on a declared ``PLANE`` layer, so a recipe that intentionally routes
    mixed-layer (``--layers 4-all`` or a stack with no planes at all)
    sees nothing, while a plane-bearing stack that still leaked signal
    onto the reference layer -- e.g. because ``--no-reserve-plane-layers``
    was passed -- gets an explicit, per-net report naming the layer and
    segment count.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kicad_tools.physics.ampacity import width_for_current
from kicad_tools.router.layers import Layer, LayerType

if TYPE_CHECKING:
    from kicad_tools.manufacturers.base import DesignRules
    from kicad_tools.router.layers import LayerStack
    from kicad_tools.router.primitives import Route
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


# --- Tier 3: plane-layer signal-reservation guardrail (Issue #5014) -----


@dataclass(frozen=True)
class PlaneZoneOutline:
    """One copper-pour zone on a declared ``PLANE`` layer (Issue #9597).

    Attributes:
        net_number: The zone's resolved net number (0 when unresolvable).
        net_name: The zone's net name ("" when the board carries none).
        layer_name: The KiCad name of the ``PLANE`` layer the zone pours on.
        clearance: The zone's own clearance (mm), as ``plane_split`` reads it.
        polygon: The zone outline vertices, translated into the router's
            sheet-absolute frame (board coordinates plus the board origin) so
            grid marking, the crossing report and committed routes all
            compare in one space.
        source_uuid: The source zone's uuid (diagnostics / grid zone ids).
    """

    net_number: int
    net_name: str
    layer_name: str
    clearance: float
    polygon: tuple[tuple[float, float], ...]
    source_uuid: str = ""


def plane_zone_outlines(
    pcb: Any, layer_stack: LayerStack, origin: tuple[float, float]
) -> tuple[PlaneZoneOutline, ...]:
    """Parse a board's pour zones that sit on declared ``PLANE`` layers.

    Only ``PLANE``-typed layers are considered: signal-layer pours stay
    invisible to the router exactly as before (out of scope for #9597 --
    the default flow never marks them, so nothing changes for a board with
    no plane stack).  Keepout rule areas are skipped; they are already
    enforced by the rule-area machinery.  Returns one outline per
    ``(zone, plane layer)`` pair, deterministically ordered.

    Args:
        pcb: A :class:`kicad_tools.schema.pcb.PCB` (board-relative frame).
        layer_stack: The resolved stack this route runs against.
        origin: The board origin added to translate into sheet coordinates.
    """
    plane_names = {name: idx for idx, name in enumerate(plane_layer_names(layer_stack))}
    if not plane_names:
        return ()

    name_to_number: dict[str, int] = {n.name: n.number for n in pcb.nets.values() if n.name}

    def resolve(number: int, name: str) -> int:
        if number == 0 and name:
            return name_to_number.get(name, 0)
        return number

    outlines: list[PlaneZoneOutline] = []
    seen: set[tuple[str, str]] = set()
    for zone in pcb.zones:
        if getattr(zone, "keepout", None) is not None or len(zone.polygon) < 3:
            continue
        net = resolve(zone.net_number, zone.net_name)
        if net == 0:
            continue
        for layer in _zone_layer_names(zone):
            if layer not in plane_names or (layer, zone.uuid or "") in seen:
                continue
            seen.add((layer, zone.uuid or ""))
            outlines.append(
                PlaneZoneOutline(
                    net_number=net,
                    net_name=zone.net_name or f"net {net}",
                    layer_name=layer,
                    clearance=max(float(zone.clearance), 0.0),
                    polygon=tuple((x + origin[0], y + origin[1]) for x, y in zone.polygon),
                    source_uuid=zone.uuid or "",
                )
            )
    outlines.sort(key=lambda o: (o.layer_name, o.net_name, o.polygon[:1]))
    return tuple(outlines)


def _zone_layer_names(zone: Any) -> list[str]:
    """Concrete layer names a zone occupies (wildcards skipped)."""
    names = [zone.layer, *getattr(zone, "layers", [])]
    out: list[str] = []
    for name in names:
        if name and "*" not in name and "&" not in name and name not in out:
            out.append(name)
    return out


def _cells_inside_polygon(
    grid: Any,
    layer_index: int,
    polygon: Any,
    gx_range: range,
    gy_range: range,
) -> set[tuple[int, int]]:
    """Free grid cells whose centre lies inside ``polygon`` (boundary inclusive).

    Vectorised ray cast over the candidate block.  Cells within 1e-6 mm of
    any polygon edge are re-decided by the scalar
    :func:`kicad_tools.core.geometry.point_in_polygon` so boundary handling is
    identical to the scalar rule; every other cell is unambiguous.
    """
    import numpy as np

    from kicad_tools.core.geometry import point_in_polygon

    if len(gx_range) == 0 or len(gy_range) == 0 or len(polygon) < 3:
        return set()
    gxs = np.arange(gx_range.start, gx_range.stop)
    gys = np.arange(gy_range.start, gy_range.stop)
    wx = np.round(grid.origin_x + gxs * grid.resolution, 4)[None, :]
    wy = np.round(grid.origin_y + gys * grid.resolution, 4)[:, None]
    wx, wy = np.broadcast_arrays(wx, wy)
    free = ~(
        grid._blocked[layer_index, gy_range.start : gy_range.stop, gx_range.start : gx_range.stop]
        | grid._is_obstacle[
            layer_index, gy_range.start : gy_range.stop, gx_range.start : gx_range.stop
        ]
    )
    inside = np.zeros(wx.shape, dtype=bool)
    near = np.zeros(wx.shape, dtype=bool)
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        straddle = (yi > wy) != (yj > wy)
        if yj != yi:
            xcross = (xj - xi) * (wy - yi) / (yj - yi) + xi
            inside ^= straddle & (wx < xcross)
        ex, ey = xj - xi, yj - yi
        seg2 = ex * ex + ey * ey
        if seg2 == 0.0:
            d2 = (wx - xi) ** 2 + (wy - yi) ** 2
        else:
            t = np.clip(((wx - xi) * ex + (wy - yi) * ey) / seg2, 0.0, 1.0)
            d2 = (wx - (xi + t * ex)) ** 2 + (wy - (yi + t * ey)) ** 2
        near |= d2 <= 1e-12
        j = i
    keep = free & inside & ~near
    cells = {(int(gxs[c]), int(gys[r])) for r, c in zip(*np.nonzero(keep), strict=True)}
    for r, c in zip(*np.nonzero(free & near), strict=True):
        x, y = float(wx[r, c]), float(wy[r, c])
        if point_in_polygon(x, y, polygon, boundary_inside=True):
            cells.add((int(gxs[c]), int(gys[r])))
    return cells


def mark_plane_zones_on_grid(grid: Any, outlines: tuple[PlaneZoneOutline, ...]) -> int:
    """Rasterize plane-zone outlines onto the routing grid as zone cells.

    Marks each outline's interior cells (cell centre inside the polygon) as
    zone copper of the zone's net via :meth:`RoutingGrid.add_zone_cells`,
    skipping cells already blocked or carrying an obstacle so foreign pad /
    route copper keeps its authoritative net.  ``add_zone_cells`` mirrors the
    marks onto the paired C++ grid, so both A* backends see them.  A pure
    Python point-in-polygon test is used (no Shapely dependency here).

    Returns the number of grid cells marked.
    """
    from types import SimpleNamespace

    marked = 0
    for outline in outlines:
        try:
            layer_index = grid.layer_to_index(Layer.from_kicad_name(outline.layer_name).value)
        except (KeyError, ValueError):
            continue
        xs = [p[0] for p in outline.polygon]
        ys = [p[1] for p in outline.polygon]
        gx_min, gy_min = grid.world_to_grid(min(xs), min(ys))
        gx_max, gy_max = grid.world_to_grid(max(xs), max(ys))
        cells = _cells_inside_polygon(
            grid,
            layer_index,
            outline.polygon,
            range(max(0, gx_min), min(grid.cols, gx_max + 1)),
            range(max(0, gy_min), min(grid.rows, gy_max + 1)),
        )
        if not cells:
            continue
        grid.add_zone_cells(
            SimpleNamespace(uuid=outline.source_uuid, net_number=outline.net_number),
            cells,
            layer_index,
        )
        marked += len(cells)
    return marked


def grid_plane_layer_indices(grid: Any) -> list[int]:
    """Grid-layer indices ``grid.layer_stack`` types as ``PLANE`` (Issue #9826).

    The same set the Python pathfinder's ``_plane_layer_indices`` computes:
    a foreign zone there is costed (``cost_zone_foreign_plane``), anywhere
    else it is blocked.  :class:`CppGrid` installs it on the native grid so
    both A* backends split plane/non-plane zones identically.  Empty when the
    grid has no stack.
    """
    stack = getattr(grid, "layer_stack", None)
    if stack is None:
        return []
    return sorted(
        {
            int(layer.index)
            for layer in getattr(stack, "layers", [])
            if getattr(layer, "layer_type", None) == LayerType.PLANE
        }
    )


def remark_plane_zones(router: Any, grid: Any) -> int:
    """Re-mark ``router.plane_zones`` on a freshly built grid (Issue #9826).

    The load path marks plane zones once, on the grid it built.  A grid
    rebuilt afterwards (Monte Carlo / evolutionary trial reset, fine-grid
    pass) starts with no zone marks, so both backends would cross foreign
    plane zones for free.  Call this after the fixed baseline (pads, keepouts,
    rule areas) is restamped so pad copper keeps its net, as on load.
    Returns the number of cells marked; 0 when the router has no outlines.
    """
    outlines = getattr(router, "plane_zones", ()) or ()
    if not outlines:
        return 0
    return mark_plane_zones_on_grid(grid, tuple(outlines))


def plane_layer_names(layer_stack: LayerStack) -> list[str]:
    """Return the KiCad names of the stack's ``LayerType.PLANE`` layers.

    Empty for a stack with no dedicated reference planes (``--layers 2``,
    ``4-all``, or an ``auto``-detected all-signal board).
    """
    return [layer.name for layer in layer_stack.layers if layer.layer_type == LayerType.PLANE]


def non_plane_layer_names(layer_stack: LayerStack) -> list[str]:
    """Return the KiCad names of every layer that is NOT ``LayerType.PLANE``.

    Includes ``SIGNAL`` and ``MIXED`` layers (a split plane that still
    carries signal intentionally keeps its signal eligibility) -- only pure
    reference-plane layers are excluded.
    """
    return [layer.name for layer in layer_stack.layers if layer.layer_type != LayerType.PLANE]


def reserve_plane_layers_allowed_layers(layer_stack: LayerStack) -> list[str] | None:
    """Compute the ``--reserve-plane-layers`` hard ``allowed_layers`` value.

    Returns the KiCad names of every non-``PLANE`` layer in ``layer_stack``
    (suitable for :attr:`~kicad_tools.router.rules.DesignRules.allowed_layers`,
    which both the Python and C++ backends already enforce as a HARD
    constraint -- see Issue #715). Returns ``None`` when the stack declares
    no ``PLANE`` layers at all: there is nothing to reserve, so the caller
    should leave ``allowed_layers`` untouched (the drift-prevention no-op --
    a 2-layer or all-signal board sees byte-identical behavior).
    """
    if not plane_layer_names(layer_stack):
        return None
    return non_plane_layer_names(layer_stack)


def plane_layer_reservation_advisory(
    layer_stack: LayerStack, *, reserve_plane_layers: bool
) -> str | None:
    """Build the Tier-3 plane-layer signal-reservation warning, or ``None``.

    Fires whenever ``layer_stack`` declares one or more ``PLANE`` layers
    (e.g. ``--layers 4``'s SIG-GND-PWR-SIG stack) and the caller has not
    passed ``--reserve-plane-layers``. ``LayerDefinition.is_routable``
    returns ``True`` for those layers by design (KiCad zone fills flow
    around traces), so nothing today stops the router from consuming a
    controlled-impedance reference plane with ordinary signal.

    Args:
        layer_stack: The resolved stack for this route.
        reserve_plane_layers: Whether ``--reserve-plane-layers`` was passed
            (``args.reserve_plane_layers``). When ``True`` the hard
            restriction is already in effect, so the advisory is silent.

    Returns:
        A loud, actionable warning string, or ``None`` when the condition
        does not hold (no plane layers, or the restriction is already
        active).
    """
    if reserve_plane_layers:
        return None
    planes = plane_layer_names(layer_stack)
    if not planes:
        return None
    plane_joined = ", ".join(planes)
    signal_joined = ", ".join(non_plane_layer_names(layer_stack))
    return (
        f"WARNING: layer stack '{layer_stack.name}' designates {plane_joined} as "
        "controlled-impedance reference plane(s), but PLANE metadata is NOT a "
        "hard routing constraint today -- LayerDefinition.is_routable treats "
        "every copper layer as signal-eligible, so the router may freely "
        "route signal onto those planes and break the continuous reference "
        "construction the design depends on. Pass --reserve-plane-layers to "
        f"hard-restrict signal routing to the non-plane layers ({signal_joined}), "
        "or accept this warning if mixed-layer routing on the plane is "
        "intentional for this recipe."
    )


@dataclass(frozen=True)
class PlaneLayerSignalViolation:
    """One net's committed copper found on a declared reference-plane layer.

    Attributes:
        net_name: The net whose segment(s) landed on the plane layer.
        layer_name: The KiCad name of the offending ``PLANE`` layer.
        segment_count: How many committed :class:`~kicad_tools.router.primitives.Segment`
            instances of this net sit on that layer.
    """

    net_name: str
    layer_name: str
    segment_count: int

    @property
    def message(self) -> str:
        """The loud, per-net post-route violation line."""
        plural = "s" if self.segment_count != 1 else ""
        return (
            f"WARNING: net '{self.net_name}' has {self.segment_count} segment{plural} "
            f"routed on reference-plane layer '{self.layer_name}' -- this "
            "consumes copper reserved for a continuous GND/PWR reference and "
            "breaks controlled-impedance return-current paths. Re-route with "
            "--reserve-plane-layers to hard-block signal from this layer, or "
            f'add avoid_layers=["{self.layer_name}"] plus --strict-layers '
            "for this net specifically."
        )


def plane_layer_signal_violations(
    routes: list[Route],
    layer_stack: LayerStack,
    net_class_map: Mapping[str, Any] | None = None,
) -> list[PlaneLayerSignalViolation]:
    """Scan committed routes for signal segments on a declared plane layer.

    Args:
        routes: The router's committed ``Autorouter.routes`` (each a
            :class:`~kicad_tools.router.primitives.Route` with ``segments``
            carrying a ``.layer`` :class:`~kicad_tools.core.types.CopperLayer`).
        layer_stack: The resolved stack this route ran against.
        net_class_map: Optional net name -> ``NetClassRouting`` map.  Issue
            #6876: a ``(net, layer)`` pair whose class opted in to that plane
            layer via ``allow_plane_layers`` is deliberate and is not
            reported.  Other nets on the same plane, and the opted-in net on
            a different plane, still are.

    Returns:
        One :class:`PlaneLayerSignalViolation` per ``(net, plane layer)``
        pair with at least one segment on it, sorted by net name then layer
        name for deterministic output. Empty when the stack declares no
        ``PLANE`` layers (the drift-prevention no-op -- a 2-layer or
        all-signal board is never scanned) or no segment landed on one.
    """
    plane_names = set(plane_layer_names(layer_stack))
    if not plane_names:
        return []

    counts: dict[tuple[str, str], int] = {}
    for route in routes:
        for segment in route.segments:
            # ``Segment.layer`` is always a ``CopperLayer`` (``router.layers``
            # aliases ``Layer = CopperLayer``), so read ``kicad_name``
            # directly: a getattr fallback to ``str(...)`` would yield
            # ``"CopperLayer.IN1_CU"``, which can never match a stack name and
            # would degrade this audit into a silent false clean (#5014).
            layer_name = segment.layer.kicad_name
            if layer_name in plane_names:
                key = (route.net_name, layer_name)
                counts[key] = counts.get(key, 0) + 1

    from kicad_tools.router.rules import allowed_plane_layer_indices

    def _opted_in(net_name: str, layer_name: str) -> bool:
        if not net_class_map:
            return False
        net_class = net_class_map.get(net_name)
        opted = getattr(net_class, "allow_plane_layers", None)
        if not opted:
            return False
        if layer_name in opted:
            return True
        layer_def = layer_stack.get_layer_by_name(layer_name)
        return layer_def is not None and layer_def.index in allowed_plane_layer_indices(net_class)

    return [
        PlaneLayerSignalViolation(net_name=net_name, layer_name=layer_name, segment_count=count)
        for (net_name, layer_name), count in sorted(counts.items())
        if not _opted_in(net_name, layer_name)
    ]


# --- Tier 4: plane-zone crossing cost & report (Issue #9597) ------------


@dataclass(frozen=True)
class PlaneZoneCrossing:
    """One net's committed copper crossing a foreign zone on a plane layer.

    Attributes:
        net_name: The routed net whose segments cross the zone.
        zone_net_name: The crossed zone's net (e.g. ``+3V3``).
        layer_name: The KiCad name of the ``PLANE`` layer the zone pours on.
        segment_count: How many of the net's segments cross the zone outline.
    """

    net_name: str
    zone_net_name: str
    layer_name: str
    segment_count: int

    @property
    def message(self) -> str:
        """The per-(net, zone, layer) crossing report line."""
        plural = "s" if self.segment_count != 1 else ""
        return (
            f"WARNING: net '{self.net_name}' crosses zone {self.zone_net_name} "
            f"on {self.layer_name} ({self.segment_count} segment{plural}) -- "
            "this slices the pour's copper and can strand pads on the far "
            "side; the router charged it a routing cost. Re-route with "
            "--protect-plane-zones to refuse such routes outright."
        )


def plane_zone_crossings(
    routes: list[Route],
    outlines: tuple[PlaneZoneOutline, ...] | list[PlaneZoneOutline],
) -> list[PlaneZoneCrossing]:
    """Report committed segments that cross a foreign zone on a plane layer.

    Issue #9597.  The post-route companion of the route-time zone cost: the
    A* search charges :attr:`DesignRules.cost_zone_foreign_plane` per cell
    inside a foreign plane-layer zone (see
    :func:`mark_plane_zones_on_grid`), and this scan reports every crossing
    that still happened -- a route that crossed despite the cost, copper on
    an opted-in plane from before, or a router without the cost active.

    A segment *crosses* when its centreline intersects the zone outline
    polygon (Shapely; without Shapely the scan is a no-op).  Only segments
    on the zone's own layer count, and only when the routing net differs
    from the zone's net.

    Returns one :class:`PlaneZoneCrossing` per ``(net, zone, layer)`` with
    at least one crossing segment, sorted deterministically; empty when the
    board carries no plane-zone outlines.
    """
    if not outlines or not routes:
        return []
    try:
        from shapely.geometry import LineString, Polygon  # type: ignore[import-untyped]
        from shapely.validation import make_valid  # type: ignore[import-untyped]
    except ImportError:
        return []

    by_layer: dict[str, list[tuple[PlaneZoneOutline, Any]]] = {}
    for outline in outlines:
        if len(outline.polygon) < 3:
            continue
        poly = Polygon(outline.polygon)
        if not poly.is_valid:
            poly = make_valid(poly)
        if not poly.is_empty:
            by_layer.setdefault(outline.layer_name, []).append((outline, poly))
    if not by_layer:
        return []

    counts: dict[tuple[str, str, str], int] = {}
    for route in routes:
        for segment in route.segments:
            polys = by_layer.get(segment.layer.kicad_name)
            if not polys:
                continue
            line = LineString([(segment.x1, segment.y1), (segment.x2, segment.y2)])
            for outline, poly in polys:
                if outline.net_number == route.net:
                    continue
                if line.intersects(poly):
                    key = (
                        route.net_name or f"net {route.net}",
                        outline.net_name,
                        outline.layer_name,
                    )
                    counts[key] = counts.get(key, 0) + 1

    return [
        PlaneZoneCrossing(
            net_name=net_name, zone_net_name=zone_net, layer_name=layer, segment_count=count
        )
        for (net_name, zone_net, layer), count in sorted(counts.items())
    ]
