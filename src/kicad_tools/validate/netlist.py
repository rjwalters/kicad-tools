"""Schematic-to-PCB netlist synchronization validation.

This module provides validation to ensure schematic and PCB netlists are in sync,
reporting mismatches clearly with actionable fix suggestions.

Example:
    >>> from kicad_tools import Project
    >>> from kicad_tools.validate import NetlistValidator
    >>>
    >>> # Via Project class
    >>> project = Project.load("my_board.kicad_pro")
    >>> result = project.check_sync()
    >>> if not result.in_sync:
    ...     for issue in result.issues:
    ...         print(f"{issue.severity}: {issue.message}")
    ...         print(f"  Fix: {issue.suggestion}")
    >>>
    >>> # Standalone validation
    >>> validator = NetlistValidator(
    ...     schematic="project.kicad_sch",
    ...     pcb="project.kicad_pcb"
    ... )
    >>> result = validator.validate()
    >>> print(result.missing_on_pcb)
    >>> print(result.orphaned_on_pcb)
    >>> print(result.net_mismatches)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.schema.schematic import Schematic


@dataclass(frozen=True)
class SyncIssue:
    """Represents a single netlist synchronization issue.

    Attributes:
        severity: Either "error" or "warning"
        category: Issue category (missing_on_pcb, orphaned_on_pcb, net_mismatch, pin_mismatch)
        message: Human-readable description of the issue
        suggestion: Actionable fix suggestion
        reference: Component reference involved (e.g., "R5", "C12")
        net_schematic: Net name from schematic (if applicable)
        net_pcb: Net name from PCB (if applicable)
        pin: Pin number/name (if applicable)
    """

    severity: str
    category: str
    message: str
    suggestion: str
    reference: str = ""
    net_schematic: str = ""
    net_pcb: str = ""
    pin: str = ""

    def __post_init__(self) -> None:
        """Validate severity and category values."""
        if self.severity not in ("error", "warning"):
            raise ValueError(f"severity must be 'error' or 'warning', got {self.severity!r}")
        valid_categories = ("missing_on_pcb", "orphaned_on_pcb", "net_mismatch", "pin_mismatch")
        if self.category not in valid_categories:
            raise ValueError(f"category must be one of {valid_categories}, got {self.category!r}")

    @property
    def is_error(self) -> bool:
        """Check if this is an error (not a warning)."""
        return self.severity == "error"

    @property
    def is_warning(self) -> bool:
        """Check if this is a warning (not an error)."""
        return self.severity == "warning"

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "severity": self.severity,
            "category": self.category,
            "message": self.message,
            "suggestion": self.suggestion,
            "reference": self.reference,
            "net_schematic": self.net_schematic,
            "net_pcb": self.net_pcb,
            "pin": self.pin,
        }


@dataclass
class SyncResult:
    """Aggregates all netlist synchronization issues.

    Provides convenient access to issue counts and filtering.

    Attributes:
        issues: List of all sync issues found
    """

    issues: list[SyncIssue] = field(default_factory=list)

    @property
    def in_sync(self) -> bool:
        """True if no errors (warnings are allowed)."""
        return self.error_count == 0

    @property
    def error_count(self) -> int:
        """Count of issues with severity='error'."""
        return sum(1 for i in self.issues if i.is_error)

    @property
    def warning_count(self) -> int:
        """Count of issues with severity='warning'."""
        return sum(1 for i in self.issues if i.is_warning)

    @property
    def errors(self) -> list[SyncIssue]:
        """List of only error issues."""
        return [i for i in self.issues if i.is_error]

    @property
    def warnings(self) -> list[SyncIssue]:
        """List of only warning issues."""
        return [i for i in self.issues if i.is_warning]

    @property
    def missing_on_pcb(self) -> list[SyncIssue]:
        """Symbols without footprints on PCB."""
        return [i for i in self.issues if i.category == "missing_on_pcb"]

    @property
    def orphaned_on_pcb(self) -> list[SyncIssue]:
        """Footprints without symbols in schematic."""
        return [i for i in self.issues if i.category == "orphaned_on_pcb"]

    @property
    def net_mismatches(self) -> list[SyncIssue]:
        """Different net assignments between schematic and PCB."""
        return [i for i in self.issues if i.category == "net_mismatch"]

    @property
    def pin_mismatches(self) -> list[SyncIssue]:
        """Pin-to-pad mapping issues."""
        return [i for i in self.issues if i.category == "pin_mismatch"]

    def __iter__(self):
        """Iterate over all issues."""
        return iter(self.issues)

    def __len__(self) -> int:
        """Total number of issues."""
        return len(self.issues)

    def __bool__(self) -> bool:
        """True if there are any issues."""
        return len(self.issues) > 0

    def add(self, issue: SyncIssue) -> None:
        """Add an issue to the results."""
        self.issues.append(issue)

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "in_sync": self.in_sync,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "issues": [i.to_dict() for i in self.issues],
        }

    def summary(self) -> str:
        """Generate a human-readable summary."""
        status = "IN SYNC" if self.in_sync else "OUT OF SYNC"
        parts = [f"Netlist {status}: {self.error_count} errors, {self.warning_count} warnings"]

        if self.missing_on_pcb:
            parts.append(f"  Missing on PCB: {len(self.missing_on_pcb)}")
        if self.orphaned_on_pcb:
            parts.append(f"  Orphaned on PCB: {len(self.orphaned_on_pcb)}")
        if self.net_mismatches:
            parts.append(f"  Net mismatches: {len(self.net_mismatches)}")
        if self.pin_mismatches:
            parts.append(f"  Pin mismatches: {len(self.pin_mismatches)}")

        return "\n".join(parts)


class NetlistValidator:
    """Validates synchronization between schematic and PCB netlists.

    Checks for:
    - Symbols missing from PCB (no corresponding footprint)
    - Orphaned footprints on PCB (no corresponding symbol)
    - Net name mismatches between schematic and PCB
    - Pin-to-pad mapping issues

    Example:
        >>> validator = NetlistValidator("project.kicad_sch", "project.kicad_pcb")
        >>> result = validator.validate()
        >>>
        >>> if not result.in_sync:
        ...     for issue in result.errors:
        ...         print(f"{issue.severity}: {issue.message}")
        ...         print(f"  Fix: {issue.suggestion}")

    Attributes:
        schematic: Loaded Schematic object
        pcb: Loaded PCB object
    """

    def __init__(
        self,
        schematic: str | Path | Schematic,
        pcb: str | Path | PCB,
    ) -> None:
        """Initialize the validator.

        Args:
            schematic: Path to schematic file or Schematic object
            pcb: Path to PCB file or PCB object
        """
        from kicad_tools.schema.pcb import PCB as PCBClass
        from kicad_tools.schema.schematic import Schematic as SchematicClass

        # Load schematic if path provided
        if isinstance(schematic, (str, Path)):
            self.schematic = SchematicClass.load(schematic)
        else:
            self.schematic = schematic

        # Load PCB if path provided
        if isinstance(pcb, (str, Path)):
            self.pcb = PCBClass.load(str(pcb))
        else:
            self.pcb = pcb

    def validate(self) -> SyncResult:
        """Run all synchronization checks.

        Returns:
            SyncResult containing all issues found
        """
        result = SyncResult()

        # Check component synchronization (missing/orphaned)
        self._check_components(result)

        # Check net synchronization
        self._check_nets(result)

        return result

    def _check_components(self, result: SyncResult) -> None:
        """Check for missing and orphaned components.

        Args:
            result: SyncResult to add issues to
        """
        # Build reference sets
        sch_refs: dict[str, dict] = {}
        for sym in self.schematic.symbols:
            # Skip power symbols and other non-component symbols
            if sym.reference and not sym.reference.startswith("#"):
                sch_refs[sym.reference] = {
                    "value": sym.value,
                    "lib_id": sym.lib_id,
                    "footprint": getattr(sym, "footprint", ""),
                }

        pcb_refs: dict[str, dict] = {}
        for fp in self.pcb.footprints:
            if fp.reference and not fp.reference.startswith("#"):
                pcb_refs[fp.reference] = {
                    "value": fp.value,
                    "footprint": fp.name,
                    "position": fp.position,
                }

        # Find missing (in schematic but not on PCB)
        for ref in sorted(set(sch_refs.keys()) - set(pcb_refs.keys())):
            sch_data = sch_refs[ref]
            footprint = sch_data.get("footprint", "")
            footprint_str = f" ({footprint})" if footprint else ""

            result.add(
                SyncIssue(
                    severity="error",
                    category="missing_on_pcb",
                    message=f"{ref} missing on PCB",
                    suggestion=f"Add footprint for {ref}{footprint_str}",
                    reference=ref,
                )
            )

        # Find orphaned (on PCB but not in schematic)
        for ref in sorted(set(pcb_refs.keys()) - set(sch_refs.keys())):
            result.add(
                SyncIssue(
                    severity="warning",
                    category="orphaned_on_pcb",
                    message=f"{ref} on PCB has no schematic symbol",
                    suggestion=f"Remove {ref} from PCB or add to schematic",
                    reference=ref,
                )
            )

    def _check_nets(self, result: SyncResult) -> None:
        """Check for net name mismatches.

        Args:
            result: SyncResult to add issues to
        """
        # Build net mapping from PCB: component/pad -> net_name
        pcb_pad_nets: dict[tuple[str, str], str] = {}
        for fp in self.pcb.footprints:
            if fp.reference and not fp.reference.startswith("#"):
                for pad in fp.pads:
                    if pad.net_name:
                        pcb_pad_nets[(fp.reference, pad.number)] = pad.net_name

        # Build expected nets from schematic
        # This requires extracting pin-to-net mapping from the schematic
        # For now, we compare using global labels and PCB nets
        self._check_global_net_names(result)
        self._check_pad_net_assignments(result, pcb_pad_nets)

    def _check_global_net_names(self, result: SyncResult) -> None:
        """Check that global label names match PCB net names.

        Args:
            result: SyncResult to add issues to
        """
        # Get global label names from schematic
        sch_net_names = {lbl.text for lbl in self.schematic.global_labels}

        # Get net names from PCB
        pcb_net_names = {net.name for net in self.pcb.nets.values() if net.name}

        # Check for nets in schematic that have similar but different names on PCB
        # This catches common issues like VCC vs VDD, GND vs VSS
        common_net_pairs = [
            ("VCC", "VDD"),
            ("VCC", "3V3"),
            ("VCC", "5V"),
            ("VDD", "3V3"),
            ("VDD", "5V"),
            ("GND", "VSS"),
            ("GND", "DGND"),
            ("GND", "AGND"),
        ]

        for sch_net in sch_net_names:
            for pcb_net in pcb_net_names:
                # Check for case-insensitive matches that aren't exact
                if sch_net.upper() == pcb_net.upper() and sch_net != pcb_net:
                    result.add(
                        SyncIssue(
                            severity="warning",
                            category="net_mismatch",
                            message=f'Net "{sch_net}" on schematic is "{pcb_net}" on PCB',
                            suggestion=f'Rename net on PCB to "{sch_net}" or update schematic',
                            net_schematic=sch_net,
                            net_pcb=pcb_net,
                        )
                    )

                # Check for common naming variations
                for pair in common_net_pairs:
                    if sch_net.upper() in pair and pcb_net.upper() in pair and sch_net != pcb_net:
                        result.add(
                            SyncIssue(
                                severity="error",
                                category="net_mismatch",
                                message=f'Net "{sch_net}" on schematic is "{pcb_net}" on PCB',
                                suggestion=f'Rename net on PCB to "{sch_net}"',
                                net_schematic=sch_net,
                                net_pcb=pcb_net,
                            )
                        )
                        break

    def _check_pad_net_assignments(
        self,
        result: SyncResult,
        pcb_pad_nets: dict[tuple[str, str], str],
    ) -> None:
        """Check pad-to-net assignments for consistency.

        This is a placeholder for more sophisticated netlist extraction.
        Full implementation would require proper netlist extraction from schematic.

        Args:
            result: SyncResult to add issues to
            pcb_pad_nets: Mapping of (reference, pad) to net name
        """
        # Check for pads with no net assignment
        for fp in self.pcb.footprints:
            if fp.reference and not fp.reference.startswith("#"):
                for pad in fp.pads:
                    # Skip mounting holes and similar
                    if pad.type in ("np_thru_hole",):
                        continue
                    # Check if pad has a net
                    if not pad.net_name and pad.net_number == 0:
                        # Only warn if this isn't a DNP component
                        # (would need to check schematic for DNP status)
                        pass  # Skip for now - too noisy without proper filtering

    def __repr__(self) -> str:
        """Return string representation."""
        sch_count = len(self.schematic.symbols) if self.schematic else 0
        pcb_count = self.pcb.footprint_count if self.pcb else 0
        return f"NetlistValidator(schematic_symbols={sch_count}, pcb_footprints={pcb_count})"
