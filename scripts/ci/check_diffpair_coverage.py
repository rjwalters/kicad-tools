#!/usr/bin/env python3
"""Diff-pair "rule exercised" CI gate (Issue #2660, Epic #2556 Phase 4N).

This script is the second pillar of the diff-pair CI strategy.  Its sibling
``check_routed_drc.py`` (Issue #2546) catches regressions in *committed*
``*_routed.kicad_pcb`` files; this script catches regressions in the
**routing algorithm itself** by re-routing a board from scratch and
asserting:

  1. The post-route DRC error count is within the per-board allowlist
     (same semantic as ``check_routed_drc.py``).
  2. Each of the three diff-pair DRC rules was actually exercised by the
     check (i.e., the per-rule counter is > 0).  Without this assertion
     a regression that disables diff-pair detection (e.g., flipping
     ``coupled_routing`` back to ``False``) would silently produce a
     0-error report and hide the defect.
  3. (Issue #3413 phase 5) Signal-net reach meets the board's
     ``REQUIRED_SIGNAL_REACH`` contract.
  4. (Issue #3509) For boards declaring ``REQUIRE_POUR_CONNECTIVITY``,
     the recipe's copper-union pour audit passes on the re-routed
     artifact: every pour net is one copper component and no
     fill-enabled zone has zero filled polygons.  Previously the recipe
     printed "POUR CONNECTIVITY: FAIL" in the job log while the gate
     stayed green.

The three rule_ids this script asserts coverage of are:

  * ``diffpair_clearance_intra`` (#2560, Epic #2556 Phase 1D)
  * ``diffpair_routing_continuity`` (#2640, Epic #2556 Phase 2G)
  * ``diffpair_length_skew`` (#2649, Epic #2556 Phase 3J)

The script is parametric over the board directory so additional boards
(e.g., board 03 once #2589's determinism work + USB-C blockers #2513
close) can be added cheaply.

Usage::

    # Re-route + check board 06 (the canonical diff-pair testbench):
    python scripts/ci/check_diffpair_coverage.py boards/06-diffpair-test

    # Check an already-routed board (skip the route step; useful for
    # debugging the assertion logic locally):
    python scripts/ci/check_diffpair_coverage.py boards/06-diffpair-test \
        --skip-route

Exit codes (mirror ``check_routed_drc.py``):

    0 -- Gate passed (errors within tolerance AND all 3 rules exercised).
    1 -- Tool failure (board dir missing, route step crashed, allowlist
         parse error, etc.).
    2 -- Gate failed: errors exceed allowlist OR at least one of the 3
         diff-pair rules did not run.

GitHub-Actions ``::error file=...::`` annotations are emitted on failure
so the PR Files-changed view surfaces the failure inline.

Reusing logic from ``check_routed_drc.py``:

    * Allowlist loading (``load_allowlist``)
    * Allowlist comparison (``check_file``)
    * Annotation helpers (``annotate_error``)

Why a separate script (not folded into ``check_routed_drc.py``):

    The "rule exercised" assertion is a sibling concern from the
    per-board error allowlist; it needs router-level context (a
    ``net_class_map`` from the board's ``generate_design.build_net_class_map``)
    that the committed-PCB gate intentionally does not require.
    Keeping the two concerns in separate scripts preserves each gate's
    minimal blast radius.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

# --- shared with check_routed_drc.py ------------------------------------------

DEFAULT_ALLOWLIST = Path(".github/routed-drc-tolerance.yml")

# The three diff-pair rule_ids that MUST be exercised on a board that
# declares engaged diff pairs (board 06 declares 9 pairs across 4
# protocols, so all three rules should run with rules_checked_by_rule
# entries >= 1).  See ``src/kicad_tools/drc/violation.py`` and
# ``src/kicad_tools/validate/rules/diffpair_*.py`` for the canonical
# rule_id strings + per-rule counter increments (Issue #2660).
DIFFPAIR_RULE_IDS: tuple[str, ...] = (
    "diffpair_clearance_intra",
    "diffpair_length_skew",
    "diffpair_routing_continuity",
)


def load_allowlist(allowlist_path: Path) -> dict[str, int]:
    """Load the per-board DRC tolerance allowlist.

    Behaviour matches ``check_routed_drc.load_allowlist`` exactly so the
    two gates share the same YAML contract; duplicated here to keep this
    script self-contained (the CI environment runs both as separate
    ``python`` processes).
    """
    if not allowlist_path.exists():
        return {}
    try:
        data = yaml.safe_load(allowlist_path.read_text())
    except yaml.YAMLError as e:
        raise ValueError(f"Malformed allowlist YAML at {allowlist_path}: {e}") from e

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(
            f"Allowlist {allowlist_path} must be a YAML mapping at the top level, "
            f"got {type(data).__name__}"
        )

    tolerances = data.get("tolerances", {})
    if not isinstance(tolerances, dict):
        raise ValueError(
            f"Allowlist {allowlist_path} 'tolerances' field must be a mapping, "
            f"got {type(tolerances).__name__}"
        )

    result: dict[str, int] = {}
    for key, value in tolerances.items():
        if not isinstance(key, str):
            raise ValueError(f"Allowlist {allowlist_path}: key {key!r} must be a string")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(
                f"Allowlist {allowlist_path}: value for {key!r} must be a "
                f"non-negative integer, got {value!r}"
            )
        result[key] = value
    return result


def annotate_error(file: str, message: str) -> None:
    """Emit a GitHub-Actions ``::error file=...::`` annotation."""
    print(f"::error file={file}::{message}", flush=True)


# --- script-specific logic ----------------------------------------------------


def re_route_board(board_dir: Path, seed: int) -> bool:
    """Re-route the board's PCB from scratch using ``generate_design.py``.

    Args:
        board_dir: Path to the board directory (e.g.,
            ``boards/06-diffpair-test``).
        seed: Seed for ``random.seed()`` (Issue #2589).  Required for
            determinism so the gate produces the same artifact run-to-run.

    Returns:
        True on success (return code 0), False otherwise.

    Notes:
        Runs ``python generate_design.py --step route --seed N`` via
        ``subprocess.run`` so the route logic runs in a fresh process
        (no inherited state).  Stdout/stderr are streamed to the
        CI log for diagnostic visibility.
    """
    script = board_dir / "generate_design.py"
    if not script.is_file():
        print(
            f"::error::generate_design.py not found at {script} -- cannot re-route board.",
            flush=True,
        )
        return False

    cmd = [
        sys.executable,
        str(script),
        "--step",
        "route",
        "--seed",
        str(seed),
    ]
    print(f"\n[re-route] Running: {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, capture_output=False, text=True, check=False)
    if proc.returncode != 0:
        print(
            f"::error::Re-route failed for {board_dir} (exit code {proc.returncode}).",
            flush=True,
        )
        return False
    return True


def find_routed_pcb(board_dir: Path) -> Path | None:
    """Locate the board's freshly-routed PCB.

    Walks ``board_dir/output`` looking for the canonical
    ``*_routed.kicad_pcb`` artifact emitted by ``generate_design.py``.
    Returns ``None`` if not found (caller emits the error).
    """
    out = board_dir / "output"
    if not out.is_dir():
        return None
    candidates = list(out.glob("*_routed.kicad_pcb"))
    if not candidates:
        return None
    if len(candidates) > 1:
        print(
            f"::warning::Multiple routed PCBs found in {out}; using first: {candidates[0]}",
            flush=True,
        )
    return candidates[0]


# ``build_net_class_map_for_board`` was promoted to the shared
# ``net_class_map_resolver`` module (Issue #3151) so both the diff-pair
# coverage gate and the strict ``check_routed_drc`` error-count gate derive
# the map the same way.  Re-export it here under the original name so this
# script's public surface (and its tests) are unchanged.  ``scripts/ci`` is
# on ``sys.path`` because this file lives in it.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from net_class_map_resolver import (  # noqa: E402
    build_net_class_map_for_board,
    load_board_recipe_module,
)


def measure_signal_reach(pcb_path: Path, pour_nets: set[str]) -> tuple[int, int, list[str]]:
    """Measure routed-signal-net reach on a routed PCB.

    Issue #3413 phase 5: the gate previously asserted only the DRC error
    count + rule-exercise, so a reach regression (e.g. 18/21 with 3
    connectivity errors) stayed green as long as the error count fit
    under the allowlist floor.  This helper counts COMPLETE signal nets
    per :class:`~kicad_tools.analysis.net_status.NetStatusAnalyzer` --
    the same per-net completeness model the connectivity DRC rule uses.

    Pour nets are excluded: their connectivity is plane/stitching-based
    and is gated separately (the board recipe's copper-union audit;
    see boards/06-diffpair-test/generate_design.py step 11).

    Args:
        pcb_path: Routed PCB to measure.
        pour_nets: Net names excluded from the signal universe (the
            board module's ``POUR_NETS``).

    Returns:
        ``(complete_count, signal_total, incomplete_names)``.
    """
    from kicad_tools.analysis.net_status import NetStatusAnalyzer

    result = NetStatusAnalyzer(pcb_path).analyze()
    signal = [n for n in result.nets if n.net_name and n.net_name not in pour_nets]
    complete = [n for n in signal if n.status == "complete"]
    incomplete_names = sorted(n.net_name for n in signal if n.status != "complete")
    return len(complete), len(signal), incomplete_names


def measure_pour_connectivity(recipe_mod, pcb_path: Path, pour_nets: set[str]) -> list[str]:
    """Run the board recipe's copper-union pour audit on a routed PCB.

    Issue #3509: the board 06 recipe runs a shapely copper-union audit
    (``_audit_pour_nets``) at the end of its pour pipeline and prints
    PASS/FAIL -- but until this helper the gate never consumed that
    verdict, so a re-route whose pours failed the audit still went green
    (PR #3506 run 27343006197: "POUR CONNECTIVITY: FAIL" in the log,
    job passed).  Boards opt in by declaring
    ``REQUIRE_POUR_CONNECTIVITY = True`` next to their ``POUR_NETS``
    contract; the gate then re-runs the audit against the routed
    artifact and fails the job on any disjoint pour net or zero-fill
    zone.

    Args:
        recipe_mod: The board's imported ``generate_design`` module (must
            expose ``_audit_pour_nets(pcb_path, net_names)``).
        pcb_path: Routed PCB to audit.
        pour_nets: The board's ``POUR_NETS`` set.

    Returns:
        List of human-readable failure strings; empty means every pour
        net is one copper component with real fill geometry.

    Raises:
        RuntimeError: When the audit cannot run at all (missing
            ``_audit_pour_nets``, shapely unavailable, audit crash).
            Callers must treat this as a tool failure (exit 1), NOT a
            pass -- a silent skip is how dead pours shipped in the
            first place (PR #3481).
    """
    audit_fn = getattr(recipe_mod, "_audit_pour_nets", None)
    if audit_fn is None:
        raise RuntimeError(
            "Board declares REQUIRE_POUR_CONNECTIVITY but its "
            "generate_design module does not expose _audit_pour_nets()."
        )
    try:
        audit = audit_fn(pcb_path, sorted(pour_nets))
    except Exception as e:
        raise RuntimeError(f"Pour-connectivity audit crashed: {e}") from e

    failures: list[str] = []
    for net in sorted(pour_nets):
        info = audit.get(net)
        if info is None:
            failures.append(f"{net}: missing from audit result")
            continue
        n_pads = sum(len(g) for g in info["pad_groups"])
        if not info["connected"]:
            largest = len(info["pad_groups"][0]) if info["pad_groups"] else 0
            failures.append(
                f"{net}: {len(info['pad_groups'])} disjoint pad groups (largest {largest}/{n_pads})"
            )
        if info.get("zero_fill_zones"):
            failures.append(
                f"{net}: {info['zero_fill_zones']} fill-enabled zone(s) "
                f"with ZERO filled polygons (dead pour)"
            )
    return failures


def _measure_skew_data_from_pcb(
    pcb, engaged_pairs: set[tuple[int, int]]
) -> dict[tuple[str, str], float]:
    """Compute per-pair length skew from segments on a routed PCB.

    The :class:`~kicad_tools.validate.rules.diffpair_length_skew.DiffPairLengthSkewRule`
    requires both ``engaged_pairs`` and a ``skew_data`` map populated
    by the autorouter side.  When running standalone DRC on a routed
    PCB from disk, no router context is available -- but the routed
    segments themselves carry enough information to compute the skew.

    For each engaged pair, sum the geometric segment lengths per net
    (across all copper layers), then take the absolute difference.
    Via drilled length is ignored (we'd need ``board_thickness_mm`` to
    convert layer-deltas into Z-length, which is not on the PCB at
    DRC time).  This matches the producer-side behaviour when
    ``board_thickness_mm=None`` -- see
    :meth:`DiffPairLengthTracker._measure_route`.

    Args:
        pcb: Loaded :class:`~kicad_tools.schema.pcb.PCB`.
        engaged_pairs: Set of ``(min_net_id, max_net_id)`` tuples (the
            engagement-state output from ``derive_engagement_state``).

    Returns:
        Mapping of ``(net_name_a, net_name_b)`` -> ``skew_mm``.  The
        rule consumes this directly; keys are normalised to a sorted
        name-tuple internally by the rule's constructor.
    """
    import math

    if not engaged_pairs:
        return {}

    # Build net_id -> net_name map for the engaged pairs.
    net_id_to_name: dict[int, str] = {}
    for net in pcb.nets.values():
        name = getattr(net, "name", None)
        if name:
            net_id_to_name[net.number] = name

    # Sum segment lengths per net id; only for nets that appear in
    # engaged_pairs (cheap filter -- avoids scanning every segment).
    engaged_net_ids: set[int] = set()
    for a, b in engaged_pairs:
        engaged_net_ids.add(a)
        engaged_net_ids.add(b)

    lengths_by_net: dict[int, float] = dict.fromkeys(engaged_net_ids, 0.0)
    for layer in pcb.copper_layers:
        for seg in pcb.segments_on_layer(layer.name):
            if seg.net_number in lengths_by_net:
                x1, y1 = seg.start
                x2, y2 = seg.end
                lengths_by_net[seg.net_number] += math.hypot(x2 - x1, y2 - y1)

    # Build the {(p_name, n_name): skew_mm} dict for the rule.
    skew_data: dict[tuple[str, str], float] = {}
    for a, b in engaged_pairs:
        l_a = lengths_by_net.get(a, 0.0)
        l_b = lengths_by_net.get(b, 0.0)
        # Skip pairs where neither half is routed (both 0.0); these
        # cannot produce a meaningful skew measurement and the rule's
        # graceful-degradation contract says "unrouted -> omit".
        if l_a <= 0.0 and l_b <= 0.0:
            continue
        name_a = net_id_to_name.get(a)
        name_b = net_id_to_name.get(b)
        if not name_a or not name_b:
            continue
        skew_data[(name_a, name_b)] = abs(l_a - l_b)
    return skew_data


def count_errors_via_kct_check(pcb_path: Path) -> int:
    """Count errors via ``kct check --mfr jlcpcb --errors-only --format json``.

    Mirrors ``check_routed_drc.count_errors`` so the allowlist semantic
    matches the sibling diff-driven CI gate exactly -- a value of 28
    on the same routed PCB must mean the same thing across both gates.

    Uses a subprocess invocation (not in-process) because:

    1. The sibling gate's behaviour is what reviewers calibrate the
       allowlist against; in-process variants risk silent drift.
    2. The same ``kct check`` exit-code (0/2) + JSON envelope this
       script depends on is what users see when they reproduce the
       check locally.
    """
    cmd = [
        "uv",
        "run",
        "kct",
        "check",
        str(pcb_path),
        "--mfr",
        "jlcpcb",
        "--errors-only",
        "--format",
        "json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode == 1:
        raise RuntimeError(
            f"kct check failed on {pcb_path} (exit 1). stderr:\n{proc.stderr.strip()}"
        )
    if proc.returncode not in (0, 2):
        raise RuntimeError(
            f"kct check returned unexpected exit code {proc.returncode} on "
            f"{pcb_path}. stderr:\n{proc.stderr.strip()}"
        )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"kct check produced invalid JSON on {pcb_path}: {e}") from e

    summary = data.get("summary", {})
    errors = summary.get("errors")
    if not isinstance(errors, int):
        raise RuntimeError(f"kct check JSON missing summary.errors field for {pcb_path}: {data!r}")
    return errors


def compute_rule_coverage(
    pcb_path: Path,
    net_class_map: dict | None,
) -> dict[str, int]:
    """Compute per-rule check counts for the diff-pair rules.

    Invokes :class:`kicad_tools.validate.DRCChecker` directly so the
    per-rule counter is accessible without re-parsing JSON.  The
    ``net_class_map`` is threaded through to the checker so the
    diff-pair routing-continuity rule can derive its engaged-pairs set
    (the same way the autorouter does).  For the length-skew rule,
    ``skew_data`` is measured from the routed PCB itself (no router
    context required) so AC-#4 can be satisfied without spinning up
    the autorouter twice.

    This is a SEPARATE pass from the error-count check (which uses
    the standalone ``kct check`` for allowlist parity).  The two passes
    serve different purposes: error-count gates regression in the
    committed-or-re-routed PCB, while coverage gates regression in
    the detection logic.

    Args:
        pcb_path: Path to the routed PCB.
        net_class_map: Optional ``{net_name: NetClassRouting}`` map from
            the board's ``build_net_class_map()``.  Required for the
            diff-pair rules to engage.

    Returns:
        ``rules_checked_by_rule`` dict mapping rule_id -> count.

    Raises:
        RuntimeError: If the PCB cannot be loaded.
    """
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate import DRCChecker
    from kicad_tools.validate.diffpair_engagement import derive_engagement_state
    from kicad_tools.validate.rules.diffpair_length_skew import DiffPairLengthSkewRule

    try:
        pcb = PCB.load(pcb_path)
    except Exception as e:
        raise RuntimeError(f"Failed to load PCB {pcb_path}: {e}") from e

    detected = len(pcb.copper_layers)
    layers = detected if detected > 0 else 2

    checker = DRCChecker(
        pcb,
        manufacturer="jlcpcb",
        layers=layers,
        copper_oz=1.0,
        net_class_map=net_class_map,
    )

    # Run all rules via the standard checker entry point.
    results = checker.check_all()

    # Phase 4N (#2660) AC #4: also exercise the diffpair_length_skew
    # rule with skew_data MEASURED FROM THE ROUTED PCB.  The standalone
    # checker.check_diffpair_length_skew() constructs the rule with empty
    # skew_data (no router context), so the rule never increments its
    # per-rule counter.  Compute skew_data here from the segment lengths
    # and re-run the rule so the per-rule counter records the
    # engagement -- WITHOUT requiring the autorouter to be re-spun.
    engaged_pairs, threshold_map = derive_engagement_state(pcb, net_class_map)
    if engaged_pairs:
        skew_data = _measure_skew_data_from_pcb(pcb, engaged_pairs)
        if skew_data:
            skew_rule = DiffPairLengthSkewRule(
                skew_data=skew_data,
                engaged_pairs=engaged_pairs,
                threshold_map=threshold_map,
            )
            skew_results = skew_rule.check(pcb, checker.design_rules)
            results.merge(skew_results)

    return dict(results.rules_checked_by_rule)


def check_rule_coverage(
    rules_checked_by_rule: dict[str, int],
    required_rule_ids: tuple[str, ...] = DIFFPAIR_RULE_IDS,
) -> list[str]:
    """Return the list of rule_ids that did NOT run (counter < 1 or missing).

    A returned list is empty iff every required rule was exercised at
    least once.  The ordering matches ``required_rule_ids`` so error
    messages list rules in a stable order.
    """
    missing: list[str] = []
    for rule_id in required_rule_ids:
        if rules_checked_by_rule.get(rule_id, 0) < 1:
            missing.append(rule_id)
    return missing


def check_board(
    board_dir: Path,
    allowlist: dict[str, int],
    seed: int,
    skip_route: bool,
) -> int:
    """Run the full gate for a single board.

    Returns the exit code (0 = pass, 1 = tool failure, 2 = gate failure).
    Mirrors the exit-code convention from ``check_routed_drc.py``.
    """
    if not board_dir.is_dir():
        annotate_error(str(board_dir), f"Board directory not found: {board_dir}")
        return 1

    if not skip_route:
        if not re_route_board(board_dir, seed):
            return 1
    else:
        print(f"\n[skip-route] Using existing routed PCB in {board_dir}/output/")

    routed_pcb = find_routed_pcb(board_dir)
    if routed_pcb is None:
        annotate_error(
            str(board_dir),
            "No *_routed.kicad_pcb found in board output dir after re-route.",
        )
        return 1

    # Build the net_class_map from the board's generate_design module
    # so diff-pair detection in the DRC rules has the per-protocol
    # net-class context (coupled_routing flags, skew_tolerance_mm, etc.).
    try:
        net_class_map = build_net_class_map_for_board(board_dir)
    except Exception as e:
        annotate_error(
            str(board_dir),
            f"Failed to import build_net_class_map from {board_dir}: {e}",
        )
        return 1

    # Issue #3413 phase 5: read the board's reach contract.  Boards that
    # declare ``REQUIRED_SIGNAL_REACH`` get a hard reach assertion;
    # boards without it (none today in this gate's matrix) skip it.
    required_reach: int | None = None
    pour_nets: set[str] = set()
    require_pour_connectivity = False
    recipe_mod = None
    try:
        recipe_mod = load_board_recipe_module(board_dir)
        if recipe_mod is not None:
            required_reach = getattr(recipe_mod, "REQUIRED_SIGNAL_REACH", None)
            pour_nets = set(getattr(recipe_mod, "POUR_NETS", ()) or ())
            require_pour_connectivity = bool(
                getattr(recipe_mod, "REQUIRE_POUR_CONNECTIVITY", False)
            )
    except Exception as e:
        annotate_error(
            str(board_dir),
            f"Failed to read reach contract from {board_dir}: {e}",
        )
        return 1

    # Compute the allowlist key in the same way check_routed_drc.py does:
    # repo-relative path string.
    try:
        rel = routed_pcb.resolve().relative_to(Path.cwd())
        lookup_key = str(rel)
    except ValueError:
        lookup_key = str(routed_pcb)
    allowed = allowlist.get(lookup_key, 0)

    # Two-pass strategy (see docstrings on count_errors_via_kct_check
    # and compute_rule_coverage for the rationale):
    #   1. Error count via subprocess ``kct check`` -- matches the
    #      sibling allowlist semantic exactly.
    #   2. Rule coverage via in-process DRCChecker(..., net_class_map=...)
    #      -- the only way to exercise the diff-pair rules whose
    #      engagement is gated on router context.
    try:
        error_count = count_errors_via_kct_check(routed_pcb)
    except RuntimeError as e:
        annotate_error(str(routed_pcb), f"kct check failed: {e}")
        return 1
    try:
        rules_by_rule = compute_rule_coverage(routed_pcb, net_class_map)
    except RuntimeError as e:
        annotate_error(str(routed_pcb), f"Rule-coverage probe failed: {e}")
        return 1

    print(f"\n[diffpair-coverage] Board: {board_dir.name}")
    print(f"[diffpair-coverage] Routed PCB: {routed_pcb}")
    print(f"[diffpair-coverage] DRC error count: {error_count} (allowed: {allowed})")
    print(f"[diffpair-coverage] rules_checked_by_rule: {rules_by_rule}")

    failed = False

    # Issue #3413 phase 5: reach assertion.  Without it the gate is
    # green at ANY reach as long as the connectivity errors fit under
    # the allowlist floor (the 18/21 regression shape from the #3413
    # re-measure).  Reach is measured with the same NetStatusAnalyzer
    # completeness model the connectivity DRC rule uses.
    if required_reach is not None:
        try:
            complete, signal_total, incomplete_names = measure_signal_reach(routed_pcb, pour_nets)
        except Exception as e:
            annotate_error(str(routed_pcb), f"Reach measurement failed: {e}")
            return 1
        print(
            f"[diffpair-coverage] Signal reach: {complete}/{signal_total} "
            f"(required: {required_reach})"
        )
        if complete < required_reach:
            annotate_error(
                str(routed_pcb),
                f"Reach regression on re-routed {routed_pcb}: only "
                f"{complete}/{signal_total} signal nets complete "
                f"(required {required_reach}).  Incomplete: "
                f"{', '.join(incomplete_names)}.  See "
                f"{board_dir.name}/generate_design.py REQUIRED_SIGNAL_REACH.",
            )
            failed = True
        else:
            print(f"[diffpair-coverage] OK: reach {complete}/{signal_total} >= {required_reach}.")

    # Issue #3509: pour-connectivity assertion.  The board recipe's
    # copper-union audit verdict was previously informational only --
    # the recipe printed "POUR CONNECTIVITY: FAIL" while this gate stayed
    # green.  Boards opting in via REQUIRE_POUR_CONNECTIVITY get the
    # audit re-run here against the routed artifact; failures gate.
    if require_pour_connectivity:
        try:
            pour_failures = measure_pour_connectivity(recipe_mod, routed_pcb, pour_nets)
        except RuntimeError as e:
            annotate_error(
                str(routed_pcb),
                f"Pour-connectivity audit could not run: {e}  (A skipped "
                "audit must be a tool failure, never a silent pass.)",
            )
            return 1
        if pour_failures:
            annotate_error(
                str(routed_pcb),
                f"Pour-connectivity regression on re-routed {routed_pcb}: "
                f"{'; '.join(pour_failures)}.  Every pour net must be ONE "
                "copper component with non-empty zone fills (copper-union "
                "audit, Issue #3509).  See the recipe's pour pipeline in "
                f"{board_dir.name}/generate_design.py (steps 9-11); if the "
                "fill log shows 'kicad-cli not found', the CI environment "
                "lost its KiCad container.",
            )
            failed = True
        else:
            print(
                f"[diffpair-coverage] OK: pour connectivity "
                f"({len(pour_nets)} pour nets, copper-union audit)."
            )

    # AC #4: each of the three diff-pair rules must have been exercised.
    missing = check_rule_coverage(rules_by_rule)
    if missing:
        msg = (
            f"Diff-pair rule(s) NOT exercised on {routed_pcb}: "
            f"{', '.join(missing)}.  This is a silent regression -- the rule "
            "short-circuited because no engaged pairs were detected.  Likely "
            "causes: ``coupled_routing`` flag flipped to False on the net "
            "class, diff-pair suffix-inference broken, or the routed PCB has "
            "no traces matching the declared pairs.  See "
            "src/kicad_tools/validate/rules/diffpair_*.py for the per-rule "
            "engagement conditions."
        )
        annotate_error(str(routed_pcb), msg)
        failed = True
    else:
        print(f"[diffpair-coverage] OK: all {len(DIFFPAIR_RULE_IDS)} diff-pair rules exercised.")

    # AC #1 (allowlist semantic): error count must be <= allowed.
    if error_count > allowed:
        if allowed == 0:
            msg = (
                f"DRC errors detected on re-routed {routed_pcb}: {error_count} "
                f"error(s).  Boards NOT in .github/routed-drc-tolerance.yml must "
                "report 0 errors.  Fix the routing regression or add an explicit "
                "allowlist entry with reviewer sign-off."
            )
        else:
            msg = (
                f"DRC regression on re-routed {routed_pcb}: {error_count} "
                f"error(s) exceeds allowlist value {allowed} in "
                ".github/routed-drc-tolerance.yml.  Either fix the new "
                "violations or (if intentional) raise the allowlist value."
            )
        annotate_error(str(routed_pcb), msg)
        failed = True
    else:
        if allowed == 0:
            print("[diffpair-coverage] OK: 0 errors (strict gate).")
        else:
            print(f"[diffpair-coverage] OK: {error_count} errors (within allowlist max {allowed}).")

    return 2 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_diffpair_coverage",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "boards",
        nargs="+",
        help=(
            "One or more board directories to check (e.g., boards/06-diffpair-test).  "
            "Each board must contain generate_design.py with --step route + --seed support."
        ),
    )
    parser.add_argument(
        "--allowlist",
        default=str(DEFAULT_ALLOWLIST),
        help=f"Path to the tolerance allowlist YAML (default: {DEFAULT_ALLOWLIST}).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        metavar="N",
        help=(
            "Seed forwarded to generate_design.py --seed for deterministic "
            "re-routes (default: 42, matches Issue #2589 / Phase 3X.2 examples)."
        ),
    )
    parser.add_argument(
        "--skip-route",
        action="store_true",
        help=(
            "Skip the re-route step and check the existing committed routed "
            "PCB.  Useful for local debugging of the rule-coverage assertion."
        ),
    )
    args = parser.parse_args(argv)

    try:
        allowlist = load_allowlist(Path(args.allowlist))
    except ValueError as e:
        print(f"::error::{e}", flush=True)
        return 1

    overall = 0
    for raw in args.boards:
        board_dir = Path(raw).resolve()
        rc = check_board(board_dir, allowlist, args.seed, args.skip_route)
        if rc > overall:
            overall = rc

    if overall:
        print(
            f"\nGate failed (exit {overall}).  See ::error:: annotations "
            "above; offending boards are also surfaced in the PR Files-changed "
            "view.",
            flush=True,
        )
    return overall


if __name__ == "__main__":
    sys.exit(main())
