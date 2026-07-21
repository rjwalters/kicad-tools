"""Divergence guard for the boards migrated to the shared success gate (#3912).

Issue #3912 finishes migrating every board recipe's ``main()`` onto the shared
:func:`kicad_tools.recipes.gate.evaluate_pipeline_gate` helper so that a
board's printed ``SUMMARY`` block and its process exit code are BOTH derived
from ONE :class:`~kicad_tools.recipes.gate.PipelineGateResult` and can never
diverge (the exact defect the issue names: board-06 printed ``DRC: FAIL`` while
exiting 0; board-05 dropped route-completion from its exit expression).

Boards 04/05/06 landed in earlier increments; this file locks in the final
five:

* ``boards/00-simple-led``
* ``boards/01-voltage-divider``
* ``boards/02-charlieplex-led``
* ``boards/03-usb-joystick``
* ``boards/07-matchgroup-test`` (PARTIAL by design -- #3438 known opens)

Two layers of guard:

1. **Structural** -- assert each migrated recipe drives BOTH the SUMMARY and
   the exit code from the shared gate (``gate.summary_lines()`` +
   ``gate.passed`` / ``gate.exit_code()``) and no longer hand-rolls the
   per-leg ``Routing:``/``DRC:``/``LVS:`` result f-strings or the misleading
   ``MFG bundle: PASS`` wording that used to conflate "bundle written" with
   "board DRC-clean".
2. **Behavioral** -- exercise :func:`evaluate_pipeline_gate` on the two
   representative recipe verdicts (a manufacturable-clean board like 00, and
   the PARTIAL-plus-residual-DRC board 07) and assert the SUMMARY's
   ``Overall:`` line agrees with :meth:`~PipelineGateResult.exit_code` -- the
   invariant that makes divergence impossible.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.drc.geometric import GeometricDRCResult
from kicad_tools.recipes.gate import evaluate_pipeline_gate

REPO_ROOT = Path(__file__).resolve().parent.parent

# (board dir, generate_design.py relative to the board dir).  All five use
# ``generate_design.py`` (board-05's recipe is ``design.py`` and landed in an
# earlier increment, so it is intentionally not in this list).
MIGRATED_BOARDS = [
    "boards/00-simple-led/generate_design.py",
    "boards/01-voltage-divider/generate_design.py",
    "boards/02-charlieplex-led/generate_design.py",
    "boards/03-usb-joystick/generate_design.py",
    "boards/07-matchgroup-test/generate_design.py",
]

# Hand-rolled per-leg result f-strings the migration REMOVES.  Their presence
# would mean a SUMMARY line is authored independently of the gate again -- the
# precise way exit-code and SUMMARY drift apart (#3912).
FORBIDDEN_HANDROLLED_SNIPPETS = [
    "if route_success else 'PARTIAL'",
    "if drc_success else 'FAIL'",
    "if drc_ok else 'FAIL",
    "if lvs_success else 'FAIL'",
    "bundle: {'PASS'",  # the "bundle written == board clean" conflation
]


def _clean_drc() -> GeometricDRCResult:
    return GeometricDRCResult(ran=True, error_count=0, by_type={})


@pytest.fixture(params=MIGRATED_BOARDS)
def board_source(request: pytest.FixtureRequest) -> str:
    path = REPO_ROOT / request.param
    assert path.is_file(), f"migrated recipe not found: {path}"
    return path.read_text()


# --------------------------------------------------------------------------
# Structural guard: the gate is the single source of truth
# --------------------------------------------------------------------------
class TestGateWiring:
    def test_imports_and_calls_shared_gate(self, board_source: str) -> None:
        assert "from kicad_tools.recipes.gate import evaluate_pipeline_gate" in board_source
        # Exactly one gate evaluation per recipe -- a second would reintroduce
        # the multi-verdict drift this issue removes.
        assert board_source.count("evaluate_pipeline_gate(") == 1

    def test_summary_derived_from_gate(self, board_source: str) -> None:
        assert "for line in gate.summary_lines():" in board_source

    def test_exit_code_derived_from_gate(self, board_source: str) -> None:
        assert ("gate.passed" in board_source) or ("gate.exit_code()" in board_source)

    def test_no_handrolled_result_lines(self, board_source: str) -> None:
        for snippet in FORBIDDEN_HANDROLLED_SNIPPETS:
            assert snippet not in board_source, (
                f"migrated recipe still hand-rolls a SUMMARY line ({snippet!r}); "
                "it must derive from gate.summary_lines() so the SUMMARY and the "
                "exit code cannot diverge (#3912)."
            )

    def test_bundle_wording_is_written_not_pass(self, board_source: str) -> None:
        # The manufacturing-bundle line must say WRITTEN/FAILED (bundle was
        # produced), never PASS -- "bundle written" is not "board DRC-clean".
        if "MFG bundle:" in board_source or "MFG:" in board_source:
            assert "'WRITTEN'" in board_source


# --------------------------------------------------------------------------
# Behavioral guard: SUMMARY Overall line agrees with the exit code
# --------------------------------------------------------------------------
class TestSummaryExitAgreement:
    def _overall_from_summary(self, result) -> str:  # noqa: ANN001
        overall = [ln for ln in result.summary_lines() if ln.strip().startswith("Overall:")]
        assert len(overall) == 1, result.summary_lines()
        return overall[0].split(":", 1)[1].strip()

    def test_clean_board_like_00_passes(self) -> None:
        """A manufacturable-clean board (route+DRC+LVS all clean) -> PASS/exit 0.

        Mirrors boards 00/01/02: ``route_ok`` True, geometric DRC clean, the
        recipe ``run_drc`` verdict (supplemental) clean, copper-LVS clean.
        """
        res = evaluate_pipeline_gate(
            Path("board00_routed.kicad_pcb"),
            route_ok=True,
            route_allowance=0,
            lvs_ok=True,
            supplemental_drc_ok=True,
            _drc_result=_clean_drc(),
        )
        assert res.passed is True
        assert res.exit_code() == 0
        assert self._overall_from_summary(res) == "PASS"

    def test_partial_board_like_07_fails_honestly(self) -> None:
        """Board 07 is PARTIAL by design + carries kct-check DRC residuals.

        The honest verdict is FAIL/exit 1: ``route_ok`` False (the ``Routing:``
        line must read PARTIAL, not a falsely-inflated SUCCESS), ``lvs_ok`` None
        (the 5 #3438 opens are advisory, so LVS must NOT gate -- otherwise the
        board is falsely failed for being exactly at its documented plateau),
        and the supplemental ``kct check`` verdict False.  Even though the
        authoritative geometric DRC leg is clean, the supplemental verdict
        tightens ``drc_ok`` to False.
        """
        res = evaluate_pipeline_gate(
            Path("matchgroup_test_routed.kicad_pcb"),
            route_ok=False,
            route_allowance=0,
            lvs_ok=None,
            supplemental_drc_ok=False,
            _drc_result=_clean_drc(),
        )
        assert res.route_ok is False
        assert res.drc_ok is False  # supplemental tightened it
        assert res.passed is False
        assert res.exit_code() == 1
        assert res.route_status() == "PARTIAL"
        assert res.drc_status() == "FAIL"
        assert res.lvs_status() == "n/a"  # advisory, not a gating leg
        assert self._overall_from_summary(res) == "FAIL"

    @pytest.mark.parametrize(
        ("route_ok", "supplemental_drc_ok", "lvs_ok"),
        [
            (True, True, True),
            (True, True, None),
            (True, False, True),
            (False, True, True),
            (True, True, False),
            (False, False, None),
        ],
    )
    def test_overall_line_always_matches_exit_code(
        self, route_ok: bool, supplemental_drc_ok: bool, lvs_ok: bool | None
    ) -> None:
        """The SUMMARY ``Overall:`` line can NEVER disagree with the exit code.

        This is the divergence-guard invariant #3912 requires of every migrated
        recipe: since both are derived from one ``PipelineGateResult``,
        ``Overall: PASS`` <=> ``exit_code() == 0`` for every leg combination.
        """
        res = evaluate_pipeline_gate(
            Path("dummy_routed.kicad_pcb"),
            route_ok=route_ok,
            route_allowance=0,
            lvs_ok=lvs_ok,
            supplemental_drc_ok=supplemental_drc_ok,
            _drc_result=_clean_drc(),
        )
        overall = self._overall_from_summary(res)
        assert (overall == "PASS") == (res.exit_code() == 0)
        assert (overall == "PASS") == res.passed
