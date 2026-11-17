"""Detour-probe wall-clock exclusion (issue #9244).

The ``--report-detour-ratio`` / ``--detour-repair`` probe pre-pass runs one
A* per net BEFORE the real route.  Its cost used to count against every
budget the real run obeys -- the stage ``timeout``, the CLI's
``args._routing_deadline`` / ``args._wall_clock_deadline`` -- so a binding
``--timeout`` could change which real searches time out and therefore the
routed copper, with the flag on vs off.  Issue #9244 excludes probe time
from all of them:

* the probe measures itself into ``Autorouter.detour_probe_elapsed_s`` and
  fires ``Autorouter.on_probe_elapsed`` once;
* drivers holding a stage clock across the probe re-base it by the delta;
* the CLI shifts its deadline stamps forward through the hook;
* probe searches run under ``wall_deadline.suspended()`` so an installed
  excursion deadline cannot clamp them into different probe rows.

Every slow-probe test below makes the probe artificially slow (one real
sleep per probed router -- the verdict depends only on sleep > timeout >>
the tiny board's real runtime, never on machine speed) so that probe +
real run exceeds the binding limit while the real run alone does not.
Each one also asserts the sleep actually fired -- a recorded probe-elapsed
value, or the report sidecar's probed count -- because a slow-probe test
whose injected sleep silently fails to apply passes as vacuous instead of
failing (the CLI identity test once drove ``route_cmd.main``, whose
supervisor runs any finite ``--timeout`` in a child process the
parent-process patch cannot reach; its probe never slept and nothing
noticed).

Measured bite scope on this board (re-base neutralized at source): the
plain ``route_all`` stage test and the in-process CLI test fail without
the fix -- a spent budget changes their copper.  The negotiated stage
test does NOT: iteration 0 completes and the issue-#3452 grace pass
re-routes a spent stage with identical copper, so it pins the re-base's
presence and the probe-fired guard rather than a behavioral bite.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.cli import route_cmd
from kicad_tools.router import wall_deadline
from kicad_tools.router.core import Autorouter
from tests import test_detour_ratio_report_9003 as base

# Sleep sizes: chosen so SLEEP > TIMEOUT >> the synthetic board's real
# routing time (tens of ms -- see test_detour_ratio_report_9003.py, whose
# full CLI identity tests run this board twice per test).
_STAGE_TIMEOUT_S = 2.0
_SLOW_PROBE_SLEEP_S = 5.0


@pytest.fixture
def board_path(tmp_path: Path) -> Path:
    """The #9003 synthetic board (4 congested nets, routes fully)."""
    pcb = tmp_path / "congested.kicad_pcb"
    pcb.write_text(base._board(nets=4))
    return pcb


def _slow_probe_route_net(monkeypatch, sleep_s: float) -> None:
    """Make each router's FIRST probe search cost ``sleep_s`` real seconds.

    Only the probe is slowed (``_detour_probe_active`` is set on the
    Autorouter for the duration of the pre-pass and restored by its
    snapshot); the real run searches at full speed.  One sleep per router
    instance keeps the test fast while still exceeding every budget below.
    """
    real_route_net = Autorouter.route_net
    slept: set[int] = set()

    def route_net_with_slow_probe(self, net, *args, **kwargs):
        if getattr(self, "_detour_probe_active", False) and id(self) not in slept:
            slept.add(id(self))
            time.sleep(sleep_s)
        return real_route_net(self, net, *args, **kwargs)

    monkeypatch.setattr(Autorouter, "route_net", route_net_with_slow_probe)


class TestStageTimeoutIdentity:
    """A binding stage ``timeout`` must not see the probe's cost."""

    def test_negotiated_stage_timeout_identity_with_slow_probe(
        self, board_path: Path, monkeypatch
    ) -> None:
        """Negotiated driver: probe cost re-based out of the stage clock.

        Identity + fired-sleep guard for the negotiated driver's
        ``start_time`` delta re-base.  NOT a bite test, measured: with the
        re-base neutralized at source the spent stage budget still leaves
        identical copper on this board -- iteration 0 completes (all nets
        routed, zero overflow) and the issue-#3452 grace pass re-routes
        the starved tail inside its own fund, so the spent clock never
        reaches the copper.  The behavioral bites live in
        ``test_plain_route_all_stage_timeout_identity_with_slow_probe``
        (whose initial-pass ``check_timeout`` does starve nets) and in the
        CLI identity test below; this test pins that the negotiated
        driver re-bases by the probe's real elapsed time (guard below)
        and leaves the routed copper untouched.
        """
        from kicad_tools.router.io import load_pcb_for_routing

        _slow_probe_route_net(monkeypatch, _SLOW_PROBE_SLEEP_S)
        router_on, _ = load_pcb_for_routing(str(board_path), strategy="grid")
        router_off, _ = load_pcb_for_routing(str(board_path), strategy="grid")
        router_on.report_detour_ratio = True

        kwargs = {
            "max_iterations": 4,
            "per_net_timeout": 10.0,
            "timeout": _STAGE_TIMEOUT_S,
            "use_targeted_ripup": True,
        }
        router_on.route_all_negotiated(**kwargs)
        router_off.route_all_negotiated(**kwargs)

        assert router_on.detour_probe_elapsed_s >= _SLOW_PROBE_SLEEP_S
        assert base._route_signature(router_on) == base._route_signature(router_off)

    def test_plain_route_all_stage_timeout_identity_with_slow_probe(
        self, board_path: Path, monkeypatch
    ) -> None:
        """Same bite for the plain ``route_all`` driver's outer budget."""
        from kicad_tools.router.io import load_pcb_for_routing

        _slow_probe_route_net(monkeypatch, _SLOW_PROBE_SLEEP_S)
        router_on, _ = load_pcb_for_routing(str(board_path), strategy="grid")
        router_off, _ = load_pcb_for_routing(str(board_path), strategy="grid")
        router_on.report_detour_ratio = True

        router_on.route_all(timeout=_STAGE_TIMEOUT_S, suppress_no_timeout_warning=True)
        router_off.route_all(timeout=_STAGE_TIMEOUT_S, suppress_no_timeout_warning=True)

        assert router_on.detour_probe_elapsed_s >= _SLOW_PROBE_SLEEP_S
        assert base._route_signature(router_on) == base._route_signature(router_off)
        assert len(router_on.routes) == len(router_off.routes)

    def test_reentered_driver_probe_skip_does_not_shift_again(
        self, board_path: Path, monkeypatch
    ) -> None:
        """A second driver call on a probed router adds a zero delta.

        The re-base uses the probe-elapsed DELTA across the call, so a
        re-entered driver whose probe was skipped (``detour_probe_records``
        already set) must not re-add the first probe's cost.
        """
        from kicad_tools.router.io import load_pcb_for_routing

        _slow_probe_route_net(monkeypatch, 0.0)  # no sleep needed
        router, _ = load_pcb_for_routing(str(board_path), strategy="grid")
        router.report_detour_ratio = True

        router.route_all(suppress_no_timeout_warning=True)
        elapsed_after_first = router.detour_probe_elapsed_s
        assert elapsed_after_first > 0.0
        assert router.detour_probe_records is not None

        # Second entry: the records guard skips the probe entirely, so the
        # accumulated elapsed must not move (a delta of exactly 0.0).
        router.route_all(suppress_no_timeout_warning=True)
        assert router.detour_probe_elapsed_s == elapsed_after_first


class TestCliDeadlinePath:
    """The CLI's ``args`` deadline stamps are shifted past the probe."""

    def test_apply_detour_ratio_report_shifts_deadlines_by_probe_elapsed(
        self, board_path: Path, monkeypatch
    ) -> None:
        from kicad_tools.router.io import load_pcb_for_routing

        _slow_probe_route_net(monkeypatch, 1.0)
        router, _ = load_pcb_for_routing(str(board_path), strategy="grid")

        args = SimpleNamespace(report_detour_ratio=True, timeout=30.0)
        route_cmd._set_wall_clock_deadline(args)
        route_cmd._apply_detour_ratio_report(router, args)
        routing_before = args._routing_deadline
        wall_before = args._wall_clock_deadline

        router.route_all(timeout=20.0, suppress_no_timeout_warning=True)

        elapsed = router.detour_probe_elapsed_s
        assert elapsed >= 1.0
        assert args._routing_deadline == pytest.approx(routing_before + elapsed)
        assert args._wall_clock_deadline == pytest.approx(wall_before + elapsed)
        assert args._detour_probe_shift_s == pytest.approx(elapsed)
        # The routing budget is intact: ~20 s of the 30 s timeout minus
        # reserves remain, probe included.
        remaining = route_cmd._remaining_budget(args)
        assert remaining is not None and remaining > 15.0

    def test_detour_repair_flag_wires_deadline_hook(self, board_path: Path) -> None:
        from kicad_tools.router.io import load_pcb_for_routing

        router, _ = load_pcb_for_routing(str(board_path), strategy="grid")
        assert router.on_probe_elapsed is None

        args = SimpleNamespace(detour_repair=True, strategy="negotiated", timeout=30.0)
        route_cmd._set_wall_clock_deadline(args)
        route_cmd._apply_detour_repair(router, args)
        assert router.on_probe_elapsed is not None

        # Flag-off appliers leave the default hook untouched.
        router2, _ = load_pcb_for_routing(str(board_path), strategy="grid")
        route_cmd._apply_detour_ratio_report(router2, SimpleNamespace(report_detour_ratio=False))
        assert router2.on_probe_elapsed is None

    def test_cli_run_output_bytes_identical_under_binding_timeout(
        self, board_path: Path, tmp_path: Path, monkeypatch
    ) -> None:
        """End-to-end, IN-PROCESS: binding ``--timeout``, slow probe, same bytes.

        Drives ``route_deadline._in_process_main`` directly.  The public
        ``route_cmd.main`` path supervises every finite positive
        ``--timeout`` in a child process where this module's
        parent-process patch cannot apply -- driving the supervisor's
        in-process entry keeps the slow probe inside the very process
        whose deadline stamps must absorb it.  The flagged arm's probe
        sleeps ``_SLOW_PROBE_SLEEP_S`` vs ``--timeout _STAGE_TIMEOUT_S``:
        on pre-#9244 main (no re-base, no hook) the sleep spends the
        budget and the copper differs; with the shift both arms route
        identically.  Measured limit: with the driver re-base in place,
        neutralizing only the CLI hook does NOT change this board's
        copper (a single-attempt basic run never consults the CLI stamps
        for copper); the hook's shift mechanics are pinned by
        ``test_apply_detour_ratio_report_shifts_deadlines_by_probe_elapsed``.

        Guards, observable from the test process (a silently fast probe
        would make the identity vacuous, not failing): exactly one probe
        pre-pass runs, its recorded ``detour_probe_elapsed_s`` is at
        least the injected sleep, and the flagged sidecar reports the
        probed connections while the unflagged arm writes none.
        """
        from kicad_tools.cli.route_cmd import _in_process_main

        probe_elapsed_seen: list[float] = []
        real_probe = Autorouter._probe_alone_on_empty_lengths

        def probe_with_elapsed_capture(self, *args, **kwargs):
            real_probe(self, *args, **kwargs)
            # Only a completed pre-pass records (the flag-off arm early-
            # returns with ``detour_probe_records`` still None).
            if self.detour_probe_records is not None:
                probe_elapsed_seen.append(self.detour_probe_elapsed_s)

        monkeypatch.setattr(Autorouter, "_probe_alone_on_empty_lengths", probe_with_elapsed_capture)
        _slow_probe_route_net(monkeypatch, _SLOW_PROBE_SLEEP_S)

        out_on = tmp_path / "on.kicad_pcb"
        out_off = tmp_path / "off.kicad_pcb"
        common = [
            "--strategy",
            "basic",
            "--skip-drc",
            "--seed",
            "7",
            "--timeout",
            str(_STAGE_TIMEOUT_S),
        ]

        rc_on = _in_process_main(
            [str(board_path), "-o", str(out_on), *common, "--report-detour-ratio"]
        )

        # Guard: the slow probe demonstrably fired in THIS process -- one
        # pre-pass, measured elapsed >= the injected sleep.
        assert len(probe_elapsed_seen) == 1
        assert probe_elapsed_seen[0] >= _SLOW_PROBE_SLEEP_S

        rc_off = _in_process_main([str(board_path), "-o", str(out_off), *common])

        # The unflagged arm ran no probe pre-pass at all.
        assert len(probe_elapsed_seen) == 1
        assert rc_on == rc_off == 0

        assert out_on.exists() and out_off.exists()
        sidecar = tmp_path / "on.detour_ratio.json"
        assert sidecar.exists() and not (tmp_path / "off.detour_ratio.json").exists()
        payload = json.loads(sidecar.read_text())
        assert payload["summary"]["probed"] >= 4
        assert out_on.read_bytes() == out_off.read_bytes()


class TestInstalledDeadlineSuspended:
    """Probe searches must not be clamped by an installed wall deadline."""

    def test_probe_searches_see_no_installed_deadline(self, board_path: Path, monkeypatch) -> None:
        """The probe pre-pass runs under ``wall_deadline.suspended()``.

        ``clamp_search_timeout`` is the choke point both A* entry points
        consult; an installed excursion deadline would clamp every probe
        search to its remaining seconds, and a clamped probe measures
        different alone-on-empty lengths (a slower-machine artifact in the
        report).  A spy on ``route_net`` records whether the process-wide
        deadline is active at each probe search -- with the suspension it
        never is (pre-#9244 main sees it active and fails here).
        """
        from kicad_tools.router.io import load_pcb_for_routing

        real_route_net = Autorouter.route_net
        deadline_active_during_probe: list[bool] = []

        def spying_route_net(self, net, *args, **kwargs):
            if getattr(self, "_detour_probe_active", False):
                deadline_active_during_probe.append(wall_deadline.active())
            return real_route_net(self, net, *args, **kwargs)

        monkeypatch.setattr(Autorouter, "route_net", spying_route_net)

        router, _ = load_pcb_for_routing(str(board_path), strategy="grid")
        router.report_detour_ratio = True

        with wall_deadline.deadline(30.0):  # active for the whole route
            router.route_all(suppress_no_timeout_warning=True)

        assert router.detour_probe_records
        assert deadline_active_during_probe, "no probe search ran"
        assert not any(deadline_active_during_probe), (
            "an installed wall deadline was visible to probe searches"
        )


class TestPerTierBudgetRestore:
    """``per_tier_routing_budget`` folds probe shifts into its restore."""

    def test_restore_keeps_shift_grown_inside_the_block(self) -> None:
        from kicad_tools.cli.mfr_tier_budget import per_tier_routing_budget

        now = time.monotonic()
        args = SimpleNamespace(_routing_deadline=now + 100.0, _detour_probe_shift_s=1.0)
        original = args._routing_deadline

        with per_tier_routing_budget(args, tier_index=0, tier_count=2):
            assert args._routing_deadline < original  # narrowed inside
            # Exactly what the probe hook does when a probe runs inside:
            args._detour_probe_shift_s += 5.0
            args._routing_deadline += 5.0

        # Restored original PLUS the 5 s that grew inside -- not the bare
        # original (which would re-count the probe against later tiers),
        # not the narrowed value, and the pre-block 1.0 s is not re-added.
        assert args._routing_deadline == pytest.approx(original + 5.0)

    def test_restore_without_probe_shift_is_bare_original(self) -> None:
        from kicad_tools.cli.mfr_tier_budget import per_tier_routing_budget

        now = time.monotonic()
        args = SimpleNamespace(_routing_deadline=now + 100.0)
        original = args._routing_deadline

        with per_tier_routing_budget(args, tier_index=0, tier_count=2):
            pass

        assert args._routing_deadline == pytest.approx(original)
