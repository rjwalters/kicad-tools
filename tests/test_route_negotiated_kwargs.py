"""Guards for the shared ``route_all_negotiated`` kwargs builder (#7377 step 3).

Every ``Autorouter.route_all_negotiated`` call in ``route_cmd.py`` must take its
kwargs from ``_negotiated_kwargs`` so a new negotiated opt-in flag is wired once
rather than at seven hand-built call sites.
"""

from __future__ import annotations

import ast
import inspect
from argparse import Namespace
from pathlib import Path

from kicad_tools.cli import route_cmd
from kicad_tools.router import Autorouter

ROUTE_CMD = Path(route_cmd.__file__)


def _args(**overrides) -> Namespace:
    return Namespace(iterations=7, **overrides)


def test_helper_keys_are_route_all_negotiated_parameters() -> None:
    params = set(inspect.signature(Autorouter.route_all_negotiated).parameters)
    keys = set(route_cmd._negotiated_kwargs(_args(), timeout=1.0, checkpoint_callback=None))
    assert keys <= params, f"unknown route_all_negotiated kwargs: {sorted(keys - params)}"
    assert {"timeout", "checkpoint_callback", "max_iterations", "seed"} <= keys


def test_helper_forwards_per_site_values() -> None:
    cb = object()
    kwargs = route_cmd._negotiated_kwargs(
        _args(seed=3, early_stop_patience=0), timeout=12.5, checkpoint_callback=cb
    )
    assert kwargs["max_iterations"] == 7
    assert kwargs["timeout"] == 12.5
    assert kwargs["checkpoint_callback"] is cb
    assert kwargs["seed"] == 3
    assert kwargs["best_stall_patience"] is None


def test_multistart_trial_kwargs_derive_from_helper() -> None:
    args = _args(targeted_ripup=True, hierarchical=True)
    full = route_cmd._negotiated_kwargs(args, timeout=None, checkpoint_callback=None)
    trial = route_cmd._multistart_trial_kwargs(args)["negotiated_kwargs"]
    expected = {k: v for k, v in full.items() if k not in route_cmd._MULTISTART_TRIAL_EXCLUDED_KEYS}
    assert trial == expected
    assert "checkpoint_callback" not in trial
    assert trial["use_targeted_ripup"] is True


def test_no_route_all_negotiated_call_spells_kwargs_by_hand() -> None:
    tree = ast.parse(ROUTE_CMD.read_text())
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "route_all_negotiated"
    ]
    assert len(calls) >= 7
    for call in calls:
        hand_written = [k.arg for k in call.keywords if k.arg is not None]
        assert not hand_written, (
            f"route_all_negotiated at route_cmd.py:{call.lineno} spells "
            f"{hand_written} by hand; use **_negotiated_kwargs(...)"
        )
        assert any(
            k.arg is None
            and isinstance(k.value, ast.Call)
            and getattr(k.value.func, "id", None) == "_negotiated_kwargs"
            for k in call.keywords
        ), f"route_all_negotiated at route_cmd.py:{call.lineno} must splat _negotiated_kwargs"
