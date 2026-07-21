"""Copper-LVS coverage for board 03 (usb-joystick) — issue #3787.

Board 03 carries GND planes on *both* ``F.Cu`` and ``B.Cu`` but the
router emits zero GND vias (every via is a signal-net layer change).
Without a via tying the two GND planes together, the J1 USB-C connector's
F.Cu-only shield/return pads (``J1.A12`` / ``J1.B1``) strand in their own
copper islands and ``compare_copper_netlist`` correctly reports them as
GND *opens* — a physical bond the schematic-blind copper extractor
catches even though KiCad's DRC (which treats same-net zones as logically
connected) does not.

Issue #3787 adds an ``add_gnd_stitching_vias`` recipe step that drops a
GND ``F.Cu``<->``B.Cu`` stitching via at each fragmented GND island; the
subsequent zone re-fill bonds them into one net.  This test pins that
outcome on the committed routed artifact: copper-LVS must be clean and the
GND stitching vias must be present.

It is fast (sub-second) and hermetic — it reads the committed files only,
never spawns ``kicad-cli`` and never invokes the router.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.drc.geometric import GeometricDRCResult
from kicad_tools.lvs import compare_copper_netlist

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "03-usb-joystick"
BOARD_OUTPUT = BOARD_DIR / "output"
BOARD_SCH = BOARD_OUTPUT / "usb_joystick.kicad_sch"
BOARD_PCB = BOARD_OUTPUT / "usb_joystick_routed.kicad_pcb"


def _count_gnd_fb_stitch_vias(pcb_text: str) -> int:
    """Count GND-net F.Cu<->B.Cu through-vias in a routed PCB's text."""
    net_table = dict(re.findall(r'\(net (\d+) "([^"]*)"\)', pcb_text))
    gnd_ids = {nid for nid, name in net_table.items() if name == "GND"}
    if not gnd_ids:
        return 0
    count = 0
    for via in re.finditer(
        r"\(via\s*\(at [^\)]*\)\s*\(size [\d.]+\)\s*\(drill [\d.]+\)\s*"
        r'\(layers ([^\)]*)\)\s*\(uuid "[^"]*"\)\s*\(net (\d+)\)',
        pcb_text,
    ):
        layers, net_id = via.group(1), via.group(2)
        if net_id in gnd_ids and '"F.Cu"' in layers and '"B.Cu"' in layers:
            count += 1
    return count


@pytest.fixture(scope="module")
def board03_artifacts() -> tuple[Path, Path]:
    """The committed board 03 schematic + routed PCB.

    Skips if either artifact is missing (e.g. a contributor wiped the
    output directory) — run ``boards/03-usb-joystick/generate_design.py``
    to regenerate.
    """
    if not BOARD_SCH.exists() or not BOARD_PCB.exists():
        pytest.skip(
            f"board 03 artifacts not present "
            f"(sch={BOARD_SCH.exists()}, pcb={BOARD_PCB.exists()}); "
            "run boards/03-usb-joystick/generate_design.py to regenerate."
        )
    return BOARD_SCH, BOARD_PCB


class TestBoard03CopperLVSClean:
    """The committed board 03 routed PCB must be copper-LVS clean (#3787)."""

    def test_no_copper_opens_or_shorts(self, board03_artifacts: tuple[Path, Path]) -> None:
        sch, pcb = board03_artifacts
        result = compare_copper_netlist(sch, pcb)
        assert result.clean is True, (
            "copper-LVS unexpectedly dirty on the committed board 03 routed "
            f"PCB: shorts={list(result.shorts)} opens={list(result.opens)}.  "
            "The GND F.Cu<->B.Cu stitching vias (add_gnd_stitching_vias, "
            "#3787) may be missing or the route/fill regressed — regenerate "
            "via generate_design.py."
        )
        assert result.shorts == ()
        assert result.opens == ()

    def test_gnd_stitching_vias_present(self, board03_artifacts: tuple[Path, Path]) -> None:
        """At least one GND-net via must bond the F.Cu and B.Cu GND planes."""
        _, pcb = board03_artifacts
        text = pcb.read_text()
        net_table = dict(re.findall(r'\(net (\d+) "([^"]*)"\)', text))
        gnd_ids = {nid for nid, name in net_table.items() if name == "GND"}
        assert gnd_ids, 'board 03 PCB has no (net N "GND") entry'

        gnd_fb_vias = 0
        for via in re.finditer(
            r"\(via\s*\(at [^\)]*\)\s*\(size [\d.]+\)\s*\(drill [\d.]+\)\s*"
            r'\(layers ([^\)]*)\)\s*\(uuid "[^"]*"\)\s*\(net (\d+)\)',
            text,
        ):
            layers, net_id = via.group(1), via.group(2)
            if net_id in gnd_ids and '"F.Cu"' in layers and '"B.Cu"' in layers:
                gnd_fb_vias += 1

        assert gnd_fb_vias >= 1, (
            "board 03 routed PCB has no GND F.Cu<->B.Cu stitching via; "
            "the two GND planes are unbonded and copper-LVS will report "
            "opens (#3787)."
        )


def _load_board03_module():
    """Import the board-03 ``generate_design.py`` recipe module."""
    import importlib.util

    gen = BOARD_DIR / "generate_design.py"
    spec = importlib.util.spec_from_file_location("board03_generate_design", gen)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_add_gnd_stitching_vias():
    """Import ``add_gnd_stitching_vias`` from the board-03 recipe module."""
    return _load_board03_module().add_gnd_stitching_vias


class TestBoard03UnconditionalUsbcStitch:
    """The USB-C F.Cu-only GND stitch is fill-independent + idempotent (#3841).

    These are fast and hermetic: they operate on a *copy* of the committed
    routed PCB and never invoke the router or ``kicad-cli``.  They pin the
    two structural guarantees of the #3841 fix that the committed-artifact
    tests cannot express.
    """

    def test_idempotent_on_committed_board(
        self, board03_artifacts: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """Re-running on an already-stitched board adds no duplicate vias."""
        sch, pcb = board03_artifacts
        add_gnd_stitching_vias = _load_add_gnd_stitching_vias()

        work_sch = tmp_path / sch.name
        work_pcb = tmp_path / pcb.name
        work_sch.write_text(sch.read_text())
        work_pcb.write_text(pcb.read_text())

        before = _count_gnd_fb_stitch_vias(work_pcb.read_text())
        # First run may top up the committed board (A1/B12) to all four J1
        # shield pads; the *second* run must be a strict no-op.
        add_gnd_stitching_vias(work_pcb)
        after_first = _count_gnd_fb_stitch_vias(work_pcb.read_text())
        added_second = add_gnd_stitching_vias(work_pcb)
        after_second = _count_gnd_fb_stitch_vias(work_pcb.read_text())

        assert added_second == 0, "re-run added vias to an already-stitched board"
        assert after_second == after_first, "re-run changed the GND via count (not idempotent)"
        assert after_first >= before, "stitch pass removed pre-existing vias"

    def test_stitches_all_four_usbc_gnd_pads_fill_independent(
        self, board03_artifacts: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """Every J1 F.Cu-only GND shield pad ends up with a stitch via.

        This is the exact failure mode of #3841: even when a fill bonds the
        pads (the committed board's persisted fill does, on most hosts), the
        unconditional pass must still place a via at each F.Cu-only USB-C GND
        pad so the result does not depend on which fill happened to bond it.
        The four pads are J1.A1 (145.75,65.5), J1.A12 (151.25,65.5),
        J1.B1 (151.25,66.5), J1.B12 (145.75,66.5).

        (Pad coordinates updated for the sheet-centering translation of the
        committed board-03 artifact: dx=+8.5, dy=-42.5 -- kct pcb
        center-on-sheet.)
        """
        sch, pcb = board03_artifacts
        add_gnd_stitching_vias = _load_add_gnd_stitching_vias()

        work_sch = tmp_path / sch.name
        work_pcb = tmp_path / pcb.name
        work_sch.write_text(sch.read_text())
        # Strip ALL four committed J1 GND stitch vias to simulate a fresh
        # route whose fill happened to bond the pads (the single-island
        # detect result that shipped the #3841 bug).
        text = pcb.read_text()
        # J1 shield pad positions in the sheet-centered committed artifact
        # (was 137.25/142.75 x 108/109 before the center-on-sheet shift).
        for coord in ("145.75 65.5", "151.25 65.5", "151.25 66.5", "145.75 66.5"):
            text = re.sub(
                r"\t\(via\n\t\t\(at " + re.escape(coord) + r"\)\n.*?\t\)\n",
                "",
                text,
                flags=re.DOTALL,
            )
        work_pcb.write_text(text)
        # Sanity: the strip removed every J1 GND stitch via.
        assert _count_gnd_fb_stitch_vias(work_pcb.read_text()) == 0

        added = add_gnd_stitching_vias(work_pcb)
        assert added == 4, f"expected 4 USB-C GND stitch vias, added {added}"

        out = work_pcb.read_text()
        for coord in ("145.75 65.5", "151.25 65.5", "151.25 66.5", "145.75 66.5"):
            assert f"(at {coord})" in out, f"no GND stitch via placed at J1 pad ({coord})"


class TestBoard03PartialRouteFastFail:
    """A partial route fails fast with a distinct message, not an LVS trace (#4027).

    Root cause of the #4027 flake: ``route_pcb`` routes under a ``--timeout
    600`` wall-clock SAFETY backstop layered above the load-independent
    per-net ``--deterministic-budget`` iteration cap.  Under concurrent CPU
    load that outer deadline can fire before every signal net lands, so
    ``route_pcb`` returns ``False``.  Before this fix ``main()`` never
    checked that return value and fell through to ``add_gnd_stitching_vias``
    -> ``fill_zones_in_routed_pcb`` -> ``write_lvs_report(require_clean=True)``,
    which raised ``BoardNetlistMismatch`` on the unrouted net's copper OPEN
    and surfaced as a misleading "copper-LVS DIRTY / GND stitching" failure.

    These tests are fast and hermetic: they monkeypatch the recipe's own
    module-level functions so ``main()`` runs without invoking the router,
    ``kicad-cli``, or the LVS comparator.  They pin two guarantees:
      1. a partial route (``route_pcb`` -> ``False``) exits non-zero with a
         distinct "partial route" message BEFORE any stitching/fill/LVS step;
      2. a full route (``route_pcb`` -> ``True``) still reaches the existing
         LVS gate (no false positive on the happy path).
    """

    def _stub_pipeline_prefix(self, module, monkeypatch, tmp_path: Path) -> None:
        """Neutralise the recipe steps that run before the route_success gate."""
        sch = tmp_path / "usb_joystick.kicad_sch"
        sch.write_text("(kicad_sch)")
        pcb = tmp_path / "usb_joystick.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        monkeypatch.setattr(module, "create_project", lambda *a, **k: tmp_path / "p.kicad_pro")
        monkeypatch.setattr(module, "create_usb_joystick_schematic", lambda *a, **k: sch)
        monkeypatch.setattr(module, "run_erc", lambda *a, **k: True)
        monkeypatch.setattr(module, "create_usb_joystick_pcb", lambda *a, **k: pcb)
        monkeypatch.setattr(module, "create_zones_for_pcb", lambda *a, **k: None)

    def _forbid_downstream(self, module, monkeypatch) -> None:
        """Make every post-gate step blow up loudly if the gate lets them run."""

        def _boom(name):
            def _raise(*a, **k):
                raise AssertionError(
                    f"{name} ran despite a partial route -- the route_success "
                    "gate (#4027) did not short-circuit the pipeline"
                )

            return _raise

        monkeypatch.setattr(module, "add_gnd_stitching_vias", _boom("add_gnd_stitching_vias"))
        monkeypatch.setattr(module, "fill_zones_in_routed_pcb", _boom("fill_zones_in_routed_pcb"))
        monkeypatch.setattr(module, "run_drc", _boom("run_drc"))
        monkeypatch.setattr(module, "write_lvs_report", _boom("write_lvs_report"))

    def test_partial_route_fails_fast_with_distinct_message(
        self, monkeypatch, capsys, tmp_path: Path
    ) -> None:
        module = _load_board03_module()
        self._stub_pipeline_prefix(module, monkeypatch, tmp_path)
        self._forbid_downstream(module, monkeypatch)
        # The proximate cause of the #4027 flake: route_pcb returns False.
        monkeypatch.setattr(module, "route_pcb", lambda *a, **k: False)
        monkeypatch.setattr(module.sys, "argv", ["generate_design.py", str(tmp_path / "out")])

        rc = module.main()

        assert rc == 1, "partial route must make main() exit non-zero"
        err = capsys.readouterr().err
        # The message must name the real cause (partial route / wall-clock
        # budget) and must NOT be a copper-LVS / GND-stitching trace.
        assert "partial route" in err.lower(), (
            "partial-route failure must be reported with a distinct 'partial "
            f"route' message, got stderr:\n{err}"
        )
        assert "wall-clock budget" in err.lower()
        assert "BoardNetlistMismatch" not in err, (
            "a partial route must NOT surface as an LVS BoardNetlistMismatch"
        )

    def test_full_route_still_reaches_lvs_gate(self, monkeypatch, tmp_path: Path) -> None:
        """A full route (N==M) must NOT trip the fast-fail gate.

        The gate keys strictly off ``route_pcb`` returning ``False``; a full
        route still flows into the existing stitching/fill/LVS steps.  We stub
        those to no-ops and assert the LVS gate is the one that runs (proving
        the fast-fail path did not swallow the happy path).
        """
        module = _load_board03_module()
        self._stub_pipeline_prefix(module, monkeypatch, tmp_path)
        monkeypatch.setattr(module, "route_pcb", lambda *a, **k: True)
        monkeypatch.setattr(module, "add_gnd_stitching_vias", lambda *a, **k: 0)
        monkeypatch.setattr(module, "fill_zones_in_routed_pcb", lambda *a, **k: None)
        monkeypatch.setattr(module, "run_drc", lambda *a, **k: True)

        lvs_called: list[bool] = []

        def _fake_lvs(*a, **k):
            lvs_called.append(True)
            # #3912: the migrated main() unpacks ``copper_clean, _label_clean =
            # write_lvs_report(...)`` and ANDs ``copper_clean`` into the shared
            # gate's LVS leg.  Return a clean 2-tuple so the happy path reaches
            # a PASSING gate (the whole point of this test).
            return (True, True)

        monkeypatch.setattr(module, "write_lvs_report", _fake_lvs)
        monkeypatch.setattr(module, "export_manufacturing_bundle", lambda *a, **k: True)

        # #3912: neutralise the gate's AUTHORITATIVE geometric-DRC leg.  The
        # migrated main() calls ``evaluate_pipeline_gate(routed_path, ...)``,
        # which shells ``run_geometric_drc`` (kicad-cli pcb drc) on the routed
        # PCB.  This unit test stubs the entire pipeline prefix, so no real
        # routed board exists for kicad-cli to check -- the DRC run would report
        # ``ran=False`` and (with the default ``require_drc=True``) fail the gate
        # for a reason this test is NOT trying to exercise.  Wrap the recipe's
        # ``evaluate_pipeline_gate`` reference to inject a clean, "did-run"
        # ``GeometricDRCResult`` via the gate's documented ``_drc_result`` test
        # seam.  The gate's route/LVS legs (the ones this test asserts on) still
        # run for real, so a PASSING verdict genuinely proves the full-route
        # path reaches and clears the LVS gate.
        real_gate = module.evaluate_pipeline_gate

        def _gate_with_clean_drc(*a, **k):
            k.setdefault("_drc_result", GeometricDRCResult(ran=True, by_type={}))
            return real_gate(*a, **k)

        monkeypatch.setattr(module, "evaluate_pipeline_gate", _gate_with_clean_drc)
        monkeypatch.setattr(module.sys, "argv", ["generate_design.py", str(tmp_path / "out")])

        rc = module.main()

        assert lvs_called == [True], (
            "a full (N==M) route must still reach write_lvs_report -- the "
            "#4027 fast-fail gate must not fire on a complete route"
        )
        assert rc == 0


@pytest.mark.slow
class TestBoard03FreshRegenCopperLVSClean:
    """A *fresh* regen of board 03 must be copper-LVS clean (#3841).

    The committed-artifact tests above only validate the static bytes that
    ship in ``output/`` — they cannot catch the failure mode of #3841, where
    ``add_gnd_stitching_vias`` decided *whether* to stitch the J1 USB-C
    F.Cu-only GND shield pads against a *throwaway* fill that differed from
    the persisted fill, so a clean regen could strand ``J1.A12`` / ``J1.B1``
    even though the committed board was clean.

    This test regenerates the whole board from source into a temp dir and
    asserts the persisted artifact is copper-LVS clean with the GND stitch
    vias present — closing the recipe-reproducibility gap that let the bug
    ship.  It is slow (a full route + fill, several minutes) and needs
    ``kicad-cli`` for the zone fill, so it is marked ``slow`` and skips when
    KiCad is unavailable (CI runners without KiCad).
    """

    def test_fresh_regen_is_copper_lvs_clean(self, tmp_path: Path) -> None:
        if find_kicad_cli() is None:
            pytest.skip("kicad-cli not available; fresh regen needs it for the zone fill")

        out_dir = tmp_path / "board03-fresh"
        gen = BOARD_DIR / "generate_design.py"
        result = subprocess.run(
            [sys.executable, str(gen), str(out_dir)],
            capture_output=True,
            text=True,
            timeout=1800,
        )
        assert result.returncode == 0, (
            "fresh board-03 regen (generate_design.py) failed with exit "
            f"{result.returncode}.\nstdout tail:\n{result.stdout[-3000:]}\n"
            f"stderr tail:\n{result.stderr[-3000:]}"
        )

        sch = out_dir / "usb_joystick.kicad_sch"
        pcb = out_dir / "usb_joystick_routed.kicad_pcb"
        assert sch.exists() and pcb.exists(), (
            f"fresh regen did not produce expected artifacts "
            f"(sch={sch.exists()}, pcb={pcb.exists()})"
        )

        # (1) The persisted artifact must be copper-LVS clean.
        lvs = compare_copper_netlist(sch, pcb)
        assert lvs.clean is True, (
            "fresh board-03 regen is copper-LVS DIRTY — the GND stitching "
            "vias did not bond J1's F.Cu-only shield pads on this regen "
            f"(#3841): shorts={list(lvs.shorts)} opens={list(lvs.opens)}."
        )
        assert lvs.opens == ()
        assert lvs.shorts == ()

        # (2) The GND F.Cu<->B.Cu stitch vias must be present after regen —
        # the unconditional USB-C stitch pass (#3841) places one per J1
        # F.Cu-only GND shield pad (A1/A12/B1/B12), so >= 2 (and in practice
        # 4) must be present regardless of how the fill settled.
        gnd_fb_vias = _count_gnd_fb_stitch_vias(pcb.read_text())
        assert gnd_fb_vias >= 2, (
            "fresh board-03 regen has too few GND F.Cu<->B.Cu stitch vias "
            f"({gnd_fb_vias}); the #3841 unconditional USB-C GND stitch pass "
            "should place one per J1 F.Cu-only shield pad."
        )
