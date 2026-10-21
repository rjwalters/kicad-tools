#!/usr/bin/env python3
"""Match-group "rule exercised" CI gate (Issue #2726, Epic #2661 Phase 3N).

This script is the match-group sibling of
:mod:`scripts.ci.check_diffpair_coverage` (Issue #2660, Epic #2556 Phase
4N).  It catches regressions in the **match-group routing + DRC
algorithm itself** by re-routing a board from scratch and asserting:

  1. Every blocking post-route DRC finding is listed by identity in the
     board's allowance (Issue #7367), every listed entry still fires, and
     the count of blocking findings outside the allowance is within the
     per-board allowlist (absent = 0, the fixture's value).
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
    2 -- Gate failed: errors exceed allowlist OR the rule did not run OR
         fewer signal nets complete than the recipe's
         ``REQUIRED_SIGNAL_REACH`` (Issue #7319).

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

Most helpers are duplicated rather than imported to keep each CI gate's
blast radius minimal and to match the byte-for-byte convention
established by ``check_diffpair_coverage.py``.

Exception (Issue #4008): the blocking-vs-advisory error counter is
IMPORTED from :mod:`net_class_map_resolver` rather than duplicated, so
this gate and ``check_routed_drc.py`` compare the *same* count against the
*same* ``.github/routed-drc-tolerance.yml`` floor.  This script previously
read the raw ``summary.errors`` integer (advisory ``connectivity`` errors
included), which diverged from ``check_routed_drc.py``'s blocking-only
count and forced the shared floor up to absorb the difference.

Exception (Issue #4634): the net-class-map sidecar *filename* probe is
likewise no longer duplicated -- ``find_net_class_map_sidecar`` delegates
the name set and their in-directory precedence to
``kicad_tools.sidecars``.  Four copies of that probe had drifted apart
after #4601/PR #4629 taught ``kct check`` about the stem-keyed
``<pcb_stem>.net_class_map.json`` name, so this gate could have passed a
different sidecar to ``kct check`` than a developer's local ``kct check``
would have auto-discovered.  The directory follows the selected recipe artifact, including isolated legacy fixtures.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from board_recipe_artifacts import (  # noqa: E402
    measure_signal_reach,
    recipe_baseline_key,
    recipe_output_dir,
)

# Issue #4634: the sidecar filename rules are shared with ``kct check``, the
# routed-DRC gate and the ``kct export`` report surface via the stdlib-only
# leaf module ``kicad_tools.sidecars``, so no two of them can resolve
# different files for the same board.
from kicad_tools.sidecars import (
    first_existing_net_class_map_sidecar,
    net_class_map_sidecar_names,
)

# Issue #4008: import the single shared blocking-error counter so this gate
# and ``check_routed_drc.py`` agree on the count they gate against.
# ``net_class_map_resolver`` lives next to this script in ``scripts/ci``.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from identity_allowance import (  # noqa: E402
    Allowance,
    error_identities,
    format_stale,
    stale_entries,
    unallowed_findings,
    unlisted_findings,
)
from net_class_map_resolver import count_blocking_errors  # noqa: E402

# --- shared with check_routed_drc.py / check_diffpair_coverage.py ------------

DEFAULT_ALLOWLIST = Path(".github/routed-drc-tolerance.yml")

# The match-group rule_id that MUST be exercised on a board that
# declares match groups (board 07 declares 4 groups across 4 protocols,
# so this rule should run with rules_checked_by_rule entry >= 1).  See
# ``src/kicad_tools/drc/violation.py`` and
# ``src/kicad_tools/validate/rules/match_group_length_skew.py`` for the
# canonical rule_id string + per-rule counter increment (Issue #2702).
MATCHGROUP_RULE_IDS: tuple[str, ...] = ("match_group_length_skew",)

#: Open tracker for board 07's seed-42 skew residuals (Issue #7367): the 6
#: diff pairs and 2 match groups below.  The groups used to cite #3916 / #7339
#: (closed) and #5798 (pending close).
MATCHGROUP_SKEW_TRACKER = 7550

# Repo-relative key of board 07's synthetic regression fixture.  Its re-route
# lands in the git-ignored ``regression-output/``, which
# ``recipe_baseline_key`` canonicalises back to this key.
MATCHGROUP_FIXTURE_KEY = (
    "boards/07-matchgroup-test/regression-fixture/matchgroup_test_routed.kicad_pcb"
)

# Issue #3828 / #7339 -- the match groups that may carry a
# ``match_group_length_skew`` error, each with an OPEN tracking issue.
#
# Issue #3931: ADDR_BUS's via-count imbalance is compensated by the via-aware
# tuner, so it no longer fires on the re-route.  Issue #3916 made pair-only
# groups checkable; MIPI_CSI_LANES is the one pair-only group whose skew
# survives (7.039 mm vs 0.050 mm).  Issue #7339: DDR_DATA_BYTE_0 fires once
# DQ0 routes (#7331): 16.717 mm vs 0.100 mm.  Both are tracked in #7550.
# Issue #7557: HDMI_TMDS_LANES becomes measurable once TMDS_D0_N routes (U4's
# TMDS balls moved to its J2-facing column): 7.723 mm vs 0.075 mm, also #7550.
#
# Per the operator ruling in #7270 (Q3) the allowance is NOT a count: it names
# the exact groups that may fire, so a DIFFERENT group failing (e.g. ADDR_BUS
# regressing) still trips the gate.  On the full-reroute path (the Match-Group
# Routing Regression CI job) an allowed group that no longer fires is STALE
# and fails the gate, so fixing a group forces its entry to be deleted.  On
# ``--skip-route`` the group allowance stays an upper bound only
# (``enforce_stale=False``, unchanged by #7367): the committed fixture gates
# DDR/HDMI out as partially routed.  Staleness of the identity entries in
# MATCHGROUP_FIXTURE_ALLOWED_FINDINGS is enforced on both paths.
#
# REMOVAL: delete an entry once its group meets its length-match tolerance
# (tracked in the issue it cites).
MATCHGROUP_ALLOWED_GROUPS: dict[str, dict[str, int]] = {
    MATCHGROUP_FIXTURE_KEY: {
        "MIPI_CSI_LANES": MATCHGROUP_SKEW_TRACKER,
        "DDR_DATA_BYTE_0": MATCHGROUP_SKEW_TRACKER,
        "HDMI_TMDS_LANES": MATCHGROUP_SKEW_TRACKER,
    },
}


def check_group_allowances(
    fired_groups: set[str],
    allowed_groups: dict[str, int],
    *,
    enforce_stale: bool,
) -> list[str]:
    """Return failures for match groups outside / no longer needing the allowance.

    Issue #7339 / #7270 Q3: the allowance is keyed to named groups plus a
    tracking issue, not a count.  A fired group that is not allowed fails
    (so a different regression is never absorbed in place of a known one);
    with ``enforce_stale`` an allowed group that did not fire fails too, so
    fixing it forces removal of its entry.
    """
    failures: list[str] = []
    unexpected = sorted(fired_groups - allowed_groups.keys())
    if unexpected:
        failures.append(
            "match-group skew fired on group(s) with no allowance: " + ", ".join(unexpected)
        )
    if enforce_stale:
        stale = sorted(allowed_groups.keys() - fired_groups)
        if stale:
            detail = ", ".join(f"{g} (#{allowed_groups[g]})" for g in stale)
            failures.append(
                f"STALE allowance: {detail} no longer fires -- remove the entry from "
                "MATCHGROUP_ALLOWED_GROUPS"
            )
    return failures


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
        str(recipe_output_dir(board_dir, prepare=True)),
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

    Walks the isolated recipe output (or legacy fixture) looking for the canonical
    ``*_routed.kicad_pcb`` artifact emitted by ``generate_design.py``.
    Returns ``None`` if not found (caller emits the error).
    """
    out = recipe_output_dir(board_dir)
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


def find_net_class_map_sidecar(board_dir: Path, routed_pcb: Path) -> Path | None:
    """Locate the board's net-class-map sidecar.

    The Phase 3L scaffolding (Issue #2724) emits the sidecar from
    ``generate_design.write_sidecar()`` during the route step, so the
    file exists after ``re_route_board`` returns success.  This sidecar
    is mandatory for the ``match_group_length_skew`` rule to fire under
    standalone ``kct check`` -- without it the rule degrades to a no-op
    (see ``MatchGroupLengthSkewRule`` docstring).

    Issue #4634: both accepted filenames are probed in the shared order
    (stem-keyed ``<pcb_stem>.net_class_map.json`` first, then the bare
    ``net_class_map.json``) via :mod:`kicad_tools.sidecars`, so this gate
    can never resolve a different file than the ``kct check`` invocation
    it goes on to gate.  The directory follows the selected routed PCB, so a synthetic fixture
    cannot accidentally consume the assembled board's sidecar.

    Args:
        board_dir: The board directory (``boards/NN-name``).
        routed_pcb: The routed PCB the sidecar must describe -- supplies
            the exact stem for the stem-keyed candidate.  The caller
            already holds it from :func:`find_routed_pcb`.
    """
    return first_existing_net_class_map_sidecar([routed_pcb.parent], routed_pcb.stem)


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


def count_errors_via_kct_check(
    pcb_path: Path,
    sidecar: Path | None,
    *,
    error_rules: dict[str, int] | None = None,
    error_violations: list[dict] | None = None,
) -> int:
    """Count BLOCKING errors via ``kct check --mfr jlcpcb --errors-only``.

    Issue #4008: this counter now applies the same advisory-rule filter as
    ``check_routed_drc.py`` -- it delegates to the shared
    :func:`net_class_map_resolver.count_blocking_errors`, which excludes
    ``DRCChecker.ADVISORY_RULE_IDS`` (currently just ``connectivity``) from
    the count.  Previously this function returned the raw ``summary.errors``
    integer, so on board 07 it reported 14 (9 blocking + 5 advisory
    connectivity) while ``check_routed_drc.py`` reported 9 for the SAME
    routed PCB against the SAME ``.github/routed-drc-tolerance.yml`` floor.
    That divergence forced the shared floor up to the raw count and granted
    the blocking gate dead slack.  Both gates now compare the identical
    blocking-only count, so the floor can be tightened to the blocking
    number.

    The allowlist semantic still matches the sibling CI gates exactly -- a
    value of N on the same routed PCB means the same thing across all
    gates (committed-diff, diff-pair regression, match-group regression),
    now that they all count blocking errors.

    The ``--net-class-map`` sidecar (when present) is passed through
    explicitly per the Phase 3L pattern (#2724 / curator comment).
    Without the sidecar the ``match_group_length_skew`` rule degrades
    to a no-op and the violation count would be artificially low.

    Args:
        pcb_path: Path to the routed PCB.
        sidecar: Optional path to ``net_class_map.json``.  Required for
            the match-group rule to fire under standalone ``kct check``.

    Returns:
        The blocking (advisory-filtered) error count from the kct check
        JSON envelope.
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

    # ``kct check``'s advisory drift banner is routed to stderr, but strip
    # any leading non-``{`` lines defensively (mirrors check_routed_drc.py)
    # so an older/regressed ``kct`` that prints ahead of the JSON body
    # cannot break this parser.  The payload is always a single top-level
    # object, so the first ``{`` reliably marks its start.
    raw_stdout = proc.stdout
    first_brace = raw_stdout.find("{")
    json_stdout = raw_stdout[first_brace:] if first_brace > 0 else raw_stdout
    try:
        data = json.loads(json_stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"kct check produced invalid JSON on {pcb_path}: {e}") from e

    try:
        blocking, _advisory_by_rule = count_blocking_errors(data)
    except RuntimeError as e:
        raise RuntimeError(f"{e} (source: {pcb_path})") from e
    if error_rules is not None:
        for violation in data.get("violations", []):
            if violation.get("severity") == "error":
                rule = violation.get("rule_id", "unknown")
                error_rules[rule] = error_rules.get(rule, 0) + 1
    if error_violations is not None:
        error_violations.extend(
            v for v in data.get("violations", []) if v.get("severity") == "error"
        )
    return blocking


# Issue #7332 -- declared ``target_diff_impedance`` is judged on coupled
# segments (#7306/#7293).  The model is right; the routed copper misses.  The
# coupled trunk keeps the breakout pitch instead of tightening to the solved
# ``intra_pair_clearance`` (0.100 mm in net_class_map.json):
#   fixture MIPI_DAT1 trunk F.Cu w=0.225  gap 0.283 mm vs 0.100 mm -> 116.3 ohm
#   seed-42 re-route MIPI_DAT1  gap ~0.43 mm -> 122.6 ohm
#   seed-42 re-route TMDS_D1    gap ~0.49 mm -> 125.3 ohm
# (declared 100 ohm +/-10 %).  Same root cause as #7312 (board 06) and the
# shared router defect #7344.  Seed-42 yields 4 errors: MIPI_DAT1 P/N and
# TMDS_D1 P/N.
# Issue #7349: the recipe now runs the #7344 gap-tightening pass
# (``--tighten-diffpair-gap``).  Measured on the seed-42 re-route with #7397's
# match-group top-up merged, the pass leaves all 4 findings: MIPI_DAT1 tightens
# 1 of its 2 runs (the other is declined because it would worsen the pair's
# ``kct check`` skew), so its remaining run still reads 122.5 ohm; TMDS_D1
# (125.2 ohm) is declined for the same reason.  MIPI_DAT1's skew drops
# 1.440 -> 1.021 mm.
# The #7349 unequal (``balanced_*``) splits can bring both pairs to 100 ohm,
# but only by adding uncoupled jogs to trunks that already sit inside the
# 0.5 mm coupling window (MIPI_DAT1 uncoupled P+N 4.341 -> 5.095 mm, coupled
# fraction 86.7 % -> 84.6 %; TMDS_D1 48.372 -> 49.379 mm), so the
# uncoupled-length gate (operator Q12) declines them and the 4 entries stay.
# Deleting these 4 entries is the whole of #7349.
# Issue #7557: once TMDS_D0_N routes (U4's TMDS balls on its J2-facing
# column) the TMDS_D0 pair's F.Cu run reads 125.2 ohm too -- the same
# defect, so it joins the #7349 entries (6 in total).
#
# Issue #7367 -- the ``diffpair_length_skew`` pairs, keyed by pair, not by a
# count.  Until #7367 the gate let every skew/continuity finding through by
# rule and bounded them only with the ``.github/routed-drc-tolerance.yml``
# total of 8, so a new pair could fail in place of a fixed one.  The pairs
# below are the ones the seed-42 re-route measures over ``max_skew_mm``
# (``e1c361478``, 2026-10-20, identical to ``c2c1beb02``); each cites #7550.
#
# Allowed by identity, not by count (#7359, #7270 Q3 ruling): only these
# identities may carry the listed errors, a listed identity that stops firing
# on the full re-route is stale and fails the gate, and an allowed finding
# does not count against the (now strict 0) routed-DRC total.
MATCHGROUP_FIXTURE_ALLOWED_FINDINGS: Allowance = {
    ("impedance", "MIPI_DAT1_N"): 7349,
    ("impedance", "MIPI_DAT1_P"): 7349,
    ("impedance", "TMDS_D1_N"): 7349,
    ("impedance", "TMDS_D1_P"): 7349,
    ("impedance", "TMDS_D0_N"): 7349,  # Issue #7557: routes now
    ("impedance", "TMDS_D0_P"): 7349,
    # skew vs max_skew_mm, mm
    # DQS is routed with unequal via counts (P 0, N 2); gap tightening's
    # ``declined:skew`` is correct (Issue #7613), tracked in #7619.  Since
    # issue #7557 moved U4's TMDS balls to its J2-facing column, TMDS_D0
    # (newly routed) carries the via imbalance instead (P 2, N 0) and TMDS_D1
    # no longer does (its skew fell 4.968 -> 1.584 mm, now plain #7550).
    ("diffpair_length_skew", "DQS_N/DQS_P"): 7619,  # 5.562 / 0.050
    ("diffpair_length_skew", "MIPI_CLK_N/MIPI_CLK_P"): MATCHGROUP_SKEW_TRACKER,  # 4.854 / 0.050
    ("diffpair_length_skew", "MIPI_DAT0_N/MIPI_DAT0_P"): MATCHGROUP_SKEW_TRACKER,  # 4.060 / 0.050
    ("diffpair_length_skew", "MIPI_DAT1_N/MIPI_DAT1_P"): MATCHGROUP_SKEW_TRACKER,  # 1.021 / 0.050
    ("diffpair_length_skew", "TMDS_D0_N/TMDS_D0_P"): 7619,  # 4.149 / 0.075
    ("diffpair_length_skew", "TMDS_D1_N/TMDS_D1_P"): MATCHGROUP_SKEW_TRACKER,  # 1.584 / 0.075
    ("diffpair_length_skew", "TMDS_D2_N/TMDS_D2_P"): MATCHGROUP_SKEW_TRACKER,  # 0.833 / 0.075
}

#: Every board key that carries an identity allowance (Issue #7367).  A key
#: absent here has no allowance at all: every blocking finding counts.
MATCHGROUP_ALLOWANCES: dict[str, Allowance] = {
    MATCHGROUP_FIXTURE_KEY: MATCHGROUP_FIXTURE_ALLOWED_FINDINGS,
}


def _is_advisory(rule: str) -> bool:
    """``connectivity`` and friends are reported but never block (Issue #4008)."""
    from kicad_tools.validate.checker import DRCChecker

    return DRCChecker.is_advisory_rule(rule)


def board_allowance(key: str) -> Allowance:
    """Every ``(rule, identity)`` allowed on ``key``: findings plus named groups.

    The match groups in ``MATCHGROUP_ALLOWED_GROUPS`` are identities of the
    ``match_group_length_skew`` rule, so they are folded in here; their
    staleness is judged by ``check_group_allowances``.
    """
    allowance = dict(MATCHGROUP_ALLOWANCES.get(key, {}))
    for group, issue in MATCHGROUP_ALLOWED_GROUPS.get(key, {}).items():
        allowance[("match_group_length_skew", group)] = issue
    return allowance


def unexpected_baseline_errors(
    key: str,
    errors: dict[str, int],
    identities: dict[str, set[str]] | None = None,
) -> dict[str, int]:
    """Blocking findings on an allowance key that no entry covers, per rule.

    ``identities`` maps rule -> identities carrying an error
    (``error_identities``).  Every blocking rule is judged by identity
    (Issue #7367: no rule is let through by name); without ``identities``
    nothing is trusted, so every finding counts as unexpected.  Advisory
    ``connectivity`` is outside the blocking set (reach is gated by
    ``check_signal_reach``).  A key with no allowance returns ``{}``: there
    the strict-0 total already fails on any blocking finding.
    """
    if key not in MATCHGROUP_ALLOWANCES and key not in MATCHGROUP_ALLOWED_GROUPS:
        return {}
    allowance = board_allowance(key)
    unexpected: dict[str, int] = {}
    for rule, count in errors.items():
        if not count or _is_advisory(rule):
            continue
        if identities is not None:
            count = len(unlisted_findings(allowance, rule, identities.get(rule, set())))
        if count:
            unexpected[rule] = count
    return unexpected


def stale_allowances(key: str, identities: dict[str, set[str]]) -> list[tuple[str, str, int]]:
    """Listed findings that no longer occur on the baselined board."""
    return stale_entries(MATCHGROUP_ALLOWANCES.get(key, {}), identities)


def unallowed_blocking(key: str, violations: list[dict]) -> list[tuple[str, str]]:
    """Blocking findings ``key``'s allowance does not cover (Issue #7367).

    This, not the raw blocking count, is what the routed-DRC total is
    compared to: an identity-allowed finding never uses up budget.
    """
    return unallowed_findings(board_allowance(key), violations, advisory=_is_advisory)


def compute_rule_coverage(
    pcb_path: Path,
    net_class_map: dict | None,
) -> tuple[dict[str, int], dict[str, int], set[str]]:
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
        A ``(rules_checked_by_rule, error_violations_by_rule, fired_groups)``
        tuple.  The first maps ``rule_id -> times-run`` (coverage); the second
        maps ``rule_id -> count of error-severity violations`` (restricted
        to ``MATCHGROUP_RULE_IDS``); the third is the set of match-group
        names with an error-severity violation (Issue #7339).

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

    fired_groups = {
        name
        for v in results.violations
        if v.rule_id in MATCHGROUP_RULE_IDS and v.is_error
        for name in v.items
    }
    return dict(results.rules_checked_by_rule), error_violations_by_rule, fired_groups


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


def check_signal_reach(
    board_dir: Path,
    routed_pcb: Path,
    required_reach: int | None,
    pour_nets: set[str],
) -> int:
    """Assert the board's ``REQUIRED_SIGNAL_REACH`` contract (Issue #7319).

    Counts complete signal nets (pour nets excluded) with the same
    :func:`board_recipe_artifacts.measure_signal_reach` board 06's gate
    uses.  Returns an exit code: 0 (met, or the board declares no
    contract), 2 (gate failure: reach below the contract, incomplete nets
    named), or 1 (tool failure: the measurement could not run -- a skipped
    reach check must never pass silently).
    """
    if required_reach is None:
        return 0
    try:
        complete, signal_total, incomplete_names = measure_signal_reach(routed_pcb, pour_nets)
    except Exception as e:
        annotate_error(str(routed_pcb), f"Reach measurement failed: {e}")
        return 1
    print(
        f"[matchgroup-coverage] Signal reach: {complete}/{signal_total} "
        f"(required: {required_reach}); incomplete: {incomplete_names or 'none'}"
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
        return 2
    print(f"[matchgroup-coverage] OK: reach {complete}/{signal_total} >= {required_reach}.")
    return 0


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

    sidecar = find_net_class_map_sidecar(board_dir, routed_pcb)
    if sidecar is None:
        expected = " or ".join(
            f"{board_dir}/output/{name}" for name in net_class_map_sidecar_names(routed_pcb.stem)
        )
        annotate_error(
            str(board_dir),
            (
                "No net-class-map sidecar found in board output dir; "
                "the match_group_length_skew rule will degrade to a no-op "
                f"without it.  Expected at {expected} (emitted by "
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
    # Issue #7319: the same module's ``REQUIRED_SIGNAL_REACH`` (board 06's
    # #3413 phase 5 contract) gets a hard reach assertion.
    pour_nets: set[str] = set()
    require_pour_connectivity = False
    required_reach: int | None = None
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
            f"Failed to read pour contract from {board_dir}: {e}",
        )
        return 1

    # Compute the allowlist key in the same way check_routed_drc.py does:
    # repo-relative path string.
    lookup_key = recipe_baseline_key(routed_pcb)
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
        error_rules: dict[str, int] = {}
        error_violations: list[dict] = []
        error_count = count_errors_via_kct_check(
            routed_pcb, sidecar, error_rules=error_rules, error_violations=error_violations
        )
    except RuntimeError as e:
        annotate_error(str(routed_pcb), f"kct check failed: {e}")
        return 1
    try:
        rules_by_rule, matchgroup_error_violations, fired_groups = compute_rule_coverage(
            routed_pcb, net_class_map
        )
    except RuntimeError as e:
        annotate_error(str(routed_pcb), f"Rule-coverage probe failed: {e}")
        return 1

    allowed_groups = MATCHGROUP_ALLOWED_GROUPS.get(lookup_key, {})

    print(f"\n[matchgroup-coverage] Board: {board_dir.name}")
    print(f"[matchgroup-coverage] Routed PCB: {routed_pcb}")
    print(f"[matchgroup-coverage] Sidecar: {sidecar}")
    print(f"[matchgroup-coverage] DRC error count: {error_count} (allowed: {allowed})")
    print(f"[matchgroup-coverage] All error rules: {error_rules}")
    print(f"[matchgroup-coverage] rules_checked_by_rule: {rules_by_rule}")
    print(
        f"[matchgroup-coverage] match-group error violations: "
        f"{matchgroup_error_violations} (fired groups: {sorted(fired_groups)}; "
        f"allowed: {sorted(allowed_groups)})"
    )

    failed = False

    # Issue #7319: reach assertion.  Before it, the seed-42 re-route
    # dropped DQ0 (30/31 -> 29/31) after #7163 and the gate stayed silent;
    # the only symptom was a tripled wall time.
    reach_rc = check_signal_reach(board_dir, routed_pcb, required_reach, pour_nets)
    if reach_rc == 1:
        return 1
    if reach_rc:
        failed = True

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

    # Issue #3828 / #7339: zero-violation assertion with a NAMED allowance.
    # Distinct from coverage>=1 (a rule that RAN) and from the error-count
    # allowlist: only the groups in MATCHGROUP_ALLOWED_GROUPS may fire (default
    # none), and on the full reroute each must still fire (else it is stale).
    # Without it a future match_group_length_skew regression would slip under
    # board 07's large 120-error allowlist floor, or be absorbed in place of a
    # known group by a bare count.
    violation_failures = check_group_allowances(
        fired_groups, allowed_groups, enforce_stale=not skip_route
    )
    if violation_failures:
        msg = (
            f"Match-group VIOLATION regression on {routed_pcb}: "
            f"{'; '.join(violation_failures)}.  These are real length-match "
            "skew defects measured WITH the net-class sidecar.  Either fix the "
            "routing so the groups meet their length-match tolerance, or -- if "
            "this is an accepted, separately-tracked defect -- add the NAMED "
            "group with its tracking issue to MATCHGROUP_ALLOWED_GROUPS in "
            "scripts/ci/check_matchgroup_coverage.py.  Do NOT allow by count: "
            "the point of this gate is to catch regressions beyond the "
            "documented groups."
        )
        annotate_error(str(routed_pcb), msg)
        failed = True
    elif not allowed_groups:
        print("[matchgroup-coverage] OK: 0 match-group error violations (strict).")
    else:
        print(
            f"[matchgroup-coverage] OK: fired groups {sorted(fired_groups)} are all "
            "within the named allowance."
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

    identities = error_identities(error_violations)
    unexpected = unexpected_baseline_errors(lookup_key, error_rules, identities)
    if unexpected:
        detail = "; ".join(
            f"{rule} on {', '.join(unlisted_findings(board_allowance(lookup_key), rule, identities.get(rule, set())))}"
            for rule in sorted(unexpected)
        )
        annotate_error(
            str(routed_pcb),
            f"Errors outside the identity allowance: {unexpected} ({detail}).  Fix them, "
            "or -- for an accepted, separately-tracked defect -- list the identity with "
            "its OPEN tracking issue in MATCHGROUP_FIXTURE_ALLOWED_FINDINGS.",
        )
        failed = True
    # Issue #7367: an allowance entry that no longer fires fails the gate (on
    # the full re-route and on the E2E job's regenerated bundle alike), so
    # fixing a finding forces its entry out.  It is never just a warning.
    stale = stale_allowances(lookup_key, identities)
    if stale:
        annotate_error(str(routed_pcb), f"Stale identity allowance: {format_stale(stale)}")
        failed = True

    # AC #1 (allowlist semantic), Issue #7367: the blocking findings NOT
    # covered by the identity allowance must be <= allowed (absent = 0).
    # Identity-allowed findings never consume this budget.
    unallowed = unallowed_blocking(lookup_key, error_violations)
    print(
        f"[matchgroup-coverage] Unallowed blocking findings: {len(unallowed)} "
        f"(allowed: {allowed}) {unallowed or ''}"
    )
    if len(unallowed) > allowed:
        if allowed == 0:
            msg = (
                f"DRC errors detected on re-routed {routed_pcb}: {len(unallowed)} "
                f"blocking error(s) outside the identity allowance: {unallowed}.  "
                "Boards NOT in .github/routed-drc-tolerance.yml must report 0 such "
                "errors.  Fix the routing regression; do not add a count."
            )
        else:
            msg = (
                f"DRC regression on re-routed {routed_pcb}: {len(unallowed)} "
                f"unallowed error(s) exceeds allowlist value {allowed} in "
                f".github/routed-drc-tolerance.yml: {unallowed}."
            )
        annotate_error(str(routed_pcb), msg)
        failed = True
    elif allowed == 0:
        print(
            f"[matchgroup-coverage] OK: 0 unallowed errors (strict gate; "
            f"{error_count - len(unallowed)} identity-allowed)."
        )
    else:
        print(
            f"[matchgroup-coverage] OK: {len(unallowed)} unallowed errors "
            f"(within allowlist max {allowed})."
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
