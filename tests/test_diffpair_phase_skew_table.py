"""Phase-derived skew table: the identities the research note relies on (Issue #7883).

``scripts/research/diffpair_phase_skew_table.py`` generates the proposed limit
table of ``docs/research/diffpair-phase-skew-tolerance.md``.  It changes no
limit; these tests pin the arithmetic the note quotes so a reader can trust it.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "research" / "diffpair_phase_skew_table.py"


def _load():
    spec = importlib.util.spec_from_file_location("diffpair_phase_skew_table_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


table = _load()


def test_minus_30_db_is_ui_over_50_at_nyquist():
    """-30 dB mode conversion at Nyquist is TI's UI/50 MIPI estimate."""
    dphi = table.dphi_max_rad(-30.0)
    assert math.degrees(dphi) == pytest.approx(3.624, abs=1e-3)
    # At f = 1/(2 UI), dt = dphi / (2 pi f) = dphi * UI / pi.
    assert dphi / math.pi == pytest.approx(1 / 50, rel=0.01)


def test_issue_worked_example():
    """0.5 mm at eps_eff 3.0, 2.5 GHz: 2.9 ps, 2.6 deg, -33 dB, ~0.002 dB."""
    dt_ps = 0.5 * math.sqrt(3.0) / table.C_MM_PER_PS
    dphi = 2 * math.pi * 2.5 * dt_ps * 1e-3
    assert dt_ps == pytest.approx(2.89, abs=0.01)
    assert math.degrees(dphi) == pytest.approx(2.60, abs=0.01)
    assert table.scd21_db(dphi) == pytest.approx(-32.9, abs=0.1)
    assert -20 * math.log10(math.cos(dphi / 2)) == pytest.approx(0.0022, abs=1e-4)


def test_mode_conversion_binds_before_differential_loss():
    """A 1 dB differential-loss budget would already mean -6.9 dB conversion."""
    dphi = 2 * math.acos(10 ** (-1 / 20))
    assert table.scd21_db(dphi) == pytest.approx(-6.87, abs=0.01)
    at_tol = table.dphi_max_rad(-30.0)
    assert -20 * math.log10(math.cos(at_tol / 2)) < 0.005


def test_knee_coefficient_is_single_pole_bandwidth():
    assert pytest.approx(0.3497, abs=1e-4) == table.KNEE_COEFF


def test_table_covers_every_pair():
    """Every pair on 03/06/06rf/07rf gets a row; board 07 has none."""
    rows, notes = table.build_rows()
    by_board: dict[str, int] = {}
    for r in rows:
        by_board[r.board] = by_board.get(r.board, 0) + 1
        assert r.recommended_mm > 0
        assert r.today_mm > 0
    assert by_board == {"03": 2, "06": 4, "06rf": 9, "07rf": 7}
    assert any(n.startswith("07: 0 diff pairs") for n in notes)
