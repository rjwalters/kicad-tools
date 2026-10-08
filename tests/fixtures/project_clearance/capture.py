"""Verify project JSON precedence through the native CLI DRC referee."""

import hashlib
import json
import re
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pcbnew

root = Path(__file__).resolve().parent
cases = json.loads((root / "oracle.json").read_text())["cases"]
out = Path(sys.argv[1])
out.mkdir(exist_ok=False)
results = []
cli = shutil.which("kicad-cli")
assert cli, "kicad-cli is required"
for case in cases:
    expected = {}
    for name in ("USB1", "USB2", "Other"):
        folder = out / (case["id"] + "-" + name)
        folder.mkdir()
        pcb = folder / "probe.kicad_pcb"
        board = pcbnew.BOARD()
        for code, net_name, y in ((1, name, 10), (2, "REF", 10.15)):
            net = pcbnew.NETINFO_ITEM(board, net_name, code)
            board.Add(net)
            track = pcbnew.PCB_TRACK(board)
            track.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(10), pcbnew.FromMM(y)))
            track.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(15), pcbnew.FromMM(y)))
            track.SetWidth(pcbnew.FromMM(0.1))
            track.SetLayer(pcbnew.F_Cu)
            track.SetNet(net)
            board.Add(track)
        pcbnew.SaveBoard(str(pcb), board)
        project = deepcopy(case["project"])
        s = project["net_settings"]
        s["classes"].append({"name": "Reference", "clearance": 0.0, "priority": -10})
        s["netclass_assignments"]["REF"] = (
            "Reference" if s["meta"]["version"] == 3 else ["Reference"]
        )
        pro = pcb.with_suffix(".kicad_pro")
        pro.write_text(json.dumps(project))
        hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (pcb, pro)}
        cmd = [
            cli,
            "pcb",
            "drc",
            "--format",
            "json",
            "--output",
            str(folder / "drc.json"),
            str(pcb),
        ]
        run = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=20
        )
        (folder / "native.log").write_text(run.stdout)
        assert run.returncode == 0, run.stdout
        assert hashes == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (pcb, pro)}
        report = json.loads((folder / "drc.json").read_text())
        violations = [v for v in report["violations"] if v["type"] == "clearance"]
        assert len(violations) == 1, violations
        value = float(re.search(r"clearance ([0-9.]+) mm", violations[0]["description"]).group(1))
        expected[name] = value
        (folder / "receipt.json").write_text(
            json.dumps({"argv": cmd, "exit": run.returncode, "hashes": hashes}, indent=2)
        )
    results.append({"id": case["id"], "project": case["project"], "expected_clearances": expected})
(out / "oracle.json").write_text(
    json.dumps(
        {
            "native_version": subprocess.check_output([cli, "version"], text=True).strip(),
            "cases": results,
        },
        indent=2,
    )
)
print("Captured", len(results) * 3, "native project/clearance controls")
