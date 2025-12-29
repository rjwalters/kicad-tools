"""DRC report parsing for KiCad text and JSON formats."""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .violation import DRCViolation, Location, Severity, ViolationType


@dataclass
class DRCReport:
    """Parsed DRC report from KiCad."""

    source_file: str
    created_at: Optional[datetime]
    pcb_name: str
    violations: list[DRCViolation] = field(default_factory=list)
    footprint_errors: int = 0

    @property
    def violation_count(self) -> int:
        """Total number of violations."""
        return len(self.violations)

    @property
    def error_count(self) -> int:
        """Number of error-level violations."""
        return sum(1 for v in self.violations if v.is_error)

    @property
    def warning_count(self) -> int:
        """Number of warning-level violations."""
        return sum(1 for v in self.violations if not v.is_error)

    @property
    def errors(self) -> list[DRCViolation]:
        """Get only error-level violations."""
        return [v for v in self.violations if v.is_error]

    @property
    def warnings(self) -> list[DRCViolation]:
        """Get only warning-level violations."""
        return [v for v in self.violations if not v.is_error]

    def by_type(self, vtype: ViolationType) -> list[DRCViolation]:
        """Get violations of a specific type."""
        return [v for v in self.violations if v.type == vtype]

    def by_net(self, net_name: str) -> list[DRCViolation]:
        """Get violations involving a specific net."""
        return [v for v in self.violations if net_name in v.nets]

    def violations_by_type(self) -> dict[ViolationType, list[DRCViolation]]:
        """Group violations by type."""
        result: dict[ViolationType, list[DRCViolation]] = {}
        for v in self.violations:
            if v.type not in result:
                result[v.type] = []
            result[v.type].append(v)
        return result

    def violations_near(
        self, x_mm: float, y_mm: float, radius_mm: float = 5.0
    ) -> list[DRCViolation]:
        """Find violations within a radius of a point."""
        result = []
        for v in self.violations:
            for loc in v.locations:
                dx = loc.x_mm - x_mm
                dy = loc.y_mm - y_mm
                if (dx * dx + dy * dy) <= (radius_mm * radius_mm):
                    result.append(v)
                    break
        return result

    def summary(self) -> dict:
        """Generate a summary of the report."""
        by_type = self.violations_by_type()
        return {
            "pcb_name": self.pcb_name,
            "total_violations": self.violation_count,
            "errors": self.error_count,
            "warnings": self.warning_count,
            "footprint_errors": self.footprint_errors,
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
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "pcb_name": self.pcb_name,
            "violation_count": self.violation_count,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "footprint_errors": self.footprint_errors,
            "violations": [v.to_dict() for v in self.violations],
        }

    @classmethod
    def load(cls, path: Path | str) -> "DRCReport":
        """Load a DRC report from file (auto-detects format)."""
        path = Path(path)
        content = path.read_text()

        # Detect format
        if content.strip().startswith("{"):
            return parse_json_report(content, str(path))
        else:
            return parse_text_report(content, str(path))


def parse_text_report(content: str, source_file: str = "") -> DRCReport:
    """Parse KiCad text-format DRC report (.rpt).

    Format example:
        ** Drc report for board.kicad_pcb **
        ** Created on 2025-12-28T21:29:34-0800 **

        ** Found 606 DRC violations **
        [clearance]: Clearance violation (netclass 'Default' clearance 0.2000 mm; actual 0.1154 mm)
            Rule: netclass 'Default'; error
            @(162.4500 mm, 100.3250 mm): Pad 6 [<no net>] of U3 on F.Cu
            @(161.6000 mm, 100.9000 mm): Via [SPI_NSS] on F.Cu - B.Cu

        ** Found 0 Footprint errors **
        ** End of Report **
    """
    lines = content.splitlines()
    violations: list[DRCViolation] = []

    # Parse header
    pcb_name = ""
    created_at = None
    footprint_errors = 0

    header_pcb = re.search(r"\*\* Drc report for (.+?) \*\*", content)
    if header_pcb:
        pcb_name = header_pcb.group(1)

    header_date = re.search(r"\*\* Created on (.+?) \*\*", content)
    if header_date:
        date_str = header_date.group(1).strip()
        try:
            # Python 3.10 fromisoformat doesn't handle all timezone formats
            # Try to normalize the timezone format first
            if re.match(r".*[+-]\d{4}$", date_str):
                # Convert -0800 to -08:00 format
                date_str = date_str[:-2] + ":" + date_str[-2:]
            created_at = datetime.fromisoformat(date_str)
        except ValueError:
            pass

    # Parse footprint error count
    fp_match = re.search(r"\*\* Found (\d+) Footprint errors \*\*", content)
    if fp_match:
        footprint_errors = int(fp_match.group(1))

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
                violations.append(_build_violation(current_violation))

            current_violation = {
                "type_str": violation_match.group(1),
                "message": violation_match.group(2),
                "rule": "",
                "severity": Severity.ERROR,
                "locations": [],
                "items": [],
                "nets": [],
            }

            # Extract values from message
            _extract_values(current_violation, current_violation["message"])
            continue

        if current_violation is None:
            continue

        # Rule line: Rule: ...; severity
        rule_match = re.match(r"\s+Rule:\s*(.+);\s*(error|warning)", line, re.IGNORECASE)
        if rule_match:
            current_violation["rule"] = rule_match.group(1)
            current_violation["severity"] = Severity.from_string(rule_match.group(2))
            continue

        # Location line: @(x mm, y mm): description
        loc_match = re.match(r"\s+@\s*\(\s*([\d.]+)\s*mm\s*,\s*([\d.]+)\s*mm\s*\):\s*(.+)", line)
        if loc_match:
            x_mm = float(loc_match.group(1))
            y_mm = float(loc_match.group(2))
            description = loc_match.group(3)

            # Extract layer from description
            layer = ""
            layer_match = re.search(r"on\s+(F\.Cu|B\.Cu|[\w.]+)", description)
            if layer_match:
                layer = layer_match.group(1)

            current_violation["locations"].append(Location(x_mm, y_mm, layer))
            current_violation["items"].append(description)

            # Extract net names from [NetName] pattern
            net_matches = re.findall(r"\[([^\]]+)\]", description)
            for net in net_matches:
                if net != "<no net>" and net not in current_violation["nets"]:
                    current_violation["nets"].append(net)

    # Don't forget the last violation
    if current_violation:
        violations.append(_build_violation(current_violation))

    return DRCReport(
        source_file=source_file,
        created_at=created_at,
        pcb_name=pcb_name,
        violations=violations,
        footprint_errors=footprint_errors,
    )


def parse_json_report(content: str, source_file: str = "") -> DRCReport:
    """Parse KiCad JSON-format DRC report.

    KiCad 8+ can output DRC reports in JSON format with --format json.
    """
    data = json.loads(content)

    # Extract metadata
    pcb_name = data.get("source", "")
    created_at = None
    if "date" in data:
        try:
            created_at = datetime.fromisoformat(data["date"])
        except ValueError:
            pass

    violations: list[DRCViolation] = []

    for item in data.get("violations", []):
        type_str = item.get("type", "unknown")
        message = item.get("description", "")
        severity = Severity.from_string(item.get("severity", "error"))
        rule = item.get("rule", "")

        locations: list[Location] = []
        items: list[str] = []
        nets: list[str] = []

        # Parse position
        if "pos" in item:
            pos = item["pos"]
            locations.append(
                Location(
                    x_mm=pos.get("x", 0),
                    y_mm=pos.get("y", 0),
                )
            )

        # Parse items
        for item_data in item.get("items", []):
            desc = item_data.get("description", "")
            items.append(desc)

            if "pos" in item_data:
                pos = item_data["pos"]
                locations.append(
                    Location(
                        x_mm=pos.get("x", 0),
                        y_mm=pos.get("y", 0),
                    )
                )

            # Extract nets
            if "net" in item_data:
                net = item_data["net"]
                if net and net not in nets:
                    nets.append(net)

        violation = DRCViolation(
            type=ViolationType.from_string(type_str),
            type_str=type_str,
            severity=severity,
            message=message,
            rule=rule,
            locations=locations,
            items=items,
            nets=nets,
        )
        _extract_values(violation.__dict__, message)
        violations.append(violation)

    return DRCReport(
        source_file=source_file,
        created_at=created_at,
        pcb_name=pcb_name,
        violations=violations,
        footprint_errors=data.get("footprint_errors", 0),
    )


def _extract_values(data: dict, message: str) -> None:
    """Extract numeric values from violation message."""
    # Pattern: "clearance X.XXXX mm; actual Y.YYYY mm"
    clearance_match = re.search(
        r"clearance\s+([\d.]+)\s*mm.*?actual\s+([\d.]+)\s*mm", message, re.IGNORECASE
    )
    if clearance_match:
        data["required_value_mm"] = float(clearance_match.group(1))
        data["actual_value_mm"] = float(clearance_match.group(2))
        return

    # Pattern: "width X.XXXX mm; actual Y.YYYY mm"
    width_match = re.search(
        r"width\s+([\d.]+)\s*mm.*?actual\s+([\d.]+)\s*mm", message, re.IGNORECASE
    )
    if width_match:
        data["required_value_mm"] = float(width_match.group(1))
        data["actual_value_mm"] = float(width_match.group(2))


def _build_violation(data: dict) -> DRCViolation:
    """Build a DRCViolation from parsed data."""
    return DRCViolation(
        type=ViolationType.from_string(data["type_str"]),
        type_str=data["type_str"],
        severity=data["severity"],
        message=data["message"],
        rule=data.get("rule", ""),
        locations=data.get("locations", []),
        items=data.get("items", []),
        nets=data.get("nets", []),
        required_value_mm=data.get("required_value_mm"),
        actual_value_mm=data.get("actual_value_mm"),
    )
