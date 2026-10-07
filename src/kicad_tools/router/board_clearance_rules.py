"""Per-item-pair clearance, hole clearance and edge clearance, as KiCad's DRC resolves them.

Issues #6122 and #6139 (follow-ups to #6107).  ``kct route-auto``'s foreign-copper
gate used to measure new copper at **one** board-wide clearance: the
strictest unconditional value in the ``.kicad_dru`` / ``.kicad_pro``.  KiCad's
DRC resolves the requirement **per item pair**, so that was:

* **too loose** for a net in a netclass with a larger clearance than
  ``Default`` (an ``HV`` class), and for conditional ``.kicad_dru`` rules;
* **too strict** for a board whose unconditional ``.kicad_dru`` rule is
  *below* the ``Default`` netclass -- KiCad lets the custom rule win.

It also measured nothing against **drilled holes** (KiCad's ``hole_clearance``)
or against an NPTH **slot**, which KiCad treats as board edge
(``copper_edge_clearance``).

The model, verified against ``kicad-cli pcb drc`` 10.0.1
---------------------------------------------------------
For each constraint (``clearance``, ``hole_clearance``, ``edge_clearance``,
``hole_to_hole``, ``physical_clearance``, ``physical_hole_clearance``):

1. The ``.kicad_dru`` rules are tried **last to first**; the last rule whose
   ``(condition ...)`` and ``(layer ...)`` match the pair wins outright -- it
   overrides the netclass *and* the board minimum, in both directions.  A
   condition is tried with the items in both orders (``A``/``B`` swapped).
   A winning rule with ``(severity ignore)`` switches the check off for
   that pair: it does **not** fall through to a lower rule or the default
   (issue #6150).  Its requirement is 0.
2. With no matching rule:

   * ``clearance`` is ``max(netclass(A), netclass(B), board min_clearance)``;
   * ``hole_clearance`` is ``.kicad_pro`` ``min_hole_clearance`` (KiCad's
     default 0.25 mm when the project does not declare it).  Netclass
     clearance does not apply to a bare hole;
   * ``edge_clearance`` is ``.kicad_pro`` ``min_copper_edge_clearance``
     (KiCad's default 0.5 mm);
   * ``hole_to_hole`` (drill edge to drill edge) is ``.kicad_pro``
     ``min_hole_to_hole`` (KiCad's default 0.25 mm);
   * ``physical_clearance`` and ``physical_hole_clearance`` have no default:
     only a ``.kicad_dru`` rule sets them.

``physical_clearance``, ``physical_hole_clearance`` and ``hole_to_hole`` apply
between items of the **same** net too.  KiCad reports the first two as
``clearance`` / ``hole_clearance`` violations.

A ``.kicad_dru`` that ``kicad-cli`` cannot parse -- a syntax error, a
constraint keyword or severity it does not know (``solder_mask_margin`` is
one) -- makes it ignore **every** custom rule in the file, silently.  The
gate does the same, but says so: :attr:`BoardClearanceRules.dru_error`
carries the reason and ``kct route-auto`` prints it as a warning.

Conditions
----------
:func:`evaluate_condition` is a small evaluator for KiCad's DRC expression
language.  It understands ``||``, ``&&``, ``!``, ``==`` / ``!=`` (KiCad's
case-insensitive comparison with ``*`` / ``?`` wildcards), parentheses and
these item properties:

* ``A.NetName``, ``A.NetClass``, ``A.Type`` (``'Track'`` -- an arc too, as
  ``kicad-cli`` 10.0.1 reports it -- ``'Via'``, ``'Pad'``);
* ``A.Pad_Type`` of a pad (``'SMD'``, ``'Through-hole'``,
  ``'NPTH, mechanical'``) -- the fleet's JLCPCB ``.kicad_dru`` keys its hole
  rules on it;
* ``A.hasNetclass('X')``, ``A.isPlated()``;
* ``A.Layer`` of a track: its layer, compared **case-sensitively** with
  ``*`` / ``?`` wildcards (``'F.*'`` and ``'*.Cu'`` match ``F.Cu``,
  ``'f.cu'`` does not).  A via's or pad's ``Layer`` is KiCad's null.

Everything else -- ``A.insideArea(...)`` / ``intersectsArea`` /
``enclosedByArea``, numeric properties -- evaluates to **unknown**.  A track's
or via's ``Pad_Type``, and a via's or pad's ``Layer``, is KiCad's null, which
compares false under both ``==`` and ``!=``.  The evaluator is
three-valued, and an unknown rule is handled **conservatively**: it is
treated as possibly applying, so the requirement is the *largest* of the
values it could resolve to (the unknown rule's own value and whatever a
lower-priority rule or the default would give).  The gate can therefore
refuse copper KiCad would accept on such a board, but never the reverse.
A ``(layer ...)`` clause is evaluated against the layer the new copper is on
(a via is on every layer, so its layer clause is unknown).

Netclasses
----------
A net's class comes from ``.kicad_pro`` ``net_settings.netclass_assignments``
then ``netclass_patterns`` (wildcard or regular-expression patterns), else
``Default``.  A net that matches several patterns (KiCad 9's multiple
netclasses) is measured at the largest of their clearances.  An unassigned
(net-0) item is in ``Default``.  ``Default``'s clearance is the project's own
``Default`` class, else KiCad's built-in 0.2 mm.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: KiCad's built-in ``Default`` netclass clearance, in mm.
KICAD_DEFAULT_CLEARANCE_MM = 0.2
#: KiCad's default board-setup ``min_hole_clearance``, in mm.
KICAD_DEFAULT_HOLE_CLEARANCE_MM = 0.25
#: KiCad's default board-setup ``min_copper_edge_clearance``, in mm.
KICAD_DEFAULT_EDGE_CLEARANCE_MM = 0.5
#: KiCad's default board-setup ``min_hole_to_hole``, in mm.
KICAD_DEFAULT_HOLE_TO_HOLE_MM = 0.25

logger = logging.getLogger(__name__)

CLEARANCE = "clearance"
HOLE_CLEARANCE = "hole_clearance"
EDGE_CLEARANCE = "edge_clearance"
HOLE_TO_HOLE = "hole_to_hole"
PHYSICAL_CLEARANCE = "physical_clearance"
PHYSICAL_HOLE_CLEARANCE = "physical_hole_clearance"
#: KiCad's older spellings of the two physical constraints; it reports them
#: under the same violation types (``clearance`` / ``hole_clearance``), so the
#: gate evaluates them as the ``physical_*`` equivalents.
_CONSTRAINT_ALIASES = {
    "mechanical_clearance": PHYSICAL_CLEARANCE,
    "mechanical_hole_clearance": PHYSICAL_HOLE_CLEARANCE,
}
_CONSTRAINTS = (
    CLEARANCE,
    HOLE_CLEARANCE,
    EDGE_CLEARANCE,
    HOLE_TO_HOLE,
    PHYSICAL_CLEARANCE,
    PHYSICAL_HOLE_CLEARANCE,
)

#: Every ``(constraint ...)`` keyword ``kicad-cli`` 10.0.1 accepts, each
#: verified by probing it beside a rule it would otherwise drop (issue #6150).
#: Any other keyword -- ``solder_mask_margin``, ``max_uncoupled`` -- makes
#: KiCad discard the whole file.
KICAD_CONSTRAINT_KEYWORDS = frozenset(
    {
        "annular_width",
        "assertion",
        "bridged_mask",
        "clearance",
        "connection_width",
        "courtyard_clearance",
        "creepage",
        "diff_pair_gap",
        "diff_pair_uncoupled",
        "disallow",
        "edge_clearance",
        "hole_clearance",
        "hole_size",
        "hole_to_hole",
        "length",
        "mechanical_clearance",
        "mechanical_hole_clearance",
        "min_resolved_spokes",
        "physical_clearance",
        "physical_hole_clearance",
        "silk_clearance",
        "skew",
        "solder_mask_expansion",
        "solder_mask_sliver",
        "solder_paste_abs_margin",
        "solder_paste_rel_margin",
        "text_height",
        "text_thickness",
        "thermal_relief_gap",
        "thermal_spoke_width",
        "track_angle",
        "track_segment_length",
        "track_width",
        "via_count",
        "via_dangling",
        "via_diameter",
        "zone_connection",
    }
)
#: The ``(severity ...)`` values ``kicad-cli`` 10.0.1 accepts; any other
#: (``info``) makes it discard the whole file.
KICAD_RULE_SEVERITIES = frozenset({"error", "warning", "ignore", "exclusion"})


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ItemProps:
    """What a rule condition can ask about one item of the pair.

    ``None`` in any field means *unknown*; conditions that read it evaluate
    to unknown.

    Attributes:
        type: KiCad's ``Type`` property: ``"Track"`` (arcs included),
            ``"Via"``, ``"Pad"``, or ``None`` when unknown.
        net_name: The net name (``""`` for unassigned copper).
        plated: ``A.isPlated()`` -- ``True`` for a via or plated pad,
            ``False`` for an NPTH pad.
        pad_type: KiCad's ``Pad_Type`` of a pad (``"SMD"``,
            ``"Through-hole"``, ``"NPTH, mechanical"``).
        layer: A track's copper layer (``"F.Cu"``), its ``Layer`` property.
            Ignored for a via or pad, whose ``Layer`` is KiCad's null.
    """

    type: str | None
    net_name: str | None
    plated: bool | None = None
    pad_type: str | None = None
    layer: str | None = None


# ---------------------------------------------------------------------------
# .kicad_dru rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DruRule:
    """One ``(rule ...)`` of a ``.kicad_dru``, reduced to the constraints the gate uses."""

    name: str
    condition: str | None
    layer: str | None
    constraints: tuple[tuple[str, float], ...]
    severity: str | None = None

    @property
    def ignored(self) -> bool:
        """``(severity ignore)``: where this rule wins, KiCad checks nothing."""
        return self.severity == "ignore"

    def value(self, constraint: str) -> float | None:
        for kind, value in self.constraints:
            if kind == constraint:
                return value
        return None


def _parse_length_mm(raw: object) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().lower()
    scale = 1.0
    if text.endswith("mm"):
        text = text[:-2]
    elif text.endswith("mil"):
        text, scale = text[:-3], 0.0254
    elif text.endswith("in"):
        text, scale = text[:-2], 25.4
    try:
        return float(text) * scale
    except ValueError:
        return None


def parse_dru(dru_path: str | Path) -> tuple[list[DruRule], str | None]:
    """The gate's rules of a ``.kicad_dru``, in file order, and why it was rejected.

    Returns ``(rules, None)`` for a file ``kicad-cli`` accepts, and
    ``([], reason)`` for one it would discard whole -- a syntax error, or a
    constraint keyword or severity it does not know (issue #6150).  A
    missing file gives ``([], None)``.
    """
    try:
        text = Path(dru_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], None
    try:
        from kicad_tools.sexp import parse_string

        tree = parse_string("(rules " + text + ")")
    except Exception as exc:
        return [], f"it does not parse ({type(exc).__name__}: {exc})"

    rules: list[DruRule] = []
    for rule in tree.find_all("rule"):
        atoms = rule.get_atoms()
        name = str(atoms[0]) if atoms else ""
        for constraint in rule.find_all("constraint"):
            c_atoms = constraint.get_atoms()
            keyword = str(c_atoms[0]) if c_atoms else ""
            if keyword not in KICAD_CONSTRAINT_KEYWORDS:
                return [], f"rule {name!r} has an unknown constraint {keyword!r}"
        severity_node = rule.find_child("severity")
        severity = None
        if severity_node is not None:
            raw = severity_node.get_first_atom()
            severity = str(raw).casefold() if raw is not None else ""
            if severity not in KICAD_RULE_SEVERITIES:
                return [], f"rule {name!r} has an unknown severity {severity!r}"
        cond_node = rule.find_child("condition")
        condition = None
        if cond_node is not None:
            raw = cond_node.get_first_atom()
            condition = str(raw) if raw is not None else None
        layer_node = rule.find_child("layer")
        layer = None
        if layer_node is not None:
            raw = layer_node.get_first_atom()
            layer = str(raw) if raw is not None else None
        constraints: list[tuple[str, float]] = []
        for constraint in rule.find_all("constraint"):
            c_atoms = constraint.get_atoms()
            if not c_atoms:
                continue
            c_name = _CONSTRAINT_ALIASES.get(str(c_atoms[0]), str(c_atoms[0]))
            if c_name not in _CONSTRAINTS:
                continue
            minimum = constraint.find_child("min")
            if minimum is None:
                continue
            value = _parse_length_mm(minimum.get_first_atom())
            if value is None or value < 0:
                continue
            constraints.append((c_name, value))
        if constraints:
            rules.append(DruRule(name, condition, layer, tuple(constraints), severity))
    return rules, None


def read_dru_rules(dru_path: str | Path) -> list[DruRule]:
    """The gate's rules of a ``.kicad_dru`` (see :func:`parse_dru`), in file order.

    A file ``kicad-cli`` would discard gives ``[]`` -- as KiCad applies none
    of its rules -- and logs a warning saying why.
    """
    rules, error = parse_dru(dru_path)
    if error is not None:
        logger.warning(dru_error_message(dru_path, error))
    return rules


def dru_error_message(dru_path: str | Path, error: str) -> str:
    """The warning for a ``.kicad_dru`` that ``kicad-cli`` discards whole."""
    return (
        f"{Path(dru_path).name}: {error}. kicad-cli ignores every custom rule in a "
        ".kicad_dru it cannot parse, without an error; route-auto's clearance gate "
        "does the same and falls back to the .kicad_pro netclasses and board "
        "minimums (issue #6150). Fix the file to have its rules enforced."
    )


# ---------------------------------------------------------------------------
# Condition evaluation (three-valued: True / False / None = unknown)
# ---------------------------------------------------------------------------


class _Unknown:
    """A value the evaluator cannot know."""

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "UNKNOWN"


_UNKNOWN = _Unknown()


class _Null:
    """A property the item does not have (a track's ``Pad_Type``).

    KiCad compares it as false under ``==`` *and* ``!=`` (verified with
    ``kicad-cli`` 10.0.1), so ``!(A.Pad_Type == 'X')`` is true while
    ``A.Pad_Type != 'X'`` is false.
    """

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "NULL"


_NULL = _Null()


class _LayerName(str):
    """A track's ``Layer``: compared case-sensitively, with wildcards.

    Verified with ``kicad-cli`` 10.0.1: ``A.Layer == 'F.*'`` and ``'*.Cu'``
    match a track on ``F.Cu``; ``'f.cu'`` does not.
    """


_TOKEN_RE = re.compile(
    r"""
    \s*(?:
        (?P<str>'[^']*'|"[^"]*")
      | (?P<num>\d+(?:\.\d*)?(?:mm|mil|in)?)
      | (?P<op>&&|\|\||==|!=|<=|>=|<|>|!|\(|\)|,)
      | (?P<ident>[A-Za-z_][A-Za-z0-9_.]*)
    )
    """,
    re.VERBOSE,
)


class ConditionError(ValueError):
    """The condition could not be parsed."""


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if m is None or m.end() == pos:
            raise ConditionError(f"cannot tokenize {text[pos:]!r}")
        pos = m.end()
        kind = m.lastgroup
        assert kind is not None
        tokens.append((kind, m.group(kind)))
        # Skip trailing whitespace so the loop ends cleanly.
        while pos < len(text) and text[pos].isspace():
            pos += 1
    return tokens


def _not3(v: bool | None) -> bool | None:
    return None if v is None else not v


def _and3(a: bool | None, b: bool | None) -> bool | None:
    if a is False or b is False:
        return False
    if a is None or b is None:
        return None
    return True


def _or3(a: bool | None, b: bool | None) -> bool | None:
    if a is True or b is True:
        return True
    if a is None or b is None:
        return None
    return False


def _truth(value: Any) -> bool | None:
    if value is _UNKNOWN:
        return None
    if value is _NULL:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return None


def _strings_equal(a: str, b: str) -> bool:
    """KiCad string equality: case-insensitive, with ``*`` / ``?`` wildcards.

    Verified with ``kicad-cli`` 10.0.1: ``A.NetName == '/b'``,
    ``A.NetName == '/B*'`` and ``A.NetName == '/?'`` all match net ``/B``.
    """
    folded_a, folded_b = a.casefold(), b.casefold()
    return (
        folded_a == folded_b
        or fnmatch.fnmatchcase(folded_a, folded_b)
        or fnmatch.fnmatchcase(folded_b, folded_a)
    )


class _Evaluator:
    def __init__(self, tokens: list[tuple[str, str]], ctx: _PairContext):
        self.tokens = tokens
        self.pos = 0
        self.ctx = ctx

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _take(self, value: str | None = None) -> tuple[str, str]:
        tok = self._peek()
        if tok is None or (value is not None and tok[1] != value):
            raise ConditionError(f"expected {value!r}, got {tok!r}")
        self.pos += 1
        return tok

    def parse(self) -> bool | None:
        result = self._or()
        if self._peek() is not None:
            raise ConditionError(f"trailing tokens {self.tokens[self.pos :]!r}")
        return _truth(result) if not isinstance(result, bool) else result

    def _or(self) -> Any:
        left = self._and()
        while (tok := self._peek()) is not None and tok[1] == "||":
            self._take()
            right = self._and()
            left = _or3(_truth(left), _truth(right))
        return left

    def _and(self) -> Any:
        left = self._not()
        while (tok := self._peek()) is not None and tok[1] == "&&":
            self._take()
            right = self._not()
            left = _and3(_truth(left), _truth(right))
        return left

    def _not(self) -> Any:
        tok = self._peek()
        if tok is not None and tok[1] == "!":
            self._take()
            return _not3(_truth(self._not()))
        return self._cmp()

    def _cmp(self) -> Any:
        left = self._primary()
        tok = self._peek()
        if tok is None or tok[1] not in ("==", "!=", "<", ">", "<=", ">="):
            return left
        op = self._take()[1]
        right = self._primary()
        if left is _UNKNOWN or right is _UNKNOWN:
            return None
        if left is _NULL or right is _NULL:
            return False
        if op in ("==", "!="):
            if isinstance(left, _LayerName) or isinstance(right, _LayerName):
                if not (isinstance(left, str) and isinstance(right, str)):
                    return None
                eq = (
                    left == right
                    or fnmatch.fnmatchcase(left, right)
                    or fnmatch.fnmatchcase(right, left)
                )
            elif isinstance(left, str) and isinstance(right, str):
                eq = _strings_equal(left, right)
            elif isinstance(left, (int, float)) and isinstance(right, (int, float)):
                eq = left == right
            elif isinstance(left, bool) or isinstance(right, bool):
                eq = _truth(left) == _truth(right)
            else:
                return None
            return eq if op == "==" else _not3(eq)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return {
                "<": left < right,
                ">": left > right,
                "<=": left <= right,
                ">=": left >= right,
            }[op]
        return None

    def _primary(self) -> Any:
        tok = self._take()
        kind, text = tok
        if text == "(":
            value = self._or()
            self._take(")")
            return value
        if kind == "str":
            return text[1:-1]
        if kind == "num":
            value = _parse_length_mm(text)
            return _UNKNOWN if value is None else value
        if kind == "ident":
            nxt = self._peek()
            if nxt is not None and nxt[1] == "(":
                self._take("(")
                args: list[Any] = []
                if (p := self._peek()) is not None and p[1] != ")":
                    args.append(self._or())
                    while (p := self._peek()) is not None and p[1] == ",":
                        self._take()
                        args.append(self._or())
                self._take(")")
                return self.ctx.call(text, args)
            return self.ctx.prop(text)
        raise ConditionError(f"unexpected token {text!r}")


def _norm(name: str) -> str:
    return name.replace("_", "").casefold()


class _PairContext:
    def __init__(self, rules: BoardClearanceRules, a: ItemProps, b: ItemProps):
        self.rules = rules
        self.items = {"a": a, "b": b}

    def _split(self, text: str) -> tuple[ItemProps | None, str]:
        head, _, tail = text.partition(".")
        item = self.items.get(head.casefold()) if tail else None
        return item, _norm(tail)

    def prop(self, text: str) -> Any:
        item, name = self._split(text)
        if item is None:
            if text.casefold() in ("true", "false"):
                return text.casefold() == "true"
            return _UNKNOWN
        if name == "netname":
            return _UNKNOWN if item.net_name is None else item.net_name
        if name == "netclass":
            classes = self.rules.netclasses_of(item.net_name)
            return classes[0] if classes is not None and len(classes) == 1 else _UNKNOWN
        if name == "type":
            return _UNKNOWN if item.type is None else item.type
        if name == "padtype":
            if item.type in ("Track", "Via"):
                return _NULL
            return _UNKNOWN if item.pad_type is None else item.pad_type
        if name == "layer":
            if item.type in ("Via", "Pad"):
                return _NULL  # verified with kicad-cli 10.0.1 (issue #6150)
            if item.type == "Track" and item.layer is not None:
                return _LayerName(item.layer)
            return _UNKNOWN
        return _UNKNOWN

    def call(self, text: str, args: list[Any]) -> Any:
        item, name = self._split(text)
        if item is None:
            return _UNKNOWN
        if name == "isplated" and not args:
            return _UNKNOWN if item.plated is None else item.plated
        if name == "hasnetclass" and len(args) == 1 and isinstance(args[0], str):
            classes = self.rules.netclasses_of(item.net_name)
            if classes is None:
                return _UNKNOWN
            return any(_strings_equal(c, args[0]) for c in classes)
        return _UNKNOWN


def evaluate_condition(
    condition: str, a: ItemProps, b: ItemProps, rules: BoardClearanceRules | None = None
) -> bool | None:
    """Evaluate a KiCad DRC rule condition for the ordered pair ``(A, B)``.

    Returns ``True`` / ``False``, or ``None`` when the answer depends on
    something the evaluator does not model (or the condition does not parse).
    """
    try:
        tokens = _tokenize(condition)
        return _Evaluator(tokens, _PairContext(rules or BoardClearanceRules(), a, b)).parse()
    except ConditionError:
        return None


def _layer_matches(clause: str | None, layer: str | None) -> bool | None:
    if clause is None:
        return True
    if layer is None:
        return None
    c = clause.casefold()
    if c == "outer":
        return layer in ("F.Cu", "B.Cu")
    if c == "inner":
        return layer.startswith("In") and layer.endswith(".Cu")
    return clause == layer


# ---------------------------------------------------------------------------
# The board's rules
# ---------------------------------------------------------------------------


def _pattern_matches(pattern: str, net_name: str) -> bool:
    if fnmatch.fnmatchcase(net_name, pattern):
        return True
    try:
        return re.fullmatch(pattern, net_name) is not None
    except re.error:
        return False


@dataclass
class BoardClearanceRules:
    """Everything a board's ``.kicad_pro`` and ``.kicad_dru`` say about clearance.

    Construct with :meth:`from_board` (or directly, in tests).
    """

    class_clearance: dict[str, float] = field(default_factory=dict)
    netclass_assignments: dict[str, str] = field(default_factory=dict)
    netclass_patterns: list[tuple[str, str]] = field(default_factory=list)
    min_clearance: float | None = None
    min_hole_clearance: float = KICAD_DEFAULT_HOLE_CLEARANCE_MM
    min_edge_clearance: float = KICAD_DEFAULT_EDGE_CLEARANCE_MM
    min_hole_to_hole: float = KICAD_DEFAULT_HOLE_TO_HOLE_MM
    dru_rules: list[DruRule] = field(default_factory=list)
    #: Why the ``.kicad_dru`` was discarded (see :func:`parse_dru`), else ``None``.
    dru_error: str | None = None
    _cache: dict[tuple[Any, ...], float] = field(default_factory=dict, repr=False)

    # -- loading ------------------------------------------------------------

    @classmethod
    def from_board(cls, pcb_path: str | Path) -> BoardClearanceRules:
        """Read the ``.kicad_pro`` / ``.kicad_dru`` beside ``pcb_path``.  Never raises."""
        path = Path(pcb_path)
        dru_path = path.with_suffix(".kicad_dru")
        dru_rules, error = parse_dru(dru_path)
        rules = cls(
            dru_rules=dru_rules,
            dru_error=None if error is None else dru_error_message(dru_path, error),
        )
        try:
            data = json.loads(path.with_suffix(".kicad_pro").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            rules._read_project(data)
        return rules

    def _read_project(self, data: dict) -> None:
        board = data.get("board")
        settings = board.get("design_settings") if isinstance(board, dict) else None
        if isinstance(settings, dict):
            section = settings.get("rules")
            if isinstance(section, dict):
                value = _parse_length_mm(section.get("min_clearance"))
                if value is not None and value > 0:
                    self.min_clearance = value
                value = _parse_length_mm(section.get("min_hole_clearance"))
                if value is not None and value >= 0:
                    self.min_hole_clearance = value
                value = _parse_length_mm(section.get("min_copper_edge_clearance"))
                if value is not None and value >= 0:
                    self.min_edge_clearance = value
                value = _parse_length_mm(section.get("min_hole_to_hole"))
                if value is not None and value >= 0:
                    self.min_hole_to_hole = value
        net_settings = data.get("net_settings")
        if not isinstance(net_settings, dict):
            return
        for entry in net_settings.get("classes") or []:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            value = _parse_length_mm(entry.get("clearance"))
            if isinstance(name, str) and name and value is not None and value >= 0:
                self.class_clearance[name] = value
        assignments = net_settings.get("netclass_assignments")
        if isinstance(assignments, dict):
            for net, cls_name in assignments.items():
                if isinstance(cls_name, list):  # KiCad 9 may list several
                    cls_name = cls_name[0] if len(cls_name) == 1 else None
                if isinstance(net, str) and isinstance(cls_name, str):
                    self.netclass_assignments[net] = cls_name
        for entry in net_settings.get("netclass_patterns") or []:
            if isinstance(entry, dict):
                cls_name, pattern = entry.get("netclass"), entry.get("pattern")
                if isinstance(cls_name, str) and isinstance(pattern, str) and pattern:
                    self.netclass_patterns.append((cls_name, pattern))

    # -- netclasses ---------------------------------------------------------

    def default_class_clearance(self) -> float:
        return self.class_clearance.get("Default", KICAD_DEFAULT_CLEARANCE_MM)

    def netclasses_of(self, net_name: str | None) -> tuple[str, ...] | None:
        """The netclass(es) of a net; ``None`` when the net is unknown."""
        if net_name is None:
            return None
        if not net_name:
            return ("Default",)
        assigned = self.netclass_assignments.get(net_name)
        if assigned is not None:
            return (assigned,)
        matched = tuple(
            dict.fromkeys(c for c, p in self.netclass_patterns if _pattern_matches(p, net_name))
        )
        return matched or ("Default",)

    def class_clearance_of(self, net_name: str | None) -> float:
        """The netclass clearance of a net (the largest, when it is ambiguous)."""
        classes = self.netclasses_of(net_name)
        if classes is None:
            values = [self.default_class_clearance(), *self.class_clearance.values()]
            return max(values)
        return max(
            self.class_clearance.get(
                c, self.default_class_clearance() if c == "Default" else KICAD_DEFAULT_CLEARANCE_MM
            )
            for c in classes
        )

    # -- resolution ---------------------------------------------------------

    def _default(self, constraint: str, a: ItemProps, b: ItemProps) -> float:
        if constraint == CLEARANCE:
            value = max(self.class_clearance_of(a.net_name), self.class_clearance_of(b.net_name))
            if self.min_clearance is not None:
                value = max(value, self.min_clearance)
            return value
        if constraint == HOLE_CLEARANCE:
            return self.min_hole_clearance
        if constraint == EDGE_CLEARANCE:
            return self.min_edge_clearance
        if constraint == HOLE_TO_HOLE:
            return self.min_hole_to_hole
        return 0.0  # physical_clearance / physical_hole_clearance: rules only

    def required(
        self, constraint: str, a: ItemProps, b: ItemProps, layer: str | None = None
    ) -> float:
        """The ``constraint`` minimum KiCad applies between ``a`` and ``b`` on ``layer``.

        The largest value it could be when a rule's condition is unknown
        (see the module docstring).  0 where an ``ignore`` rule wins.
        """
        key = (constraint, a, b, layer)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        candidates: list[float] = []
        settled = False
        for rule in reversed(self.dru_rules):
            value = rule.value(constraint)
            if value is None:
                continue
            applies = _layer_matches(rule.layer, layer)
            if rule.condition is not None and applies is not False:
                applies = _and3(
                    applies,
                    _or3(
                        evaluate_condition(rule.condition, a, b, self),
                        evaluate_condition(rule.condition, b, a, self),
                    ),
                )
            if applies is False:
                continue
            candidates.append(0.0 if rule.ignored else value)
            if applies is True:
                settled = True
                break
        if not settled:
            candidates.append(self._default(constraint, a, b))
        result = max(candidates)
        self._cache[key] = result
        return result

    def clearance(self, a: ItemProps, b: ItemProps, layer: str | None = None) -> float:
        return self.required(CLEARANCE, a, b, layer)

    def hole_clearance(self, a: ItemProps, b: ItemProps, layer: str | None = None) -> float:
        return self.required(HOLE_CLEARANCE, a, b, layer)

    def edge_clearance(self, a: ItemProps, b: ItemProps, layer: str | None = None) -> float:
        return self.required(EDGE_CLEARANCE, a, b, layer)

    def hole_to_hole(self, a: ItemProps, b: ItemProps, layer: str | None = None) -> float:
        return self.required(HOLE_TO_HOLE, a, b, layer)

    def physical_clearance(self, a: ItemProps, b: ItemProps, layer: str | None = None) -> float:
        return self.required(PHYSICAL_CLEARANCE, a, b, layer)

    def physical_hole_clearance(
        self, a: ItemProps, b: ItemProps, layer: str | None = None
    ) -> float:
        return self.required(PHYSICAL_HOLE_CLEARANCE, a, b, layer)

    def board_clearance(self) -> float:
        """Copper clearance between two unassigned (``Default``-class) tracks.

        The board-wide figure messages quote and the fallback a caller with no
        per-pair context uses; per-pair values may be larger or smaller.
        """
        probe = ItemProps("Track", "")
        return self.clearance(probe, probe, None)

    def net_requirement(self, net_name: str | None, counterparts: Iterable[str]) -> float:
        """The largest copper clearance ``net_name`` needs from any of ``counterparts``.

        What a router must keep that net's new tracks and vias from every other
        net on the board (``""`` stands for unassigned copper and is always
        included) so that no pair the gate measures is too close: the larger
        of each pair's ``clearance`` and ``physical_clearance``.
        """
        best = 0.0
        names = set(counterparts) | {""}
        if net_name is not None:
            names.discard(net_name)
        for other in names:
            for ta in ("Track", "Via"):
                for tb in ("Track", "Via", "Pad"):
                    a, b = ItemProps(ta, net_name), ItemProps(tb, other)
                    best = max(
                        best,
                        self.clearance(a, b, None),
                        self.physical_clearance(a, b, None),
                    )
        return best

    def max_requirement(self) -> float:
        """An upper bound on any requirement :meth:`required` can return."""
        values: list[float] = [
            self.default_class_clearance(),
            self.min_hole_clearance,
            self.min_edge_clearance,
            self.min_hole_to_hole,
            *self.class_clearance.values(),
        ]
        if self.min_clearance is not None:
            values.append(self.min_clearance)
        for rule in self.dru_rules:
            values.extend(v for _, v in rule.constraints)
        return max(values)

    def declares_conditional_rules(self) -> bool:
        return any(r.condition is not None for r in self.dru_rules)


#: The schema pad ``type`` keyword -> KiCad's ``Pad_Type`` string
#: (verified with ``kicad-cli`` 10.0.1).
PAD_TYPE_NAMES = {
    "smd": "SMD",
    "thru_hole": "Through-hole",
    "np_thru_hole": "NPTH, mechanical",
}


def item_props_for_type(
    kind: str,
    net_name: str | None,
    plated: bool | None = None,
    pad_type: str | None = None,
    layer: str | None = None,
) -> ItemProps:
    """:class:`ItemProps` for a gate item kind (``track``/``arc``/``via``/``pad``).

    KiCad reports an arc's ``Type`` as ``'Track'`` (verified with ``kicad-cli``
    10.0.1).  ``layer`` is kept for a track or arc only.
    """
    dru_type = {"track": "Track", "arc": "Track", "via": "Via", "pad": "Pad"}.get(kind)
    return ItemProps(
        dru_type,
        net_name,
        plated,
        PAD_TYPE_NAMES.get(pad_type or "") if kind == "pad" else None,
        layer if dru_type == "Track" else None,
    )


__all__: Sequence[str] = [
    "CLEARANCE",
    "EDGE_CLEARANCE",
    "HOLE_CLEARANCE",
    "HOLE_TO_HOLE",
    "KICAD_CONSTRAINT_KEYWORDS",
    "KICAD_DEFAULT_CLEARANCE_MM",
    "KICAD_DEFAULT_EDGE_CLEARANCE_MM",
    "KICAD_DEFAULT_HOLE_CLEARANCE_MM",
    "KICAD_DEFAULT_HOLE_TO_HOLE_MM",
    "KICAD_RULE_SEVERITIES",
    "PAD_TYPE_NAMES",
    "PHYSICAL_CLEARANCE",
    "PHYSICAL_HOLE_CLEARANCE",
    "BoardClearanceRules",
    "DruRule",
    "ItemProps",
    "dru_error_message",
    "evaluate_condition",
    "item_props_for_type",
    "parse_dru",
    "read_dru_rules",
]
