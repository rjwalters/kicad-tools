"""``.env`` loading and contact plumbing for submit-jlcpcb (#8859)."""

import io
import os

from kicad_tools.parts import jlc_env

SENTINEL = "SENTINEL-8859-xyz"


def test_parse_quotes_export_comments():
    text = "\n".join(
        [
            "# comment",
            "",
            "export JLCPCB_A=one",
            'JLCPCB_B="two words"',
            "JLCPCB_C='three'",
            "JLCPCB_D=",
            "JLCPCB_E=val # trailing",
            "noequals",
        ]
    )
    d = jlc_env.parse_env_text(text)
    assert d == {
        "JLCPCB_A": "one",
        "JLCPCB_B": "two words",
        "JLCPCB_C": "three",
        "JLCPCB_D": "",
        "JLCPCB_E": "val",
    }


def _project(tmp_path, env_text):
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / ".env").write_text(env_text)
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    return sub / "board.kicad_pcb"


def test_load_prefix_only_no_override_logs_names_only(tmp_path, monkeypatch):
    board = _project(tmp_path, f"JLCPCB_APP_ID={SENTINEL}\nJLCPCB_ACCESS_KEY=envfile\nOTHER=nope\n")
    monkeypatch.delenv("JLCPCB_APP_ID", raising=False)
    monkeypatch.setenv("JLCPCB_ACCESS_KEY", "shell")
    monkeypatch.delenv("OTHER", raising=False)
    buf = io.StringIO()
    keys = jlc_env.load_jlc_env(board, stream=buf)
    assert keys == ["JLCPCB_APP_ID"]
    assert os.environ["JLCPCB_APP_ID"] == SENTINEL
    assert os.environ["JLCPCB_ACCESS_KEY"] == "shell"
    assert "OTHER" not in os.environ
    log = buf.getvalue()
    assert str(tmp_path / ".env") in log and "JLCPCB_APP_ID" in log
    assert SENTINEL not in log
    monkeypatch.delenv("JLCPCB_APP_ID")


def test_missing_env_and_no_root_are_silent(tmp_path):
    (tmp_path / "project.kct").write_text("")
    buf = io.StringIO()
    assert jlc_env.load_jlc_env(tmp_path / "b.kicad_pcb", stream=buf) == []
    assert buf.getvalue() == ""


def test_unreadable_env_warns_and_continues(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / ".env").write_bytes(b"\xff\xfe\x00bad")
    buf = io.StringIO()
    assert jlc_env.load_jlc_env(tmp_path / "b.kicad_pcb", stream=buf) == []
    assert "could not read" in buf.getvalue()


def test_contact_never_exposes_values(monkeypatch):
    monkeypatch.setenv("JLCPCB_CONTACT_FIRST_NAME", SENTINEL)
    monkeypatch.setenv("JLCPCB_CONTACT_PHONE", SENTINEL)
    c = jlc_env.JLCContact.from_env()
    assert SENTINEL not in repr(c) and SENTINEL not in str(c.as_dict())
    assert c.as_dict()["first_name"] is True and c.as_dict()["city"] is False
    assert c.set_keys() == ["JLCPCB_CONTACT_FIRST_NAME", "JLCPCB_CONTACT_PHONE"]
    assert any(SENTINEL in ln for ln in c.guided_lines(show_values=True))
    assert not any(SENTINEL in ln for ln in c.guided_lines(show_values=False))
