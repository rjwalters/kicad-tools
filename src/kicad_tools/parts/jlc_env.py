"""Load JLCPCB operator settings from a project ``.env`` (Issue #8859).

Only ``JLCPCB_``-prefixed keys are read, existing environment variables are
never overridden, and values are never logged or exposed by ``repr``/``as_dict``
(the contact block is personal data).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, fields
from pathlib import Path

PREFIX = "JLCPCB_"
ROOT_MARKERS = ("project.kct", "pyproject.toml")


def find_project_root(start: Path) -> Path | None:
    """Nearest ancestor of ``start`` holding ``project.kct`` / ``pyproject.toml``."""
    p = Path(start).resolve()
    for d in (p, *p.parents):
        if d.is_dir() and any((d / m).is_file() for m in ROOT_MARKERS):
            return d
    return None


def parse_env_text(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines (comments, ``export``, single/double quotes)."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        value = value.strip()
        if value[:1] in ("'", '"'):
            end = value.find(value[0], 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            hash_at = value.find(" #")
            if hash_at != -1:
                value = value[:hash_at].rstrip()
        out[key] = value
    return out


def load_jlc_env(board: Path, *, stream=None) -> list[str]:
    """Load ``JLCPCB_*`` keys from the project ``.env`` into ``os.environ``.

    Returns the key names set. Missing/unreadable ``.env`` is non-fatal.
    Logs the file and key *names* only.
    """
    stream = stream if stream is not None else sys.stderr
    root = find_project_root(Path(board).parent)
    if root is None:
        return []
    env_file = root / ".env"
    if not env_file.is_file():
        return []
    try:
        parsed = parse_env_text(env_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        print(f"warning: could not read {env_file}; continuing without it", file=stream)
        return []
    set_keys = []
    for key, value in parsed.items():
        if key.startswith(PREFIX) and key not in os.environ:
            os.environ[key] = value
            set_keys.append(key)
    print(
        f"Read {env_file}; set from it: {', '.join(set_keys) if set_keys else '(none)'}",
        file=stream,
    )
    return set_keys


@dataclass(repr=False)
class JLCContact:
    """Checkout contact details from ``JLCPCB_CONTACT_*``; values never shown in repr."""

    email: str = ""
    customer_type: str = ""
    company: str = ""
    first_name: str = ""
    last_name: str = ""
    country: str = ""
    state: str = ""
    city: str = ""
    street: str = ""
    building: str = ""
    postal_code: str = ""
    phone_country_code: str = ""
    phone: str = ""

    @classmethod
    def from_env(cls) -> JLCContact:
        kwargs = {
            f.name: os.environ.get(f"{PREFIX}CONTACT_{f.name.upper()}", "").strip()
            for f in fields(cls)
        }
        return cls(**kwargs)

    def _names(self) -> list[str]:
        return [f.name for f in fields(self) if getattr(self, f.name)]

    def set_keys(self) -> list[str]:
        return [f"{PREFIX}CONTACT_{n.upper()}" for n in self._names()]

    def as_dict(self) -> dict[str, bool]:
        """Which fields are set; never the values."""
        return {f.name: bool(getattr(self, f.name)) for f in fields(self)}

    def __repr__(self) -> str:
        return f"JLCContact(set={self._names()})"

    def __bool__(self) -> bool:
        return bool(self._names())

    def guided_lines(self, *, show_values: bool) -> list[str]:
        """Checklist step: values only when ``show_values`` (a TTY), else key names."""
        if not self:
            return []
        if not show_values:
            return ["Checkout contact details set in .env: " + ", ".join(self.set_keys())]
        lines = ["Checkout contact details: paste these"]
        lines += [f"    {n}: {getattr(self, n)}" for n in self._names()]
        return lines
