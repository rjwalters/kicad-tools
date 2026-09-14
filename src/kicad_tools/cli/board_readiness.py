"""Validate saved manufacturing evidence against the current board files."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path


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
        if data["status"] == "ready":
            if not {"kct_check", "native_drc", "artifacts", "bom"}.issubset(
                c["name"] for c in checks
            ):
                raise ValueError("Ready requires kct_check, native_drc, artifacts and bom checks")
            if (
                not any(n.endswith(".kicad_pcb") for n in inputs)
                or not any(n.endswith(".kicad_sch") for n in inputs)
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
