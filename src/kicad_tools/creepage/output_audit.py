"""Whole-board creepage audit shared by ``optimize-traces`` and ``pcb prune`` (#9640).

Both commands rewrite copper the creepage gate cannot see edit-by-edit, so each
re-runs the ``kct creepage`` census on the candidate board and rejects a rewrite
that makes it worse.  "Worse" is **per-pair shortfall**, not just "a new failing
pair appeared": a deliberately shaped run that already sat short of an envelope
(range) map's requirement is *already* a failing key, so a key-set comparison
lets a rewrite shave another half millimetre off it unnoticed (#9640).
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

#: Shortfall resolution of the verifier's counts (mm).  A pair is reported as
#: ``round(shortfall / SHORTFALL_STEP_MM)`` units (at least 1), so a rewrite is a
#: regression when it moves a failing pair at least one step further under its
#: requirement, or fails a pair that passed.  0.1 um: exactly the census' reported
#: margin precision (4 decimals), so a deeper shortfall -- e.g. -0.799 mm ->
#: -0.800 mm -- is never rounded away (#9640 review).
SHORTFALL_STEP_MM = 0.0001

_CACHE_LIMIT = 16


def pair_key(pair: Mapping[str, Any]) -> str:
    """Stable ``net_a|net_b|layer`` key of a creepage census row."""
    return f"{pair['net_a']}|{pair['net_b']}|{pair.get('layer')}"


class CreepageAudit:
    """Callable ``board_text -> {pair key: shortfall units}`` for un-waived failures.

    Runs the same census as ``kct creepage`` (same voltage map, standard,
    pollution degree, material group and HV threshold; same-footprint and
    attach-zone waivers on).  Returns ``None`` when the audit cannot run -- the
    callers treat that as "unverified", never as a pass.  Censuses are cached by
    board text so the baseline and final reports reuse the verifier's own runs.
    """

    def __init__(
        self,
        *,
        voltage_map: str | Path,
        standard: str = "iec60664",
        pollution_degree: int = 2,
        material_group: str = "IIIa",
        hv_threshold: float = 30.0,
    ) -> None:
        self.voltage_map = str(voltage_map)
        self.standard = standard
        self.pollution_degree = pollution_degree
        self.material_group = material_group
        self.hv_threshold = hv_threshold
        self._cache: dict[str, list[dict[str, Any]] | None] = {}

    def required_pairs(self) -> dict[tuple[str, str], float] | None:
        """Voltage-map pairs the census is expected to score (``{(a, b): mm}``).

        Derived here from the map file with the census' own semantics --
        interval-aware worst case ``max(|a.hi - b.lo|, |b.hi - a.lo|)``, then
        ``_pair_bounds`` caps, then the HV threshold -- rather than from a
        caller's scalar-collapsed routing table.  ``None`` when the map cannot
        be read (the audit itself then reports "unverified").
        """
        from kicad_tools.placement.hv_domains import build_required_by_domain_pair
        from kicad_tools.router.pairwise_clearance import (
            _norm_net_key,
            load_pair_bounds,
            load_signed_voltage_map,
            load_voltage_intervals,
        )

        try:
            path = Path(self.voltage_map)
            voltages = {
                _norm_net_key(n): float(v) for n, v in load_signed_voltage_map(path).items()
            }
            intervals = {
                _norm_net_key(n): (float(lo), float(hi))
                for n, (lo, hi) in load_voltage_intervals(path).items()
            }
            return build_required_by_domain_pair(
                voltages,
                standard_id=self.standard,
                pollution_degree=self.pollution_degree,
                material_group=self.material_group,
                hv_threshold=self.hv_threshold,
                pair_bounds=load_pair_bounds(path) or None,
                domain_intervals=intervals,
            )
        except Exception:
            return None

    def census(self, board_text: str) -> list[dict[str, Any]] | None:
        """Every audited pair row (JSON dicts) of ``board_text``; ``None`` if it cannot run."""
        digest = hashlib.sha256(board_text.encode()).hexdigest()
        if digest in self._cache:
            return self._cache[digest]
        pairs = self._run(board_text)
        if len(self._cache) >= _CACHE_LIMIT:
            self._cache.pop(next(iter(self._cache)))
        self._cache[digest] = pairs
        return pairs

    def _run(self, board_text: str) -> list[dict[str, Any]] | None:
        from kicad_tools.cli.commands.creepage import run_creepage_command

        try:
            with tempfile.TemporaryDirectory(prefix="kct_creepaudit_") as td:
                board = Path(td) / "creep.kicad_pcb"
                board.write_text(board_text)
                ns = SimpleNamespace(
                    pcb=str(board),
                    format="json",
                    min=None,
                    standard=self.standard,
                    working_voltage=None,
                    pollution_degree=self.pollution_degree,
                    material_group=self.material_group,
                    voltage_map=self.voltage_map,
                    census_threshold=self.hv_threshold,
                    waive_same_footprint=True,
                    waive_attach_zone=True,
                    witness=False,
                    net_class="HV",
                    net_class_map=None,
                )
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    run_creepage_command(ns)
                data = json.loads(out.getvalue())
        except Exception:
            return None
        pairs = data.get("pairs")
        return list(pairs) if pairs is not None else None

    def __call__(self, board_text: str) -> dict[str, int] | None:
        pairs = self.census(board_text)
        if pairs is None:
            return None
        return failing_units(pairs)


def failing_units(pairs: list[dict[str, Any]]) -> dict[str, int]:
    """``{pair key: shortfall units}`` for the un-waived failing rows of a census."""
    out: dict[str, int] = {}
    for p in pairs:
        if p.get("pass", True) or p.get("waived"):
            continue
        shortfall = max(0.0, -float(p.get("margin_mm", 0.0)))
        out[pair_key(p)] = max(1, round(shortfall / SHORTFALL_STEP_MM))
    return out


def worse_pairs(audit: CreepageAudit, before_text: str, after_text: str) -> dict[str, int]:
    """Pairs ``after_text`` leaves further under requirement than ``before_text``.

    ``{pair key: shortfall units}``: a pair failing in ``after_text`` whose
    shortfall exceeds its shortfall in ``before_text`` (0 when it passed).  If
    either census cannot run the result is ``{"unverifiable": 1}`` -- never a
    pass.
    """
    base = audit(before_text)
    cur = audit(after_text)
    if base is None or cur is None:
        return {"unverifiable": 1}
    return {k: n for k, n in cur.items() if n > base.get(k, 0)}


def audit_summary(
    audit: CreepageAudit,
    before_text: str,
    after_text: str,
    required_pairs: Mapping[Any, Any] | None = None,
) -> dict[str, Any]:
    """The JSON report block shared by both commands (#9640).

    ``creepage_pairs_checked`` lists every pair the census scored with its
    margin before and after.  ``creepage_pairs_unchecked`` lists the pairs the
    voltage map requires (``required_pairs``: frozenset-of-two-net-keys ->
    requirement) that the census did not score at all -- nets with no copper
    on the board -- i.e. pairs this run could not check.  Each census reuses
    the audit's cache, so this adds at most one new audit run (the final text).
    """
    from kicad_tools.router.pairwise_clearance import _norm_net_key

    before = audit.census(before_text)
    after = audit.census(after_text)
    if before is None or after is None:
        return {
            "creepage_pairs_checked": None,
            "creepage_pairs_unchecked": None,
            "creepage_unwaived_failures_before": None,
            "creepage_unwaived_failures_after": None,
        }
    before_margin = {pair_key(p): p.get("margin_mm") for p in before}
    checked = [
        {
            "net_a": p["net_a"],
            "net_b": p["net_b"],
            "layer": p.get("layer"),
            "margin_mm_before": before_margin.get(pair_key(p)),
            "margin_mm_after": p.get("margin_mm"),
            "waived": bool(p.get("waived")),
        }
        for p in after
    ]
    scored = {
        frozenset((_norm_net_key(str(p["net_a"])), _norm_net_key(str(p["net_b"])))) for p in after
    }
    unchecked = sorted(sorted(k) for k in (required_pairs or {}) if frozenset(k) not in scored)
    return {
        "creepage_pairs_checked": checked,
        "creepage_pairs_unchecked": [{"net_a": a, "net_b": b} for a, b in unchecked],
        "creepage_unwaived_failures_before": len(failing_units(before)),
        "creepage_unwaived_failures_after": len(failing_units(after)),
    }
