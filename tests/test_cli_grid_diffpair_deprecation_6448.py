"""Issue #6448: grid ``--differential-pairs`` emits a one-shot deprecation warning."""

from __future__ import annotations

import argparse
import warnings

import pytest

from kicad_tools.cli.route_cmd import _build_diffpair_config


def _args(**kw) -> argparse.Namespace:
    base = {
        "differential_pairs": True,
        "route_engine": "grid",
        "diffpair_spacing": None,
        "diffpair_max_delta": None,
        "diffpair_per_pair_timeout": None,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def test_grid_warns_once_naming_replacement(capsys):
    args = _args()
    assert _build_diffpair_config(args) is not None
    assert _build_diffpair_config(args) is not None  # second dispatch site
    err = capsys.readouterr().err
    assert err.count("deprecated") == 1
    assert "--route-engine lattice --strategy basic --differential-pairs" in err


def test_default_engine_none_is_grid_and_warns(capsys):
    _build_diffpair_config(_args(route_engine=None))
    assert "deprecated" in capsys.readouterr().err


def test_lattice_is_silent(capsys):
    assert _build_diffpair_config(_args(route_engine="lattice")) is not None
    assert capsys.readouterr().err == ""


def test_no_differential_pairs_is_silent(capsys):
    assert _build_diffpair_config(_args(differential_pairs=False)) is None
    assert capsys.readouterr().err == ""


def test_library_prepass_emits_deprecation_warning():
    from kicad_tools.router import Autorouter

    router = Autorouter(width=10.0, height=10.0)
    with pytest.warns(DeprecationWarning, match="route_diffpair_prepass"):
        router.route_diffpair_prepass(None)
    # No config -> still a no-op returning the empty triple.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert router.route_diffpair_prepass(None) == ([], [], set())
