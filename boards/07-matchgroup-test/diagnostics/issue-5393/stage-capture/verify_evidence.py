"""Verify the retained archive without extracting or executing its contents."""

import hashlib
import json
import tarfile
from pathlib import Path

root = Path(__file__).resolve().parent
manifest = json.loads((root / "evidence-manifest.json").read_text())
archive = root / "evidence.tar.gz"
if hashlib.sha256(archive.read_bytes()).hexdigest() != manifest["archive_sha256"]:
    raise SystemExit("Archive hash mismatch")
with tarfile.open(archive) as bundle:
    names = [member.name for member in bundle.getmembers()]
    if len(names) != len(set(names)) or set(names) != set(manifest["files"]):
        raise SystemExit("Archive member set mismatch")
    for name, expected in manifest["files"].items():
        member = bundle.getmember(name)
        if not member.isfile():
            raise SystemExit(f"Non-file archive member: {name}")
        data = bundle.extractfile(member).read()
        if len(data) != expected["bytes"] or hashlib.sha256(data).hexdigest() != expected["sha256"]:
            raise SystemExit(f"Member mismatch: {name}")
print(f"Verified {len(names)} retained evidence files")

# Check that the retained observations support the report's narrow conclusions.
with tarfile.open(archive) as bundle:

    def read_json(name):
        return json.loads(bundle.extractfile(name).read())

    preflight = read_json("preflight.json")
    assert preflight["source_commit"] == "a6c016e2c3fa32f4db5b5f3f2334b02dde2a1db0"
    assert preflight["verified_files"] == 3777
    assert not preflight["source_mismatches"]
    assert not read_json("post-prefix-source-check.json")["mismatches"]
    assert read_json("terminal.json")["exit"] == 0
    assert read_json("finishing/completion.json")["recipe_quality_passed"] is False
    for filename in ("control-copper-comparison.json", "final-control-copper-comparison.json"):
        comparison = read_json(filename)
        assert comparison["equal_copper_multisets"]
        assert comparison["equal_pad_geometry"]
        assert not comparison["a_only"] and not comparison["b_only"]
    events = read_json("postprocessing/events.json")
    assert len(events) == 18
    for event in events:
        assert event["serialization_preserved_live_geometry_and_route_metadata"]
        raw = bundle.extractfile("postprocessing/" + event["pcb"]).read()
        assert hashlib.sha256(raw).hexdigest() == event["pcb_sha256"]
        shape = read_json("postprocessing/" + event["pcb"].replace(".kicad_pcb", ".geometry.json"))
        assert (
            hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()
            == event["geometry_sha256"]
        )
    native = read_json("native-stage-summary.json")
    assert len(native) == len(events) + 2 + len(read_json("finishing/events.json"))
    for label, entry in native.items():
        if "identical_bytes_to" in entry:
            entry = native[entry["identical_bytes_to"]]
        for kind in ("saved", "refilled"):
            assert entry[kind]["pad_count"] == 244
            assert "DQ3" in entry[kind]["all_open_nets"]
            assert entry[kind]["native_exit"] == 0
print("Verified source, neutral selected copper, capture coverage and native DQ3 observations")
