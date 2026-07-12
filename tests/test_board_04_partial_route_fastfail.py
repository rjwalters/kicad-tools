"""Partial-route fast-fail gate for board 04 (stm32-devboard) — issue #4066.

Sibling of ``tests/test_board_03_copper_lvs.py::TestBoard03PartialRouteFastFail``.
Under concurrent CPU load the wall-clock ``--timeout`` safety backstop can fire
before every signal net lands, so ``route_pcb`` returns ``False``.  Without the
#4066 gate ``main()`` fell through five copper-mutating steps
(``fix_osc_escape`` -> ``stitch_pcb`` -> ``tie_power_pads`` ->
``quantize_escapes`` -> ``fill_zones``) and then
``write_lvs_report(require_clean=True)``, which raised ``BoardNetlistMismatch``
on the unrouted net's copper OPEN and surfaced as a misleading LVS failure.

The gate must land immediately after ``route_pcb`` and before all five of
those steps, so the forbid-list below is the widest of the five boards.  Note:
board 04's final exit gate also ANDs ``route_success`` (#3839) as
defense-in-depth; the early raise makes that term unreachable on the
partial-route path but the tests here exercise the early gate.

Fast and hermetic: monkeypatch the recipe's module-level functions so
``main()`` runs without the router, ``kicad-cli``, or the LVS comparator.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "04-stm32-devboard"


def _load_board04_module():
    """Import the board-04 ``generate_design.py`` recipe module."""
    gen = BOARD_DIR / "generate_design.py"
    spec = importlib.util.spec_from_file_location("board04_generate_design", gen)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestBoard04PartialRouteFastFail:
    """A partial route fails fast with a distinct message, not an LVS trace."""

    def _stub_pipeline_prefix(self, module, monkeypatch, tmp_path: Path) -> None:
        sch = tmp_path / "stm32_devboard.kicad_sch"
        sch.write_text("(kicad_sch)")
        pcb = tmp_path / "stm32_devboard.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        monkeypatch.setattr(module, "create_project", lambda *a, **k: tmp_path / "p.kicad_pro")
        monkeypatch.setattr(module, "create_stm32_schematic", lambda *a, **k: sch)
        monkeypatch.setattr(module, "run_erc", lambda *a, **k: True)
        monkeypatch.setattr(module, "create_stm32_pcb", lambda *a, **k: pcb)

    def _forbid_downstream(self, module, monkeypatch) -> None:
        def _boom(name):
            def _raise(*a, **k):
                raise AssertionError(
                    f"{name} ran despite a partial route -- the route_success "
                    "gate (#4066) did not short-circuit the pipeline"
                )

            return _raise

        # The widest forbid-list of the five boards: the gate must sit BEFORE
        # every one of these five copper-mutating steps, not just before DRC.
        monkeypatch.setattr(module, "fix_osc_escape", _boom("fix_osc_escape"))
        monkeypatch.setattr(module, "stitch_pcb", _boom("stitch_pcb"))
        monkeypatch.setattr(module, "tie_power_pads", _boom("tie_power_pads"))
        monkeypatch.setattr(module, "quantize_escapes", _boom("quantize_escapes"))
        monkeypatch.setattr(module, "fill_zones", _boom("fill_zones"))
        monkeypatch.setattr(module, "run_drc", _boom("run_drc"))
        monkeypatch.setattr(module, "write_lvs_report", _boom("write_lvs_report"))
        monkeypatch.setattr(module, "generate_manufacturing", _boom("generate_manufacturing"))

    def test_partial_route_fails_fast_with_distinct_message(
        self, monkeypatch, capsys, tmp_path: Path
    ) -> None:
        module = _load_board04_module()
        self._stub_pipeline_prefix(module, monkeypatch, tmp_path)
        self._forbid_downstream(module, monkeypatch)
        monkeypatch.setattr(module, "route_pcb", lambda *a, **k: False)
        monkeypatch.setattr(module.sys, "argv", ["generate_design.py", str(tmp_path / "out")])

        rc = module.main()

        assert rc == 1, "partial route must make main() exit non-zero"
        err = capsys.readouterr().err
        assert "partial route" in err.lower(), (
            "partial-route failure must be reported with a distinct 'partial "
            f"route' message, got stderr:\n{err}"
        )
        assert "wall-clock budget" in err.lower()
        assert "BoardNetlistMismatch" not in err, (
            "a partial route must NOT surface as an LVS BoardNetlistMismatch"
        )

    def test_full_route_still_reaches_lvs_gate(self, monkeypatch, tmp_path: Path) -> None:
        module = _load_board04_module()
        self._stub_pipeline_prefix(module, monkeypatch, tmp_path)
        monkeypatch.setattr(module, "route_pcb", lambda *a, **k: True)
        monkeypatch.setattr(module, "fix_osc_escape", lambda *a, **k: True)
        monkeypatch.setattr(module, "stitch_pcb", lambda *a, **k: True)
        monkeypatch.setattr(module, "tie_power_pads", lambda *a, **k: True)
        monkeypatch.setattr(module, "quantize_escapes", lambda *a, **k: True)
        monkeypatch.setattr(module, "fill_zones", lambda *a, **k: True)
        monkeypatch.setattr(module, "run_drc", lambda *a, **k: True)
        monkeypatch.setattr(module, "generate_manufacturing", lambda *a, **k: True)

        lvs_called: list[bool] = []

        def _fake_lvs(*a, **k):
            lvs_called.append(True)
            return (True, True)

        monkeypatch.setattr(module, "write_lvs_report", _fake_lvs)
        monkeypatch.setattr(module.sys, "argv", ["generate_design.py", str(tmp_path / "out")])

        rc = module.main()

        assert lvs_called == [True], (
            "a full (N==M) route must still reach write_lvs_report -- the "
            "#4066 fast-fail gate must not fire on a complete route"
        )
        assert rc == 0
