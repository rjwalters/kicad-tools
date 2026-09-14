"""Advisory lock marker policy and writer integration regressions."""

import os

import pytest

from kicad_tools.core.project_file import save_project
from kicad_tools.core.sexp_file import save_design_rules, save_footprint, save_pcb, save_schematic
from kicad_tools.sexp import parse_string

WRITERS = [
    (save_pcb, "(kicad_pcb)", "kicad_pcb"),
    (save_schematic, "(kicad_sch)", "kicad_sch"),
    (save_footprint, '(footprint "x")', "kicad_mod"),
    (save_design_rules, "(design_rules (version 1))", "kicad_dru"),
    (save_project, None, "kicad_pro"),
]


@pytest.mark.parametrize("writer,sexp,extension", WRITERS)
@pytest.mark.parametrize("policy", ["warn", "error", "ignore"])
def test_all_core_writers(tmp_path, monkeypatch, capsys, writer, sexp, extension, policy):
    path = tmp_path / f"my board.rev2.{extension}"
    path.write_bytes(b"original")
    marker = path.with_name("~" + path.name + ".lck")
    marker.write_text('{"username":"alice","hostname":"workstation"}')
    original_marker = marker.read_bytes()
    monkeypatch.setenv("KCT_KICAD_LOCK_POLICY", policy)
    data = parse_string(sexp) if sexp else {"new": True}
    if policy == "error":
        with pytest.raises(PermissionError, match="may be open in KiCad"):
            writer(data, path)
        assert path.read_bytes() == b"original"
    else:
        writer(data, path)
        assert path.read_bytes() != b"original"
    output = capsys.readouterr()
    assert not output.out
    assert ("may be open in KiCad" in output.err) == (policy == "warn")
    assert marker.read_bytes() == original_marker
    assert not path.with_suffix(path.suffix + ".tmp").exists()


@pytest.mark.parametrize(
    "content,owner",
    [
        ('{"username":"alice","hostname":"host"}', ("alice", "host")),
        ("", (None, None)),
        ("not-json PRIVATE", (None, None)),
        ("[]", (None, None)),
        ('{"username":12,"hostname":false}', (None, None)),
        ('{"username":"","hostname":"host"}', ("", "host")),
    ],
)
def test_probe_metadata(tmp_path, content, owner):
    from kicad_tools.core.kicad_lock import probe_kicad_lock

    path = tmp_path / "board.kicad_pcb"
    marker = tmp_path / "~board.kicad_pcb.lck"
    marker.write_text(content)
    presence = probe_kicad_lock(path)
    assert presence.present and presence.path == marker
    assert (presence.username, presence.hostname) == owner
    assert marker.read_text() == content


def test_unreadable_marker_is_present(tmp_path, monkeypatch):
    from kicad_tools.core.kicad_lock import probe_kicad_lock

    path = tmp_path / "board.kicad_pcb"
    marker = tmp_path / "~board.kicad_pcb.lck"
    marker.touch()
    real = os.open

    def denied(self, *args, **kwargs):
        if self == marker:
            raise PermissionError("denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(os, "open", denied)
    presence = probe_kicad_lock(path)
    assert presence.present and presence.username is None


@pytest.mark.parametrize(
    "name", ["board.kicad_pcb.lck", ".board.kicad_pcb.lck", "~other.kicad_pcb.lck"]
)
def test_unrelated_markers_do_not_warn(tmp_path, capsys, name):
    from kicad_tools.core.kicad_lock import check_kicad_lock

    (tmp_path / name).touch()
    assert not check_kicad_lock(tmp_path / "board.kicad_pcb").present
    assert capsys.readouterr() == ("", "")


def test_policy_precedence_and_invalid_values(tmp_path, monkeypatch):
    from kicad_tools.core.kicad_lock import check_kicad_lock

    path = tmp_path / "board.kicad_pcb"
    (tmp_path / "~board.kicad_pcb.lck").touch()
    monkeypatch.setenv("KCT_KICAD_LOCK_POLICY", "error")
    assert check_kicad_lock(path, policy="ignore") is None
    monkeypatch.setenv("KCT_KICAD_LOCK_POLICY", "invalid")
    with pytest.raises(ValueError, match="expected warn, error or ignore"):
        check_kicad_lock(path)
    assert check_kicad_lock(path, policy="ignore") is None
    with pytest.raises(ValueError):
        check_kicad_lock(path, policy="")


def test_default_warning_and_json_stdout(tmp_path, monkeypatch, capsys):
    import json

    from kicad_tools.cli.sch_json import run_with_json_summary

    monkeypatch.delenv("KCT_KICAD_LOCK_POLICY", raising=False)
    path = tmp_path / "board.kicad_sch"
    (tmp_path / "~board.kicad_sch.lck").write_text("MALFORMED PRIVATE")
    result = run_with_json_summary(
        "test-save",
        path,
        lambda: save_schematic(parse_string("(kicad_sch)"), path),
        output_format="json",
    )
    output = capsys.readouterr()
    assert result == 0 and json.loads(output.out)["success"] is True
    assert "may be open in KiCad" in output.err
    assert "MALFORMED PRIVATE" not in output.err


@pytest.mark.parametrize("policy", ["warn", "error", "ignore"])
@pytest.mark.parametrize("partial", [False, True])
def test_routing_writers(tmp_path, monkeypatch, capsys, policy, partial):
    from types import SimpleNamespace

    from kicad_tools.cli import route_cmd

    source = tmp_path / "source.kicad_pcb"
    source.write_text("(kicad_pcb (version 20240108))")
    output = tmp_path / "routed.kicad_pcb"
    actual = output.with_stem("routed_partial") if partial else output
    actual.write_bytes(b"original")
    marker = actual.with_name("~" + actual.name + ".lck")
    marker.touch()
    monkeypatch.setenv("KCT_KICAD_LOCK_POLICY", policy)
    if partial:
        router = SimpleNamespace(routes=[1], to_sexp=lambda **kwargs: "(segment (width 0.2))")
        monkeypatch.setattr(
            route_cmd,
            "_interrupt_state",
            {"router": router, "output_path": output, "pcb_path": source, "quiet": True},
        )
        assert route_cmd._save_partial_results() == (policy != "error")
    elif policy == "error":
        with pytest.raises(PermissionError):
            route_cmd._write_routed_pcb(source, output, "")
    else:
        assert route_cmd._write_routed_pcb(source, output, "") == output
    assert (actual.read_bytes() == b"original") == (policy == "error")
    assert not actual.with_suffix(actual.suffix + ".tmp").exists()
    assert marker.read_bytes() == b""
    captured = capsys.readouterr()
    assert ("may be open in KiCad" in captured.err) == (policy == "warn")


@pytest.mark.parametrize("completed", [False, True])
def test_partial_uses_actual_destination_only(tmp_path, monkeypatch, completed):
    from types import SimpleNamespace

    from kicad_tools.cli import route_cmd

    source = tmp_path / "source.kicad_pcb"
    source.write_text("(kicad_pcb (version 20240108))")
    output = tmp_path / "output.kicad_pcb"
    actual = output if completed else output.with_stem("output_partial")
    # Lock the input, and (unless canonical is the actual output) the intended output.
    source.with_name("~" + source.name + ".lck").touch()
    if not completed:
        output.with_name("~" + output.name + ".lck").touch()
    monkeypatch.setenv("KCT_KICAD_LOCK_POLICY", "error")
    router = SimpleNamespace(routes=[1], to_sexp=lambda **kwargs: "(segment (width 0.2))")
    monkeypatch.setattr(
        route_cmd,
        "_interrupt_state",
        {
            "router": router,
            "output_path": output,
            "pcb_path": source,
            "quiet": True,
            "best_completed_attempt": completed,
        },
    )
    assert route_cmd._save_partial_results() is True
    before = actual.read_bytes()
    actual.with_name("~" + actual.name + ".lck").touch()
    assert route_cmd._save_partial_results() is False
    assert actual.read_bytes() == before


@pytest.mark.parametrize("kind", ["fifo", "directory", "symlink"])
def test_nonregular_markers_are_present_without_open(tmp_path, monkeypatch, kind):
    from kicad_tools.core.kicad_lock import probe_kicad_lock

    path = tmp_path / "board.kicad_pcb"
    marker = tmp_path / "~board.kicad_pcb.lck"
    if kind == "fifo":
        os.mkfifo(marker)
    elif kind == "directory":
        marker.mkdir()
    else:
        marker.symlink_to(tmp_path / "absent")

    def unexpected(*args, **kwargs):
        pytest.fail("Nonregular marker must not be opened")

    monkeypatch.setattr(os, "open", unexpected)
    presence = probe_kicad_lock(path)
    assert presence.present and presence.username is None


@pytest.mark.parametrize("writer,sexp,extension", WRITERS)
def test_absent_marker_success(tmp_path, capsys, writer, sexp, extension):
    path = tmp_path / ("board." + extension)
    writer(parse_string(sexp) if sexp else {"value": True}, path)
    assert path.exists()
    assert capsys.readouterr() == ("", "")


def test_invalid_policy_preserves_destination_and_temp(tmp_path, monkeypatch):
    path = tmp_path / "board.kicad_pcb"
    path.write_bytes(b"original")
    monkeypatch.setenv("KCT_KICAD_LOCK_POLICY", "WARN")
    with pytest.raises(ValueError, match="Invalid KCT_KICAD_LOCK_POLICY"):
        save_pcb(parse_string("(kicad_pcb)"), path)
    assert path.read_bytes() == b"original"
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_control_characters_in_metadata_and_paths_are_escaped(tmp_path, capsys):
    import json

    from kicad_tools.core.kicad_lock import check_kicad_lock

    path = tmp_path / "board\n\x1b[31m.kicad_pcb"
    marker = path.with_name("~" + path.name + ".lck")
    marker.write_text(json.dumps({"username": "\x1b[31m", "hostname": "host\nsecond"}))
    check_kicad_lock(path)
    stderr = capsys.readouterr().err
    assert "\x1b" not in stderr
    assert stderr.count("\n") == 1


def test_deeply_malformed_json_remains_present(tmp_path):
    from kicad_tools.core.kicad_lock import probe_kicad_lock

    path = tmp_path / "board.kicad_pcb"
    path.with_name("~" + path.name + ".lck").write_text("[" * 1500)
    assert probe_kicad_lock(path).present
