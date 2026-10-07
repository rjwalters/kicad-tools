r"""Explicit ``.kicad_dru`` rules for keepout rule areas (Issue #6039).

Headless ``kicad-cli pcb drc`` **10.0.1** does not report copper inside a
keepout rule area: a track routed straight through a ``(keepout (tracks
not_allowed))`` zone passes cleanly, even though pcbnew parses the area
correctly (``GetIsRuleArea()``/``GetDoNotAllowTracks()`` are both true).  The
DRC engine *can* evaluate the area -- it just never builds the implicit
keepout rule from it.  A custom rule does fire ``items_not_allowed``::

    (rule "kct keepout wall"
      (condition "A.intersectsArea('<zone uuid>')")
      (constraint disallow track via))

So every writer that knows the board (``write_drc_constraints`` -- used by
``kct route``, ``kct check --emit-drc-constraints``, ``kct mfr apply-rules``
and the manufacturing export -- plus ``kct check``'s DRU-only emit) installs
one such rule per keepout rule area, inside its own sentinel-delimited
managed block.  Re-running replaces the block; user-authored rules and the
other kct blocks (fab floors #4600, creepage #4508) are preserved verbatim.

Behaviour measured against ``kicad-cli`` 10.0.1 (see
``tests/test_keepout_dru.py`` for the KiCad-gated half):

* ``A.intersectsArea('<uuid>')`` and ``A.intersectsArea('<name>')`` both
  resolve; we key on the **UUID** because names are optional and need not be
  unique.  A zone with no UUID falls back to its name; one with neither
  cannot be referenced and is skipped.
* ``intersectsArea`` is already layer-aware -- a B.Cu track crossing an
  F.Cu-only wall is not flagged -- so multi-layer / wildcard areas need no
  ``(layer ...)`` clause.  A single-copper-layer area additionally gets an
  explicit ``(layer "<name>")`` clause.
* ``disallow zone`` is evaluated against the zone's *fill*, not its outline:
  a pour whose saved fill was carved around a ``(copperpour not_allowed)``
  area is not flagged (board 05's 13 pour-only keepouts produce 0 new
  findings), while fill copper actually inside the area is.
* The rule area never matches itself, and overlapping areas with different
  flags each report their own items (disallow constraints accumulate rather
  than the later rule masking the earlier one).
* ``disallow pad`` / ``disallow footprint`` are likewise not enforced
  natively on 10.0.1 and fire through the explicit rule.

Footprint-embedded keepouts (Issue #6087) -- an RF module's antenna keepout,
say -- are not enforced natively either.  Measured on 10.0.1:

* ``intersectsArea('<uuid>')`` resolves a footprint-owned zone by the UUID
  written in the board file (and by name), so they get the same rule.
  Pre-KiCad-7 boards spell that id ``(tstamp ...)``; KiCad loads it into the
  same slot, so the tstamp is used as the reference.
* The parent footprint's own **pads** do trip a plain ``intersectsArea`` rule
  (the parent *footprint* object itself does not), whereas KiCad's
  implicit keepout exempts its parent.  The rule therefore adds
  ``&& !A.memberOfFootprint('<parent reference>')``.  ``A.Reference`` is no
  substitute: it is null on tracks/vias/pads, so ``A.Reference != 'AE1'``
  silently disables the whole rule.  ``memberOfFootprint`` matches the
  reference (wildcards allowed), not the footprint UUID.
* When two zones share one UUID (a footprint copied verbatim), the
  reference resolves to the **first** zone in board order only; later
  duplicates cannot be addressed and are skipped with a warning.

Expression strings: KiCad's DRC expression lexer knows exactly one escape,
``\'``; every other backslash is literal (``'a\b'`` matches a zone named
``a\b``; ``'a\\b'`` does not).  The s-expression layer around it decodes
``\\`` and ``\"`` first.  A trailing backslash would escape the closing
quote, so it is written as the ``?`` wildcard (``intersectsArea`` and
``memberOfFootprint`` match names with wildcards).

If a future KiCad restores the implicit keepout rule, a violation may be
reported twice (implicit + explicit).  That is harmless for a
zero-violations gate.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

#: Sentinel markers for the kct-owned keepout block.  Distinct from the
#: fab-floors (#4600) and creepage (#4508) markers so all three coexist.
DRU_KEEPOUT_BLOCK_BEGIN = "# BEGIN kct keepout rules (Issue #6039) -- managed, do not edit"
DRU_KEEPOUT_BLOCK_END = "# END kct keepout rules"

_DRU_VERSION_HEADER = "(version 1)"

#: Rule-name prefix of every generated keepout rule.
KEEPOUT_RULE_PREFIX = "kct keepout"

# ``(keepout ...)`` flag attribute -> KiCad ``disallow`` item type.
_FLAG_TO_DISALLOW: tuple[tuple[str, str], ...] = (
    ("tracks_allowed", "track"),
    ("vias_allowed", "via"),
    ("pads_allowed", "pad"),
    ("copperpour_allowed", "zone"),
    ("footprints_allowed", "footprint"),
)

_COPPER_LAYER_RE = re.compile(r"^(F|B|In\d+)\.Cu$")


@dataclass(frozen=True)
class KeepoutDruRule:
    """One rendered keepout rule (one per rule area that forbids anything)."""

    name: str
    reference: str  # UUID (preferred) or zone name, as passed to intersectsArea
    disallow: tuple[str, ...]
    layer: str | None = None  # explicit layer clause for single-layer areas
    # Reference of the footprint that owns the area (footprint-embedded
    # keepouts, Issue #6087): its own items are exempt, as in KiCad.
    parent_footprint: str | None = None

    def render(self) -> str:
        expr = f"A.intersectsArea('{_escape_expr_string(self.reference)}')"
        if self.parent_footprint:
            expr += f" && !A.memberOfFootprint('{_escape_expr_string(self.parent_footprint)}')"
        lines = [f"(rule {_sexp_quote(self.name)}"]
        if self.layer:
            lines.append(f"  (layer {_sexp_quote(self.layer)})")
        lines.append(f"  (condition {_sexp_quote(expr)})")
        lines.append(f"  (constraint disallow {' '.join(self.disallow)}))")
        return "\n".join(lines)


def _sexp_quote(value: str) -> str:
    """Quote ``value`` as a KiCad s-expression string literal.

    Not ``json.dumps``: that would turn non-ASCII names into ``\\uXXXX``
    escapes, which KiCad's lexer does not decode.
    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _escape_expr_string(value: str) -> str:
    r"""Escape ``value`` for a single-quoted string inside a DRC expression.

    Measured on kicad-cli 10.0.1 (Issue #6087): the expression lexer decodes
    only ``\'``; any other backslash is kept literally, so backslashes must
    NOT be doubled here (the old doubling made a backslash-named area
    unmatchable).  A trailing backslash would swallow the closing quote and
    becomes the single-character wildcard ``?`` instead.  The result is then
    s-expression quoted by :func:`_sexp_quote`.
    """
    escaped = value.replace("'", "\\'")
    if escaped.endswith("\\"):
        escaped = escaped[:-1] + "?"
    return escaped


def keepout_rules_from_zones(zones: list) -> list[KeepoutDruRule]:
    """Build one :class:`KeepoutDruRule` per keepout rule area in ``zones``.

    ``zones`` are :class:`kicad_tools.schema.pcb.Zone` objects (normally
    ``PCB.rule_areas + PCB.footprint_rule_areas`` -- the same #4605 parse
    the router's keepout projection reads).  Non-rule-area zones, areas that
    forbid nothing, areas with a degenerate polygon and areas with neither a
    UUID nor a name are skipped.  A zone whose reference repeats an earlier
    zone's is skipped with a warning: KiCad resolves the reference to the
    first zone only.  Output order follows input order, so it is
    deterministic.
    """
    rules: list[KeepoutDruRule] = []
    seen: set[str] = set()
    for zone in zones:
        keepout = getattr(zone, "keepout", None)
        if keepout is None or len(getattr(zone, "polygon", []) or []) < 3:
            continue
        disallow = tuple(kind for attr, kind in _FLAG_TO_DISALLOW if not getattr(keepout, attr))
        if not disallow:
            continue
        uuid = (zone.uuid or "").strip()
        name = (zone.name or "").strip()
        reference = uuid or name
        if not reference:
            continue
        if reference in seen:
            print(
                f"Warning: keepout rule area {reference!r} shares its id with an "
                "earlier rule area; KiCad DRC can only address the first, so no "
                "rule was emitted for this one.",
                file=sys.stderr,
            )
            continue
        seen.add(reference)
        label = f"{name} [{uuid}]" if name and uuid else reference
        parent = (getattr(zone, "parent_reference", "") or "").strip() or None
        if parent:
            label += f" in {parent}"
        layer_names = list(zone.layers) or ([zone.layer] if zone.layer else [])
        layer = (
            layer_names[0]
            if len(layer_names) == 1 and _COPPER_LAYER_RE.match(layer_names[0])
            else None
        )
        rules.append(
            KeepoutDruRule(
                name=f"{KEEPOUT_RULE_PREFIX} {label}",
                reference=reference,
                disallow=disallow,
                layer=layer,
                parent_footprint=parent,
            )
        )
    return rules


def keepout_rules_for_board(pcb_path: str | Path) -> list[KeepoutDruRule]:
    """Read ``pcb_path`` and return its keepout rules (empty when none).

    A cheap text scan short-circuits boards with no ``(keepout`` block, so
    keepout-free boards never pay for a full parse.  An unreadable board
    raises ``OSError`` (callers already treat that as a non-fatal sidecar
    failure); a board that reads but fails to parse warns on stderr and
    yields no rules.
    """
    path = Path(pcb_path)
    text = path.read_text(encoding="utf-8", errors="replace")
    if "(keepout" not in text:
        return []
    from kicad_tools.schema.pcb import PCB

    try:
        pcb = PCB.load(path)
    except Exception as exc:  # defensive: never fail a sidecar write on a bad board
        print(
            f"Warning: could not read keepout rule areas from {path} "
            f"({type(exc).__name__}: {exc}); no keepout DRC rules emitted.",
            file=sys.stderr,
        )
        return []
    return keepout_rules_from_zones(pcb.rule_areas + pcb.footprint_rule_areas)


def render_keepout_block_body(rules: list[KeepoutDruRule]) -> str:
    """Render the managed block body: a provenance comment + the rules."""
    lines = [
        "# Generated from the board's keepout rule areas, board-level (Issue #6039)",
        "# and footprint-embedded (Issue #6087).",
        "# kicad-cli 10.0.1 does not enforce rule areas on its own; these",
        "# explicit intersectsArea rules make `kicad-cli pcb drc` report them.",
    ]
    return "\n".join(lines + [rule.render() for rule in rules])


def merge_keepout_block(existing: str | None, rules: list[KeepoutDruRule]) -> str | None:
    """Merge the keepout managed block into ``.kicad_dru`` content.

    * Rules present -> drop any existing keepout block and append a fresh one
      at the end (adding ``(version 1)`` when the content has none).  Every
      byte outside the block is preserved.
    * No rules, block present -> remove the block (the board lost its last
      keepout; a stale rule would reference a zone that no longer exists).
    * No rules, no block -> return ``existing`` unchanged, so boards without
      keepouts see byte-identical sidecars.

    Returns ``None`` only when ``existing`` is ``None`` and there are no rules
    (nothing to write).  Idempotent.
    """
    pattern = re.compile(
        r"\n*" + re.escape(DRU_KEEPOUT_BLOCK_BEGIN) + r".*?" + re.escape(DRU_KEEPOUT_BLOCK_END),
        re.DOTALL,
    )
    had_block = existing is not None and pattern.search(existing) is not None
    base = pattern.sub("", existing).lstrip("\n") if had_block and existing else existing
    if not rules:
        if not had_block or base is None:
            return existing
        return base.rstrip("\n") + "\n" if base.strip() else ""

    block = (
        f"{DRU_KEEPOUT_BLOCK_BEGIN}\n{render_keepout_block_body(rules)}\n{DRU_KEEPOUT_BLOCK_END}"
    )
    if base is None or not base.strip():
        return f"{_DRU_VERSION_HEADER}\n\n{block}\n"
    prefix = base.rstrip("\n") + "\n"
    if not re.search(r"^\s*\(version\b", base, re.MULTILINE):
        prefix = f"{_DRU_VERSION_HEADER}\n" + prefix
    return f"{prefix}\n{block}\n"


def apply_keepout_rules(existing: str | None, pcb_path: str | Path) -> str | None:
    """Convenience: :func:`merge_keepout_block` with the board's own rules."""
    return merge_keepout_block(existing, keepout_rules_for_board(pcb_path))
