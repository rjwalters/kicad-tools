"""Best-so-far checkpointing, --resume and regressing-pass rollback (Issue #5945).

Covers both routing paths:

* ``kct route`` (Autorouter): ``--checkpoint PATH`` writes on every
  improvement, a run killed part-way leaves a valid LVS-consistent
  checkpoint, ``--resume`` reaches at least the checkpoint's completion, and
  the negotiated loop reports (and rolls back to) the best pass when the final
  pass regresses.
* ``kct route-auto`` (RoutingOrchestrator): a net pass that lowers the number
  of complete nets is undone, and the checkpoint keeps the best pass.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.router.checkpoint import (
    BestCheckpointWriter,
    RouteScore,
    checkpoint_identity_mismatch,
    read_checkpoint_meta,
    sidecar_path,
)
from kicad_tools.router.preserve_existing import fully_connected_nets

REPO = Path(__file__).resolve().parents[1]
VD_DIR = REPO / "boards" / "01-voltage-divider" / "output"
VD_UNROUTED = VD_DIR / "voltage_divider.kicad_pcb"
VD_ROUTED = VD_DIR / "voltage_divider_routed.kicad_pcb"


def _copy_board(src: Path, dest: Path) -> Path:
    shutil.copyfile(src, dest)
    for suffix in (".kicad_pro", ".kicad_dru"):
        if src.with_suffix(suffix).exists():
            shutil.copyfile(src.with_suffix(suffix), dest.with_suffix(suffix))
    return dest


# ---------------------------------------------------------------------------
# RouteScore ordering
# ---------------------------------------------------------------------------


class TestRouteScore:
    def test_more_complete_nets_wins_over_everything(self):
        a = RouteScore(nets_complete=5, drc_violations=9, overflow=9, wirelength_mm=999, vias=99)
        b = RouteScore(nets_complete=4)
        assert a.is_better_than(b)
        assert not b.is_better_than(a)

    def test_drc_then_wirelength_then_vias(self):
        base = RouteScore(nets_complete=3, drc_violations=1, wirelength_mm=10.0, vias=2)
        assert RouteScore(nets_complete=3, drc_violations=0, wirelength_mm=50.0).is_better_than(
            base
        )
        assert RouteScore(
            nets_complete=3, drc_violations=1, wirelength_mm=9.0, vias=5
        ).is_better_than(base)
        assert RouteScore(
            nets_complete=3, drc_violations=1, wirelength_mm=10.0, vias=1
        ).is_better_than(base)

    def test_equal_is_not_better_and_float_noise_ignored(self):
        a = RouteScore(nets_complete=2, wirelength_mm=10.0)
        b = RouteScore(nets_complete=2, wirelength_mm=10.0 + 1e-9)
        assert not a.is_better_than(b)
        assert not b.is_better_than(a)
        assert a.is_better_than(None)

    def test_round_trip_dict(self):
        s = RouteScore(nets_complete=3, drc_violations=1, overflow=2, nets_routed=4, vias=5)
        assert RouteScore.from_dict(s.to_dict()) == s

    def test_from_board_counts_complete_nets(self):
        assert RouteScore.from_board(VD_ROUTED).nets_complete == 3
        assert RouteScore.from_board(VD_UNROUTED).nets_complete == 0


# ---------------------------------------------------------------------------
# BestCheckpointWriter
# ---------------------------------------------------------------------------


class TestBestCheckpointWriter:
    def test_writes_only_on_improvement(self, tmp_path):
        ck = tmp_path / "ck.kicad_pcb"
        writes: list[str] = []
        w = BestCheckpointWriter(ck, command="route", quiet=True)

        def writer(tag):
            def _write(dest: Path) -> None:
                dest.write_text(tag)
                writes.append(tag)

            return _write

        assert w.offer(RouteScore(nets_complete=2), 0, writer("p0"))
        assert w.offer(RouteScore(nets_complete=4), 1, writer("p1"))
        # A regressing pass never overwrites the best checkpoint.
        assert not w.offer(RouteScore(nets_complete=3), 2, writer("p2"))
        # Equal is not an improvement either.
        assert not w.offer(RouteScore(nets_complete=4), 3, writer("p3"))
        assert writes == ["p0", "p1"]
        assert ck.read_text() == "p1"
        meta = read_checkpoint_meta(ck)
        assert meta is not None
        assert meta["pass"] == 1
        assert meta["score"]["nets_complete"] == 4
        assert meta["command"] == "route"
        assert sidecar_path(ck) == tmp_path / "ck.checkpoint.json"

    def test_seed_blocks_worse_offers(self, tmp_path):
        w = BestCheckpointWriter(tmp_path / "ck.kicad_pcb", command="route", quiet=True)
        w.seed(RouteScore(nets_complete=5), None)
        assert not w.offer(RouteScore(nets_complete=4), 0, lambda d: d.write_text("x"))
        assert not (tmp_path / "ck.kicad_pcb").exists()

    def test_identity_mismatch(self, tmp_path):
        assert checkpoint_identity_mismatch(VD_UNROUTED, VD_ROUTED) is None
        other = REPO / "boards" / "00-simple-led" / "output" / "simple_led.kicad_pcb"
        assert checkpoint_identity_mismatch(VD_UNROUTED, other)


# ---------------------------------------------------------------------------
# Negotiated loop: a regressing final pass is rolled back and reported
# ---------------------------------------------------------------------------


class TestNegotiatedRegressingFinalPass:
    def test_regressing_final_pass_does_not_override_best(self, capsys):
        from kicad_tools.router.core import Autorouter
        from tests.test_route_all_negotiated_overflow_regression import _OverflowSequenceGrid

        ar = Autorouter(width=20.0, height=20.0)
        ar.add_component(
            "R1",
            [
                {"number": "1", "x": 2.0, "y": 10.0, "net": 1, "net_name": "NET1"},
                {"number": "2", "x": 18.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            ],
        )
        ar.add_component(
            "R2",
            [
                {"number": "1", "x": 10.0, "y": 2.0, "net": 2, "net_name": "NET2"},
                {"number": "2", "x": 10.0, "y": 18.0, "net": 2, "net_name": "NET2"},
            ],
        )
        offered: list[tuple[int, int]] = []

        def checkpoint_cb(routes, metrics):
            offered.append((metrics.iteration, metrics.overflow))

        # Iteration 0 overflow=16, every later pass regresses to 36.
        seq = _OverflowSequenceGrid(ar.grid, sequence=[16, 36, 36, 36, 36])
        with (
            patch.object(ar.grid, "get_total_overflow", side_effect=seq),
            patch(
                "kicad_tools.router.core.NegotiatedRouter.find_nets_through_overused_cells",
                return_value={1, 2},
            ),
        ):
            ar.route_all_negotiated(
                max_iterations=2,
                timeout=10.0,
                adaptive=False,
                perturbation=False,
                checkpoint_callback=checkpoint_cb,
            )

        out = capsys.readouterr().out
        assert ar.emitted_iteration == 0
        assert ar.emitted_iteration_rolled_back is True
        assert "Emitting iteration 0 result" in out
        assert "rolled back" in out
        # The checkpoint hook never saw the regressing overflow=36 state, so a
        # --checkpoint file can never hold it.
        assert all(overflow != 36 for _, overflow in offered)


# ---------------------------------------------------------------------------
# kct route: killed run leaves a valid checkpoint; resume reaches >= it
# ---------------------------------------------------------------------------


def _route_main(argv: list[str]) -> int:
    from kicad_tools.cli.route_cmd import main as route_main

    return route_main(argv)


class TestRouteCheckpointResume:
    def test_killed_run_checkpoint_is_valid_and_resume_reaches_it(self, tmp_path):
        board = _copy_board(VD_UNROUTED, tmp_path / "vd.kicad_pcb")
        ck = tmp_path / "vd_ck.kicad_pcb"

        original_offer = BestCheckpointWriter.offer

        def offer_then_kill(self, *a, **kw):
            wrote = original_offer(self, *a, **kw)
            if wrote:
                # Simulate the run being killed right after the first
                # checkpoint landed on disk.
                raise KeyboardInterrupt
            return wrote

        with patch.object(BestCheckpointWriter, "offer", offer_then_kill):
            with contextlib.suppress(KeyboardInterrupt, SystemExit):
                _route_main(
                    [
                        str(board),
                        "-o",
                        str(tmp_path / "killed.kicad_pcb"),
                        "--checkpoint",
                        str(ck),
                        "--quiet",
                        "--no-auto-layers",
                        "--checkpoint-interval",
                        "0",
                    ]
                )

        # A valid, LVS-consistent checkpoint board + sidecar exist.
        assert ck.exists()
        assert checkpoint_identity_mismatch(board, ck) is None
        from kicad_tools.router.optimizer.pcb import parse_segments

        assert ck.read_text().count("(") == ck.read_text().count(")")
        assert parse_segments(ck.read_text()), "checkpoint must carry routed copper"
        meta = read_checkpoint_meta(ck)
        assert meta is not None and meta["command"] == "route"
        ck_complete = set(fully_connected_nets(ck))

        # Resume from it: completion must not drop below the checkpoint's.
        out = tmp_path / "resumed.kicad_pcb"
        rc = _route_main(
            [str(board), "-o", str(out), "--resume", str(ck), "--quiet", "--no-auto-layers"]
        )
        assert rc in (0, 3, 4)
        assert out.exists()
        assert ck_complete <= set(fully_connected_nets(out))
        assert checkpoint_identity_mismatch(board, out) is None

    def test_resume_rejects_foreign_checkpoint(self, tmp_path, capsys):
        board = _copy_board(VD_UNROUTED, tmp_path / "vd.kicad_pcb")
        other = REPO / "boards" / "00-simple-led" / "output" / "simple_led.kicad_pcb"
        rc = _route_main([str(board), "--resume", str(other), "--quiet"])
        assert rc == 2
        assert "does not match" in capsys.readouterr().err

    def test_resume_missing_checkpoint(self, tmp_path):
        board = _copy_board(VD_UNROUTED, tmp_path / "vd.kicad_pcb")
        assert _route_main([str(board), "--resume", str(tmp_path / "nope.kicad_pcb")]) == 1


# ---------------------------------------------------------------------------
# kct route-auto: regressing pass rollback + checkpoint
# ---------------------------------------------------------------------------


def _auto_args(**kw) -> argparse.Namespace:
    base = {
        "pcb": None,
        "net": None,
        "nets": None,
        "output": None,
        "strategy": "auto",
        "dry_run": False,
        "format": "text",
        "quiet": True,
        "checkpoint": None,
        "resume": None,
        "no_repair": False,
        "no_via_resolution": False,
        "verbose": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


class TestRouteAutoPasses:
    def test_regressing_pass_is_rolled_back_and_checkpoint_keeps_best(self, tmp_path, monkeypatch):
        from kicad_tools.cli.commands import routing

        board = _copy_board(VD_UNROUTED, tmp_path / "vd.kicad_pcb")
        out = tmp_path / "out.kicad_pcb"
        ck = tmp_path / "ck.kicad_pcb"
        routed_text = VD_ROUTED.read_text()
        unrouted_text = VD_UNROUTED.read_text()
        sources: list[str] = []

        def fake_one(args, net_name, pcb_path, output_path, **kwargs):
            sources.append(pcb_path)
            # Pass 1 completes every net; pass 2 "routes" by wiping them out.
            text = routed_text if net_name == "VIN" else unrouted_text
            Path(output_path).write_text(text)
            return 0, {"net": net_name, "success": True}

        monkeypatch.setattr(routing, "_route_auto_one", fake_one)
        rc = routing.run_route_auto_command(
            _auto_args(pcb=str(board), nets="VIN,VOUT", output=str(out), checkpoint=str(ck))
        )

        # The regressing pass was undone: the output still holds pass 1.
        assert rc == 1
        assert out.read_text() == routed_text
        assert len(fully_connected_nets(out)) == 3
        meta = read_checkpoint_meta(ck)
        assert meta is not None and meta["pass"] == 1
        assert ck.read_text() == routed_text

    def test_rolled_back_net_reports_not_successful_in_json(self, tmp_path, monkeypatch, capsys):
        from kicad_tools.cli.commands import routing

        board = _copy_board(VD_UNROUTED, tmp_path / "vd.kicad_pcb")
        out = tmp_path / "out.kicad_pcb"
        routed_text = VD_ROUTED.read_text()
        unrouted_text = VD_UNROUTED.read_text()

        def fake_one(args, net_name, pcb_path, output_path, **kwargs):
            Path(output_path).write_text(routed_text if net_name == "VIN" else unrouted_text)
            return 0, {"net": net_name, "success": True}

        monkeypatch.setattr(routing, "_route_auto_one", fake_one)
        rc = routing.run_route_auto_command(
            _auto_args(pcb=str(board), nets="VIN,VOUT", output=str(out), format="json")
        )
        assert rc == 1
        doc = json.loads(capsys.readouterr().out)
        vin, vout = doc["nets"]
        assert vin["success"] is True and "rolled_back" not in vin
        assert vout["success"] is False
        assert vout["rolled_back"] is True and vout["rolled_back_to_pass"] == 1
        assert doc["nets_routed"] == 1 and doc["success"] is False

    def test_no_rollback_keeps_regressing_pass(self, tmp_path, monkeypatch):
        from kicad_tools.cli.commands import routing

        board = _copy_board(VD_UNROUTED, tmp_path / "vd.kicad_pcb")
        out = tmp_path / "out.kicad_pcb"
        ck = tmp_path / "ck.kicad_pcb"
        routed_text = VD_ROUTED.read_text()
        unrouted_text = VD_UNROUTED.read_text()
        docs: list[dict] = []

        def fake_one(args, net_name, pcb_path, output_path, **kwargs):
            Path(output_path).write_text(routed_text if net_name == "VIN" else unrouted_text)
            doc = {"net": net_name, "success": True}
            docs.append(doc)
            return 0, doc

        monkeypatch.setattr(routing, "_route_auto_one", fake_one)
        rc = routing.run_route_auto_command(
            _auto_args(
                pcb=str(board),
                nets="VIN,VOUT",
                output=str(out),
                checkpoint=str(ck),
                no_rollback=True,
            )
        )
        assert rc == 0
        assert out.read_text() == unrouted_text
        assert all(d["success"] and "rolled_back" not in d for d in docs)
        # Scoring still runs: the checkpoint keeps the best pass.
        assert ck.read_text() == routed_text

    def test_checkpoint_without_output_uses_working_copy(self, tmp_path, monkeypatch):
        from kicad_tools.cli.commands import routing

        board = _copy_board(VD_UNROUTED, tmp_path / "vd.kicad_pcb")
        ck = tmp_path / "ck.kicad_pcb"
        routed_text = VD_ROUTED.read_text()

        def fake_one(args, net_name, pcb_path, output_path, **kwargs):
            assert output_path and Path(pcb_path).exists()
            Path(output_path).write_text(routed_text)
            return 0, {"net": net_name, "success": True}

        monkeypatch.setattr(routing, "_route_auto_one", fake_one)
        rc = routing.run_route_auto_command(
            _auto_args(pcb=str(board), net="VIN", checkpoint=str(ck))
        )
        assert rc == 0
        assert ck.read_text() == routed_text
        assert json.loads(sidecar_path(ck).read_text())["command"] == "route-auto"
        # The private working copy is cleaned up.
        assert not list(tmp_path.glob("ck_working_*"))

    def test_resume_skips_nets_complete_in_checkpoint(self, tmp_path, monkeypatch):
        from kicad_tools.cli.commands import routing

        board = _copy_board(VD_UNROUTED, tmp_path / "vd.kicad_pcb")
        ck = _copy_board(VD_ROUTED, tmp_path / "ck.kicad_pcb")
        called: list[str] = []

        def fake_one(args, net_name, pcb_path, output_path, **kwargs):
            called.append(net_name)
            return 0, {"net": net_name, "success": True}

        monkeypatch.setattr(routing, "_route_auto_one", fake_one)
        rc = routing.run_route_auto_command(
            _auto_args(pcb=str(board), nets="VIN,VOUT", resume=str(ck))
        )
        assert rc == 0
        assert called == []

    def test_resume_all_complete_still_writes_output(self, tmp_path, monkeypatch):
        """Every requested net complete in CK: -o must hold CK, not be missing/stale."""
        from kicad_tools.cli.commands import routing

        board = _copy_board(VD_UNROUTED, tmp_path / "vd.kicad_pcb")
        ck = _copy_board(VD_ROUTED, tmp_path / "ck.kicad_pcb")
        out = tmp_path / "out.kicad_pcb"
        out.write_text("stale output from an earlier run\n")

        def fake_one(*a, **kw):  # pragma: no cover - must not be called
            raise AssertionError("no net should be routed")

        monkeypatch.setattr(routing, "_route_auto_one", fake_one)
        rc = routing.run_route_auto_command(
            _auto_args(pcb=str(board), nets="VIN,VOUT", resume=str(ck), output=str(out))
        )
        assert rc == 0
        assert out.read_text() == ck.read_text()
        assert out.with_suffix(".kicad_pro").exists() == ck.with_suffix(".kicad_pro").exists()

    def test_resume_first_routed_net_chains_from_seeded_output(self, tmp_path, monkeypatch):
        from kicad_tools.cli.commands import routing

        board = _copy_board(VD_UNROUTED, tmp_path / "vd.kicad_pcb")
        ck = _copy_board(VD_UNROUTED, tmp_path / "ck.kicad_pcb")
        out = tmp_path / "out.kicad_pcb"
        sources: list[tuple[str, str]] = []

        def fake_one(args, net_name, pcb_path, output_path, **kwargs):
            sources.append((pcb_path, Path(pcb_path).read_text()))
            return 1, {"net": net_name, "success": False}

        monkeypatch.setattr(routing, "_route_auto_one", fake_one)
        routing.run_route_auto_command(
            _auto_args(pcb=str(board), nets="VIN,VOUT", resume=str(ck), output=str(out))
        )
        assert [p for p, _ in sources] == [str(out), str(out)]
        assert all(text == ck.read_text() for _, text in sources)

    def test_failed_first_net_does_not_chain_from_stale_output(self, tmp_path, monkeypatch):
        """Issue #6010: a net that writes nothing must not leave a stale -o in play."""
        from kicad_tools.cli.commands import routing

        board = _copy_board(VD_UNROUTED, tmp_path / "vd.kicad_pcb")
        out = tmp_path / "out.kicad_pcb"
        out.write_text("stale output from an earlier run\n")
        seen: list[str] = []

        def fake_one(args, net_name, pcb_path, output_path, **kwargs):
            seen.append(Path(pcb_path).read_text())
            return 1, {"net": net_name, "success": False}

        monkeypatch.setattr(routing, "_route_auto_one", fake_one)
        rc = routing.run_route_auto_command(
            _auto_args(pcb=str(board), nets="VIN,VOUT", output=str(out))
        )
        assert rc == 1
        assert seen == [VD_UNROUTED.read_text()] * 2
        assert out.read_text() == VD_UNROUTED.read_text()


def test_flags_declared_on_route_and_route_auto():
    from kicad_tools.cli.parser import create_parser

    parser = create_parser()
    ns = parser.parse_args(
        ["route", "b.kicad_pcb", "--checkpoint", "c.kicad_pcb", "--resume", "r.kicad_pcb"]
    )
    assert ns.checkpoint == "c.kicad_pcb" and ns.resume == "r.kicad_pcb"
    ns = parser.parse_args(
        [
            "route-auto",
            "b.kicad_pcb",
            "--net",
            "A",
            "--checkpoint",
            "c.kicad_pcb",
            "--resume",
            "r.kicad_pcb",
        ]
    )
    assert ns.checkpoint == "c.kicad_pcb" and ns.resume == "r.kicad_pcb"
    assert ns.no_rollback is False
    ns = parser.parse_args(["route-auto", "b.kicad_pcb", "--nets", "A,B", "--no-rollback"])
    assert ns.no_rollback is True


def test_route_all_checkpoint_measures_completion():
    """Issue #5945: route_all / two-phase snapshots carry nets_fully_connected."""
    from kicad_tools.router.core import _emit_route_checkpoint

    got: list = []

    def cb(routes, metrics):
        got.append(metrics)

    cb.wants_completion = True  # type: ignore[attr-defined]
    _emit_route_checkpoint(cb, [], 0, completion=lambda routes: 7)
    assert got[-1].nets_fully_connected == 7
    assert RouteScore.from_routes([], got[-1]).nets_complete == 7

    cb.wants_completion = False  # type: ignore[attr-defined]
    _emit_route_checkpoint(cb, [], 0, completion=lambda routes: 7)
    assert got[-1].nets_fully_connected == 0


def test_resume_baseline_makes_route_scores_like_for_like():
    seed = RouteScore.from_board(VD_ROUTED)
    # A resumed pass that adds nothing scores exactly like the seed copper.
    same = RouteScore.from_routes([], None, complete_offset=seed.nets_complete, baseline=seed)
    assert same.wirelength_mm == seed.wirelength_mm and same.vias == seed.vias
    assert same.nets_routed >= seed.nets_routed
    assert not same.is_better_than(seed)


@pytest.mark.parametrize("flag", ["--checkpoint", "--resume"])
def test_route_shim_forwards_flag(flag, monkeypatch):
    from kicad_tools.cli import route_cmd
    from kicad_tools.cli.commands import routing
    from kicad_tools.cli.parser import create_parser

    seen: list[list[str]] = []
    monkeypatch.setattr(route_cmd, "main", lambda argv: seen.append(argv) or 0)
    ns = create_parser().parse_args(["route", "b.kicad_pcb", flag, "x.kicad_pcb"])
    routing.run_route_command(ns)
    assert seen and flag in seen[0] and "x.kicad_pcb" in seen[0]
