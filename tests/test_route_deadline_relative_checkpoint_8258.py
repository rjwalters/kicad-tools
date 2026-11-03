"""Issue #8258: timeout.json must not name a moved checkpoint for a relative -o."""

import json
import os
import sys
from pathlib import Path

import pytest

from kicad_tools.cli import route_deadline


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group regression")
@pytest.mark.parametrize("relative", [True, False])
def test_checkpoint_repointed_to_quarantined_file(tmp_path, monkeypatch, relative):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "wip").mkdir()
    output = tmp_path / "wip" / "out.kicad_pcb"
    output.write_text("(kicad_pcb)")
    printed = "wip/out.kicad_pcb" if relative else str(output)
    script = f"""
import signal, time, json, os
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(os.environ["KCT_ROUTE_DEADLINE_CONTROL"]).write_text(json.dumps(dict(
    stage="routing", output={str(output)!r}, checkpoint={printed!r}, snapshot_saved=False)))
time.sleep(100)
"""
    code = route_deadline._supervise(
        [sys.executable, "-c", script], 1.5, tmp_path / "control.json", save_seconds=0.3
    )
    assert code == 124
    report = json.loads(output.with_suffix(".timeout.json").read_text())
    assert not output.exists()
    assert report["checkpoint"] == report["unverified_output"]
    assert Path(report["checkpoint"]).exists()
