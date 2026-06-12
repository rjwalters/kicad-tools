"""
Schematic Elements Mixin

Provides basic element addition operations (symbols, wires, labels, etc.)
and grid snapping functionality.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING

from ..grid import is_on_grid, snap_to_grid
from ..logging import _log_debug, _log_info
from .elements import (
    GlobalLabel,
    HierarchicalLabel,
    Junction,
    Label,
    NoConnect,
    PowerSymbol,
    Wire,
)
from .symbol import SymbolDef, SymbolInstance

if TYPE_CHECKING:
    pass


class SchematicElementsMixin:
    """Mixin providing element addition and grid snapping for Schematic class."""

    # Default tolerance for point matching (in mm)
    POINT_TOLERANCE = 0.1

    def _point_on_wire(
        self, x: float, y: float, wire: Wire, tolerance: float = POINT_TOLERANCE
    ) -> bool:
        """Check if a point lies on a wire segment.

        Args:
            x, y: Point coordinates
            wire: Wire to check against
            tolerance: Maximum distance from wire to be considered "on" it

        Returns:
            True if point is on the wire segment within tolerance
        """
        x1, y1, x2, y2 = wire.x1, wire.y1, wire.x2, wire.y2

        # Check if point is within bounding box
        if not (min(x1, x2) - tolerance <= x <= max(x1, x2) + tolerance):
            return False
        if not (min(y1, y2) - tolerance <= y <= max(y1, y2) + tolerance):
            return False

        # Calculate wire length
        dx = x2 - x1
        dy = y2 - y1
        length = (dx * dx + dy * dy) ** 0.5

        if length < tolerance:
            # Wire is basically a point - check distance to that point
            return ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5 < tolerance

        # Calculate perpendicular distance from point to line
        dist = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / length
        return dist < tolerance

    def _point_on_any_wire(self, x: float, y: float, tolerance: float = POINT_TOLERANCE) -> bool:
        """Check if a point lies on any wire in the schematic.

        Args:
            x, y: Point coordinates
            tolerance: Maximum distance from wire to be considered "on" it

        Returns:
            True if point is on any wire segment within tolerance
        """
        for wire in self.wires:
            if self._point_on_wire(x, y, wire, tolerance):
                return True
        return False

    def _point_on_wire_segment_interior(
        self, x: float, y: float, wire: Wire, tolerance: float = POINT_TOLERANCE
    ) -> bool:
        """Check if a point lies strictly INSIDE a wire segment (not at endpoints).

        This is used to detect wire endpoint collisions that would create
        unintentional connections when a new wire's endpoint lands on an
        existing wire's interior.

        Args:
            x, y: Point coordinates
            wire: Wire to check against
            tolerance: Maximum distance from wire to be considered "on" it

        Returns:
            True if point is on the interior of the wire segment (not at endpoints)
        """
        # First check if point is on the wire at all
        if not self._point_on_wire(x, y, wire, tolerance):
            return False

        # Check if point is at either endpoint (within tolerance)
        dist_to_start = ((x - wire.x1) ** 2 + (y - wire.y1) ** 2) ** 0.5
        dist_to_end = ((x - wire.x2) ** 2 + (y - wire.y2) ** 2) ** 0.5

        # If point is close to either endpoint, it's not in the interior
        if dist_to_start < tolerance or dist_to_end < tolerance:
            return False

        return True

    def _find_wire_collisions_for_point(
        self, x: float, y: float, tolerance: float = POINT_TOLERANCE
    ) -> list[Wire]:
        """Find all wires where the given point lands on their interior.

        Args:
            x, y: Point coordinates
            tolerance: Maximum distance from wire to be considered "on" it

        Returns:
            List of wires where the point is on the wire's interior
        """
        collisions = []
        for wire in self.wires:
            if self._point_on_wire_segment_interior(x, y, wire, tolerance):
                collisions.append(wire)
        return collisions

    def _find_nearest_wire_point(
        self, x: float, y: float
    ) -> tuple[tuple[float, float] | None, float]:
        """Find the nearest point on any wire to the given coordinates.

        Args:
            x, y: Point coordinates

        Returns:
            Tuple of (nearest_point, distance) where nearest_point is (x, y) or None if no wires
        """
        if not self.wires:
            return None, float("inf")

        nearest_point = None
        min_dist = float("inf")

        for wire in self.wires:
            x1, y1, x2, y2 = wire.x1, wire.y1, wire.x2, wire.y2

            # Calculate wire length
            dx = x2 - x1
            dy = y2 - y1
            length_sq = dx * dx + dy * dy

            if length_sq < 0.0001:
                # Wire is basically a point
                closest = (x1, y1)
            else:
                # Project point onto line and clamp to segment
                t = max(0, min(1, ((x - x1) * dx + (y - y1) * dy) / length_sq))
                closest = (x1 + t * dx, y1 + t * dy)

            dist = ((x - closest[0]) ** 2 + (y - closest[1]) ** 2) ** 0.5
            if dist < min_dist:
                min_dist = dist
                nearest_point = closest

        return nearest_point, min_dist

    def _snap_coord(self, value: float, context: str = "") -> float:
        """Apply grid snapping to a single coordinate based on snap_mode.

        Args:
            value: Coordinate value
            context: Context for warning messages

        Returns:
            Snapped or original value based on snap_mode
        """
        from .schematic import SnapMode

        if self.snap_mode == SnapMode.OFF:
            return round(value, 2)

        on_grid = is_on_grid(value, self.grid)

        if self.snap_mode == SnapMode.WARN:
            if not on_grid:
                snapped = snap_to_grid(value, self.grid)
                warnings.warn(
                    f"Off-grid coordinate ({context}): {value} -> nearest: {snapped}", stacklevel=4
                )
            return round(value, 2)

        if self.snap_mode == SnapMode.STRICT:
            if not on_grid:
                snapped = snap_to_grid(value, self.grid)
                warnings.warn(
                    f"Auto-snapping off-grid coordinate ({context}): {value} -> {snapped}",
                    stacklevel=4,
                )
            return snap_to_grid(value, self.grid)

        # SnapMode.AUTO - silently snap
        return snap_to_grid(value, self.grid)

    def _snap_point(self, point: tuple[float, float], context: str = "") -> tuple[float, float]:
        """Apply grid snapping to a point based on snap_mode.

        Args:
            point: (x, y) coordinate tuple
            context: Context for warning messages

        Returns:
            Snapped or original point based on snap_mode
        """
        return (
            self._snap_coord(point[0], f"{context} x"),
            self._snap_coord(point[1], f"{context} y"),
        )

    def _load_symbol_def(self, lib_id: str) -> SymbolDef:
        """Load a symbol definition, honoring ``local_symbol_libs``.

        Resolution order:

        1. If the lib_id's library prefix matches a local lib registered
           on ``self.local_symbol_libs``, parse from that file directly.
        2. Otherwise, fall through to ``SymbolDef.from_library()`` which
           uses the global ``get_symbol_search_paths()`` search.

        Args:
            lib_id: Library:Symbol format id (e.g., ``"softstart_custom:UCC27211"``).

        Returns:
            SymbolDef parsed from the matching library file.
        """
        # Check for a local-lib match first
        if ":" in lib_id:
            lib_name = lib_id.split(":", 1)[0]
            local_libs = getattr(self, "local_symbol_libs", None)
            if local_libs:
                # Build a list of parent directories so the existing
                # SymbolDef parser (which appends "<lib_name>.kicad_sym"
                # to each search path) finds our local file naturally.
                local_dirs = []
                for lp in local_libs:
                    if lp.name == f"{lib_name}.kicad_sym" and lp.exists():
                        local_dirs.append(lp.parent)
                if local_dirs:
                    return SymbolDef.from_library(lib_id, lib_paths=local_dirs)

        # Fall through to global library search
        return SymbolDef.from_library(lib_id)

    def add_symbol(
        self,
        lib_id: str,
        x: float,
        y: float,
        ref: str,
        value: str = None,
        rotation: float = 0,
        footprint: str = "",
        snap: bool = True,
        auto_layout: bool = False,
        auto_footprint: bool = False,
        properties: dict[str, str] = None,
        unit: int = 1,
    ) -> SymbolInstance:
        """Add a symbol to the schematic.

        Args:
            lib_id: Library:Symbol format (e.g., "Audio:PCM5122PW")
            x, y: Center position (snapped to grid unless snap=False)
            ref: Reference designator (e.g., "U1")
            value: Value (defaults to symbol name)
            rotation: Rotation in degrees (0, 90, 180, 270)
            footprint: Footprint string (explicit footprint takes precedence)
            snap: Whether to apply grid snapping (default: True)
            auto_layout: If True, automatically adjust position to avoid overlaps
            auto_footprint: If True, automatically select footprint for passive
                components based on value and the configured profile
            properties: Optional dict of custom symbol properties (e.g.,
                {"Thermal_Rth_JC": "0.5", "Power_Dissipation": "5W"}).
                These are stored as hidden KiCad symbol properties.
            unit: Symbol unit number for multi-unit symbols (1-indexed,
                default 1).  KiCad symbols like LM393 contain several
                logical units in a single library entry; each unit's
                pins live in a different sub-symbol block.  Passing the
                correct unit here makes :meth:`SymbolInstance.pin_position`
                resolve to the unit-local coordinates so that pin 4
                (LM393 V-, on unit 3) returns the unit-3 position
                instead of a phantom unit-1 fallback (issue #3346).

        Returns:
            SymbolInstance with pin_position() method

        Example:
            # Add with auto-layout to avoid overlaps
            sym = sch.add_symbol(
                "Device:R", x=100, y=50, ref="R1",
                value="10k", auto_layout=True
            )

            # Add with automatic footprint selection
            sym = sch.add_symbol(
                "Device:C", x=100, y=50, ref="C1",
                value="100nF", auto_footprint=True
            )

            # Add with custom properties for thermal analysis
            sym = sch.add_symbol(
                "Device:Q_NMOS", x=100, y=50, ref="Q1",
                value="IRLZ44N",
                properties={"Thermal_Rth_JC": "0.5", "Power_Dissipation": "5W"}
            )
        """
        # Apply grid snapping if enabled
        if snap:
            x = self._snap_coord(x, f"symbol {ref}")
            y = self._snap_coord(y, f"symbol {ref}")

        # Apply auto-layout to find non-overlapping position
        if auto_layout:
            x, y = self.suggest_position(
                lib_id=lib_id,
                near=(x, y),
                avoid_overlaps=True,
                rotation=rotation,
            )

        # Load symbol definition if not cached.  When the schematic has
        # local symbol libs (set via ``Schematic(local_symbol_libs=...)``),
        # consult those first by passing an extended ``lib_paths`` list
        # to ``SymbolDef.from_library``.  This lets project-local
        # ``.kicad_sym`` files resolve their custom lib_ids without
        # requiring the user to globally install them.
        if lib_id not in self._symbol_defs:
            self._symbol_defs[lib_id] = self._load_symbol_def(lib_id)

        sym_def = self._symbol_defs[lib_id]

        # Auto-select footprint for passive components if enabled
        effective_footprint = footprint
        if auto_footprint and not footprint and value:
            from ..footprint_selector import select_footprint_for_passive

            # Get profile from schematic's footprint_selector if available
            profile = getattr(self, "_footprint_profile", "default")
            auto_fp = select_footprint_for_passive(lib_id, value, profile)
            if auto_fp:
                effective_footprint = auto_fp
                _log_debug(f"  Auto-selected footprint for {ref}: {auto_fp}")

        instance = SymbolInstance(
            symbol_def=sym_def,
            x=x,
            y=y,
            rotation=rotation,
            reference=ref,
            value=value or sym_def.name,
            footprint=effective_footprint,
            properties=properties or {},
            unit=unit,
        )

        self.symbols.append(instance)
        _log_info(f"Added symbol {ref} ({lib_id}) at ({x}, {y})")
        _log_debug(f"  Symbol {ref} has {len(sym_def.pins)} pins")
        return instance

    def add_power(
        self, lib_id: str, x: float, y: float, rotation: float = 0, snap: bool = True
    ) -> PowerSymbol:
        """Add a power symbol (GND, VCC, etc.).

        Args:
            lib_id: Power symbol library ID (e.g., "power:GND")
            x, y: Position (snapped to grid unless snap=False)
            rotation: Rotation in degrees
            snap: Whether to apply grid snapping (default: True)
        """
        ref = f"#PWR{self._pwr_counter:02d}"
        self._pwr_counter += 1

        # Apply grid snapping if enabled
        if snap:
            x = self._snap_coord(x, f"power {lib_id}")
            y = self._snap_coord(y, f"power {lib_id}")
        else:
            x = round(x, 2)
            y = round(y, 2)

        # Load power symbol definition
        if lib_id not in self._symbol_defs:
            self._symbol_defs[lib_id] = SymbolDef.from_library(lib_id)

        pwr = PowerSymbol(
            lib_id=lib_id,
            x=x,
            y=y,
            rotation=rotation,
            reference=ref,
            _symbol_def=self._symbol_defs[lib_id],
        )
        self.power_symbols.append(pwr)
        _log_info(f"Added power symbol {lib_id.split(':')[1]} at ({x}, {y})")
        return pwr

    # Private lib prefix for synthesized power symbols.  Distinguishing
    # these from KiCad's stock ``power:`` library makes round-trips
    # unambiguous and prevents accidental collisions if a future KiCad
    # release ships a ``power:VMOTOR`` symbol.
    _PWR_SYNTH_LIB_PREFIX = "kicad_tools_pwr"

    def add_pwr_symbol(
        self,
        net_name: str,
        x: float,
        y: float,
        rotation: float = 0,
        snap: bool = True,
    ) -> PowerSymbol:
        """Add a power symbol whose net name is set by the caller.

        Unlike :meth:`add_power`, which reaches into KiCad's stock
        ``power:`` library (where the symbol name and the published net
        name are baked in together — ``power:+24V`` always publishes
        ``+24V``), this helper **synthesizes** a one-pin power-input
        symbol on the fly with ``net_name`` as the published global net.

        The synthesized lib_symbol is cached per net name and registered
        for emission via :meth:`_build_lib_symbols_node`, so the symbol
        round-trips through save/load cycles (the next reader sees a
        normal ``lib_symbols`` entry in the schematic file, identical in
        structure to a stock ``power:+5V`` entry).

        This is the right call site whenever a rail label uses a name
        that doesn't match any stock ``power:`` symbol — most commonly
        domain-specific rails like ``VMOTOR`` (no KiCad analogue) or
        project-convention names like ``+3.3V`` (KiCad uses ``+3V3``).

        Args:
            net_name: The global net name to publish (e.g., ``"VMOTOR"``,
                ``"+3.3V"``, ``"+5V"``).  Becomes the symbol's ``Value``
                property AND its power-input pin name; KiCad uses the
                ``Value`` property to determine the global power net.
            x, y: Position (snapped to grid unless ``snap=False``).
            rotation: Rotation in degrees (0 = arrow up, 180 = arrow
                down, typical for GND).
            snap: Whether to apply grid snapping (default: True).

        Returns:
            The :class:`PowerSymbol` instance placed in the schematic.

        Example:
            # VMOTOR rail has no stock analogue — synthesize one.
            sch.add_rail(y=80, x_start=20, x_end=200, net_label="VMOTOR")
            sch.add_pwr_symbol("VMOTOR", x=30, y=70)
            sch.add_wire((30, 70), (30, 80))  # tie symbol pin to rail

            # +3.3V (with the dot) matches project convention; KiCad's
            # stock symbol is +3V3 which would publish a mismatched net.
            sch.add_pwr_symbol("+3.3V", x=100, y=70)

        See Also:
            - add_power(): for stock ``power:`` symbols (when the symbol
              name already matches the rail label).
            - add_pwr_flag(): to mark a power net as externally driven.
        """
        ref = f"#PWR{self._pwr_counter:02d}"
        self._pwr_counter += 1

        if snap:
            x = self._snap_coord(x, f"pwr_symbol {net_name}")
            y = self._snap_coord(y, f"pwr_symbol {net_name}")
        else:
            x = round(x, 2)
            y = round(y, 2)

        lib_id = f"{self._PWR_SYNTH_LIB_PREFIX}:{net_name}"

        # Build (or fetch from cache) the synthesized lib_symbol entry.
        if net_name not in self._synthesized_pwr_defs:
            sym_node = self._build_synth_pwr_lib_symbol(net_name)
            self._synthesized_pwr_defs[net_name] = sym_node
            # Register in _embedded_lib_symbols so _build_lib_symbols_node
            # emits the entry on save.  Keyed by lib_id (full prefixed
            # name), matching how loaded schematics key stock entries
            # like ``power:+5V``.
            self._embedded_lib_symbols[lib_id] = sym_node

        pwr = PowerSymbol(
            lib_id=lib_id,
            x=x,
            y=y,
            rotation=rotation,
            reference=ref,
        )
        self.power_symbols.append(pwr)
        _log_info(f"Added synthesized power symbol '{net_name}' at ({x}, {y})")
        return pwr

    def _build_synth_pwr_lib_symbol(self, net_name: str):
        """Construct a synthesized power-symbol lib_symbol S-expression.

        The structure mirrors KiCad's stock ``power:+5V`` entry exactly,
        differing only in:

        * The outer symbol name (``"kicad_tools_pwr:{net_name}"``).
        * The ``Value`` property (``"{net_name}"`` — this is what KiCad
          uses to determine the published global net).
        * The ``Description`` property (mentions ``net_name`` for
          discoverability).
        * The power_in pin's ``name`` field (set to ``net_name`` as a
          belt-and-suspenders match; KiCad's stock symbols leave it
          empty but the issue body specifies the pin name should match
          for net unification on older KiCad readers).
        * The graphical unit symbols are renamed to
          ``"{net_name}_0_1"`` / ``"{net_name}_1_1"`` per KiCad's
          parent/unit naming convention.

        Args:
            net_name: The net name to bake in.

        Returns:
            An :class:`SExp` node ready to insert into ``lib_symbols``.
        """
        from kicad_tools.sexp import parse_string

        # Escape any embedded quotes in net_name for the Description
        # property's quoted-string substitution (e.g., a hypothetical
        # net name containing a double quote).  S-expression strings
        # use backslash-escapes.
        desc_net = net_name.replace("\\", "\\\\").replace('"', '\\"')

        # Use a triple-quoted template so the structure is reviewable
        # at a glance.  All five substitutions of ``net_name`` are
        # explicit and use ``{nn}`` for legibility.
        template = """
        (symbol "{lib_id}"
            (power)
            (pin_numbers (hide yes))
            (pin_names (offset 0) (hide yes))
            (exclude_from_sim no)
            (in_bom yes)
            (on_board yes)
            (duplicate_pin_numbers_are_jumpers no)
            (property "Reference" "#PWR"
                (at 0 -3.81 0)
                (effects (font (size 1.27 1.27)) (hide yes))
            )
            (property "Value" "{nn}"
                (at 0 3.556 0)
                (effects (font (size 1.27 1.27)))
            )
            (property "Footprint" ""
                (at 0 0 0)
                (effects (font (size 1.27 1.27)) (hide yes))
            )
            (property "Datasheet" ""
                (at 0 0 0)
                (effects (font (size 1.27 1.27)) (hide yes))
            )
            (property "Description" "Synthesized power symbol for net \\"{desc_nn}\\""
                (at 0 0 0)
                (effects (font (size 1.27 1.27)) (hide yes))
            )
            (symbol "{nn}_0_1"
                (polyline
                    (pts (xy -0.762 1.27) (xy 0 2.54))
                    (stroke (width 0) (type default))
                    (fill (type none))
                )
                (polyline
                    (pts (xy 0 2.54) (xy 0.762 1.27))
                    (stroke (width 0) (type default))
                    (fill (type none))
                )
                (polyline
                    (pts (xy 0 0) (xy 0 2.54))
                    (stroke (width 0) (type default))
                    (fill (type none))
                )
            )
            (symbol "{nn}_1_1"
                (pin power_in line
                    (at 0 0 90)
                    (length 0)
                    (name "{nn}" (effects (font (size 1.27 1.27))))
                    (number "1" (effects (font (size 1.27 1.27))))
                )
            )
            (embedded_fonts no)
        )
        """.format(
            lib_id=f"{self._PWR_SYNTH_LIB_PREFIX}:{net_name}",
            nn=net_name,
            desc_nn=desc_net,
        )

        # parse_string returns a single top-level node when the input
        # has one expression.  In some implementations the root may be
        # the symbol node directly, in others wrapped in an anonymous
        # container; handle both defensively.
        parsed = parse_string(template.strip())
        if parsed.name == "symbol":
            return parsed
        # Wrapped form: pick out the inner symbol child.
        for child in parsed.children:
            if not child.is_atom and child.name == "symbol":
                return child
        raise RuntimeError(
            f"Failed to synthesize power lib_symbol for net '{net_name}': "
            f"unexpected parse tree shape (root={parsed.name!r})"
        )

    def add_pwr_flag(self, x: float, y: float) -> PowerSymbol:
        """Add a PWR_FLAG symbol to mark a power net as intentionally driven.

        Power symbols like +5V and GND are defined as **power input** pins - they
        indicate where a net receives power but don't declare a power source.
        Without PWR_FLAG, ERC reports: "Input Power pin not driven by any Output
        Power pins".

        PWR_FLAG tells KiCad that external power is intentionally provided at this
        point (e.g., from a connector, battery, or regulator output).

        When to use PWR_FLAG:
            - Power connector pins (DC jack, USB VBUS)
            - Voltage regulator outputs
            - Battery connections
            - Test points where power can be injected

        Args:
            x, y: Position for the PWR_FLAG (should be on the power net wire)

        Returns:
            The PowerSymbol created

        Example:
            # Power entry from USB connector
            sch.add_power("power:+5V", x=50, y=30)
            sch.add_pwr_flag(50, 35)  # Mark +5V as externally driven
            sch.add_wire((50, 30), (50, 50))

            # Voltage regulator output
            sch.add_power("power:+3.3V", x=100, y=30)
            sch.add_pwr_flag(100, 35)  # 3.3V is driven by regulator
            sch.add_wire((100, 30), (100, 50))

        See Also:
            - add_power(): Add power symbols (+5V, GND, etc.)
            - README.md in this module for power symbol documentation
        """
        return self.add_power("power:PWR_FLAG", x, y, rotation=0)

    def add_wire(
        self,
        p1: tuple[float, float],
        p2: tuple[float, float],
        snap: bool = True,
        warn_on_collision: bool = True,
    ) -> Wire:
        """Add a wire between two points.

        IMPORTANT - Wire Connectivity Rule:
            KiCad establishes electrical connections only where **wire endpoints meet**.
            A wire passing through a point does NOT connect to other wires at that point.

            To create a T-connection, you must split the rail into segments so that
            wire endpoints meet at the intersection point.

        Args:
            p1: Start point (x, y)
            p2: End point (x, y)
            snap: Whether to apply grid snapping (default: True)
            warn_on_collision: If True (default), emit a warning when wire endpoints
                land on the interior of existing wire segments. This detects potential
                unintended connections that often cause ERC errors.

        Returns:
            The Wire created

        Example:
            # WRONG: Wire passes through - NO CONNECTION
            sch.add_wire((0, 50), (200, 50))     # Continuous rail
            sch.add_wire((100, 50), (100, 100))  # Touches rail but NOT connected!

            # CORRECT: Wire endpoints meet - CONNECTED
            sch.add_wire((0, 50), (100, 50))     # Segment 1 ends at intersection
            sch.add_wire((100, 50), (200, 50))  # Segment 2 starts at intersection
            sch.add_wire((100, 50), (100, 100)) # Vertical meets at same point
            sch.add_junction(100, 50)           # Visual indicator (optional)

        Warning:
            When warn_on_collision is True and a wire endpoint lands on an existing
            wire's interior, a warning is emitted. This helps catch bugs where wire
            paths accidentally intersect unrelated nets, causing silent shorts.

        See Also:
            - add_junction(): Add visual indicator at wire connections
            - wire_to_rail(): Higher-level helper that handles segmentation
            - check_wire_collisions(): Validate all wires for endpoint collisions
            - README.md in this module for detailed connectivity documentation
        """
        # Apply grid snapping if enabled
        if snap:
            p1 = self._snap_point(p1, "wire start")
            p2 = self._snap_point(p2, "wire end")

        # Check for endpoint collisions with existing wires
        if warn_on_collision and self.wires:
            for point, name in [(p1, "start"), (p2, "end")]:
                collisions = self._find_wire_collisions_for_point(point[0], point[1])
                for colliding_wire in collisions:
                    warnings.warn(
                        f"Wire {name} endpoint ({point[0]}, {point[1]}) lands on existing wire "
                        f"segment from ({colliding_wire.x1}, {colliding_wire.y1}) to "
                        f"({colliding_wire.x2}, {colliding_wire.y2}), creating unintended connection. "
                        f"Consider using segmented rails or checking wire routing.",
                        stacklevel=2,
                    )

        wire = Wire.between(p1, p2)
        self.wires.append(wire)
        _log_debug(f"Added wire from ({p1[0]}, {p1[1]}) to ({p2[0]}, {p2[1]})")
        return wire

    def add_wire_path(
        self,
        *points: tuple[float, float],
        snap: bool = True,
        warn_on_collision: bool = True,
    ) -> list[Wire]:
        """Add a series of connected wire segments.

        Args:
            points: Sequence of (x, y) points to connect
            snap: Whether to apply grid snapping (default: True)
            warn_on_collision: If True (default), warn when endpoints land on existing wires

        Returns:
            List of wires created
        """
        wires = []
        for i in range(len(points) - 1):
            wires.append(
                self.add_wire(
                    points[i], points[i + 1], snap=snap, warn_on_collision=warn_on_collision
                )
            )
        return wires

    def add_junction(self, x: float, y: float, snap: bool = True) -> Junction:
        """Add a junction point (visual indicator only).

        IMPORTANT - Junction Semantics:
            Junctions are **visual indicators only** - they do NOT establish
            electrical connectivity. Wire endpoints must already meet at the
            junction point for an actual connection to exist.

            Use junctions to:
            1. Show that wires are intentionally connected (not just crossing)
            2. Distinguish connected crossings from non-connected overlaps

        Args:
            x, y: Junction position (snapped to grid unless snap=False)
            snap: Whether to apply grid snapping (default: True)

        Returns:
            The Junction created

        Example:
            # WRONG: Junction without endpoint connectivity - NO CONNECTION!
            sch.add_wire((0, 50), (200, 50))     # Continuous wire
            sch.add_wire((100, 0), (100, 100))   # Passes through
            sch.add_junction(100, 50)            # Visual only - NOT connected!

            # CORRECT: Endpoints meet, junction for visual clarity
            sch.add_wire((0, 50), (100, 50))     # Ends at intersection
            sch.add_wire((100, 50), (200, 50))   # Starts at intersection
            sch.add_wire((100, 0), (100, 50))    # Meets at intersection
            sch.add_wire((100, 50), (100, 100))  # Continues from intersection
            sch.add_junction(100, 50)            # Visual indicator

        See Also:
            - add_wire(): Create wires with proper endpoint connectivity
            - README.md in this module for detailed connectivity documentation
        """
        if snap:
            x = self._snap_coord(x, "junction")
            y = self._snap_coord(y, "junction")
        else:
            x = round(x, 2)
            y = round(y, 2)
        junc = Junction(x=x, y=y)
        self.junctions.append(junc)
        return junc

    def add_no_connect(self, x: float, y: float, snap: bool = True) -> NoConnect:
        """Add a no-connect marker at a pin to indicate intentional non-connection.

        No-connect markers silence ERC "pin not connected" warnings for pins
        that are intentionally left unconnected (e.g., NC pins on ICs).

        Args:
            x, y: Position of the pin (snapped to grid unless snap=False)
            snap: Whether to apply grid snapping (default: True)

        Returns:
            The NoConnect created

        Example:
            # Mark NC pins on an IC as intentionally unconnected
            ic = sch.add_symbol("Package_DIP:DIP-8_W7.62mm", 100, 100, "U1")
            # If pins 5 and 6 are NC:
            pin5_pos = ic.pin_position("5")
            pin6_pos = ic.pin_position("6")
            sch.add_no_connect(pin5_pos[0], pin5_pos[1])
            sch.add_no_connect(pin6_pos[0], pin6_pos[1])
        """
        if snap:
            x = self._snap_coord(x, "no_connect")
            y = self._snap_coord(y, "no_connect")
        else:
            x = round(x, 2)
            y = round(y, 2)
        nc = NoConnect(x=x, y=y)
        self.no_connects.append(nc)
        _log_debug(f"Added no-connect marker at ({x}, {y})")
        return nc

    def add_label(
        self,
        text: str,
        x: float,
        y: float,
        rotation: float = 0,
        snap: bool = True,
        validate_connection: bool = True,
    ) -> Label:
        """Add a net label.

        Args:
            text: Label text
            x, y: Label position (snapped to grid unless snap=False)
            rotation: Rotation in degrees
            snap: Whether to apply grid snapping (default: True)
            validate_connection: Whether to warn if label is not on a wire (default: True)

        Warning:
            If validate_connection is True and the label position is not on any wire,
            a warning will be issued. This helps catch disconnected labels that would
            cause ERC errors in KiCad.
        """
        if snap:
            x = self._snap_coord(x, f"label {text}")
            y = self._snap_coord(y, f"label {text}")
        else:
            x = round(x, 2)
            y = round(y, 2)

        # Validate that the label is on a wire
        if validate_connection and self.wires:
            if not self._point_on_any_wire(x, y):
                nearest_point, distance = self._find_nearest_wire_point(x, y)
                if nearest_point:
                    warnings.warn(
                        f"Label '{text}' at ({x}, {y}) is not on any wire. "
                        f"Nearest wire point is ({nearest_point[0]:.2f}, {nearest_point[1]:.2f}), "
                        f"{distance:.2f}mm away. This will cause ERC errors.",
                        stacklevel=2,
                    )
                else:
                    warnings.warn(
                        f"Label '{text}' at ({x}, {y}) is not on any wire. "
                        f"No wires found in schematic. This will cause ERC errors.",
                        stacklevel=2,
                    )

        label = Label(text=text, x=x, y=y, rotation=rotation)
        self.labels.append(label)
        return label

    def add_hier_label(
        self,
        text: str,
        x: float,
        y: float,
        shape: str = "input",
        rotation: float = 0,
        snap: bool = True,
        validate_connection: bool = True,
    ) -> HierarchicalLabel:
        """Add a hierarchical label.

        Args:
            text: Label text
            x, y: Label position (snapped to grid unless snap=False)
            shape: Label shape (input, output, bidirectional, passive)
            rotation: Rotation in degrees
            snap: Whether to apply grid snapping (default: True)
            validate_connection: Whether to warn if label is not on a wire (default: True)

        Warning:
            If validate_connection is True and the label position is not on any wire,
            a warning will be issued. This helps catch disconnected labels that would
            cause ERC errors in KiCad.
        """
        if snap:
            x = self._snap_coord(x, f"hier_label {text}")
            y = self._snap_coord(y, f"hier_label {text}")
        else:
            x = round(x, 2)
            y = round(y, 2)

        # Validate that the label is on a wire
        if validate_connection and self.wires:
            if not self._point_on_any_wire(x, y):
                nearest_point, distance = self._find_nearest_wire_point(x, y)
                if nearest_point:
                    warnings.warn(
                        f"Hierarchical label '{text}' at ({x}, {y}) is not on any wire. "
                        f"Nearest wire point is ({nearest_point[0]:.2f}, {nearest_point[1]:.2f}), "
                        f"{distance:.2f}mm away. This will cause ERC errors.",
                        stacklevel=2,
                    )

        hl = HierarchicalLabel(text=text, x=x, y=y, shape=shape, rotation=rotation)
        self.hier_labels.append(hl)
        return hl

    def add_global_label(
        self,
        text: str,
        x: float,
        y: float,
        shape: str = "bidirectional",
        rotation: float = 0,
        snap: bool = True,
        validate_connection: bool = True,
    ) -> GlobalLabel:
        """Add a global label that connects nets by name across all sheets.

        Global labels are simpler than hierarchical labels - they don't require
        sheet pins on parent sheets. Nets with the same global label name are
        automatically connected throughout the entire schematic hierarchy.

        Args:
            text: Label text (net name, e.g., "VCC_3V3A", "AGND")
            x, y: Label position (snapped to grid unless snap=False)
            shape: Signal type shape (input, output, bidirectional, tri_state, passive)
            rotation: Rotation in degrees
            snap: Whether to apply grid snapping (default: True)
            validate_connection: Whether to warn if label is not on a wire (default: True)

        Returns:
            The GlobalLabel created

        Warning:
            If validate_connection is True and the label position is not on any wire,
            a warning will be issued. This helps catch disconnected labels that would
            cause ERC errors in KiCad.

        Example:
            # Add global labels for power rails
            sch.add_global_label("VCC_3V3", 100, 50, shape="input")
            sch.add_global_label("GND", 100, 100, shape="input")

            # Add domain-specific power labels
            sch.add_global_label("VCC_3V3A", 100, 50, shape="input")  # Analog
            sch.add_global_label("AGND", 100, 100, shape="passive")   # Analog ground

            # Add global label for I2C bus
            sch.add_global_label("I2C_SDA", 200, 50, shape="bidirectional")
        """
        if snap:
            x = self._snap_coord(x, f"global_label {text}")
            y = self._snap_coord(y, f"global_label {text}")
        else:
            x = round(x, 2)
            y = round(y, 2)

        # Validate that the label is on a wire
        if validate_connection and self.wires:
            if not self._point_on_any_wire(x, y):
                nearest_point, distance = self._find_nearest_wire_point(x, y)
                if nearest_point:
                    warnings.warn(
                        f"Global label '{text}' at ({x}, {y}) is not on any wire. "
                        f"Nearest wire point is ({nearest_point[0]:.2f}, {nearest_point[1]:.2f}), "
                        f"{distance:.2f}mm away. This will cause ERC errors.",
                        stacklevel=2,
                    )

        gl = GlobalLabel(text=text, x=x, y=y, shape=shape, rotation=rotation)
        self.global_labels.append(gl)
        _log_info(f"Added global label '{text}' at ({x}, {y})")
        return gl

    def add_text(self, text: str, x: float, y: float, snap: bool = True):
        """Add a text note.

        Args:
            text: Note text
            x, y: Text position (snapped to grid unless snap=False)
            snap: Whether to apply grid snapping (default: True)
        """
        if snap:
            x = self._snap_coord(x, "text")
            y = self._snap_coord(y, "text")
        self.text_notes.append((text, x, y))
