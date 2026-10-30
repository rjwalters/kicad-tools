"""Issue #8156: a non-zero route names the ``*_unverified_*`` sets it left behind."""

from __future__ import annotations

from kicad_tools.cli.route_cmd import _list_unverified_sets


def test_lists_only_this_stems_unverified_sets(tmp_path, capsys):
    out = tmp_path / "board.kicad_pcb"
    out.write_text("")
    mine = tmp_path / "board_checkpoint_unverified_abc.kicad_pcb"
    done = tmp_path / "board_completed_unverified_xyz.kicad_pcb"
    other = tmp_path / "other_checkpoint_unverified_q.kicad_pcb"
    for p in (mine, done, other):
        p.write_text("")
    found = _list_unverified_sets(out)
    assert found == sorted([mine, done])
    err = capsys.readouterr().err
    assert str(mine) in err and str(done) in err and str(other) not in err
    assert mine.exists() and done.exists()  # nothing deleted


def test_silent_when_none(tmp_path, capsys):
    out = tmp_path / "board.kicad_pcb"
    assert _list_unverified_sets(out) == []
    assert capsys.readouterr().err == ""
