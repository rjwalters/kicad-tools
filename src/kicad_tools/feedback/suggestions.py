"""Fix suggestions for common DRC/ERC errors.

Provides intelligent suggestions to guide agents toward solutions for common
design rule and electrical rule violations.

Example::

    from kicad_tools.feedback import FixSuggestionGenerator
    from kicad_tools.drc.violation import DRCViolation, ViolationType

    generator = FixSuggestionGenerator()
    suggestions = generator.suggest(violation)
    # ['Move component to increase clearance to 0.20mm',
    #  'Reroute net GND around the obstruction']
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.drc.violation import DRCViolation
    from kicad_tools.erc.violation import ERCViolation


class FixSuggestionGenerator:
    """Generate fix suggestions for common DRC/ERC errors.

    Provides actionable suggestions based on violation type and context.
    Suggestions are prioritized with the most likely effective fix first.
    """

    def suggest(self, violation: DRCViolation | ERCViolation) -> list[str]:
        """Generate suggestions for a violation.

        Args:
            violation: A DRC or ERC violation to analyze.

        Returns:
            List of actionable suggestions, ordered by likelihood of success.
        """
        # Import here to avoid circular imports
        from kicad_tools.drc.violation import DRCViolation
        from kicad_tools.erc.violation import ERCViolation

        if isinstance(violation, DRCViolation):
            return self._suggest_for_drc(violation)
        elif isinstance(violation, ERCViolation):
            return self._suggest_for_erc(violation)
        return []

    def _suggest_for_drc(self, violation: DRCViolation) -> list[str]:
        """Generate suggestions for DRC violations."""
        from kicad_tools.drc.violation import ViolationType

        handlers = {
            ViolationType.CLEARANCE: self._suggest_clearance,
            ViolationType.COPPER_EDGE_CLEARANCE: self._suggest_edge_clearance,
            ViolationType.COURTYARD_OVERLAP: self._suggest_courtyard,
            ViolationType.UNCONNECTED_ITEMS: self._suggest_unconnected,
            ViolationType.SHORTING_ITEMS: self._suggest_shorting,
            ViolationType.TRACK_WIDTH: self._suggest_track_width,
            ViolationType.VIA_ANNULAR_WIDTH: self._suggest_annular_ring,
            ViolationType.VIA_HOLE_LARGER_THAN_PAD: self._suggest_via_hole,
            ViolationType.DRILL_HOLE_TOO_SMALL: self._suggest_drill_hole,
            ViolationType.SILK_OVER_COPPER: self._suggest_silk_over_copper,
            ViolationType.SILK_OVERLAP: self._suggest_silk_overlap,
            ViolationType.SOLDER_MASK_BRIDGE: self._suggest_solder_mask,
            ViolationType.MISSING_FOOTPRINT: self._suggest_missing_footprint,
            ViolationType.DUPLICATE_FOOTPRINT: self._suggest_duplicate_footprint,
            ViolationType.EXTRA_FOOTPRINT: self._suggest_extra_footprint,
            ViolationType.MALFORMED_OUTLINE: self._suggest_malformed_outline,
            ViolationType.HOLE_NEAR_HOLE: self._suggest_hole_near_hole,
        }

        handler = handlers.get(violation.type)
        if handler:
            return handler(violation)

        # Generic fallback
        return self._suggest_generic_drc(violation)

    def _suggest_for_erc(self, violation: ERCViolation) -> list[str]:
        """Generate suggestions for ERC violations."""
        from kicad_tools.erc.violation import ERCViolationType

        handlers = {
            ERCViolationType.PIN_NOT_CONNECTED: self._suggest_pin_not_connected,
            ERCViolationType.PIN_NOT_DRIVEN: self._suggest_pin_not_driven,
            ERCViolationType.POWER_PIN_NOT_DRIVEN: self._suggest_power_not_driven,
            ERCViolationType.NO_CONNECT_CONNECTED: self._suggest_nc_connected,
            ERCViolationType.NO_CONNECT_DANGLING: self._suggest_nc_dangling,
            ERCViolationType.DUPLICATE_REFERENCE: self._suggest_duplicate_ref,
            ERCViolationType.LABEL_DANGLING: self._suggest_label_dangling,
            ERCViolationType.GLOBAL_LABEL_DANGLING: self._suggest_global_label_dangling,
            ERCViolationType.HIER_LABEL_MISMATCH: self._suggest_hier_label,
            ERCViolationType.WIRE_DANGLING: self._suggest_wire_dangling,
            ERCViolationType.MISSING_UNIT: self._suggest_missing_unit,
            ERCViolationType.UNANNOTATED: self._suggest_unannotated,
            ERCViolationType.SIMILAR_LABELS: self._suggest_similar_labels,
            ERCViolationType.ENDPOINT_OFF_GRID: self._suggest_off_grid,
            ERCViolationType.MULTIPLE_NET_NAMES: self._suggest_multiple_nets,
        }

        handler = handlers.get(violation.type)
        if handler:
            return handler(violation)

        return self._suggest_generic_erc(violation)

    # =========================================================================
    # DRC SUGGESTION HANDLERS
    # =========================================================================

    def _suggest_clearance(self, violation: DRCViolation) -> list[str]:
        """Suggest fixes for clearance violations."""
        suggestions = []

        # Calculate how much clearance is needed
        if violation.required_value_mm and violation.actual_value_mm:
            gap = violation.required_value_mm - violation.actual_value_mm
            suggestions.append(
                f"Move affected elements apart by at least {gap:.2f}mm to meet "
                f"clearance requirement of {violation.required_value_mm:.2f}mm"
            )

        # Check for specific element types in items
        involves_track = any(
            "track" in item.lower() or "segment" in item.lower() for item in violation.items
        )
        involves_via = any("via" in item.lower() for item in violation.items)
        involves_pad = any("pad" in item.lower() for item in violation.items)

        if involves_track:
            if violation.nets:
                net_name = (
                    violation.nets[0] if len(violation.nets) == 1 else "/".join(violation.nets)
                )
                suggestions.append(f"Reroute net '{net_name}' around the obstruction")
            else:
                suggestions.append("Reroute the track to increase spacing")

        if involves_via:
            suggestions.append("Move via to a different location or change to a smaller via size")
            suggestions.append("Consider using a blind or buried via if design permits")

        if involves_pad:
            suggestions.append("Adjust component placement to increase pad-to-pad spacing")
            suggestions.append("Check if a smaller footprint variant is available")

        # Generic suggestions
        if violation.nets and len(violation.nets) == 2:
            suggestions.append(
                f"Review net class rules for '{violation.nets[0]}' and '{violation.nets[1]}'"
            )

        if not suggestions:
            suggestions.append("Increase spacing between copper elements")
            suggestions.append("Check design rules in Board Setup > Design Rules")

        return suggestions

    def _suggest_edge_clearance(self, violation: DRCViolation) -> list[str]:
        """Suggest fixes for copper-to-edge clearance violations."""
        suggestions = []

        if violation.actual_value_mm and violation.required_value_mm:
            gap = violation.required_value_mm - violation.actual_value_mm
            suggestions.append(
                f"Move copper {gap:.2f}mm inward from board edge "
                f"(required: {violation.required_value_mm:.2f}mm)"
            )

        suggestions.extend(
            [
                "Move affected tracks/pads away from the board edge",
                "Extend the board outline if the current size is not critical",
                "Check Edge.Cuts layer for correct board boundary definition",
                "Verify manufacturer edge clearance requirements (typically 0.25-0.5mm)",
            ]
        )

        return suggestions

    def _suggest_courtyard(self, violation: DRCViolation) -> list[str]:
        """Suggest fixes for courtyard overlap violations."""
        suggestions = []

        # Extract component references from items
        components = [item for item in violation.items if any(c.isalpha() for c in item)]

        if len(components) >= 2:
            suggestions.append(f"Move {components[0]} or {components[1]} to eliminate overlap")

        suggestions.extend(
            [
                "Increase spacing between components to clear courtyards",
                "Check if components can be rotated to reduce overlap area",
                "Consider using tighter courtyard footprint variants if available",
                "Verify the courtyard outline is correctly defined in footprint editor",
            ]
        )

        return suggestions

    def _suggest_unconnected(self, violation: DRCViolation) -> list[str]:
        """Suggest fixes for unconnected items."""
        suggestions = []

        if violation.nets:
            net_name = violation.nets[0]
            suggestions.append(f"Add wire/trace to connect to net '{net_name}'")
            suggestions.append(f"Route the incomplete connection for net '{net_name}'")

        # Check for specific elements
        if any("pad" in item.lower() for item in violation.items):
            suggestions.append("Verify pad is correctly assigned in schematic")
            suggestions.append("Check symbol-to-footprint pin mapping")

        if any("zone" in item.lower() for item in violation.items):
            suggestions.append("Refill copper zones to regenerate connections")
            suggestions.append("Check zone priority and clearance settings")

        suggestions.extend(
            [
                "Run the autorouter for incomplete connections",
                "Verify netlist is up-to-date (update PCB from schematic)",
                "Check for broken or missing net ties",
            ]
        )

        return suggestions

    def _suggest_shorting(self, violation: DRCViolation) -> list[str]:
        """Suggest fixes for shorting items."""
        suggestions = []

        if len(violation.nets) >= 2:
            net1, net2 = violation.nets[0], violation.nets[1]
            suggestions.append(f"Remove the connection between '{net1}' and '{net2}'")
            suggestions.append(
                f"Reroute one of the traces for '{net1}' or '{net2}' to eliminate overlap"
            )

        suggestions.extend(
            [
                "Delete the offending trace segment or via causing the short",
                "Move the conflicting copper elements to different layers",
                "Check for accidental copper pour connections",
                "Verify zone settings and net assignments",
            ]
        )

        return suggestions

    def _suggest_track_width(self, violation: DRCViolation) -> list[str]:
        """Suggest fixes for track width violations."""
        suggestions = []

        if violation.required_value_mm and violation.actual_value_mm:
            suggestions.append(
                f"Widen track from {violation.actual_value_mm:.3f}mm to at least "
                f"{violation.required_value_mm:.3f}mm"
            )

        if violation.nets:
            net_name = violation.nets[0]
            suggestions.append(f"Update net class for '{net_name}' to set proper track width")

        suggestions.extend(
            [
                "Check design rules in Board Setup > Design Rules > Net Classes",
                "Edit track properties to increase width (select and press 'E')",
                "Consider using a different net class for power/signal nets",
                "Verify manufacturer minimum track width capability",
            ]
        )

        return suggestions

    def _suggest_annular_ring(self, violation: DRCViolation) -> list[str]:
        """Suggest fixes for via annular ring violations."""
        suggestions = []

        if violation.actual_value_mm and violation.required_value_mm:
            diff = violation.required_value_mm - violation.actual_value_mm
            suggestions.append(
                f"Increase via pad size by {diff * 2:.3f}mm to meet annular ring requirement"
            )

        suggestions.extend(
            [
                "Use a larger via pad size in Board Setup > Design Rules > Via",
                "Use a smaller drill size while keeping the same pad size",
                "Check manufacturer minimum annular ring requirement (typically 0.1-0.15mm)",
                "Switch to a different via size preset that meets requirements",
            ]
        )

        return suggestions

    def _suggest_via_hole(self, violation: DRCViolation) -> list[str]:
        """Suggest fixes for via hole larger than pad violations."""
        return [
            "Increase via pad diameter to accommodate the drill size",
            "Use a smaller drill size for the via",
            "Review via definitions in Board Setup > Design Rules",
            "Check if blind/buried via settings are correct",
        ]

    def _suggest_drill_hole(self, violation: DRCViolation) -> list[str]:
        """Suggest fixes for drill hole too small violations."""
        suggestions = []

        if violation.actual_value_mm and violation.required_value_mm:
            suggestions.append(
                f"Increase drill size from {violation.actual_value_mm:.3f}mm to at least "
                f"{violation.required_value_mm:.3f}mm"
            )

        suggestions.extend(
            [
                "Check manufacturer minimum drill size capability",
                "Update via or pad drill settings in Board Setup",
                "Consider using a different manufacturer with smaller drill capability",
                "For vias, use laser-drilled microvias if available",
            ]
        )

        return suggestions

    def _suggest_silk_over_copper(self, violation: DRCViolation) -> list[str]:
        """Suggest fixes for silkscreen over copper violations."""
        return [
            "Move silkscreen text/graphics away from exposed copper",
            "Shrink silkscreen text size to fit within available space",
            "Delete unnecessary silkscreen elements over pads",
            "Check footprint silkscreen layer in footprint editor",
            "Enable 'Clip silkscreen' option in Board Setup if available",
        ]

    def _suggest_silk_overlap(self, violation: DRCViolation) -> list[str]:
        """Suggest fixes for silkscreen overlap violations."""
        return [
            "Move overlapping silkscreen elements apart",
            "Reduce silkscreen text size",
            "Remove redundant silkscreen graphics",
            "Adjust reference designator positions in footprints",
            "Use smaller font for component values if needed",
        ]

    def _suggest_solder_mask(self, violation: DRCViolation) -> list[str]:
        """Suggest fixes for solder mask bridge violations."""
        suggestions = []

        if violation.actual_value_mm and violation.required_value_mm:
            suggestions.append(
                f"Increase solder mask expansion or spacing to at least "
                f"{violation.required_value_mm:.3f}mm"
            )

        suggestions.extend(
            [
                "Adjust solder mask clearance in Board Setup > Board Stackup",
                "Reduce pad sizes if electrically acceptable",
                "Increase spacing between pads/tracks",
                "Check manufacturer solder mask bridge capability (typically 0.1mm)",
            ]
        )

        return suggestions

    def _suggest_missing_footprint(self, violation: DRCViolation) -> list[str]:
        """Suggest fixes for missing footprint violations."""
        return [
            "Assign footprint to symbol in schematic (press 'E' to edit)",
            "Check library path configuration in Preferences > Manage Libraries",
            "Verify footprint name matches library entry",
            "Update PCB from schematic to sync footprint assignments",
            "Create missing footprint in Footprint Editor if needed",
        ]

    def _suggest_duplicate_footprint(self, violation: DRCViolation) -> list[str]:
        """Suggest fixes for duplicate footprint violations."""
        return [
            "Delete the duplicate footprint instance",
            "Update PCB from schematic to resolve duplicates",
            "Check for accidental copy-paste in PCB layout",
            "Verify component references are unique in schematic",
        ]

    def _suggest_extra_footprint(self, violation: DRCViolation) -> list[str]:
        """Suggest fixes for extra footprint violations."""
        return [
            "Delete the extra footprint not present in schematic",
            "If intentional, add corresponding symbol to schematic",
            "Update PCB from schematic to sync component list",
            "Check for PCB-only components that need annotation",
        ]

    def _suggest_malformed_outline(self, violation: DRCViolation) -> list[str]:
        """Suggest fixes for malformed board outline violations."""
        return [
            "Ensure board outline on Edge.Cuts layer forms a closed polygon",
            "Check for gaps or overlaps in board outline segments",
            "Use 'Board Setup > Board Outline' tools to validate",
            "Verify arc segments connect properly to line segments",
            "Remove duplicate or stacked outline segments",
        ]

    def _suggest_hole_near_hole(self, violation: DRCViolation) -> list[str]:
        """Suggest fixes for hole-to-hole spacing violations."""
        suggestions = []

        if violation.actual_value_mm and violation.required_value_mm:
            gap = violation.required_value_mm - violation.actual_value_mm
            suggestions.append(
                f"Move holes apart by at least {gap:.2f}mm to meet spacing requirement"
            )

        suggestions.extend(
            [
                "Relocate vias or mounting holes to increase spacing",
                "Use smaller drill sizes if design permits",
                "Check manufacturer hole-to-hole spacing requirements",
                "Consider using slot instead of multiple close holes",
            ]
        )

        return suggestions

    def _suggest_generic_drc(self, violation: DRCViolation) -> list[str]:
        """Generic suggestions for unknown DRC violation types."""
        suggestions = [
            f"Review '{violation.type_str}' violation details and affected elements",
            "Check design rules in Board Setup > Design Rules",
            "Consult manufacturer DFM guidelines for this violation type",
        ]

        if violation.nets:
            suggestions.append(
                f"Check net class settings for affected nets: {', '.join(violation.nets)}"
            )

        if violation.locations:
            loc = violation.locations[0]
            suggestions.append(f"Inspect area around ({loc.x_mm:.2f}, {loc.y_mm:.2f})mm")

        return suggestions

    # =========================================================================
    # ERC SUGGESTION HANDLERS
    # =========================================================================

    def _suggest_pin_not_connected(self, violation: ERCViolation) -> list[str]:
        """Suggest fixes for unconnected pin errors."""
        suggestions = []

        # Extract component/pin from items or description
        if violation.items:
            for item in violation.items:
                if "pin" in item.lower():
                    suggestions.append(f"Add wire to connect {item}")
                    break

        suggestions.extend(
            [
                "Connect the pin to the appropriate net",
                "Add a No-Connect (X) flag if the pin is intentionally unconnected",
                "Check if the symbol pin configuration matches the datasheet",
                "Verify the wire endpoint connects to the pin",
            ]
        )

        return suggestions

    def _suggest_pin_not_driven(self, violation: ERCViolation) -> list[str]:
        """Suggest fixes for input pin not driven errors."""
        return [
            "Connect an output or bidirectional pin to drive this input",
            "Add a pull-up or pull-down resistor if floating is acceptable",
            "Connect to a power symbol if this is a power input",
            "Check symbol pin electrical type configuration",
            "Add explicit driver source (buffer, logic gate output, etc.)",
        ]

    def _suggest_power_not_driven(self, violation: ERCViolation) -> list[str]:
        """Suggest fixes for power input not driven errors."""
        return [
            "Connect a power symbol (VCC, GND, +3V3, etc.) to drive the power pin",
            "Add a power flag symbol if power is supplied from the PCB",
            "Check that power symbol net names match component power pins",
            "Verify power pin electrical type in symbol editor",
            "Add PWR_FLAG symbol to indicate external power source",
        ]

    def _suggest_nc_connected(self, violation: ERCViolation) -> list[str]:
        """Suggest fixes for no-connect flag connected errors."""
        return [
            "Remove the wire connected to the no-connect pin",
            "Remove the no-connect flag if connection is intentional",
            "Check if the symbol pin should be a different type",
            "Verify schematic intent for this connection",
        ]

    def _suggest_nc_dangling(self, violation: ERCViolation) -> list[str]:
        """Suggest fixes for dangling no-connect flag errors."""
        return [
            "Move the no-connect flag to connect directly to the unconnected pin",
            "Delete the no-connect flag if not needed",
            "Ensure the no-connect flag is on the pin endpoint",
            "Check for overlapping wires that may cause misalignment",
        ]

    def _suggest_duplicate_ref(self, violation: ERCViolation) -> list[str]:
        """Suggest fixes for duplicate reference designator errors."""
        return [
            "Run 'Annotate Schematic' to reassign unique references",
            "Manually edit one component's reference to be unique",
            "Check for copy-paste errors that duplicated components",
            "Verify multi-unit symbols have correct unit assignments",
        ]

    def _suggest_label_dangling(self, violation: ERCViolation) -> list[str]:
        """Suggest fixes for dangling label errors."""
        return [
            "Connect a wire to the label",
            "Delete the unused label",
            "Move the label to connect to a wire endpoint",
            "Check for invisible wire or junction at the label position",
        ]

    def _suggest_global_label_dangling(self, violation: ERCViolation) -> list[str]:
        """Suggest fixes for dangling global label errors."""
        return [
            "Connect a wire to the global label",
            "Verify the global label is used on at least one other sheet",
            "Delete the global label if it's not needed",
            "Check global label spelling matches other sheets",
        ]

    def _suggest_hier_label(self, violation: ERCViolation) -> list[str]:
        """Suggest fixes for hierarchical label mismatch errors."""
        return [
            "Ensure hierarchical labels match corresponding sheet pins",
            "Check spelling and case of hierarchical label names",
            "Add missing hierarchical labels or sheet pins",
            "Delete orphaned hierarchical labels/pins",
            "Verify hierarchical sheet symbol connections",
        ]

    def _suggest_wire_dangling(self, violation: ERCViolation) -> list[str]:
        """Suggest fixes for dangling wire errors."""
        return [
            "Extend the wire to connect to a pin or junction",
            "Delete the dangling wire segment",
            "Add a no-connect flag if intentionally unconnected",
            "Check for nearly-connected wire endpoints (snap to grid)",
            "Merge the wire with an adjacent wire segment",
        ]

    def _suggest_missing_unit(self, violation: ERCViolation) -> list[str]:
        """Suggest fixes for missing unit errors."""
        return [
            "Add the missing unit of the multi-unit symbol",
            "Check if all units are required for your design",
            "Use 'Add Symbol' to add remaining units (same component)",
            "Verify symbol definition includes all required units",
        ]

    def _suggest_unannotated(self, violation: ERCViolation) -> list[str]:
        """Suggest fixes for unannotated symbol errors."""
        return [
            "Run 'Annotate Schematic' from Tools menu",
            "Manually enter a reference designator for the symbol",
            "Check annotation settings (start number, prefix)",
            "Verify the symbol has a Reference field",
        ]

    def _suggest_similar_labels(self, violation: ERCViolation) -> list[str]:
        """Suggest fixes for similar labels errors."""
        return [
            "Verify label names are intentionally different (not a typo)",
            "Rename one label to match if they should be the same net",
            "Add distinguishing characters if labels should differ",
            "Check for case sensitivity issues in label names",
        ]

    def _suggest_off_grid(self, violation: ERCViolation) -> list[str]:
        """Suggest fixes for off-grid endpoint errors."""
        return [
            "Move the wire endpoint to snap to the grid",
            "Use 'Edit > Cleanup Graphics' to fix off-grid issues",
            "Check symbol pin positions for off-grid placement",
            "Adjust grid settings if using non-standard grid",
        ]

    def _suggest_multiple_nets(self, violation: ERCViolation) -> list[str]:
        """Suggest fixes for multiple net names on wire errors."""
        return [
            "Remove duplicate labels leaving only one net name",
            "Add a net tie symbol if nets should be connected",
            "Check for overlapping labels at the same position",
            "Verify hierarchical connections don't create conflicts",
        ]

    def _suggest_generic_erc(self, violation: ERCViolation) -> list[str]:
        """Generic suggestions for unknown ERC violation types."""
        suggestions = [
            f"Review '{violation.type_str}' violation in schematic",
            "Check ERC settings in Schematic Setup > Electrical Rules",
        ]

        if violation.sheet:
            suggestions.append(f"Navigate to sheet '{violation.sheet}' to inspect the issue")

        if violation.pos_x or violation.pos_y:
            suggestions.append(
                f"Inspect area around ({violation.pos_x:.1f}, {violation.pos_y:.1f})"
            )

        return suggestions


def generate_drc_suggestions(violation: DRCViolation) -> list[str]:
    """Convenience function to generate DRC suggestions.

    Args:
        violation: A DRC violation to analyze.

    Returns:
        List of actionable suggestions.
    """
    return FixSuggestionGenerator().suggest(violation)


def generate_erc_suggestions(violation: ERCViolation) -> list[str]:
    """Convenience function to generate ERC suggestions.

    Args:
        violation: An ERC violation to analyze.

    Returns:
        List of actionable suggestions.
    """
    return FixSuggestionGenerator().suggest(violation)
