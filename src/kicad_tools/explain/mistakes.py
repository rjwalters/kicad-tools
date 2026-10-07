"""Common PCB design mistake detection with educational explanations.

This module provides tools for detecting common PCB design mistakes that
experienced designers avoid, along with educational explanations and fix
suggestions.

Example:
    >>> from kicad_tools.explain.mistakes import detect_mistakes
    >>> from kicad_tools.schema.pcb import PCB
    >>> pcb = PCB.load("design.kicad_pcb")
    >>> mistakes = detect_mistakes(pcb)
    >>> for m in mistakes:
    ...     print(f"[{m.severity}] {m.title}")
    ...     print(f"  Location: {m.components}")
    ...     print(f"  Problem: {m.explanation}")
    ...     print(f"  Fix: {m.fix_suggestion}")
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, Protocol

from ..router.net_class import (
    is_ground_rail_name,
    is_power_rail_name,
    is_power_rail_pin,
    is_switch_node_name,
    is_unconnected_net_name,
)

if TYPE_CHECKING:
    from collections.abc import Collection

    from ..schema.pcb import PCB, Footprint


class MistakeCategory(Enum):
    """Categories of PCB design mistakes."""

    BYPASS_CAP = "bypass_capacitor"
    CRYSTAL = "crystal_oscillator"
    DIFFERENTIAL_PAIR = "differential_pair"
    POWER_TRACE = "power_trace"
    THERMAL = "thermal_management"
    EMI = "emi_shielding"
    DECOUPLING = "decoupling"
    GROUNDING = "grounding"
    VIA = "via_placement"
    MANUFACTURABILITY = "manufacturability"
    # Issue #4899: net-new categories added for the Konnect design-review
    # audit parity pass (audit_connections / check_bom_health).
    CONNECTIVITY = "connectivity"
    BOM_HEALTH = "bom_health"


@dataclass
class Mistake:
    """A detected PCB design mistake.

    Attributes:
        category: The category of mistake
        severity: Severity level ("error", "warning", "info")
        title: Short descriptive title
        components: List of component references involved (e.g., ["C5", "U1"])
        location: Optional (x, y) coordinates in mm
        explanation: Detailed explanation of why this is a problem
        fix_suggestion: Actionable suggestion for fixing the issue
        learn_more_url: Optional URL or path to educational documentation
        rule_id: Stable id of the check that produced the finding, e.g.
            ``"mistake.bypass_cap_distance"`` (set by
            :class:`MistakeDetector`, Issue #6006).
        key: Stable finding key ``rule_id|items|nets|layer`` and
        evidence_hash: Local-evidence hash -- the same identities
            ``kct check`` findings carry (Issue #5946), attached by
            :func:`kicad_tools.explain.mistake_waivers.annotate_mistakes`.
        waived / waiver_reason / waiver_issue: Set when an evidence-bound
            waiver acknowledged the finding.
        stale_waiver_hash / stale_waiver_reason: Set when a waiver names the
            finding but its evidence changed since review (the finding stays
            active).
    """

    category: MistakeCategory
    severity: str  # "error", "warning", "info"
    title: str
    components: list[str]
    explanation: str
    fix_suggestion: str
    location: tuple[float, float] | None = None
    learn_more_url: str | None = None
    rule_id: str = ""
    key: str | None = None
    evidence_hash: str | None = None
    waived: bool = False
    waiver_reason: str | None = None
    waiver_issue: str | None = None
    stale_waiver_hash: str | None = None
    stale_waiver_reason: str | None = None

    @property
    def is_active(self) -> bool:
        """True unless an (unstale) waiver acknowledged the finding."""
        return not self.waived

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        data: dict[str, Any] = {
            "category": self.category.value,
            "severity": self.severity,
            "title": self.title,
            "components": self.components,
            "location": self.location,
            "explanation": self.explanation,
            "fix_suggestion": self.fix_suggestion,
            "learn_more_url": self.learn_more_url,
            # Issue #6006: the same identity / evidence / waiver fields as a
            # ``kct check`` finding.
            "rule_id": self.rule_id or None,
            "key": self.key,
            "evidence_hash": self.evidence_hash,
            "status": "waived" if self.waived else self.severity,
            "waived": self.waived,
        }
        if self.waived:
            data["waiver_reason"] = self.waiver_reason
            data["waiver_issue"] = self.waiver_issue
        if self.stale_waiver_hash is not None:
            from kicad_tools.validate.evidence import is_outdated_evidence_hash

            data["waiver_status"] = "stale"
            data["stale_waiver_evidence_hash"] = self.stale_waiver_hash
            data["stale_waiver_reason"] = self.stale_waiver_reason
            data["stale_waiver_cause"] = (
                "outdated_evidence_version"
                if is_outdated_evidence_hash(self.stale_waiver_hash)
                else "evidence_changed"
            )
        return data

    def format_tree(self) -> str:
        """Format as a tree structure for terminal output."""
        lines = [f"[{self.severity.upper()}] {self.title}"]
        lines.append(f"├─ Components: {', '.join(self.components)}")
        if self.location:
            lines.append(f"├─ Location: ({self.location[0]:.2f}, {self.location[1]:.2f}) mm")
        lines.append(f"├─ Problem: {self.explanation}")
        lines.append(f"├─ Fix: {self.fix_suggestion}")
        if self.learn_more_url:
            lines.append(f"└─ Learn more: {self.learn_more_url}")
        return "\n".join(lines)


class CheckIncomplete(Exception):
    """Raised by :meth:`MistakeCheck.check` to report partial coverage.

    Issue #4899 mirrors the #4011 ``lvs`` vacuity-guard discipline for
    ``detect_mistakes``: a check that lacked the input data it needed to
    reach a verdict (e.g. a schematic-dependent check with no sibling
    schematic file next to the PCB) must say so explicitly rather than
    silently returning an empty ``list[Mistake]`` -- which is
    indistinguishable from "I looked and found nothing wrong".

    This is deliberately **not** the same signal as "I ran and found zero
    applicable components" (e.g. a board with no crystals) -- that is a
    legitimate clean run and should return ``[]`` normally, not raise.
    ``CheckIncomplete`` is reserved for "I could not evaluate this at
    all".

    Args:
        reason: Human-readable explanation of what prerequisite was
            missing, surfaced verbatim in :attr:`CheckCoverage.reason`.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass
class CheckCoverage:
    """Coverage outcome for a single :class:`MistakeCheck` run (issue #4899).

    Mirrors the ``SubCheckResult`` pattern used by ``kct check``'s meta
    rollup (#3750/#4011): findings (``Mistake``) and coverage
    (``CheckCoverage``) are reported on separate channels so an
    ``incomplete`` check can never be mistaken for "ran clean".
    """

    check_name: str
    category: MistakeCategory
    status: Literal["ran", "incomplete"]
    reason: str | None = None
    rule_id: str | None = None  # Issue #6006: see :func:`mistake_rule_id`

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "check_name": self.check_name,
            "category": self.category.value,
            "status": self.status,
            "reason": self.reason,
            "rule_id": self.rule_id,
        }


class MistakeCheck(Protocol):
    """Protocol for mistake detection checks.

    Each check implementation must provide:
    - category: The MistakeCategory this check relates to
    - check(pcb): Method that returns list of detected mistakes

    A check that cannot reach a verdict because required input data is
    unavailable (e.g. no sibling schematic for a schematic-dependent
    check) should raise :class:`CheckIncomplete` instead of returning
    ``[]`` -- see that class's docstring for the "incomplete" vs.
    "ran clean" distinction.
    """

    category: MistakeCategory

    def check(self, pcb: PCB) -> list[Mistake]:
        """Run the check on a PCB and return detected mistakes.

        Raises:
            CheckIncomplete: If the check could not run to completion
                because required input data was unavailable.
        """
        ...


def mistake_rule_id(check: object) -> str:
    """Stable rule id of a mistake check (Issue #6006).

    ``mistake.`` plus the snake-cased class name without its ``Check``
    suffix (``BypassCapDistanceCheck`` -> ``mistake.bypass_cap_distance``),
    unless the check declares its own ``rule_id``.  The ``mistake.`` prefix
    keeps these ids apart from ``kct check`` rule ids in a shared waivers
    sidecar.
    """
    from kicad_tools.validate.rules.waivers import MISTAKE_RULE_PREFIX

    declared = getattr(check, "rule_id", None)
    if isinstance(declared, str) and declared:
        return (
            declared if declared.startswith(MISTAKE_RULE_PREFIX) else MISTAKE_RULE_PREFIX + declared
        )
    name = type(check).__name__
    if name.endswith("Check") and len(name) > len("Check"):
        name = name[: -len("Check")]
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name).lower()
    return MISTAKE_RULE_PREFIX + snake


class MistakeDetector:
    """Detect common PCB design mistakes.

    Runs a configurable set of checks against a PCB and returns
    all detected mistakes sorted by severity.

    Example:
        >>> detector = MistakeDetector()
        >>> mistakes = detector.detect(pcb)
        >>> for m in mistakes:
        ...     print(f"[{m.severity}] {m.title}")
    """

    def __init__(self, checks: list[MistakeCheck] | None = None):
        """Initialize detector with optional custom checks.

        Args:
            checks: List of MistakeCheck implementations. If None,
                    uses the default set of built-in checks.
        """
        if checks is None:
            checks = get_default_checks()
        self._checks = checks

    @property
    def checks(self) -> list[MistakeCheck]:
        """The list of checks this detector runs."""
        return self._checks

    def detect(self, pcb: PCB) -> list[Mistake]:
        """Run all checks and return detected mistakes.

        Args:
            pcb: The PCB to analyze

        Returns:
            List of Mistake objects sorted by severity (error > warning > info)

        Note:
            A check that raised :class:`CheckIncomplete` contributes no
            mistakes here and is silently skipped -- this method keeps its
            original ``list[Mistake]``-only signature for backward
            compatibility. Callers that need the honest "did every check
            actually run" signal (issue #4899, mirroring #4011) should use
            :meth:`detect_with_coverage` instead, which is what
            ``kct detect-mistakes`` and the ``detect_mistakes`` MCP tool
            use by default.
        """
        mistakes, _coverage = self.detect_with_coverage(pcb)
        return mistakes

    def detect_with_coverage(self, pcb: PCB) -> tuple[list[Mistake], list[CheckCoverage]]:
        """Run all checks and return both mistakes and per-check coverage.

        Args:
            pcb: The PCB to analyze

        Returns:
            ``(mistakes, coverage)`` -- mistakes sorted by severity, and one
            :class:`CheckCoverage` entry per registered check recording
            whether it ran or was ``incomplete`` (issue #4899).
        """
        return self._run(self._checks, pcb)

    def detect_by_category(
        self,
        pcb: PCB,
        category: MistakeCategory,
    ) -> list[Mistake]:
        """Run only checks in a specific category.

        Args:
            pcb: The PCB to analyze
            category: Only run checks in this category

        Returns:
            List of Mistake objects from checks in the specified category
        """
        mistakes, _coverage = self.detect_by_category_with_coverage(pcb, category)
        return mistakes

    def detect_by_category_with_coverage(
        self,
        pcb: PCB,
        category: MistakeCategory,
    ) -> tuple[list[Mistake], list[CheckCoverage]]:
        """Run only checks in a specific category, with coverage (issue #4899).

        Args:
            pcb: The PCB to analyze
            category: Only run checks in this category

        Returns:
            ``(mistakes, coverage)`` for checks in the specified category.
        """
        checks = [c for c in self._checks if c.category == category]
        return self._run(checks, pcb)

    @staticmethod
    def _run(checks: list[MistakeCheck], pcb: PCB) -> tuple[list[Mistake], list[CheckCoverage]]:
        """Run *checks* against *pcb*, catching :class:`CheckIncomplete` per-check.

        A check that raises ``CheckIncomplete`` contributes a ``"incomplete"``
        :class:`CheckCoverage` entry (and no mistakes); every other check
        -- including one that ran cleanly and found nothing -- contributes a
        ``"ran"`` entry.
        """
        mistakes: list[Mistake] = []
        coverage: list[CheckCoverage] = []
        for check in checks:
            check_name = type(check).__name__
            rule_id = mistake_rule_id(check)
            try:
                found = check.check(pcb)
            except CheckIncomplete as exc:
                coverage.append(
                    CheckCoverage(
                        check_name=check_name,
                        category=check.category,
                        status="incomplete",
                        reason=exc.reason,
                        rule_id=rule_id,
                    )
                )
                continue
            for m in found:
                if not m.rule_id:
                    m.rule_id = rule_id
            mistakes.extend(found)
            coverage.append(
                CheckCoverage(
                    check_name=check_name,
                    category=check.category,
                    status="ran",
                    reason=None,
                    rule_id=rule_id,
                )
            )

        # Sort by severity: error > warning > info
        severity_order = {"error": 0, "warning": 1, "info": 2}
        mistakes.sort(key=lambda m: severity_order.get(m.severity, 99))
        return mistakes, coverage


def detect_mistakes(pcb: PCB) -> list[Mistake]:
    """Convenience function to detect all mistakes in a PCB.

    This is the main entry point for mistake detection.

    Args:
        pcb: The PCB to analyze

    Returns:
        List of Mistake objects sorted by severity

    Example:
        >>> from kicad_tools.schema.pcb import PCB
        >>> from kicad_tools.explain.mistakes import detect_mistakes
        >>> pcb = PCB.load("my_board.kicad_pcb")
        >>> mistakes = detect_mistakes(pcb)
        >>> print(f"Found {len(mistakes)} potential issues")
    """
    detector = MistakeDetector()
    return detector.detect(pcb)


def detect_mistakes_with_coverage(pcb: PCB) -> tuple[list[Mistake], list[CheckCoverage]]:
    """Detect mistakes *and* report per-check coverage (issue #4899).

    This is the recommended entry point when the caller needs to honor the
    #4011 vacuity-guard discipline: a check that could not run (e.g. a
    schematic-dependent check with no sibling schematic file) must not
    silently look like a clean pass. ``kct detect-mistakes`` and the
    ``detect_mistakes`` MCP tool use this function.

    Args:
        pcb: The PCB to analyze

    Returns:
        ``(mistakes, coverage)`` -- see :meth:`MistakeDetector.detect_with_coverage`.

    Example:
        >>> from kicad_tools.schema.pcb import PCB
        >>> from kicad_tools.explain.mistakes import detect_mistakes_with_coverage
        >>> pcb = PCB.load("my_board.kicad_pcb")
        >>> mistakes, coverage = detect_mistakes_with_coverage(pcb)
        >>> incomplete = [c for c in coverage if c.status == "incomplete"]
        >>> if incomplete:
        ...     print(f"{len(incomplete)} check(s) could not run")
    """
    detector = MistakeDetector()
    return detector.detect_with_coverage(pcb)


def get_default_checks() -> list[MistakeCheck]:
    """Get the default set of mistake checks.

    Returns:
        List of MistakeCheck implementations
    """
    # Import checks here to avoid circular imports
    from .checks import (
        AcidTrapCheck,
        BomFieldHealthCheck,
        BypassCapDistanceCheck,
        CrystalNoiseProximityCheck,
        CrystalTraceLengthCheck,
        DifferentialPairSkewCheck,
        LedSeriesResistorCheck,
        MissingDecouplingCapCheck,
        PowerTraceWidthCheck,
        PullUpResistorCheck,
        ThermalPadConnectionCheck,
        TombstoningRiskCheck,
        ViaInPadCheck,
    )

    return [
        BypassCapDistanceCheck(),
        CrystalTraceLengthCheck(),
        CrystalNoiseProximityCheck(),
        DifferentialPairSkewCheck(),
        PowerTraceWidthCheck(),
        ThermalPadConnectionCheck(),
        ViaInPadCheck(),
        AcidTrapCheck(),
        TombstoningRiskCheck(),
        # Issue #4899: Konnect design-review audit parity (net-new checks).
        MissingDecouplingCapCheck(),
        PullUpResistorCheck(),
        LedSeriesResistorCheck(),
        BomFieldHealthCheck(),
    ]


# =============================================================================
# Utility functions for checks
# =============================================================================


def distance(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """Calculate Euclidean distance between two points.

    Args:
        p1: First point (x, y) in mm
        p2: Second point (x, y) in mm

    Returns:
        Distance in mm
    """
    return math.sqrt((p2[0] - p1[0]) ** 2 + (p2[1] - p1[1]) ** 2)


def trace_length(segments: list[Any]) -> float:
    """Calculate total length of trace segments.

    Args:
        segments: List of Segment objects

    Returns:
        Total length in mm
    """
    total = 0.0
    for seg in segments:
        total += distance(seg.start, seg.end)
    return total


def power_pin_nets(pcb: PCB) -> set[str]:
    """Return nets with *evidence* of being a power rail (issue #5939).

    Evidence is a pad whose schematic pin electrical type (``pintype``,
    copied onto the board by KiCad 6+) is ``power_in`` or ``power_out``.
    Ground nets carry ``power_in`` pins too, so ground-named nets are
    excluded -- this set answers "non-ground supply rail?", matching
    :func:`is_power_net`.

    A switching regulator's switch node is not a rail even though KiCad
    types its ``SW``/``LX`` pin ``power_out`` (issue #5998): pins named as
    switch-node or bootstrap pins (``pinfunction``, see
    :func:`~kicad_tools.router.net_class.is_power_rail_pin`) are not
    evidence, and a net that carries a switch-node pin is never reported.
    When a pad has no ``pinfunction``, a switch-node *net* name (``SW``,
    ``BUCK_SW``) disqualifies its ``power_out`` pins instead.

    Boards without pin-type data (pre-KiCad-6, or generated without a
    schematic) yield an empty set and the checks fall back to
    :func:`is_power_net`'s anchored name heuristic.
    """
    nets: set[str] = set()
    switch_nodes: set[str] = set()
    for fp in pcb.footprints:
        for pad in fp.pads:
            net = pad.net_name
            if not net or is_unconnected_net_name(net) or is_ground_rail_name(net):
                continue
            pinfunction = getattr(pad, "pinfunction", "") or ""
            if is_switch_node_name(pinfunction):
                switch_nodes.add(net)
                continue
            pintype = getattr(pad, "pintype", "") or ""
            if not is_power_rail_pin(pintype, pinfunction):
                continue
            if not pinfunction and pintype == "power_out" and is_switch_node_name(net):
                continue
            nets.add(net)
    return nets - switch_nodes


def is_power_net(net_name: str, power_pin_evidence: Collection[str] | None = None) -> bool:
    """Check whether a net is a (non-ground) power rail.

    Prefers real evidence over the name: when *power_pin_evidence* (from
    :func:`power_pin_nets`) contains the net, it is power.  Otherwise the
    shared whole-token name heuristic
    :func:`kicad_tools.router.net_class.is_power_rail_name` decides, after
    stripping the hierarchical sheet path.  KiCad ``unconnected-(...)`` nets
    are never power.

    Issue #5939: this used to be a substring match, so ``USB_D+``,
    ``ISENSE_A+``, ``/PG_3V3``, ``TRIGGER_5V`` and
    ``unconnected-(U11-D+-Pad2)`` were all "power".

    Args:
        net_name: The net name to check
        power_pin_evidence: Optional set of net names known to carry a
            ``power_in``/``power_out`` pin.

    Returns:
        True if the net is a power net
    """
    if not net_name or is_unconnected_net_name(net_name):
        return False
    if power_pin_evidence is not None and net_name in power_pin_evidence:
        return True
    return is_power_rail_name(net_name)


def is_ground_net(net_name: str) -> bool:
    """Check whether a net name looks like a ground rail.

    Whole-token match after stripping the hierarchical sheet path
    (``GND``, ``/AGND``, ``GNDA``, ``SHIELD_GND``, ``VSS``); KiCad
    ``unconnected-(...)`` nets are never ground (issue #5939).

    Args:
        net_name: The net name to check

    Returns:
        True if the net appears to be a ground net
    """
    return is_ground_rail_name(net_name)


def is_supply_net(net_name: str, power_pin_evidence: Collection[str] | None = None) -> bool:
    """True for any supply rail: power (:func:`is_power_net`) or ground."""
    return is_power_net(net_name, power_pin_evidence) or is_ground_net(net_name)


def is_bypass_cap(reference: str, value: str) -> bool:
    """Check if a component appears to be a bypass capacitor.

    Args:
        reference: Component reference (e.g., "C5")
        value: Component value (e.g., "100nF")

    Returns:
        True if the component appears to be a bypass capacitor
    """
    if not reference.upper().startswith("C"):
        return False

    # Check for typical bypass cap values
    bypass_values = ["100n", "0.1u", "10n", "1u", "4.7u", "10u"]
    lower_value = value.lower().replace(" ", "").replace("f", "")
    return any(bv in lower_value for bv in bypass_values)


# --- IC classification (issue #5970) ---------------------------------------
#
# Shared by the decoupling-related checks: a bypass capacitor decouples an
# *IC* supply pin, so the "IC" side of the pairing must be a real IC -- not a
# resistor, another capacitor, a connector or a fuse that merely touches the
# same rail.

#: Reference prefixes that positively identify an IC (``U3``, ``IC1``).
#: Matches :data:`kicad_tools.optim.clustering.IC_PREFIXES`.
IC_REFERENCE_PREFIXES = frozenset({"U", "IC"})

#: Minimum pad count for a footprint to be considered an IC rather than a
#: passive/connector (used by :class:`MissingDecouplingCapCheck` and by the
#: package-based fallback of :func:`is_ic_footprint`).
MIN_IC_PADS = 4

#: Reference prefixes that are never ICs needing a bypass cap of their own:
#: R/C/L/D/Y/X are passives; J/SW/TP/FB are connectors, switches, test
#: points and ferrite beads; Q is a discrete transistor (an 8-pad SO-8 power
#: MOSFET on a rail is not an IC supply pin); MH/FID are mechanical.
NON_IC_REFERENCE_PREFIXES = (
    "R",
    "C",
    "L",
    "D",
    "Y",
    "X",
    "J",
    "Q",
    "SW",
    "TP",
    "FB",
    "MH",
    "FID",
)

# Extra exact prefixes the positive classifier also rejects (fuses,
# varistors, batteries, relays, LEDs, plugs) -- see :func:`is_ic_footprint`.
# Issue #5995 review added connectors (``CONN``/``CON``), board-to-board
# connectors (``BRD``), shield cans (``SH``/``SHLD``), mechanical hardware
# (``H``/``MP``), transformers/magnetics (``T``/``TR``), antennas and
# miscellaneous electrical parts (``E``/``ANT``).  These are *exact*-prefix
# matches (``T1`` but not ``TP1``), unlike :data:`NON_IC_REFERENCE_PREFIXES`
# which :mod:`~kicad_tools.explain.checks.decoupling` matches with
# ``startswith``.
_EXTRA_NON_IC_PREFIXES = frozenset(
    {
        "F",
        "RV",
        "BT",
        "K",
        "LED",
        "P",
        "CN",
        "RN",
        "RSH",
        "CONN",
        "CON",
        "BRD",
        "SH",
        "SHLD",
        "H",
        "MP",
        "T",
        "TR",
        "E",
        "ANT",
    }
)

# KiCad footprint libraries that hold IC packages / modules.  Matched on the
# exact library name, or -- for the families listed in
# ``_IC_FOOTPRINT_LIBRARY_PREFIXES`` -- on the library-name prefix
# (``Sensor_Humidity``, ``Converter_DCDC`` ...).  Issue #5995 added the
# oscillator, sensor and DC/DC-converter libraries: every footprint in them is
# an active part with its own supply pin.  All library comparisons are
# case-insensitive (both sides upper-cased), so ``oscillator:`` and
# ``connector_generic:`` classify like their canonical spellings.
_IC_FOOTPRINT_LIBRARIES = frozenset(
    {
        "PACKAGE_SO",
        "PACKAGE_QFP",
        "PACKAGE_DFN_QFN",
        "PACKAGE_BGA",
        "PACKAGE_CSP",
        "PACKAGE_DIP",
        "PACKAGE_SON",
        "PACKAGE_LGA",
        "PACKAGE_LCC",
        "RF_MODULE",
        "MODULE",
        "OSCILLATOR",
    }
)
_IC_FOOTPRINT_LIBRARY_PREFIXES = ("SENSOR", "CONVERTER_")

# KiCad footprint libraries that never hold an IC (issue #5995).  A footprint
# from one of these is rejected whatever its reference prefix, pad count or
# package-name tokens say: ``Button_Switch_THT:SW_DIP_SPSTx04`` is a DIP
# *switch*, not a DIP IC, and ``Connector_*`` / passive libraries never need
# a bypass cap of their own.
_NON_IC_FOOTPRINT_LIBRARY_PREFIXES = (
    "BUTTON_SWITCH",
    "CONNECTOR",
    "TERMINALBLOCK",
    "RESISTOR",
    "CAPACITOR",
    "INDUCTOR",
    "CRYSTAL",
    "FUSE",
    "VARISTOR",
    "POTENTIOMETER",
    "LED",
    "DIODE",
    "RELAY",
    "JUMPER",
    "TESTPOINT",
    "MOUNTING",  # MountingHole, MountingEquipment, Mounting_Wuerth ...
    "FIDUCIAL",
    "BATTERY",
    "BUZZER_BEEPER",
    "FERRITE",
    "FILTER",
    "TRANSFORMER",
    "SYMBOL",
    "NETTIE",
    "RF_SHIELDING",
    "HEATSINK",
)

# Package-name (footprint name after the ``:``) prefixes of connectors and
# headers.  Catches a bare ``PinHeader_2x20_P2.54mm`` with no library, or a
# project-local ``myproj:Hirose_DF40_40`` / ``myproj:B2B_40pin``.  Matched
# against the upper-cased start of the package name only, so an IC in a
# socket (``DIP-28_W7.62mm_Socket``) is unaffected.
_CONNECTOR_PACKAGE_PREFIXES = (
    "PINHEADER",
    "PINSOCKET",
    "PIN_HEADER",
    "PIN_SOCKET",
    "HEADER",
    "BOXHEADER",
    "IDC",
    "CONN",  # Conn_*, Connector_*, CONN-...
    "B2B",
    "BOARDTOBOARD",
    "BOARD_TO_BOARD",
    "FPC",
    "FFC",
    "HIROSE",
    "MOLEX",
    "JST",
    "SAMTEC",
    "TERMINALBLOCK",
)

# Package-name token suffixes that identify IC packages: QFN/VQFN, QFP/LQFP,
# SOIC, SOP/SSOP/TSSOP/MSOP/HVSSOP/TSOP, SON/WSON, DFN, BGA, LGA, CSP, DIP.
_IC_PACKAGE_SUFFIXES = (
    "QFN",
    "QFP",
    "SOIC",
    "SOP",
    "SON",
    "DFN",
    "BGA",
    "LGA",
    "CSP",
    "DIP",
    "PLCC",
)
_SOT_MULTIPIN_RE = re.compile(r"T?SOT-?23-[5-8]\b")

#: Reference prefixes of linear/switching regulators and power-supply
#: modules (IEEE 315 ``VR``; common ``REG``/``VREG``/``LDO``/``PS``/``PSU``).  Issue
#: #5995: a ``VR1`` in ``SOT-223`` is an IC, but ``VR`` is also used for
#: trimmers in some regions, so these prefixes only count when the package is
#: a regulator package (or any other IC package), never a potentiometer.
REGULATOR_REFERENCE_PREFIXES = frozenset({"VR", "REG", "VREG", "LDO", "PS", "PSU"})

# Power packages regulators ship in (but discrete transistors also do, so a
# ``Q`` part in one of these is still rejected by its prefix).
_REGULATOR_PACKAGE_RE = re.compile(
    r"SOT-?223|SOT-?89|SOT-?23\b|SOT-?3[256]3|SC-?70"
    r"|TO-?252|TO-?263|TO-?220|TO-?92|TO-?277|D2?PAK"
)

#: Reference prefixes of crystal/oscillator parts (IEEE 315 ``Y``; ``X`` is
#: common too).  A passive crystal is not an IC, but an *oscillator* (a
#: powered part with its own VDD pin) in the same class letter is.
_OSCILLATOR_REFERENCE_PREFIXES = frozenset({"X", "Y", "XO", "OSC", "XTAL"})

#: Pad ``pintype`` values (copied from the schematic symbol on "Update PCB
#: from Schematic") that mark a pin as a supply input of an active part.
#: The PCB carries no symbol ``lib_id``; ``pintype`` is the symbol-derived
#: signal that survives into the board file.
_SUPPLY_PIN_TYPES = frozenset({"power_in", "power_out"})

#: Pad count from which a footprint with an unrecognised (project-local)
#: library and a :data:`MODULE_REFERENCE_PREFIXES` reference prefix is taken
#: to be an IC/module -- ``MOD2`` in ``myproj:RPi_CM4``.
_MANY_PADS_IC_THRESHOLD = 16

#: Reference prefixes that may qualify through the many-pad project-library
#: fallback of :func:`is_ic_footprint` (issue #5995 review).  The fallback is a
#: positive *allowlist*: "lots of pads in a custom footprint" is just as true of
#: a 40-pin board-to-board connector, a shield can or Ethernet magnetics, and
#: :class:`~kicad_tools.explain.checks.bypass.BypassCapDistanceCheck` pairs
#: every bypass cap with every IC power pad on the rail, so a false IC here
#: multiplies into many bogus warnings.  The list:
#:
#: * ``MOD`` / ``MODULE`` -- generic module designators (``MOD2`` = RPi CM4);
#: * ``A`` -- IEEE 315 / IEC 81346 "assembly, sub-assembly" (ESP32 / Arduino);
#: * ``M`` -- module in many EDA conventions (rarely a motor with >=16 pads);
#: * ``MCU`` / ``SOM`` / ``CM`` -- microcontroller, system-on-module and
#:   compute-module designators.
#:
#: A genuine module with any other prefix still qualifies through an IC
#: library/package name or a ``power_in`` pad ``pintype``.
MODULE_REFERENCE_PREFIXES = frozenset({"MOD", "MODULE", "A", "M", "MCU", "SOM", "CM"})


def reference_prefix(reference: str) -> str:
    """Leading alphabetic prefix of a reference designator (``"RV2"`` -> ``"RV"``)."""
    match = re.match(r"[A-Za-z]+", reference or "")
    return match.group(0).upper() if match else ""


def _split_footprint_name(footprint_name: str) -> tuple[str, str]:
    library, _, package = (footprint_name or "").rpartition(":")
    return library, package


def _is_ic_library(library: str) -> bool:
    upper = library.upper()
    return upper in _IC_FOOTPRINT_LIBRARIES or upper.startswith(_IC_FOOTPRINT_LIBRARY_PREFIXES)


def _is_non_ic_library(library: str) -> bool:
    return bool(library) and library.upper().startswith(_NON_IC_FOOTPRINT_LIBRARY_PREFIXES)


def _is_connector_package(package: str) -> bool:
    return package.upper().startswith(_CONNECTOR_PACKAGE_PREFIXES)


def _has_ic_package(footprint_name: str, pad_count: int) -> bool:
    """True when the footprint's library or package name is an IC package."""
    library, package = _split_footprint_name(footprint_name)
    if _is_ic_library(library):
        return True
    upper = package.upper()
    tokens = re.split(r"[^A-Z0-9]+", upper)
    if any(tok.endswith(_IC_PACKAGE_SUFFIXES) for tok in tokens if tok):
        return True
    return pad_count >= 5 and bool(_SOT_MULTIPIN_RE.search(upper))


def _has_supply_pintype(fp: Footprint) -> bool:
    return any(getattr(pad, "pintype", "") in _SUPPLY_PIN_TYPES for pad in fp.pads)


def is_ic_footprint(fp: Footprint) -> bool:
    """Positively classify a footprint as an IC (issues #5970, #5995).

    Having a pad on a power rail is **not** evidence: resistors, capacitors,
    connectors and fuses all touch rails.  Reference prefix, footprint
    library/package name, pad count and -- when the board carries it -- the
    symbol-derived pad ``pintype`` are combined:

    * a footprint from a library that never holds ICs (``Button_Switch*``,
      ``Connector*``, ``RF_Shielding``, ``Mounting*``, ``Heatsink``, passives,
      ``Crystal`` ...; compared case-insensitively) is never an IC -- this
      check runs first, so even a ``U1`` in ``Connector_Generic:`` is not an
      IC (it is a connector mis-annotated as ``U``, or a plug-in module whose
      own decoupling lives on the module);
    * prefix ``U`` / ``IC`` with at least 3 pads is an IC (a SOT-23 LDO);
    * an oscillator: ``X``/``Y``/``OSC`` prefix in the ``Oscillator:``
      library (or an ``Oscillator`` package name), or with a ``power_in``
      pad, and at least 3 pads -- a passive ``Crystal:`` part is not;
    * a regulator: ``VR``/``REG``/``VREG``/``LDO``/``PS``/``PSU`` prefix with at
      least 3 pads in a regulator package (SOT-223, TO-252/DPAK, SOT-89 ...),
      an IC package, or with a ``power_in`` pad;
    * any other prefix that is not a known passive/connector/discrete/
      mechanical prefix (``CONN``, ``BRD``, ``SH``, ``H``, ``MP``, ``T``,
      ``E``, ``ANT`` ...), whose package name does not look like a connector
      (``PinHeader_*``, ``Conn_*``, ``B2B*``, ``Hirose*`` ...), with at least
      :data:`MIN_IC_PADS` pads and an IC package or IC library
      (``Package_SO:``, ``...QFN...``, ``Sensor*:``, ``Oscillator:``,
      ``Converter_*:``, ``RF_Module:`` ...) or a ``power_in`` pad -- so an
      ``A1`` module, ``MCU1`` or ``S1`` sensor counts;
    * finally a :data:`MODULE_REFERENCE_PREFIXES` prefix (``MOD``, ``A``,
      ``M``, ``MCU``, ``SOM``, ``CM``) with at least 16 pads in an
      unrecognised (project-local) library -- ``MOD2`` in ``myproj:RPi_CM4``.
    """
    prefix = reference_prefix(fp.reference)
    pad_count = len(fp.pads)
    library, package = _split_footprint_name(fp.name)
    if _is_non_ic_library(library):
        return False
    if prefix in IC_REFERENCE_PREFIXES:
        return pad_count >= 3
    if prefix in _OSCILLATOR_REFERENCE_PREFIXES:
        if pad_count < 3:
            return False
        return (
            library.upper() == "OSCILLATOR"
            or "OSCILLATOR" in package.upper()
            or _has_supply_pintype(fp)
        )
    if prefix in REGULATOR_REFERENCE_PREFIXES:
        if pad_count < 3 or "POTENTIOMETER" in package.upper():
            return False
        return (
            bool(_REGULATOR_PACKAGE_RE.search(package.upper()))
            or _has_ic_package(fp.name, pad_count)
            or _has_supply_pintype(fp)
        )
    if prefix in NON_IC_REFERENCE_PREFIXES or prefix in _EXTRA_NON_IC_PREFIXES:
        return False
    if pad_count < MIN_IC_PADS or _is_connector_package(package):
        return False
    if _has_ic_package(fp.name, pad_count) or _has_supply_pintype(fp):
        return True
    # A big part in a project-local library (``myproj:RPi_CM4``) is a module
    # only when its prefix says so -- a 40-pin B2B connector has as many pads.
    if prefix not in MODULE_REFERENCE_PREFIXES:
        return False
    known_kicad_library = library.upper().startswith(("PACKAGE_", "CONNECTOR", "BUTTON_SWITCH"))
    return pad_count >= _MANY_PADS_IC_THRESHOLD and not known_kicad_library


def is_crystal(reference: str, footprint: str) -> bool:
    """Check if a component appears to be a crystal or oscillator.

    Args:
        reference: Component reference (e.g., "Y1")
        footprint: Footprint name

    Returns:
        True if the component appears to be a crystal
    """
    ref_upper = reference.upper()
    fp_lower = footprint.lower()

    return (
        ref_upper.startswith("Y")
        or ref_upper.startswith("X")
        or "crystal" in fp_lower
        or "oscillator" in fp_lower
        or "xtal" in fp_lower
    )


def is_differential_pair_net(net_name: str) -> tuple[bool, str | None]:
    """Check if a net is part of a differential pair.

    Args:
        net_name: The net name to check

    Returns:
        Tuple of (is_diff_pair, pair_base_name)
        pair_base_name is None if not a differential pair
    """
    upper_name = net_name.upper()

    # Common differential pair patterns
    patterns = [
        ("USB_D+", "USB_D-", "USB_D"),
        ("USB_DP", "USB_DM", "USB_D"),
        ("D+", "D-", "USB"),
        ("DP", "DM", "USB"),
        ("TX+", "TX-", "TX"),
        ("TXP", "TXN", "TX"),
        ("RX+", "RX-", "RX"),
        ("RXP", "RXN", "RX"),
        ("LVDS+", "LVDS-", "LVDS"),
    ]

    for pos, neg, base in patterns:
        if pos in upper_name or neg in upper_name:
            return True, base

    # Check for generic _P/_N suffix
    if upper_name.endswith("_P") or upper_name.endswith("_N"):
        base = upper_name[:-2]
        return True, base

    return False, None
