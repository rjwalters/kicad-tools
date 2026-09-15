"""Issue #5333: the coupled diff-pair phase is still wall-clock governed.

``--deterministic-budget`` prints, at the top of every run::

    per-net wall-clock cutoff DISABLED, C++ A* iteration backstop pinned to
    12,000,000 node expansions.  Routed output is reproducible across machines.

That contract (#3538/#3881) was only ever implemented for the SINGLE-ENDED
per-net A*.  ``DiffPairRouter.route_all_with_diffpairs`` keeps two wall-clock
cutoffs of its own -- the per-pair budget (#3089/#3321) and the aggregate
coupled-phase budget (#3439) -- and the flag does not touch either.  So under
a flag that promises a machine-independent route, *which pairs qualify as
coupled* remains a function of machine speed and load.

Measured on Board07's committed regression fixture (seed 42, native ABI 31,
identical source and argv, one loaded host):

===========================  ==========================
per-pair wall budget         pairs qualified coupled-ok
===========================  ==========================
60 s (derived from timeout)  3/7, then 4/7 on a re-run
300 s (explicit)             6/7
===========================  ==========================

Only the wall allowance differed, so those were budget exits rather than
physical rejections -- and a "7/7 qualified" count measured on a fast idle
host does not transfer to a busy one.

These tests pin the warning that makes that falsifiable.  They deliberately do
NOT assert any change to budgets or routing: retiring the cutoffs in favour of
the phase's existing deterministic ledgers needs a measured full-recipe cost
comparison first, because the hard total the recipe grants the whole board is
shared with the single-ended remainder.
"""

from __future__ import annotations

import logging

from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair import DifferentialPairConfig
from kicad_tools.router.rules import DesignRules, NetClassRouting

_MARKER = "DIFFPAIR_NONDETERMINISTIC_BUDGET"


def _opt_in_diffpair_class_map(net_names: list[str]) -> dict[str, NetClassRouting]:
    nc = NetClassRouting(name="HighSpeedOptIn", coupled_routing=True)
    return dict.fromkeys(net_names, nc)


def _two_pad_diffpair_router() -> Autorouter:
    """30x10mm board with one straight two-pad diff pair plus one plain net.

    Same fixture as ``tests/test_diffpair_budget_exit_warning.py`` so the
    budget surfaces are exercised identically.
    """
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, grid_resolution=0.1)
    router = Autorouter(
        width=30.0,
        height=10.0,
        rules=rules,
        net_class_map=_opt_in_diffpair_class_map(["USB_D+", "USB_D-"]),
    )
    for name, x in (("U1", 5.0), ("J1", 25.0)):
        router.add_component(
            name,
            [
                {
                    "number": "1",
                    "x": x,
                    "y": 4.6,
                    "width": 0.4,
                    "height": 0.4,
                    "net": 1,
                    "net_name": "USB_D+",
                },
                {
                    "number": "2",
                    "x": x,
                    "y": 5.4,
                    "width": 0.4,
                    "height": 0.4,
                    "net": 2,
                    "net_name": "USB_D-",
                },
                {
                    "number": "3",
                    "x": x,
                    "y": 8.0,
                    "width": 0.4,
                    "height": 0.4,
                    "net": 3,
                    "net_name": "GPIO1",
                },
            ],
        )
    return router


def _warnings_for(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if _MARKER in r.getMessage()]


class TestWarningFires:
    def test_aggregate_wall_budget_under_deterministic_budget_warns(self, caplog):
        """An aggregate wall cutoff is announced as machine-dependent."""
        router = _two_pad_diffpair_router()
        config = DifferentialPairConfig(enabled=True, spacing=0.8, deterministic_budget=True)

        with caplog.at_level(logging.WARNING):
            router._diffpair.route_all_with_diffpairs(config, aggregate_timeout=0.001)

        messages = _warnings_for(caplog)
        assert messages, (
            "--deterministic-budget with a live aggregate coupled-phase wall "
            "budget must announce that pair qualification is still "
            f"machine-dependent; saw {[r.getMessage() for r in caplog.records]}"
        )
        assert "aggregate 0.0s" in messages[0], messages[0]

    def test_per_pair_wall_budget_under_deterministic_budget_warns(self, caplog):
        """A per-pair wall cutoff alone is enough to trigger the warning."""
        router = _two_pad_diffpair_router()
        config = DifferentialPairConfig(enabled=True, spacing=0.8, deterministic_budget=True)

        with caplog.at_level(logging.WARNING):
            router._diffpair.route_all_with_diffpairs(config, per_pair_timeout=30.0)

        messages = _warnings_for(caplog)
        assert messages, "a per-pair wall budget must trigger the warning on its own"
        assert "per-pair 30.0s" in messages[0], messages[0]
        assert "aggregate none" in messages[0], messages[0]


class TestWarningStaysSilent:
    def test_no_warning_without_deterministic_budget(self, caplog):
        """The default (legacy) run is not claiming machine-independence."""
        router = _two_pad_diffpair_router()
        config = DifferentialPairConfig(enabled=True, spacing=0.8)

        with caplog.at_level(logging.WARNING):
            router._diffpair.route_all_with_diffpairs(config, aggregate_timeout=0.001)

        assert not _warnings_for(caplog), (
            "the warning is specifically about the --deterministic-budget "
            "contract; a run that never set the flag must stay silent"
        )

    def test_no_warning_when_no_wall_budget_is_active(self, caplog):
        """With both cutoffs off there is no contract mismatch to report."""
        router = _two_pad_diffpair_router()
        config = DifferentialPairConfig(enabled=True, spacing=0.8, deterministic_budget=True)

        with caplog.at_level(logging.WARNING):
            router._diffpair.route_all_with_diffpairs(config)

        assert not _warnings_for(caplog), (
            "no per-pair and no aggregate wall cutoff means the coupled "
            "phase really is iteration-governed -- nothing to warn about"
        )

    def test_zero_per_pair_budget_is_not_a_wall_budget(self, caplog):
        """``0`` disables the cutoff (same convention as the budget code)."""
        router = _two_pad_diffpair_router()
        config = DifferentialPairConfig(enabled=True, spacing=0.8, deterministic_budget=True)

        with caplog.at_level(logging.WARNING):
            router._diffpair.route_all_with_diffpairs(config, per_pair_timeout=0.0)

        assert not _warnings_for(caplog)


class TestReportOnly:
    def test_flag_changes_no_routing_outcome(self):
        """The flag is instrumentation: identical runs route identically.

        Both runs use the same near-zero aggregate budget, so the pair
        budget-exits to the single-ended fallback either way.  If the flag
        ever starts changing budgets, this is the guard that notices.
        """
        results = []
        for deterministic in (False, True):
            router = _two_pad_diffpair_router()
            config = DifferentialPairConfig(
                enabled=True, spacing=0.8, deterministic_budget=deterministic
            )
            routes, _warnings = router._diffpair.route_all_with_diffpairs(
                config, aggregate_timeout=0.001
            )
            results.append(
                (
                    sorted(r.net for r in routes),
                    list(router._diffpair._last_budget_exit_pair_names),
                    router._diffpair._last_coupled_attempted_count,
                )
            )
        assert results[0] == results[1], (
            "--deterministic-budget must not change any routing decision in "
            f"the coupled phase; saw {results[0]} vs {results[1]}"
        )

    def test_default_config_is_off(self):
        assert DifferentialPairConfig().deterministic_budget is False


class TestCliPlumbing:
    def test_build_diffpair_config_forwards_the_flag(self):
        """``--deterministic-budget`` reaches the coupled phase's config.

        Without this the warning could never fire from a real CLI run --
        which is exactly how the contract mismatch stayed invisible.
        """
        from argparse import Namespace

        from kicad_tools.cli.route_cmd import _build_diffpair_config

        args = Namespace(
            differential_pairs=True,
            diffpair_spacing=None,
            diffpair_max_delta=None,
            diffpair_per_pair_timeout=None,
            deterministic_budget=True,
        )
        config = _build_diffpair_config(args)
        assert config is not None
        assert config.deterministic_budget is True

    def test_build_diffpair_config_defaults_off(self):
        from argparse import Namespace

        from kicad_tools.cli.route_cmd import _build_diffpair_config

        args = Namespace(
            differential_pairs=True,
            diffpair_spacing=None,
            diffpair_max_delta=None,
            diffpair_per_pair_timeout=None,
            deterministic_budget=False,
        )
        config = _build_diffpair_config(args)
        assert config is not None
        assert config.deterministic_budget is False
