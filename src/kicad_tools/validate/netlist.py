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


def _names_same_net_strict(name_a: str, name_b: str) -> bool:
    """True when two net names are unambiguously spellings of one net.

    Stricter than :func:`_same_net_spelling`, for deciding that a PCB net
    carries the *name of another* schematic net (issue #5980).  Accepts
    equality after stripping the leading ``/`` (``X`` / ``/X``), and a leaf
    match only when one side has no sheet path (``X`` vs ``/Sheet/X``).
    Two fully sheet-qualified names on different sheets (``/A/CLK`` vs
    ``/B/CLK``) are different nets -- a net class or zone keyed on one does
    not apply to the other -- so they never match.
    """
    a = name_a.lstrip("/")
    b = name_b.lstrip("/")
    if a == b:
        return True
    if "/" in a and "/" in b:
        return False
    return a.rsplit("/", 1)[-1] == b.rsplit("/", 1)[-1]


def _spelling_tier(sch_net: str, pcb_net: str) -> int:
    """Pairing tie-break: 0 strict spelling, 1 leaf-only spelling, 2 unrelated.

    Ranking the strict match first keeps ``/A/CLK`` paired with PCB
    ``/A/CLK`` rather than ``/B/CLK`` when both overlap it equally.
    """
    if _names_same_net_strict(sch_net, pcb_net):
        return 0
    if _same_net_spelling(sch_net, pcb_net):
        return 1
    return 2


def _is_generated_net_name(name: str) -> bool:
    """True for tool-invented net names that carry no design intent.

    ``Net-(R1-Pad2)`` and ``unconnected-(...)`` are derived from a pad, not
    chosen by the designer, so no net class, diff pair or zone is keyed on
    them and they never count as "the name of another net".
    """
    return name.startswith(("Net-(", "unconnected-"))


def _name_match_rank(sch_net: str, pcb_net: str) -> int:
    """Lower is a closer spelling match (exact, then ``/`` prefix, then leaf)."""
    if sch_net == pcb_net:
        return 0
    if sch_net.lstrip("/") == pcb_net.lstrip("/"):
        return 1
    return 2


def _format_pads(pads: list[tuple[str, str]], limit: int = 5) -> str:
    """Render ``[("C1", "1"), ...]`` as ``C1.1, C2.1`` (truncated past ``limit``)."""
    shown = ", ".join(f"{ref}.{pad}" for ref, pad in pads[:limit])
    if len(pads) > limit:
        shown += f", ... ({len(pads) - limit} more)"
    return shown


class NetlistValidator:
    """Validates synchronization between schematic and PCB netlists.

    Checks for:
    - Symbols missing from PCB (no corresponding footprint)
    - Orphaned footprints on PCB (no corresponding symbol)
    - Net name mismatches between schematic and PCB
    - Pads on a different net than their schematic pin (compared by
      connectivity, so net renaming alone is not drift)
    - PCB nets carrying another schematic net's name (swapped net names)

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
        5. A schematic net whose paired PCB net carries the *name* of a
           different schematic net is an error, even when every pad is
           wired correctly (issue #5980): see :meth:`_check_swapped_net_names`.

        When a pad is reported because most of its schematic net moved to
        another PCB net while the pad itself kept the net's own name, the
        message says the net is *split* and names the pads that moved,
        rather than presenting the one pad still on the right name as the
        culprit.  That wording is used only when the pad's PCB net carries
        no other schematic net: if it does, the pad sits on that net's
        copper (a short), and the plain mismatch message is kept.

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
                _spelling_tier(item[0][0], item[0][1]),
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

        # Schematic nets whose paired PCB net holds exactly their pads (no
        # pad strayed off, no foreign pad on it): only for these is "the
        # copper joins the right pads" true.
        clean_nets = {
            s_net
            for s_net, p_net in sch_to_pcb.items()
            if all(pcb_pad_nets[k] == p_net for k in sch_groups[s_net])
            and all(sch_pin_nets[k] == s_net for k in pcb_groups[p_net])
        }
        self._check_swapped_net_names(result, sch_to_pcb, set(sch_groups), clean_nets)

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
            # ``p_net`` holding another schematic net's pads means this pad
            # sits on that net's copper -- a short, not a split -- so keep
            # the plain mismatch wording (and never suggest moving the rest
            # of the net onto that copper).
            owner = pcb_to_sch.get(p_net)
            if (
                expected is not None
                and owner is None
                and _same_net_spelling(s_net, p_net)
                and not _same_net_spelling(s_net, expected)
            ):
                # Most of this net moved to ``expected`` while this pad kept
                # the net's own name (issue #5980).  Calling this pad wrong
                # ("expected PCB net 'Net-X'") points at the one pad that is
                # most likely right, so describe the split instead.
                moved = sorted(k for k in sch_groups[s_net] if pcb_pad_nets[k] == expected)
                message = (
                    f"{ref}.{pad}: schematic net {s_net!r} is split on the PCB: this pad "
                    f"is on {p_net!r}, but {len(moved)} of the net's "
                    f"{len(sch_groups[s_net])} pads ({_format_pads(moved)}) are on "
                    f"{expected!r}"
                )
                suggestion = (
                    f"Rejoin schematic net {s_net!r} on one PCB net: move "
                    f"{_format_pads(moved)} back to {p_net!r}, or move {ref}.{pad} to "
                    f"{expected!r} (update PCB from schematic)"
                )
            else:
                expected_str = f" (expected PCB net {expected!r})" if expected else ""
                message = f"{ref}.{pad}: schematic net {s_net!r}, PCB net {p_net!r}{expected_str}"
                if owner is not None and owner != s_net:
                    message += f"; PCB net {p_net!r} carries schematic net {owner!r}"
                suggestion = (
                    f"Move {ref}.{pad} to the PCB net that carries schematic net "
                    f"{s_net!r} (update PCB from schematic)"
                )
            result.add(
                SyncIssue(
                    severity="error",
                    category="net_mismatch",
                    message=message,
                    suggestion=suggestion,
                    reference=ref,
                    net_schematic=s_net,
                    net_pcb=p_net,
                    pin=pad,
                )
            )

    def _check_swapped_net_names(
        self,
        result: SyncResult,
        sch_to_pcb: dict[str, str],
        sch_nets: set[str],
        clean_nets: set[str] | None = None,
    ) -> None:
        """Flag PCB nets that carry the name of a *different* schematic net.

        The pad check compares by connectivity, so when every pad of two
        nets is exchanged wholesale (all ``SWDIO`` pads on PCB net
        ``SWCLK`` and vice versa) the copper still joins the right pads and
        no pad is out of place (issue #5980).  The **names** are wrong,
        though, and names are what net classes (track width, clearance),
        diff-pair and match-group membership, zone nets and kicad-tools'
        own name-based power-net inference key on -- so a ``GND``/``+3.3V``
        swap puts the power rules on the wrong copper.

        A schematic net ``S`` is reported when its paired PCB net ``P``:

        * is not strictly a spelling of ``S`` itself (``/X``, ``X``,
          ``/Sheet/X``; :func:`_names_same_net_strict`), and
        * is unambiguously the name of another schematic net ``S2`` that has
          pads here (:func:`_names_same_net_strict`: ``/A/CLK`` is not the
          name of ``/B/CLK``, but it *is* the name of ``/A/CLK``, so a
          wholesale ``/A/CLK`` <-> ``/B/CLK`` exchange is a swap, issue
          #5999), and
        * neither ``P`` nor ``S2`` is a tool-generated ``Net-(...)`` name.

        When ``S2`` is in turn paired with a PCB net named like ``S``, the
        two are reported once, as a swap.  Severity is ``error``: the trigger
        requires a designer-chosen name to land on another net's pads, which
        no correct board does (none of the fleet boards does), and
        ``in_sync: true`` would otherwise certify a board whose name-keyed
        design rules apply to the wrong pads.

        Args:
            result: SyncResult to add issues to
            sch_to_pcb: The one-to-one schematic-to-PCB net pairing
            sch_nets: Schematic nets that have at least one compared pad
            clean_nets: Schematic nets whose paired PCB net holds exactly
                their pads.  The swap message claims the copper is right only
                when both nets are in this set (``None``: unknown, never claim).
        """
        named_after: dict[str, str] = {}
        # Every best-ranked claimant of a PCB name, when more than one (an
        # unqualified ``CLK`` spells both ``/A/CLK`` and ``/B/CLK``).
        ambiguous: dict[str, list[str]] = {}
        for s_net, p_net in sch_to_pcb.items():
            # Self-name test.  ``P`` is S's own name when it strictly spells
            # S (``X`` / ``/X`` / ``/Sheet/X``).  A merely leaf-equal name
            # (``/B/CLK`` for ``/A/CLK``) is tolerated too -- kct's identity
            # for a net and KiCad's chosen label can differ in the sheet
            # path, e.g. a hierarchical label in a repeated sheet -- but only
            # when no *other* schematic net strictly owns that name; if one
            # does, ``P`` is that net's name (issue #5999).
            if _is_generated_net_name(p_net) or _names_same_net_strict(s_net, p_net):
                continue
            others = [
                s2
                for s2 in sch_nets
                if s2 != s_net
                and not _is_generated_net_name(s2)
                and _names_same_net_strict(s2, p_net)
            ]
            if not others:
                continue
            best_rank = min(_name_match_rank(s2, p_net) for s2 in others)
            best = sorted(s2 for s2 in others if _name_match_rank(s2, p_net) == best_rank)
            # Several sheets share the leaf of an unqualified PCB name: prefer
            # the claimant whose own PCB net carries S's name (a swap partner)
            # over whichever sheet sorts first.
            partners = [
                s2
                for s2 in best
                if s2 in sch_to_pcb
                and not _is_generated_net_name(sch_to_pcb[s2])
                and _names_same_net_strict(s_net, sch_to_pcb[s2])
            ]
            named_after[s_net] = partners[0] if partners else best[0]
            if not partners and len(best) > 1:
                ambiguous[s_net] = best

        for s_net in sorted(named_after):
            s2 = named_after[s_net]
            p_net = sch_to_pcb[s_net]
            if named_after.get(s2) == s_net:
                if s2 < s_net:
                    continue  # reported with the pair's first net
                p2 = sch_to_pcb[s2]
                if clean_nets is not None and s_net in clean_nets and s2 in clean_nets:
                    lead = "The copper joins the right pads but the names are exchanged, "
                else:
                    lead = (
                        "The names are exchanged (stray pads on these nets are "
                        "reported separately), "
                    )
                result.add(
                    SyncIssue(
                        severity="error",
                        category="net_mismatch",
                        message=(
                            f"Net names swapped: schematic net {s_net!r} is PCB net "
                            f"{p_net!r} and schematic net {s2!r} is PCB net {p2!r}"
                        ),
                        suggestion=(
                            f"{lead}"
                            "so net classes, diff pairs and zones keyed on these names "
                            f"apply to the wrong pads. Exchange the names of PCB nets "
                            f"{p_net!r} and {p2!r} (update PCB from schematic)"
                        ),
                        net_schematic=s_net,
                        net_pcb=p_net,
                    )
                )
                continue
            if s_net in ambiguous:
                owners = ", ".join(repr(n) for n in ambiguous[s_net])
                owned_by = f"which is the name of schematic nets {owners}"
            else:
                owned_by = f"which is the name of schematic net {s2!r}"
            result.add(
                SyncIssue(
                    severity="error",
                    category="net_mismatch",
                    message=(
                        f"Net named after another net: schematic net {s_net!r} is PCB net "
                        f"{p_net!r}, {owned_by}"
                    ),
                    suggestion=(
                        f"Rename PCB net {p_net!r} after schematic net {s_net!r}, so net "
                        f"classes, diff pairs and zones keyed on {p_net!r} do not apply to "
                        "its pads (update PCB from schematic)"
                    ),
                    net_schematic=s_net,
                    net_pcb=p_net,
                )
            )

    def __repr__(self) -> str:
        """Return string representation."""
        sch_count = len(self.schematic.symbols) if self.schematic else 0
        pcb_count = self.pcb.footprint_count if self.pcb else 0
        return f"NetlistValidator(schematic_symbols={sch_count}, pcb_footprints={pcb_count})"
