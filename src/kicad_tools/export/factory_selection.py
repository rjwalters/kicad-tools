"""Compare declared factory-selected CSV evidence; never authorize assembly.

Column names and selection tokens are explicit caller policy, not a presumed
supplier CSV schema. Source kind is a declaration, not authenticated provenance.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal, Sequence

_REFERENCE = re.compile(r"[A-Za-z]+[0-9]+\Z")


@dataclass(frozen=True)
class ExpectedPart:
    """One expected physical reference with approved exact identity strings."""

    reference: str
    catalog_id: str = ""
    mpn: str = ""


@dataclass(frozen=True)
class SelectionMapping:
    """Explicit CSV schema and exact case-sensitive selection token policy."""

    reference: str
    selected: str
    selected_values: tuple[str, ...] | list[str]
    unselected_values: tuple[str, ...] | list[str]
    catalog_id: str | None = None
    mpn: str | None = None
    group_separator: Literal[",", ";"] = ","
    identity: Literal["catalog_id", "mpn", "both"] = "both"


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def compare_factory_selection(
    expected: Sequence[ExpectedPart],
    factory_csv: bytes,
    mapping: SelectionMapping,
    *,
    source_kind: str,
) -> dict[str, Any]:
    """Return content-bound comparison evidence, including every expected ref.

    Invalid caller plans/mappings raise ValueError. Invalid CSV/evidence produces
    an incomplete report; a validly parsed selection that differs is a mismatch.
    Groups are literal comma/semicolon-separated references (letters + digits),
    with surrounding reference whitespace stripped. Ranges are unsupported.
    Identity and selection strings are never case-folded or fuzzily normalized.
    """
    identity_fields = {
        "catalog_id": ("catalog_id",),
        "mpn": ("mpn",),
        "both": ("catalog_id", "mpn"),
    }.get(mapping.identity)
    if identity_fields is None or mapping.group_separator not in {",", ";"}:
        raise ValueError("Unsupported identity policy or grouped-reference separator")
    selected = mapping.selected_values
    unselected = mapping.unselected_values
    if not isinstance(selected, (tuple, list)) or not isinstance(unselected, (tuple, list)):
        raise ValueError(
            "Selection tokens must be tuple/list sequences, not scalar or mapping values"
        )
    if (
        not selected
        or not unselected
        or any(not isinstance(v, str) or not v for v in (*selected, *unselected))
        or len({*selected, *unselected}) != len((*selected, *unselected))
    ):
        raise ValueError("Selection tokens must be nonempty, unique and disjoint")
    columns = [mapping.reference, mapping.selected]
    columns.extend(getattr(mapping, field) for field in identity_fields)
    if any(not isinstance(c, str) or not c for c in columns) or len(set(columns)) != len(columns):
        raise ValueError("Required mapped columns must be nonempty and distinct")
    plan: dict[str, ExpectedPart] = {}
    for part in expected:
        if (
            not isinstance(part.reference, str)
            or not _REFERENCE.fullmatch(part.reference)
            or part.reference in plan
        ):
            raise ValueError("Expected references must be unique letters followed by digits")
        for field in ("catalog_id", "mpn"):
            value = getattr(part, field)
            if not isinstance(value, str) or (field in identity_fields and not value.strip()):
                raise ValueError(f"{part.reference}: missing/invalid exact {field}")
        plan[part.reference] = part
    if not plan:
        raise ValueError("Expected plan must contain references")

    issues: list[str] = []
    coverage: dict[str, dict[str, Any]] = {
        ref: {"reference": ref, "expected": asdict(part), "rows": [], "issues": []}
        for ref, part in sorted(plan.items())
    }
    report: dict[str, Any] = {
        "schema_version": 1,
        "source_kind": source_kind,
        "source_authentication": "caller_declared",
        "factory_csv_sha256": hashlib.sha256(factory_csv).hexdigest(),
        "expected_plan_sha256": _digest([asdict(plan[ref]) for ref in sorted(plan)]),
        "comparison_policy_sha256": _digest(asdict(mapping)),
        "comparison_matches": False,
        "factory_matched": False,
        "release_eligible": False,
        "status": "incomplete",
        "issues": issues,
        "coverage": list(coverage.values()),
    }
    malformed = source_kind != "factory_selected_csv"
    if malformed:
        issues.append(
            "Source kind must declare factory_selected_csv; expected lists are not evidence"
        )
    try:
        reader = csv.reader(io.StringIO(factory_csv.decode("utf-8-sig"), newline=""), strict=True)
        header = next(reader, [])
        if not header or len(set(header)) != len(header) or any(not h for h in header):
            raise ValueError("Missing, empty or duplicate CSV headers")
        if any(column not in header for column in columns):
            raise ValueError("Required mapped columns absent from CSV")
        for row in reader:
            line = reader.line_num
            if len(row) != len(header):
                raise ValueError(f"CSV line {line}: row width differs from header")
            record = dict(zip(header, row, strict=True))
            refs = [ref.strip() for ref in record[mapping.reference].split(mapping.group_separator)]
            if any(not _REFERENCE.fullmatch(ref) for ref in refs):
                malformed = True
                issues.append(f"CSV line {line}: malformed grouped references (ranges unsupported)")
                continue
            token = record[mapping.selected]
            known_selection = token in (*selected, *unselected)
            if not known_selection:
                malformed = True
                issues.append(f"CSV line {line}: unknown selection token {token!r}")
            for ref in refs:
                if ref not in coverage:
                    issues.append(f"CSV line {line}: unexpected reference {ref}")
                    continue
                item = coverage[ref]
                item["rows"].append({"line": line, "record": record})
                if len(item["rows"]) > 1:
                    item["issues"].append("Duplicate factory reference")
                if not known_selection:
                    item["issues"].append("Unknown selection token")
                elif token not in selected:
                    item["issues"].append("Reference is explicitly unselected")
                for field in identity_fields:
                    if record[getattr(mapping, field)] != getattr(plan[ref], field):
                        item["issues"].append(f"Exact {field} mismatch")
    except (UnicodeError, csv.Error, ValueError) as exc:
        malformed = True
        issues.append(str(exc))
    for ref, item in coverage.items():
        if not item["rows"]:
            item["issues"].append("Missing factory reference")
        item["matches"] = not item["issues"]
        issues.extend(f"{ref}: {issue}" for issue in item["issues"])
    report["status"] = "incomplete" if malformed else "mismatch" if issues else "compared"
    report["comparison_matches"] = not issues
    return report
