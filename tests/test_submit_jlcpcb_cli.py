"""``kct submit-jlcpcb`` composition and the one-driver browser lock (#8465).

Offline: the website, the API and Chrome are all replaced; any contact fails.
"""

import json
import os
import platform

import pytest

from kicad_tools.cli import submit_jlcpcb_cmd as cmd
from kicad_tools.manufacturers import jlc_site_driver as drv
from kicad_tools.parts import jlcpcb_api

# `env` is the shared pytest fixture, re-exported here; its parameter uses are not redefinitions.
from tests.test_submission_workflow import Source, env, write_manifest  # noqa: F401

# ruff: noqa: F811


def _lock_file(lock_dir, port=9222, **fields):
    lock_dir.mkdir(parents=True, exist_ok=True)
    held = {"pid": os.getpid(), "host": platform.node(), "port": port, "acquired_at": 1000.0}
    held.update(fields)
    path = lock_dir / f"jlc-cdp-{port}.lock"
    path.write_text(json.dumps(held))
    return path


# --- browser_lock -----------------------------------------------------------


def test_lock_refuses_a_second_driver_and_releases(tmp_path):
    with drv.browser_lock(9222, lock_dir=tmp_path) as path:
        assert json.loads(path.read_text())["pid"] == os.getpid()
        with pytest.raises(drv.DriverAbort) as exc, drv.browser_lock(9222, lock_dir=tmp_path):
            pass
        assert exc.value.step == "lock"
        # A different debug port is a different browser.
        with drv.browser_lock(9333, lock_dir=tmp_path):
            pass
    assert not path.exists()


def test_fresh_live_lock_from_another_host_refuses(tmp_path):
    _lock_file(tmp_path, host="other-host", acquired_at=1000.0)
    with pytest.raises(drv.DriverAbort, match="another driver"):
        with drv.browser_lock(9222, lock_dir=tmp_path, now=lambda: 1060.0):
            pass


@pytest.mark.parametrize(
    "fields, alive",
    [
        ({"acquired_at": 0.0, "host": "other-host"}, True),  # older than the stale window
        ({"pid": 999_999_999}, False),  # dead holder on this host
    ],
)
def test_stale_lock_is_taken_over(tmp_path, fields, alive):
    _lock_file(tmp_path, **fields)
    with drv.browser_lock(
        9222,
        lock_dir=tmp_path,
        now=lambda: drv.LOCK_STALE_AFTER_S + 10.0,
        pid_alive=lambda pid: alive,
    ) as path:
        assert json.loads(path.read_text())["pid"] == os.getpid()


def test_unreadable_lock_is_taken_over(tmp_path):
    (tmp_path / "jlc-cdp-9222.lock").write_text("not json")
    with drv.browser_lock(9222, lock_dir=tmp_path) as path:
        assert json.loads(path.read_text())["pid"] == os.getpid()


def test_release_never_removes_someone_elses_lock(tmp_path):
    with drv.browser_lock(9222, lock_dir=tmp_path) as path:
        path.write_text(json.dumps({"pid": -1, "host": "x", "acquired_at": 0}))
    assert path.exists()


# --- CLI composition --------------------------------------------------------


def _argv(env, *extra):
    return [
        str(env["bundle"]),
        "--board",
        str(env["src"] / "b.kicad_pcb"),
        "--schematic",
        str(env["src"] / "b.kicad_sch"),
        "--quantity",
        "10",
        "-o",
        str(env["out"]),
        *extra,
    ]


def _no_browser(monkeypatch):
    def boom(*a, **k):
        pytest.fail("website contacted")

    monkeypatch.setattr(drv, "connect_attended_page", boom)
    monkeypatch.setattr(drv, "run_driver", boom)


def _stock(monkeypatch, stock):
    monkeypatch.setattr(jlcpcb_api.JLCCredentials, "from_env", classmethod(lambda cls: object()))
    monkeypatch.setattr(jlcpcb_api, "JLCOpenAPIClient", lambda creds: Source(stock))


def test_dry_run_with_website_is_refused_before_anything(env, monkeypatch, capsys):
    write_manifest(env)
    _no_browser(monkeypatch)
    monkeypatch.setattr(cmd, "run_submission", lambda **k: pytest.fail("preflight ran"))
    rc = cmd.main(_argv(env, "--dry-run", "--website", "--match-mapping", "m.json"))
    assert rc == 2
    assert "--dry-run never touches the website" in capsys.readouterr().err
    assert not env["out"].exists()


def test_stock_block_skips_website_leg(env, monkeypatch, capsys):
    write_manifest(env)
    _no_browser(monkeypatch)
    _stock(monkeypatch, {"C1": 5})  # C1 short, C2 unknown
    rc = cmd.main(_argv(env, "--website", "--match-mapping", "m.json"))
    out = capsys.readouterr()
    assert rc == 1
    assert "website leg skipped" in out.err
    assert "STOCK:" in out.out


def test_held_lock_aborts_website_leg_without_attaching(env, monkeypatch, tmp_path, capsys):
    write_manifest(env)
    _no_browser(monkeypatch)
    _stock(monkeypatch, {"C1": 100, "C2": 100})
    monkeypatch.setattr(drv, "require_attended", lambda: None)
    lock_dir = tmp_path / "locks"
    _lock_file(lock_dir, acquired_at=10**12)  # live holder (this pid), fresh
    mapping = tmp_path / "mapping.json"
    mapping.write_text(
        json.dumps(
            {
                "reference": "Refs",
                "selected": "Selected",
                "selected_values": ["yes"],
                "unselected_values": ["no"],
                "catalog_id": "Catalog",
                "identity": "catalog_id",
            }
        )
    )
    args = ["--website", "--match-mapping", str(mapping), "--lock-dir", str(lock_dir)]
    rc = cmd.main(_argv(env, *args, "--thickness-mm", "1.6", "--outer-copper-oz", "1"))
    err = capsys.readouterr().err
    assert rc == 2
    assert "ABORTED at lock" in err and "Nothing was submitted" in err
