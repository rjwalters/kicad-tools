#!/usr/bin/env python3
"""Match-group "rule exercised" CI gate (Issue #2726, Epic #2661 Phase 3N).

This script is the match-group sibling of
:mod:`scripts.ci.check_diffpair_coverage` (Issue #2660, Epic #2556 Phase
4N).  It catches regressions in the **match-group routing + DRC
algorithm itself** by re-routing a board from scratch and asserting:

  1. The post-route DRC error count is within the per-board allowlist
     (same semantic as ``check_routed_drc.py`` / ``check_diffpair_coverage.py``).
  2. The ``match_group_length_skew`` DRC rule was actually exercised by
     the check (i.e., its per-rule counter is >= 1).  Without this
     assertion a regression that disables match-group detection (e.g.,
     a future change that breaks ``derive_group_skew_data`` or unwires
     ``length_match_group`` from the net classes) would silently produce
     a 0-error report and hide the defect.

Rule id asserted:

  * ``match_group_length_skew`` (Issue #2702, Epic #2661 Phase 2G;
    producer wiring #2710 Phase 2.5G).

The script is parametric over the board directory so additional boards
can be added cheaply, but the default Phase 3N target is
``boards/07-matchgroup-test`` (Issue #2724 Phase 3L scaffolding).

Usage::

    # Re-route + check board 07 (the canonical match-group testbench):
    python scripts/ci/check_matchgroup_coverage.py boards/07-matchgroup-test

    # Check an already-routed board (skip the route step; useful for
    # debugging the assertion logic locally):
    python scripts/ci/check_matchgroup_coverage.py boards/07-matchgroup-test \\
        --skip-route

Exit codes (mirror ``check_routed_drc.py`` / ``check_diffpair_coverage.py``):

    0 -- Gate passed (errors within tolerance AND the rule was exercised).
    1 -- Tool failure (board dir missing, route step crashed, allowlist
         parse error, etc.).
    2 -- Gate failed: errors exceed allowlist OR the rule did not run.

GitHub-Actions ``::error file=...::`` annotations are emitted on failure
so the PR Files-changed view surfaces the failure inline.

Why a separate script (not folded into ``check_diffpair_coverage.py``):

    Per the curator recommendation on Issue #2726 (curator comment
    2026-05-11), the match-group gate is a SIBLING to the diff-pair
    gate, not a matrix-extension.  Concrete reasons:

      1. Minimal blast radius -- a match-group regression PR cannot
         accidentally break the diff-pair gate and vice versa.
      2. The diff-pair gate's "rule exercised" check has three rule_ids
         hardcoded; this script asserts a different (currently single)
         rule_id.  Forking is cleaner than parametrising
         ``check_diffpair_coverage.py`` with a ``--rules`` flag.
      3. Per-job CI timeout budgets are independent (each gets a fresh
         10-min ceiling).
      4. The diff-pair coverage script re-computes skew_data from PCB
         segments to satisfy the diff-pair length-skew rule's injection
         contract.  The match-group equivalent (``derive_group_skew_data``
         in ``src/kicad_tools/validate/match_group_skew.py``) is invoked
         automatically by ``DRCChecker.check_match_group_length_skew``
         when ``net_class_map`` is supplied -- so this script does NOT
         need to replicate that producer-side logic; it just passes the
         sidecar through to ``kct check``.

Reusing logic from ``check_diffpair_coverage.py`` / ``check_routed_drc.py``:

    * Allowlist loading (``load_allowlist``)
    * Annotation helpers (``annotate_error``)
    * Re-route + PCB-locate helpers (``re_route_board``, ``find_routed_pcb``)
    * net_class_map import from generate_design.py (``build_net_class_map_for_board``)

The helpers are duplicated rather than imported to keep each CI gate's
blast radius minimal and to match the byte-for-byte convention
established by ``check_diffpair_coverage.py``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import yaml

# --- shared with check_routed_drc.py / check_diffpair_coverage.py ------------

DEFAULT_ALLOWLIST = Path(".github/routed-drc-tolerance.yml")

# The match-group rule_id that MUST be exercised on a board that
# declares match groups (board 07 declares 4 groups across 4 protocols,
# so this rule should run with rules_checked_by_rule entry >= 1).  See
# ``src/kicad_tools/drc/violation.py`` and
# ``src/kicad_tools/validate/rules/match_group_length_skew.py`` for the
# canonical rule_id string + per-rule counter increment (Issue #2702).
MATCHGROUP_RULE_IDS: tuple[str, ...] = ("match_group_length_skew",)

# Issue #3828 -- documented match-group violation BASELINE (per routed PCB).
# Symmetric with ``check_diffpair_coverage.DIFFPAIR_VIOLATION_BASELINE``.
# The map exists so a board with an accepted, separately-tracked match-group
# skew baseline can document it loudly rather than relying on the large
# error-count allowlist floor to absorb new skew errors.  Keyed by
# repo-relative routed-PCB path.
#
# Issue #3931 (via-imbalance now COMPENSATED by the tuner) -- board 07's
# ADDR_BUS match group carries a via-count mismatch: the A4/A6 members escape
# to an inner layer behind a full-stack via while A0/A1/A2/A3/A5/A7 route flat
# on F.Cu, adding ~1.6 mm of via drilled length that pushed the group past its
# 0.5 mm length-match tolerance.  This was invisible while the DRC checker was
# via-blind; PR #3915 threaded the board stackup (``board_thickness_mm`` /
# ``num_copper_layers``) into ``derive_group_skew_data`` so via-transition
# length is now counted, and the pre-existing imbalance was flagged as 1
# ``match_group_length_skew`` error.  #3931 closes the loop on the *router*
# side: the match-group length tuner is now ALSO via-aware
# (``tune_match_group_v2`` / ``_tune_match_group_single_ended`` measure member
# lengths with ``MatchGroupTracker._measure_route_total`` when a stackup is
# supplied), so during a re-route the tuner adds F.Cu meander to the via-free
# members to compensate the via-carrying members' drilled length.  A
# via-imbalanced group therefore converges to within tolerance and ADDR_BUS no
# longer fires on the reroute path.
#
# Issue #3916 (pair-only groups now length-checked) -- the producer used to
# skip any match group whose members were exclusively differential pairs
# (``net_ids=[]`` after ``_extract_pair_ids``), so board 07's MIPI_CSI_LANES
# and HDMI_TMDS_LANES were never skew-checked.  ``derive_group_skew_data`` now
# measures diff-pair members via the pair-average ``(L_P + L_N) / 2``
# contribution, making both groups checkable.  MIPI_CSI_LANES is the one
# genuine pair-only skew that survives -- it is tracked separately under #3916
# and remains the sole entry in this baseline.
#
# The value the gate observes depends on WHICH artifact it runs against, and a
# SINGLE baseline of **1** satisfies BOTH paths:
#
#   * ``--skip-route`` (committed-artifact path, used by the diff-driven
#     routed-pcb-drc-check and by local verification): counts exactly **1**
#     match-group violation.  The #3931 tuner fix is a ROUTER change and does
#     NOT regenerate the committed ``matchgroup_test_routed.kicad_pcb``
#     artifact, so that artifact still carries the pre-fix via-imbalanced
#     ADDR_BUS (which fires) while MIPI/HDMI/DDR are gated out by the
#     partial-routing rule (at least one unrouted diff-pair leg each: e.g.
#     MIPI_CLK_N, TMDS_D0_*, DQ3 carry zero geometry).  So the committed
#     artifact reports 1 (ADDR_BUS), which passes (1 <= 1).  (Refreshing the
#     committed artifact to also clear ADDR_BUS is the artifact-churn work
#     tracked in #3925 -- once it lands the committed count drops to the MIPI
#     violation and this baseline can be revisited.)
#
#   * full reroute (the Match-Group Routing Regression CI job, which runs
#     ``generate_design.py --step route`` end-to-end before the gate):
#     also counts **1**.  The via-aware tuner now compensates ADDR_BUS's via
#     imbalance during the re-route, so ADDR_BUS lands within tolerance and no
#     longer fires; only MIPI_CSI_LANES's pair-only skew (#3916) remains.
#     HDMI_TMDS_LANES does NOT fire even after the reroute: its TMDS_D0_N /
#     TMDS_D1_N legs remain unrouted ("still stranded -- NO relief path"), so
#     the unrouted-leg gate correctly excludes HDMI.
#
# Composition of the baseline (1):
#   1x MIPI_CSI_LANES pair-only skew (#3916).  (Prior to #3931 the reroute
#   count was 2 = ADDR_BUS via-imbalance + MIPI; the via-aware tuner removed
#   the ADDR_BUS term, so the reroute count is now 1 and matches the
#   committed-artifact count.)
MATCHGROUP_VIOLATION_BASELINE: dict[str, int] = {
    "boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb": 1,
}


def check_zero_violations(
    error_violations_by_rule: dict[str, int],
    baseline: int,
    required_rule_ids: tuple[str, ...] = MATCHGROUP_RULE_IDS,
) -> list[str]:
    """Return failure strings when match-group violations exceed baseline.

    Issue #3828: the old gate asserted coverage>=1 (the rule RAN) but never
    that it found zero violations -- so a future ``match_group_length_skew``
    regression of a few nets would slip under the large error-count allowlist
    floor (board 07's is 120) and the coverage check would still pass.  This
    mirrors ``check_diffpair_coverage.check_zero_violations``.
    """
    total = sum(error_violations_by_rule.get(rid, 0) for rid in required_rule_ids)
    if total <= baseline:
        return []
    detail = ", ".join(f"{rid}={error_violations_by_rule.get(rid, 0)}" for rid in required_rule_ids)
    return [
        f"match-group error-severity violations ({total}) EXCEED the documented "
        f"baseline ({baseline}): {detail}"
    ]


def load_allowlist(allowlist_path: Path) -> dict[str, int]:
    """Load the per-board DRC tolerance allowlist.

    Behaviour matches ``check_routed_drc.load_allowlist`` and
    ``check_diffpair_coverage.load_allowlist`` exactly so all three
    gates share the same YAML contract.  Duplicated here to keep this
    script self-contained.
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
            ``boards/07-matchgroup-test``).
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


def find_net_class_map_sidecar(board_dir: Path) -> Path | None:
    """Locate the board's ``net_class_map.json`` sidecar.

    The Phase 3L scaffolding (Issue #2724) emits the sidecar from
    ``generate_design.write_sidecar()`` during the route step, so the
    file exists after ``re_route_board`` returns success.  This sidecar
    is mandatory for the ``match_group_length_skew`` rule to fire under
    standalone ``kct check`` -- without it the rule degrades to a no-op
    (see ``MatchGroupLengthSkewRule`` docstring).
    """
    out = board_dir / "output"
    sidecar = out / "net_class_map.json"
    return sidecar if sidecar.is_file() else None


def _import_module_from_path(module_name: str, path: Path):
    """Import a module by file path without adding it to sys.path permanently."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_board_recipe_module(board_dir: Path):
    """Import a board's ``generate_design.py`` module by path.

    Returns ``None`` when the board has no ``generate_design.py``.  The
    imported module exposes the board's declarative routing contract:
    ``build_net_class_map()`` and -- Issue #3617 -- the optional
    ``POUR_NETS`` / ``REQUIRE_POUR_CONNECTIVITY`` constants + the
    ``_audit_pour_nets`` copper-union audit the pour-connectivity gate
    term consumes.  Mirrors ``net_class_map_resolver.load_board_recipe_module``
    (kept local to this gate to preserve the byte-for-byte sibling
    convention with ``check_diffpair_coverage.py``).
    """
    script = board_dir / "generate_design.py"
    if not script.is_file():
        return None

    # The board's generate_design.py imports its sibling modules via a
    # ``sys.path.insert(0, str(Path(__file__).parent))`` -- replicate that
    # so the import resolves correctly.
    saved_path = list(sys.path)
    sys.path.insert(0, str(board_dir))
    try:
        return _import_module_from_path(
            f"_matchgroup_coverage_generate_design_{board_dir.name.replace('-', '_')}",
            script,
        )
    finally:
        sys.path[:] = saved_path


def build_net_class_map_for_board(board_dir: Path) -> dict | None:
    """Import ``generate_design.build_net_class_map()`` from a board dir.

    Returns ``None`` if the board's ``generate_design.py`` does not
    expose a ``build_net_class_map`` function.  In that case the
    match-group rule will degrade to a no-op (the design doesn't declare
    any match groups).
    """
    mod = load_board_recipe_module(board_dir)
    if mod is None:
        return None
    return getattr(mod, "build_net_class_map", lambda: None)()


def measure_pour_connectivity(recipe_mod, pcb_path: Path, pour_nets: set[str]) -> list[str]:
    """Run the board recipe's copper-union pour audit on a routed PCB.

    Issue #3617 (sibling of board 06's #3509): board 07's recipe runs a
    shapely copper-union audit (``_audit_pour_nets``) at the end of its
    pour pipeline and prints PASS/FAIL -- but until this gate term the
    job never consumed that verdict, so a re-route whose pours failed the
    audit (or never filled at all) still went green.  Boards opt in by
    declaring ``REQUIRE_POUR_CONNECTIVITY = True`` next to their
    ``POUR_NETS`` contract; the gate then re-runs the audit against the
    routed artifact and fails the job on any disjoint pour net or
    zero-fill zone.

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
            pass -- a silent skip is how dead pours shipped in the first
            place (PR #3481).
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


def count_errors_via_kct_check(pcb_path: Path, sidecar: Path | None) -> int:
    """Count errors via ``kct check --mfr jlcpcb --errors-only --format json``.

    Mirrors ``check_diffpair_coverage.count_errors_via_kct_check`` so the
    allowlist semantic matches the sibling CI gates exactly -- a value of
    80 on the same routed PCB must mean the same thing across all three
    gates (committed-diff, diff-pair regression, match-group regression).

    The ``--net-class-map`` sidecar (when present) is passed through
    explicitly per the Phase 3L pattern (#2724 / curator comment).
    Without the sidecar the ``match_group_length_skew`` rule degrades
    to a no-op and the violation count would be artificially low.

    Args:
        pcb_path: Path to the routed PCB.
        sidecar: Optional path to ``net_class_map.json``.  Required for
            the match-group rule to fire under standalone ``kct check``.

    Returns:
        The ``summary.errors`` integer from the kct check JSON envelope.
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
    if sidecar is not None:
        cmd.extend(["--net-class-map", str(sidecar)])
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
) -> tuple[dict[str, int], dict[str, int]]:
    """Compute per-rule check counts AND per-rule error-violation counts.

    Issue #3828: also returns a ``{rule_id -> error_violation_count}`` map
    (error-severity only, restricted to ``MATCHGROUP_RULE_IDS``) so
    ``check_board`` can fail when the match-group rule fires beyond its
    documented baseline.  The ``DRCResults`` already carries both, so no
    second tool run is needed.

    Invokes :class:`kicad_tools.validate.DRCChecker` directly so the
    per-rule counter is accessible without re-parsing JSON.  The
    ``net_class_map`` is threaded through to the checker so
    :func:`~kicad_tools.validate.match_group_skew.derive_group_skew_data`
    runs and the match-group rule engages (without it the rule
    degrades to a no-op and ``rules_checked_by_rule`` never gets the
    ``match_group_length_skew`` entry).

    This is a SEPARATE pass from the error-count check (which uses
    the standalone ``kct check`` for allowlist parity).  The two passes
    serve different purposes: error-count gates regression in the
    re-routed PCB, while coverage gates regression in the detection
    logic (e.g., a future change that breaks ``derive_group_skew_data``
    would silently zero the rule's per-rule counter).

    Args:
        pcb_path: Path to the routed PCB.
        net_class_map: Optional ``{net_name: NetClassRouting}`` map from
            the board's ``build_net_class_map()``.  Required for the
            match-group rule to engage.

    Returns:
        A ``(rules_checked_by_rule, error_violations_by_rule)`` tuple.
        The first maps ``rule_id -> times-run`` (coverage); the second
        maps ``rule_id -> count of error-severity violations`` (restricted
        to ``MATCHGROUP_RULE_IDS``).

    Raises:
        RuntimeError: If the PCB cannot be loaded.
    """
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate import DRCChecker

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

    # Run all rules via the standard checker entry point.  The
    # match-group rule auto-derives skew data via
    # ``derive_group_skew_data`` when ``net_class_map`` is supplied
    # (see ``DRCChecker.check_match_group_length_skew``).  Unlike the
    # diff-pair length-skew rule, no extra second-pass is required --
    # the producer wiring (#2710 Phase 2.5G) handles everything.
    results = checker.check_all()

    # Issue #3828: per-rule ERROR-severity violation counts for the
    # match-group rule(s).
    error_violations_by_rule: dict[str, int] = {}
    for rule_id in MATCHGROUP_RULE_IDS:
        error_violations_by_rule[rule_id] = sum(
            1 for v in results.violations if v.rule_id == rule_id and v.is_error
        )

    return dict(results.rules_checked_by_rule), error_violations_by_rule


def check_rule_coverage(
    rules_checked_by_rule: dict[str, int],
    required_rule_ids: tuple[str, ...] = MATCHGROUP_RULE_IDS,
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
    Mirrors the exit-code convention from ``check_routed_drc.py`` /
    ``check_diffpair_coverage.py``.
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

    sidecar = find_net_class_map_sidecar(board_dir)
    if sidecar is None:
        annotate_error(
            str(board_dir),
            (
                "No net_class_map.json sidecar found in board output dir; "
                "the match_group_length_skew rule will degrade to a no-op "
                "without it.  Expected at "
                f"{board_dir}/output/net_class_map.json (emitted by "
                "generate_design.write_sidecar)."
            ),
        )
        return 1

    # Build the net_class_map from the board's generate_design module
    # so match-group detection in the DRC rule has the per-class
    # context (length_match_group declarations, length_match_tolerance_mm
    # overrides, etc.).
    try:
        net_class_map = build_net_class_map_for_board(board_dir)
    except Exception as e:
        annotate_error(
            str(board_dir),
            f"Failed to import build_net_class_map from {board_dir}: {e}",
        )
        return 1

    if not net_class_map:
        annotate_error(
            str(board_dir),
            (
                "build_net_class_map() returned an empty/None map; the "
                "match_group_length_skew rule cannot fire on a board with "
                "no declared match groups.  Check that the board's "
                "generate_design.py exposes a non-empty build_net_class_map."
            ),
        )
        return 1

    # Issue #3617: read the board's pour-connectivity contract.  Boards
    # that declare ``REQUIRE_POUR_CONNECTIVITY = True`` get a hard
    # copper-union pour audit; boards without it skip the term.
    pour_nets: set[str] = set()
    require_pour_connectivity = False
    recipe_mod = None
    try:
        recipe_mod = load_board_recipe_module(board_dir)
        if recipe_mod is not None:
            pour_nets = set(getattr(recipe_mod, "POUR_NETS", ()) or ())
            require_pour_connectivity = bool(
                getattr(recipe_mod, "REQUIRE_POUR_CONNECTIVITY", False)
            )
    except Exception as e:
        annotate_error(
            str(board_dir),
            f"Failed to read pour contract from {board_dir}: {e}",
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
    #   1. Error count via subprocess ``kct check --net-class-map`` --
    #      matches the sibling allowlist semantic exactly AND ensures
    #      the match-group rule contributes to the error count.
    #   2. Rule coverage via in-process DRCChecker(..., net_class_map=...)
    #      -- the only way to confirm the rule's per-rule counter
    #      actually incremented (a regression in derive_group_skew_data
    #      would zero the counter even with a correct error count).
    try:
        error_count = count_errors_via_kct_check(routed_pcb, sidecar)
    except RuntimeError as e:
        annotate_error(str(routed_pcb), f"kct check failed: {e}")
        return 1
    try:
        rules_by_rule, matchgroup_error_violations = compute_rule_coverage(
            routed_pcb, net_class_map
        )
    except RuntimeError as e:
        annotate_error(str(routed_pcb), f"Rule-coverage probe failed: {e}")
        return 1

    baseline = MATCHGROUP_VIOLATION_BASELINE.get(lookup_key, 0)

    print(f"\n[matchgroup-coverage] Board: {board_dir.name}")
    print(f"[matchgroup-coverage] Routed PCB: {routed_pcb}")
    print(f"[matchgroup-coverage] Sidecar: {sidecar}")
    print(f"[matchgroup-coverage] DRC error count: {error_count} (allowed: {allowed})")
    print(f"[matchgroup-coverage] rules_checked_by_rule: {rules_by_rule}")
    print(
        f"[matchgroup-coverage] match-group error violations: "
        f"{matchgroup_error_violations} (baseline: {baseline})"
    )

    failed = False

    # AC #4: the match_group_length_skew rule must have been exercised.
    missing = check_rule_coverage(rules_by_rule)
    if missing:
        msg = (
            f"Match-group rule(s) NOT exercised on {routed_pcb}: "
            f"{', '.join(missing)}.  This is a silent regression -- the rule "
            "short-circuited because no declared groups were detected with "
            "measurable skew.  Likely causes: ``length_match_group`` field "
            "unwired from one or more net classes in build_net_class_map, "
            "derive_group_skew_data broken (e.g., detect_match_groups "
            "returning empty), or the routed PCB has no traces matching any "
            "declared group's members.  See "
            "src/kicad_tools/validate/match_group_skew.py and "
            "src/kicad_tools/validate/rules/match_group_length_skew.py for "
            "the engagement conditions."
        )
        annotate_error(str(routed_pcb), msg)
        failed = True
    else:
        print(
            f"[matchgroup-coverage] OK: all {len(MATCHGROUP_RULE_IDS)} match-group "
            "rule(s) exercised."
        )

    # Issue #3828: zero-violation assertion (symmetric with the diff-pair
    # gate).  Distinct from coverage>=1 (a rule that RAN) and from the
    # error-count allowlist: the match-group rule's ERROR-severity violation
    # count must not exceed the documented per-board baseline (default 0).
    # Without it a future match_group_length_skew regression of a few nets
    # would slip under board 07's large 120-error allowlist floor and the
    # coverage>=1 check would still pass.
    violation_failures = check_zero_violations(matchgroup_error_violations, baseline)
    if violation_failures:
        msg = (
            f"Match-group VIOLATION regression on {routed_pcb}: "
            f"{'; '.join(violation_failures)}.  These are real length-match "
            "skew defects measured WITH the net-class sidecar.  Either fix the "
            "routing so the groups meet their length-match tolerance, or -- if "
            "this is an accepted, separately-tracked baseline -- raise "
            "MATCHGROUP_VIOLATION_BASELINE for this PCB in "
            "scripts/ci/check_matchgroup_coverage.py with a tracking reference. "
            "Do NOT silently widen it: the point of this gate is to catch "
            "regressions beyond the documented baseline."
        )
        annotate_error(str(routed_pcb), msg)
        failed = True
    else:
        if baseline == 0:
            print("[matchgroup-coverage] OK: 0 match-group error violations (strict).")
        else:
            total = sum(matchgroup_error_violations.get(rid, 0) for rid in MATCHGROUP_RULE_IDS)
            print(
                f"[matchgroup-coverage] OK: {total} match-group error violation(s) "
                f"within documented baseline {baseline}."
            )

    # Issue #3617: pour-connectivity assertion.  The board recipe's
    # copper-union audit verdict was previously informational only -- the
    # recipe never even filled its pours, so the routed artifact shipped
    # zone outlines with zero fill geometry and this gate stayed green.
    # Boards opting in via REQUIRE_POUR_CONNECTIVITY get the audit re-run
    # here against the routed artifact; failures gate (exit 2).  An audit
    # that cannot run at all is a tool failure (exit 1) -- never a silent
    # pass.
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
                "audit, Issue #3617).  See the recipe's pour pipeline in "
                f"{board_dir.name}/generate_design.py (zone fill + stitch + "
                "repair loop); if the fill log shows 'kicad-cli not found', "
                "the CI environment lost its KiCad container.",
            )
            failed = True
        else:
            print(
                f"[matchgroup-coverage] OK: pour connectivity "
                f"({len(pour_nets)} pour nets, copper-union audit)."
            )

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
            print("[matchgroup-coverage] OK: 0 errors (strict gate).")
        else:
            print(
                f"[matchgroup-coverage] OK: {error_count} errors (within allowlist max {allowed})."
            )

    return 2 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_matchgroup_coverage",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "boards",
        nargs="+",
        help=(
            "One or more board directories to check (e.g., boards/07-matchgroup-test).  "
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
