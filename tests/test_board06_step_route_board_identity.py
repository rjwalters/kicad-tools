"""``--step route`` must say so when it is handed a board board06 didn't make.

Issue #5628.  ``generate_design.py --step route <dir>`` routes whatever
``<dir>/diffpair_test.kicad_pcb`` the caller staged.  Step 9's zone plan is a
fixed five-zone assignment over ``POUR_NETS`` wrapped in one
``except Exception``, so a single missing pour net silently drops ALL FIVE
recipe pours -- and every downstream line (``kct zones fill``, the
``UNREPAIRED:`` list, ``POUR CONNECTIVITY``) then describes an un-poured
board while looking exactly like a router/pour-repair regression.

That is not hypothetical: #5628 was filed as a 6x ``UNREPAIRED: +3V3: cannot
reconnect component ['U3.1']`` "regression" and bisected across #5619/#5620
before the real variable turned out to be the INPUT -- a scratch dir seeded
from ``boards/06-diffpair-test/output/``, which has carried a different
design (no ``VBUS_USB``) since ``d95b6eff`` (2026-09-10); cf. #5607.

These tests pin the check itself, not any committed artifact's current
contents, so refreshing ``output/`` from the recipe (one disposition of
#5607) cannot make them stale.  The one artifact assertion is on
``regression-fixture/`` -- the frozen unrouted PCB CI re-routes -- which must
always satisfy the recipe's plane-net contract.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "06-diffpair-test"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - import guard
        raise ImportError(f"Cannot load module {name!r} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generate_design_mod():
    """Load ``boards/06-diffpair-test/generate_design.py`` as a module."""
    gp = _load_module("board_06_generate_pcb_ident", BOARD_DIR / "generate_pcb.py")
    sys.modules["generate_pcb"] = gp
    gs = _load_module("board_06_generate_schematic_ident", BOARD_DIR / "generate_schematic.py")
    sys.modules["generate_schematic"] = gs
    return _load_module("board_06_generate_design_ident", BOARD_DIR / "generate_design.py")


def _write_pcb(path: Path, nets: list[str]) -> Path:
    """A minimal ``.kicad_pcb`` declaring exactly *nets* (plus net 0)."""
    body = "\n".join(f'  (net {i} "{name}")' for i, name in enumerate(nets, start=1))
    path.write_text('(kicad_pcb\n  (net 0 "")\n' + body + "\n)\n")
    return path


def test_recipe_board_reports_no_missing_pour_nets(generate_design_mod, tmp_path):
    """A board carrying every ``POUR_NETS`` entry is accepted silently."""
    mod = generate_design_mod
    pcb = _write_pcb(tmp_path / "ok.kicad_pcb", [*mod.POUR_NETS, "USB2_D+", "USB2_D-"])

    assert mod._recipe_board_pour_nets_missing(pcb) == []
    assert mod.check_route_input_is_recipe_board(pcb) == []


def test_foreign_board_is_named_with_its_missing_pour_nets(
    generate_design_mod, tmp_path, capsys, monkeypatch
):
    """The #5628 shape: a board missing one pour net is reported, not ignored.

    ``VBUS_USB`` is the net the committed ``output/`` design actually drops,
    and dropping it is what makes ``ZoneGenerator`` raise -- taking all five
    zones down with it inside step 9's single ``except Exception``.
    """
    mod = generate_design_mod
    monkeypatch.delenv("KCT_BOARD06_REQUIRE_RECIPE_BOARD", raising=False)
    kept = [net for net in mod.POUR_NETS if net != "VBUS_USB"]
    pcb = _write_pcb(tmp_path / "foreign.kicad_pcb", [*kept, "LVDS1_P", "LVDS1_N"])

    assert mod._recipe_board_pour_nets_missing(pcb) == ["VBUS_USB"]
    assert mod.check_route_input_is_recipe_board(pcb) == ["VBUS_USB"]

    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "VBUS_USB" in out
    # The banner must carry the attribution, so a future reader of the log
    # cannot repeat #5628's misdiagnosis: these UNREPAIRED lines are not
    # comparable to the recipe's baselines.
    assert "UNREPAIRED" in out
    assert "5628" in out
    assert "--step all" in out
    assert "regression-fixture" in out


def test_hard_gate_env_var_upgrades_the_warning_to_a_failure(
    generate_design_mod, tmp_path, monkeypatch
):
    """``KCT_BOARD06_REQUIRE_RECIPE_BOARD=1`` is the L2 gate (cf. KCT_REQUIRE_SPEC)."""
    mod = generate_design_mod
    monkeypatch.setenv("KCT_BOARD06_REQUIRE_RECIPE_BOARD", "1")
    kept = [net for net in mod.POUR_NETS if net != "VBUS_USB"]
    pcb = _write_pcb(tmp_path / "foreign.kicad_pcb", kept)

    with pytest.raises(RuntimeError, match="NOT the board this recipe generates"):
        mod.check_route_input_is_recipe_board(pcb)


def test_hard_gate_stays_quiet_on_the_recipe_board(generate_design_mod, tmp_path, monkeypatch):
    """The L2 gate must not fire on a conforming board (no false positives)."""
    mod = generate_design_mod
    monkeypatch.setenv("KCT_BOARD06_REQUIRE_RECIPE_BOARD", "1")
    pcb = _write_pcb(tmp_path / "ok.kicad_pcb", list(mod.POUR_NETS))

    assert mod.check_route_input_is_recipe_board(pcb) == []


def test_committed_regression_fixture_satisfies_the_plane_net_contract(generate_design_mod):
    """CI's ``--step route`` input must never trip the check.

    ``scripts/ci/check_diffpair_coverage.py`` seeds ``regression-output/``
    from ``regression-fixture/`` and re-routes it, so this fixture is the
    board the ``diffpair-routing-regression`` gate measures.  If it ever
    stopped carrying ``POUR_NETS``, that job's pour verdict would silently
    become meaningless in exactly the way #5628 describes.
    """
    mod = generate_design_mod
    fixture = BOARD_DIR / "regression-fixture" / "diffpair_test.kicad_pcb"
    assert fixture.is_file(), f"missing CI re-route fixture: {fixture}"

    assert mod._recipe_board_pour_nets_missing(fixture) == []
