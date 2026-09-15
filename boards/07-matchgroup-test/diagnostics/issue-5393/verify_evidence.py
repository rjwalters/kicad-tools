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
