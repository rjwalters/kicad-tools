"""
Layered differential-pair detection (Issue #2558, Epic #2556 Phase 1B).

This module wraps the existing suffix-based detector in
``router/diffpair.py`` with two higher-priority sources:

1. **Explicit declaration** -- per-net config via
   :class:`kicad_tools.router.rules.NetClassRouting` ``diffpair_partner``
   field.  Authoritative; overrides everything else.
2. **KiCad group** -- ``(diff_pair_template ...)`` directives in PCB
   s-expressions and a kicad-tools-specific
   ``net_settings.diff_pairs: [{p, n}, ...]`` field in the project
   JSON.
3. **Suffix inference** -- the existing pattern matcher in
   ``router/diffpair.py``.  Used only for nets that none of the higher-
   priority sources have already paired.

The output preserves the existing :class:`DifferentialPair` shape so
the rest of the router (``diffpair_routing.py``,
``CoupledPathfinder``, etc.) doesn't change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from .diffpair import (
    DifferentialPair,
    DifferentialPairRules,
    DifferentialSignal,
    _detect_pair_type,
)
from .diffpair import (
    detect_differential_pairs as _suffix_detect_pairs,
)

if TYPE_CHECKING:
    from kicad_tools.sexp.parser import SExp

    from .rules import NetClassRouting


logger = logging.getLogger(__name__)


class DetectionSource(Enum):
    """Where a differential pair came from in the layered detector."""

    EXPLICIT = "explicit"
    KICAD_GROUP = "kicad_group"
    SUFFIX = "suffix"


@dataclass
class DetectedPair:
    """A diff pair plus metadata about how it was detected."""

    pair: DifferentialPair
    source: DetectionSource


# =============================================================================
# Public entry point
# =============================================================================


def detect_diff_pairs(
    net_names: dict[int, str],
    *,
    net_class_routing: dict[str, NetClassRouting] | None = None,
    net_to_class: dict[str, str] | None = None,
    kicad_groups: list[tuple[str, str]] | None = None,
) -> list[DetectedPair]:
    """Layered differential-pair detection.

    Args:
        net_names: ``{net_id: net_name}`` from the autorouter.
        net_class_routing: Optional ``{class_name: NetClassRouting}``
            map.  When a net is mapped (via ``net_to_class``) to a
            class whose ``diffpair_partner`` is set, the explicit
            declaration wins over all other sources.
        net_to_class: Optional ``{net_name: class_name}`` map used to
            look up which net class a net belongs to.  If omitted (or
            a net is missing), explicit declarations through
            ``diffpair_partner`` are not consulted for that net.
        kicad_groups: Optional list of ``(positive_net_name,
            negative_net_name)`` pairs harvested from KiCad's
            ``(diff_pair_template ...)`` directives or the project-
            file ``net_settings.diff_pairs`` list.  These pairs win
            over suffix inference but lose to explicit declarations.

    Returns:
        A list of :class:`DetectedPair`, with ``source`` recording
        which detection path produced each pair.  Pairs are emitted
        in declaration order: explicit first, then KiCad-group, then
        suffix-detected.

    Notes:
        - A net pair is reported AT MOST ONCE.  If both an explicit
          declaration and a KiCad group claim the same nets, only
          the explicit version is reported.
        - One-sided explicit declarations (only one of the two
          half-pairs has ``diffpair_partner`` set) are supported.
        - Single-ended refusal applies ONLY to suffix inference --
          designers can still pair USB-C ``CC1``/``CC2`` explicitly.
    """
    name_to_id = _name_to_id_map(net_names)
    paired_net_ids: set[int] = set()
    out: list[DetectedPair] = []

    # 1. Explicit declarations -- authoritative.
    explicit_pairs = _gather_explicit_pairs(
        net_names=net_names,
        name_to_id=name_to_id,
        net_class_routing=net_class_routing,
        net_to_class=net_to_class,
    )
    for pair in explicit_pairs:
        out.append(DetectedPair(pair=pair, source=DetectionSource.EXPLICIT))
        paired_net_ids.add(pair.positive.net_id)
        paired_net_ids.add(pair.negative.net_id)
        logger.info(
            "[diffpair] %s <-> %s (source: explicit)",
            pair.positive.net_name,
            pair.negative.net_name,
        )

    # 2. KiCad group declarations.
    if kicad_groups:
        for p_name, n_name in kicad_groups:
            kicad_pair = _make_pair_from_names(
                p_name=p_name,
                n_name=n_name,
                name_to_id=name_to_id,
            )
            if kicad_pair is None:
                continue
            if kicad_pair.positive.net_id in paired_net_ids:
                continue
            if kicad_pair.negative.net_id in paired_net_ids:
                continue
            out.append(DetectedPair(pair=kicad_pair, source=DetectionSource.KICAD_GROUP))
            paired_net_ids.add(kicad_pair.positive.net_id)
            paired_net_ids.add(kicad_pair.negative.net_id)
            logger.info(
                "[diffpair] %s <-> %s (source: kicad_group)",
                kicad_pair.positive.net_name,
                kicad_pair.negative.net_name,
            )

    # 3. Suffix-based fall-back.  Only consider nets that aren't
    #    already part of an explicit / KiCad-group pair.
    remaining_names: dict[int, str] = {
        nid: nm for nid, nm in net_names.items() if nid not in paired_net_ids
    }
    for pair in _suffix_detect_pairs(remaining_names):
        if pair.positive.net_id in paired_net_ids:
            continue
        if pair.negative.net_id in paired_net_ids:
            continue
        out.append(DetectedPair(pair=pair, source=DetectionSource.SUFFIX))
        paired_net_ids.add(pair.positive.net_id)
        paired_net_ids.add(pair.negative.net_id)
        logger.info(
            "[diffpair] %s <-> %s (source: suffix)",
            pair.positive.net_name,
            pair.negative.net_name,
        )

    return out


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


def _make_pair_from_names(
    *,
    p_name: str,
    n_name: str,
    name_to_id: dict[str, int],
) -> DifferentialPair | None:
    """Build a :class:`DifferentialPair` from the two net names.

    Returns ``None`` when either name is missing from the net list.
    Does NOT consult the suffix matcher -- the inputs are already
    declared as a pair.
    """
    p_id = name_to_id.get(p_name)
    n_id = name_to_id.get(n_name)
    if p_id is None or n_id is None:
        return None

    base_name = _common_prefix(p_name, n_name) or p_name
    p_signal = DifferentialSignal(
        net_name=p_name,
        net_id=p_id,
        base_name=base_name,
        polarity="P",
        notation="explicit",
    )
    n_signal = DifferentialSignal(
        net_name=n_name,
        net_id=n_id,
        base_name=base_name,
        polarity="N",
        notation="explicit",
    )
    pair_type = _detect_pair_type(base_name)
    return DifferentialPair(
        name=base_name,
        positive=p_signal,
        negative=n_signal,
        pair_type=pair_type,
        rules=DifferentialPairRules.for_type(pair_type),
    )


def _common_prefix(a: str, b: str) -> str:
    """Return the longest common prefix of ``a`` and ``b``, stripped of
    trailing separator characters (``_``, ``-``, ``+``)."""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    prefix = a[:i]
    return prefix.rstrip("_-+")


def _gather_explicit_pairs(
    *,
    net_names: dict[int, str],
    name_to_id: dict[str, int],
    net_class_routing: dict[str, NetClassRouting] | None,
    net_to_class: dict[str, str] | None,
) -> list[DifferentialPair]:
    """Collect pairs declared via ``NetClassRouting.diffpair_partner``.

    Supports one-sided declarations: if only one of the two half-pairs
    has the field set, the partner is still found provided it exists
    in ``net_names``.

    Lookup scoping (Issue #3455): ``net_class_routing`` may be keyed by
    NET NAME (autorouter ``net_class_map`` convention), by CLASS NAME
    (``validate/match_group_skew`` convention), or by both (the
    ``synth_routing`` idiom in ``router/core.py``).  A net-name-keyed
    entry is authoritative for THAT net only.  A class-name-keyed entry
    fans out to every net of the class, which is a pollution hazard:
    when a per-net annotation (e.g. ``USB_D+`` with partner ``USB_D-``)
    is registered under its class name (``HighSpeed``), every other net
    of that class (``USB_CC1``, ``USB_CC2``, ...) would otherwise
    inherit the partner declaration.  Class-keyed declarations are
    therefore honored only when unambiguous -- see
    :func:`_declared_partner_for_net`.
    """
    if not net_class_routing or not net_to_class:
        return []

    # Class-membership view restricted to nets actually on the board,
    # used to disambiguate class-name-keyed declarations.
    class_members: dict[str, list[str]] = {}
    for net_name in net_names.values():
        class_name = net_to_class.get(net_name)
        if class_name is not None:
            members = class_members.setdefault(class_name, [])
            if net_name not in members:
                members.append(net_name)

    declared: dict[str, str] = {}  # net_name -> partner_name
    for net_name in net_names.values():
        partner_name = _declared_partner_for_net(
            net_name=net_name,
            net_class_routing=net_class_routing,
            net_to_class=net_to_class,
            class_members=class_members,
        )
        if partner_name is not None:
            declared[net_name] = partner_name

    pairs: list[DifferentialPair] = []
    seen: set[frozenset[str]] = set()  # canonical {p, n} pair sets
    used: set[str] = set()  # nets already consumed by an emitted pair

    for net_name, partner_name in declared.items():
        # Canonicalise so we don't emit the same pair twice from a
        # bidirectional declaration.
        key = frozenset({net_name, partner_name})
        if key in seen:
            continue
        if partner_name not in name_to_id:
            logger.warning(
                "[diffpair] explicit declaration on %s names partner %s "
                "which is not in the net list; skipping",
                net_name,
                partner_name,
            )
            continue
        # A net belongs to AT MOST one pair.  Conflicting declarations
        # (two nets both claiming the same partner) keep the first and
        # warn on the rest (Issue #3455: previously USB_CC2 -> USB_D-
        # was emitted alongside USB_D+ -> USB_D-, and the last writer
        # clobbered USB_D-'s partner in the autorouter wiring loop).
        if net_name in used or partner_name in used:
            logger.warning(
                "[diffpair] explicit declaration %s <-> %s conflicts with "
                "an already-emitted pair; skipping",
                net_name,
                partner_name,
            )
            continue
        seen.add(key)

        # Disambiguate which side is positive.  Prefer the side whose
        # name suggests the positive polarity (ends in +, _P, _DP,
        # _POS) but fall back to alphabetical to keep the result
        # deterministic.
        pos_name, neg_name = _order_explicit_pair(net_name, partner_name)
        pair = _make_pair_from_names(
            p_name=pos_name,
            n_name=neg_name,
            name_to_id=name_to_id,
        )
        if pair is not None:
            pairs.append(pair)
            used.add(net_name)
            used.add(partner_name)

    return pairs


def _declared_partner_for_net(
    *,
    net_name: str,
    net_class_routing: dict[str, NetClassRouting],
    net_to_class: dict[str, str],
    class_members: dict[str, list[str]],
) -> str | None:
    """Resolve the explicitly-declared partner for ``net_name``, if any.

    Resolution order (Issue #3455 -- net-name-scoped gathering):

    1. **Net-name-keyed entry** (``net_class_routing[net_name]``): the
       key IS the net name, so a ``diffpair_partner`` here is
       authoritative for this net.  Arbitrary partner names (including
       single-ended ones like ``USB_CC2``) remain honored -- this is
       the documented "designers can still pair CC1/CC2 explicitly"
       escape hatch.
    2. **Class-name-keyed entry** (``net_class_routing[class_name]``):
       the declaration is shared by every net of the class, so it can
       describe at most ONE pair.  It is honored only when:

       * this net is the ONLY class member besides the partner itself
         (single-member classes -- the pre-#3455 explicit-declaration
         shape used by all earlier tests), or
       * this net is the unique polarity counterpart of the declared
         partner (matching base name with ``+``/``-``, ``_P``/``_N``,
         etc. polarity: ``USB_D+`` for partner ``USB_D-``).

       Any other class member (``USB_CC2`` for partner ``USB_D-``) gets
       NO partner from the class-level declaration.
    """
    # 1. Net-name-keyed lookup: authoritative for this net only.
    nc = net_class_routing.get(net_name)
    if nc is not None and getattr(nc, "diffpair_partner", None):
        partner = nc.diffpair_partner
        return partner if partner != net_name else None

    # 2. Class-name-keyed lookup with fan-out disambiguation.
    class_name = net_to_class.get(net_name)
    if class_name is None or class_name == net_name:
        return None
    cnc = net_class_routing.get(class_name)
    if cnc is None or cnc is nc or not getattr(cnc, "diffpair_partner", None):
        return None
    partner = cnc.diffpair_partner
    if partner == net_name:
        # This net IS the declared partner; the other half of the pair
        # resolves the declaration from its own side.
        return None

    candidates = [m for m in class_members.get(class_name, []) if m != partner]
    if len(candidates) <= 1:
        # Unambiguous: this net is the only class member that could be
        # the partner's other half.
        return partner

    # Multiple class members compete for the declared partner -- only a
    # true polarity counterpart may claim it.
    if not _is_polarity_counterpart(net_name, partner):
        logger.debug(
            "[diffpair] class %s declares partner %s but member %s is not "
            "its polarity counterpart; not pairing",
            class_name,
            partner,
            net_name,
        )
        return None
    rivals = [m for m in candidates if m != net_name and _is_polarity_counterpart(m, partner)]
    if rivals:
        logger.warning(
            "[diffpair] class %s declares partner %s but multiple members "
            "(%s, %s) are polarity counterparts; skipping ambiguous "
            "declaration",
            class_name,
            partner,
            net_name,
            ", ".join(rivals),
        )
        return None
    return partner


def _is_polarity_counterpart(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` form a polarity pair: identical base
    name with opposite polarity suffixes (``USB_D+``/``USB_D-``,
    ``CLK_P``/``CLK_N``, ...).  Names without a recognisable polarity
    suffix (``USB_CC2``, ``TX0``) are never counterparts."""
    from .diffpair import parse_differential_signal

    a_parsed = parse_differential_signal(a)
    b_parsed = parse_differential_signal(b)
    if a_parsed is None or b_parsed is None:
        return False
    return a_parsed[0] == b_parsed[0] and a_parsed[1] != b_parsed[1]


def _order_explicit_pair(a: str, b: str) -> tuple[str, str]:
    """Order two explicitly-declared net names as (positive, negative).

    The choice is deterministic but otherwise heuristic.  Most boards
    follow standard suffix conventions even when declared explicitly,
    so we use the existing parser to detect polarity.  When neither
    side carries a recognisable polarity suffix, fall back to
    alphabetical order so the result is stable.
    """
    from .diffpair import parse_differential_signal

    a_parsed = parse_differential_signal(a)
    b_parsed = parse_differential_signal(b)
    if a_parsed and a_parsed[1] == "P":
        return a, b
    if a_parsed and a_parsed[1] == "N":
        return b, a
    if b_parsed and b_parsed[1] == "P":
        return b, a
    if b_parsed and b_parsed[1] == "N":
        return a, b
    # Deterministic fallback.
    return (a, b) if a <= b else (b, a)


# =============================================================================
# KiCad-group source helpers
# =============================================================================


def parse_diff_pair_templates_from_pcb(pcb_sexp: SExp) -> list[tuple[str, str]]:
    """Walk a parsed PCB s-expression for ``(diff_pair_template ...)`` blocks.

    Each block is expected in the shape::

        (diff_pair_template
            (positive "USB_D+")
            (negative "USB_D-"))

    Args:
        pcb_sexp: Parsed PCB s-expression (root node, typically named
            ``kicad_pcb``).

    Returns:
        A list of ``(positive_net_name, negative_net_name)`` tuples.
        Malformed blocks are silently skipped.
    """
    pairs: list[tuple[str, str]] = []
    if pcb_sexp is None:
        return pairs

    for child in _iter_named(pcb_sexp, "diff_pair_template"):
        pos = _first_child_value(child, "positive")
        neg = _first_child_value(child, "negative")
        if pos and neg:
            pairs.append((pos, neg))

    return pairs


def _iter_named(node: SExp, name: str):
    """Yield every direct child of ``node`` whose name matches."""
    children = getattr(node, "children", None)
    if not children:
        return
    for c in children:
        if getattr(c, "name", None) == name:
            yield c


def _first_child_value(node: SExp, child_name: str) -> str | None:
    """Get the first atom value from the first child matching ``child_name``."""
    for c in _iter_named(node, child_name):
        for sub in getattr(c, "children", []) or []:
            v = getattr(sub, "value", None)
            if v is not None:
                return str(v)
    return None
