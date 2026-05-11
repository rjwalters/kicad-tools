"""
Differential pair routing support for the autorouter.

This module provides:
- DifferentialSignal: Represents one signal (P or N) of a differential pair
- DifferentialPair: A pair of P/N signals with routing constraints
- detect_differential_pairs: Parse net names to identify differential pairs
- DifferentialPairConfig: Configuration for differential pair routing
- should_engage_coupled: Engagement-layer gate for CoupledPathfinder (Phase 2E)

Differential pairs are detected from common naming conventions:
- Plus/minus notation: USB_D+/USB_D-, ETH_TX+/ETH_TX-
- P/N suffix: HDMI_D0_P/HDMI_D0_N, USB3_TX_P/USB3_TX_N
- Positive/negative suffix: CLK_POS/CLK_NEG
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .rules import NetClassRouting


class DifferentialPairType(Enum):
    """Known differential pair signal types with predefined rules."""

    USB2 = "usb2"
    USB3 = "usb3"
    ETHERNET = "ethernet"
    HDMI = "hdmi"
    LVDS = "lvds"
    CUSTOM = "custom"


@dataclass
class DifferentialPairRules:
    """Design rules for a specific differential pair type.

    Attributes:
        spacing: Target spacing between P and N traces in mm
        max_length_delta: Maximum allowed length difference in mm
        trace_width: Recommended trace width in mm
        impedance: Target differential impedance in ohms (for reference)
    """

    spacing: float
    max_length_delta: float
    trace_width: float = 0.2
    impedance: float = 90.0

    @classmethod
    def for_type(cls, pair_type: DifferentialPairType) -> "DifferentialPairRules":
        """Get predefined rules for a differential pair type."""
        rules_map = {
            DifferentialPairType.USB2: cls(
                spacing=0.2, max_length_delta=2.5, trace_width=0.2, impedance=90.0
            ),
            DifferentialPairType.USB3: cls(
                spacing=0.15, max_length_delta=0.5, trace_width=0.2, impedance=90.0
            ),
            DifferentialPairType.ETHERNET: cls(
                spacing=0.2, max_length_delta=2.0, trace_width=0.2, impedance=100.0
            ),
            DifferentialPairType.HDMI: cls(
                spacing=0.15, max_length_delta=0.5, trace_width=0.2, impedance=100.0
            ),
            DifferentialPairType.LVDS: cls(
                spacing=0.15, max_length_delta=0.5, trace_width=0.15, impedance=100.0
            ),
            DifferentialPairType.CUSTOM: cls(
                spacing=0.2, max_length_delta=1.0, trace_width=0.2, impedance=90.0
            ),
        }
        return rules_map.get(pair_type, rules_map[DifferentialPairType.CUSTOM])


@dataclass
class DifferentialSignal:
    """A signal that is part of a differential pair.

    Attributes:
        net_name: Original net name (e.g., "USB_D+")
        net_id: Net ID in the router
        base_name: Base name without polarity suffix (e.g., "USB_D")
        polarity: "P" for positive, "N" for negative
        notation: How the pair was named ("plus_minus", "pn_suffix", "pos_neg")
    """

    net_name: str
    net_id: int
    base_name: str
    polarity: str  # "P" or "N"
    notation: str  # "plus_minus", "pn_suffix", "pos_neg"

    def __hash__(self) -> int:
        return hash((self.net_id, self.base_name, self.polarity))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DifferentialSignal):
            return NotImplemented
        return (
            self.net_id == other.net_id
            and self.base_name == other.base_name
            and self.polarity == other.polarity
        )


@dataclass
class DifferentialPair:
    """A differential pair consisting of P and N signals.

    Attributes:
        name: Base name of the pair (e.g., "USB_D")
        positive: The positive signal (D+, _P, etc.)
        negative: The negative signal (D-, _N, etc.)
        pair_type: Detected or assigned pair type
        rules: Design rules for this pair
        routed_length_p: Length of routed positive trace (populated after routing)
        routed_length_n: Length of routed negative trace (populated after routing)
    """

    name: str
    positive: DifferentialSignal
    negative: DifferentialSignal
    pair_type: DifferentialPairType = DifferentialPairType.CUSTOM
    rules: DifferentialPairRules | None = None
    routed_length_p: float = 0.0
    routed_length_n: float = 0.0

    def __post_init__(self):
        if self.rules is None:
            self.rules = DifferentialPairRules.for_type(self.pair_type)

    @property
    def length_delta(self) -> float:
        """Get the length difference between P and N traces."""
        return abs(self.routed_length_p - self.routed_length_n)

    @property
    def is_length_matched(self) -> bool:
        """Check if the pair meets length matching requirements."""
        if self.rules is None:
            return True
        return self.length_delta <= self.rules.max_length_delta

    def get_net_ids(self) -> tuple[int, int]:
        """Get net IDs as (positive, negative) tuple."""
        return (self.positive.net_id, self.negative.net_id)

    def __str__(self) -> str:
        notation_map = {
            "plus_minus": f"{self.name}+/-",
            "pn_suffix": f"{self.name}_P/N",
            "pos_neg": f"{self.name}_POS/NEG",
        }
        return notation_map.get(self.positive.notation, self.name)


# =============================================================================
# REGEX PATTERNS FOR DIFFERENTIAL PAIR DETECTION
# =============================================================================
#
# CANONICAL SOURCE OF TRUTH (Issue #2558, Epic #2556 Phase 1B).
#
# Two pattern sets historically existed in this codebase:
#   - ``diffpair.py`` (this module) -- structured tuple-returning matcher
#   - ``router/net_class.py`` NET_CLASS_PATTERNS[NetClass.DIFFERENTIAL] --
#     broad regex used by classify_net()
# The two drifted: net_class.py had ``[_-]?[PN]$`` which mis-classified
# names like ``USB_CC1`` (the trailing digit forces the classifier into
# DIFFERENTIAL) while diffpair.py correctly returned ``None``.
#
# To prevent further drift, ``net_class.is_differential_pair_name`` now
# delegates to ``parse_differential_signal`` (defined here) so there is
# a single source of truth for what counts as a diff-pair name.

# Pattern 1: Plus/minus notation - USB_D+, ETH_TX-, etc.
# Matches the base name and the +/- suffix
_PLUS_MINUS_PATTERN = re.compile(r"^(.+)([+-])$")

# Pattern 2: P/N suffix notation - HDMI_D0_P, USB3_TX_N, etc.
# Note: _DP/_DN is handled SEPARATELY below so the "_D" stays part of the
# base name (USB_DP -> base="USB_D", not "USB"). See issue #2558 / A6.
_PN_SUFFIX_PATTERN = re.compile(r"^(.+)_([PN])$", re.IGNORECASE)

# Pattern 2b: _DP/_DN suffix notation - USB_DP, CLK_DP, etc.
# The "_D" is part of the base name so a board can carry BOTH
# ``USB_D+/USB_D-`` AND ``USB_DP/USB_DN`` (different pairs) without
# colliding on a shared base of ``USB``.
_DP_DN_SUFFIX_PATTERN = re.compile(r"^(.+)_(D[PN])$", re.IGNORECASE)

# Pattern 3: POS/NEG suffix notation - CLK_POS, CLK_NEG
_POS_NEG_PATTERN = re.compile(r"^(.+)_(POS|NEG)$", re.IGNORECASE)


# =============================================================================
# REFUSAL PATTERNS -- known single-ended pairs that must NOT be inferred as
# differential pairs from suffixes (Issue #2558, Epic #2556 Phase 1B).
# =============================================================================
#
# These pin pairs look diff-pair-ish (matching numbers, similar prefixes)
# but are SINGLE-ENDED by spec.  Examples:
#   - USB-C ``CC1``/``CC2``: orientation/role detection (analog).
#   - USB-C ``SBU1``/``SBU2``: sideband use (alt-mode mux).
# A designer can still force pairing via the explicit
# ``NetClassRouting.diffpair_partner`` field -- the refusal list applies
# only to suffix INFERENCE.
_SINGLE_ENDED_REFUSAL_PATTERN = re.compile(r"^(.+_)?(CC|SBU)\d+$", re.IGNORECASE)


# =============================================================================
# POWER-RAIL FILTER -- prevent ``VCC_NEG`` / ``VBUS_POS`` etc. from being
# matched as ``pos_neg`` diff-pair signals (Issue #2558 / A5).
# =============================================================================
#
# The base-name prefixes here are taken from
# ``router/net_class.py::NET_CLASS_PATTERNS[NetClass.POWER]`` (the same
# set used by classify_net).  Names whose base matches a power-rail
# prefix are excluded from POS/NEG suffix inference.
_POWER_RAIL_PREFIX_PATTERN = re.compile(
    r"^(VCC|VDD|VBUS|VIN|VOUT|PWR|POWER|AVDD|DVDD|"
    r"PVDD|PVCC|VBAT|VCORE|VCAP|VIO|"
    r"VMOTOR|VMOT|VMAIN|VPWR|VDRIVE|VACT|VSRV)(_.*)?$",
    re.IGNORECASE,
)


def _is_power_rail_base(base_name: str) -> bool:
    """Return True if ``base_name`` looks like a power-rail name.

    Used to refuse ``VCC_NEG``/``VBUS_POS``-style false positives.
    """
    return bool(_POWER_RAIL_PREFIX_PATTERN.match(base_name))


def is_single_ended_refused(net_name: str) -> bool:
    """Return True if ``net_name`` matches the single-ended refusal list.

    Refusal applies to suffix INFERENCE only -- explicit declarations
    (via ``NetClassRouting.diffpair_partner``) and KiCad group
    declarations bypass this check (designer override wins).

    Note: a SEPARATE engagement-layer refusal in
    :func:`should_engage_coupled` re-applies this pattern at the
    CoupledPathfinder dispatch site so an explicit declaration cannot
    force coupling on electrically-single-ended pins like USB-C CC1/CC2.
    See Issue #2638 / Epic #2556 Phase 2E.
    """
    return bool(_SINGLE_ENDED_REFUSAL_PATTERN.match(net_name))


# =============================================================================
# ENGAGEMENT-LAYER GATE -- decides whether CoupledPathfinder runs for a pair
# (Issue #2638, Epic #2556 Phase 2E).
# =============================================================================


def _lookup_net_class(
    net_name: str,
    net_class_routing: dict | None,
    net_to_class: dict | None,
) -> "NetClassRouting | None":
    """Resolve the :class:`NetClassRouting` for ``net_name``.

    Supports two key conventions in ``net_class_routing``:

    1. ``class_name -> NetClassRouting`` paired with a
       ``net_to_class: {net_name: class_name}`` map -- the layered
       detector's convention (see ``diffpair_detection.py``).
    2. ``net_name -> NetClassRouting`` directly -- the autorouter's
       ``net_class_map`` convention (see ``router/core.py``).

    The function tries convention (1) first when ``net_to_class`` is
    supplied, then falls back to convention (2).  Returns ``None`` if
    neither lookup yields a match.
    """
    if not net_class_routing:
        return None

    # Convention 1: class_name-keyed lookup via net_to_class.
    if net_to_class is not None:
        class_name = net_to_class.get(net_name)
        if class_name is not None:
            nc = net_class_routing.get(class_name)
            if nc is not None:
                return nc

    # Convention 2: net_name-keyed lookup (autorouter.net_class_map style).
    return net_class_routing.get(net_name)


def should_engage_coupled(
    pair: "DifferentialPair",
    net_class_routing: dict | None,
    net_to_class: dict | None = None,
) -> tuple[bool, str]:
    """Decide whether CoupledPathfinder should engage on ``pair``.

    Phase 2E (Issue #2638, Epic #2556) refines diff-pair engagement from
    "always run coupled when ``--differential-pairs`` is on" to "run
    coupled only when the net class explicitly opts in AND the pair is
    not single-ended-by-spec."  This is the engagement-layer counterpart
    to suffix-time refusal in :func:`is_single_ended_refused`.

    The single-ended refusal here fires regardless of detection source.
    The #2527 case (designer accidentally declares
    ``diffpair_partner="USB_CC2"`` on a USB_CC1 net class) is caught
    here even though the explicit declaration bypassed the suffix-time
    refusal: USB-C CC1/CC2 are orientation pins, not a coupled pair,
    and routing them as a coupled pair would be electrically wrong.

    Args:
        pair: The detected :class:`DifferentialPair`.
        net_class_routing: Map of either ``{class_name: NetClassRouting}``
            or ``{net_name: NetClassRouting}`` (see
            :func:`_lookup_net_class`).  When ``None`` or empty, no class
            opted in -> ``(False, "no_class_match")``.
        net_to_class: Optional ``{net_name: class_name}`` lookup used in
            tandem with the class-name-keyed convention.

    Returns:
        ``(engaged, reason)`` where ``reason`` is one of:

        * ``"engaged"`` -- proceed with coupled routing.
        * ``"single_ended_refusal"`` -- both nets match
          :func:`is_single_ended_refused`.  Coupling them would be
          electrically wrong (USB-C CC1/CC2, SBU1/SBU2).  Fires even
          when explicit declaration would otherwise bypass suffix-time
          refusal.
        * ``"opt_in_disabled"`` -- a class match exists but
          ``coupled_routing`` is ``False`` on the matched class.
        * ``"no_class_match"`` -- neither net resolves to a
          :class:`NetClassRouting`.  Treated as disabled.
    """
    # 1. Engagement-layer single-ended refusal (#2527 lesson).  Fires
    #    regardless of detection source -- explicit declarations cannot
    #    force coupling on electrically-single-ended pins.
    p_name = pair.positive.net_name
    n_name = pair.negative.net_name
    if is_single_ended_refused(p_name) and is_single_ended_refused(n_name):
        return False, "single_ended_refusal"

    # 2. Look up the net class for either half.  Either side opting in
    #    is sufficient -- this mirrors the one-sided-declaration policy
    #    in ``_gather_explicit_pairs`` (#2558).
    p_class = _lookup_net_class(p_name, net_class_routing, net_to_class)
    n_class = _lookup_net_class(n_name, net_class_routing, net_to_class)

    if p_class is None and n_class is None:
        return False, "no_class_match"

    p_opt = bool(p_class is not None and getattr(p_class, "coupled_routing", False))
    n_opt = bool(n_class is not None and getattr(n_class, "coupled_routing", False))

    if p_opt or n_opt:
        return True, "engaged"

    return False, "opt_in_disabled"


def parse_differential_signal(net_name: str) -> tuple[str, str, str] | None:
    """Parse a net name to extract differential pair information.

    Args:
        net_name: The net name to parse

    Returns:
        Tuple of (base_name, polarity, notation) if this is a differential signal,
        None otherwise. polarity is "P" for positive, "N" for negative.
        notation is one of: "plus_minus", "pn_suffix", "pos_neg"

    Refuses (returns None) for:
      - Names matching the single-ended refusal pattern (CC1/CC2, SBU1/SBU2,
        prefix variants like ``USB_CC1``).  See Issue #2558.
      - ``pos_neg`` matches whose base name is a known power rail prefix
        (e.g. ``VCC_NEG``, ``VBUS_POS``).  See Issue #2558.
    """
    # Reject known single-ended pairs up front -- suffix inference only.
    # Explicit declarations and KiCad group declarations bypass this in
    # ``detect_diff_pairs``.
    if is_single_ended_refused(net_name):
        return None

    # Try plus/minus notation first (most common for USB, etc.)
    match = _PLUS_MINUS_PATTERN.match(net_name)
    if match:
        base_name = match.group(1)
        polarity = "P" if match.group(2) == "+" else "N"
        return (base_name, polarity, "plus_minus")

    # Try _DP/_DN suffix notation BEFORE the plain _P/_N pattern so the
    # "D" stays part of the base name (USB_DP -> base="USB_D"), avoiding
    # the collision-with-USB_D+/USB_D- bug noted in #2558 / A6.
    match = _DP_DN_SUFFIX_PATTERN.match(net_name)
    if match:
        base_name = match.group(1) + "_D"
        suffix = match.group(2).upper()
        polarity = "P" if suffix == "DP" else "N"
        return (base_name, polarity, "pn_suffix")

    # Try plain P/N suffix notation (HDMI, USB3, etc.)
    match = _PN_SUFFIX_PATTERN.match(net_name)
    if match:
        base_name = match.group(1)
        suffix = match.group(2).upper()
        polarity = "P" if suffix == "P" else "N"
        return (base_name, polarity, "pn_suffix")

    # Try POS/NEG suffix notation -- but reject power rails like
    # ``VCC_NEG`` / ``VBUS_POS`` (Issue #2558 / A5).
    match = _POS_NEG_PATTERN.match(net_name)
    if match:
        base_name = match.group(1)
        if _is_power_rail_base(base_name):
            return None
        polarity = "P" if match.group(2).upper() == "POS" else "N"
        return (base_name, polarity, "pos_neg")

    return None


def detect_differential_signals(
    net_names: dict[int, str],
) -> list[DifferentialSignal]:
    """Detect differential pair signals from net names.

    Args:
        net_names: Mapping of net ID to net name

    Returns:
        List of detected DifferentialSignal objects
    """
    signals: list[DifferentialSignal] = []

    for net_id, net_name in net_names.items():
        parsed = parse_differential_signal(net_name)
        if parsed:
            base_name, polarity, notation = parsed
            signals.append(
                DifferentialSignal(
                    net_name=net_name,
                    net_id=net_id,
                    base_name=base_name,
                    polarity=polarity,
                    notation=notation,
                )
            )

    return signals


def _detect_pair_type(base_name: str) -> DifferentialPairType:
    """Detect the differential pair type from the base name.

    Args:
        base_name: Base name of the differential pair

    Returns:
        Detected DifferentialPairType
    """
    name_upper = base_name.upper()

    # USB detection
    if "USB" in name_upper:
        if "USB3" in name_upper or "SS" in name_upper:  # SuperSpeed
            return DifferentialPairType.USB3
        return DifferentialPairType.USB2

    # Ethernet detection
    if any(eth in name_upper for eth in ["ETH", "ETHERNET", "RGMII", "SGMII", "MDI"]):
        return DifferentialPairType.ETHERNET

    # HDMI detection
    if "HDMI" in name_upper or "TMDS" in name_upper:
        return DifferentialPairType.HDMI

    # LVDS detection
    if "LVDS" in name_upper:
        return DifferentialPairType.LVDS

    return DifferentialPairType.CUSTOM


def group_differential_pairs(
    signals: list[DifferentialSignal],
) -> list[DifferentialPair]:
    """Group differential signals into pairs.

    Issue #2558 / Epic #2556 Phase 1B: signals are grouped by both
    base name AND notation so a board carrying both ``USB_D+/USB_D-``
    (plus_minus) and ``USB_DP/USB_DN`` (pn_suffix) -- which share the
    base ``USB_D`` after the _DP/_DN base-name fix -- still yields
    two distinct pairs instead of collapsing into one.

    Args:
        signals: List of DifferentialSignal objects

    Returns:
        List of DifferentialPair objects (only complete pairs)
    """
    # Group by (base_name, notation) so different notations produce
    # different pairs even when they share a base name.
    by_key: dict[tuple[str, str], dict[str, DifferentialSignal]] = {}

    for signal in signals:
        key = (signal.base_name, signal.notation)
        if key not in by_key:
            by_key[key] = {}
        by_key[key][signal.polarity] = signal

    # Create pairs where both P and N exist
    pairs: list[DifferentialPair] = []

    for (base_name, _notation), polarity_map in sorted(by_key.items()):
        if "P" in polarity_map and "N" in polarity_map:
            pair_type = _detect_pair_type(base_name)
            pairs.append(
                DifferentialPair(
                    name=base_name,
                    positive=polarity_map["P"],
                    negative=polarity_map["N"],
                    pair_type=pair_type,
                    rules=DifferentialPairRules.for_type(pair_type),
                )
            )

    return pairs


def detect_differential_pairs(
    net_names: dict[int, str],
) -> list[DifferentialPair]:
    """Detect and group differential pairs from net names.

    This is a convenience function that combines detect_differential_signals
    and group_differential_pairs.

    Args:
        net_names: Mapping of net ID to net name

    Returns:
        List of complete DifferentialPair objects
    """
    signals = detect_differential_signals(net_names)
    return group_differential_pairs(signals)


@dataclass
class DifferentialPairConfig:
    """Configuration for differential pair routing.

    Attributes:
        enabled: Whether differential pair routing is enabled
        auto_detect: Automatically detect pairs from net names
        spacing: Override spacing for all pairs (None = use per-type defaults)
        max_length_delta: Override max length delta (None = use per-type defaults)
        add_serpentines: Add serpentine/meander for length matching
    """

    enabled: bool = False
    auto_detect: bool = True
    spacing: float | None = None
    max_length_delta: float | None = None
    add_serpentines: bool = True

    def get_rules(self, pair_type: DifferentialPairType) -> DifferentialPairRules:
        """Get rules with any config overrides applied."""
        base_rules = DifferentialPairRules.for_type(pair_type)
        return DifferentialPairRules(
            spacing=self.spacing if self.spacing is not None else base_rules.spacing,
            max_length_delta=(
                self.max_length_delta
                if self.max_length_delta is not None
                else base_rules.max_length_delta
            ),
            trace_width=base_rules.trace_width,
            impedance=base_rules.impedance,
        )


@dataclass
class LengthMismatchWarning:
    """Warning for length mismatch in a differential pair.

    Attributes:
        pair: The differential pair with length mismatch
        delta: Actual length difference in mm
        max_allowed: Maximum allowed difference in mm
    """

    pair: DifferentialPair
    delta: float
    max_allowed: float

    def __str__(self) -> str:
        return (
            f"Length mismatch in {self.pair.name}: "
            f"{self.delta:.3f}mm (max allowed: {self.max_allowed:.3f}mm)"
        )


def analyze_differential_pairs(net_names: dict[int, str]) -> dict[str, any]:
    """Analyze net names to provide a differential pair detection summary.

    Args:
        net_names: Mapping of net ID to net name

    Returns:
        Dictionary with analysis results
    """
    signals = detect_differential_signals(net_names)
    pairs = group_differential_pairs(signals)

    # Find unpaired signals
    paired_net_ids = set()
    for pair in pairs:
        paired_net_ids.add(pair.positive.net_id)
        paired_net_ids.add(pair.negative.net_id)

    unpaired_signals = [s for s in signals if s.net_id not in paired_net_ids]

    return {
        "total_pairs": len(pairs),
        "total_signals": len(signals),
        "unpaired_signals": len(unpaired_signals),
        "pairs": [
            {
                "name": str(pair),
                "base_name": pair.name,
                "type": pair.pair_type.value,
                "positive_net": pair.positive.net_name,
                "negative_net": pair.negative.net_name,
                "spacing": pair.rules.spacing if pair.rules else 0,
                "max_delta": pair.rules.max_length_delta if pair.rules else 0,
            }
            for pair in pairs
        ],
        "unpaired": [
            {
                "net_name": s.net_name,
                "polarity": s.polarity,
                "base_name": s.base_name,
            }
            for s in unpaired_signals
        ],
    }
