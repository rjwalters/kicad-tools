"""MCP tool for detecting common PCB design mistakes.

Provides the detect_mistakes function for AI agents to identify design
issues with educational explanations and fix suggestions.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def detect_mistakes(
    pcb_path: str,
    category: str | None = None,
    severity: str | None = None,
) -> dict[str, Any]:
    """Detect common PCB design mistakes with educational explanations.

    This tool analyzes a PCB file and identifies common design mistakes that
    experienced designers would catch. Each mistake includes a detailed
    explanation of why it's a problem and how to fix it.

    Args:
        pcb_path: Path to the .kicad_pcb file to analyze
        category: Optional category filter (bypass_capacitor, crystal_oscillator,
                  differential_pair, power_trace, thermal_management, via_placement,
                  manufacturability)
        severity: Optional minimum severity filter (error, warning, info)

    Returns:
        Dictionary with detected mistakes:
        {
            "pcb_file": "board.kicad_pcb",
            "summary": {
                "errors": 2,
                "warnings": 5,
                "info": 3
            },
            "mistakes": [
                {
                    "category": "bypass_capacitor",
                    "severity": "warning",
                    "title": "Bypass capacitor too far from power pin",
                    "components": ["C5", "U1"],
                    "location": [25.4, 12.7],
                    "explanation": "C5 is 8.2mm from U1 pin 3 (VCC)...",
                    "fix_suggestion": "Move C5 to within 3mm of U1 pin 3...",
                    "learn_more_url": "docs/mistakes/bypass-cap-placement.md"
                },
                ...
            ],
            "coverage": [
                {"check_name": "BomFieldHealthCheck", "category": "bom_health",
                 "status": "incomplete", "reason": "..."},
                ...
            ],
            "coverage_complete": false
        }

        ``coverage`` (issue #4899) reports one entry per check that was run,
        with ``status`` either ``"ran"`` or ``"incomplete"``. A check is
        ``"incomplete"`` when it could not reach a verdict because required
        input data was unavailable (e.g. no MPN/LCSC data anywhere on the
        board for ``BomFieldHealthCheck``) -- this must not be read as a
        clean pass for that check (mirrors the #4011 vacuity-guard
        discipline used by ``kct check``'s ``lvs`` sub-check).
        ``coverage_complete`` is ``true`` only when every check ran.

    Raises:
        FileNotFoundError: If the PCB file doesn't exist
        ValueError: If the PCB file is invalid

    Example:
        >>> result = detect_mistakes("board.kicad_pcb")
        >>> print(f"Found {result['summary']['errors']} errors")
        >>> for m in result['mistakes']:
        ...     print(f"[{m['severity']}] {m['title']}")
        >>> if not result["coverage_complete"]:
        ...     print("Warning: some checks could not run")
    """
    from pathlib import Path

    from kicad_tools.explain.mistakes import (
        CheckCoverage,
        MistakeCategory,
        MistakeDetector,
    )
    from kicad_tools.schema.pcb import PCB

    # Load PCB
    path = Path(pcb_path)
    if not path.exists():
        return {
            "error": f"File not found: {pcb_path}",
            "pcb_file": pcb_path,
        }

    if path.suffix != ".kicad_pcb":
        return {
            "error": f"Expected .kicad_pcb file, got {path.suffix}",
            "pcb_file": pcb_path,
        }

    try:
        pcb = PCB.load(str(path))
    except Exception as e:
        return {
            "error": f"Error loading PCB: {e}",
            "pcb_file": pcb_path,
        }

    # Detect mistakes. Always request coverage (issue #4899) so a check
    # that could not run is reported explicitly rather than silently
    # looking like a clean pass.
    detector = MistakeDetector()
    coverage: list[CheckCoverage]
    if category:
        try:
            cat = MistakeCategory(category)
            mistakes, coverage = detector.detect_by_category_with_coverage(pcb, cat)
        except ValueError:
            return {
                "error": f"Invalid category: {category}",
                "pcb_file": pcb_path,
                "valid_categories": [c.value for c in MistakeCategory],
            }
    else:
        mistakes, coverage = detector.detect_with_coverage(pcb)

    # Filter by severity if specified
    if severity:
        severity_order = {"error": 0, "warning": 1, "info": 2}
        if severity not in severity_order:
            return {
                "error": f"Invalid severity: {severity}",
                "pcb_file": pcb_path,
                "valid_severities": ["error", "warning", "info"],
            }
        min_severity = severity_order[severity]
        mistakes = [m for m in mistakes if severity_order.get(m.severity, 99) <= min_severity]

    # Build response
    return {
        "pcb_file": str(path.name),
        "summary": {
            "errors": sum(1 for m in mistakes if m.severity == "error"),
            "warnings": sum(1 for m in mistakes if m.severity == "warning"),
            "info": sum(1 for m in mistakes if m.severity == "info"),
        },
        "mistakes": [m.to_dict() for m in mistakes],
        "coverage": [c.to_dict() for c in coverage],
        "coverage_complete": all(c.status == "ran" for c in coverage),
    }


def list_mistake_categories() -> dict[str, Any]:
    """List all available mistake detection categories.

    Returns information about each category of design mistakes that
    can be detected, along with the number of checks in each category.

    Returns:
        Dictionary with category information:
        {
            "total_categories": 10,
            "total_checks": 9,
            "categories": [
                {
                    "id": "bypass_capacitor",
                    "name": "Bypass Capacitor",
                    "description": "Bypass capacitor placement issues",
                    "check_count": 1
                },
                ...
            ]
        }

    Example:
        >>> cats = list_mistake_categories()
        >>> for cat in cats['categories']:
        ...     print(f"{cat['id']}: {cat['description']}")
    """
    from kicad_tools.explain.mistakes import MistakeCategory, get_default_checks

    checks = get_default_checks()

    # Count checks per category
    by_category: dict[MistakeCategory, int] = {}
    for check in checks:
        cat = check.category
        by_category[cat] = by_category.get(cat, 0) + 1

    # Build category descriptions
    descriptions = {
        MistakeCategory.BYPASS_CAP: "Bypass capacitor placement issues",
        MistakeCategory.CRYSTAL: "Crystal oscillator layout problems",
        MistakeCategory.DIFFERENTIAL_PAIR: "Differential pair routing issues",
        MistakeCategory.POWER_TRACE: "Power trace width problems",
        MistakeCategory.THERMAL: "Thermal management issues",
        MistakeCategory.EMI: "EMI and shielding concerns",
        MistakeCategory.DECOUPLING: "Decoupling capacitor issues",
        MistakeCategory.GROUNDING: "Grounding and return path issues",
        MistakeCategory.VIA: "Via placement problems",
        MistakeCategory.MANUFACTURABILITY: "Manufacturing-related issues",
        MistakeCategory.CONNECTIVITY: "Pull-up / series-resistor connectivity issues",
        MistakeCategory.BOM_HEALTH: "BOM part-number health issues",
    }

    # Format category names
    def format_name(cat: MistakeCategory) -> str:
        return cat.value.replace("_", " ").title()

    categories = [
        {
            "id": cat.value,
            "name": format_name(cat),
            "description": descriptions.get(cat, "General PCB design issues"),
            "check_count": by_category.get(cat, 0),
        }
        for cat in MistakeCategory
    ]

    return {
        "total_categories": len(MistakeCategory),
        "total_checks": len(checks),
        "categories": categories,
    }
