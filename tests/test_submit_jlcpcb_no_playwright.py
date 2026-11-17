"""`kct submit-jlcpcb` must work without the optional playwright extra (issue #8481)."""

import argparse
import sys

import pytest

from kicad_tools.cli import submit_jlcpcb_cmd as cmd
from kicad_tools.manufacturers import jlc_site_driver as drv
from tests.test_submission_workflow import (
    Source,
    env,  # noqa: F401
    write_manifest,
)


@pytest.fixture
def no_playwright(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)


def test_connect_abort_names_submit_extra(no_playwright, monkeypatch):
    monkeypatch.setattr(drv, "require_attended", lambda *a, **k: None)
    with pytest.raises(drv.DriverAbort) as exc:
        drv.connect_attended_page(9222)
    assert "kicad-tools[submit]" in exc.value.reason
    assert "--guided" in exc.value.reason


@pytest.mark.parametrize("mode", ["dry_run", "guided"])
def test_dry_run_and_guided_need_no_playwright(no_playwright, env, monkeypatch, mode):  # noqa: F811
    write_manifest(env)
    monkeypatch.setattr(
        "kicad_tools.parts.jlcpcb_api.JLCCredentials.from_env", classmethod(lambda cls: object())
    )
    monkeypatch.setattr(
        "kicad_tools.parts.jlcpcb_api.JLCOpenAPIClient",
        lambda creds: Source({"C1": 100, "C2": 100}),
    )
    parser = argparse.ArgumentParser()
    cmd.add_arguments(parser)
    argv = [
        str(env["bundle"]),
        "--board",
        str(env["src"] / "b.kicad_pcb"),
        "--schematic",
        str(env["src"] / "b.kicad_sch"),
        "-o",
        str(env["out"]),
        "--dry-run" if mode == "dry_run" else "--guided",
    ]
    if mode == "guided":
        argv += ["--thickness-mm", "1.6", "--outer-copper-oz", "1", "--inner-copper-oz", "0.5"]
    args = parser.parse_args(argv)
    rc = cmd.run(args)
    assert rc == 0
