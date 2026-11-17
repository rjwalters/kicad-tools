#!/usr/bin/env python3
"""Clean-room probe of KiCad's ``embedded_files`` checksum (issue #7688).

KiCad 10 boards can carry 3D bodies inline::

    (embedded_files (file (name "x.step") (type model)
        (data |<base64(zstd(raw))>|) (checksum "<32 hex>")))

The 128-bit ``checksum`` is undocumented, and kicad-cli validates it: a
mismatch makes the loader warn ``Checksum error in embedded file`` and drop the
file. This script records the spike's black-box work so the result can be
re-run against any KiCad release. It reads **only KiCad data files** (demo
boards, footprints) and never KiCad source.

Subcommands:

``samples FILE...``
    Extract every ``(file … (data |…|) (checksum …))`` block as JSON lines.

``hypotheses FILE...``
    Test each sample against a battery of standard 128-bit hashes of the raw,
    compressed and base64 bytes, in every common output byte order. It then
    runs MurmurHash3 x64_128 and x86_128 *backwards* from the observed
    checksum to their initial state. That rules each one in or out for
    **every** seed at once: a seeded hash starts with all lanes equal, and any
    fixed initial state would be the same across samples.

``verify BOARD``
    Ask kicad-cli whether a board's embedded checksums are accepted, using
    ``kicad-cli pcb upgrade --force`` on a scratch copy and its
    ``Checksum error`` warning as the accept/reject oracle.

Optional dependencies (fetched ad hoc, not project deps)::

    uv run --with zstandard --with mmh3 --with xxhash \\
        python scripts/research/embedded_checksum_probe.py hypotheses \\
        "/Library/Application Support/kicad/demos"/*/*.kicad_pcb
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path

_FILE_RE = re.compile(
    r'\(file\s+\(name\s+"((?:[^"\\]|\\.)*)"\)\s+\(type\s+(\w+)\)\s+'
    r"\(data\s+\|([^|]*)\|\s*\)\s+\(checksum\s+\"([0-9A-Fa-f]*)\"\)",
    re.S,
)

# --------------------------------------------------------------------------
# MurmurHash3 (public-domain reference algorithm), forward and inverse
# --------------------------------------------------------------------------

_M64 = (1 << 64) - 1
_M32 = (1 << 32) - 1
_C1_64, _C2_64 = 0x87C37B91114253D5, 0x4CF5AD432745937F
_F1_64, _F2_64 = 0xFF51AFD7ED558CCD, 0xC4CEB9FE1A85EC53


def _rotl64(x: int, r: int) -> int:
    return ((x << r) | (x >> (64 - r))) & _M64


def _rotr64(x: int, r: int) -> int:
    return ((x >> r) | (x << (64 - r))) & _M64


def _inv(a: int, bits: int) -> int:
    return pow(a, -1, 1 << bits)


def _fmix64(k: int) -> int:
    k ^= k >> 33
    k = k * _F1_64 & _M64
    k ^= k >> 33
    k = k * _F2_64 & _M64
    return k ^ (k >> 33)


def _unfmix64(k: int) -> int:
    k ^= k >> 33  # x ^= x >> s is an involution for s >= width / 2
    k = k * _inv(_F2_64, 64) & _M64
    k ^= k >> 33
    k = k * _inv(_F1_64, 64) & _M64
    return k ^ (k >> 33)


def _mk1_64(k: int) -> int:
    return _rotl64(k * _C1_64 & _M64, 31) * _C2_64 & _M64


def _mk2_64(k: int) -> int:
    return _rotl64(k * _C2_64 & _M64, 33) * _C1_64 & _M64


def _tail64(data: bytes) -> tuple[int, int, int]:
    nb = len(data) // 16
    k1, k2 = struct.unpack_from("<QQ", data[nb * 16 :] + b"\0" * 16)
    return k1, k2, len(data) & 15


def mmh3_x64_128(data: bytes, h1: int, h2: int | None = None) -> tuple[int, int]:
    """MurmurHash3 x64_128 from an arbitrary initial state (seed: ``h1 == h2``)."""
    h2 = h1 if h2 is None else h2
    n = len(data)
    for i in range(n // 16):
        k1, k2 = struct.unpack_from("<QQ", data, i * 16)
        h1 = ((_rotl64(h1 ^ _mk1_64(k1), 27) + h2) * 5 + 0x52DCE729) & _M64
        h2 = ((_rotl64(h2 ^ _mk2_64(k2), 31) + h1) * 5 + 0x38495AB5) & _M64
    k1, k2, r = _tail64(data)
    if r > 8:
        h2 ^= _mk2_64(k2)
    if r > 0:
        h1 ^= _mk1_64(k1)
    h1 ^= n
    h2 ^= n
    h1 = (h1 + h2) & _M64
    h2 = (h2 + h1) & _M64
    h1, h2 = _fmix64(h1), _fmix64(h2)
    h1 = (h1 + h2) & _M64
    h2 = (h2 + h1) & _M64
    return h1, h2


def mmh3_x64_128_initial_state(data: bytes, h1: int, h2: int) -> tuple[int, int]:
    """Run x64_128 backwards from output ``(h1, h2)`` to its initial state."""
    n = len(data)
    i5 = _inv(5, 64)
    h2 = (h2 - h1) & _M64
    h1 = (h1 - h2) & _M64
    h1, h2 = _unfmix64(h1), _unfmix64(h2)
    h2 = (h2 - h1) & _M64
    h1 = (h1 - h2) & _M64
    h1 ^= n
    h2 ^= n
    k1, k2, r = _tail64(data)
    if r > 8:
        h2 ^= _mk2_64(k2)
    if r > 0:
        h1 ^= _mk1_64(k1)
    for i in reversed(range(n // 16)):
        k1, k2 = struct.unpack_from("<QQ", data, i * 16)
        h2 = _rotr64((((h2 - 0x38495AB5) * i5) - h1) & _M64, 31) ^ _mk2_64(k2)
        h1 = _rotr64((((h1 - 0x52DCE729) * i5) - h2) & _M64, 27) ^ _mk1_64(k1)
    return h1, h2


_C32 = (0x239B961B, 0xAB0E9789, 0x38B34AE5, 0xA1E38B93)
_KR32 = (15, 16, 17, 18)
_HR32 = (19, 17, 15, 13)
_HA32 = (0x561CCD1B, 0x0BCAA747, 0x96CD1C35, 0x32AC3B17)


def _rotl32(x: int, r: int) -> int:
    return ((x << r) | (x >> (32 - r))) & _M32


def _rotr32(x: int, r: int) -> int:
    return ((x >> r) | (x << (32 - r))) & _M32


def _km32(i: int, k: int) -> int:
    return _rotl32(k * _C32[i] & _M32, _KR32[i]) * _C32[(i + 1) % 4] & _M32


def _fmix32(h: int) -> int:
    h ^= h >> 16
    h = h * 0x85EBCA6B & _M32
    h ^= h >> 13
    h = h * 0xC2B2AE35 & _M32
    return h ^ (h >> 16)


def _unfmix32(h: int) -> int:
    h ^= h >> 16
    h = h * _inv(0xC2B2AE35, 32) & _M32
    h ^= (h >> 13) ^ (h >> 26)
    h = h * _inv(0x85EBCA6B, 32) & _M32
    return h ^ (h >> 16)


def _mix4(h: list[int]) -> list[int]:
    h0 = sum(h) & _M32
    return [h0] + [(x + h0) & _M32 for x in h[1:]]


def _unmix4(g: list[int]) -> list[int]:
    rest = [(x - g[0]) & _M32 for x in g[1:]]
    return [(g[0] - sum(rest)) & _M32] + rest


def _tail32(h: list[int], data: bytes) -> None:
    nb = len(data) // 16
    k = struct.unpack_from("<4I", data[nb * 16 :] + b"\0" * 16)
    r = len(data) & 15
    for i in range(4):
        if r > 4 * i:
            h[i] ^= _km32(i, k[i])


def mmh3_x86_128(data: bytes, state: Iterable[int] | int) -> list[int]:
    """MurmurHash3 x86_128 from a seed or an explicit 4-lane initial state."""
    h = [state] * 4 if isinstance(state, int) else list(state)
    for b in range(len(data) // 16):
        k = struct.unpack_from("<4I", data, b * 16)
        for i in range(4):
            x = _rotl32(h[i] ^ _km32(i, k[i]), _HR32[i])
            h[i] = ((x + h[(i + 1) % 4]) * 5 + _HA32[i]) & _M32
    _tail32(h, data)
    h = [x ^ len(data) for x in h]
    return _mix4([_fmix32(x) for x in _mix4(h)])


def mmh3_x86_128_initial_state(data: bytes, out: Iterable[int]) -> list[int]:
    """Run x86_128 backwards from its four output lanes to its initial state."""
    i5 = _inv(5, 32)
    h = _unmix4([_unfmix32(x) for x in _unmix4(list(out))])
    h = [x ^ len(data) for x in h]
    _tail32(h, data)
    for b in reversed(range(len(data) // 16)):
        k = struct.unpack_from("<4I", data, b * 16)
        for i in (3, 2, 1, 0):
            x = _rotr32((((h[i] - _HA32[i]) * i5) - h[(i + 1) % 4]) & _M32, _HR32[i])
            h[i] = x ^ _km32(i, k[i])
    return h


# --------------------------------------------------------------------------
# Samples
# --------------------------------------------------------------------------


def iter_samples(paths: Iterable[Path]) -> Iterator[dict[str, str]]:
    """Yield ``{src, name, type, data, checksum}`` for every data-bearing block."""
    for path in paths:
        text = Path(path).read_text(errors="replace")
        for m in _FILE_RE.finditer(text):
            yield {
                "src": str(path),
                "name": m[1],
                "type": m[2],
                "data": re.sub(r"\s", "", m[3]),
                "checksum": m[4].upper(),
            }


def decode_payload(b64: str) -> tuple[bytes, bytes]:
    """Return ``(compressed, raw)`` for a ``data`` field (needs ``zstandard``)."""
    import zstandard  # type: ignore[import-not-found]

    comp = base64.b64decode(b64)
    return comp, zstandard.ZstdDecompressor().decompressobj().decompress(comp)


def output_orders(digest: bytes) -> dict[str, bytes]:
    """The byte orders a 128-bit value is plausibly printed in."""
    return {
        "as-is": digest,
        "reversed": digest[::-1],
        "halves-swapped": digest[8:] + digest[:8],
        "u64-be": digest[:8][::-1] + digest[8:][::-1],
        "u32-be": b"".join(digest[i : i + 4][::-1] for i in range(0, 16, 4)),
    }


def _candidate_digests(x: bytes) -> Iterator[tuple[str, bytes]]:
    yield "md5", hashlib.md5(x).digest()
    yield "sha1[:16]", hashlib.sha1(x).digest()[:16]
    yield "sha256[:16]", hashlib.sha256(x).digest()[:16]
    yield "sha256[16:]", hashlib.sha256(x).digest()[16:]
    yield "blake2b-128", hashlib.blake2b(x, digest_size=16).digest()
    yield "blake2s-128", hashlib.blake2s(x, digest_size=16).digest()
    try:
        import xxhash  # type: ignore[import-not-found]

        for seed in (0, 1):
            yield f"xxh3_128/{seed}", xxhash.xxh3_128(x, seed=seed).digest()
    except ImportError:
        pass


def run_hypotheses(samples: list[dict[str, str]]) -> dict[str, object]:
    """Run the battery; return matches and recovered MurmurHash3 initial states."""
    matches: list[str] = []
    states64: dict[str, set[tuple[int, int]]] = {}
    states86: dict[str, set[tuple[int, ...]]] = {}
    for s in samples:
        comp, raw = decode_payload(s["data"])
        target = bytes.fromhex(s["checksum"])
        inputs = {"raw": raw, "compressed": comp, "base64": s["data"].encode()}
        for in_name, x in inputs.items():
            for h_name, digest in _candidate_digests(x):
                for o_name, form in output_orders(digest).items():
                    if form == target:
                        matches.append(f"{s['name']}: {h_name}({in_name}) {o_name}")
            for o_name, form in output_orders(target).items():
                st = mmh3_x64_128_initial_state(x, *struct.unpack("<QQ", form))
                if st[0] == st[1]:
                    matches.append(f"{s['name']}: mmh3_x64_128({in_name}) seed={st[0]:#x}")
                states64.setdefault(f"{in_name}/{o_name}", set()).add(st)
                st86 = tuple(mmh3_x86_128_initial_state(x, struct.unpack("<4I", form)))
                if len(set(st86)) == 1:
                    matches.append(f"{s['name']}: mmh3_x86_128({in_name}) seed={st86[0]:#x}")
                states86.setdefault(f"{in_name}/{o_name}", set()).add(st86)
    n = len(samples)
    # A fixed (non-seed) initial state would recover to one value for all samples.
    shared = [k for k, v in states64.items() if n > 1 and len(v) == 1]
    shared += [k for k, v in states86.items() if n > 1 and len(v) == 1]
    return {"samples": n, "matches": matches, "shared_initial_state": shared}


# --------------------------------------------------------------------------
# kicad-cli accept/reject oracle
# --------------------------------------------------------------------------


def verify_with_kicad_cli(board: Path, kicad_cli: str = "kicad-cli") -> list[str]:
    """Return the names kicad-cli rejects for a bad checksum (empty: all accepted)."""
    with tempfile.TemporaryDirectory(prefix="kct-embed-probe-") as tmp:
        scratch = Path(tmp) / board.name
        shutil.copyfile(board, scratch)
        proc = subprocess.run(
            [kicad_cli, "pcb", "upgrade", "--force", str(scratch)],
            capture_output=True,
            text=True,
            check=False,
        )
    out = proc.stdout + proc.stderr
    return re.findall(r"Checksum error in embedded file (.+?) in '", out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("samples")
    p.add_argument("files", nargs="+", type=Path)
    p = sub.add_parser("hypotheses")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("--limit", type=int, default=8, help="smallest N distinct samples")
    p = sub.add_parser("verify")
    p.add_argument("board", type=Path)
    p.add_argument("--kicad-cli", default="kicad-cli")
    args = parser.parse_args(argv)

    if args.cmd == "samples":
        for s in iter_samples(args.files):
            print(json.dumps(s))
        return 0
    if args.cmd == "hypotheses":
        distinct: dict[str, dict[str, str]] = {}
        for s in iter_samples(args.files):
            distinct.setdefault(s["checksum"], s)
        chosen = sorted(distinct.values(), key=lambda s: len(s["data"]))[: args.limit]
        print(json.dumps(run_hypotheses(chosen), indent=2))
        return 0
    rejected = verify_with_kicad_cli(args.board, args.kicad_cli)
    print(json.dumps({"board": str(args.board), "rejected": rejected}))
    return 1 if rejected else 0


if __name__ == "__main__":
    sys.exit(main())
