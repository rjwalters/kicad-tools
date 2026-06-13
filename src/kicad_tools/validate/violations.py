"""DRC violation models for pure Python validation.

This module defines the data structures used to represent DRC violations
and results from the pure Python DRC checker.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DRCViolation:
    """Represents a single DRC violation found by the checker.

    This class is hashable (frozen) to support deduplication of violations.

    Attributes:
        rule_id: Unique identifier for the rule, e.g., "clearance_trace_trace"
        severity: One of "error", "warning", or "info".  ``info`` is used
            for advisory findings that are categorized by the rule as
            non-actionable (e.g., a single-pad net that matches the
            KiCad-emitted ``unconnected-(REF-PIN-PadN)`` convention for
            explicit symbol no-connect pins).
        message: Human-readable description of the violation
        location: (x_mm, y_mm) tuple of the violation location, or None
        layer: Layer name where violation occurs, e.g., "F.Cu"
        actual_value: The measured value that violated the rule
        required_value: The minimum/maximum value required by the rule
        items: Tuple of item references involved, e.g., ("D1", "C5")
    """

    rule_id: str
    severity: str
    message: str
    location: tuple[float, float] | None = None
    layer: str | None = None
    actual_value: float | None = None
    required_value: float | None = None
    items: tuple[str, ...] = ()
    nets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate severity value."""
        if self.severity not in ("error", "warning", "info"):
            raise ValueError(
                f"severity must be 'error', 'warning', or 'info', got {self.severity!r}"
            )

    @property
    def is_error(self) -> bool:
        """Check if this is an error (not a warning or info)."""
        return self.severity == "error"

    @property
    def is_warning(self) -> bool:
        """Check if this is a warning (not an error or info)."""
        return self.severity == "warning"

    @property
    def is_info(self) -> bool:
        """Check if this is an informational finding (not an error or warning)."""
        return self.severity == "info"

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization.

        Includes a ``type`` field that mirrors ``rule_id`` so that
        downstream consumers (e.g., ``drc.report._parse_kct_check_json``)
        can resolve the violation to a ``ViolationType`` enum member via
        ``ViolationType.from_string()``.
        """
        from kicad_tools.drc.violation import ViolationType

        resolved_type = ViolationType.from_string(self.rule_id)
        return {
            "rule_id": self.rule_id,
            "type": resolved_type.value,
            "severity": self.severity,
            "message": self.message,
            "location": list(self.location) if self.location else None,
            "layer": self.layer,
            "actual_value": self.actual_value,
            "required_value": self.required_value,
            "items": list(self.items),
            "nets": list(self.nets),
        }


@dataclass
class DRCResults:
    """Aggregates all DRC violations from a check run.

    Provides convenient access to violation counts and filtering.

    Attributes:
        violations: List of all violations found
        rules_checked: Number of rules that were checked (aggregate)
        rules_checked_by_rule: Per-rule check counter mapping
            ``rule_id -> count`` of times the rule actually ran (Issue
            #2660 / Epic #2556 Phase 4N).  Rules that short-circuit
            (e.g., a diff-pair rule on a board with no engaged pairs)
            contribute ``0`` -- and the absence of an entry / a value of
            ``0`` is the CI-side signal that the rule did NOT exercise
            on the board.  Allows the ``diffpair-routing-regression``
            CI gate to assert that the three diff-pair rules
            (``diffpair_clearance_intra``, ``diffpair_length_skew``,
            ``diffpair_routing_continuity``) actually ran against
            board 06 on every PR, catching regressions in detection
            logic (e.g., accidentally flipping ``coupled_routing`` back
            to ``False``) that would otherwise silently report 0
            errors.
        suppressed_count: Number of violations suppressed by filters
    """

    violations: list[DRCViolation] = field(default_factory=list)
    rules_checked: int = 0
    rules_checked_by_rule: dict[str, int] = field(default_factory=dict)
    suppressed_count: int = 0

    @property
    def error_count(self) -> int:
        """Count of violations with severity='error'."""
        return sum(1 for v in self.violations if v.is_error)

    @property
    def warning_count(self) -> int:
        """Count of violations with severity='warning'."""
        return sum(1 for v in self.violations if v.is_warning)

    @property
    def info_count(self) -> int:
        """Count of violations with severity='info'."""
        return sum(1 for v in self.violations if v.is_info)

    @property
    def passed(self) -> bool:
        """True if no errors (warnings and infos are allowed)."""
        return self.error_count == 0

    @property
    def errors(self) -> list[DRCViolation]:
        """List of only error violations."""
        return [v for v in self.violations if v.is_error]

    @property
    def warnings(self) -> list[DRCViolation]:
        """List of only warning violations."""
        return [v for v in self.violations if v.is_warning]

    @property
    def infos(self) -> list[DRCViolation]:
        """List of only informational violations."""
        return [v for v in self.violations if v.is_info]

    def __iter__(self):
        """Iterate over all violations."""
        return iter(self.violations)

    def __len__(self) -> int:
        """Total number of violations."""
        return len(self.violations)

    def __bool__(self) -> bool:
        """True if there are any violations."""
        return len(self.violations) > 0

    def add(self, violation: DRCViolation) -> None:
        """Add a violation to the results."""
        self.violations.append(violation)

    def merge(self, other: DRCResults) -> None:
        """Merge violations from another DRCResults into this one.

        Sums ``rules_checked`` (aggregate counter) and per-rule entries
        in ``rules_checked_by_rule`` so a downstream consumer can ask
        "how many times did rule X run across all categories" after
        ``DRCChecker.check_all()`` has called ``merge()`` on every
        per-rule result.
        """
        self.violations.extend(other.violations)
        self.rules_checked += other.rules_checked
        for rule_id, count in other.rules_checked_by_rule.items():
            self.rules_checked_by_rule[rule_id] = self.rules_checked_by_rule.get(rule_id, 0) + count
        self.suppressed_count += other.suppressed_count

    def filter_by_rule(self, rule_id: str) -> list[DRCViolation]:
        """Get violations for a specific rule."""
        return [v for v in self.violations if v.rule_id == rule_id]

    def filter_by_layer(self, layer: str) -> list[DRCViolation]:
        """Get violations on a specific layer."""
        return [v for v in self.violations if v.layer == layer]

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "passed": self.passed,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "info_count": self.info_count,
            "rules_checked": self.rules_checked,
            "rules_checked_by_rule": dict(self.rules_checked_by_rule),
            "violations": [v.to_dict() for v in self.violations],
        }

    def summary(self) -> str:
        """Generate a human-readable summary."""
        status = "PASSED" if self.passed else "FAILED"
        info_part = f", {self.info_count} infos" if self.info_count else ""
        return (
            f"DRC {status}: {self.error_count} errors, "
            f"{self.warning_count} warnings{info_part} "
            f"({self.rules_checked} rules checked)"
        )
