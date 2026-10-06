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

import warnings
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


def _same_net_spelling(sch_net: str, pcb_net: str) -> bool:
    """True when ``pcb_net`` plausibly names the same net as ``sch_net``.

    Used only as a tie-breaker when pairing nets by connectivity, never as
    the test of whether a pad is correct.  Accepts exact equality and the
    sheet-path spellings KiCad and kicad-tools produce for one local net
    (``/SENSE``, ``SENSE``, ``/MCU/SENSE``, ``MCU/SENSE``).
    """
    if sch_net == pcb_net:
        return True
    s = sch_net.lstrip("/")
    p = pcb_net.lstrip("/")
    return s == p or s.rsplit("/", 1)[-1] == p.rsplit("/", 1)[-1]


class NetlistValidator:
    """Validates synchronization between schematic and PCB netlists.

    Checks for:
    - Symbols missing from PCB (no corresponding footprint)
    - Orphaned footprints on PCB (no corresponding symbol)
    - Net name mismatches between schematic and PCB
    - Pads on a different net than their schematic pin (compared by
      connectivity, so net renaming alone is not drift)

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
            schematic: Path to schematic file or Schematic object.
                A path is strongly preferred -- it enables hierarchical
                BOM extraction so sub-sheet symbols are included. Passing
                a loaded :class:`Schematic` object falls back to root-sheet
                enumeration and emits a runtime warning on hierarchical
                projects (see :issue:`2625`).
            pcb: Path to PCB file or PCB object
        """
        from kicad_tools.schema.pcb import PCB as PCBClass
        from kicad_tools.schema.schematic import Schematic as SchematicClass

        # Track the schematic path so _check_components can use
        # extract_bom(..., hierarchical=True). When the caller passes a
        # Schematic object directly we cannot walk sub-sheets.
        self._schematic_path: str | None = None

        # Load schematic if path provided
        if isinstance(schematic, (str, Path)):
            self._schematic_path = str(schematic)
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

    def _collect_schematic_refs(self) -> dict[str, dict]:
        """Build {reference: {value, lib_id, footprint}} for the schematic.

        Uses :func:`kicad_tools.schema.bom.extract_bom` with
        ``hierarchical=True`` when a schematic path is known so that
        symbols placed inside sub-sheets are included. When the validator
        was constructed with a loaded :class:`Schematic` object (no path),
        falls back to the legacy root-sheet-only enumeration and emits a
        runtime warning so hierarchical bugs are surfaced rather than
        silently producing false positives (see issue #2625).
        """
        sch_refs: dict[str, dict] = {}

        if self._schematic_path:
            from kicad_tools.schema.bom import extract_bom

            bom = extract_bom(self._schematic_path, hierarchical=True)
            for item in bom.items:
                # Skip power symbols (their references start with "#").
                if item.is_power_symbol:
                    continue
                # Skip components that are not placed on the PCB.
                if not item.on_board:
                    continue
                if item.reference and not item.reference.startswith("#"):
                    sch_refs[item.reference] = {
                        "value": item.value,
                        "lib_id": item.lib_id,
                        "footprint": item.footprint,
                    }
            return sch_refs

        # Fallback: no path available, can only enumerate the root sheet.
        if self._schematic_has_sheets():
            warnings.warn(
                "NetlistValidator was constructed with a Schematic object "
                "instead of a path; hierarchical sub-sheets will be skipped "
                "and PCB footprints from those sheets will appear as "
                "orphans. Pass a schematic path to enable hierarchical "
                "traversal.",
                RuntimeWarning,
                stacklevel=3,
            )

        for sym in self.schematic.symbols:
            if sym.reference and not sym.reference.startswith("#"):
                sch_refs[sym.reference] = {
                    "value": sym.value,
                    "lib_id": sym.lib_id,
                    "footprint": getattr(sym, "footprint", ""),
                }
        return sch_refs

    def _schematic_has_sheets(self) -> bool:
        """Return True if the loaded schematic contains sub-sheet references."""
        sheets = getattr(self.schematic, "sheets", None)
        return bool(sheets)

    def _check_components(self, result: SyncResult) -> None:
        """Check for missing and orphaned components.

        Args:
            result: SyncResult to add issues to
        """
        # Build reference sets from the schematic. When we have a path,
        # walk all sub-sheets via extract_bom() so hierarchical projects
        # don't report every footprint as orphaned (issue #2625). When
        # constructed with a Schematic object, fall back to root-sheet
        # enumeration and warn the caller.
        sch_refs: dict[str, dict] = self._collect_schematic_refs()

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
        """Check net names and per-pad net connectivity.

        Args:
            result: SyncResult to add issues to
        """
        self._check_global_net_names(result)
        self._check_pad_net_assignments(result, self._pcb_pad_nets())

    def _pcb_pad_nets(self) -> dict[tuple[str, str], str | None]:
        """Build ``{(ref, pad) -> net | None}`` from the PCB.

        The empty net-0 name and KiCad's explicit no-connect sentinel
        ``unconnected-(<REF>-<PIN>-Pad<PAD>)`` naming *this* pad both mean
        "not connected" and normalize to ``None``.  Non-plated holes are
        skipped: they carry no copper and can never be on a net.
        """
        pcb_pad_nets: dict[tuple[str, str], str | None] = {}
        for fp in self.pcb.footprints:
            ref = fp.reference
            if not ref or ref.startswith("#"):
                continue
            for pad in fp.pads:
                if pad.type == "np_thru_hole" or not pad.number:
                    continue
                name: str | None = pad.net_name or None
                if (
                    name is not None
                    and name.startswith(f"unconnected-({ref}-")
                    and name.endswith(f"-Pad{pad.number})")
                ):
                    name = None
                key = (ref, pad.number)
                # A footprint may repeat a pad number (e.g. a thermal pad
                # split into several copper shapes).  Keep any real binding
                # rather than letting an unbound duplicate erase it.
                if name is not None or key not in pcb_pad_nets:
                    pcb_pad_nets[key] = name
        return pcb_pad_nets

    def _schematic_pin_nets(self) -> dict[tuple[str, str], str | None] | None:
        """Build ``{(ref, pin) -> net | None}`` from the schematic hierarchy.

        Reuses the LVS extraction
        (:func:`kicad_tools.lvs.board_lvs._schematic_pin_to_net`), which
        walks every sub-sheet and returns one canonical identity per
        connected component.  Returns ``None`` when no schematic file path
        is available (an unsaved in-memory :class:`Schematic`), in which
        case the pad check cannot run.
        """
        from kicad_tools.lvs.board_lvs import _schematic_pin_to_net

        sch_path = self._schematic_path
        if sch_path is None:
            path = getattr(self.schematic, "path", None)
            sch_path = str(path) if path else None
        if sch_path is None or not Path(sch_path).exists():
            return None
        return {
            key: net
            for key, net in _schematic_pin_to_net(Path(sch_path)).items()
            if key[0] and not key[0].startswith("#")
        }

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
        pcb_pad_nets: dict[tuple[str, str], str | None],
    ) -> None:
        """Check that every pad sits on the net the schematic puts its pin on.

        The comparison is by **connectivity, not by net name** (issue
        #5937).  Net names legitimately differ between the two documents --
        KiCad qualifies a local label as ``/SENSE`` while a generated board
        may carry ``SENSE``, and unnamed nets get tool-invented
        ``Net-(...)`` names -- so a name-equality test would flag correct
        boards.  Instead:

        1. Only pads present on both sides are compared; missing and
           orphaned footprints are already reported by
           :meth:`_check_components`.
        2. Each schematic net is paired with the PCB net that holds the
           most of its pads (one-to-one, largest overlap first; ties prefer
           the PCB net whose name is a spelling of the schematic name, then
           the alphabetically first).
        3. A pad whose PCB net is not the one paired with its schematic net
           is an error: a swapped pad, a short into another net, or a pad
           split off its net (an open).
        4. A pin the schematic connects to other pins whose PCB pad has no
           net is an error.  A pin the schematic leaves floating whose pad
           the PCB joins to other pads is an error.  Single-pin nets on
           either side are electrically the same as "unconnected" and are
           not reported.

        Args:
            result: SyncResult to add issues to
            pcb_pad_nets: Mapping of (reference, pad) to PCB net (``None`` =
                unconnected)
        """
        sch_pin_nets = self._schematic_pin_nets()
        if not sch_pin_nets:
            return

        common = sorted(set(sch_pin_nets) & set(pcb_pad_nets))
        if not common:
            return

        # Vacuity guard (mirrors the LVS legs, #4005 / #4681): a schematic
        # that wires none of these pins -- symbols placed but no wires or
        # labels, as in a PCB-first fixture -- gives no connectivity to
        # compare against.  Reporting every PCB net as "unconnected in
        # schematic" would be noise, and staying silent would read as a
        # pass, so say explicitly that the check could not run.
        if all(sch_pin_nets[key] is None for key in common):
            result.add(
                SyncIssue(
                    severity="warning",
                    category="net_mismatch",
                    message=(
                        f"Pad-net check skipped: the schematic connects none of the "
                        f"{len(common)} pins that have PCB pads"
                    ),
                    suggestion=(
                        "Wire the schematic (wires, labels or power symbols) so pad "
                        "nets can be compared against it"
                    ),
                )
            )
            return

        sch_groups: dict[str, set[tuple[str, str]]] = {}
        pcb_groups: dict[str, set[tuple[str, str]]] = {}
        overlap: dict[tuple[str, str], int] = {}
        for key in common:
            s_net = sch_pin_nets[key]
            p_net = pcb_pad_nets[key]
            if s_net is not None:
                sch_groups.setdefault(s_net, set()).add(key)
            if p_net is not None:
                pcb_groups.setdefault(p_net, set()).add(key)
            if s_net is not None and p_net is not None:
                overlap[(s_net, p_net)] = overlap.get((s_net, p_net), 0) + 1

        # Greedy one-to-one pairing, largest overlap first.
        ranked = sorted(
            overlap.items(),
            key=lambda item: (
                -item[1],
                0 if _same_net_spelling(item[0][0], item[0][1]) else 1,
                item[0][0],
                item[0][1],
            ),
        )
        sch_to_pcb: dict[str, str] = {}
        pcb_to_sch: dict[str, str] = {}
        for (s_net, p_net), _count in ranked:
            if s_net in sch_to_pcb or p_net in pcb_to_sch:
                continue
            sch_to_pcb[s_net] = p_net
            pcb_to_sch[p_net] = s_net

        for key in common:
            ref, pad = key
            s_net = sch_pin_nets[key]
            p_net = pcb_pad_nets[key]

            if s_net is None and p_net is None:
                continue

            if s_net is None:
                assert p_net is not None
                # Floating in the schematic; only a problem if the PCB
                # actually joins this pad to something.
                if len(pcb_groups[p_net]) < 2:
                    continue
                result.add(
                    SyncIssue(
                        severity="error",
                        category="net_mismatch",
                        message=(f"{ref}.{pad}: unconnected in schematic, PCB net {p_net!r}"),
                        suggestion=(
                            f"Remove {ref}.{pad} from net {p_net!r} on the PCB, or "
                            "connect the pin in the schematic and update the PCB"
                        ),
                        reference=ref,
                        net_pcb=p_net,
                        pin=pad,
                    )
                )
                continue

            if p_net is None:
                if len(sch_groups[s_net]) < 2:
                    continue
                result.add(
                    SyncIssue(
                        severity="error",
                        category="net_mismatch",
                        message=(f"{ref}.{pad}: schematic net {s_net!r}, unconnected on PCB"),
                        suggestion=(
                            f"Assign {ref}.{pad} to net {s_net!r} on the PCB "
                            "(update PCB from schematic)"
                        ),
                        reference=ref,
                        net_schematic=s_net,
                        pin=pad,
                    )
                )
                continue

            if sch_to_pcb.get(s_net) == p_net:
                continue
            # A pad alone on a PCB net that no other schematic net claims,
            # whose schematic net is also a single pin: nothing is wired
            # differently, only named differently.
            if (
                len(sch_groups[s_net]) < 2
                and len(pcb_groups[p_net]) < 2
                and p_net not in pcb_to_sch
            ):
                continue
            expected = sch_to_pcb.get(s_net)
            expected_str = f" (expected PCB net {expected!r})" if expected else ""
            result.add(
                SyncIssue(
                    severity="error",
                    category="net_mismatch",
                    message=(
                        f"{ref}.{pad}: schematic net {s_net!r}, PCB net {p_net!r}{expected_str}"
                    ),
                    suggestion=(
                        f"Move {ref}.{pad} to the PCB net that carries schematic net "
                        f"{s_net!r} (update PCB from schematic)"
                    ),
                    reference=ref,
                    net_schematic=s_net,
                    net_pcb=p_net,
                    pin=pad,
                )
            )

    def __repr__(self) -> str:
        """Return string representation."""
        sch_count = len(self.schematic.symbols) if self.schematic else 0
        pcb_count = self.pcb.footprint_count if self.pcb else 0
        return f"NetlistValidator(schematic_symbols={sch_count}, pcb_footprints={pcb_count})"
