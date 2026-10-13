"""Fast checks for the lattice manufacturable guard's gates and budgets (#6463).

The guard itself (``tests/test_lattice_boards_manufacturable_6449.py``) is
``slow``; these run in the default suite and prove that a board which is not
manufacturable fails it, and that each case's inner budget ends before
pytest-timeout would kill it.
"""

from __future__ import annotations

import pytest

from tests import test_lattice_boards_manufacturable_6449 as guard

GOOD_ROW = {
    "router_nets": "9/9",
    "exit_code": 0,
    "signal_nets_unfinished": 0,
    "kct_check_errors": 0,
    "kicad_drc_errors": 0,
    "kicad_drc_unconnected": 0,
    "artifact": "final",
    "manufacturable": True,
}


def test_good_row_passes():
    guard.assert_row_manufacturable(dict(GOOD_ROW), "9/9")


@pytest.mark.parametrize(
    "field,bad",
    [
        ("router_nets", "8/9"),
        ("exit_code", 2),
        ("signal_nets_unfinished", 1),
        ("kct_check_errors", 3),
        ("kicad_drc_errors", 1),
        ("kicad_drc_unconnected", 2),
        ("artifact", "deadline"),
        ("manufacturable", False),
        # No kicad-cli: the harness reports the DRC gates as None (#6420).
        ("kicad_drc_errors", None),
        ("kicad_drc_unconnected", None),
        ("manufacturable", None),
    ],
)
def test_non_manufacturable_row_fails(field, bad):
    row = dict(GOOD_ROW, **{field: bad})
    with pytest.raises(AssertionError) as exc:
        guard.assert_row_manufacturable(row, "9/9")
    # The summary dict names the failing gate's value.
    assert repr(field) in str(exc.value)


def test_unconnected_is_asserted_even_if_manufacturable_claims_true():
    row = dict(GOOD_ROW, kicad_drc_unconnected=1)
    with pytest.raises(AssertionError):
        guard.assert_row_manufacturable(row, "9/9")


def _case(case_id):
    return next(p for p in guard.CASES if p.id == case_id)


def _pytest_timeout(param):
    for mark in param.marks:
        if mark.name == "timeout":
            return mark.args[0]
    return guard.SLOW_JOB_PYTEST_TIMEOUT_S


@pytest.mark.parametrize("case_id", ["board04-2L-6451", "board05-4L-6449"])
def test_inner_budget_expires_before_pytest_timeout(case_id):
    param = _case(case_id)
    route_timeout_s = param.values[3]
    assert route_timeout_s + guard.SUBPROCESS_GRACE_S < _pytest_timeout(param)


def test_only_board05_is_opt_in():
    def skip_marks(param):
        return [m for m in param.marks if m.name == "skipif"]

    assert skip_marks(_case("board04-2L-6451")) == []
    (mark,) = skip_marks(_case("board05-4L-6449"))
    assert "KCT_LATTICE_BOARD_GUARD" in mark.kwargs["reason"]
