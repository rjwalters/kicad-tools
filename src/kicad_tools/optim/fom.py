"""Hybrid Figure-of-Merit (FOM) for PCB placement / routing evaluation.

Issue #3186.

This module implements the layered FOM:

::

    FOM = Pi pass(constraint_i)            <- hard gate (any fail -> 0)
        x exp(-Sum w_j * soft_term_j)      <- log-linear soft penalty
        x predictor(placement)^beta        <- learned residual (issue #3187)

The hard gate is binary: any failing constraint (DRC, LVS, ERC, mfg-tolerance
allowlist) drops the FOM to 0.  Otherwise the soft gate is a weighted sum
of 10 normalised terms (0 = perfect, larger = worse).

The predictor hook is *exposed but unused* in this issue -- the parameter
exists so issue #3187 can drop in a learned residual model without
changing call sites.

Public API:

* :func:`compute_fom` -- main entry point.  Returns a :class:`FOMResult`.
* :class:`FOMResult` -- dataclass with per-term scores plus the composite.
* :class:`FOMWeights` -- typed configuration loaded from YAML.
* :func:`load_weights_from_yaml` -- read a weights file with light schema validation.

The per-term implementations live in:

* :mod:`kicad_tools.optim.fom_geometry` (length, turning, congestion, crossing, compactness)
* :mod:`kicad_tools.optim.fom_electrical` (vias, match-groups, diff-pair, decoupling)
* :mod:`kicad_tools.optim.fom_thermal` (thermal spread)

Each term function is independently callable and individually unit-tested.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from kicad_tools.optim.fom_electrical import (
    decoupling_proximity,
    diff_pair_clearance_margin,
    match_group_skew,
    weighted_via_count,
)
from kicad_tools.optim.fom_features import BoardFeatures, extract_features
from kicad_tools.optim.fom_geometry import (
    compactness,
    crossing_count,
    net_congestion_variance,
    trace_length_excess,
    turning_penalty,
)
from kicad_tools.optim.fom_thermal import thermal_spread

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB


__all__ = [
    "FOMResult",
    "FOMWeights",
    "compute_fom",
    "compute_soft_terms",
    "check_hard_constraints",
    "load_weights_from_yaml",
    "default_weights",
    "legacy_weights",
    "SOFT_TERM_NAMES",
    "HARD_CONSTRAINT_NAMES",
]


# Order matters: it sets the column order in fom-debug output and the YAML
# keys callers can override.
SOFT_TERM_NAMES = (
    "trace_length_excess",
    "weighted_via_count",
    "turning_penalty",
    "net_congestion_variance",
    "match_group_skew",
    "diff_pair_clearance_margin",
    "decoupling_proximity",
    "crossing_count",
    "thermal_spread",
    "compactness",
)

HARD_CONSTRAINT_NAMES = (
    "drc_clean",
    "lvs_clean",
    "erc_clean",
    "mfg_tolerance_allowlist",
)


@dataclass
class FOMWeights:
    """Per-term weights for the soft FOM gate.

    Defaults to 1.0 per term (the issue specifies uniform weights for
    Phase 1 -- weight calibration is deferred to issue #3188).
    """

    trace_length_excess: float = 1.0
    weighted_via_count: float = 1.0
    turning_penalty: float = 1.0
    net_congestion_variance: float = 1.0
    match_group_skew: float = 1.0
    diff_pair_clearance_margin: float = 1.0
    decoupling_proximity: float = 1.0
    crossing_count: float = 1.0
    thermal_spread: float = 1.0
    compactness: float = 1.0

    def as_dict(self) -> dict[str, float]:
        """Return weights as a flat dict keyed by term name."""
        return {name: getattr(self, name) for name in SOFT_TERM_NAMES}

    @classmethod
    def from_dict(cls, data: dict[str, float]) -> FOMWeights:
        """Build a :class:`FOMWeights` from an arbitrary dict.

        Unknown keys are silently ignored (forward compatibility with
        future term additions); missing keys default to 1.0.
        """
        kwargs: dict[str, float] = {}
        for name in SOFT_TERM_NAMES:
            if name in data and data[name] is not None:
                kwargs[name] = float(data[name])
        return cls(**kwargs)


def default_weights() -> FOMWeights:
    """Return the uniform-1.0 default weights (issue #3186 baseline)."""
    return FOMWeights()


def legacy_weights() -> FOMWeights:
    """Return the "legacy" weights used for backward-compat with #3114.

    The ``--use-routing-fitness`` baseline in PR #3114 only optimised
    routed wirelength; this profile zeroes every term except
    ``trace_length_excess`` so the composite FOM matches that baseline
    within +-5% (acceptance criterion 5 of issue #3186).
    """
    return FOMWeights(
        trace_length_excess=1.0,
        weighted_via_count=0.0,
        turning_penalty=0.0,
        net_congestion_variance=0.0,
        match_group_skew=0.0,
        diff_pair_clearance_margin=0.0,
        decoupling_proximity=0.0,
        crossing_count=0.0,
        thermal_spread=0.0,
        compactness=0.0,
    )


@dataclass
class FOMResult:
    """The decomposed result of a :func:`compute_fom` call.

    The composite :attr:`score` is the integer/float value the caller
    optimises against; the :attr:`soft_terms` and :attr:`hard_failures`
    fields let downstream tooling (fom-debug, GA loggers) say *why*
    a placement scored what it did.

    Attributes:
        score: The composite FOM, in [0, 1].  1 = perfect.
        soft_score: The exp(-sum(w*term)) component (no hard gate, no predictor).
        hard_gate_passed: True if all hard constraints passed.
        hard_failures: Names of failing hard constraints (empty if all passed).
        soft_terms: Per-term scores (raw, before weighting).
        weighted_soft_terms: Per-term contributions to the soft sum (w*term).
        predictor_value: Output of the optional learned predictor (1.0 if none).
        beta: The beta exponent applied to the predictor (0.0 in this issue).
        feature_cache: The extracted :class:`BoardFeatures` (for downstream reuse).
    """

    score: float
    soft_score: float
    hard_gate_passed: bool
    hard_failures: list[str] = field(default_factory=list)
    soft_terms: dict[str, float] = field(default_factory=dict)
    weighted_soft_terms: dict[str, float] = field(default_factory=dict)
    predictor_value: float = 1.0
    beta: float = 0.0
    feature_cache: BoardFeatures | None = None

    def summary(self) -> str:
        """Human-readable one-paragraph summary."""
        lines = []
        lines.append(f"FOM = {self.score:.4f}")
        if not self.hard_gate_passed:
            lines.append(f"  hard gate FAILED: {', '.join(self.hard_failures)}")
        lines.append(f"  soft score = {self.soft_score:.4f}")
        lines.append("  per-term (raw -> weighted):")
        for name in SOFT_TERM_NAMES:
            raw = self.soft_terms.get(name, 0.0)
            w = self.weighted_soft_terms.get(name, 0.0)
            lines.append(f"    {name:<30s} {raw:>10.4f} -> {w:>10.4f}")
        return "\n".join(lines)


# ------------------------------------------------------------------
# Hard-constraint gate
# ------------------------------------------------------------------


def check_hard_constraints(
    pcb: PCB,
    manufacturer: str | None = None,
    drc_report=None,
    erc_report=None,
    lvs_orphan_pads: int | None = None,
    tolerance_allowlist_path: str | Path | None = None,
    pcb_path: str | Path | None = None,
) -> tuple[bool, list[str]]:
    """Evaluate the four hard constraints.

    All arguments except ``pcb`` are optional.  If a check's input isn't
    supplied we skip it -- this lets callers wire up incrementally (e.g.
    only run DRC during a GA inner loop).

    Args:
        pcb: The PCB under evaluation.
        manufacturer: Manufacturer profile name.  When supplied along with a
            DRC report, the count is compared against the tolerance
            allowlist.  When ``None`` (the default), the DRC check just
            requires ``drc_report.error_count == 0``.
        drc_report: A DRC report object exposing ``error_count``.
            ``None`` skips the DRC check.
        erc_report: An ERC report object exposing ``error_count``.
            ``None`` skips the ERC check.
        lvs_orphan_pads: Number of pads on the PCB that don't appear in
            the schematic netlist.  ``None`` skips the LVS check; ``0``
            passes; positive values fail.
        tolerance_allowlist_path: Path to a routed-drc-tolerance YAML
            file.  Used in conjunction with ``pcb_path`` to compute
            the per-board tolerance floor.
        pcb_path: Path to the routed PCB file (the key used in the
            tolerance allowlist).

    Returns:
        (passed, failures): ``passed`` is True iff every applied check
        passed.  ``failures`` lists the names of failing constraints.
    """
    failures: list[str] = []

    # DRC check
    if drc_report is not None:
        try:
            errors = int(drc_report.error_count)
        except (TypeError, AttributeError):
            errors = -1  # treat as inconclusive -> fail conservatively
        tolerance = _tolerance_floor(pcb_path, tolerance_allowlist_path)
        if errors < 0 or errors > tolerance:
            failures.append("drc_clean")

    # ERC check
    if erc_report is not None:
        try:
            errors = int(erc_report.error_count)
        except (TypeError, AttributeError):
            errors = -1
        if errors != 0:
            failures.append("erc_clean")

    # LVS check
    if lvs_orphan_pads is not None:
        if lvs_orphan_pads != 0:
            failures.append("lvs_clean")

    # Mfg-tolerance allowlist
    # The DRC check above already consults the allowlist for its tolerance
    # floor; the standalone "mfg_tolerance_allowlist" check here is a
    # sanity gate that the allowlist file itself parsed cleanly and didn't
    # silently swallow a missing entry.  When tolerance_allowlist_path is
    # supplied but unreadable, fail.
    if tolerance_allowlist_path is not None:
        try:
            _ = _load_tolerance_yaml(tolerance_allowlist_path)
        except Exception:
            failures.append("mfg_tolerance_allowlist")

    return (not failures, failures)


def _tolerance_floor(
    pcb_path: str | Path | None,
    allowlist_path: str | Path | None,
) -> int:
    """Look up the per-board tolerance floor from the allowlist YAML.

    Returns 0 (strict) when either path is missing or the board isn't in
    the allowlist.
    """
    if not pcb_path or not allowlist_path:
        return 0
    try:
        data = _load_tolerance_yaml(allowlist_path)
    except Exception:
        return 0
    tolerances = data.get("tolerances", {}) if isinstance(data, dict) else {}
    pcb_path_str = str(pcb_path)
    # The allowlist keys are repo-root-relative paths; try exact match first
    # then suffix match for convenience.
    if pcb_path_str in tolerances:
        return int(tolerances[pcb_path_str])
    for key, value in tolerances.items():
        if pcb_path_str.endswith(str(key)):
            return int(value)
    return 0


def _load_tolerance_yaml(path: str | Path) -> dict:
    """Load a YAML tolerance allowlist; raises if unreadable."""
    import yaml

    text = Path(path).read_text(encoding="utf-8")
    return yaml.safe_load(text) or {}


# ------------------------------------------------------------------
# Soft-term computation
# ------------------------------------------------------------------


def compute_soft_terms(
    pcb: PCB,
    features: BoardFeatures | None = None,
) -> dict[str, float]:
    """Compute every soft term in the order :data:`SOFT_TERM_NAMES`.

    The returned dict is in canonical order so callers can iterate
    deterministically.  Each value is normalised so that 0 = perfect and
    larger = worse.

    Args:
        pcb: The PCB under evaluation.
        features: Optional pre-extracted features (cache hit).  When
            ``None``, this function calls :func:`extract_features`.

    Returns:
        ``{term_name: score}`` for every term in :data:`SOFT_TERM_NAMES`.
    """
    if features is None:
        features = extract_features(pcb)

    return {
        "trace_length_excess": trace_length_excess(features),
        "weighted_via_count": weighted_via_count(features),
        "turning_penalty": turning_penalty(features),
        "net_congestion_variance": net_congestion_variance(features),
        "match_group_skew": match_group_skew(pcb),
        "diff_pair_clearance_margin": diff_pair_clearance_margin(features, pcb),
        "decoupling_proximity": decoupling_proximity(features),
        "crossing_count": crossing_count(features),
        "thermal_spread": thermal_spread(features, pcb),
        "compactness": compactness(features),
    }


# ------------------------------------------------------------------
# Main entry point
# ------------------------------------------------------------------


def compute_fom(
    pcb: PCB,
    weights: FOMWeights | None = None,
    *,
    manufacturer: str | None = None,
    drc_report=None,
    erc_report=None,
    lvs_orphan_pads: int | None = None,
    tolerance_allowlist_path: str | Path | None = None,
    pcb_path: str | Path | None = None,
    features: BoardFeatures | None = None,
    predictor: Callable[[PCB], float] | None = None,
    beta: float = 0.0,
) -> FOMResult:
    """Compute the hybrid FOM for a placement / routing.

    See the module docstring for the formula.  The hard-constraint gate
    short-circuits to ``score=0`` if any check fails.  Otherwise the
    composite is ``exp(-sum(w_j * term_j)) * predictor(pcb)^beta``.

    Args:
        pcb: The PCB under evaluation.  Must be a parsed
            :class:`~kicad_tools.schema.pcb.PCB`, not a path.
        weights: Per-term weights.  Defaults to :func:`default_weights`
            (uniform 1.0) when ``None``.
        manufacturer: Manufacturer profile (forwarded to the DRC check).
        drc_report: Optional DRC report (forwarded to
            :func:`check_hard_constraints`).
        erc_report: Optional ERC report (forwarded).
        lvs_orphan_pads: Optional LVS orphan-pad count (forwarded).
        tolerance_allowlist_path: Path to routed-drc-tolerance YAML.
        pcb_path: Path to the routed PCB (used to key the allowlist).
        features: Pre-extracted :class:`BoardFeatures`.  Computed if not
            supplied.
        predictor: Optional callable taking a PCB and returning a
            probability in [0, 1].  Provided for issue #3187 (learned
            residual).  Unused in this issue unless ``beta != 0``.
        beta: Exponent on the predictor output.  Default 0.0 (predictor
            multiplies by 1.0 regardless of its value).

    Returns:
        A :class:`FOMResult` with the composite score and per-term
        decomposition.
    """
    weights = weights or default_weights()
    if features is None:
        features = extract_features(pcb)

    # Hard gate
    passed, failures = check_hard_constraints(
        pcb,
        manufacturer=manufacturer,
        drc_report=drc_report,
        erc_report=erc_report,
        lvs_orphan_pads=lvs_orphan_pads,
        tolerance_allowlist_path=tolerance_allowlist_path,
        pcb_path=pcb_path,
    )

    # Soft terms
    soft_terms = compute_soft_terms(pcb, features=features)
    weights_dict = weights.as_dict()
    weighted = {name: weights_dict[name] * soft_terms[name] for name in SOFT_TERM_NAMES}

    # exp(-sum(weighted))  -- careful with overflow.
    s = sum(weighted.values())
    # Cap the exponent so a single pathological term doesn't underflow to 0
    # and lose all signal across the rest of the score.
    capped = min(max(s, 0.0), 60.0)
    soft_score = math.exp(-capped)

    # Predictor (no-op unless beta != 0 and predictor != None)
    predictor_value = 1.0
    if predictor is not None:
        try:
            predictor_value = float(predictor(pcb))
        except Exception:
            predictor_value = 1.0
        # Clamp to [0, 1] to stay in our composite-score domain.
        predictor_value = max(0.0, min(1.0, predictor_value))

    if beta != 0.0:
        predictor_factor = predictor_value**beta if predictor_value > 0 else 0.0
    else:
        predictor_factor = 1.0

    # Hard gate -> 0 if any constraint failed.
    if not passed:
        score = 0.0
    else:
        score = soft_score * predictor_factor

    return FOMResult(
        score=score,
        soft_score=soft_score,
        hard_gate_passed=passed,
        hard_failures=failures,
        soft_terms=soft_terms,
        weighted_soft_terms=weighted,
        predictor_value=predictor_value,
        beta=beta,
        feature_cache=features,
    )


# ------------------------------------------------------------------
# YAML config
# ------------------------------------------------------------------


def load_weights_from_yaml(path: str | Path) -> FOMWeights:
    """Load FOM weights from a YAML file.

    The file is a flat ``term_name: weight`` mapping.  Example:

    .. code-block:: yaml

        trace_length_excess: 2.0
        weighted_via_count: 0.5
        # Unspecified terms default to 1.0.

    A top-level ``weights:`` key is also accepted (so the file can carry
    metadata next to the weights without colliding with term names).

    Unknown keys are silently ignored for forward compatibility.
    """
    import yaml

    text = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    if isinstance(data, dict) and "weights" in data and isinstance(data["weights"], dict):
        data = data["weights"]
    if not isinstance(data, dict):
        return default_weights()
    return FOMWeights.from_dict(data)
