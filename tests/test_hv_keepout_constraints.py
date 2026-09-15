"""HV keepout output must retain the constraints native refill consumes."""

import pytest

from kicad_tools.cli import zones_cmd
from tests.test_zones_hv_keepout import _BOARD


@pytest.mark.parametrize("in_place", [False, True])
@pytest.mark.parametrize("suffixes", [(), (".kicad_pro",), (".kicad_pro", ".kicad_dru")])
def test_constraints_present_before_refill(tmp_path, monkeypatch, in_place, suffixes):
    source = tmp_path / "source.kicad_pcb"
    source.write_text(_BOARD)
    output = source if in_place else tmp_path / "output.kicad_pcb"
    payloads = {
        ".kicad_pro": b'{"meta": {"custom": true}}\r\n',
        ".kicad_dru": b"(version 1)\r\n; authored constraint\r\n",
    }
    for suffix in suffixes:
        source.with_suffix(suffix).write_bytes(payloads[suffix])
    calls = []

    def refill(path, quiet):
        assert "keepout" in path.read_text()
        for suffix, content in payloads.items():
            sidecar = path.with_suffix(suffix)
            assert sidecar.exists() == (suffix in suffixes)
            if suffix in suffixes:
                assert sidecar.read_bytes() == content
        calls.append(path)
        return 0

    monkeypatch.setattr(zones_cmd, "_refill_after_keepout", refill)
    assert (
        zones_cmd.main(
            ["hv-keepout", str(source), "-o", str(output), "--clearance", "1.6", "--refill", "-q"]
        )
        == 0
    )
    assert calls == [output]
    if not in_place:
        assert source.read_text() == _BOARD
    for suffix in suffixes:
        assert source.with_suffix(suffix).read_bytes() == payloads[suffix]


@pytest.mark.parametrize("source_has_rules", [False, True])
def test_conflicting_destination_rejected_before_any_write(tmp_path, monkeypatch, source_has_rules):
    source, output = tmp_path / "source.kicad_pcb", tmp_path / "output.kicad_pcb"
    source.write_text(_BOARD)
    output.write_text("existing output")
    source.with_suffix(".kicad_pro").write_text("{}")
    if source_has_rules:
        source.with_suffix(".kicad_dru").write_text("(version 1)")
    output.with_suffix(".kicad_dru").write_text("unrelated rules")
    monkeypatch.setattr(zones_cmd, "_refill_after_keepout", lambda *_: pytest.fail("refill ran"))
    assert (
        zones_cmd.main(
            ["hv-keepout", str(source), "-o", str(output), "--clearance", "1.6", "--refill", "-q"]
        )
        != 0
    )
    assert output.read_text() == "existing output"
    assert output.with_suffix(".kicad_dru").read_text() == "unrelated rules"
    assert not output.with_suffix(".kicad_pro").exists()
    assert source.read_text() == _BOARD


@pytest.mark.parametrize("alias", [False, True])
def test_existing_identical_constraints_and_source_alias(tmp_path, alias):
    source, output = tmp_path / "source.kicad_pcb", tmp_path / "output.kicad_pcb"
    source.write_text(_BOARD)
    authored = source.with_suffix(".kicad_pro")
    authored.write_text("{}")
    destination = output.with_suffix(".kicad_pro")
    if alias:
        destination.symlink_to(authored)
    else:
        destination.write_bytes(authored.read_bytes())
    result = zones_cmd.main(
        ["hv-keepout", str(source), "-o", str(output), "--clearance", "1.6", "-q"]
    )
    assert (result != 0) == alias
    assert output.exists() != alias
    assert authored.read_text() == "{}"
    assert source.read_text() == _BOARD


def test_renamed_board_alias_rejected_before_refill(tmp_path, monkeypatch):
    source, output = tmp_path / "source.kicad_pcb", tmp_path / "renamed.kicad_pcb"
    source.write_text(_BOARD)
    for suffix in (".kicad_pro", ".kicad_dru"):
        source.with_suffix(suffix).write_bytes(b"authored bytes\r\n")
    output.symlink_to(source)
    monkeypatch.setattr(zones_cmd, "_refill_after_keepout", lambda *_: pytest.fail("refill ran"))
    assert (
        zones_cmd.main(
            ["hv-keepout", str(source), "-o", str(output), "--clearance", "1.6", "--refill", "-q"]
        )
        != 0
    )
    assert output.is_symlink()
    assert source.read_text() == _BOARD
    for suffix in (".kicad_pro", ".kicad_dru"):
        assert source.with_suffix(suffix).read_bytes() == b"authored bytes\r\n"
        assert not output.with_suffix(suffix).exists()
