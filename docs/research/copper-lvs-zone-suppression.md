# Copper LVS zone suppression: independent Board05 evidence

Measured on 2026-09-11 for #4982 / PR #5150. Native zone refill and save
reproduce the original report: the historical board has real native
unconnected items, and strict copper LVS now reports opens instead of
waiving every net that owns a zone.

## Results

| Board05 source | Native unconnected items | Other native violations | Bound logical pads | Copper LVS opens | Copper LVS clean |
| --- | ---: | ---: | ---: | ---: | --- |
| Original report, `b8d5f59504b5633e84b73524167ed23473714a02` | 57 | 99 | 197 | 44 | false |
| Current artifact, `456593d815fb8c4e93b662854c5be7a5d57be4b3` | 0 | 524 | 149 | 0 | true |

Historical opens by net: `+24V: 4`, `+3V3: 3`, `+5V: 3`, `GND: 33`,
`VIN: 1`. All 44 mismatches are opens; there are no shorts. These counts
match the original issue's strict connectivity evidence. Native DRC
independently reports disconnected power pads; for example, U3.16 and
U3.49 on +3V3 appear in native unconnected records and copper-LVS opens.
The algorithms use different witnesses and counts, so 57 native items
should not be interpreted as 57 expected comparator mismatches.

The current artifact was last changed by
`df2134e56d99a1b0653fe11d1c8daa4a791bfdf0`. Its clean copper result is an
expected control, not a reproduction failure: it is no longer the board
from the original report. No claim that current Board05 remains dirty is
supported by this measurement.

## Inputs and execution environment

Native tools came from cached Docker image `kicad/kicad:10.0`, image ID
`sha256:182c8005cb775a2c448a4c18681d489f1ff472a761885eba3e08b07e3c0564de`,
with `--platform linux/amd64`. `pcbnew.GetBuildVersion()` reported
**10.0.5**, not the original report's 10.0.6. Copper comparison used this
PR's working tree atop `456593d815fb8c4e93b662854c5be7a5d57be4b3`, including
the subsequent change that makes `advisory_net_names` a deprecated no-op.
The production comparison already omitted that argument at the base SHA.

SHA256 of source files:

| Source | File | SHA256 |
| --- | --- | --- |
| Historical | Routed PCB | `e1b116769ab75b8998b90bc094f4bafe4d428a444c5207d1f7317efdf2efc9eb` |
| Historical | Schematic | `e25e02188f5fc3aadb7b52ca753f82dff27a5418fae0229fe67bcd6034cc5391` |
| Current | Routed PCB | `6198353fe3bbd3a7573963dece749d1edea6577ab3042db93f230980c525a923` |
| Current | Schematic | `74cc0c297a1f587eaad810b2e4d080b70523c606f5c9c0fe3ef9a6fc13d321ac` |

Observed saved/refilled file hashes:

| Source | Native saved PCB | After numeric binding restoration |
| --- | --- | --- |
| Historical | `2a037d8ba75332c394898d51d15c0d1c77bb9461fc20844a352079ad5be4903c` | `607120dddf8cfffa16d9ded2f44535f866d144a5dedd674739bb5806b4719b19` |
| Current | `8d4fb191290a62d7c115fa018f8040b7122b75211bf40076def9fa17d07ee4b9` | `d01825bd14438c5faf892c31bfdc23efe550df66acfb2c3a35e29460a4fe468e` |

Saved hashes identify this run; serializer-generated metadata may change
between runs. The input hashes and measured connectivity are the durable
reproduction contract.

## Reproduction

Run from this repository with Docker and its cached KiCad image available.
This script exports both snapshots into a temporary directory; it does not
regenerate or modify repository board outputs. It saves the refill with
native `pcbnew` before either check, runs native DRC on that saved file,
then makes a separate copy for restoring numeric net declarations and
per-element bindings using the runner's existing helpers. Restoration
addresses the separate native-save/parser representation problem; it does
not add traces, vias, pads, or zone fill.

```bash
uv run python - <<'PY'
from collections import Counter
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import tempfile

from kicad_tools.cli.runner import (
    _snapshot_element_nets,
    _snapshot_net_declarations,
    _restore_net_declarations,
)
from kicad_tools.lvs.copper_lvs import compare_copper_netlist, result_to_json

root = Path(tempfile.mkdtemp(prefix='copper-lvs-4982-')).resolve()
image = 'kicad/kicad:10.0'
snapshots = {
    'historical': 'b8d5f59504b5633e84b73524167ed23473714a02',
    'current': '456593d815fb8c4e93b662854c5be7a5d57be4b3',
}
for label, revision in snapshots.items():
    folder = root / label
    folder.mkdir()
    for name in ('bldc_controller_routed.kicad_pcb', 'bldc_controller.kicad_sch'):
        content = subprocess.check_output([
            'git', 'show',
            f'{revision}:boards/05-bldc-motor-controller/output/{name}',
        ])
        (folder / name).write_bytes(content)
        print(label, name, hashlib.sha256(content).hexdigest(), flush=True)
    docker = [
        'docker', 'run', '--rm', '--network', 'none',
        '--platform', 'linux/amd64', '--mount',
        f'type=bind,src={folder},dst=/evidence', image,
    ]
    subprocess.run(docker + ['python3', '-c', '''
import pcbnew
print(pcbnew.GetBuildVersion(), flush=True)
board = pcbnew.LoadBoard('/evidence/bldc_controller_routed.kicad_pcb')
pcbnew.ZONE_FILLER(board).Fill(board.Zones())
pcbnew.SaveBoard('/evidence/refilled.kicad_pcb', board)
'''], check=True, timeout=120)
    subprocess.run(docker + [
        'kicad-cli', 'pcb', 'drc', '--format', 'json',
        '--output', '/evidence/native-drc.json',
        '/evidence/refilled.kicad_pcb',
    ], check=True, timeout=120)
    original = folder / 'bldc_controller_routed.kicad_pcb'
    restored = folder / 'refilled-restored.kicad_pcb'
    shutil.copyfile(folder / 'refilled.kicad_pcb', restored)
    _restore_net_declarations(
        restored, _snapshot_net_declarations(original),
        _snapshot_element_nets(original),
    )
    result = result_to_json(compare_copper_netlist(
        folder / 'bldc_controller.kicad_sch', restored,
    ))
    (folder / 'copper-lvs.json').write_text(json.dumps(result, indent=2))
    native = json.loads((folder / 'native-drc.json').read_text())
    print(label, 'native_unconnected', len(native['unconnected_items']),
          'other_violations', len(native['violations']),
          'clean', result['clean'], 'bound', result['bound_pad_count'],
          'opens', dict(Counter(m['net_a'] for m in result['mismatches']
                                if m['kind'] == 'open')), flush=True)
print('Evidence directory:', root)
PY
```

## Limits

This establishes removal of the original false-clean outcome and a clean
current-board connectivity control. It does not qualify either board for
manufacturing: both native runs report other violations. It also does not
claim that every extracted open has received an individual geometric
proof, or that native 10.0.5 and 10.0.6 are identical. The pure comparator's
paired partition tests separately cover disconnected and connected inputs,
including callers that still supply the legacy advisory argument.
