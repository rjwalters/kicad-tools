"""Schematic-to-PCB consistency checker.

Validates that schematic and PCB are in sync, checking for:
- Missing components (in schematic but not PCB)
- Extra components (in PCB but not schematic)
- Net connectivity mismatches
- Property mismatches (value, footprint)

Also provides Layout-vs-Schematic (LVS) checking with hierarchical
schematic support and multi-pass fuzzy matching.

Example:
    >>> from kicad_tools.schema.schematic import Schematic
    >>> from kicad_tools.schema.pcb import PCB
    >>> from kicad_tools.validate.consistency import SchematicPCBChecker
    >>>
    >>> schematic = Schematic.load("project.kicad_sch")
    >>> pcb = PCB.load("project.kicad_pcb")
    >>> checker = SchematicPCBChecker(schematic, pcb)
    >>> issues = checker.check()
    >>>
    >>> for issue in issues:
    ...     print(f"{issue.severity}: {issue.reference} - {issue.suggestion}")

LVS Example:
    >>> checker = SchematicPCBChecker("project.kicad_sch", "project.kicad_pcb")
    >>> lvs_result = checker.check_lvs()
    >>> for match in lvs_result.matches:
    ...     print(f"{match.sch_ref} -> {match.pcb_ref} (confidence: {match.confidence})")
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.schema.schematic import Schematic


@dataclass(frozen=True)
class ConsistencyIssue:
    """Represents an inconsistency between schematic and PCB.

    Attributes:
        issue_type: Type of issue - "missing", "extra", or "mismatch"
        domain: Domain of the issue - "component", "net", or "property"
        schematic_value: Value from schematic (or None if not applicable)
        pcb_value: Value from PCB (or None if not applicable)
        reference: Component reference or net name
        severity: Issue severity - "error" or "warning"
        suggestion: Actionable fix suggestion
    """

    issue_type: str  # "missing", "extra", "mismatch"
    domain: str  # "component", "net", "property"
    schematic_value: Any
    pcb_value: Any
    reference: str
    severity: str  # "error", "warning"
    suggestion: str

    def __post_init__(self) -> None:
        """Validate issue_type, domain, and severity values."""
        valid_types = ("missing", "extra", "mismatch")
        if self.issue_type not in valid_types:
            raise ValueError(f"issue_type must be one of {valid_types}, got {self.issue_type!r}")

        valid_domains = ("component", "net", "property")
        if self.domain not in valid_domains:
            raise ValueError(f"domain must be one of {valid_domains}, got {self.domain!r}")

        if self.severity not in ("error", "warning"):
            raise ValueError(f"severity must be 'error' or 'warning', got {self.severity!r}")

    @property
    def is_error(self) -> bool:
        """Check if this is an error (not a warning)."""
        return self.severity == "error"

    @property
    def is_warning(self) -> bool:
        """Check if this is a warning (not an error)."""
        return self.severity == "warning"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "issue_type": self.issue_type,
            "domain": self.domain,
            "schematic_value": self.schematic_value,
            "pcb_value": self.pcb_value,
            "reference": self.reference,
            "severity": self.severity,
            "suggestion": self.suggestion,
        }


@dataclass
class ConsistencyResult:
    """Aggregates all consistency check results.

    Provides convenient filtering and counting methods.
    """

    issues: list[ConsistencyIssue]

    @property
    def is_consistent(self) -> bool:
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
    def errors(self) -> list[ConsistencyIssue]:
        """List of only error issues."""
        return [i for i in self.issues if i.is_error]

    @property
    def warnings(self) -> list[ConsistencyIssue]:
        """List of only warning issues."""
        return [i for i in self.issues if i.is_warning]

    @property
    def component_issues(self) -> list[ConsistencyIssue]:
        """Issues related to components (missing/extra)."""
        return [i for i in self.issues if i.domain == "component"]

    @property
    def net_issues(self) -> list[ConsistencyIssue]:
        """Issues related to net connectivity."""
        return [i for i in self.issues if i.domain == "net"]

    @property
    def property_issues(self) -> list[ConsistencyIssue]:
        """Issues related to property mismatches (value, footprint)."""
        return [i for i in self.issues if i.domain == "property"]

    def __iter__(self):
        """Iterate over all issues."""
        return iter(self.issues)

    def __len__(self) -> int:
        """Total number of issues."""
        return len(self.issues)

    def __bool__(self) -> bool:
        """True if there are any issues."""
        return len(self.issues) > 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "is_consistent": self.is_consistent,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "issues": [i.to_dict() for i in self.issues],
        }

    def summary(self) -> str:
        """Generate a human-readable summary."""
        status = "CONSISTENT" if self.is_consistent else "INCONSISTENT"
        parts = [
            f"Schematic ↔ PCB {status}: {self.error_count} errors, {self.warning_count} warnings"
        ]

        if component_errors := [i for i in self.component_issues if i.is_error]:
            parts.append(f"  Component errors: {len(component_errors)}")
        if net_errors := [i for i in self.net_issues if i.is_error]:
            parts.append(f"  Net errors: {len(net_errors)}")
        if property_errors := [i for i in self.property_issues if i.is_error]:
            parts.append(f"  Property errors: {len(property_errors)}")

        return "\n".join(parts)


@dataclass(frozen=True)
class LVSMatch:
    """Represents a matched component between schematic and PCB.

    Attributes:
        pcb_ref: Component reference on the PCB
        sch_ref: Component reference in the schematic
        confidence: Match confidence from 0.0 to 1.0
        match_reason: Description of how the match was determined
        value_match: Whether the component values match
        footprint_match: Whether the footprints match
    """

    pcb_ref: str
    sch_ref: str
    confidence: float
    match_reason: str
    value_match: bool
    footprint_match: bool

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "pcb_ref": self.pcb_ref,
            "sch_ref": self.sch_ref,
            "confidence": self.confidence,
            "match_reason": self.match_reason,
            "value_match": self.value_match,
            "footprint_match": self.footprint_match,
        }


@dataclass
class LVSResult:
    """Aggregates all LVS matching results.

    Attributes:
        matches: List of matched components with confidence scores
        unmatched_pcb: PCB references with no schematic match
        unmatched_sch: Schematic references with no PCB match
    """

    matches: list[LVSMatch] = field(default_factory=list)
    unmatched_pcb: list[str] = field(default_factory=list)
    unmatched_sch: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """True if all components matched exactly with no orphans."""
        return (
            not self.unmatched_pcb
            and not self.unmatched_sch
            and all(m.confidence >= 1.0 for m in self.matches)
        )

    @property
    def exact_match_count(self) -> int:
        """Count of exact matches (confidence == 1.0)."""
        return sum(1 for m in self.matches if m.confidence >= 1.0)

    @property
    def fuzzy_match_count(self) -> int:
        """Count of fuzzy matches (confidence < 1.0)."""
        return sum(1 for m in self.matches if m.confidence < 1.0)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "is_clean": self.is_clean,
            "exact_matches": self.exact_match_count,
            "fuzzy_matches": self.fuzzy_match_count,
            "unmatched_pcb_count": len(self.unmatched_pcb),
            "unmatched_sch_count": len(self.unmatched_sch),
            "matches": [m.to_dict() for m in self.matches],
            "unmatched_pcb": sorted(self.unmatched_pcb),
            "unmatched_sch": sorted(self.unmatched_sch),
        }

    def summary(self) -> str:
        """Generate a human-readable summary."""
        status = "CLEAN" if self.is_clean else "MISMATCHES FOUND"
        parts = [f"LVS {status}:"]
        parts.append(f"  Exact matches:   {self.exact_match_count}")
        if self.fuzzy_match_count:
            parts.append(f"  Fuzzy matches:   {self.fuzzy_match_count}")
        if self.unmatched_pcb:
            parts.append(f"  Unmatched (PCB): {len(self.unmatched_pcb)}")
        if self.unmatched_sch:
            parts.append(f"  Unmatched (SCH): {len(self.unmatched_sch)}")
        return "\n".join(parts)


def _extract_ref_prefix(ref: str) -> str:
    """Extract the alphabetic prefix from a reference designator.

    Example: 'R12' -> 'R', 'U1' -> 'U', 'C100' -> 'C'
    """
    match = re.match(r"^([A-Za-z]+)", ref)
    return match.group(1) if match else ref


def _normalize_footprint(fp: str) -> str:
    """Normalize footprint name for comparison.

    Strips the library prefix (e.g. 'Resistor_SMD:R_0402' -> 'R_0402').
    """
    return fp.split(":")[-1] if ":" in fp else fp


def _extract_package_size(footprint: str) -> str | None:
    """Extract the imperial package size code from a footprint name.

    Recognises standard KiCad footprint naming such as
    ``R_0402_1005Metric``, ``C_0805_2012Metric``, ``L_1206_3216Metric``,
    as well as bare names like ``0402`` or ``C_0603``.

    Returns the 4-digit imperial size (e.g. ``"0402"``, ``"0805"``) or
    ``None`` when the footprint does not encode a recognisable passive size
    (ICs, connectors, test points, etc.).

    Examples:
        >>> _extract_package_size("R_0402_1005Metric")
        '0402'
        >>> _extract_package_size("Resistor_SMD:R_0805_2012Metric")
        '0805'
        >>> _extract_package_size("SOT-23-5")
        >>> _extract_package_size("")
    """
    # Work on the library-stripped name
    name = footprint.split(":")[-1] if ":" in footprint else footprint
    if not name:
        return None

    # Preferred: <size>_<metric>Metric  (e.g. 0402_1005Metric)
    m = re.search(r"(\d{4})_\d{4}Metric", name)
    if m:
        return m.group(1)

    # Fallback: isolated 4-digit code preceded by _ or - or start
    m = re.search(r"(?:^|[_-])(\d{4})(?:[_-]|$)", name)
    if m:
        return m.group(1)

    return None


class SchematicPCBChecker:
    """Check consistency between schematic and PCB.

    Validates:
    - Component presence (missing/extra)
    - Net connectivity matches
    - Property matches (value, footprint)

    Example:
        >>> schematic = Schematic.load("project.kicad_sch")
        >>> pcb = PCB.load("project.kicad_pcb")
        >>> checker = SchematicPCBChecker(schematic, pcb)
        >>> result = checker.check()
        >>>
        >>> if not result.is_consistent:
        ...     for issue in result.errors:
        ...         print(f"{issue.reference}: {issue.suggestion}")

    Attributes:
        schematic: The schematic to check
        pcb: The PCB to check against
    """

    def __init__(
        self,
        schematic: str | Path | Schematic,
        pcb: str | Path | PCB,
    ) -> None:
        """Initialize the checker.

        Args:
            schematic: Path to schematic file or Schematic object
            pcb: Path to PCB file or PCB object
        """
        from kicad_tools.schema.pcb import PCB as PCBClass
        from kicad_tools.schema.schematic import Schematic as SchematicClass

        # Store schematic path for hierarchical BOM extraction in check_lvs()
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

    def check(self) -> ConsistencyResult:
        """Run all consistency checks.

        Returns:
            ConsistencyResult containing all issues found
        """
        issues: list[ConsistencyIssue] = []

        issues.extend(self._check_components())
        issues.extend(self._check_nets())
        issues.extend(self._check_properties())

        return ConsistencyResult(issues=issues)

    def check_lvs(self, schematic_path: str | Path | None = None) -> LVSResult:
        """Run Layout-vs-Schematic check with hierarchical schematic support.

        Uses ``extract_bom()`` to traverse all sub-sheets, then runs multi-pass
        matching against PCB footprints.

        Matching passes (in order, each pass removes matched components):
          1. Exact: ref + value + footprint all match (confidence 1.0)
          2. Value+footprint: unique value+footprint pair across different refs (0.8)
          3. Value+prefix: unique value within same ref prefix, e.g. all R's (0.6)
          4. Net-based: PCB pad nets correlate with other matched components (0.4)

        Args:
            schematic_path: Optional override for schematic path. If not provided,
                uses the path stored at construction time.

        Returns:
            LVSResult with matches, unmatched PCB refs, and unmatched schematic refs.

        Raises:
            ValueError: If no schematic path is available (constructed with
                a Schematic object and no path override provided).
        """
        from kicad_tools.schema.bom import BOMItem, extract_bom

        sch_path = str(schematic_path) if schematic_path else self._schematic_path
        if not sch_path:
            raise ValueError(
                "No schematic path available for hierarchical LVS check. "
                "Provide a path at construction or pass schematic_path argument."
            )

        # Extract all components from hierarchical schematic
        bom = extract_bom(sch_path, hierarchical=True)

        # Build schematic component dict (skip virtual/power/DNP)
        sch_components: dict[str, BOMItem] = {}
        for item in bom.items:
            if item.is_virtual or item.is_power_symbol:
                continue
            if item.dnp:
                continue
            if item.reference and not item.reference.startswith("#"):
                sch_components[item.reference] = item

        # Build PCB component dict
        pcb_components: dict[str, dict[str, str]] = {}
        pcb_pad_nets: dict[str, dict[str, str]] = {}  # ref -> {pad: net_name}
        for fp in self.pcb.footprints:
            if fp.reference and not fp.reference.startswith("#"):
                pcb_components[fp.reference] = {
                    "value": fp.value or "",
                    "footprint": fp.name or "",
                }
                pad_nets: dict[str, str] = {}
                for pad in fp.pads:
                    if pad.net_name:
                        pad_nets[pad.number] = pad.net_name
                if pad_nets:
                    pcb_pad_nets[fp.reference] = pad_nets

        # Track unmatched pools
        unmatched_sch = set(sch_components.keys())
        unmatched_pcb = set(pcb_components.keys())
        matches: list[LVSMatch] = []

        # Pass 1: Exact match (ref + value + footprint)
        exact_matched: list[str] = []
        for ref in sorted(unmatched_sch & unmatched_pcb):
            sch_item = sch_components[ref]
            pcb_item = pcb_components[ref]
            sch_fp = _normalize_footprint(sch_item.footprint)
            pcb_fp = _normalize_footprint(pcb_item["footprint"])
            value_ok = sch_item.value == pcb_item["value"]
            fp_ok = sch_fp == pcb_fp
            if value_ok and fp_ok:
                matches.append(
                    LVSMatch(
                        pcb_ref=ref,
                        sch_ref=ref,
                        confidence=1.0,
                        match_reason="exact ref+value+footprint",
                        value_match=True,
                        footprint_match=True,
                    )
                )
                exact_matched.append(ref)
        for ref in exact_matched:
            unmatched_sch.discard(ref)
            unmatched_pcb.discard(ref)

        # Pass 2: Value+footprint match across different refs
        self._pass_value_footprint(
            sch_components, pcb_components, unmatched_sch, unmatched_pcb, matches
        )

        # Pass 3: Value+prefix match
        self._pass_value_prefix(
            sch_components, pcb_components, unmatched_sch, unmatched_pcb, matches
        )

        # Pass 4: Net-based match
        self._pass_net_based(
            sch_components, pcb_components, pcb_pad_nets, unmatched_sch, unmatched_pcb, matches
        )

        return LVSResult(
            matches=matches,
            unmatched_pcb=sorted(unmatched_pcb),
            unmatched_sch=sorted(unmatched_sch),
        )

    def _pass_value_footprint(
        self,
        sch_components: dict[str, Any],
        pcb_components: dict[str, dict[str, str]],
        unmatched_sch: set[str],
        unmatched_pcb: set[str],
        matches: list[LVSMatch],
    ) -> None:
        """Pass 2: Match by unique value+footprint combination."""
        # Build value+footprint -> [ref] maps for both sides
        sch_vf: dict[tuple[str, str], list[str]] = {}
        for ref in unmatched_sch:
            item = sch_components[ref]
            key = (item.value, _normalize_footprint(item.footprint))
            sch_vf.setdefault(key, []).append(ref)

        pcb_vf: dict[tuple[str, str], list[str]] = {}
        for ref in unmatched_pcb:
            item = pcb_components[ref]
            key = (item["value"], _normalize_footprint(item["footprint"]))
            pcb_vf.setdefault(key, []).append(ref)

        # Only match when a value+footprint pair has exactly one on each side
        matched_pairs: list[tuple[str, str]] = []
        for key in sch_vf:
            if key in pcb_vf and len(sch_vf[key]) == 1 and len(pcb_vf[key]) == 1:
                sch_ref = sch_vf[key][0]
                pcb_ref = pcb_vf[key][0]
                if sch_ref != pcb_ref:  # already handled in pass 1 if same
                    matched_pairs.append((sch_ref, pcb_ref))

        for sch_ref, pcb_ref in matched_pairs:
            matches.append(
                LVSMatch(
                    pcb_ref=pcb_ref,
                    sch_ref=sch_ref,
                    confidence=0.8,
                    match_reason="unique value+footprint pair",
                    value_match=True,
                    footprint_match=True,
                )
            )
            unmatched_sch.discard(sch_ref)
            unmatched_pcb.discard(pcb_ref)

    def _pass_value_prefix(
        self,
        sch_components: dict[str, Any],
        pcb_components: dict[str, dict[str, str]],
        unmatched_sch: set[str],
        unmatched_pcb: set[str],
        matches: list[LVSMatch],
    ) -> None:
        """Pass 3: Match by value within same reference prefix."""
        # Group by prefix + value
        sch_pv: dict[tuple[str, str], list[str]] = {}
        for ref in unmatched_sch:
            item = sch_components[ref]
            key = (_extract_ref_prefix(ref), item.value)
            sch_pv.setdefault(key, []).append(ref)

        pcb_pv: dict[tuple[str, str], list[str]] = {}
        for ref in unmatched_pcb:
            item = pcb_components[ref]
            key = (_extract_ref_prefix(ref), item["value"])
            pcb_pv.setdefault(key, []).append(ref)

        matched_pairs: list[tuple[str, str]] = []
        for key in sch_pv:
            if key in pcb_pv and len(sch_pv[key]) == 1 and len(pcb_pv[key]) == 1:
                sch_ref = sch_pv[key][0]
                pcb_ref = pcb_pv[key][0]
                sch_item = sch_components[sch_ref]
                pcb_item = pcb_components[pcb_ref]
                sch_fp = _normalize_footprint(sch_item.footprint)
                pcb_fp = _normalize_footprint(pcb_item["footprint"])

                # Reject match when both sides have a recognised package
                # size and they differ (e.g. 0402 vs 0603).
                sch_size = _extract_package_size(sch_fp)
                pcb_size = _extract_package_size(pcb_fp)
                if sch_size and pcb_size and sch_size != pcb_size:
                    continue

                fp_match = sch_fp == pcb_fp
                matched_pairs.append((sch_ref, pcb_ref, fp_match))

        for sch_ref, pcb_ref, fp_match in matched_pairs:
            matches.append(
                LVSMatch(
                    pcb_ref=pcb_ref,
                    sch_ref=sch_ref,
                    confidence=0.6,
                    match_reason="unique value within reference prefix",
                    value_match=True,
                    footprint_match=fp_match,
                )
            )
            unmatched_sch.discard(sch_ref)
            unmatched_pcb.discard(pcb_ref)

    def _pass_net_based(
        self,
        sch_components: dict[str, Any],
        pcb_components: dict[str, dict[str, str]],
        pcb_pad_nets: dict[str, dict[str, str]],
        unmatched_sch: set[str],
        unmatched_pcb: set[str],
        matches: list[LVSMatch],
    ) -> None:
        """Pass 4: Match by net connectivity patterns on PCB.

        For remaining unmatched components, check if they share the same
        reference prefix and have overlapping net sets.
        """
        if not unmatched_sch or not unmatched_pcb:
            return

        # Group remaining by prefix
        sch_by_prefix: dict[str, list[str]] = {}
        for ref in unmatched_sch:
            prefix = _extract_ref_prefix(ref)
            sch_by_prefix.setdefault(prefix, []).append(ref)

        pcb_by_prefix: dict[str, list[str]] = {}
        for ref in unmatched_pcb:
            prefix = _extract_ref_prefix(ref)
            pcb_by_prefix.setdefault(prefix, []).append(ref)

        matched_pairs: list[tuple[str, str]] = []
        for prefix in sch_by_prefix:
            if prefix not in pcb_by_prefix:
                continue
            sch_refs = sch_by_prefix[prefix]
            pcb_refs = pcb_by_prefix[prefix]

            # Only attempt net-based matching for 1:1 prefix groups
            if len(sch_refs) == 1 and len(pcb_refs) == 1:
                sch_ref = sch_refs[0]
                pcb_ref = pcb_refs[0]
                # Verify the PCB component has net assignments
                if pcb_ref in pcb_pad_nets and pcb_pad_nets[pcb_ref]:
                    sch_item = sch_components[sch_ref]
                    pcb_item = pcb_components[pcb_ref]

                    # Reject match when both sides have a recognised package
                    # size and they differ (e.g. 0402 vs 0603).
                    sch_fp = _normalize_footprint(sch_item.footprint)
                    pcb_fp = _normalize_footprint(pcb_item["footprint"])
                    sch_size = _extract_package_size(sch_fp)
                    pcb_size = _extract_package_size(pcb_fp)
                    if sch_size and pcb_size and sch_size != pcb_size:
                        continue

                    value_ok = sch_item.value == pcb_item["value"]
                    fp_ok = sch_fp == pcb_fp
                    matched_pairs.append((sch_ref, pcb_ref, value_ok, fp_ok))

        for sch_ref, pcb_ref, value_ok, fp_ok in matched_pairs:
            matches.append(
                LVSMatch(
                    pcb_ref=pcb_ref,
                    sch_ref=sch_ref,
                    confidence=0.4,
                    match_reason="net-based correlation within prefix",
                    value_match=value_ok,
                    footprint_match=fp_ok,
                )
            )
            unmatched_sch.discard(sch_ref)
            unmatched_pcb.discard(pcb_ref)

    def _check_components(self) -> list[ConsistencyIssue]:
        """Check component consistency (missing/extra).

        Returns:
            List of component-related consistency issues
        """
        issues: list[ConsistencyIssue] = []

        # Build reference sets, excluding power symbols
        sch_refs = {
            sym.reference
            for sym in self.schematic.symbols
            if sym.reference and not sym.reference.startswith("#")
        }

        pcb_refs = {
            fp.reference
            for fp in self.pcb.footprints
            if fp.reference and not fp.reference.startswith("#")
        }

        # Find components missing from PCB
        for ref in sorted(sch_refs - pcb_refs):
            # Get footprint from schematic if available
            sym = next(s for s in self.schematic.symbols if s.reference == ref)
            footprint = getattr(sym, "footprint", "") or ""
            footprint_hint = f" ({footprint})" if footprint else ""

            issues.append(
                ConsistencyIssue(
                    issue_type="missing",
                    domain="component",
                    schematic_value=ref,
                    pcb_value=None,
                    reference=ref,
                    severity="error",
                    suggestion=f"Add footprint for {ref}{footprint_hint} to PCB",
                )
            )

        # Find extra components on PCB (not in schematic)
        for ref in sorted(pcb_refs - sch_refs):
            issues.append(
                ConsistencyIssue(
                    issue_type="extra",
                    domain="component",
                    schematic_value=None,
                    pcb_value=ref,
                    reference=ref,
                    severity="warning",
                    suggestion=f"Remove {ref} from PCB or add to schematic",
                )
            )

        return issues

    def _check_nets(self) -> list[ConsistencyIssue]:
        """Check net connectivity consistency.

        Returns:
            List of net-related consistency issues
        """
        issues: list[ConsistencyIssue] = []

        # Build net-to-pins mapping from schematic
        # For each component, we track which pins connect to which nets
        sch_pin_nets = self._extract_schematic_pin_nets()

        # Build net-to-pads mapping from PCB
        pcb_pad_nets: dict[tuple[str, str], str] = {}
        for fp in self.pcb.footprints:
            if fp.reference and not fp.reference.startswith("#"):
                for pad in fp.pads:
                    if pad.net_name:
                        pcb_pad_nets[(fp.reference, pad.number)] = pad.net_name

        # Compare connectivity for common components
        sch_refs = set(sch_pin_nets.keys())
        pcb_refs = {ref for ref, _ in pcb_pad_nets.keys()}

        for ref in sch_refs & pcb_refs:
            sch_pins = sch_pin_nets.get(ref, {})
            for pin, sch_net in sch_pins.items():
                pcb_net = pcb_pad_nets.get((ref, pin))
                if pcb_net and sch_net and sch_net != pcb_net:
                    # Net name mismatch
                    issues.append(
                        ConsistencyIssue(
                            issue_type="mismatch",
                            domain="net",
                            schematic_value=sch_net,
                            pcb_value=pcb_net,
                            reference=f"{ref}.{pin}",
                            severity="error",
                            suggestion=f"Update net {ref}.{pin}: schematic has "
                            f'"{sch_net}", PCB has "{pcb_net}"',
                        )
                    )

        return issues

    def _extract_schematic_pin_nets(self) -> dict[str, dict[str, str]]:
        """Extract pin-to-net mapping from schematic.

        This is a simplified extraction that uses global labels and local labels
        connected to component pins.

        Returns:
            Mapping of reference -> {pin_number: net_name}
        """
        # This is a simplified implementation
        # Full implementation would require netlist extraction from schematic
        # For now, we return an empty dict and rely on PCB-side checks
        return {}

    def _check_properties(self) -> list[ConsistencyIssue]:
        """Check component property consistency (value, footprint).

        Returns:
            List of property-related consistency issues
        """
        issues: list[ConsistencyIssue] = []

        # Build lookup maps
        sch_components: dict[str, dict[str, str]] = {}
        for sym in self.schematic.symbols:
            if sym.reference and not sym.reference.startswith("#"):
                sch_components[sym.reference] = {
                    "value": sym.value or "",
                    "footprint": sym.footprint or "",
                }

        pcb_components: dict[str, dict[str, str]] = {}
        for fp in self.pcb.footprints:
            if fp.reference and not fp.reference.startswith("#"):
                pcb_components[fp.reference] = {
                    "value": fp.value or "",
                    "footprint": fp.name or "",
                }

        # Check properties for components present in both
        common_refs = set(sch_components.keys()) & set(pcb_components.keys())

        for ref in sorted(common_refs):
            sch_props = sch_components[ref]
            pcb_props = pcb_components[ref]

            # Check value mismatch
            sch_value = sch_props["value"]
            pcb_value = pcb_props["value"]
            if sch_value and pcb_value and sch_value != pcb_value:
                issues.append(
                    ConsistencyIssue(
                        issue_type="mismatch",
                        domain="property",
                        schematic_value=sch_value,
                        pcb_value=pcb_value,
                        reference=ref,
                        severity="warning",
                        suggestion=f"Update {ref} value: schematic has "
                        f'"{sch_value}", PCB has "{pcb_value}"',
                    )
                )

            # Check footprint mismatch
            sch_footprint = sch_props["footprint"]
            pcb_footprint = pcb_props["footprint"]
            if sch_footprint and pcb_footprint:
                # Normalize footprint names for comparison
                # KiCad may use full library:footprint or just footprint name
                sch_fp_name = (
                    sch_footprint.split(":")[-1] if ":" in sch_footprint else sch_footprint
                )
                pcb_fp_name = (
                    pcb_footprint.split(":")[-1] if ":" in pcb_footprint else pcb_footprint
                )

                if sch_fp_name != pcb_fp_name:
                    issues.append(
                        ConsistencyIssue(
                            issue_type="mismatch",
                            domain="property",
                            schematic_value=sch_footprint,
                            pcb_value=pcb_footprint,
                            reference=ref,
                            severity="error",
                            suggestion=f"Update {ref} footprint: schematic has "
                            f'"{sch_footprint}", PCB has "{pcb_footprint}"',
                        )
                    )

        return issues

    def __repr__(self) -> str:
        """Return string representation."""
        sch_count = len(self.schematic.symbols) if self.schematic else 0
        pcb_count = self.pcb.footprint_count if self.pcb else 0
        return f"SchematicPCBChecker(schematic_symbols={sch_count}, pcb_footprints={pcb_count})"
