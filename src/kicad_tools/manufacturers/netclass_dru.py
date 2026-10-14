"""Router net-class clearance rules for ``.kicad_dru`` (Issue #6390).

``kct route`` declares the router's stricter net classes (``Power``,
``Clock``, ...) in the routed ``.kicad_pro`` (see
:func:`~kicad_tools.manufacturers.project_generator.apply_router_netclasses`).
A ``.kicad_pro`` class alone is not enough for ``kicad-cli``: the
unconditional ``Clearance`` fab-floor rule in the managed ``.kicad_dru``
block outranks netclass clearance in KiCad, so each class also needs a
``NetClass``-conditioned clearance rule *after* the floor.

Under the default (``stricter``) and ``full`` preserve modes the existing
``Reviewed clearance - <class>`` rules of
:func:`~kicad_tools.manufacturers.project_generator.generate_project_dru`
already cover every class in the project, the router's included, sorted
together by ascending clearance so the stricter class wins where two meet.
``KCT_PRESERVE_BOARD_RULES=0`` drops those reviewed rules: that mode exists
so a reviewed fabrication process may *relax* the tier floors (board 04's
paid 0.15 mm drilling), not to discard routing constraints.  This module
therefore emits the router's classes -- and only those -- in their own
sentinel-delimited managed block when the mode is ``0``.  In the other modes
the block is removed, so a rule is never emitted twice.

Rules are emitted in ascending clearance order, because later matching rules
win in KiCad.  The conditions test ``NetClass`` only, never ``Type``, so the
router's via-scoped DRU recognizer
(:func:`kicad_tools.router.clearance_resolver._is_via_scoped_condition`)
ignores them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable

#: Sentinel markers of the kct-owned router net-class block.  Distinct from
#: the fab-floors (#4600), creepage (#4508) and keepout (#6039) markers.
DRU_NETCLASS_BLOCK_BEGIN = (
    "# BEGIN kct router net-class clearances (Issue #6390) -- managed, do not edit"
)
DRU_NETCLASS_BLOCK_END = "# END kct router net-class clearances"

#: Rule-name prefix of every generated rule.
NETCLASS_RULE_PREFIX = "kct net class clearance - "

_DRU_VERSION_HEADER = "(version 1)"


#: Pads of one footprint: fixed package geometry the router cannot move.
#: Measured on kicad-cli 10.0.6: ``A.Reference`` resolves to the parent
#: footprint for a pad, so this matches exactly the intra-footprint pad pairs
#: (a 0.15 mm GND/signal gap inside one footprint passes, the same gap between
#: two footprints is flagged).  kicad-cli 10.0.1 still flags both, which errs
#: on the strict side.
SAME_FOOTPRINT_PADS = "A.Type == 'Pad' && B.Type == 'Pad' && A.Reference == B.Reference"


#: The two halves of one coupled differential pair (``USB_D+``/``USB_D-``,
#: ``TX_P``/``TX_N``, ``CLK_P_1``/``CLK_N_1``).  KiCad's own predicate:
#: complementary nets per ``DRC_ENGINE::MatchDpSuffix``, mirrored for
#: ``kct check`` by :func:`kicad_coupled_complement`.  Measured on kicad-cli
#: 10.0.6: a ``USB_D+``/``USB_D-`` gap of 0.127 mm under a 0.15 mm class
#: passes, the same gap between ``USB_D+`` and ``USB_X-`` is still flagged.
COUPLED_DIFF_PAIR = "A.isCoupledDiffPair()"


def kicad_coupled_complement(net_name: str) -> str | None:
    """The complement KiCad's ``isCoupledDiffPair()`` pairs ``net_name`` with.

    A faithful port of KiCad's ``DRC_ENGINE::MatchDpSuffix``: walking back
    from the end of the name, digits and ``_`` are skipped, then the first
    other character decides -- ``+``/``-`` or uppercase ``P``/``N`` swap to
    their complement, anything else means "not a diff-pair net".  So
    ``USB_D+`` -> ``USB_D-``, ``TX_P`` -> ``TX_N``, ``CLK_P_1`` ->
    ``CLK_N_1``; ``VBUS`` -> ``None``.  Case-sensitive, name-only (no
    declared-pair or geometry evidence), exactly like kicad-cli, so both
    engines exempt the same pairs (Issue #6390).
    """
    count = 0
    swap = None
    for ch in reversed(net_name):
        count += 1
        if (ch.isascii() and ch.isdigit()) or ch == "_":
            continue
        swap = {"+": "-", "-": "+", "P": "N", "N": "P"}.get(ch)
        break
    if swap is None:
        return None
    base = net_name[: len(net_name) - count]
    tail = net_name[len(net_name) - count + 1 :]
    return f"{base}{swap}{tail}"


def is_coupled_diff_pair(name_a: str | None, name_b: str | None) -> bool:
    """Whether two nets are the halves of one pair to ``isCoupledDiffPair()``."""
    if not name_a or not name_b:
        return False
    return kicad_coupled_complement(name_a) == name_b


def is_router_coupled_pair(
    name_a: str | None,
    name_b: str | None,
    recorded: Iterable[Iterable[str]] = (),
) -> bool:
    """Whether a router class's clearance skips the gap between two nets.

    The single predicate both engines share (Issue #6429): KiCad's own
    ``isCoupledDiffPair()`` (:func:`is_coupled_diff_pair`), or a pair the
    router recorded as coupled-routed under ``meta.kct_router_diff_pairs``
    (names KiCad does not recognise, e.g. ``X_POS``/``X_NEG``, ``x_p``/``x_n``).
    The ``.kicad_dru`` side is :func:`netclass_condition`'s ``extra_pairs``.
    Order-insensitive; exact, case-sensitive names.
    """
    if not name_a or not name_b:
        return False
    if is_coupled_diff_pair(name_a, name_b):
        return True
    key = frozenset((name_a, name_b))
    return len(key) == 2 and any(frozenset(pair) == key for pair in recorded)


def _dru_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def coupled_pair_exemption(extra_pairs: Iterable[tuple[str, str]] = ()) -> str:
    """The DRC expression true for the two halves of a coupled pair.

    Without ``extra_pairs`` exactly :data:`COUPLED_DIFF_PAIR`.  Each recorded
    pair adds a net-name term in **both** orderings (``A=P, B=N`` and ``A=N,
    B=P``): measured on kicad-cli 10.0.6, a one-sided term does not exempt the
    pair -- KiCad evaluates the rule with the items in both orders and applies
    it whenever either order matches (Issue #6429).
    """
    terms = [COUPLED_DIFF_PAIR]
    for p, n in extra_pairs:
        ep, en = _dru_string(p), _dru_string(n)
        terms.append(f"(A.NetName == '{ep}' && B.NetName == '{en}')")
        terms.append(f"(A.NetName == '{en}' && B.NetName == '{ep}')")
    if len(terms) == 1:
        return COUPLED_DIFF_PAIR
    return "(" + " || ".join(terms) + ")"


def netclass_condition(
    name: str, *, router: bool, extra_pairs: Iterable[tuple[str, str]] = ()
) -> str:
    """The DRC condition selecting copper of netclass ``name``.

    ``router=True`` (a class kct declared from the router's map, Issue #6390)
    excludes two kinds of pair the router's class does not govern:

    * pad pairs inside one footprint: a router class is a routing intent for
      the copper the router places, and a connector's or a fine-pitch
      package's own pin gaps (USB-C pins 0.15 mm apart, QFP corner pads
      0.1414 mm apart) are vendor geometry no re-route can widen -- the same
      scoping #5152 gave the different-net SMD pad floor;
    * the two halves of a coupled differential pair: the router routes P
      against N at the class's ``intra_pair_clearance`` (``HighSpeed``:
      0.075 mm against a 0.15 mm clearance), so holding the pair to the full
      class clearance flags exactly the copper the router intended.  The fab
      floor still applies to the pair.

    ``extra_pairs`` are the pairs the router coupled-routed whose names
    KiCad's ``isCoupledDiffPair()`` does not recognise (``X_POS``/``X_NEG``,
    ``x_p``/``x_n``; recorded under ``meta.kct_router_diff_pairs``, Issue
    #6429).  They join the exemption by exact net name, in both orderings
    (see :func:`coupled_pair_exemption`).  Without them the condition is
    byte-identical to the #6390 one.

    Authored classes keep KiCad's full semantics.
    """
    escaped = _dru_string(name)
    condition = f"A.NetClass == '{escaped}' || B.NetClass == '{escaped}'"
    if not router:
        return condition
    exemption = coupled_pair_exemption(extra_pairs)
    return f"({condition}) && !({SAME_FOOTPRINT_PADS}) && !{exemption}"


def render_netclass_rule(
    name: str, clearance: float, extra_pairs: Iterable[tuple[str, str]] = ()
) -> str:
    """One router-class clearance rule (same grammar as the reviewed rules)."""
    condition = netclass_condition(name, router=True, extra_pairs=extra_pairs)
    return (
        f"(rule {json.dumps(NETCLASS_RULE_PREFIX + name)}\n"
        f"  (condition {json.dumps(condition)})\n"
        f"  (constraint clearance (min {clearance}mm)))"
    )


def netclass_block_rules(
    project_data: dict, floor_mm: float, *, mode_off: bool
) -> list[tuple[str, float]]:
    """``(class name, clearance)`` pairs the managed block must carry.

    Empty unless ``mode_off`` (see the module docstring).  Only classes kct
    declared from the router map (``meta.kct_router_netclasses``) and still
    present in ``net_settings.classes`` qualify, and only when their
    clearance exceeds ``floor_mm``.
    """
    if not mode_off:
        return []
    from .project_generator import router_netclass_provenance

    names, _nets = router_netclass_provenance(project_data)
    if not names:
        return []
    classes = (project_data.get("net_settings") or {}).get("classes") or []
    rules: list[tuple[str, float]] = []
    for cls in classes:
        if not isinstance(cls, dict) or cls.get("name") not in names:
            continue
        clearance = cls.get("clearance")
        if isinstance(clearance, bool) or not isinstance(clearance, (int, float)):
            continue
        if clearance > floor_mm:
            rules.append((cls["name"], float(clearance)))
    return sorted(rules, key=lambda item: (item[1], item[0]))


def merge_netclass_block(
    existing: str | None,
    rules: list[tuple[str, float]],
    extra_pairs: Iterable[tuple[str, str]] = (),
) -> str | None:
    """Merge the router net-class block into ``.kicad_dru`` content.

    Same contract as :func:`~kicad_tools.manufacturers.keepout_dru.merge_keepout_block`:
    rules present -> replace any existing block with a fresh one appended at
    the end; no rules -> remove a stale block, otherwise return ``existing``
    unchanged (boards without router classes stay byte-identical).
    ``extra_pairs``: see :func:`netclass_condition`.  Idempotent.
    """
    pairs = list(extra_pairs)
    pattern = re.compile(
        r"\n*" + re.escape(DRU_NETCLASS_BLOCK_BEGIN) + r".*?" + re.escape(DRU_NETCLASS_BLOCK_END),
        re.DOTALL,
    )
    had_block = existing is not None and pattern.search(existing) is not None
    base = pattern.sub("", existing).lstrip("\n") if had_block and existing else existing
    if not rules:
        if not had_block or base is None:
            return existing
        return base.rstrip("\n") + "\n" if base.strip() else ""

    body = [
        "# The router's net classes, declared in the routed .kicad_pro. Without",
        "# these the unconditional fab-floor clearance above outranks them.",
        *(render_netclass_rule(name, clearance, pairs) for name, clearance in rules),
    ]
    block = f"{DRU_NETCLASS_BLOCK_BEGIN}\n" + "\n".join(body) + f"\n{DRU_NETCLASS_BLOCK_END}"
    if base is None or not base.strip():
        return f"{_DRU_VERSION_HEADER}\n\n{block}\n"
    prefix = base.rstrip("\n") + "\n"
    if not re.search(r"^\s*\(version\b", base, re.MULTILINE):
        prefix = f"{_DRU_VERSION_HEADER}\n" + prefix
    return f"{prefix}\n{block}\n"


# ---------------------------------------------------------------------------
# Router diff-pair gap rules (Issue #6443, P1 piece (c))
# ---------------------------------------------------------------------------

#: Sentinel markers of the kct-owned router diff-pair gap block.  Sits next to
#: the router net-class block; distinct from every other managed block.
DRU_PAIR_GAP_BLOCK_BEGIN = "# BEGIN kct router diff-pair gaps (Issue #6443) -- managed, do not edit"
DRU_PAIR_GAP_BLOCK_END = "# END kct router diff-pair gaps"

#: Rule-name prefix of every generated pair-gap rule.
PAIR_GAP_RULE_PREFIX = "kct diff pair gap - "


def pair_gap_condition(p: str, n: str) -> str:
    """The DRC condition selecting the copper of the pair ``p``/``n``.

    KiCad evaluates ``diff_pair_gap`` with the item as ``A`` and no ``B``, so
    the condition names the two nets on ``A`` alone (exact, case-sensitive,
    escaped as :func:`coupled_pair_exemption` escapes them).  Measured on
    kicad-cli 10.0.6: ``A.NetName == 'LVDS1_P' || A.NetName == 'LVDS1_N'``,
    ``A.inDiffPair('LVDS1_')`` and ``A.inDiffPair('LVDS1')`` select the same
    coupled segments; the net-name form is used because it is exact, where
    ``inDiffPair`` wildcard-matches its argument.
    """
    ep, en = _dru_string(p), _dru_string(n)
    return f"A.NetName == '{ep}' || A.NetName == '{en}'"


def render_pair_gap_rule(p: str, n: str, gap: float) -> str:
    """One ``diff_pair_gap (min ..)`` rule for the pair ``p``/``n``."""
    return (
        f"(rule {json.dumps(f'{PAIR_GAP_RULE_PREFIX}{p}/{n}')}\n"
        f"  (condition {json.dumps(pair_gap_condition(p, n))})\n"
        f"  (constraint diff_pair_gap (min {gap}mm)))"
    )


def pair_gap_block_rules(
    pairs: Iterable[tuple[str, str, float]],
    net_names: Iterable[str] | None = None,
) -> list[tuple[str, str, float]]:
    """``(P, N, gap)`` rules the diff-pair gap block must carry.

    Only pairs KiCad recognises by name (:func:`is_coupled_diff_pair`, the
    predicate the #6429 exemption shares) get a native rule: KiCad's
    ``diff_pair_gap`` test only visits nets ``MatchDpSuffix`` pairs, so a
    rule for ``X_POS``/``X_NEG`` is inert whatever its condition says
    (measured on kicad-cli 10.0.6 with both the ``A``-only and the #6429
    two-sided net-name forms).  Those pairs stay kct-only and
    ``kct route`` already warns about them
    (``_warn_unrecognised_coupled_pairs``).

    With ``net_names``, pairs whose nets are not both on the board are
    dropped.  Pairs are oriented ``(P, N)`` by KiCad's own suffix rule,
    de-duplicated (the stricter gap wins) and sorted, and gaps are rounded to
    1 nm, so the block is byte-stable across re-routes.
    """
    present = None if net_names is None else set(net_names)
    best: dict[tuple[str, str], float] = {}
    for a, b, gap in pairs:
        if not a or not b or a == b or not is_coupled_diff_pair(a, b):
            continue
        if present is not None and (a not in present or b not in present):
            continue
        try:
            value = float(gap)
        except (TypeError, ValueError):
            continue
        if not value > 0:
            continue
        # ``MatchDpSuffix`` orientation: '+' / 'P' is the positive net.
        p, n = (a, b) if _positive_suffix(a) else (b, a)
        value = round(value, 6)
        best[(p, n)] = max(value, best.get((p, n), 0.0))
    return sorted((p, n, gap) for (p, n), gap in best.items())


def _positive_suffix(net_name: str) -> bool:
    """Whether ``net_name``'s ``MatchDpSuffix`` polarity character is ``+``/``P``."""
    for ch in reversed(net_name):
        if (ch.isascii() and ch.isdigit()) or ch == "_":
            continue
        return ch in "+P"
    return False


def merge_pair_gap_block(existing: str | None, rules: list[tuple[str, str, float]]) -> str | None:
    """Merge the router diff-pair gap block into ``.kicad_dru`` content.

    Same contract as :func:`merge_netclass_block`: rules present -> replace
    any existing block with a fresh one appended at the end; no rules ->
    remove a stale block, otherwise return ``existing`` unchanged.
    Idempotent.
    """
    pattern = re.compile(
        r"\n*" + re.escape(DRU_PAIR_GAP_BLOCK_BEGIN) + r".*?" + re.escape(DRU_PAIR_GAP_BLOCK_END),
        re.DOTALL,
    )
    had_block = existing is not None and pattern.search(existing) is not None
    base = pattern.sub("", existing).lstrip("\n") if had_block and existing else existing
    if not rules:
        if not had_block or base is None:
            return existing
        return base.rstrip("\n") + "\n" if base.strip() else ""

    body = [
        "# The gap the router laid each coupled pair at: max(class intra-pair",
        "# gap, manufacturer minimum, authored board min_clearance).",
        *(render_pair_gap_rule(p, n, gap) for p, n, gap in rules),
    ]
    block = f"{DRU_PAIR_GAP_BLOCK_BEGIN}\n" + "\n".join(body) + f"\n{DRU_PAIR_GAP_BLOCK_END}"
    if base is None or not base.strip():
        return f"{_DRU_VERSION_HEADER}\n\n{block}\n"
    prefix = base.rstrip("\n") + "\n"
    if not re.search(r"^\s*\(version\b", base, re.MULTILINE):
        prefix = f"{_DRU_VERSION_HEADER}\n" + prefix
    return f"{prefix}\n{block}\n"
