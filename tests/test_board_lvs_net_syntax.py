"""Read real KiCad pad net syntax without weakening the LVS vacuity guard."""

import pytest

from kicad_tools.lvs import board_lvs


def _write_pcb(tmp_path, binding):
    path = tmp_path / "board.kicad_pcb"
    path.write_text(
        f"""(kicad_pcb (version 20260101)
          (footprint "Connector" (layer "F.Cu")
            (property "Reference" "J1")
            (pad "1" smd rect (at 0 0) (size 1 1)
              (layers "F.Cu") {binding})))"""
    )
    return path


@pytest.mark.parametrize(
    ("binding", "name"),
    [('(net "JOY_X")', "JOY_X"), ('(net 7 "JOY_X")', "JOY_X"), ('(net "5")', "5")],
)
def test_named_pad_net_is_real_lvs_evidence(tmp_path, monkeypatch, binding, name):
    pcb = _write_pcb(tmp_path, binding)
    assert board_lvs._pcb_pin_to_net(pcb) == {("J1", "1"): name}
    monkeypatch.setattr(board_lvs, "_schematic_pin_to_net", lambda _: {("J1", "1"): name})
    result = board_lvs.compare_netlists("dummy.kicad_sch", pcb)
    assert result.clean
    assert not result.vacuous

    # Reading name-only syntax must not hide an actual net mismatch.
    monkeypatch.setattr(board_lvs, "_schematic_pin_to_net", lambda _: {("J1", "1"): "OTHER"})
    result = board_lvs.compare_netlists("dummy.kicad_sch", pcb)
    assert not result.clean
    assert not result.vacuous
    assert result.mismatches[0].pcb_net == name


@pytest.mark.parametrize("binding", ["", '(net 0 "")', "(net 7)", '(net "")'])
def test_unbound_pad_syntax_still_triggers_vacuity_guard(tmp_path, monkeypatch, binding):
    pcb = _write_pcb(tmp_path, binding)
    assert not board_lvs._pcb_pin_to_net(pcb)[("J1", "1")]
    monkeypatch.setattr(board_lvs, "_schematic_pin_to_net", lambda _: {("J1", "1"): "JOY_X"})
    result = board_lvs.compare_netlists("dummy.kicad_sch", pcb)
    assert not result.clean
    assert result.vacuous
    assert len(result.mismatches) == 1
