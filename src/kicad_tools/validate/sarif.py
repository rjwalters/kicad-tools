"""SARIF 2.1.0 output for ``kct check`` and ``kct detect-mistakes`` (Issue #6006).

`SARIF <https://docs.oasis-open.org/sarif/sarif/v2.1.0/>`_ is the static
analysis interchange format CI systems ingest (GitHub code scanning, GitLab,
Azure DevOps).  This module turns the finding dicts the JSON reports already
carry into one SARIF ``run``:

* **Severity.**  ``error`` -> ``error``, ``warning`` -> ``warning``, ``info``
  -> ``note``.  A waived finding keeps its level and gains an accepted
  ``external`` suppression carrying the waiver's reason, so SARIF consumers
  hide it the same way ``kct check`` stops counting it; a finding whose
  waiver went stale is *not* suppressed.
* **Fingerprints.**  The stable finding ``key`` (``rule|items|nets|layer``,
  Issue #5946) is the ``partialFingerprints`` entry ``kctFindingKey/v1``:
  it survives small edits, so a CI system tracks one alert across commits.
  The ``evidence_hash`` is the result ``fingerprints`` entry
  ``kctEvidence/<version>`` (``kctEvidence/ev2``): it changes exactly when the
  local evidence does, and its name changes with the evidence recipe.
* **Locations.**  SARIF regions are text positions, so each result points at
  the board file and, when a finding names a footprint (or a net), at the
  line where that footprint (or net) is defined.  The finding's position in
  **board coordinates** -- millimetres, KiCad's board frame (+Y down) -- and
  its layer ride in the location's ``properties`` (``boardLocation``), with
  the footprints / nets named as ``logicalLocations`` and any closest points
  as ``relatedLocations``.

The builder is fed plain dicts (the shape of ``DRCViolation.to_dict()``),
so the same code serves a live check, a ``--diff`` result (where each result
gets a ``baselineState``) and mistake findings.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

SARIF_VERSION = "2.1.0"
SARIF_SCHEMA_URI = (
    "https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/schemas/sarif-schema-2.1.0.json"
)
INFORMATION_URI = "https://github.com/rjwalters/kicad-tools"

KEY_FINGERPRINT = "kctFindingKey/v1"

LEVELS = {"error": "error", "warning": "warning", "info": "note"}
_LEVEL_RANK = {"error": 3, "warning": 2, "note": 1, "none": 0}

# Same separators ``validate.evidence`` uses to find the ref in ``U1.3`` etc.
_REF_SPLIT = re.compile(r"[.\-:/ ]")
_FOOTPRINT_OPEN = re.compile(r"^\s*\(footprint\s")
_REFERENCE = re.compile(
    r'^\s*\((?:property\s+"Reference"|fp_text\s+reference)\s+"((?:[^"\\]|\\.)*)"'
)
_NET_DECL = re.compile(r'^\s*\(net\s+\d+\s+"((?:[^"\\]|\\.)*)"\s*\)\s*$')


def sarif_level(severity: str | None) -> str:
    """Map a kct severity to a SARIF ``level``."""
    return LEVELS.get(str(severity or ""), "warning")


def evidence_fingerprint_name(evidence_hash: str) -> str:
    """``kctEvidence/<version>`` for an ``ev2:...`` hash."""
    version = evidence_hash.split(":", 1)[0] if ":" in evidence_hash else "v1"
    return f"kctEvidence/{version}"


def artifact_uri(path: str | Path) -> str:
    """A relative POSIX URI under the working directory, else a ``file:`` URI."""
    p = Path(path)
    try:
        resolved = p.resolve()
        rel = os.path.relpath(resolved, Path.cwd().resolve())
    except (OSError, ValueError):
        return p.as_posix()
    if not rel.startswith(".."):
        return Path(rel).as_posix()
    return resolved.as_uri()


class TextAnchors:
    """Line numbers of footprint / net definitions in a ``.kicad_pcb`` file."""

    def __init__(self, footprints: Mapping[str, int], nets: Mapping[str, int]) -> None:
        self.footprints = dict(footprints)
        self.nets = dict(nets)

    @classmethod
    def from_file(cls, path: str | Path | None) -> TextAnchors:
        footprints: dict[str, int] = {}
        nets: dict[str, int] = {}
        if path is None:
            return cls(footprints, nets)
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return cls(footprints, nets)
        footprint_line: int | None = None
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _FOOTPRINT_OPEN.match(line):
                footprint_line = lineno
                continue
            ref = _REFERENCE.match(line)
            if ref and footprint_line is not None:
                footprints.setdefault(ref.group(1), footprint_line)
                continue
            net = _NET_DECL.match(line)
            if net and net.group(1):
                nets.setdefault(net.group(1), lineno)
        return cls(footprints, nets)

    def line_for(self, items: Iterable[str], nets: Iterable[str]) -> int | None:
        for item in items:
            for candidate in (item, _REF_SPLIT.split(item, maxsplit=1)[0]):
                if candidate in self.footprints:
                    return self.footprints[candidate]
        for net in nets:
            if net in self.nets:
                return self.nets[net]
        return None


def _point(raw: Any) -> tuple[float, float] | None:
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        try:
            return (float(raw[0]), float(raw[1]))
        except (TypeError, ValueError):
            return None
    return None


def _board_location(point: tuple[float, float], layer: str | None) -> dict[str, Any]:
    loc: dict[str, Any] = {"x": point[0], "y": point[1], "units": "mm", "frame": "board"}
    if layer:
        loc["layer"] = layer
    return loc


def _location(
    finding: Mapping[str, Any], uri: str | None, anchors: TextAnchors
) -> dict[str, Any] | None:
    items = [str(i) for i in finding.get("items") or []]
    nets = [str(n) for n in finding.get("nets") or []]
    layer = finding.get("layer")
    point = _point(finding.get("location"))
    location: dict[str, Any] = {}
    if uri is not None:
        physical: dict[str, Any] = {"artifactLocation": {"uri": uri}}
        line = anchors.line_for(items, nets)
        if line is not None:
            physical["region"] = {"startLine": line}
        location["physicalLocation"] = physical
    logical = [{"name": i, "fullyQualifiedName": f"item/{i}", "kind": "element"} for i in items]
    logical += [{"name": n, "fullyQualifiedName": f"net/{n}", "kind": "element"} for n in nets]
    if logical:
        location["logicalLocations"] = logical
    if point is not None:
        location["properties"] = {"boardLocation": _board_location(point, layer)}
        location["message"] = {
            "text": f"({point[0]:.3f}, {point[1]:.3f}) mm" + (f" on {layer}" if layer else "")
        }
    return location or None


def sarif_result(
    finding: Mapping[str, Any],
    *,
    rule_index: int,
    uri: str | None,
    anchors: TextAnchors,
    baseline_state: str | None = None,
) -> dict[str, Any]:
    """Build one SARIF ``result`` from a finding dict."""
    rule_id = str(finding.get("rule_id") or "unknown")
    result: dict[str, Any] = {
        "ruleId": rule_id,
        "ruleIndex": rule_index,
        "level": sarif_level(finding.get("severity")),
        "message": {"text": str(finding.get("message") or rule_id)},
    }
    location = _location(finding, uri, anchors)
    if location is not None:
        result["locations"] = [location]
    related = []
    for idx, raw in enumerate(finding.get("closest_locations") or []):
        point = _point(raw)
        if point is None:
            continue
        related.append(
            {
                "id": idx,
                "message": {"text": f"closest point ({point[0]:.3f}, {point[1]:.3f}) mm"},
                "properties": {"boardLocation": _board_location(point, finding.get("layer"))},
            }
        )
    if related:
        result["relatedLocations"] = related
    key = finding.get("key")
    if key:
        result["partialFingerprints"] = {KEY_FINGERPRINT: str(key)}
    evidence_hash = finding.get("evidence_hash")
    if evidence_hash:
        result["fingerprints"] = {evidence_fingerprint_name(str(evidence_hash)): str(evidence_hash)}
    if finding.get("waived"):
        suppression: dict[str, Any] = {"kind": "external", "status": "accepted"}
        justification = finding.get("waiver_reason")
        issue = finding.get("waiver_issue")
        if justification or issue:
            suppression["justification"] = " ".join(
                part for part in (justification, f"({issue})" if issue else None) if part
            )
        result["suppressions"] = [suppression]
    if baseline_state is not None:
        result["baselineState"] = baseline_state
    properties: dict[str, Any] = {"severity": finding.get("severity")}
    for name in ("key", "evidence_hash", "items", "nets", "layer", "category"):
        value = finding.get(name)
        if value not in (None, "", []):
            properties[name] = value
    for name in ("actual_value", "required_value"):
        if finding.get(name) is not None:
            properties[name] = finding[name]
    if finding.get("waiver_status") == "stale":
        properties["waiverStatus"] = "stale"
        properties["staleWaiverEvidenceHash"] = finding.get("stale_waiver_evidence_hash")
        properties["staleWaiverCause"] = finding.get("stale_waiver_cause")
    result["properties"] = properties
    return result


def sarif_log(
    findings: Sequence[Mapping[str, Any]],
    *,
    tool_name: str,
    artifact: str | Path | None,
    rule_descriptions: Mapping[str, str] | None = None,
    baseline_states: Sequence[str | None] | None = None,
    run_properties: Mapping[str, Any] | None = None,
    anchors_from: str | Path | None = None,
) -> dict[str, Any]:
    """Build a complete SARIF 2.1.0 log with one run.

    Args:
        findings: Finding dicts (``DRCViolation.to_dict()`` shape).
        tool_name: ``tool.driver.name`` (e.g. ``"kct check"``).
        artifact: The analysed board file; results point at it.
        rule_descriptions: Optional ``rule_id -> short description``.
        baseline_states: Optional per-finding SARIF ``baselineState``
            (``"new"``, ``"updated"``, ``"absent"``, ``"unchanged"``).
        run_properties: Extra run-level properties (e.g. the summary).
        anchors_from: File to read footprint / net line numbers from
            (defaults to ``artifact``).
    """
    from kicad_tools import __version__

    if baseline_states is not None and len(baseline_states) != len(findings):
        raise ValueError("baseline_states must align with findings")
    descriptions = dict(rule_descriptions or {})
    rule_ids: list[str] = []
    levels: dict[str, str] = {}
    for f in findings:
        rid = str(f.get("rule_id") or "unknown")
        if rid not in levels:
            rule_ids.append(rid)
            levels[rid] = "none"
        level = sarif_level(f.get("severity"))
        if _LEVEL_RANK[level] > _LEVEL_RANK[levels[rid]]:
            levels[rid] = level
    index = {rid: i for i, rid in enumerate(rule_ids)}
    rules = [
        {
            "id": rid,
            "name": rid,
            "shortDescription": {"text": descriptions.get(rid) or rid},
            "defaultConfiguration": {"level": levels[rid]},
        }
        for rid in rule_ids
    ]
    uri = artifact_uri(artifact) if artifact is not None else None
    anchors = TextAnchors.from_file(anchors_from if anchors_from is not None else artifact)
    results = [
        sarif_result(
            f,
            rule_index=index[str(f.get("rule_id") or "unknown")],
            uri=uri,
            anchors=anchors,
            baseline_state=baseline_states[i] if baseline_states is not None else None,
        )
        for i, f in enumerate(findings)
    ]
    run: dict[str, Any] = {
        "tool": {
            "driver": {
                "name": tool_name,
                "version": str(__version__),
                "informationUri": INFORMATION_URI,
                "rules": rules,
            }
        },
        "invocations": [{"executionSuccessful": True}],
        "results": results,
    }
    if uri is not None:
        run["artifacts"] = [{"location": {"uri": uri}}]
    if run_properties:
        run["properties"] = dict(run_properties)
    return {"$schema": SARIF_SCHEMA_URI, "version": SARIF_VERSION, "runs": [run]}


def diff_sarif_log(
    diff: Mapping[str, Any],
    *,
    tool_name: str,
    artifact: str | Path | None,
    anchors_from: str | Path | None = None,
) -> dict[str, Any]:
    """SARIF for a ``kct check --diff`` result: introduced / changed / resolved.

    Introduced findings are ``baselineState: "new"``, changed ones (same key,
    evidence moved) ``"updated"`` and resolved ones ``"absent"``.  Unchanged
    findings are counted in the run properties only (the diff does not list
    them).
    """
    findings: list[Mapping[str, Any]] = []
    states: list[str | None] = []
    for row in diff.get("introduced") or []:
        findings.append(row)
        states.append("new")
    for row in diff.get("changed") or []:
        findings.append(row.get("new") or {})
        states.append("updated")
    for row in diff.get("resolved") or []:
        findings.append(row)
        states.append("absent")
    properties: dict[str, Any] = {"summary": dict(diff.get("summary") or {})}
    for side in ("old", "new"):
        info = diff.get(side)
        if isinstance(info, Mapping) and info.get("spec") is not None:
            properties[f"{side}Spec"] = info.get("spec")
    return sarif_log(
        findings,
        tool_name=tool_name,
        artifact=artifact,
        baseline_states=states,
        run_properties=properties,
        anchors_from=anchors_from,
    )
