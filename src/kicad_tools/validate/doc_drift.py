"""LLM-free doc-drift lint for ``kct check`` (Issue #4540).

The ``doc_drift`` category diffs a fixed, machine-parseable doc contract
against committed artifacts.  It never parses free prose: the only thing
it evaluates is the opt-in **doc-pin marker**, an HTML comment placed
adjacent to a numeric claim in a markdown doc:

.. code-block:: markdown

    <!-- kct:doc-pin drc-tolerance boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb = 8 -->

A marker names a *resolver* (``drc-tolerance``), a *key* (here a
repo-relative routed-PCB path), and the *claimed value*.  The check
resolves ground truth via a small resolver registry and compares.  Two
rule ids, both :data:`Severity.INFO` -- advisory by construction, never
entering ``error_count``, the check verdict, exit codes, or any fab /
tapeout gate:

* ``doc_drift_stale_pin`` -- the marker's claimed value differs from the
  resolved ground truth (the README says one number, the machine source
  of truth says another).
* ``doc_drift_unresolvable_pin`` -- the marker names an unknown resolver
  or a key the resolver cannot resolve (e.g. a typo'd board path).  This
  keeps the gate from going silently vacuous when a marker rots.

v1 ships exactly one resolver:

``drc-tolerance``
    Ground truth is the per-board pin in
    ``.github/routed-drc-tolerance.yml`` (the ``tolerances:`` map keyed
    by repo-relative routed-PCB path).  **Absence of an entry resolves
    to 0** -- the yml's documented "absence == strict 0 errors"
    convention -- provided the referenced routed-PCB path exists in the
    repo; a nonexistent path is unresolvable (typo guard).

Bootstrap carve-outs (copperhead's zero-symbol discipline, per the
issue): zero markers found => the category passes silently; missing
README, undiscoverable repo root, or missing repo-root tolerance file
=> silent pass.  External users and scaffolding boards are unaffected;
the contract is strictly opt-in per claim.

The check is dispatched as a ``check_cmd``-local category (like
``pad_grid`` / ``sch_fields``) because it needs the PCB *path* -- to
locate the board-dir README -- rather than the parsed PCB object.  It is
deliberately NOT a :class:`DRCChecker` method and NOT part of
``CHECK_ALL_METHODS``.

Determinism: findings are sorted by (doc path, line number, rule id),
and only marker comments are evaluated, so repeated runs are
byte-identical for CI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from .violations import DRCResults, DRCViolation

RULE_STALE_PIN = "doc_drift_stale_pin"
RULE_UNRESOLVABLE_PIN = "doc_drift_unresolvable_pin"

#: Repo-relative location of the drc-tolerance ground-truth file.  Its
#: presence also serves as one of the repo-root discovery sentinels.
TOLERANCE_FILE = Path(".github") / "routed-drc-tolerance.yml"

# The full single-line marker form.  Anything that does not match this
# exactly (modulo flexible whitespace) is silently treated as prose --
# markers are opt-in, and a malformed comment is just a comment.
_DOC_PIN_RE = re.compile(
    r"<!--\s*kct:doc-pin\s+"
    r"(?P<resolver>[A-Za-z0-9_-]+)\s+"
    r"(?P<key>\S+)\s*=\s*"
    r"(?P<value>\S+)\s*-->"
)


@dataclass(frozen=True)
class DocPin:
    """One parsed ``kct:doc-pin`` marker."""

    resolver: str
    key: str
    claimed: str
    doc_path: Path
    line: int


@dataclass(frozen=True)
class Resolution:
    """Outcome of resolving a doc-pin's ground truth.

    ``status`` semantics:

    * ``"resolved"`` -- ground truth found; ``value`` holds it and
      ``source`` names where it came from (quoted in messages).
    * ``"unresolvable"`` -- the key cannot be resolved (typo'd path,
      unknown resolver); ``reason`` explains why.  Emits
      ``doc_drift_unresolvable_pin``.
    * ``"skip"`` -- a documented carve-out applies (e.g. the repo-root
      tolerance file is absent); the marker is silently ignored.
    """

    status: Literal["resolved", "unresolvable", "skip"]
    value: int | None = None
    source: str = ""
    reason: str = ""


def parse_doc_pins(doc_path: Path) -> list[DocPin]:
    """Extract all well-formed ``kct:doc-pin`` markers from a doc.

    Returns an empty list when the doc does not exist or is unreadable
    (carve-out: docs are optional).  Markers are single-line only; a
    comment split across lines never matches and is treated as prose.
    """
    try:
        text = doc_path.read_text(encoding="utf-8")
    except OSError:
        return []
    pins: list[DocPin] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in _DOC_PIN_RE.finditer(line):
            pins.append(
                DocPin(
                    resolver=match.group("resolver"),
                    key=match.group("key"),
                    claimed=match.group("value"),
                    doc_path=doc_path,
                    line=lineno,
                )
            )
    return pins


def find_repo_root(start: Path) -> Path | None:
    """Walk up from ``start`` looking for the repo root.

    A directory counts as the root when it contains ``.git`` or the
    tolerance file (:data:`TOLERANCE_FILE`).  Returns ``None`` when no
    ancestor qualifies (carve-out: the check silently passes outside a
    repo checkout).
    """
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists() or (candidate / TOLERANCE_FILE).is_file():
            return candidate
    return None


def _resolve_drc_tolerance(repo_root: Path, key: str) -> Resolution:
    """Resolver ``drc-tolerance``: per-board pin in the tolerance yml.

    ``key`` is the repo-relative routed-PCB path used as the yml map key.
    Absence of an entry resolves to 0 (the yml's "absence == strict 0
    errors" convention) as long as the referenced path exists in the
    repo; a nonexistent path is unresolvable so typo'd markers cannot
    silently compare against the 0 default.
    """
    tolerance_path = repo_root / TOLERANCE_FILE
    if not tolerance_path.is_file():
        # Carve-out per the issue: a repo without the tolerance file
        # passes silently (external consumers of kct check).
        return Resolution(status="skip")

    if not (repo_root / key).is_file():
        return Resolution(
            status="unresolvable",
            reason=(
                f"referenced routed-PCB path {key!r} does not exist in the repo (root {repo_root})"
            ),
        )

    import yaml

    try:
        data = yaml.safe_load(tolerance_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        return Resolution(
            status="unresolvable",
            reason=f"could not read {TOLERANCE_FILE}: {e}",
        )

    tolerances = data.get("tolerances", {}) if isinstance(data, dict) else {}
    if not isinstance(tolerances, dict):
        return Resolution(
            status="unresolvable",
            reason=f"{TOLERANCE_FILE} 'tolerances' field is not a mapping",
        )

    raw = tolerances.get(key)
    if raw is None:
        # Absence == strict 0 errors (the yml's documented convention).
        return Resolution(
            status="resolved",
            value=0,
            source=f"{TOLERANCE_FILE} (no entry => strict 0)",
        )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return Resolution(
            status="unresolvable",
            reason=f"{TOLERANCE_FILE} entry for {key!r} is not an integer: {raw!r}",
        )
    return Resolution(status="resolved", value=value, source=str(TOLERANCE_FILE))


#: Resolver registry.  v1 ships exactly one resolver; adding another is
#: one entry here plus a function above (see Issue #4540 for deferred v2
#: candidates: BOM-vs-schematic contract, version strings).
RESOLVERS: dict[str, Callable[[Path, str], Resolution]] = {
    "drc-tolerance": _resolve_drc_tolerance,
}


def _display_path(doc_path: Path, repo_root: Path | None) -> str:
    """Render ``doc_path`` repo-relative when possible (stable messages)."""
    if repo_root is not None:
        try:
            return doc_path.resolve().relative_to(repo_root).as_posix()
        except ValueError:
            pass
    return str(doc_path)


def _readme_candidates(pcb_path: Path) -> list[Path]:
    """Docs scanned for markers, given the PCB path.

    Board artifacts live at ``boards/NN-name/output/board.kicad_pcb``
    with the board README at ``boards/NN-name/README.md`` (the
    grandparent), but a PCB committed next to its README (no ``output/``
    nesting) is also supported via the parent.  Missing candidates are
    skipped by :func:`parse_doc_pins`.
    """
    resolved = pcb_path.resolve()
    candidates = [
        resolved.parent / "README.md",
        resolved.parent.parent / "README.md",
    ]
    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def check_doc_drift(pcb_path: Path) -> DRCResults:
    """Run the doc-drift lint for the board owning ``pcb_path``.

    Args:
        pcb_path: Path to the ``.kicad_pcb`` being checked.  Only the
            path is used (to locate the board README and repo root); the
            PCB is never parsed.

    Returns:
        :class:`DRCResults` containing only INFO-severity findings
        (``doc_drift_stale_pin`` / ``doc_drift_unresolvable_pin``),
        sorted by (doc path, line, rule id).  Every carve-out returns an
        empty result set.
    """
    results = DRCResults()
    results.rules_checked = 2
    results.rules_checked_by_rule[RULE_STALE_PIN] = 1
    results.rules_checked_by_rule[RULE_UNRESOLVABLE_PIN] = 1

    pins: list[DocPin] = []
    for doc in _readme_candidates(pcb_path):
        pins.extend(parse_doc_pins(doc))
    if not pins:
        return results  # Bootstrap carve-out: zero markers => silent pass.

    repo_root = find_repo_root(pcb_path.resolve().parent)
    if repo_root is None:
        return results  # Carve-out: not inside a discoverable repo.

    findings: list[tuple[tuple[str, int, str], DRCViolation]] = []
    for pin in pins:
        doc_rel = _display_path(pin.doc_path, repo_root)
        resolver = RESOLVERS.get(pin.resolver)
        if resolver is None:
            message = (
                f"{doc_rel}:{pin.line}: doc-pin names unknown resolver "
                f"{pin.resolver!r} (known: {', '.join(sorted(RESOLVERS))})"
            )
            findings.append(
                (
                    (doc_rel, pin.line, RULE_UNRESOLVABLE_PIN),
                    DRCViolation(
                        rule_id=RULE_UNRESOLVABLE_PIN,
                        severity="info",
                        message=message,
                    ),
                )
            )
            continue

        resolution = resolver(repo_root, pin.key)
        if resolution.status == "skip":
            continue
        if resolution.status == "unresolvable":
            message = (
                f"{doc_rel}:{pin.line}: doc-pin key {pin.key!r} could not be "
                f"resolved: {resolution.reason}"
            )
            findings.append(
                (
                    (doc_rel, pin.line, RULE_UNRESOLVABLE_PIN),
                    DRCViolation(
                        rule_id=RULE_UNRESOLVABLE_PIN,
                        severity="info",
                        message=message,
                    ),
                )
            )
            continue

        assert resolution.value is not None
        try:
            claimed_value: int | None = int(pin.claimed)
        except ValueError:
            claimed_value = None
        if claimed_value == resolution.value:
            continue
        message = (
            f"{doc_rel}:{pin.line}: doc-pin claims {pin.resolver} "
            f"{pin.key} = {pin.claimed}, but {resolution.source} "
            f"pins {resolution.value}"
        )
        findings.append(
            (
                (doc_rel, pin.line, RULE_STALE_PIN),
                DRCViolation(
                    rule_id=RULE_STALE_PIN,
                    severity="info",
                    message=message,
                    actual_value=float(resolution.value),
                    required_value=float(claimed_value) if claimed_value is not None else None,
                ),
            )
        )

    findings.sort(key=lambda pair: pair[0])
    for _, violation in findings:
        results.add(violation)
    return results
