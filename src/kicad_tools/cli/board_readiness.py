"""Validate saved manufacturing evidence against the current board files."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path

from .known_issues import KNOWN_ISSUES_FILENAME, defects, validate_known_issues


def read_readiness(board_dir: Path) -> dict:
    """Load the v1 sidecar, failing closed on missing or changed evidence.

    Paths and hashes are relative to the board directory, never timestamps.
    This validates evidence provenance; the report writer runs the checks.
    """
    unknown = {"status": "unverified", "blockers": ["Readiness has not been verified."]}
    try:
        data = json.loads((board_dir / "output/readiness.json").read_text())
        if not isinstance(data, dict):
            raise ValueError("report must be an object")
        if (
            data.get("schema_version") != 1
            or data.get("mode") not in {"assembly", "pcb_only"}
            or data.get("status") not in {"ready", "blocked", "unverified"}
        ):
            raise ValueError("unsupported readiness report")
        if not isinstance(data.get("checked_at"), str):
            raise ValueError("invalid verification timestamp")
        datetime.fromisoformat(data["checked_at"].replace("Z", "+00:00"))
        if not isinstance(data.get("blockers"), list) or not all(
            isinstance(item, str) for item in data["blockers"]
        ):
            raise ValueError("invalid blockers")
        checks = data.get("checks", [])
        metrics = data.get("metrics", {})
        if not isinstance(metrics, dict):
            raise ValueError("invalid current metrics")
        for name in ("drc_violations", "lvs_mismatches", "nets_routed_pct"):
            if name in metrics:
                value = metrics[name]
                if (
                    type(value) not in (int, float)
                    or not math.isfinite(value)
                    or value < 0
                    or (name != "nets_routed_pct" and value != int(value))
                    or (name == "nets_routed_pct" and value > 100)
                ):
                    raise ValueError(f"invalid current metric: {name}")
        if "lvs_clean" in metrics and not isinstance(metrics["lvs_clean"], bool):
            raise ValueError("invalid current metric: lvs_clean")
        if not isinstance(checks, list) or any(
            not isinstance(c, dict)
            or not isinstance(c.get("name"), str)
            or c.get("status") not in {"passed", "failed", "not_run"}
            or ("detail" in c and not isinstance(c["detail"], str))
            for c in checks
        ):
            raise ValueError("invalid checks")
        inputs = data.get("inputs")
        if not isinstance(inputs, dict) or not inputs:
            raise ValueError("missing input hashes")
        root = board_dir.resolve()
        for name, expected in inputs.items():
            path = Path(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name:
                raise ValueError("input path must be board-relative")
            target = (root / path).resolve()
            if not target.is_relative_to(root):
                raise ValueError("input path leaves board directory")
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual != expected:
                raise ValueError(f"Evidence is stale: {name} changed")
        external = data.get("external_inputs")
        if external is not None:
            declared = data.get("project_root")
            if not isinstance(external, dict) or not isinstance(declared, str):
                raise ValueError("invalid external inputs")
            project_root = (root / declared).resolve()
            if project_root == root or not root.is_relative_to(project_root):
                raise ValueError("project root must be an ancestor of the board directory")
            for name, expected in external.items():
                path = Path(name)
                if path.is_absolute() or ".." in path.parts or "\\" in name:
                    raise ValueError("external input path must be project-root-relative")
                target = (project_root / path).resolve()
                if not target.is_relative_to(project_root):
                    raise ValueError("external input path leaves project root")
                if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                    raise ValueError(f"Evidence is stale: {name} changed")
        output = board_dir / "output"
        required = {
            p.relative_to(board_dir).as_posix()
            for p in output.iterdir()
            if p.is_file()
            and (
                p.suffix in {".kicad_pro", ".kicad_dru"}
                or p.name.endswith("net_class_map.json")
                or p.name == "fab_profile.json"
            )
        }
        if not required.issubset(inputs):
            raise ValueError("Evidence omits current project or rule files")
        if "manufacturer" in data and (
            not isinstance(data["manufacturer"], str) or not data["manufacturer"]
        ):
            raise ValueError("invalid manufacturer")
        known_issues = _bound_known_issues(data, inputs, root)
        if data["status"] == "ready" and defects(known_issues):
            numbers = ", ".join(f"#{entry['issue']}" for entry in defects(known_issues))
            raise ValueError(f"Ready conflicts with known defect {numbers}")
        if data["status"] == "ready":
            if not {"kct_check", "native_drc", "artifacts", "bom"}.issubset(
                c["name"] for c in checks
            ):
                raise ValueError("Ready requires kct_check, native_drc, artifacts and bom checks")
            if (
                not any(n.endswith(".kicad_pcb") for n in inputs)
                or not any(n.endswith(".kicad_sch") for n in (*inputs, *(external or ())))
                or "output/manufacturing/manifest.json" not in inputs
            ):
                raise ValueError("Ready requires PCB, schematic and manufacturing evidence")
            if data["blockers"] or any(c["status"] != "passed" for c in checks):
                raise ValueError("Ready conflicts with incomplete or failed checks")
        if data["status"] == "unverified":
            data.pop("metrics", None)
        return data
    except FileNotFoundError:
        return {**unknown, "blockers": ["Readiness report or checked files are missing."]}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {**unknown, "blockers": [f"Readiness needs verification: {exc}"]}


def _bound_known_issues(data: dict, inputs: dict, root: Path) -> list[dict]:
    """Validate ``known_issues`` and require it to match its hashed evidence (#7261).

    The report itself is not hashed, so the list is also written to a
    ``known-issues.json`` evidence file listed in ``inputs`` (whose hash was
    checked above).  A list missing from that file, or differing from it, is
    an edited or stale record.  A ``known-issues.json`` left in the evidence
    folder but absent from ``inputs`` is an unbound-and-dropped record (#7323).
    Only the record's own evidence folder(s) are scanned: ``--verify`` writes
    to ``output/readiness-verification/`` while the committed evidence stays in
    ``output/readiness/``, and neither folder orphans the other (#7337).
    """
    known_issues = validate_known_issues(data.get("known_issues"))
    orphans = sorted(
        rel
        for folder in _evidence_folders(data, inputs)
        for path in (root / folder).rglob(KNOWN_ISSUES_FILENAME)
        if path.is_file() and (rel := path.relative_to(root).as_posix()) not in inputs
    )
    if orphans:
        raise ValueError(f"{orphans[0]} is not listed in inputs (orphaned known-issues evidence)")
    bound = [name for name in inputs if Path(name).name == KNOWN_ISSUES_FILENAME]
    if len(bound) > 1:
        raise ValueError("known_issues is bound to more than one evidence file")
    if not bound:
        if known_issues:
            raise ValueError("known_issues is not bound to hashed evidence")
        return known_issues
    recorded = json.loads((root / bound[0]).read_text())
    if (
        not isinstance(recorded, dict)
        or validate_known_issues(recorded.get("known_issues")) != known_issues
    ):
        raise ValueError("known_issues differs from its hashed evidence")
    return known_issues


def _evidence_folders(data: dict, inputs: dict) -> set[str]:
    """Board-relative evidence folders the record itself names (#7337).

    The folder of an evidence path is its first directory below ``output/``
    (``output/readiness/refilled-native/x.json`` -> ``output/readiness``), or
    its first directory when the board has no ``output/`` folder.  Folders come
    from the ``evidence`` map and from any bound ``known-issues.json``; a
    record naming neither falls back to the default ``output/readiness``.
    """
    evidence = data.get("evidence")
    names = (
        [v for v in evidence.values() if isinstance(v, str)] if isinstance(evidence, dict) else []
    )
    names += [n for n in inputs if Path(n).name == KNOWN_ISSUES_FILENAME]
    folders = set()
    for name in names:
        parts = Path(name).parts
        if ".." in parts or Path(name).is_absolute():
            continue
        depth = 2 if parts[:1] == ("output",) else 1
        if len(parts) > depth:
            folders.add(Path(*parts[:depth]).as_posix())
    return folders or {"output/readiness"}
