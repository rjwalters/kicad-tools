"""Record and replay native coupled diff-pair searches (Issue #6816).

The coupled joint-state A* (``router_cpp.CoupledPathfinder``) runs entirely on
a native ``Grid3D`` that Python fills through a long sequence of calls:
``mark_blocked`` per occupied cell, ``add_pad``, stored routes, halo marks,
authored floors and so on.  Reproducing a board's search outside the board
means reproducing *exactly* that sequence.  Rather than re-deriving each input
class from a live :class:`~kicad_tools.router.grid.RoutingGrid` (and silently
missing whichever one a later change adds), this module records the native
calls themselves:

* :func:`recording_native_types` returns ``Grid3D`` / ``CoupledPathfinder``
  subclasses that log every public method call (arguments and, for searches,
  the result) to a shared :class:`NativeCallLog`, then forward it unchanged.
  A capture driver swaps them into the ``router_cpp`` module for a bounded
  window, so production code needs no hook, env var or flag.
* :func:`save_fixture` / :func:`load_fixture` serialise the log to a JSON
  manifest plus a compressed ``.npz`` of the large integer arrays.
* :func:`replay` rebuilds the native objects from the log on a fresh
  interpreter and returns every ``route`` call's diagnostics, which a test can
  compare against the recorded ones.

Long runs of consecutive same-method calls with integer/bool arguments (the
``mark_blocked`` bulk copy) are stored column-wise and delta-encoded, which is
what keeps a full board grid under a few megabytes.

Nothing here is imported by the router; it is test and diagnostic tooling.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

#: Bump when the on-disk layout changes.
CAPTURE_FORMAT = 1

#: Lists of plain ints at least this long are stored as npz arrays.
_ARRAY_MIN_LEN = 256

#: Methods whose return value is recorded (searches; the rest are setters).
_RESULT_METHODS = frozenset({"route", "route_centerline"})


class CaptureStop(BaseException):  # noqa: N818 - control-flow sentinel, not an error
    """Raised by a capture driver to stop a run once the target is recorded.

    A ``BaseException`` so the router's ``except Exception`` fallbacks cannot
    swallow it.
    """


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------


@dataclass
class _Batch:
    """Consecutive calls of one method with int/bool-only arguments."""

    obj: int
    method: str
    kinds: tuple[str, ...]  # "i" or "b" per argument
    columns: list[list[int]]

    def accepts(self, obj: int, method: str, kinds: tuple[str, ...]) -> bool:
        return self.obj == obj and self.method == method and self.kinds == kinds

    def add(self, args: tuple) -> None:
        for column, value in zip(self.columns, args, strict=True):
            column.append(int(value))


@dataclass
class NativeCallLog:
    """Ordered record of native object construction and method calls."""

    entries: list[Any] = field(default_factory=list)
    _next_id: int = 0

    def new_object(self, type_name: str, args: tuple, kwargs: dict) -> int:
        oid = self._next_id
        self._next_id += 1
        self.entries.append(
            {"op": "new", "obj": oid, "type": type_name, "args": args, "kwargs": kwargs}
        )
        return oid

    def record_call(self, oid: int, method: str, args: tuple, kwargs: dict) -> dict | None:
        kinds = _scalar_kinds(args) if not kwargs else None
        if kinds:
            last = self.entries[-1] if self.entries else None
            if isinstance(last, _Batch) and last.accepts(oid, method, kinds):
                last.add(args)
                return None
            if isinstance(last, dict) and _batchable_with(last, oid, method, kinds):
                batch = _Batch(oid, method, kinds, [[] for _ in kinds])
                batch.add(last["args"])
                batch.add(args)
                self.entries[-1] = batch
                return None
        entry = {"op": "call", "obj": oid, "method": method, "args": args, "kwargs": kwargs}
        self.entries.append(entry)
        return entry

    def objects_of_type(self, type_name: str) -> list[int]:
        return [
            e["obj"]
            for e in self.entries
            if isinstance(e, dict) and e["op"] == "new" and e["type"] == type_name
        ]


def _scalar_kinds(args: tuple) -> tuple[str, ...] | None:
    kinds = []
    for value in args:
        if isinstance(value, bool):
            kinds.append("b")
        elif isinstance(value, int):
            kinds.append("i")
        else:
            return None
    return tuple(kinds) or None


def _batchable_with(entry: dict, oid: int, method: str, kinds: tuple[str, ...]) -> bool:
    return (
        entry["op"] == "call"
        and entry["obj"] == oid
        and entry["method"] == method
        and not entry["kwargs"]
        and "result" not in entry
        and _scalar_kinds(entry["args"]) == kinds
    )


def recording_native_types(router_cpp, log: NativeCallLog) -> dict[str, type]:
    """Return logging subclasses of the native types the coupled search uses.

    Keys are attribute names on ``router_cpp``.  Each subclass registers itself
    in ``log`` on construction and logs every public method call made from
    Python, in order, before forwarding it.  Calls the native side makes
    internally are not (and need not be) logged: replaying the Python-visible
    calls reproduces them.
    """

    def make(base: type, type_name: str) -> type:
        class Recording(base):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                object.__setattr__(self, "_capture_id", log.new_object(type_name, args, kwargs))

            def __getattribute__(self, name):
                attr = super().__getattribute__(name)
                if name.startswith("_") or not callable(attr):
                    return attr
                oid = object.__getattribute__(self, "_capture_id")

                def call(*args, **kwargs):
                    entry = log.record_call(oid, name, args, kwargs)
                    result = attr(*args, **kwargs)
                    if entry is not None and name in _RESULT_METHODS:
                        entry["result"] = result
                    return result

                return call

        Recording.__name__ = Recording.__qualname__ = f"Recording{type_name}"
        return Recording

    return {
        "Grid3D": make(router_cpp.Grid3D, "Grid3D"),
        "CoupledPathfinder": make(router_cpp.CoupledPathfinder, "CoupledPathfinder"),
    }


# --------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------


class _Encoder:
    def __init__(self, native_module_name: str):
        self.arrays: dict[str, np.ndarray] = {}
        self._native = native_module_name

    def array(self, values: np.ndarray, *, delta: bool) -> str:
        key = f"a{len(self.arrays)}"
        values = np.asarray(values, dtype=np.int64)
        if delta and values.size:
            values = np.diff(values, prepend=0)
        self.arrays[key] = _narrow(values)
        return key

    def value(self, v: Any) -> Any:
        if v is None or isinstance(v, (bool, str)):
            return v
        if isinstance(v, (int, np.integer)):
            return int(v)
        if isinstance(v, (float, np.floating)):
            return float(v)
        if isinstance(v, (list, tuple)):
            if len(v) >= _ARRAY_MIN_LEN and all(
                isinstance(x, int) and not isinstance(x, bool) for x in v
            ):
                return {
                    "__array__": self.array(np.asarray(v), delta=False),
                    "kind": type(v).__name__,
                }
            tag = "__tuple__" if isinstance(v, tuple) else "__list__"
            return {tag: [self.value(x) for x in v]}
        if isinstance(v, dict):
            return {"__dict__": [[self.value(k), self.value(x)] for k, x in v.items()]}
        oid = getattr(v, "_capture_id", None)
        if oid is not None:
            return {"__obj__": int(oid)}
        if type(v).__module__.startswith(self._native):
            fields = {
                name: self.value(getattr(v, name))
                for name in sorted(dir(v))
                if not name.startswith("_") and not callable(getattr(v, name))
            }
            return {"__struct__": type(v).__name__, "fields": fields}
        raise TypeError(f"cannot encode {type(v).__qualname__} in a coupled-search capture")


def _narrow(values: np.ndarray) -> np.ndarray:
    if values.size == 0:
        return values.astype(np.int8)
    lo, hi = int(values.min()), int(values.max())
    for dtype in (np.int8, np.int16, np.int32):
        info = np.iinfo(dtype)
        if info.min <= lo and hi <= info.max:
            return values.astype(dtype)
    return values


def encode_log(log: NativeCallLog, native_module_name: str) -> tuple[list, dict[str, np.ndarray]]:
    """Encode ``log`` to JSON-safe entries plus the arrays they reference."""
    enc = _Encoder(native_module_name)
    out: list[dict] = []
    for e in log.entries:
        if isinstance(e, _Batch):
            out.append(
                {
                    "op": "batch",
                    "obj": e.obj,
                    "method": e.method,
                    "kinds": "".join(e.kinds),
                    "rows": len(e.columns[0]),
                    "columns": [enc.array(np.asarray(c), delta=True) for c in e.columns],
                }
            )
            continue
        item = {k: e[k] for k in ("op", "obj") if k in e}
        if e["op"] == "new":
            item["type"] = e["type"]
        else:
            item["method"] = e["method"]
        item["args"] = [enc.value(a) for a in e["args"]]
        item["kwargs"] = {k: enc.value(v) for k, v in e["kwargs"].items()}
        if "result" in e:
            item["result"] = enc.value(e["result"])
        out.append(item)
    return out, enc.arrays


# --------------------------------------------------------------------------
# Fixture I/O
# --------------------------------------------------------------------------


def save_fixture(
    directory: Path, manifest: dict, calls: list, arrays: dict[str, np.ndarray]
) -> None:
    """Write ``manifest.json`` and ``grid.npz`` (calls + arrays) deterministically."""
    directory.mkdir(parents=True, exist_ok=True)
    manifest = dict(manifest)
    manifest["capture_format"] = CAPTURE_FORMAT
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    payload = {"calls": np.frombuffer(json.dumps(calls).encode(), dtype=np.uint8)}
    payload.update(arrays)
    # ``np.savez_compressed`` stamps the current time into each member, so
    # write the zip by hand with a fixed timestamp: re-capturing the same
    # commit then yields a byte-identical file.
    with zipfile.ZipFile(directory / "grid.npz", "w", zipfile.ZIP_DEFLATED) as zf:
        for key in sorted(payload):
            buf = io.BytesIO()
            np.lib.format.write_array(buf, np.ascontiguousarray(payload[key]), allow_pickle=False)
            info = zipfile.ZipInfo(f"{key}.npy", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, buf.getvalue(), compresslevel=9)


@dataclass
class Fixture:
    """A loaded capture: the manifest and the decoded native call sequence."""

    manifest: dict
    calls: list[dict]


def load_fixture(directory: Path) -> Fixture:
    """Load and decode a fixture written by :func:`save_fixture`."""
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("capture_format") != CAPTURE_FORMAT:
        raise ValueError(f"unsupported capture format {manifest.get('capture_format')!r}")
    with np.load(directory / "grid.npz", allow_pickle=False) as npz:
        arrays = {k: npz[k] for k in npz.files}
    raw = json.loads(arrays.pop("calls").tobytes().decode())
    calls = [_decode_entry(e, arrays) for e in raw]
    return Fixture(manifest=manifest, calls=calls)


def _decode_entry(e: dict, arrays: dict[str, np.ndarray]) -> dict:
    if e["op"] == "batch":
        columns = [np.cumsum(arrays[k].astype(np.int64)) for k in e["columns"]]
        return {**e, "columns": columns}
    out = dict(e)
    out["args"] = [_decode(a, arrays) for a in e["args"]]
    out["kwargs"] = {k: _decode(v, arrays) for k, v in e["kwargs"].items()}
    if "result" in e:
        out["result"] = _decode(e["result"], arrays)
    return out


def _decode(v: Any, arrays: dict[str, np.ndarray]) -> Any:
    if not isinstance(v, dict):
        return v
    if "__list__" in v:
        return [_decode(x, arrays) for x in v["__list__"]]
    if "__tuple__" in v:
        return tuple(_decode(x, arrays) for x in v["__tuple__"])
    if "__dict__" in v:
        return {_decode(k, arrays): _decode(x, arrays) for k, x in v["__dict__"]}
    if "__array__" in v:
        values = arrays[v["__array__"]].astype(np.int64).tolist()
        return tuple(values) if v["kind"] == "tuple" else values
    if "__obj__" in v:
        return _ObjRef(v["__obj__"])
    if "__struct__" in v:
        return _Struct(v["__struct__"], {k: _decode(x, arrays) for k, x in v["fields"].items()})
    raise ValueError(f"unknown encoded value {v!r}")


@dataclass(frozen=True)
class _ObjRef:
    obj: int


@dataclass
class _Struct:
    type_name: str
    fields: dict[str, Any]


# --------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------


def diagnostics_of(result: Any) -> dict:
    """Comparable diagnostics of a native or recorded ``route`` result.

    Same keys and conversions as ``CppCoupledPathfinder.route`` plus
    ``success`` and the path length.
    """
    if isinstance(result, _Struct):
        f = result.fields
        rejections = f["rejections"]
        path = f["path"]
        return {
            "success": bool(f["success"]),
            "iterations": int(f["iterations"]),
            "best_progress": float(f["best_progress"]),
            "timeout_exceeded": bool(f["timeout_exceeded"]),
            "iteration_limited": bool(f["iteration_limited"]),
            "rejections": {str(k): int(x) for k, x in sorted(dict(rejections).items())},
            "path_len": len(path),
        }
    return {
        "success": bool(result.success),
        "iterations": int(result.iterations),
        "best_progress": float(result.best_progress),
        "timeout_exceeded": bool(result.timeout_exceeded),
        "iteration_limited": bool(result.iteration_limited),
        "rejections": {str(k): int(x) for k, x in sorted(dict(result.rejections).items())},
        "path_len": len(result.path),
    }


@dataclass
class ReplayResult:
    """Diagnostics of every replayed ``route`` call, in order."""

    routes: list[dict]
    objects: dict[int, Any]


def replay(calls: list[dict], router_cpp=None) -> ReplayResult:
    """Re-execute a decoded call sequence against the native module.

    ``calls`` is :attr:`Fixture.calls`, possibly edited by a control.  Returns
    the diagnostics of every ``route`` call (see :func:`diagnostics_of`).
    """
    if router_cpp is None:
        from . import cpp_backend

        router_cpp = cpp_backend.router_cpp
    objects: dict[int, Any] = {}

    def resolve(v: Any) -> Any:
        if isinstance(v, _ObjRef):
            return objects[v.obj]
        if isinstance(v, _Struct):
            obj = getattr(router_cpp, v.type_name)()
            for name, x in v.fields.items():
                setattr(obj, name, resolve(x))
            return obj
        if isinstance(v, list):
            return [resolve(x) for x in v]
        if isinstance(v, tuple):
            return tuple(resolve(x) for x in v)
        if isinstance(v, dict):
            return {resolve(k): resolve(x) for k, x in v.items()}
        return v

    routes: list[dict] = []
    for e in calls:
        op = e["op"]
        if op == "new":
            cls = getattr(router_cpp, e["type"])
            objects[e["obj"]] = cls(
                *[resolve(a) for a in e["args"]],
                **{k: resolve(v) for k, v in e["kwargs"].items()},
            )
        elif op == "batch":
            fn = getattr(objects[e["obj"]], e["method"])
            cols = [
                c.astype(bool).tolist() if kind == "b" else c.tolist()
                for c, kind in zip(e["columns"], e["kinds"], strict=True)
            ]
            for row in zip(*cols, strict=True):
                fn(*row)
        else:
            fn = getattr(objects[e["obj"]], e["method"])
            result = fn(
                *[resolve(a) for a in e["args"]],
                **{k: resolve(v) for k, v in e["kwargs"].items()},
            )
            if e["method"] == "route":
                routes.append(diagnostics_of(result))
    return ReplayResult(routes=routes, objects=objects)


def recorded_routes(calls: list[dict]) -> list[dict]:
    """Diagnostics recorded at capture time for every ``route`` call."""
    return [
        diagnostics_of(e["result"])
        for e in calls
        if e["op"] == "call" and e["method"] == "route" and "result" in e
    ]


__all__ = [
    "CAPTURE_FORMAT",
    "CaptureStop",
    "Fixture",
    "NativeCallLog",
    "ReplayResult",
    "diagnostics_of",
    "encode_log",
    "load_fixture",
    "recorded_routes",
    "recording_native_types",
    "replay",
    "save_fixture",
]
