"""Detect a rule-file rewrite that *loosens* authored design rules (Issue #7385).

kct has several writers that rewrite a board's rule files on an existing
project -- ``kct route`` / ``kct check --emit-drc-constraints`` (through
:func:`~kicad_tools.manufacturers.project_generator.merge_project_rules`),
``kct mfr apply-rules``, ``kicad-fab-rules apply`` and ``kct init``.  The
policy every one of them must follow is the #6191 one: **a rewrite may only
tighten authored rules**, unless the project explicitly opts into the legacy
overwrite (``KCT_PRESERVE_BOARD_RULES=0``) or the change is one of the few
documented exceptions.

:func:`rule_loosenings` is the one shared check.  It compares a rule file
*before* and *after* a rewrite and lists every way the rewrite made the rules
more permissive.  Writers call it before saving and refuse an unallowed
loosening; the guard test ``tests/test_rule_loosening_7385.py`` calls it on
every committed board.

The comparison understands two shapes behind the one entry point: the KiCad
``.kicad_pro`` mapping, and -- since Issue #8937 -- the ``.kicad_dru`` rule
file, passed as a mapping with a ``"dru_rules"`` key holding the file's
``clearance``-constraint rules in file order (see
:func:`kicad_tools.manufacturers.dru_generator.dru_clearance_rules`).  For the
DRU shape a loosening is not only a hand-written rule that dropped or lowered:
a rewrite whose *resolved* clearance for a (track, track), (pad, zone) or
(via, zone) pair is smaller than the pre-rewrite resolution -- or smaller than
the clearance the design itself authorises (net class, zone
``connect_pads``) -- fails closed.  That is the gate the #8937 fab-floor
defect needed: the unconditional ``Clearance`` floor rule *replaces* larger
authored clearances in KiCad, so an emitted block that undercuts them must be
refused, not shipped.

The signature deliberately takes plain mappings, so a later
``net_class_map.json`` variant (dropped ``swap_group``, lowered
``clearance``) can slot in behind the same entry point without changing
callers.

This module is pure (no kct imports): the *policy* that decides which
loosenings are allowed for a given project lives with the merge itself, in
:func:`kicad_tools.manufacturers.project_generator.merge_policy_allowances`.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, TypeGuard

__all__ = [
    "LOOSENING_CHANGED",
    "LOOSENING_EXCLUSION_ADDED",
    "LOOSENING_LOWERED",
    "LOOSENING_REMOVED",
    "LOOSENING_SEVERITY_DOWNGRADED",
    "NETCLASS_MINIMUM_KEYS",
    "NETCLASS_TARGET_KEYS",
    "DRU_SHAPE_KEY",
    "Loosening",
    "RuleLooseningError",
    "assert_no_loosenings",
    "rule_loosenings",
]

#: Mapping key that selects the ``.kicad_dru`` comparison shape (Issue #8937).
#: The value is the ordered list of rule records parsed from the file by
#: :func:`kicad_tools.manufacturers.dru_generator.dru_clearance_rules`.
DRU_SHAPE_KEY = "dru_rules"

#: A numeric minimum decreased.
LOOSENING_LOWERED = "lowered"
#: A ``rule_severities`` entry dropped rank (``error > warning > ignore``).
LOOSENING_SEVERITY_DOWNGRADED = "severity_downgraded"
#: A new ``drc_exclusions`` entry appeared.
LOOSENING_EXCLUSION_ADDED = "exclusion_added"
#: An authored rule, netclass, netclass key, pattern or assignment vanished.
LOOSENING_REMOVED = "removed"
#: An authored target value (diff-pair geometry, net assignment) was rewritten.
LOOSENING_CHANGED = "changed"

#: Netclass keys that are *floors*: a smaller value is more permissive.
NETCLASS_MINIMUM_KEYS: tuple[str, ...] = (
    "clearance",
    "track_width",
    "via_diameter",
    "via_drill",
    "microvia_diameter",
    "microvia_drill",
)

#: Netclass keys that are *targets*, not floors.  A diff-pair gap is the
#: coupling geometry the designer chose for an impedance; rewriting it in
#: either direction discards an authored decision (the #7385 reproduction
#: reset an authored 0.12 mm gap to the 0.25 mm template default).
NETCLASS_TARGET_KEYS: tuple[str, ...] = (
    "diff_pair_gap",
    "diff_pair_width",
    "diff_pair_via_gap",
)

_SEVERITY_RANK: dict[str, int] = {"ignore": 0, "warning": 1, "error": 2}

_TOLERANCE = 1e-9


@dataclass(frozen=True)
class Loosening:
    """One way a rewrite made the rules more permissive.

    Attributes:
        path: Stable dotted locator, e.g. ``rules.min_clearance``,
            ``defaults.clearance_min``, ``rule_severities.isolated_copper``,
            ``netclasses.Default.clearance``, ``netclasses.Power``,
            ``netclass_patterns.Power:VCC*``, ``netclass_assignments.GND``,
            ``drc_exclusions.<entry>``.  ``allow=`` patterns match against it.
        kind: One of the ``LOOSENING_*`` constants.
        before: The authored value (``None`` when it did not exist).
        after: The rewritten value (``None`` when it was removed).
    """

    path: str
    kind: str
    before: Any
    after: Any

    def describe(self) -> str:
        """Return a one-line human-readable description."""
        if self.kind == LOOSENING_LOWERED:
            return f"{self.path}: lowered {self.before} -> {self.after}"
        if self.kind == LOOSENING_SEVERITY_DOWNGRADED:
            return f"{self.path}: severity downgraded {self.before} -> {self.after}"
        if self.kind == LOOSENING_EXCLUSION_ADDED:
            return f"{self.path}: new DRC exclusion"
        if self.kind == LOOSENING_REMOVED:
            return f"{self.path}: removed (was {self.before})"
        return f"{self.path}: changed {self.before} -> {self.after}"

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form, for ``--format json`` payloads."""
        return {"path": self.path, "kind": self.kind, "before": self.before, "after": self.after}


class RuleLooseningError(ValueError):
    """A writer would have loosened authored rules without an explicit opt-in."""

    def __init__(self, loosenings: list[Loosening], writer: str = "") -> None:
        self.loosenings = loosenings
        self.writer = writer
        who = f"{writer}: " if writer else ""
        lines = "\n".join(f"  - {item.describe()}" for item in loosenings)
        super().__init__(
            f"{who}refusing to write: the rewrite would loosen "
            f"{len(loosenings)} authored rule(s):\n{lines}\n"
            "Rule rewrites may only tighten authored rules (Issue #6191/#7385). "
            "To deliberately replace the board's rules with the profile, set the "
            "project text variable KCT_PRESERVE_BOARD_RULES=0."
        )


def _is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _get(mapping: object, *keys: str) -> Any:
    current = mapping
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _as_mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _is_rule_minimum_key(key: str) -> bool:
    return key.startswith("min_")


def _is_default_minimum_key(key: str) -> bool:
    # ``design_settings.defaults`` mixes floors (``track_min_width``,
    # ``clearance_min``, ``via_min_diameter``, ``via_min_drill``) with
    # cosmetic new-object defaults (silk text size, line widths).  Only the
    # floors are rules.
    return "_min" in key or key.startswith("min_")


def _compare_minima(
    out: list[Loosening],
    prefix: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    keys: Iterable[str],
) -> None:
    for key in keys:
        old = before.get(key)
        if not _is_number(old):
            continue
        if key not in after:
            out.append(Loosening(f"{prefix}.{key}", LOOSENING_REMOVED, old, None))
            continue
        new = after.get(key)
        if not _is_number(new):
            out.append(Loosening(f"{prefix}.{key}", LOOSENING_CHANGED, old, new))
        elif new < old - _TOLERANCE:
            out.append(Loosening(f"{prefix}.{key}", LOOSENING_LOWERED, old, new))


def _netclasses(data: object) -> dict[str, Mapping[str, Any]]:
    classes = _get(data, "net_settings", "classes")
    result: dict[str, Mapping[str, Any]] = {}
    if isinstance(classes, list):
        for cls in classes:
            if isinstance(cls, Mapping) and isinstance(cls.get("name"), str):
                result.setdefault(cls["name"], cls)
    return result


def _patterns(data: object) -> list[tuple[str, str]]:
    patterns = _get(data, "net_settings", "netclass_patterns")
    result: list[tuple[str, str]] = []
    if isinstance(patterns, list):
        for entry in patterns:
            if isinstance(entry, Mapping):
                result.append((str(entry.get("netclass", "")), str(entry.get("pattern", ""))))
    return result


def _assignments(data: object) -> dict[str, str]:
    raw = _get(data, "net_settings", "netclass_assignments")
    if isinstance(raw, Mapping):
        return {str(k): json.dumps(v, sort_keys=True) for k, v in raw.items()}
    return {}


def _exclusion_key(entry: object) -> str:
    return entry if isinstance(entry, str) else json.dumps(entry, sort_keys=True)


def _project_loosenings(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[Loosening]:
    out: list[Loosening] = []

    settings_before = _as_mapping(_get(before, "board", "design_settings"))
    settings_after = _as_mapping(_get(after, "board", "design_settings"))

    # 1. Numeric floors in design_settings.rules / design_settings.defaults.
    rules_before = _as_mapping(settings_before.get("rules"))
    rules_after = _as_mapping(settings_after.get("rules"))
    _compare_minima(
        out,
        "rules",
        rules_before,
        rules_after,
        sorted(k for k in rules_before if _is_rule_minimum_key(k)),
    )
    defaults_before = _as_mapping(settings_before.get("defaults"))
    defaults_after = _as_mapping(settings_after.get("defaults"))
    _compare_minima(
        out,
        "defaults",
        defaults_before,
        defaults_after,
        sorted(k for k in defaults_before if _is_default_minimum_key(k)),
    )

    # 2. Severities: error > warning > ignore.  A removed entry falls back to
    #    KiCad's built-in severity, which we cannot know here -- report it.
    sev_before = _as_mapping(settings_before.get("rule_severities"))
    sev_after = _as_mapping(settings_after.get("rule_severities"))
    for key in sorted(sev_before):
        old = sev_before[key]
        if key not in sev_after:
            out.append(Loosening(f"rule_severities.{key}", LOOSENING_REMOVED, old, None))
            continue
        new = sev_after[key]
        old_rank = _SEVERITY_RANK.get(str(old))
        new_rank = _SEVERITY_RANK.get(str(new))
        if old_rank is not None and new_rank is not None and new_rank < old_rank:
            out.append(Loosening(f"rule_severities.{key}", LOOSENING_SEVERITY_DOWNGRADED, old, new))

    # 3. New DRC exclusions.
    excl_before = settings_before.get("drc_exclusions")
    excl_after = settings_after.get("drc_exclusions")
    known = {_exclusion_key(e) for e in excl_before} if isinstance(excl_before, list) else set()
    if isinstance(excl_after, list):
        for entry in excl_after:
            key = _exclusion_key(entry)
            if key not in known:
                out.append(
                    Loosening(f"drc_exclusions.{key}", LOOSENING_EXCLUSION_ADDED, None, entry)
                )

    # 4. Netclasses: removed classes, dropped keys, lowered floors, rewritten
    #    targets.
    classes_before = _netclasses(before)
    classes_after = _netclasses(after)
    for name in sorted(classes_before):
        old_cls = classes_before[name]
        prefix = f"netclasses.{name}"
        new_cls = classes_after.get(name)
        if new_cls is None:
            out.append(Loosening(prefix, LOOSENING_REMOVED, name, None))
            continue
        for key in sorted(old_cls):
            if key not in new_cls:
                out.append(Loosening(f"{prefix}.{key}", LOOSENING_REMOVED, old_cls[key], None))
        _compare_minima(
            out,
            prefix,
            old_cls,
            new_cls,
            [k for k in NETCLASS_MINIMUM_KEYS if k in new_cls],
        )
        for key in NETCLASS_TARGET_KEYS:
            if key in old_cls and key in new_cls:
                old, new = old_cls[key], new_cls[key]
                if _is_number(old) and _is_number(new):
                    if abs(old - new) > _TOLERANCE:
                        out.append(Loosening(f"{prefix}.{key}", LOOSENING_CHANGED, old, new))
                elif old != new:
                    out.append(Loosening(f"{prefix}.{key}", LOOSENING_CHANGED, old, new))

    # 5. Netclass patterns and explicit assignments.
    patterns_after = set(_patterns(after))
    for netclass, pattern in _patterns(before):
        if (netclass, pattern) not in patterns_after:
            out.append(
                Loosening(
                    f"netclass_patterns.{netclass}:{pattern}",
                    LOOSENING_REMOVED,
                    {"netclass": netclass, "pattern": pattern},
                    None,
                )
            )
    assign_before = _assignments(before)
    assign_after = _assignments(after)
    for net in sorted(assign_before):
        old = json.loads(assign_before[net])
        if net not in assign_after:
            out.append(Loosening(f"netclass_assignments.{net}", LOOSENING_REMOVED, old, None))
        elif assign_after[net] != assign_before[net]:
            out.append(
                Loosening(
                    f"netclass_assignments.{net}",
                    LOOSENING_CHANGED,
                    old,
                    json.loads(assign_after[net]),
                )
            )

    return out


def _loosening_shape(before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
    if DRU_SHAPE_KEY in before or DRU_SHAPE_KEY in after:
        return "dru"
    return "project"


# ---------------------------------------------------------------------------
# The .kicad_dru shape (Issue #8937)
# ---------------------------------------------------------------------------


class _UnknownCondition(Exception):
    """A condition atom the resolver does not model; the rule matches all."""


_DRU_TOKEN_RE = re.compile(
    r"""\s*(?:(?P<lparen>\()|(?P<rparen>\))|(?P<and>&&)|(?P<or>\|\|)|(?P<not>!)|(?P<eq>==)|(?P<neq>!=)|'(?P<squote>(?:[^'\\]|\\.)*)'|"(?P<dquote>(?:[^"\\]|\\.)*)"|(?P<ident>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?))"""
)


def _tokenize_dru_condition(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        match = _DRU_TOKEN_RE.match(text, pos)
        if match is None:
            if text[pos:].strip() == "":
                break
            raise _UnknownCondition(text)
        pos = match.end()
        kind = match.lastgroup
        assert kind is not None
        if kind in ("squote", "dquote"):
            tokens.append(
                (
                    "str",
                    match.group(kind).replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\"),
                )
            )
        elif kind == "lparen":
            tokens.append(("(", "("))
        elif kind == "rparen":
            tokens.append((")", ")"))
        elif kind == "and":
            tokens.append(("and", "&&"))
        elif kind == "or":
            tokens.append(("or", "||"))
        elif kind == "not":
            tokens.append(("not", "!"))
        elif kind == "eq":
            tokens.append(("cmp", "=="))
        elif kind == "neq":
            tokens.append(("cmp", "!="))
        else:
            tokens.append(("ident", match.group(kind)))
    return tokens


class _DruConditionParser:
    """Recursive-descent parser for the KiCad condition grammar kct emits.

    Produces a closure ``matcher(a, b) -> bool`` over two item contexts
    (``A``/``B``), each a mapping of DRU property name -> value.  Anything
    outside the modelled grammar raises :class:`_UnknownCondition`, which
    :func:`_rule_matches_pair` turns into ``False`` -- the rule's scope is
    unknowable, so it is excluded from pair resolution rather than treated
    as matching everything (over-approximating it would flag every
    keepout/creepage rewrite as a loosening; the name-keyed comparison in
    the same gate still catches a lowered or removed rule of that shape).
    """

    def __init__(self, text: str, *, model_router_scope: bool = False) -> None:
        self._model_router_scope = model_router_scope
        self._tokens = _tokenize_dru_condition(text)
        self._pos = 0
        self._ast = self._parse_or()
        if self._pos != len(self._tokens):
            raise _UnknownCondition(text)

    def _peek(self) -> tuple[str, str] | None:
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _next(self) -> tuple[str, str]:
        token = self._peek()
        if token is None:
            raise _UnknownCondition("unexpected end")
        self._pos += 1
        return token

    def _parse_or(self) -> Any:
        terms = [self._parse_and()]
        while (tok := self._peek()) and tok[0] == "or":
            self._next()
            terms.append(self._parse_and())
        return terms[0] if len(terms) == 1 else ("or", terms)

    def _parse_and(self) -> Any:
        terms = [self._parse_unary()]
        while (tok := self._peek()) and tok[0] == "and":
            self._next()
            terms.append(self._parse_unary())
        return terms[0] if len(terms) == 1 else ("and", terms)

    def _parse_unary(self) -> Any:
        tok = self._peek()
        if tok and tok[0] == "not":
            self._next()
            return ("not", self._parse_unary())
        return self._parse_primary()

    def _parse_primary(self) -> Any:
        tok = self._next()
        if tok[0] == "(":
            inner = self._parse_or()
            if self._next()[0] != ")":
                raise _UnknownCondition("unbalanced")
            return inner
        if tok[0] != "ident":
            raise _UnknownCondition(tok[1])
        name = tok[1]
        peeked = self._peek()
        if peeked and peeked[0] == "(":
            # A function call.  Only ``A.isCoupledDiffPair()`` -- the
            # exemption kct's own router-class rules carry (Issue #9422) --
            # is modelled; any other (intersectsCourtyard, hasNetclass, ...)
            # leaves the rule's scope unknowable.
            if name != "A.isCoupledDiffPair" or not self._model_router_scope:
                raise _UnknownCondition(name)
            self._next()
            if self._next()[0] != ")":
                raise _UnknownCondition(name)
            return ("coupled",)
        nxt = self._peek()
        if nxt and nxt[0] == "cmp":
            op = self._next()[1]
            value = self._next()
            if value[0] == "ident":
                if not self._model_router_scope:
                    raise _UnknownCondition(name)
                # Field-to-field comparison (``A.Reference == B.Reference``
                # in kct's same-footprint pad exemption).
                return ("cmpf", name, op, value[1])
            if value[0] != "str":
                raise _UnknownCondition(name)
            return ("cmp", name, op, value[1])
        raise _UnknownCondition(name)

    def matches(self, a: Mapping[str, str], b: Mapping[str, str]) -> bool:
        return self._eval(self._ast, a, b)

    @staticmethod
    def _field(name: str, a: Mapping[str, str], b: Mapping[str, str]) -> str:
        side, _, field = name.partition(".")
        item = a if side == "A" else b
        if side not in ("A", "B") or field not in item:
            raise _UnknownCondition(name)
        return item[field]

    def _eval(self, node, a: Mapping[str, str], b: Mapping[str, str]) -> bool:
        kind = node[0]
        if kind == "or":
            return any(self._eval(term, a, b) for term in node[1])
        if kind == "and":
            return all(self._eval(term, a, b) for term in node[1])
        if kind == "not":
            return not self._eval(node[1], a, b)
        if kind == "coupled":
            # The probe pairs are synthetic ordinary copper (``__net_<class>``
            # tracks, zone/pad/via contexts); none is the P/N half of a
            # coupled differential pair, so the exemption never applies.
            return False
        if kind == "cmpf":
            _, left_name, op, right_name = node
            left = self._field(left_name, a, b)
            right = self._field(right_name, a, b)
            return bool(left == right) if op == "==" else bool(left != right)
        _, name, op, value = node
        side, _, field = name.partition(".")
        if field.lower() == "type":
            # Measured on kicad-cli 10.0.6: ``A.Type`` comparisons are
            # case-insensitive ('Zone' / 'zone' / 'ZONE' all match).
            item = a if side == "A" else b
            left, right = item.get("type", "").lower(), value.lower()
        else:
            item = a if side == "A" else b
            if field not in item:
                raise _UnknownCondition(name)
            left = item[field]
            right = value
        return bool(left == right) if op == "==" else bool(left != right)


def _rule_matches_layer(rule: Mapping[str, Any], layer: str | None) -> bool:
    """Whether a rule's ``(layer ...)`` scope admits a pair on ``layer``.

    ``layer=None`` models "no particular layer": only unscoped rules match.
    """
    scope = rule.get("layer")
    if scope is None:
        return True
    if layer is None:
        return False
    # Real KiCad inner layers are ``In1.Cu``, ``In2.Cu``, ... -- never
    # ``In.Cu`` -- so the prefix test must not carry the dot (same spelling
    # as ``router.board_clearance_rules._layer_matches``).
    if scope == "outer":
        return bool(layer in ("F.Cu", "B.Cu"))
    if scope == "inner":
        return bool(layer.startswith("In") and layer.endswith(".Cu"))
    return bool(scope == layer)


def _rule_matches_pair(
    rule: Mapping[str, Any],
    a: Mapping[str, str],
    b: Mapping[str, str],
    *,
    model_router_scope: bool = False,
) -> bool:
    """Whether a rule's condition admits the ordered pair ``(a, b)``.

    KiCad evaluates a rule with the items in both orders, so both are tried.
    A condition outside the modelled grammar (``hasNetclass()``,
    ``intersectsCourtyard()``, regex literals, ...) makes the rule's scope
    unknowable here; it is excluded from resolution rather than treated as
    matching everything -- over-approximating it would flag every
    keepout/creepage/attach-zone rewrite as a loosening, while the
    name-keyed comparison still catches a lowered or removed rule of that
    shape (Issue #8937's fail-closed scope is the modelled, emitted grammar
    plus authored baselines).
    """
    condition = rule.get("condition")
    if condition is None:
        return True
    try:
        parser = _DruConditionParser(condition, model_router_scope=model_router_scope)
        return parser.matches(a, b) or parser.matches(b, a)
    except _UnknownCondition:
        return False


def _resolved_clearance(
    rules: list[Mapping[str, Any]],
    layer: str | None,
    a,
    b,
    *,
    model_router_scope: bool = False,
) -> float | None:
    """Last-match-wins clearance for one ordered pair, or ``None``."""
    resolved: float | None = None
    for rule in rules:
        if _rule_matches_layer(rule, layer) and _rule_matches_pair(
            rule, a, b, model_router_scope=model_router_scope
        ):
            value = rule.get("clearance_mm")
            if value is not None:
                resolved = value
    return resolved


def _item_context(kind: str, *, netclass: str, layer: str | None, net: str) -> dict[str, str]:
    context = {
        "type": kind,
        "NetClass": netclass,
        "NetName": net,
        "Layer": layer or "",
        "Reference": f"REF_{kind}_{netclass}",
        "Net": net,
    }
    if kind == "Pad":
        # Both pad flavours are probed by callers; the context carries the
        # SMD spelling and callers also evaluate a through-hole variant.
        context["Pad_Type"] = "SMD"
    if kind == "Via":
        context["Via_Type"] = "Standard"
    return context


def _pad_variants(netclass: str, layer: str | None, net: str) -> list[dict[str, str]]:
    contexts = []
    for pad_type in ("SMD", "Through-hole"):
        context = _item_context("Pad", netclass=netclass, layer=layer, net=net)
        context["Pad_Type"] = pad_type
        contexts.append(context)
    return contexts


def _zone_item(net: str, netclass: str, layer: str) -> dict[str, str]:
    return _item_context("Zone", netclass=netclass, layer=layer, net=net)


def _worst_resolved_for_pair(
    rules: list[Mapping[str, Any]],
    sides_a: list[dict],
    sides_b: list[dict],
    layers: list[str | None],
    *,
    model_router_scope: bool = False,
) -> float | None:
    """The smallest last-match resolution over every probed combination."""
    worst: float | None = None
    for layer in layers:
        for a in sides_a:
            for b in sides_b:
                value = _resolved_clearance(
                    rules, layer, a, b, model_router_scope=model_router_scope
                )
                if value is not None and (worst is None or value < worst):
                    worst = value
    return worst


def _authored_netclasses(authored: Mapping[str, Any]) -> dict[str, float]:
    raw = authored.get("netclasses")
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(name): value
        for name, value in raw.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def _authored_zones(authored: Mapping[str, Any]) -> list[tuple[str, str, float]]:
    raw = authored.get("zones")
    result: list[tuple[str, str, float]] = []
    if isinstance(raw, Iterable) and not isinstance(raw, (str, bytes)):
        for entry in raw:
            if (
                not isinstance(entry, (list, tuple))
                or len(entry) != 3
                or not all(isinstance(part, str) for part in entry[:2])
                or not isinstance(entry[2], (int, float))
                or isinstance(entry[2], bool)
            ):
                continue
            result.append((entry[0], entry[1], float(entry[2])))
    return result


def _dru_loosenings(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[Loosening]:
    """Compare two parsed ``.kicad_dru`` states (Issue #8937).

    ``before``/``after`` carry ``dru_rules`` (ordered rule records from
    :func:`kicad_tools.manufacturers.dru_generator.dru_clearance_rules`) and
    may carry ``managed_names`` (rule names the writer just regenerated -- its
    own output, free to move with the profile) and, on ``after``, an
    ``authored`` mapping with the design's own clearance floor sources:
    ``netclasses`` (name -> clearance mm) and ``zones`` ((layer, net, mm)
    triples from the ``.kicad_pcb`` zones' ``connect_pads`` clearance).

    Reported as loosenings:

    * a rule outside the regenerated set whose clearance minimum decreased or
      that vanished (hand-written rules, other tools' managed blocks);
    * a (track, track), (pad, zone) or (via, zone) pair whose resolved
      clearance (last matching rule wins, KiCad's documented precedence over
      the modelled condition grammar; unresolvable conditions are excluded)
      dropped below the pre-rewrite resolution, or below the clearance the
      design authorises natively: the weakest authored net class for track
      pairs, and the zone's own ``connect_pads`` clearance (against the
      weakest class) for zone pairs.
    """
    out: list[Loosening] = []
    before_rules = list(before.get(DRU_SHAPE_KEY) or [])
    after_rules = list(after.get(DRU_SHAPE_KEY) or [])
    managed = {str(name) for name in after.get("managed_names") or ()}
    managed |= {str(name) for name in before.get("managed_names") or ()}

    # 1. Authored rules by name: lowered or removed.
    after_by_name = {str(r.get("name")): r for r in after_rules}
    for rule in before_rules:
        name = str(rule.get("name"))
        if name in managed:
            continue
        value = rule.get("clearance_mm")
        if value is None:
            continue
        successor = after_by_name.get(name)
        if successor is None:
            out.append(Loosening(f"dru.{name}", LOOSENING_REMOVED, value, None))
        else:
            new = successor.get("clearance_mm")
            if new is not None and new < value - _TOLERANCE:
                out.append(Loosening(f"dru.{name}.clearance", LOOSENING_LOWERED, value, new))

    # 2. Resolved clearances per pair class, against the pre-rewrite state
    #    and the design's authored values.
    authored = _as_mapping(after.get("authored"))
    classes = _authored_netclasses(authored)
    zones = _authored_zones(authored)
    if not after_rules and not before_rules and not zones:
        return out

    weakest_class = min(classes.values()) if classes else None
    class_names = sorted(classes)
    scopes: set[str] = set()
    has_outer = has_inner = False
    for rule in after_rules + before_rules:
        scope = rule.get("layer")
        # Probe concrete copper layers only: an ``(layer outer)`` /
        # ``(layer inner)`` scope is a class of layers, not a layer name,
        # and as a probe it can never exercise the rule that carries it.
        if isinstance(scope, str) and scope.endswith(".Cu"):
            scopes.add(scope)
        elif scope == "outer":
            has_outer = True
        elif scope == "inner":
            has_inner = True
    for layer, _net, _value in zones:
        scopes.add(layer)
    probe_layers: list[str | None]
    if has_outer or has_inner:
        # A split-floor profile (Issue #9422: 2 oz outer / 0.5 oz inner) is
        # emitted as ``outer`` / ``inner``-scoped rules only.  The unscoped
        # ``None`` probe would match nothing but a hand-written unscoped rule
        # (say 0.127 mm) and report it as the board's track-track resolution,
        # although every real track lives on a concrete layer where the
        # managed scoped rules come later and win.  Probe one representative
        # concrete layer per scope class instead.  All three are probed
        # whenever *any* scoped rule exists: a one-class DRU still leaves the
        # other class's copper governed by the unscoped rules the ``None``
        # probe used to model, so dropping it without a stand-in would hide a
        # real loosening there.
        scopes.update(("F.Cu", "B.Cu", "In1.Cu"))
        probe_layers = [*sorted(scopes)]
    else:
        probe_layers = [None, *sorted(scopes)]

    def track_contexts() -> list[dict[str, str]]:
        return [
            _item_context("track", netclass=name, layer=None, net=f"__net_{name}")
            for name in class_names
        ]

    def check_pair(
        label: str,
        sides_a,
        sides_b,
        baseline: float | None,
        pair_layers: list[str | None] | None = None,
        model_router_scope: bool = False,
    ) -> None:
        probe = pair_layers if pair_layers is not None else probe_layers
        before_value = _worst_resolved_for_pair(
            before_rules, sides_a, sides_b, probe, model_router_scope=model_router_scope
        )
        after_value = _worst_resolved_for_pair(
            after_rules, sides_a, sides_b, probe, model_router_scope=model_router_scope
        )
        floor = baseline if baseline is not None else 0.0
        reference = before_value if before_value is not None else None
        limit = max(reference, floor) if reference is not None else floor
        if after_value is not None and after_value < limit - _TOLERANCE:
            out.append(Loosening(f"dru.resolved.{label}", LOOSENING_LOWERED, limit, after_value))

    if class_names:
        tracks = track_contexts()
        # Track pairs resolve the router-class "Reviewed clearance" rules kct
        # emits (``netclass_condition(router=True)``: an ``isCoupledDiffPair()``
        # call and an ``A.Reference == B.Reference`` comparison).  Left
        # unmodelled they drop out and an inner-layer probe lands on the
        # lower ``Clearance (inner)`` fab floor (Issue #9422).  Zone/via pairs
        # keep the conservative exclusion: modelling the rule there would
        # surface a pre-existing zone ``connect_pads`` vs router-class gap in
        # committed boards that this gate has never judged.
        check_pair("track_track", tracks, tracks, weakest_class, model_router_scope=True)
    for layer, net, zone_value in zones:
        # A zone's net class comes from the project; when unknown, the
        # weakest authored class is the native floor for its pairs.
        zone_netclass = f"__zone_{net}"
        zone_ctx = _zone_item(net, zone_netclass, layer)
        foreign = [
            ctx
            for name in class_names
            for ctx in (
                _item_context("track", netclass=name, layer=layer, net=f"__net_{name}"),
                *_pad_variants(name, layer, f"__net_{name}"),
                _item_context("Via", netclass=name, layer=None, net=f"__net_{name}"),
            )
        ]
        baseline = max([zone_value] + ([weakest_class] if weakest_class is not None else []))
        # A zone pair always lives on the zone's own copper layer, so the
        # layer probe is that layer alone -- never the unscoped view, where
        # the zone's own layer-scoped rule would not match.
        check_pair(f"pad_zone.{layer}.{net}", [zone_ctx], foreign, baseline, [layer])
        check_pair(f"via_zone.{layer}.{net}", [zone_ctx], foreign, baseline, [layer])
    return out


def _allowed(path: str, allow: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in allow)


def rule_loosenings(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    allow: Iterable[str] = (),
) -> list[Loosening]:
    """Return every way ``after`` is more permissive than ``before``.

    Reports, for a ``.kicad_pro`` dict:

    * a ``board.design_settings.rules.min_*`` or ``design_settings.defaults``
      floor (``track_min_width``, ``clearance_min``, ...) that decreased or
      disappeared;
    * a ``rule_severities`` entry that dropped rank (``error > warning >
      ignore``) or disappeared;
    * a new ``drc_exclusions`` entry;
    * an authored netclass that was removed, a netclass key that was dropped,
      a netclass floor (:data:`NETCLASS_MINIMUM_KEYS`) that decreased, or a
      diff-pair target (:data:`NETCLASS_TARGET_KEYS`) that was rewritten;
    * a ``netclass_patterns`` entry that was removed, and a
      ``netclass_assignments`` entry that was removed or reassigned.

    Reports, for a ``.kicad_dru`` state (a mapping with a
    :data:`DRU_SHAPE_KEY` entry; Issue #8937):

    * a rule outside the writer's regenerated set whose ``clearance``
      minimum decreased or that vanished;
    * a (track, track), (pad, zone) or (via, zone) pair whose resolved
      clearance dropped below the pre-rewrite resolution or below the
      design's authored value (weakest net class; zone ``connect_pads``
      clearance) -- the fail-closed gate for an emitted fab floor that
      would *replace* a larger authored clearance.

    Values ``before`` does not declare are never reported: adding a rule is
    not a loosening.

    Args:
        before: The rule file as authored (parsed JSON), or a parsed
            ``.kicad_dru`` state (see :data:`DRU_SHAPE_KEY`).
        after: The rule file a writer is about to save, same shape.
        allow: :mod:`fnmatch` patterns over :attr:`Loosening.path` for
            documented, permitted loosenings -- e.g. the #6191 non-blocking
            severities, or ``"*"`` under ``KCT_PRESERVE_BOARD_RULES=0``.  Build
            it with
            :func:`~kicad_tools.manufacturers.project_generator.merge_policy_allowances`.

    Returns:
        Unallowed loosenings, in a stable order; empty when the rewrite only
        tightened or kept every authored rule.
    """
    allow = tuple(allow)
    shape = _loosening_shape(before, after)
    if shape == "dru":
        items = _dru_loosenings(before, after)
    else:
        items = _project_loosenings(before, after)
    return [item for item in items if not _allowed(item.path, allow)]


def assert_no_loosenings(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    allow: Iterable[str] = (),
    writer: str = "",
) -> None:
    """Raise :class:`RuleLooseningError` when ``after`` loosens ``before``.

    Writers call this immediately before saving, so a regression in the merge
    (or a new writer that bypasses it) refuses with a list of what it would
    have lowered instead of silently loosening the board's rules.
    """
    found = rule_loosenings(before, after, allow=allow)
    if found:
        raise RuleLooseningError(found, writer)
