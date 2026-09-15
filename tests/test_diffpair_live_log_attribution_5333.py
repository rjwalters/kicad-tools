"""A pair's live-search assertion cannot borrow its predecessor's work."""

import pytest

from tests.test_diffpair_coupled_live_board07_5333 import (
    test_tmds_d1_corridor_stage_is_invoked_in_a_live_search as assert_tmds_d1_engaged,
)

PREVIOUS = """[coupled-construction] success=False corridor_attempts=1 corridor_iters=7
[coupled-pair-report] pair=TMDS_D0 class=coupled-ok coupled=True
"""
TARGET = "[coupled-pair-report] pair=TMDS_D1 class=joint-A*-plateau coupled=False\n"


def test_previous_pair_activity_cannot_prove_target_construction():
    with pytest.raises(AssertionError, match="no .* diagnostic found for TMDS_D1"):
        assert_tmds_d1_engaged(PREVIOUS + TARGET)


def test_target_own_construction_activity_is_accepted():
    assert_tmds_d1_engaged(
        PREVIOUS
        + "[coupled-construction] success=False corridor_attempts=2 corridor_iters=9\n"
        + TARGET
    )


def test_target_zero_activity_cannot_borrow_previous_nonzero():
    with pytest.raises(AssertionError, match="never invoked"):
        assert_tmds_d1_engaged(
            PREVIOUS
            + "[coupled-construction] success=False corridor_attempts=0 corridor_iters=0\n"
            + TARGET
        )
