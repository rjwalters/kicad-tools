"""Partial-route fast-fail gate for board 00 (simple-led) — issue #4066.

Sibling of ``tests/test_board_03_copper_lvs.py::TestBoard03PartialRouteFastFail``.
``route_pcb`` returns ``False`` when at least one trace-routable SIGNAL net
fails to land (``nets_routed`` < the pour-net-excluded ``total_nets``).  Without
the #4066 gate ``main()`` fell through to ``run_lvs`` ->
``write_lvs_report(require_clean=True)``, which raised ``BoardNetlistMismatch``
on the unrouted net's copper OPEN and surfaced as a misleading LVS failure.

``route_pcb``'s success computation must EXCLUDE pour nets (VCC is
``is_pour_net=True`` and is connected via a copper zone, not a trace; GND is
already collapsed to net 0 by ``load_pcb_for_routing(skip_nets=["GND"])``).
The router logs "Skipping N pour net(s)" for exactly these nets.  Counting VCC
in ``total_nets`` made ``success`` always ``False`` on a clean run — a latent
false-negative that turned into a deterministic ``RuntimeError`` once the #4066
gate landed (that is the board-00 blocker this PR revision fixes).

These tests are fast and hermetic: they monkeypatch the recipe's own
module-level functions (and, for the count coverage, the router factory) so
``main()`` / ``route_pcb`` run without invoking the real router, ``kicad-cli``,
or the LVS comparator.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

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
        assert "signal net" in err.lower(), (
            "the partial-route message must name the unrouted SIGNAL net as "
            f"the cause, got stderr:\n{err}"
        )
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


class _FakeRouter:
    """Minimal stand-in for ``Autorouter`` covering ``route_pcb``'s surface.

    Board 00 loads a router whose ``nets`` are VCC (net 1, a pour net) and
    LED_ANODE (net 2, a signal net); GND is collapsed to net 0 by
    ``skip_nets=["GND"]`` and so never appears as a key.  ``nets_routed`` is
    the number of TRACE-routed signal nets (VCC is filled as a zone, never
    traced).  ``_is_pour_net`` mirrors the real classifier: only VCC is a pour
    net here.
    """

    _POUR_NETS = {1}  # VCC

    def __init__(self, nets_routed: int) -> None:
        self.nets = {1: [("R1", "1")], 2: [("R1", "2"), ("D1", "2")]}
        self.routes: list = []
        self.grid = SimpleNamespace(width=20.0, height=20.0)
        self._nets_routed = nets_routed

    def _is_pour_net(self, net_id: int) -> bool:
        return net_id in self._POUR_NETS

    def route_all(self) -> None:  # noqa: D401 - stub
        pass

    def get_statistics(self) -> dict:
        return {
            "routes": self._nets_routed,
            "segments": self._nets_routed,
            "vias": 0,
            "total_length_mm": 1.0,
            "nets_routed": self._nets_routed,
        }

    def to_sexp(self) -> str:
        return ""


class TestBoard00RoutePcbNetCount:
    """``route_pcb`` excludes pour nets from ``total_nets`` (the #4066 fix).

    Before the fix ``total_nets`` counted VCC (a pour net), so ``success`` was
    ``stats['nets_routed'] (1) == total_nets (2)`` -> always ``False`` on a
    clean run.  These tests pin that the pour net is excluded, so a clean run
    (all SIGNAL nets routed) returns ``True`` and a genuinely unrouted signal
    net returns ``False``.
    """

    def _stub_route_pcb_deps(self, module, monkeypatch, nets_routed: int):
        import kicad_tools.cli.route_cmd as route_cmd
        import kicad_tools.router as router_pkg
        import kicad_tools.router.auto_pour as auto_pour

        fake = _FakeRouter(nets_routed)

        monkeypatch.setattr(
            router_pkg, "load_pcb_for_routing", lambda *a, **k: (fake, {"VCC": 1, "LED_ANODE": 2})
        )
        # OptimizationConfig / TraceOptimizer are imported inside route_pcb from
        # kicad_tools.router.optimizer; keep the real ones but make the
        # optimizer a no-op over the (empty) route list.
        import kicad_tools.router.optimizer as optimizer_mod

        class _NoopOptimizer:
            def __init__(self, *a, **k) -> None:
                pass

            def optimize_route(self, route):
                return route

        monkeypatch.setattr(optimizer_mod, "TraceOptimizer", _NoopOptimizer)
        # Downstream file-mutating helpers are irrelevant to the count logic.
        monkeypatch.setattr(module, "_rewrite_led_anode_route", lambda *a, **k: None)
        monkeypatch.setattr(auto_pour, "auto_pour_if_missing", lambda *a, **k: (2, ["VCC", "GND"]))
        monkeypatch.setattr(route_cmd, "_fill_zones_after_route", lambda *a, **k: None)
        return fake

    def test_clean_run_excludes_pour_net_and_returns_true(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        module = _load_board00_module()
        inp = tmp_path / "in.kicad_pcb"
        inp.write_text("(kicad_pcb)")
        out = tmp_path / "out.kicad_pcb"

        # Only the single SIGNAL net (LED_ANODE) is trace-routed; VCC is a pour.
        self._stub_route_pcb_deps(module, monkeypatch, nets_routed=1)

        assert module.route_pcb(inp, out) is True, (
            "a clean run (all trace-routable signal nets routed) must return "
            "True -- VCC (a pour net) must be excluded from total_nets"
        )

    def test_unrouted_signal_net_returns_false(self, monkeypatch, tmp_path: Path) -> None:
        module = _load_board00_module()
        inp = tmp_path / "in.kicad_pcb"
        inp.write_text("(kicad_pcb)")
        out = tmp_path / "out.kicad_pcb"

        # The signal net failed to land: 0 trace-routed vs 1 routable signal net.
        self._stub_route_pcb_deps(module, monkeypatch, nets_routed=0)

        assert module.route_pcb(inp, out) is False, (
            "a genuinely unrouted signal net must return False so the #4066 gate fires"
        )
