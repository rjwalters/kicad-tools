"""Replay lossless declarative power-access geometry against a chosen source tree."""

import argparse
import gzip
import json
import runpy
import sys
import time
from pathlib import Path

from shapely import from_wkb

parser = argparse.ArgumentParser()
parser.add_argument("fixture", type=Path)
parser.add_argument("--source", type=Path, default=Path.cwd())
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
sys.path.insert(0, str(args.source.resolve() / "src"))
api = runpy.run_path(str(args.source / "boards/06-diffpair-test/pour_escape.py"))


def decode(value):
    if isinstance(value, dict):
        if set(value) == {"wkb"}:
            return from_wkb(bytes.fromhex(value["wkb"]))
        if set(value) == {"layers"}:
            return frozenset(value["layers"])
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return tuple(decode(v) for v in value)
    return value


raw = args.fixture.read_bytes()
if args.fixture.suffix == ".gz":
    raw = gzip.decompress(raw)
fixture = json.loads(raw)
assert fixture["schema"] == 1
call = list(decode(fixture["args"]))
call[-1] = api["EscapeRules"](**call[-1])
observation = {}


def trace(frame, event, value):
    if frame.f_code.co_name == "find_escape" and event == "return":
        loc = frame.f_locals
        observation.update(
            reached=len(loc.get("previous", {})),
            frontier=len(loc.get("pending", [])),
            node_budget=loc["node_budget"],
        )
    return trace


start = time.monotonic()
sys.settrace(trace)
try:
    result = api["find_escape"](*call, step=fixture["step"], node_budget=fixture["node_budget"])
finally:
    sys.settrace(None)
observation.update(
    result=repr(result),
    elapsed=time.monotonic() - start,
    counts={
        name: len(call[i])
        for i, name in [(3, "pads"), (4, "segments"), (5, "vias"), (6, "targets")]
    },
)
args.output.write_text(json.dumps(observation, indent=2))
print(json.dumps(observation))
