"""Partial-route fast-fail gate for board 00 (simple-led) — issue #4066.

Sibling of ``tests/test_board_03_copper_lvs.py::TestBoard03PartialRouteFastFail``.
``route_pcb`` runs under a wall-clock ``--timeout`` SAFETY backstop layered
above the load-independent per-net ``--deterministic-budget`` iteration cap;
under concurrent CPU load that outer deadline can fire before every signal net
lands, so ``route_pcb`` returns ``False``.  Without the #4066 gate ``main()``
fell through to ``run_lvs`` -> ``write_lvs_report(require_clean=True)``, which
raised ``BoardNetlistMismatch`` on the unrouted net's copper OPEN and surfaced
as a misleading LVS failure.

These tests are fast and hermetic: they monkeypatch the recipe's own
module-level functions so ``main()`` runs without invoking the router,
``kicad-cli``, or the LVS comparator.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "00-simple-led"


def _load_board00_module():
    """Import the board-00 ``generate_design.py`` recipe module."""
    gen = BOARD_DIR / "generate_design.py"
    spec = importlib.util.spec_from_file_location("board00_generate_design", gen)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestBoard00PartialRouteFastFail:
    """A partial route fails fast with a distinct message, not an LVS trace."""

    def _stub_pipeline_prefix(self, module, monkeypatch, tmp_path: Path) -> None:
        sch = tmp_path / "simple_led.kicad_sch"
        sch.write_text("(kicad_sch)")
        pcb = tmp_path / "simple_led.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        monkeypatch.setattr(module, "create_project", lambda *a, **k: tmp_path / "p.kicad_pro")
        monkeypatch.setattr(module, "create_led_schematic", lambda *a, **k: sch)
        monkeypatch.setattr(module, "run_erc", lambda *a, **k: True)
        monkeypatch.setattr(module, "create_led_pcb", lambda *a, **k: pcb)

    def _forbid_downstream(self, module, monkeypatch) -> None:
        def _boom(name):
            def _raise(*a, **k):
                raise AssertionError(
                    f"{name} ran despite a partial route -- the route_success "
                    "gate (#4066) did not short-circuit the pipeline"
                )

            return _raise

        monkeypatch.setattr(module, "run_drc", _boom("run_drc"))
        monkeypatch.setattr(module, "run_lvs", _boom("run_lvs"))
        monkeypatch.setattr(
            module, "export_manufacturing_bundle", _boom("export_manufacturing_bundle")
        )

    def test_partial_route_fails_fast_with_distinct_message(
        self, monkeypatch, capsys, tmp_path: Path
    ) -> None:
        module = _load_board00_module()
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
        module = _load_board00_module()
        self._stub_pipeline_prefix(module, monkeypatch, tmp_path)
        monkeypatch.setattr(module, "route_pcb", lambda *a, **k: True)
        monkeypatch.setattr(module, "run_drc", lambda *a, **k: True)

        lvs_called: list[bool] = []

        def _fake_lvs(*a, **k):
            lvs_called.append(True)
            return True

        monkeypatch.setattr(module, "run_lvs", _fake_lvs)
        monkeypatch.setattr(module, "export_manufacturing_bundle", lambda *a, **k: True)
        monkeypatch.setattr(module.sys, "argv", ["generate_design.py", str(tmp_path / "out")])

        rc = module.main()

        assert lvs_called == [True], (
            "a full (N==M) route must still reach the LVS gate -- the #4066 "
            "fast-fail gate must not fire on a complete route"
        )
        assert rc == 0
