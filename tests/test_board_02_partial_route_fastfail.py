"""Partial-route fast-fail gate for board 02 (charlieplex-led) — issue #4066.

Sibling of ``tests/test_board_03_copper_lvs.py::TestBoard03PartialRouteFastFail``.
Under concurrent CPU load the wall-clock ``--timeout`` safety backstop can fire
before every signal net lands, so ``route_pcb`` returns ``False``.  Without the
#4066 gate ``main()`` fell through to ``write_lvs_report(require_clean=True)``,
which raised ``BoardNetlistMismatch`` on the unrouted net's copper OPEN and
surfaced as a misleading LVS failure.

Fast and hermetic: monkeypatch the recipe's module-level functions so
``main()`` runs without the router, ``kicad-cli``, or the LVS comparator.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from kicad_tools.drc.geometric import GeometricDRCResult
from kicad_tools.recipes import gate as pipeline_gate

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "02-charlieplex-led"


def _load_board02_module():
    """Import the board-02 ``generate_design.py`` recipe module.

    Board 02's recipe does a top-level ``from design_spec import ...`` of a
    sibling module that only resolves with the board dir on ``sys.path``, so
    prepend it for the duration of the exec.
    """
    gen = BOARD_DIR / "generate_design.py"
    spec = importlib.util.spec_from_file_location("board02_generate_design", gen)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    board_dir = str(BOARD_DIR)
    inserted = board_dir not in sys.path
    if inserted:
        sys.path.insert(0, board_dir)
    try:
        spec.loader.exec_module(module)
    finally:
        if inserted and sys.path and sys.path[0] == board_dir:
            sys.path.pop(0)
    return module


class TestBoard02PartialRouteFastFail:
    """A partial route fails fast with a distinct message, not an LVS trace."""

    def _stub_pipeline_prefix(self, module, monkeypatch, tmp_path: Path) -> None:
        sch = tmp_path / "charlieplex_3x3.kicad_sch"
        sch.write_text("(kicad_sch)")
        pcb = tmp_path / "charlieplex_3x3.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        monkeypatch.setattr(module, "create_project", lambda *a, **k: tmp_path / "p.kicad_pro")
        monkeypatch.setattr(module, "create_charlieplex_schematic", lambda *a, **k: sch)
        monkeypatch.setattr(module, "run_erc", lambda *a, **k: True)
        monkeypatch.setattr(module, "create_charlieplex_pcb", lambda *a, **k: pcb)

    def _forbid_downstream(self, module, monkeypatch) -> None:
        def _boom(name):
            def _raise(*a, **k):
                raise AssertionError(
                    f"{name} ran despite a partial route -- the route_success "
                    "gate (#4066) did not short-circuit the pipeline"
                )

            return _raise

        monkeypatch.setattr(module, "run_drc", _boom("run_drc"))
        monkeypatch.setattr(module, "write_lvs_report", _boom("write_lvs_report"))
        monkeypatch.setattr(
            module, "export_manufacturing_bundle", _boom("export_manufacturing_bundle")
        )

    def test_partial_route_fails_fast_with_distinct_message(
        self, monkeypatch, capsys, tmp_path: Path
    ) -> None:
        module = _load_board02_module()
        self._stub_pipeline_prefix(module, monkeypatch, tmp_path)
        self._forbid_downstream(module, monkeypatch)
        monkeypatch.setattr(module, "route_pcb", lambda *a, **k: False)
        monkeypatch.setattr(module.sys, "argv", ["generate_design.py", str(tmp_path / "out")])

        rc = module.main()

        assert rc == 1, "partial route must make main() exit non-zero"
        err = capsys.readouterr().err
        assert "routing/finalization failed" in err.lower()
        assert "has not qualified for manufacturing export" in err.lower()
        assert "wall-clock budget" not in err.lower()
        assert "BoardNetlistMismatch" not in err, (
            "a partial route must NOT surface as an LVS BoardNetlistMismatch"
        )

    def test_full_route_still_reaches_lvs_gate(self, monkeypatch, tmp_path: Path) -> None:
        # Issue #6724: route_pcb is stubbed, so the routed board is never
        # written and a real kicad-cli cannot load it.  That load failure
        # used to read as a clean native DRC; stub the leg as clean instead.
        from kicad_tools.drc.geometric import SavedFillCheck

        monkeypatch.setattr(
            "kicad_tools.recipes.gate.run_geometric_drc",
            lambda *a, **k: GeometricDRCResult(ran=True),
        )
        monkeypatch.setattr(
            "kicad_tools.recipes.gate.check_saved_fill", lambda *a, **k: SavedFillCheck(ran=True)
        )
        module = _load_board02_module()
        self._stub_pipeline_prefix(module, monkeypatch, tmp_path)
        monkeypatch.setattr(module, "route_pcb", lambda *a, **k: True)
        monkeypatch.setattr(module, "run_drc", lambda *a, **k: True)
        monkeypatch.setattr(
            pipeline_gate,
            "run_geometric_drc",
            lambda *a, **k: GeometricDRCResult(ran=True),
        )

        lvs_called: list[bool] = []

        def _fake_lvs(*a, **k):
            lvs_called.append(True)
            return (True, True)

        monkeypatch.setattr(module, "write_lvs_report", _fake_lvs)
        monkeypatch.setattr(module, "export_manufacturing_bundle", lambda *a, **k: True)
        monkeypatch.setattr(module.sys, "argv", ["generate_design.py", str(tmp_path / "out")])

        rc = module.main()

        assert lvs_called == [True], (
            "a full (N==M) route must still reach write_lvs_report -- the "
            "#4066 fast-fail gate must not fire on a complete route"
        )
        assert rc == 0


class TestBoard02RouteExitGate:
    """Issue #6427: exit 0 is the only route success; finalize never repairs exit 3.

    ``kct route`` exits 3 when either DRC engine flags the router's own copper.
    Before #6427 the recipe printed "PARTIAL" and handed that board to
    ``finalize_routing``, whose report-driven via repair moved the offending
    via until DRC passed -- hiding the router defect.
    """

    def _fake_route(self, module, monkeypatch, returncode: int, finalize_calls: list):
        import types

        def _run(cmd, *a, **k):
            output = Path(cmd[cmd.index("--output") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("(kicad_pcb)")
            return types.SimpleNamespace(returncode=returncode)

        monkeypatch.setattr(module, "subprocess", types.SimpleNamespace(run=_run))
        fake_finalize = types.ModuleType("finalize_routing")

        def _finalize(*a, **k):
            finalize_calls.append(a)
            return True

        fake_finalize.finalize_routing = _finalize
        monkeypatch.setitem(sys.modules, "finalize_routing", fake_finalize)

    def test_drc_exit_fails_route_before_finalize(self, monkeypatch, capsys, tmp_path: Path):
        module = _load_board02_module()
        finalize_calls: list = []
        self._fake_route(module, monkeypatch, 3, finalize_calls)

        ok = module.route_pcb(tmp_path / "in.kicad_pcb", tmp_path / "out_routed.kicad_pcb")

        assert ok is False
        assert finalize_calls == [], "exit 3 must never reach finalize's via repair"
        err = capsys.readouterr().err
        assert "exited with code 3" in err

    def test_clean_route_still_finalizes(self, monkeypatch, tmp_path: Path):
        module = _load_board02_module()
        finalize_calls: list = []
        self._fake_route(module, monkeypatch, 0, finalize_calls)

        assert module.route_pcb(tmp_path / "in.kicad_pcb", tmp_path / "out_routed.kicad_pcb")
        assert len(finalize_calls) == 1

    def test_main_exits_nonzero_when_route_exits_3(self, monkeypatch, capsys, tmp_path: Path):
        module = _load_board02_module()
        TestBoard02PartialRouteFastFail()._stub_pipeline_prefix(module, monkeypatch, tmp_path)
        TestBoard02PartialRouteFastFail()._forbid_downstream(module, monkeypatch)
        finalize_calls: list = []
        self._fake_route(module, monkeypatch, 3, finalize_calls)
        monkeypatch.setattr(module.sys, "argv", ["generate_design.py", str(tmp_path / "out")])

        rc = module.main()

        assert rc != 0
        assert finalize_calls == []
        assert "routing/finalization failed" in capsys.readouterr().err.lower()
