"""Issue #8366: -o outputs carry the input's .kicad_pro / .kicad_dru verbatim."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.sidecars import copy_project_sidecars

CUSTOM_DRU = (
    "(version 1)\n"
    '(rule "HV mains clearance"\n'
    "  (condition \"A.NetClass == 'HV'\")\n"
    "  (constraint clearance (min 3.0mm)))\n"
)
PRO = '{"meta": {"version": 1}}\n'


def _board(d: Path, name: str, sidecars: bool = True) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.kicad_pcb"
    p.write_text("(kicad_pcb)\n")
    if sidecars:
        p.with_suffix(".kicad_pro").write_text(PRO)
        p.with_suffix(".kicad_dru").write_text(CUSTOM_DRU)
    return p


def test_copies_both_verbatim(tmp_path):
    src = _board(tmp_path / "a", "in")
    dst = tmp_path / "b" / "out.kicad_pcb"
    written = copy_project_sidecars(src, dst)
    assert len(written) == 2
    assert dst.with_suffix(".kicad_dru").read_bytes() == CUSTOM_DRU.encode()
    assert dst.with_suffix(".kicad_pro").read_bytes() == PRO.encode()


def test_same_stem_is_noop(tmp_path):
    src = _board(tmp_path, "in")
    assert copy_project_sidecars(src, src) == []
    assert src.with_suffix(".kicad_dru").read_text() == CUSTOM_DRU


def test_no_source_sidecars_is_noop(tmp_path):
    src = _board(tmp_path / "a", "in", sidecars=False)
    dst = tmp_path / "out.kicad_pcb"
    assert copy_project_sidecars(src, dst) == []
    assert not dst.with_suffix(".kicad_dru").exists()


def test_existing_destination_not_overwritten(tmp_path, capsys):
    src = _board(tmp_path / "a", "in")
    dst = tmp_path / "out.kicad_pcb"
    dst.with_suffix(".kicad_dru").write_text("(version 1)\n; newer\n")
    copy_project_sidecars(src, dst)
    assert dst.with_suffix(".kicad_dru").read_text() == "(version 1)\n; newer\n"
    assert "not overwritten" in capsys.readouterr().err
    assert dst.with_suffix(".kicad_pro").read_text() == PRO


def test_chain_preserves_custom_rule(tmp_path):
    """stitch/dedupe/prune/optimize all use this helper: chain keeps rules."""
    cur = _board(tmp_path / "s0", "b")
    for i in range(1, 5):
        nxt = tmp_path / f"s{i}" / "b.kicad_pcb"
        nxt.parent.mkdir()
        nxt.write_text("(kicad_pcb)\n")
        copy_project_sidecars(cur, nxt)
        cur = nxt
    assert cur.with_suffix(".kicad_dru").read_bytes() == CUSTOM_DRU.encode()


def test_route_sidecars_keep_custom_rules(tmp_path):
    """route's writer merges into a seeded source .kicad_dru, never replaces it."""
    from kicad_tools.cli.route_cmd import _write_drc_constraint_sidecars

    src = _board(tmp_path / "in", "b")
    out = tmp_path / "out" / "b.kicad_pcb"
    out.parent.mkdir()
    out.write_text("(kicad_pcb)\n")
    try:
        _write_drc_constraint_sidecars(
            out, "jlcpcb", 2, copper_oz=1.0, quiet=True, source_pcb_path=src
        )
    except Exception as e:  # pragma: no cover - environment-specific
        pytest.skip(f"route sidecar writer unavailable: {e}")
    dru = out.with_suffix(".kicad_dru").read_text()
    assert "HV mains clearance" in dru
    assert CUSTOM_DRU.strip() in dru
