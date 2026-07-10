"""DRC parity regression tests for ``kct check`` net_class_map handling.

Issue #3151.  Three DRC rule families re-derive their working state from a
``net_class_map`` and **no-op when none is supplied** -- the documented
graceful-degradation contract for external-router boards (#2684, #2652,
#2675, #2710):

    * ``diffpair_length_skew``
    * ``diffpair_routing_continuity``
    * ``match_group_length_skew``

``kct check`` only populates the map when ``--net-class-map <sidecar>`` is
passed.  The in-pipeline DRC in ``generate_design.py::run_drc`` IS the same
``kct check`` call plus the sidecar, which is the entire 18-vs-27 delta on
board 07.  Before #3151 the strict CI gate
(``scripts/ci/check_routed_drc.py``) shelled out to ``kct check`` with NO
sidecar, so it silently missed those three families on routed boards.

These tests pin two contracts so a future silent-no-op regression (cf.
#3098 / PR #3145) is caught:

1. **No-op contract (must be PRESERVED):** bare ``kct check`` on board 07's
   routed PCB reports ZERO of the three families.  External-router boards
   rely on this -- it must never start firing diff-pair / match-group rules
   without a net_class_map.
2. **Parity (the fix):** ``kct check --net-class-map <sidecar>`` reports the
   bare error set PLUS exactly the three families, with the family-level
   counts pinned (diffpair_length_skew=4, diffpair_routing_continuity=4,
   match_group_length_skew=1 on board 07).

It also exercises the CI gate's net-class-map resolver
(``scripts/ci/net_class_map_resolver.py``) directly: committed-sidecar
preference, in-process fallback for boards (like 06) that don't commit one,
and ``None`` for boards that declare no net classes.

The real-``kct check`` parity checks are marked ``integration`` + ``slow``
(they run the full DRC engine on a routed BGA board, ~30s each).  The
resolver unit tests are fast (temp dirs + a tiny derived map) and always
run.

Sidecar isolation (Issue #4009): since #3948 ``kct check`` auto-discovers a
committed ``net_class_map.json`` next to the PCB, so a "bare" run against a
committed board artifact is no longer actually bare.  The ``bare`` fixture
below isolates the PCB from its sidecar via the shared
``isolate_pcb_from_sidecars`` helper in ``tests/conftest.py`` -- reuse that
helper for any future board's bare-check parity test instead of
re-deriving the tmp-dir-copy pattern.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from tests.conftest import isolate_pcb_from_sidecars

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_DIR = REPO_ROOT / "scripts" / "ci"

BOARD_07_PCB = (
    REPO_ROOT / "boards" / "07-matchgroup-test" / "output" / "matchgroup_test_routed.kicad_pcb"
)
BOARD_07_SIDECAR = REPO_ROOT / "boards" / "07-matchgroup-test" / "output" / "net_class_map.json"

BOARD_06_PCB = (
    REPO_ROOT / "boards" / "06-diffpair-test" / "output" / "diffpair_test_routed.kicad_pcb"
)
BOARD_06_SIDECAR = REPO_ROOT / "boards" / "06-diffpair-test" / "output" / "net_class_map.json"

# The three rule families gated on the net_class_map.  Pinning these by name
# is the load-bearing assertion: a regression that disables any one of them
# would drop it from the WITH-sidecar set and fail this test.
NET_CLASS_GATED_FAMILIES: tuple[str, ...] = (
    "diffpair_length_skew",
    "diffpair_routing_continuity",
    "match_group_length_skew",
)

# Expected per-family counts on board 07's committed routed artifact.
#
# Re-baselined 2026-06-13 (Issue #3617 / PR #3632, doctor pass) for the
# FILLED committed artifact.  Until #3617 the committed routed PCB carried
# zone OUTLINES with zero ``filled_polygon`` copper -- a dead pour (#3482
# boundary-test illusion).  #3617 wires the fill -> stitch -> repair loop
# into the recipe and commits the genuinely-filled artifact, so the
# committed file these pins measure is now the post-fill PCB.
#
# Two things change as a direct, MEASURED consequence of filling the pour:
#   * The net-class-gated delta rises 5+5+0 -> 7+7+1 (=15).  This is
#     ENGAGEMENT-over-silence, not a regression: the filled pour completes
#     two more diff pairs and the ADDR_BUS group end-to-end, so two more
#     pairs/the group get HONESTLY measured by the skew/continuity/
#     match-group rules (the dead-pour artifact left them disconnected, so
#     they contributed only advisory ``connectivity``).
#   * Advisory ``connectivity`` drops 5 -> 1: the filled pour + stitch +
#     repair geometry physically connects the pads the analyzer previously
#     reported as floating.
#
# These pins measure the COMMITTED artifact and are deterministic across
# machines (no re-route -- ``kct check`` on a fixed file).  The allowlist
# floor in .github/routed-drc-tolerance.yml is a SEPARATE, larger number
# (it gates the seed-42 RE-ROUTE, whose DRC profile varies with machine
# load + platform per the #3466 wall-clock-budget cliff, and which now
# also carries the stitcher's cross-net clearance residual -- see the
# board-07 entry in that file for the full forensic breakdown).
#
# Previous re-baselines: 2026-06-10 (Issue #3440: 5+5+0=10 delta, blocking
# 14); 2026-06-09 (issue #3458 inventory, PR #3462: 5+5+2=12, blocking 16);
# 2026-06-06 (Issue #3263: 5+5+1=11, blocking 17).
BOARD_07_EXPECTED_FAMILY_DELTA: dict[str, int] = {
    # Re-baselined 2026-07-08 (board-07 fresh re-route, fix/board07-gallery-ready):
    # 26/31 nets routed (DQ3/DQ4/MIPI_DAT0_N/TMDS_D0_N/TMDS_D1_N stranded, the
    # #3438 negotiated-reach residual).  4 pairs carry both legs but fail
    # skew/continuity (DQS, MIPI_CLK, MIPI_DAT1, TMDS_D2); ADDR_BUS carries
    # the via-inclusive 1.069mm residual (route-side tuner converges the
    # via-BLIND skew to 0.000 -- see the #3928/#3931 note in
    # scripts/ci/check_matchgroup_coverage.py).
    "diffpair_length_skew": 4,
    "diffpair_routing_continuity": 4,
    "match_group_length_skew": 1,
}


def _load_resolver_module():
    """Import ``scripts/ci/net_class_map_resolver.py`` as a module."""
    path = CI_DIR / "net_class_map_resolver.py"
    spec = importlib.util.spec_from_file_location("net_class_map_resolver_test_mod", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["net_class_map_resolver_test_mod"] = module
    # The resolver itself adds scripts/ci to sys.path for its own imports.
    spec.loader.exec_module(module)
    return module


def _run_kct_check(pcb: Path, sidecar: Path | None) -> dict:
    """Run ``kct check ... --errors-only --format json`` and parse the JSON.

    Mirrors the exact invocation used by ``scripts/ci/check_routed_drc.py``
    and board 07's ``generate_design.py::run_drc`` so the parity comparison
    is apples-to-apples.
    """
    cmd = [
        "uv",
        "run",
        "kct",
        "check",
        str(pcb),
        "--mfr",
        "jlcpcb",
        "--errors-only",
        "--format",
        "json",
    ]
    if sidecar is not None:
        cmd.extend(["--net-class-map", str(sidecar)])
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=REPO_ROOT)
    assert proc.returncode in (0, 2), (
        f"kct check exited {proc.returncode} on {pcb}.\nstderr:\n{proc.stderr}"
    )
    return json.loads(proc.stdout)


def _family_counts(payload: dict) -> Counter:
    """Counter of error-severity violations keyed by rule_id."""
    return Counter(
        v.get("rule_id")
        for v in payload.get("violations", [])
        if v.get("severity", "error") == "error"
    )


# ---------------------------------------------------------------------------
# Resolver unit tests (fast -- no real kct check)
# ---------------------------------------------------------------------------


class TestNetClassMapResolver:
    """The CI gate's net_class_map resolution logic (Issue #3151, Option B)."""

    def test_prefers_committed_sidecar(self, tmp_path: Path) -> None:
        """A committed ``net_class_map.json`` next to the PCB is used directly."""
        resolver = _load_resolver_module()
        out = tmp_path / "boards" / "demo" / "output"
        out.mkdir(parents=True)
        pcb = out / "demo_routed.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        sidecar = out / "net_class_map.json"
        sidecar.write_text("{}")

        with resolver.resolve_net_class_map_sidecar(pcb) as resolved:
            assert resolved is not None
            assert resolved.resolve() == sidecar.resolve()

    def test_in_process_fallback_when_no_sidecar(self, tmp_path: Path, monkeypatch) -> None:
        """No committed sidecar -> derive the map in-process to a temp file.

        The temp file must exist (and contain the derived map) inside the
        context, and be cleaned up on exit.
        """
        resolver = _load_resolver_module()
        out = tmp_path / "boards" / "demo" / "output"
        out.mkdir(parents=True)
        pcb = out / "demo_routed.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        from kicad_tools.router.rules import NetClassRouting

        derived = {"NET_A": NetClassRouting(name="DiffPair", coupled_routing=True)}
        monkeypatch.setattr(resolver, "build_net_class_map_for_board", lambda board_dir: derived)

        with resolver.resolve_net_class_map_sidecar(pcb) as resolved:
            assert resolved is not None
            assert resolved != (out / "net_class_map.json")
            assert resolved.is_file()
            data = json.loads(resolved.read_text())
            assert "NET_A" in data
            tmp_name = resolved

        # Cleaned up on exit.
        assert not tmp_name.exists()

    def test_none_when_no_map_available(self, tmp_path: Path, monkeypatch) -> None:
        """A board with no committed sidecar AND no derivable map yields None.

        This is the path that preserves the standalone-CLI no-op contract for
        external-router boards: the gate runs bare and the rules no-op.
        """
        resolver = _load_resolver_module()
        out = tmp_path / "boards" / "demo" / "output"
        out.mkdir(parents=True)
        pcb = out / "demo_routed.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        monkeypatch.setattr(resolver, "build_net_class_map_for_board", lambda board_dir: None)

        with resolver.resolve_net_class_map_sidecar(pcb) as resolved:
            assert resolved is None

    def test_build_failure_degrades_to_none(self, tmp_path: Path, monkeypatch) -> None:
        """If the board recipe raises while building the map, run bare (not crash)."""
        resolver = _load_resolver_module()
        out = tmp_path / "boards" / "demo" / "output"
        out.mkdir(parents=True)
        pcb = out / "demo_routed.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        def _boom(board_dir):
            raise RuntimeError("recipe import blew up")

        monkeypatch.setattr(resolver, "build_net_class_map_for_board", _boom)

        with resolver.resolve_net_class_map_sidecar(pcb) as resolved:
            assert resolved is None

    def test_board_07_resolves_committed_sidecar(self) -> None:
        """Board 07's real PCB resolves to its committed sidecar."""
        if not BOARD_07_PCB.is_file():
            pytest.skip("board 07 routed PCB not present")
        assert BOARD_07_SIDECAR.is_file(), "board 07 should ship a committed sidecar"
        resolver = _load_resolver_module()
        with resolver.resolve_net_class_map_sidecar(BOARD_07_PCB) as resolved:
            assert resolved is not None
            assert resolved.resolve() == BOARD_07_SIDECAR.resolve()

    def test_board_06_resolves_committed_sidecar(self) -> None:
        """Board 06's real PCB resolves to its committed sidecar.

        Issue #3828: board 06's ``generate_design.py`` now emits a
        ``net_class_map.json`` next to the routed PCB (the same sidecar its
        in-pipeline ``run_drc`` and the diff-pair coverage gate consume), so
        the diff-pair length-skew / routing-continuity rules are ACTIVE under
        ``kct check`` on both the committed artifact and every CI re-route.
        That sidecar is committed alongside the routed PCB, so -- exactly like
        board 07 -- the resolver yields the committed file directly rather than
        falling back to in-process derivation.  (The in-process derivation path
        is still covered generically by ``test_in_process_fallback_when_no_sidecar``.)
        """
        if not BOARD_06_PCB.is_file():
            pytest.skip("board 06 routed PCB not present")
        assert BOARD_06_SIDECAR.is_file(), "board 06 should ship a committed sidecar"
        resolver = _load_resolver_module()
        with resolver.resolve_net_class_map_sidecar(BOARD_06_PCB) as resolved:
            assert resolved is not None
            assert resolved.resolve() == BOARD_06_SIDECAR.resolve()
            data = json.loads(resolved.read_text())
            assert len(data) > 0, "board 06's committed map should be non-empty"


# ---------------------------------------------------------------------------
# Real kct check parity (integration / slow)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.slow
class TestBoard07KctCheckParity:
    """End-to-end parity on board 07's committed routed PCB (Issue #3151).

    Runs the real ``kct check`` engine both ways.  Slow (~30s/invocation) so
    it is gated behind ``integration``/``slow``; deselect with
    ``-m 'not slow'``.
    """

    @pytest.fixture(scope="class")
    def bare(self, tmp_path_factory: pytest.TempPathFactory) -> dict:
        """Run ``kct check`` with NO net-class map resolvable.

        Issue #3948 made ``kct check`` auto-discover a committed
        ``net_class_map.json`` next to the PCB, so running against the
        in-repo artifact is no longer "bare" -- the no-op contract this
        class pins is only observable when no sidecar is DISCOVERABLE.
        Use the shared ``isolate_pcb_from_sidecars`` helper (Issue #4009)
        to copy the routed PCB into an isolated tmp dir without the
        sidecar so the graceful-degradation path is actually exercised.
        """
        if not BOARD_07_PCB.is_file():
            pytest.skip("board 07 routed PCB not present")
        isolated = isolate_pcb_from_sidecars(
            BOARD_07_PCB, tmp_path_factory.mktemp("board07-bare")
        )
        return _run_kct_check(isolated, sidecar=None)

    @pytest.fixture(scope="class")
    def with_sidecar(self) -> dict:
        if not (BOARD_07_PCB.is_file() and BOARD_07_SIDECAR.is_file()):
            pytest.skip("board 07 routed PCB / sidecar not present")
        return _run_kct_check(BOARD_07_PCB, sidecar=BOARD_07_SIDECAR)

    def test_bare_check_noop_contract_preserved(self, bare: dict) -> None:
        """Sidecar-less ``kct check`` reports ZERO of the net-class-gated families.

        This is the graceful-degradation contract external-router boards
        rely on; it must NEVER regress to firing these rules without a map.
        (Since #3948 the map may be auto-discovered from a committed
        sidecar; the ``bare`` fixture isolates the PCB so no map resolves.)
        """
        counts = _family_counts(bare)
        for family in NET_CLASS_GATED_FAMILIES:
            assert counts.get(family, 0) == 0, (
                f"bare kct check fired {family!r} without a net_class_map -- "
                "the standalone no-op contract regressed (see Issue #3151)"
            )

    def test_sidecar_adds_exactly_the_three_families(self, bare: dict, with_sidecar: dict) -> None:
        """WITH-sidecar set == bare set PLUS exactly the three families.

        Pins the family-level delta so a silent-no-op regression in any one
        rule is caught (cf. #3098 / PR #3145).
        """
        bare_counts = _family_counts(bare)
        sidecar_counts = _family_counts(with_sidecar)

        # Every family present bare must be unchanged with the sidecar
        # (the sidecar only ADDS the gated families, it must not perturb the
        # clearance/connectivity families).
        for family, count in bare_counts.items():
            assert sidecar_counts.get(family, 0) == count, (
                f"sidecar perturbed an unrelated family {family!r}: "
                f"bare={count} sidecar={sidecar_counts.get(family, 0)}"
            )

        # The added families are EXACTLY the gated families that the
        # committed artifact still has errors for (Issue #3440: the
        # match-group tuner now brings board 07's groups within
        # tolerance, so match_group_length_skew is gated-but-clean --
        # its ENGAGEMENT is pinned by the matchgroup CI gate's
        # rules_checked_by_rule assertion, not by an error count here).
        added = {
            family
            for family in sidecar_counts
            if sidecar_counts[family] > bare_counts.get(family, 0)
        }
        expected_added = {
            family for family, expected in BOARD_07_EXPECTED_FAMILY_DELTA.items() if expected > 0
        }
        assert added == expected_added, (
            f"sidecar added {sorted(added)}; expected exactly {sorted(expected_added)}"
        )

    def test_family_delta_counts_pinned(self, bare: dict, with_sidecar: dict) -> None:
        """The per-family counts match the documented 18->27 delta."""
        bare_counts = _family_counts(bare)
        sidecar_counts = _family_counts(with_sidecar)
        for family, expected in BOARD_07_EXPECTED_FAMILY_DELTA.items():
            delta = sidecar_counts.get(family, 0) - bare_counts.get(family, 0)
            assert delta == expected, (
                f"{family!r} delta was {delta}, expected {expected} "
                "(re-baseline this test AND the tolerance floor together "
                "if the routed artifact legitimately changed)"
            )

    def test_total_error_count_delta(self, bare: dict, with_sidecar: dict) -> None:
        """Total error count rises by the sum of the three family deltas."""
        bare_total = bare["summary"]["errors"]
        sidecar_total = with_sidecar["summary"]["errors"]
        assert sidecar_total - bare_total == sum(BOARD_07_EXPECTED_FAMILY_DELTA.values())


@pytest.mark.integration
@pytest.mark.slow
class TestCiGateCountsGatedFamilies:
    """The strict CI gate now counts the gated families (Issue #3151 AC #1)."""

    def _load_gate(self):
        path = CI_DIR / "check_routed_drc.py"
        spec = importlib.util.spec_from_file_location("check_routed_drc_test_mod", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["check_routed_drc_test_mod"] = module
        spec.loader.exec_module(module)
        return module

    def test_board_07_gate_counts_diffpair_and_matchgroup(self) -> None:
        """``check_routed_drc.count_errors`` on board 07 includes the families.

        Before #3151 the gate ran bare and saw 11 blocking errors; now it
        resolves the sidecar and sees the gated-family errors too.

        Re-baselined 2026-06-13 (Issue #3617 / PR #3632, doctor pass 2) for
        the FILLED + 45-QUANTIZED committed artifact: 70 total - 1 advisory
        connectivity = 69 blocking.  Doctor pass 1 (straight-chord filled
        artifact) was 61 total / 60 blocking; pass 2 routes the pour-repair
        stubs/bridges through the #3532 quantizer so the committed artifact
        passes tests/test_fleet_45_census.py.  The +9 (+8 blocking) is the
        quantization doglegs grazing the 0.10mm-clearance DDR/MIPI/HDMI
        copper (+4 clearance_pad_segment, +3 clearance_segment_segment, +2
        clearance_segment_via); no new family, gated diff-pair/match-group
        families (15) and the connectivity advisory (1) unchanged.  See the
        board-07 entry in .github/routed-drc-tolerance.yml (note 4) for the
        per-family forensics and the #3633 interleave-fix exit clause.

        Earlier in this PR: the dead-pour artifact measured 19 total - 5
        advisory = 14 blocking; filling the pour completes more nets (honest
        diff-pair/match-group measurement) and physically connects the
        formerly-floating pads (advisory connectivity 5 -> 1).

        Previous re-baselines: 2026-06-13 pass 1 (Issue #3617: 60 blocking);
        2026-06-10 (Issue #3440: 14 blocking); 2026-06-09 (issue #3458
        inventory, PR #3462: 16); 2026-06-06 (Issue #3263: 17).
        """
        if not BOARD_07_PCB.is_file():
            pytest.skip("board 07 routed PCB not present")
        gate = self._load_gate()
        blocking, advisory = gate.count_errors(BOARD_07_PCB)
        # Re-baselined 2026-07-08 (fix/board07-gallery-ready fresh re-route):
        # the committed artifact is regenerated end-to-end on the post-#3919
        # pipeline (sibling .kicad_pro/.kicad_dru DRC-constraint sidecars,
        # sibling-stitch-net pad obstacles, edge-based repair-via drill
        # spacing).  kicad-cli pcb drc --refill-zones now reports ZERO
        # violations; kct check's remaining blocking errors are exactly the
        # sidecar-gated families: 4 diffpair_length_skew +
        # 4 diffpair_routing_continuity + 1 match_group_length_skew = 9.
        # Advisory connectivity is 5 (the DQ3/DQ4/MIPI_DAT0_N/TMDS_D0_N/
        # TMDS_D1_N #3438 negotiated-reach residual).  The tolerance floor in
        # .github/routed-drc-tolerance.yml tightens 35 -> 14 (raw count; the
        # match-group gate compares the UNFILTERED kct-check summary.errors,
        # 9 blocking + 5 advisory -- see the counter-semantics note there).
        assert blocking == 9, (
            f"expected 9 blocking errors (4+4+1 gated families) on the "
            f"2026-07-08 re-routed artifact, got {blocking}"
        )
        assert advisory.get("connectivity", 0) == 5
