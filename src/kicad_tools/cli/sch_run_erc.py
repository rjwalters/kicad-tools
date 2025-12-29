#!/usr/bin/env python3
"""
Run Electrical Rules Check (ERC) on a KiCad schematic.

Usage:
    python3 sch-run-erc.py <schematic.kicad_sch> [options]

Options:
    --format {text,json}   Output format (default: text)
    --severity {all,error,warning}  Filter by severity
    --exit-code            Exit with non-zero if violations found
    --output <file>        Write report to file

Examples:
    # Run ERC and show results
    python3 sch-run-erc.py project.kicad_sch

    # Get JSON output for parsing
    python3 sch-run-erc.py project.kicad_sch --format json

    # Exit with error code for CI
    python3 sch-run-erc.py project.kicad_sch --exit-code
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class ERCViolation:
    """A single ERC violation."""

    severity: str  # "error" or "warning"
    code: str  # e.g., "pin_not_connected"
    message: str
    sheet: str
    location: str  # e.g., "(100.0, 50.0)"

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "sheet": self.sheet,
            "location": self.location,
        }


@dataclass
class ERCReport:
    """ERC report with all violations."""

    schematic: str
    violations: List[ERCViolation]
    error_count: int
    warning_count: int

    def to_dict(self) -> dict:
        return {
            "schematic": self.schematic,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "violations": [v.to_dict() for v in self.violations],
        }


def find_kicad_cli() -> Optional[str]:
    """Find the kicad-cli executable."""
    # Try PATH first
    try:
        result = subprocess.run(
            ["which", "kicad-cli"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass

    # Try common macOS locations
    mac_paths = [
        "/opt/homebrew/bin/kicad-cli",
        "/usr/local/bin/kicad-cli",
        "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    ]
    for path in mac_paths:
        if os.path.exists(path):
            return path

    return None


def parse_json_report(json_text: str, schematic: str) -> ERCReport:
    """Parse KiCad's JSON ERC report format."""
    data = json.loads(json_text)

    violations = []
    error_count = 0
    warning_count = 0

    # KiCad 8.x JSON format
    for sheet in data.get("sheets", []):
        sheet_path = sheet.get("path", "")
        for violation in sheet.get("violations", []):
            severity = violation.get("severity", "warning")
            if severity == "error":
                error_count += 1
            else:
                warning_count += 1

            violations.append(
                ERCViolation(
                    severity=severity,
                    code=violation.get("type", "unknown"),
                    message=violation.get("description", ""),
                    sheet=sheet_path,
                    location=f"({violation.get('pos', {}).get('x', 0)}, {violation.get('pos', {}).get('y', 0)})",
                )
            )

    return ERCReport(
        schematic=schematic,
        violations=violations,
        error_count=error_count,
        warning_count=warning_count,
    )


def parse_text_report(text: str, schematic: str) -> ERCReport:
    """Parse KiCad's text ERC report format."""
    violations = []
    error_count = 0
    warning_count = 0

    # Pattern for parsing ERC report lines
    # Format: [type]: description @(x, y): message
    # Example: [pin_not_connected]: Pin not connected @(100.33mm, 50.80mm): U1 pin 5 (PWR_FLAG)

    for line in text.split("\n"):
        line = line.strip()

        # Skip empty lines and headers
        if not line or line.startswith("ERC report") or line.startswith("**"):
            continue

        # Check for error/warning markers
        if "[" in line and "]:" in line:
            # Extract violation type
            match = re.match(r"\[(\w+)\]:\s*(.*)", line)
            if match:
                code = match.group(1)
                rest = match.group(2)

                # Determine severity from code or context
                severity = "error" if "error" in code.lower() else "warning"

                # Try to extract location
                loc_match = re.search(r"@\s*\(([^)]+)\)", rest)
                location = loc_match.group(1) if loc_match else ""

                # Message is the rest
                message = rest
                if loc_match:
                    message = rest[: loc_match.start()].strip() + rest[loc_match.end() :].strip()

                violations.append(
                    ERCViolation(
                        severity=severity,
                        code=code,
                        message=message,
                        sheet="",
                        location=location,
                    )
                )

                if severity == "error":
                    error_count += 1
                else:
                    warning_count += 1

    return ERCReport(
        schematic=schematic,
        violations=violations,
        error_count=error_count,
        warning_count=warning_count,
    )


def run_erc(
    schematic: str,
    severity: str = "all",
    output_format: str = "text",
) -> ERCReport:
    """Run ERC on the schematic and return parsed results."""
    kicad_cli = find_kicad_cli()
    if not kicad_cli:
        raise RuntimeError(
            "kicad-cli not found. Please install KiCad 8.x or add kicad-cli to PATH."
        )

    # Create temp file for output
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json" if output_format != "text" else ".rpt",
        delete=False,
    ) as f:
        output_file = f.name

    try:
        # Build command
        cmd = [
            kicad_cli,
            "sch",
            "erc",
            "--output",
            output_file,
            "--format",
            "json",  # Always get JSON for parsing
            "--severity-all",
        ]
        cmd.append(schematic)

        # Run kicad-cli
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )

        # Read and parse output
        if os.path.exists(output_file):
            with open(output_file, "r") as f:
                content = f.read()

            if content.strip():
                return parse_json_report(content, schematic)

        # If no output file or empty, return empty report
        return ERCReport(
            schematic=schematic,
            violations=[],
            error_count=0,
            warning_count=0,
        )

    finally:
        # Clean up temp file
        if os.path.exists(output_file):
            os.unlink(output_file)


def format_text_output(report: ERCReport, severity_filter: str = "all") -> str:
    """Format report as human-readable text."""
    lines = []
    lines.append(f"ERC Report: {Path(report.schematic).name}")
    lines.append("=" * 60)

    if not report.violations:
        lines.append("✓ No ERC violations found")
    else:
        lines.append(f"Found {report.error_count} errors, {report.warning_count} warnings")
        lines.append("")

        for v in report.violations:
            # Filter by severity
            if severity_filter == "error" and v.severity != "error":
                continue
            if severity_filter == "warning" and v.severity != "warning":
                continue

            icon = "❌" if v.severity == "error" else "⚠️"
            lines.append(f"{icon} [{v.code}]")
            if v.message:
                lines.append(f"   {v.message}")
            if v.location:
                lines.append(f"   Location: {v.location}")
            if v.sheet:
                lines.append(f"   Sheet: {v.sheet}")
            lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Run ERC on a KiCad schematic",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format")
    parser.add_argument(
        "--severity", choices=["all", "error", "warning"], default="all", help="Filter by severity"
    )
    parser.add_argument(
        "--exit-code", action="store_true", help="Exit with non-zero if violations found"
    )
    parser.add_argument("--output", "-o", help="Write report to file")

    args = parser.parse_args()

    # Validate input
    if not os.path.exists(args.schematic):
        print(f"Error: File not found: {args.schematic}", file=sys.stderr)
        sys.exit(1)

    try:
        report = run_erc(args.schematic, args.severity)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Format output
    if args.format == "json":
        output = json.dumps(report.to_dict(), indent=2)
    else:
        output = format_text_output(report, args.severity)

    # Write or print
    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Report written to {args.output}")
    else:
        print(output)

    # Exit code
    if args.exit_code:
        if args.severity == "error" and report.error_count > 0:
            sys.exit(1)
        elif args.severity == "warning" and report.warning_count > 0:
            sys.exit(1)
        elif args.severity == "all" and (report.error_count > 0 or report.warning_count > 0):
            sys.exit(1)


if __name__ == "__main__":
    main()
