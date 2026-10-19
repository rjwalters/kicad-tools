"""``kct route --length-match-diffpairs`` on the default ``--auto-layers`` path.

Issue #7377 (step 1): ``kct route`` had four copy-pasted post-route tails and
only the single-attempt tail of ``_run_main_impl`` called
``_run_diffpair_skew_tuning``.  ``--auto-layers`` is on by default, so a plain
``kct route --differential-pairs --length-match-diffpairs`` went through
``route_with_layer_escalation`` and the flag was accepted and then silently did
nothing.  All four tails now share ``_run_post_route_pipeline``.

Modelled on ``TestDefaultPathTunes`` in ``tests/test_cli_route_match_groups.py``
(the #6968 fix of the same bug for ``--length-match-groups``).
"""

from __future__ import annotations

import ast
import inspect
import json

_HEADER = "--- Length-Match Diff-Pairs (Epic #2556 Phase 3I) ---"


def _skewed_pair_board(tmp_path):
    """U1 -> U2 carrying one ``SIG_P`` / ``SIG_N`` pair with a few mm of skew."""
    from tests.test_auto_pour import _PCB_FOOTER, _PCB_HEADER

    def fp(ref: str, x: float, y: float, pads: list[tuple[float, float, int, str]]) -> str:
        out = (
            f'  (footprint "TestLib:TestPkg" (layer "F.Cu") (at {x} {y})\n'
            f'    (property "Reference" "{ref}" (at 0 -2) (layer "F.SilkS"))\n'
        )
        for i, (dx, dy, nid, name) in enumerate(pads):
            out += (
                f'    (pad "{i + 1}" smd roundrect (at {dx} {dy}) (size 1.0 1.0) '
                f'(layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) '
                f'(net {nid} "{name}"))\n'
            )
        return out + "  )\n"

    pcb = _PCB_HEADER + '  (net 0 "")\n  (net 1 "SIG_P")\n  (net 2 "SIG_N")\n'
    pcb += fp("U1", 10, 10, [(0, 0, 1, "SIG_P"), (0, 3, 2, "SIG_N")])
    pcb += fp("U2", 35, 10, [(0, 0, 1, "SIG_P"), (0, 15, 2, "SIG_N")])
    pcb += _PCB_FOOTER
    pcb_path = tmp_path / "pair.kicad_pcb"
    pcb_path.write_text(pcb)
    nc = {"name": "PAIR", "length_critical": True}
    ncm_path = tmp_path / "ncm.json"
    ncm_path.write_text(json.dumps({"SIG_P": nc, "SIG_N": nc}))
    return pcb_path, ncm_path


def _route(tmp_path, pcb_path, ncm_path, *extra: str) -> int:
    from kicad_tools.cli.route_cmd import main as route_main

    return route_main(
        [
            str(pcb_path),
            "-o",
            str(tmp_path / "out.kicad_pcb"),
            "--differential-pairs",
            "--length-match-diffpairs",
            "--net-class-map",
            str(ncm_path),
            "--skip-drc",
            *extra,
        ]
    )


class TestDefaultPathRunsSkewTuner:
    """``--length-match-diffpairs`` with no layer flags goes through
    ``route_with_layer_escalation``; before #7377 that path never called the
    diff-pair skew tuner and printed no ``Length-Match Diff-Pairs`` section."""

    def test_default_auto_layers_path_runs_skew_tuner(self, tmp_path, capsys):
        pcb_path, ncm_path = _skewed_pair_board(tmp_path)
        # Drive the real default path (no --no-auto-layers).
        assert _route(tmp_path, pcb_path, ncm_path) == 0
        out = capsys.readouterr().out
        assert "LAYER ESCALATION SUMMARY" in out, "default path must be layer escalation"
        assert out.count(_HEADER) == 1, out[-4000:]
        assert "SIG_P/SIG_N: tuned" in out
        assert "Summary: 1 tuned" in out

    def test_single_attempt_path_runs_skew_tuner_exactly_once(self, tmp_path, capsys):
        pcb_path, ncm_path = _skewed_pair_board(tmp_path)
        assert _route(tmp_path, pcb_path, ncm_path, "--no-auto-layers") == 0
        out = capsys.readouterr().out
        assert "LAYER ESCALATION SUMMARY" not in out
        assert out.count(_HEADER) == 1, out[-4000:]
        assert "Summary: 1 tuned" in out


def _calls_in(func_name: str) -> set[str]:
    from kicad_tools.cli import route_cmd

    tree = ast.parse(inspect.getsource(getattr(route_cmd, func_name)))
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


class TestOnePostRoutePipeline:
    """Structural guard: new post-route passes are wired once (Issue #7377)."""

    TAILS = (
        "route_with_layer_escalation",
        "route_with_rule_relaxation",
        "route_with_combined_escalation",
        "_run_main_impl",
    )
    PIPELINE_ONLY = (
        "_run_diffpair_gap_tightening_phase",
        "_run_diffpair_skew_tuning",
        "_run_match_group_tuning_phase",
        "_finalize_committed_copper_or_demote",
        "_run_consolidation_pass",
        "_optimize_traces",
    )

    def test_every_tail_calls_the_pipeline(self):
        for tail in self.TAILS:
            assert "_run_post_route_pipeline" in _calls_in(tail), tail

    def test_tails_do_not_call_pipeline_stages_directly(self):
        for tail in self.TAILS:
            leaked = _calls_in(tail) & set(self.PIPELINE_ONLY)
            assert not leaked, f"{tail} calls {sorted(leaked)} outside the pipeline"

    def test_pipeline_runs_all_tuners_in_order(self):
        from kicad_tools.cli import route_cmd

        src = inspect.getsource(route_cmd._run_post_route_pipeline)
        gap = src.index("_run_diffpair_gap_tightening_phase(router")
        skew = src.index("_run_diffpair_skew_tuning(router")
        group = src.index("_run_match_group_tuning_phase(router")
        assert gap < skew < group
