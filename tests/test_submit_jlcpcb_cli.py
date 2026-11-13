"""``kct submit-jlcpcb`` composition and the one-driver browser lock (#8465).

Offline: the website, the API and Chrome are all replaced; any contact fails.
"""

import hashlib
import json
import os
import platform

import pytest

from kicad_tools.cli import submit_jlcpcb_cmd as cmd
from kicad_tools.manufacturers import jlc_site_driver as drv
from kicad_tools.parts import jlcpcb_api

# `env` is the shared pytest fixture, re-exported here; its parameter uses are not redefinitions.
from tests.test_submission_workflow import Source, env, run, write_manifest  # noqa: F401

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


# --- Issue #8539: no-credentials stock path -------------------------------


class _FakeLCSC:
    """Stands in for LCSCClient: anonymous live rows, as lookup_many returns them."""

    def __init__(self, stock, source="anonymous", fail=False):
        self.stock, self.source, self.fail = stock, source, fail
        self.bypass = None

    def lookup_many(self, codes, bypass_cache=False):
        from kicad_tools.parts.models import BatchLookupResult, Part

        self.bypass = bypass_cache
        if self.fail:
            raise RuntimeError("network down")
        result = BatchLookupResult()
        for c in codes:
            if c in self.stock:
                result[c] = Part(lcsc_part=c, stock=self.stock[c])
                result.sources[c] = self.source
        return result


def test_no_inventory_source_message_is_explicit(env):
    from kicad_tools.export import submission_workflow as wf

    write_manifest(env)
    result = run(env, None, dry_run=True)
    assert result.stock.blocked
    failures = wf.format_stock_failures(result.stock)
    assert failures and all("JLC OpenAPI credentials not set" in f for f in failures)
    snap = json.loads((env["out"] / "inventory-snapshot.json").read_text())
    assert {o["status"] for o in snap["observations"]} == {"no-inventory-source"}
    assert "transport-error" not in json.dumps(snap)


def test_lcsc_anonymous_source_verifies_in_stock_parts(env):
    from kicad_tools.parts.lcsc_inventory import LCSCInventorySource

    write_manifest(env)
    fake = _FakeLCSC({"C1": 100, "C2": 100})
    result = run(env, LCSCInventorySource(fake), dry_run=True)
    assert not result.stock.blocked
    assert fake.bypass is True
    snap = json.loads((env["out"] / "inventory-snapshot.json").read_text())
    assert snap["provenance"]["source"] == "lcsc-anonymous"
    assert snap["provenance"]["stock_verified"] is True


def test_lcsc_source_ignores_offline_rows_and_survives_outage(env):
    from kicad_tools.parts.lcsc_inventory import LCSCInventorySource

    write_manifest(env)
    offline = run(
        env, LCSCInventorySource(_FakeLCSC({"C1": 100, "C2": 100}, "offline")), dry_run=True
    )
    assert offline.stock.blocked  # offline catalog rows are not stock evidence


def test_lcsc_outage_reads_as_transport_error(env):
    from kicad_tools.parts.lcsc_inventory import LCSCInventorySource

    write_manifest(env)
    result = run(env, LCSCInventorySource(_FakeLCSC({}, fail=True)), dry_run=True)
    assert result.stock.blocked
    snap = json.loads((env["out"] / "inventory-snapshot.json").read_text())
    assert {o["status"] for o in snap["observations"]} == {"transport-error"}


def test_cli_uses_lcsc_fallback_without_credentials(env, monkeypatch):
    from kicad_tools.parts import lcsc_inventory

    write_manifest(env)
    monkeypatch.setattr(jlcpcb_api.JLCCredentials, "from_env", classmethod(lambda cls: None))
    monkeypatch.setattr(lcsc_inventory, "LCSCClient", lambda **k: _FakeLCSC({"C1": 100, "C2": 100}))
    assert cmd.main(_argv(env, "--dry-run")) == 0


def test_upload_still_requires_credentials_with_lcsc_fallback(env, monkeypatch, capsys):
    from kicad_tools.parts import lcsc_inventory

    write_manifest(env)
    monkeypatch.setattr(jlcpcb_api.JLCCredentials, "from_env", classmethod(lambda cls: None))
    monkeypatch.setattr(lcsc_inventory, "LCSCClient", lambda **k: _FakeLCSC({"C1": 100, "C2": 100}))
    assert cmd.main(_argv(env, "--upload", "--reviewer", "me")) == 2
    assert "credentials" in capsys.readouterr().err


def test_guided_runs_with_blocked_stock_and_prints_state(env, monkeypatch, capsys):
    write_manifest(env)
    _no_browser(monkeypatch)
    _stock(monkeypatch, {"C1": 5})  # blocked
    rc = cmd.main(_argv(env, "--guided"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "stock gate: BLOCKED" in out and "STOCK:" in out and "Guided checklist" in out


# --- tapeout-sign-off --stage fab-dfm (#8841) --------------------------------


def _dfm_args(env, tmp_path, counts):
    import argparse

    from kicad_tools.cli import tapeout_signoff_cmd as so

    cfile, kfile = tmp_path / "dfm.json", tmp_path / "kct.json"
    cfile.write_text(json.dumps(counts))
    board_sha = hashlib.sha256((env["src"] / "b.kicad_pcb").read_bytes()).hexdigest()
    kfile.write_text(json.dumps({"pcb_sha256": board_sha, "violations": []}))
    parser = argparse.ArgumentParser()
    so.add_arguments(parser)
    args = parser.parse_args(
        [
            str(env["bundle"]),
            "--board",
            str(env["src"] / "b.kicad_pcb"),
            "--stage",
            "fab-dfm",
            "--reviewer",
            "Bob",
            "--dfm-counts",
            str(cfile),
            "--kct-report",
            str(kfile),
        ]
    )
    return so, args


def test_fab_dfm_cli_prints_mismatch_and_exits_zero(env, tmp_path, capsys):
    from tests.test_submission_workflow import _released

    _released(env)
    from tests.test_submission_workflow import _full

    so, args = _dfm_args(env, tmp_path, _full(tht_to_smd={"danger": 2, "warning": 0, "good": 1}))
    assert so.run(args) == 0
    out = capsys.readouterr().out
    assert "MISMATCH" in out and "tht_to_smd" in out and "no mapped kct rule" in out


def test_fab_dfm_cli_refuses_unknown_category(env, tmp_path, capsys):
    from tests.test_submission_workflow import _released

    _released(env)
    so, args = _dfm_args(env, tmp_path, {"bogus": {"danger": 1}})
    assert so.run(args) == 2
    assert "Unknown DFM category" in capsys.readouterr().err


def test_fab_dfm_cli_refuses_partial_attestation(env, tmp_path, capsys):
    from tests.test_submission_workflow import _released

    _released(env)
    so, args = _dfm_args(env, tmp_path, {"fiducial": {"danger": 0, "warning": 0, "good": 2}})
    before = (env["bundle"] / "manifest.json").read_bytes()
    assert so.run(args) == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err and "all 14 categories" in err
    assert (env["bundle"] / "manifest.json").read_bytes() == before


def test_guided_contact_values_only_on_tty_and_never_in_output_tree(env, monkeypatch, capsys):
    sentinel = "SENTINEL-8859-contact"
    write_manifest(env)
    _no_browser(monkeypatch)
    _stock(monkeypatch, {"C1": 5})
    monkeypatch.setenv("JLCPCB_CONTACT_LAST_NAME", sentinel)
    assert cmd.main(_argv(env, "--guided")) == 0
    cap = capsys.readouterr()
    assert sentinel not in cap.out + cap.err  # non-TTY: key names only
    assert "JLCPCB_CONTACT_LAST_NAME" in cap.out
    for p in env["out"].rglob("*"):
        if p.is_file():
            assert sentinel.encode() not in p.read_bytes(), p
