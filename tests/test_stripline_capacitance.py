"""Independent electromagnetic references for offset stripline (#5016)."""

import math

import pytest

from kicad_tools.physics.stripline import stripline_impedance


@pytest.mark.parametrize(
    ("width", "expected"),
    [(0.05, 111.107350), (0.2, 72.138691), (0.5, 47.344311), (1, 30.807994), (2, 18.186467)],
)
def test_centered_thin_strip_matches_exact_conformal_solution(width, expected):
    # Exact thin centered strip: eta0/(4 sqrt(Er)) K(k')/K(k),
    # k=tanh(pi*w/(2*H)); H=1, Er=4.5. Constants computed independently
    # from elliptic integrals, not from panel geometry or fitted IPC formulas.
    assert stripline_impedance(width, 0.5, 0.5, 0, 4.5) == pytest.approx(expected, rel=1e-4)


def test_offset_strip_matches_independent_finite_volume_solution():
    # Independent volume discretization with grounded planes and an actual
    # rectangular conductor extrapolates to 50.25155 ohm (Er=4.5). Three mesh sizes
    # and lateral-domain verification are archived in docs/investigations/stripline-5016.
    # The former total-height formula gives ~71.7 ohm for this board07 case.
    assert stripline_impedance(0.16, 0.13, 1.078, 0.0152, 4.5) == pytest.approx(50.2, abs=0.15)


def test_centered_thick_strip_matches_independent_finite_volume_solution():
    assert stripline_impedance(0.16, 0.13, 0.13, 0.0152, 4.5) == pytest.approx(38.9, abs=0.15)


def test_second_centered_geometry_matches_finite_volume_solution():
    assert stripline_impedance(0.2, 0.2, 0.2, 0.035, 4.5) == pytest.approx(42.10279, abs=0.05)


def test_offset_reflection_and_geometric_scale_invariance():
    expected = stripline_impedance(0.16, 0.13, 1.078, 0.0152, 4.5)
    assert stripline_impedance(0.16, 1.078, 0.13, 0.0152, 4.5) == expected
    for scale in [0.001, 1000]:
        assert stripline_impedance(
            0.16 * scale, 0.13 * scale, 1.078 * scale, 0.0152 * scale, 4.5
        ) == pytest.approx(expected, rel=1e-9)


def test_homogeneous_permittivity_scaling():
    vacuum = stripline_impedance(0.16, 0.13, 1.078, 0.0152, 1)
    assert stripline_impedance(0.16, 0.13, 1.078, 0.0152, 4.5) == pytest.approx(
        vacuum / math.sqrt(4.5), rel=1e-12
    )


def test_remote_plane_approaches_finite_single_plane_limit():
    close = stripline_impedance(0.16, 0.13, 1, 0.0152, 4.5)
    far = stripline_impedance(0.16, 0.13, 100, 0.0152, 4.5)
    farther = stripline_impedance(0.16, 0.13, 1000, 0.0152, 4.5)
    assert close < far < farther < 51
    assert farther == pytest.approx(far, abs=0.001)


def test_no_artificial_ten_ohm_clamp_on_wide_trace():
    # Parallel-plate leading term with edge fringing lowering Z further.
    width, gap, er = 10, 0.1, 4
    parallel_plate = 376.730313668 * gap / (2 * width * math.sqrt(er))
    result = stripline_impedance(width, gap, gap, 0.01, er)
    assert 0 < result < parallel_plate < 10


@pytest.mark.parametrize(
    "values",
    [
        (0, 0.1, 0.2, 0.01, 4),
        (0.1, 0, 0.2, 0.01, 4),
        (0.1, 0.2, 0.3, -0.01, 4),
        (0.1, 0.2, 0.3, 0.01, 0),
        (0.1, 0.2, float("inf"), 0.01, 4),
        (0.1, 0.2, 0.3, float("nan"), 4),
    ],
)
def test_invalid_geometry_fails_instead_of_returning_plausible_impedance(values):
    with pytest.raises(ValueError):
        stripline_impedance(*values)


def test_coupled_stripline_uses_correct_offset_single_line_limit():
    from kicad_tools.physics import Stackup
    from kicad_tools.physics.coupled_lines import CoupledLines

    result = CoupledLines(Stackup.jlcpcb_4layer())._edge_coupled_stripline_calc(
        0.16, 10, 0.13, 1.078, 4.5, 0.0152
    )
    # The empirical model retains a 1% coupling floor, but must approach
    # twice the independently verified single-line impedance, not ~150 ohm.
    assert result.zdiff == pytest.approx(2 * 50.25155, rel=0.011)
