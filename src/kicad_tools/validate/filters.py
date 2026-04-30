"""DRC/ERC violation filtering and reclassification engine.

Provides a rule-based engine that can suppress (ignore) or reclassify
(change severity) DRC and ERC violations based on regex patterns matching
violation type, message, component references, net names, and sheet paths.

Filter rules are defined in ``.kicad-tools.toml`` under ``[[drc.filters]]``
and ``[[erc.filters]]`` array-of-tables sections.

Example TOML configuration::

    [[drc.filters]]
    type_pattern = "silk_overlap|silkscreen_over_pad"
    action = "ignore"
    comment = "Cosmetic silkscreen issues acceptable for prototype"

    [[drc.filters]]
    type_pattern = "courtyard_overlap"
    component_pattern = "^(U1|U2)$"
    action = "warning"
    comment = "Intentional stacking of U1/U2"

    [[erc.filters]]
    type_pattern = "single_global_label"
    action = "ignore"
    comment = "Single global labels are acceptable in this design"
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Union

from kicad_tools.drc.violation import DRCViolation as DRCReportViolation
from kicad_tools.erc.violation import ERCViolation
from kicad_tools.validate.violations import DRCViolation as ValidateViolation

# Union type for all supported violation types
AnyViolation = Union[DRCReportViolation, ERCViolation, ValidateViolation]

# Valid actions for filter rules
VALID_ACTIONS = frozenset({"ignore", "warning", "error"})


class FilterConfigError(Exception):
    """Raised when a filter rule has invalid configuration."""


@dataclass
class ViolationFilter:
    """A single filter rule for DRC/ERC violations.

    All pattern fields are optional; if provided they are compiled as
    regular expressions and matched against the corresponding violation
    attribute.  A violation must match *all* specified patterns for the
    rule to apply (logical AND).

    Attributes:
        type_pattern: Regex matched against the violation type string.
        message_pattern: Regex matched against violation message/description.
        component_pattern: Regex matched against component refs found in items.
        net_pattern: Regex matched against net names.
        sheet_pattern: Regex matched against sheet path (ERC only).
        action: What to do with matching violations: ``"ignore"`` suppresses
            them entirely, ``"warning"`` reclassifies to warning severity,
            ``"error"`` reclassifies to error severity.
        comment: Human-readable explanation of why this filter exists.
    """

    type_pattern: str | None = None
    message_pattern: str | None = None
    component_pattern: str | None = None
    net_pattern: str | None = None
    sheet_pattern: str | None = None
    action: str = "ignore"
    comment: str = ""

    # Compiled regex cache (populated on first use)
    _compiled: dict[str, re.Pattern[str]] = field(
        default_factory=dict, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.action not in VALID_ACTIONS:
            raise FilterConfigError(
                f"Invalid filter action {self.action!r}; "
                f"must be one of {', '.join(sorted(VALID_ACTIONS))}"
            )
        # Pre-compile all patterns to catch regex errors early
        for attr in ("type_pattern", "message_pattern", "component_pattern",
                      "net_pattern", "sheet_pattern"):
            value = getattr(self, attr)
            if value is not None:
                try:
                    self._compiled[attr] = re.compile(value, re.IGNORECASE)
                except re.error as exc:
                    raise FilterConfigError(
                        f"Invalid regex in {attr}: {value!r} -- {exc}"
                    ) from exc

    def _match_pattern(self, attr: str, values: list[str]) -> bool:
        """Check if compiled pattern for *attr* matches any value in *values*.

        Returns ``True`` if the pattern is not set (vacuously true) or if
        at least one value matches the pattern.
        """
        pattern = self._compiled.get(attr)
        if pattern is None:
            return True  # No pattern means "match all"
        return any(pattern.search(v) for v in values)

    def matches(self, violation: AnyViolation) -> bool:
        """Return ``True`` if *violation* matches all specified patterns."""
        # --- type ---
        type_str = _get_type_str(violation)
        if not self._match_pattern("type_pattern", [type_str]):
            return False

        # --- message / description ---
        message = _get_message(violation)
        if not self._match_pattern("message_pattern", [message]):
            return False

        # --- component refs ---
        if self.component_pattern is not None:
            items = _get_items(violation)
            refs = _extract_refs(items)
            if not refs:
                return False
            if not self._match_pattern("component_pattern", list(refs)):
                return False

        # --- nets ---
        if self.net_pattern is not None:
            nets = _get_nets(violation)
            if not nets:
                return False
            if not self._match_pattern("net_pattern", nets):
                return False

        # --- sheet (ERC only) ---
        if self.sheet_pattern is not None:
            sheet = _get_sheet(violation)
            if not sheet:
                return False
            if not self._match_pattern("sheet_pattern", [sheet]):
                return False

        return True


@dataclass
class FilterResult:
    """Result of applying filters to a list of violations.

    Attributes:
        kept: Violations that were not suppressed (may have reclassified
            severity).
        ignored: Violations suppressed by ``action="ignore"`` rules.
        reclassified: Violations whose severity was changed.
        raw_count: Original total violation count before filtering.
    """

    kept: list[Any] = field(default_factory=list)
    ignored: list[Any] = field(default_factory=list)
    reclassified: list[Any] = field(default_factory=list)
    raw_count: int = 0

    @property
    def ignored_count(self) -> int:
        return len(self.ignored)

    @property
    def reclassified_count(self) -> int:
        return len(self.reclassified)

    @property
    def kept_count(self) -> int:
        return len(self.kept)


class FilterEngine:
    """Apply a list of :class:`ViolationFilter` rules to violations.

    The engine processes rules in order; the **first matching rule wins**
    for each violation.  If no rule matches, the violation passes through
    unchanged.
    """

    def __init__(self, filters: list[ViolationFilter] | None = None) -> None:
        self.filters: list[ViolationFilter] = filters or []

    def apply(self, violations: list[AnyViolation]) -> FilterResult:
        """Apply all filter rules to *violations* and return a :class:`FilterResult`."""
        result = FilterResult(raw_count=len(violations))

        for v in violations:
            matched_filter = self._find_matching_filter(v)
            if matched_filter is None:
                # No rule matched -- keep unchanged
                result.kept.append(v)
                continue

            if matched_filter.action == "ignore":
                result.ignored.append(v)
            elif matched_filter.action in ("warning", "error"):
                reclassified = _reclassify(v, matched_filter.action)
                result.kept.append(reclassified)
                result.reclassified.append(reclassified)
            else:
                # Shouldn't happen due to __post_init__ validation, but
                # keep unchanged as a safety net.
                result.kept.append(v)

        return result

    def _find_matching_filter(self, violation: AnyViolation) -> ViolationFilter | None:
        """Return the first filter that matches *violation*, or ``None``."""
        for f in self.filters:
            if f.matches(violation):
                return f
        return None


# ---------------------------------------------------------------------------
# TOML config parsing helpers
# ---------------------------------------------------------------------------

def parse_filters_from_config(config_data: dict[str, Any]) -> tuple[
    list[ViolationFilter], list[ViolationFilter]
]:
    """Parse DRC and ERC filter lists from raw TOML config data.

    Args:
        config_data: Parsed TOML dictionary (top-level).

    Returns:
        A ``(drc_filters, erc_filters)`` tuple of filter lists.

    Raises:
        FilterConfigError: If a filter entry has invalid fields or regex.
    """
    drc_filters = _parse_filter_list(config_data.get("drc", {}), "drc")
    erc_filters = _parse_filter_list(config_data.get("erc", {}), "erc")
    return drc_filters, erc_filters


def _parse_filter_list(section: dict[str, Any], label: str) -> list[ViolationFilter]:
    """Parse ``[[drc.filters]]`` or ``[[erc.filters]]`` entries."""
    raw_list = section.get("filters", [])
    if not isinstance(raw_list, list):
        raise FilterConfigError(
            f"[{label}.filters] must be an array of tables, got {type(raw_list).__name__}"
        )

    filters: list[ViolationFilter] = []
    for i, entry in enumerate(raw_list):
        if not isinstance(entry, dict):
            raise FilterConfigError(
                f"[{label}.filters] entry {i} must be a table, got {type(entry).__name__}"
            )
        try:
            filters.append(ViolationFilter(
                type_pattern=entry.get("type_pattern"),
                message_pattern=entry.get("message_pattern"),
                component_pattern=entry.get("component_pattern"),
                net_pattern=entry.get("net_pattern"),
                sheet_pattern=entry.get("sheet_pattern"),
                action=entry.get("action", "ignore"),
                comment=entry.get("comment", ""),
            ))
        except FilterConfigError as exc:
            raise FilterConfigError(
                f"[{label}.filters] entry {i}: {exc}"
            ) from exc

    return filters


def load_filters_from_toml(path: str) -> tuple[list[ViolationFilter], list[ViolationFilter]]:
    """Load DRC and ERC filters from a TOML file.

    Args:
        path: Path to the TOML file.

    Returns:
        A ``(drc_filters, erc_filters)`` tuple.

    Raises:
        FilterConfigError: On parse or validation errors.
        FileNotFoundError: If the file does not exist.
    """
    import sys
    from pathlib import Path as _Path

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            raise FilterConfigError(
                "tomli package required for Python < 3.11: pip install tomli"
            )

    file_path = _Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Filter config not found: {path}")

    try:
        with open(file_path, "rb") as f:
            data = tomllib.load(f)
    except Exception as exc:
        raise FilterConfigError(f"Error reading {path}: {exc}") from exc

    return parse_filters_from_config(data)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_REF_PATTERN = re.compile(r"\bof\s+([A-Z]+\d+)\b", re.IGNORECASE)


def _extract_refs(items: list[str]) -> list[str]:
    """Extract component reference designators from item strings."""
    refs: list[str] = []
    for item in items:
        for m in _REF_PATTERN.finditer(item):
            ref = m.group(1).upper()
            if ref not in refs:
                refs.append(ref)
    # Also check for bare references like "D1", "C5" in validate violations
    for item in items:
        stripped = item.strip()
        if re.fullmatch(r"[A-Z]+\d+", stripped, re.IGNORECASE):
            ref = stripped.upper()
            if ref not in refs:
                refs.append(ref)
    return refs


def _get_type_str(v: AnyViolation) -> str:
    """Get the type string from any violation type."""
    if isinstance(v, DRCReportViolation):
        return v.type_str
    elif isinstance(v, ERCViolation):
        return v.type_str
    elif isinstance(v, ValidateViolation):
        return v.rule_id
    return ""


def _get_message(v: AnyViolation) -> str:
    """Get the message/description from any violation type."""
    if isinstance(v, DRCReportViolation):
        return v.message
    elif isinstance(v, ERCViolation):
        return v.description
    elif isinstance(v, ValidateViolation):
        return v.message
    return ""


def _get_items(v: AnyViolation) -> list[str]:
    """Get the item list from any violation type."""
    if isinstance(v, DRCReportViolation):
        return list(v.items)
    elif isinstance(v, ERCViolation):
        return list(v.items)
    elif isinstance(v, ValidateViolation):
        return list(v.items)
    return []


def _get_nets(v: AnyViolation) -> list[str]:
    """Get net names from any violation type."""
    if isinstance(v, DRCReportViolation):
        return list(v.nets)
    elif isinstance(v, ValidateViolation):
        return list(v.nets)
    return []


def _get_sheet(v: AnyViolation) -> str:
    """Get sheet path from an ERC violation."""
    if isinstance(v, ERCViolation):
        return v.sheet
    return ""


def _reclassify(v: AnyViolation, new_severity: str) -> AnyViolation:
    """Return a copy of *v* with its severity changed to *new_severity*.

    For frozen dataclasses (ValidateViolation), creates a new instance.
    For mutable dataclasses, mutates in place and returns the same object.
    """
    if isinstance(v, DRCReportViolation):
        from kicad_tools.core.types import Severity
        v.severity = Severity.from_string(new_severity)
        return v
    elif isinstance(v, ERCViolation):
        from kicad_tools.core.types import ERCSeverity
        v.severity = ERCSeverity.from_string(new_severity)
        return v
    elif isinstance(v, ValidateViolation):
        # Frozen dataclass -- need a new instance
        from dataclasses import asdict
        d = asdict(v)
        d["severity"] = new_severity
        # Convert tuples back from lists (asdict converts tuples to lists)
        d["items"] = tuple(d["items"])
        d["nets"] = tuple(d["nets"])
        if d["location"] is not None:
            d["location"] = tuple(d["location"])
        return ValidateViolation(**d)
    return v
