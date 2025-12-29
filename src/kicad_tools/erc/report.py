"""ERC report parsing for KiCad JSON and text formats."""

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .violation import ERCViolation, ERCViolationType, Severity


@dataclass
class ERCReport:
    """Parsed ERC report from KiCad."""

    source_file: str = ""
    kicad_version: str = ""
    coordinate_units: str = "mm"
    violations: list[ERCViolation] = field(default_factory=list)

    @property
    def violation_count(self) -> int:
        """Total number of violations (excluding excluded ones)."""
        return sum(1 for v in self.violations if not v.excluded)

    @property
    def error_count(self) -> int:
        """Number of error-level violations."""
        return sum(1 for v in self.violations if v.is_error and not v.excluded)

    @property
    def warning_count(self) -> int:
        """Number of warning-level violations."""
        return sum(1 for v in self.violations if v.severity == Severity.WARNING and not v.excluded)

    @property
    def exclusion_count(self) -> int:
        """Number of excluded violations."""
        return sum(1 for v in self.violations if v.excluded)

    @property
    def errors(self) -> list[ERCViolation]:
        """Get only error-level violations."""
        return [v for v in self.violations if v.is_error and not v.excluded]

    @property
    def warnings(self) -> list[ERCViolation]:
        """Get only warning-level violations."""
        return [v for v in self.violations if v.severity == Severity.WARNING and not v.excluded]

    @property
    def exclusions(self) -> list[ERCViolation]:
        """Get excluded violations."""
        return [v for v in self.violations if v.excluded]

    def by_type(self, vtype: ERCViolationType) -> list[ERCViolation]:
        """Get violations of a specific type."""
        return [v for v in self.violations if v.type == vtype and not v.excluded]

    def by_sheet(self, sheet: str) -> list[ERCViolation]:
        """Get violations in a specific sheet."""
        return [v for v in self.violations if v.sheet == sheet and not v.excluded]

    def violations_by_type(self) -> dict[ERCViolationType, list[ERCViolation]]:
        """Group violations by type."""
        result: dict[ERCViolationType, list[ERCViolation]] = {}
        for v in self.violations:
            if v.excluded:
                continue
            if v.type not in result:
                result[v.type] = []
            result[v.type].append(v)
        return result

    def violations_by_sheet(self) -> dict[str, list[ERCViolation]]:
        """Group violations by sheet."""
        result: dict[str, list[ERCViolation]] = defaultdict(list)
        for v in self.violations:
            if not v.excluded:
                sheet = v.sheet or "root"
                result[sheet].append(v)
        return dict(result)

    def filter_by_type(self, type_filter: str) -> list[ERCViolation]:
        """Filter violations by type (partial match on type, description, or type_description)."""
        filter_lower = type_filter.lower()
        return [
            v
            for v in self.violations
            if not v.excluded
            and (
                filter_lower in v.type_str.lower()
                or filter_lower in v.description.lower()
                or filter_lower in v.type_description.lower()
            )
        ]

    def summary(self) -> dict:
        """Generate a summary of the report."""
        by_type = self.violations_by_type()
        return {
            "source_file": self.source_file,
            "kicad_version": self.kicad_version,
            "total_violations": self.violation_count,
            "errors": self.error_count,
            "warnings": self.warning_count,
            "exclusions": self.exclusion_count,
            "by_type": {
                vtype.value: len(violations)
                for vtype, violations in sorted(
                    by_type.items(), key=lambda x: len(x[1]), reverse=True
                )
            },
        }

    def to_dict(self) -> dict:
        """Convert report to dictionary."""
        return {
            "source_file": self.source_file,
            "kicad_version": self.kicad_version,
            "coordinate_units": self.coordinate_units,
            "summary": {
                "errors": self.error_count,
                "warnings": self.warning_count,
                "exclusions": self.exclusion_count,
            },
            "violations": [v.to_dict() for v in self.violations],
        }

    @classmethod
    def load(cls, path: Path | str) -> "ERCReport":
        """Load an ERC report from file (auto-detects format)."""
        path = Path(path)
        content = path.read_text()

        # Detect format - JSON starts with {
        if content.strip().startswith("{"):
            return parse_json_report(content, str(path))
        else:
            return parse_text_report(content, str(path))


def parse_json_report(content: str, source_file: str = "") -> ERCReport:
    """Parse KiCad ERC JSON report.

    KiCad 8+ outputs ERC reports in JSON format with `kicad-cli sch erc --format json`.

    Format:
        {
            "source": "design.kicad_sch",
            "kicad_version": "8.0.0",
            "coordinate_units": "mm",
            "sheets": [
                {
                    "path": "/",
                    "uuid_path": "...",
                    "violations": [
                        {
                            "type": "pin_not_connected",
                            "severity": "error",
                            "description": "...",
                            "pos": {"x": 100, "y": 50},
                            "items": [...],
                            "excluded": false
                        }
                    ]
                }
            ]
        }
    """
    data = json.loads(content)

    report = ERCReport(
        source_file=data.get("source", source_file),
        kicad_version=data.get("kicad_version", ""),
        coordinate_units=data.get("coordinate_units", "mm"),
    )

    # Parse violations from all sheets
    for sheet_data in data.get("sheets", []):
        sheet_path = sheet_data.get("path", "")

        for item in sheet_data.get("violations", []):
            type_str = item.get("type", "unknown")
            violation = ERCViolation(
                type=ERCViolationType.from_string(type_str),
                type_str=type_str,
                severity=Severity.from_string(item.get("severity", "error")),
                description=item.get("description", ""),
                sheet=sheet_path,
                pos_x=item.get("pos", {}).get("x", 0),
                pos_y=item.get("pos", {}).get("y", 0),
                items=[i.get("description", "") for i in item.get("items", [])],
                excluded=item.get("excluded", False),
            )
            report.violations.append(violation)

    return report


def parse_text_report(content: str, source_file: str = "") -> ERCReport:
    """Parse KiCad ERC text report (.rpt format).

    Format example:
        ** ERC report for design.kicad_sch **
        ** Created on 2025-01-15 **

        ** Found 5 ERC violations **
        [pin_not_connected]: Unconnected pin
            @(100.0000 mm, 50.0000 mm): Pin 1 of R1

        ** End of Report **
    """
    report = ERCReport(source_file=source_file)
    lines = content.splitlines()

    # Parse header
    source_match = re.search(r"\*\* ERC report for (.+?) \*\*", content)
    if source_match:
        report.source_file = source_match.group(1)

    # Parse violations
    current_violation: Optional[dict] = None

    for line in lines:
        # Skip header lines
        if line.startswith("**"):
            continue

        # Start of new violation: [type]: message
        violation_match = re.match(r"\[(\w+)\]:\s*(.+)", line)
        if violation_match:
            # Save previous violation
            if current_violation:
                report.violations.append(_build_violation(current_violation))

            type_str = violation_match.group(1)
            current_violation = {
                "type_str": type_str,
                "description": violation_match.group(2),
                "severity": Severity.ERROR,
                "sheet": "",
                "pos_x": 0,
                "pos_y": 0,
                "items": [],
                "excluded": False,
            }
            continue

        if current_violation is None:
            continue

        # Location line: @(x mm, y mm): description
        loc_match = re.match(r"\s+@\s*\(\s*([\d.]+)\s*mm\s*,\s*([\d.]+)\s*mm\s*\):\s*(.+)", line)
        if loc_match:
            current_violation["pos_x"] = float(loc_match.group(1))
            current_violation["pos_y"] = float(loc_match.group(2))
            current_violation["items"].append(loc_match.group(3))
            continue

        # Severity line: severity: error/warning
        severity_match = re.match(r"\s+severity:\s*(\w+)", line, re.IGNORECASE)
        if severity_match:
            current_violation["severity"] = Severity.from_string(severity_match.group(1))
            continue

        # Sheet line
        sheet_match = re.match(r"\s+sheet:\s*(.+)", line, re.IGNORECASE)
        if sheet_match:
            current_violation["sheet"] = sheet_match.group(1).strip()

    # Don't forget the last violation
    if current_violation:
        report.violations.append(_build_violation(current_violation))

    return report


def _build_violation(data: dict) -> ERCViolation:
    """Build an ERCViolation from parsed data."""
    return ERCViolation(
        type=ERCViolationType.from_string(data["type_str"]),
        type_str=data["type_str"],
        severity=data["severity"],
        description=data["description"],
        sheet=data.get("sheet", ""),
        pos_x=data.get("pos_x", 0),
        pos_y=data.get("pos_y", 0),
        items=data.get("items", []),
        excluded=data.get("excluded", False),
    )
