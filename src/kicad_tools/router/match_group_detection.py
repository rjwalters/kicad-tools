"""
Layered match-group detection (Issue #2689, Epic #2661 Phase 1C).

This module discovers length-match groups (N parallel traces that must
arrive at their destination with matched lengths) from three priority-
ordered sources:

1. **Explicit declaration** -- per-class config via
   :class:`kicad_tools.router.rules.NetClassRouting`
   ``length_match_group`` field (Phase 1A / #2687).  AUTHORITATIVE;
   overrides every other source.
2. **Legacy API** -- groups already registered through
   :meth:`kicad_tools.router.core.Autorouter.add_match_group` /
   :func:`kicad_tools.router.length.create_match_group`.  Surfaced via
   ``LengthTracker.match_groups`` (a ``dict[str, list[int]]``).
3. **Suffix inference** -- opt-in heuristic using the
   :data:`BUS_GROUP_PATTERNS` table to recognise common bus naming
   conventions (DDR ``DQ\\d+``, MIPI ``CSI_DAT\\d+_[PN]``, HDMI
   ``TMDS_D\\d+_[PN]``, generic ``A\\d+``).  Refuses low-confidence
   matches (single-net groups, etc.) the same way
   ``router/diffpair_detection.py`` refuses single-ended pairs.

The output is a list of :class:`MatchGroup` instances annotated with a
:class:`MatchGroupSource` enum so downstream consumers (DRC rule,
serpentine tuner) can apply policy by source if needed.

**Important parallel-development note**: Phase 1B (#2688) builds the
companion ``MatchGroupTracker``.  This module defines its own
``MatchGroup`` and ``MatchGroupSource`` types so it can land
independently; the type shapes are coordinated with Phase 1B so the
two modules can interoperate once both are merged.  See
``MatchGroup``'s docstring for the type contract.

The public entry point :func:`detect_match_groups` mirrors
:func:`kicad_tools.router.diffpair_detection.detect_diff_pairs` (PR
#2558) in shape and semantics.

**Clock sentinel resolution**: when ``NetClassRouting.length_match_reference``
is the literal string ``"clock"``, the resolver iterates over the
group's members and picks the one whose name matches any of the four
clock-discriminating regexes in
:data:`kicad_tools.analysis.trace_length.CRITICAL_NET_PATTERNS` (the
import is deliberate -- a drift-prevention test asserts this module
does NOT reimplement those regexes).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from kicad_tools.analysis.trace_length import CRITICAL_NET_PATTERNS

if TYPE_CHECKING:
    from .length import LengthTracker
    from .rules import NetClassRouting


logger = logging.getLogger(__name__)


# =============================================================================
# Type contract -- coordinated with Phase 1B (#2688)
# =============================================================================


class MatchGroupSource(Enum):
    """Where a match group came from in the layered detector.

    Three-tier (not four).  ``KICAD_GROUP`` is intentionally absent:
    KiCad's PCB s-expression does not currently have a
    ``(match_group ...)`` directive analogous to
    ``(diff_pair_template ...)``, so the parallel ``KICAD_GROUP``
    source from diff-pair detection has no analog here.  Phase 2 of
    Epic #2661 (not this issue) may revisit if KiCad upstream adds
    such a directive.

    The ordering of values matches detection priority:
    ``EXPLICIT > LEGACY_API > SUFFIX``.
    """

    EXPLICIT = "explicit"
    """Declared via ``NetClassRouting.length_match_group``."""

    LEGACY_API = "legacy_api"
    """Registered via ``Autorouter.add_match_group(...)``.

    The legacy Python API path that has existed since before this
    epic.  Surfaced from ``LengthTracker.match_groups`` so already-
    routed boards that pre-date Phase 1A don't lose their group
    membership.
    """

    SUFFIX = "suffix"
    """Opt-in inference from :data:`BUS_GROUP_PATTERNS`.

    Only reported when ``enable_suffix_inference=True`` is passed to
    :func:`detect_match_groups`.  Refuses low-confidence groups
    (fewer than ``_MIN_GROUP_SIZE`` members) so that random GPIO
    names (``A0`` alone, ``DQ0``/``DQ1`` pair) don't get incorrectly
    classified.
    """


@dataclass
class MatchGroup:
    """A length-match group plus metadata about its source and policy.

    Attributes:
        group_id: Stable identifier for the group.  By convention
            this is the group name from the source (the
            ``length_match_group`` field, the ``add_match_group(name=...)``
            argument, or the :data:`BUS_GROUP_PATTERNS` template).
        members: Net IDs that belong to the group, in deterministic
            sorted order (lowest net id first).
        reference: Net ID to use as the reference (the "pace car") for
            length-matching, or ``None`` to mean "use the longest
            trace in the group".  Set by the reference resolver from
            ``NetClassRouting.length_match_reference``.
        source: Which detection path produced this group.

    Notes:
        This dataclass is intentionally minimal -- Phase 1B (#2688)
        owns the measurement / tolerance logic and may extend the
        contract with serialisation methods (``to_dict`` /
        ``from_dict``).  This module does not invent such methods; if
        Phase 1B adds them, the existing instances round-trip
        trivially because all fields are JSON-compatible primitives.
    """

    group_id: str
    members: list[int] = field(default_factory=list)
    reference: int | None = None
    source: MatchGroupSource = MatchGroupSource.EXPLICIT


# =============================================================================
# BUS_GROUP_PATTERNS -- suffix-inference table
# =============================================================================
#
# Maps (regex, group_name_template) tuples for opt-in suffix-based
# inference.  Group name template uses ``{}`` placeholders if any
# capturing group from the regex should be substituted in (currently
# only ``DDR_DATA_BYTE_{}`` uses this, with the high-order byte index).
#
# These regexes deliberately overlap with the per-net "is this
# interesting?" gate at
# :data:`kicad_tools.analysis.trace_length.CRITICAL_NET_PATTERNS` but
# are NOT pure reuse -- the former classify nets, the latter name
# groups.  A drift-prevention test asserts the discriminating
# fragments (``DQ\d``, ``DQS``, ``DM\d``, ``A\d+$``, ``MIPI``,
# ``HDMI``) are reachable from both tables so the two cannot
# silently diverge.

# Minimum members required for a suffix-inferred group to be reported.
# Below this threshold, the inferer refuses the group (mirroring the
# single-ended refusal in router/diffpair_detection.py).  Three is the
# smallest size that exceeds "could be a diff pair" (2) plus "could be
# a single GPIO" (1).
_MIN_GROUP_SIZE = 3


BUS_GROUP_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # DDR data byte: DQ0..DQ7 form an 8-bit byte; one group per byte
    # boundary.  ``DQ\d+`` without explicit byte grouping collapses
    # all matched DQ nets into a single group named "DDR_DATA".
    # Designs that need per-byte separation can declare explicitly
    # via NetClassRouting.length_match_group.
    (re.compile(r"(?i)^DQ\d+$"), "DDR_DATA"),
    # DDR strobe: DQS, DQS_P/N, DQS0..DQS3 -- often a pair, sometimes
    # a single net.  Below the min-group threshold by itself; included
    # here so the regex is present (the drift test verifies coverage)
    # but the membership filter will refuse the group unless a board
    # has >=3 strobe nets (rare; usually composed via diff-pair).
    (re.compile(r"(?i)^DQS(?:\d+)?(?:_[PN])?$"), "DDR_STROBE"),
    # DDR data mask: DM0..DM3 -- one mask per data byte.  Usually 1-2
    # nets, refused by min-group threshold.  Present for drift-test
    # symmetry.
    (re.compile(r"(?i)^DM\d+$"), "DDR_DATA_MASK"),
    # MIPI CSI data lanes (positive + negative halves).  Designs
    # typically have 2 or 4 lanes per camera; ``CSI_DAT0_P`` style.
    # Pair-aware composition is Phase 2F's job; Phase 1C just
    # reports the data-lane group containing all P/N nets.
    (re.compile(r"(?i)^CSI_DAT\d+_[PN]$"), "MIPI_CSI_DATA"),
    # MIPI DSI data lanes (display-side).
    (re.compile(r"(?i)^DSI_DAT\d+_[PN]$"), "MIPI_DSI_DATA"),
    # HDMI TMDS data lanes: TMDS_D0_P/N, TMDS_D1_P/N, TMDS_D2_P/N.
    # Clock (TMDS_CLK_P/N) is intentionally excluded -- it is the
    # reference, not a group member.  Phase 2F composes the lanes-
    # vs-clock relationship; Phase 1C only detects the data-lane
    # group.
    (re.compile(r"(?i)^TMDS_D\d+_[PN]$"), "HDMI_TMDS_DATA"),
    # Generic address bus: A0..AN.  HIGHEST false-positive risk
    # (matches GPIO names like ``A0``).  Min-group threshold of 3
    # prevents single-GPIO false positives; the partial-coverage
    # test exercises this case.  Documented in the module docstring.
    (re.compile(r"(?i)^A\d+$"), "ADDR_BUS"),
]


# Clock-name patterns drawn from CRITICAL_NET_PATTERNS lines 31-34.
# Compiled here from the existing list so this module does NOT
# reimplement the regex set; the drift-prevention test asserts these
# four entries appear in CRITICAL_NET_PATTERNS.
_CLOCK_REGEX_INDICES = (0, 1, 2, 3)  # ^CLK, CLK$, CLOCK, _CLK_


def _get_clock_regexes() -> list[re.Pattern[str]]:
    """Return the four clock-discriminating regexes from
    ``CRITICAL_NET_PATTERNS``.

    Drift-prevention: this resolves the regexes at import time but
    *via index lookup into the canonical list*, so renaming /
    reordering CRITICAL_NET_PATTERNS will surface in tests rather
    than silently produce wrong sentinel behaviour.
    """
    return [re.compile(CRITICAL_NET_PATTERNS[i]) for i in _CLOCK_REGEX_INDICES]


# =============================================================================
# Public entry point
# =============================================================================


def detect_match_groups(
    net_names: dict[int, str],
    *,
    net_class_routing: dict[str, NetClassRouting] | None = None,
    net_to_class: dict[str, str] | None = None,
    length_tracker: LengthTracker | None = None,
    enable_suffix_inference: bool = False,
) -> list[MatchGroup]:
    """Layered match-group detection.

    Args:
        net_names: ``{net_id: net_name}`` from the autorouter.
        net_class_routing: Optional ``{class_name: NetClassRouting}``
            map.  When a class has ``length_match_group`` set, every
            net mapped to that class joins the named group.  Highest-
            priority source (EXPLICIT).
        net_to_class: Optional ``{net_name: class_name}`` map used to
            look up which net class a net belongs to.  Required when
            ``net_class_routing`` is supplied; otherwise explicit
            declarations are skipped.
        length_tracker: Optional :class:`LengthTracker` whose
            ``match_groups`` dict surfaces groups registered through
            the legacy ``Autorouter.add_match_group`` API.  Second-
            priority source (LEGACY_API).  Groups already claimed by
            an EXPLICIT source (by net-id overlap) are not re-emitted.
        enable_suffix_inference: When ``True``, runs the
            :data:`BUS_GROUP_PATTERNS` matcher over nets not yet
            claimed by explicit / legacy sources.  Off by default
            because suffix patterns have a non-trivial false-positive
            rate; agents should declare explicitly when in doubt.

    Returns:
        A list of :class:`MatchGroup`, with ``source`` recording
        which detection path produced each group.  Groups are
        emitted in declaration order: EXPLICIT first, then
        LEGACY_API, then SUFFIX.  Members within each group are
        sorted by net id for deterministic output.

    Notes:
        - A net is assigned to AT MOST ONE group.  If an explicit
          declaration and a suffix pattern both claim ``DQ0``, only
          the explicit declaration is reported.
        - Suffix inference refuses groups with fewer than three
          members (``_MIN_GROUP_SIZE``).  The same lesson as the
          USB-CC1/CC2 refusal in ``diffpair_detection.py``: small
          groups are too likely to be false positives.
        - The ``"clock"`` sentinel value of
          ``NetClassRouting.length_match_reference`` is resolved
          inside this function -- the returned ``MatchGroup.reference``
          is a concrete net-id, never the sentinel string.
    """
    name_to_id = _name_to_id_map(net_names)
    claimed_net_ids: set[int] = set()
    out: list[MatchGroup] = []

    # 1. Explicit declarations -- authoritative.
    explicit_groups = _gather_explicit_groups(
        net_names=net_names,
        net_class_routing=net_class_routing,
        net_to_class=net_to_class,
    )
    for group in explicit_groups:
        group.members.sort()
        group.reference = _resolve_reference(
            group=group,
            net_names=net_names,
            name_to_id=name_to_id,
            net_class_routing=net_class_routing,
            net_to_class=net_to_class,
        )
        out.append(group)
        claimed_net_ids.update(group.members)
        logger.info(
            "[match_group] %s (%d nets, source: explicit, ref=%s)",
            group.group_id,
            len(group.members),
            net_names.get(group.reference) if group.reference is not None else "longest",
        )

    # 2. Legacy-API groups (existing add_match_group registrations).
    if length_tracker is not None:
        for name, net_ids in length_tracker.match_groups.items():
            # Skip if any member of this legacy group is already
            # claimed by an explicit declaration -- explicit wins.
            net_id_set = set(net_ids)
            if net_id_set & claimed_net_ids:
                # Partial overlap means a higher-priority source has
                # already claimed at least one member; drop the whole
                # group to avoid double-counting.
                logger.debug(
                    "[match_group] legacy group %s overlaps with explicit declaration; skipping",
                    name,
                )
                continue
            members = sorted(net_id_set)
            if not members:
                continue
            group = MatchGroup(
                group_id=name,
                members=members,
                reference=None,  # Legacy API has no reference policy.
                source=MatchGroupSource.LEGACY_API,
            )
            out.append(group)
            claimed_net_ids.update(members)
            logger.info(
                "[match_group] %s (%d nets, source: legacy_api)",
                name,
                len(members),
            )

    # 3. Suffix inference -- opt-in, last priority.
    if enable_suffix_inference:
        remaining_names: dict[int, str] = {
            nid: nm for nid, nm in net_names.items() if nid not in claimed_net_ids
        }
        suffix_groups = _infer_suffix_groups(remaining_names)
        for group in suffix_groups:
            # Re-check claim set in case two suffix patterns collide.
            if set(group.members) & claimed_net_ids:
                continue
            group.members.sort()
            out.append(group)
            claimed_net_ids.update(group.members)
            logger.info(
                "[match_group] %s (%d nets, source: suffix)",
                group.group_id,
                len(group.members),
            )

    return out


# =============================================================================
# Source 1: Explicit declarations
# =============================================================================


def _gather_explicit_groups(
    *,
    net_names: dict[int, str],
    net_class_routing: dict[str, NetClassRouting] | None,
    net_to_class: dict[str, str] | None,
) -> list[MatchGroup]:
    """Collect groups declared via ``NetClassRouting.length_match_group``.

    Multiple net classes may declare the same group name -- their
    members are merged into a single :class:`MatchGroup`.  This is
    the documented use case for MIPI lane composition: each lane has
    its own class (so per-lane impedance / diff-pair settings can
    differ) but they all share a ``length_match_group="MIPI_CSI"``.
    """
    if not net_class_routing or not net_to_class:
        return []

    # Build {group_name: [net_ids]} from the class membership map.
    group_to_ids: dict[str, list[int]] = {}
    for nid, net_name in net_names.items():
        class_name = net_to_class.get(net_name)
        if class_name is None:
            continue
        nc = net_class_routing.get(class_name)
        if nc is None:
            continue
        group_name = nc.effective_length_match_group()
        if group_name is None:
            continue
        group_to_ids.setdefault(group_name, []).append(nid)

    return [
        MatchGroup(
            group_id=name,
            members=sorted(ids),
            reference=None,  # Resolved after the gather pass.
            source=MatchGroupSource.EXPLICIT,
        )
        for name, ids in sorted(group_to_ids.items())
    ]


# =============================================================================
# Source 3: Suffix inference
# =============================================================================


def _infer_suffix_groups(net_names: dict[int, str]) -> list[MatchGroup]:
    """Run :data:`BUS_GROUP_PATTERNS` over ``net_names`` and assemble
    candidate groups.

    Each net is assigned to AT MOST ONE pattern (first match wins,
    in BUS_GROUP_PATTERNS declaration order).  Groups with fewer
    than ``_MIN_GROUP_SIZE`` members are refused.
    """
    # First pass: bucket nets by pattern (first match wins).
    pattern_to_ids: dict[str, list[int]] = {}
    for nid, name in net_names.items():
        for pattern, group_name in BUS_GROUP_PATTERNS:
            if pattern.match(name):
                pattern_to_ids.setdefault(group_name, []).append(nid)
                break

    # Second pass: refuse small groups.
    out: list[MatchGroup] = []
    for group_name, ids in sorted(pattern_to_ids.items()):
        if len(ids) < _MIN_GROUP_SIZE:
            logger.debug(
                "[match_group] suffix pattern %s matched %d nets (< %d "
                "required); refusing low-confidence group",
                group_name,
                len(ids),
                _MIN_GROUP_SIZE,
            )
            continue
        out.append(
            MatchGroup(
                group_id=group_name,
                members=sorted(ids),
                reference=None,
                source=MatchGroupSource.SUFFIX,
            )
        )
    return out


# =============================================================================
# Reference resolution (clock sentinel)
# =============================================================================


def _resolve_reference(
    *,
    group: MatchGroup,
    net_names: dict[int, str],
    name_to_id: dict[str, int],
    net_class_routing: dict[str, NetClassRouting] | None,
    net_to_class: dict[str, str] | None,
) -> int | None:
    """Resolve ``NetClassRouting.length_match_reference`` to a net id.

    Returns ``None`` (meaning "use the longest trace in the group")
    when no reference policy is set, or when the sentinel ``"clock"``
    cannot find a matching member.

    The resolver consults the FIRST class in ``net_class_routing``
    whose ``length_match_group`` equals ``group.group_id`` and uses
    that class's ``length_match_reference`` value.  When multiple
    classes share the same group name and disagree on the reference,
    the first one wins (deterministic by ``sorted()`` order in
    :func:`_gather_explicit_groups`).
    """
    if not net_class_routing or not net_to_class:
        return None

    # Find the reference policy attached to this group.
    reference_policy: str | None = None
    for class_name in sorted(net_class_routing.keys()):
        nc = net_class_routing[class_name]
        if nc.effective_length_match_group() == group.group_id:
            reference_policy = nc.effective_length_match_reference()
            if reference_policy is not None:
                break

    if reference_policy is None:
        return None  # "longest" semantics handled by the tracker.

    # Explicit net-name reference.
    if reference_policy != "clock":
        ref_id = name_to_id.get(reference_policy)
        if ref_id is None:
            logger.warning(
                "[match_group] group %s declares reference net %r which is "
                "not in the net list; falling back to longest",
                group.group_id,
                reference_policy,
            )
            return None
        if ref_id not in group.members:
            logger.warning(
                "[match_group] group %s declares reference net %r which is "
                "not a member of the group; falling back to longest",
                group.group_id,
                reference_policy,
            )
            return None
        return ref_id

    # "clock" sentinel -- find a member whose name matches a clock
    # regex from CRITICAL_NET_PATTERNS.
    return _resolve_clock_sentinel(group=group, net_names=net_names)


def _resolve_clock_sentinel(
    *,
    group: MatchGroup,
    net_names: dict[int, str],
) -> int | None:
    """Resolve the ``"clock"`` reference sentinel for a match group.

    Iterates over the four clock regexes from
    ``CRITICAL_NET_PATTERNS:31-34`` and finds any group member whose
    name matches.  Multi-match resolution is deterministic: the
    member with the lowest net id wins, with a ``logger.warning``
    if more than one matched.

    Returns the matching member's net id, or ``None`` (with a
    ``logger.warning``) if no member matches any clock regex.
    """
    clock_regexes = _get_clock_regexes()

    matches: list[int] = []
    for nid in group.members:
        name = net_names.get(nid)
        if name is None:
            continue
        for rgx in clock_regexes:
            if rgx.search(name):
                matches.append(nid)
                break

    if not matches:
        logger.warning(
            "[match_group] group %s: 'clock' sentinel declared but no "
            "member name matches any clock regex from CRITICAL_NET_PATTERNS; "
            "falling back to longest",
            group.group_id,
        )
        return None

    matches.sort()
    if len(matches) > 1:
        logger.warning(
            "[match_group] group %s: 'clock' sentinel matched %d members "
            "(%s); picking lowest net id %d (%s) for determinism",
            group.group_id,
            len(matches),
            [net_names.get(m) for m in matches],
            matches[0],
            net_names.get(matches[0]),
        )
    return matches[0]


# =============================================================================
# Internals
# =============================================================================


def _name_to_id_map(net_names: dict[int, str]) -> dict[str, int]:
    """Reverse-index net names to net IDs.  First occurrence wins."""
    out: dict[str, int] = {}
    for nid, name in net_names.items():
        if name not in out:
            out[name] = nid
    return out
