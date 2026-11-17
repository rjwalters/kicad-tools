"""Lightweight message types for KiCad IPC API wire format.

These dataclasses mirror the KiCad protobuf message structure but use
plain JSON serialization rather than protobuf binary encoding. KiCad's
IPC API accepts both protobuf and JSON-encoded messages over NNG.

Message structure follows the KiCad 9.0 API specification:
- Each request is an ``ApiRequest`` envelope containing a typed command
- Each response is an ``ApiResponse`` envelope with status and typed result
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class StatusCode(IntEnum):
    """API response status codes matching KiCad's ApiStatusCode enum."""

    AS_OK = 0
    AS_TIMEOUT = 1
    AS_BUSY = 2
    AS_ERROR = 3
    AS_INVALID_REQUEST = 4
    AS_UNHANDLED = 5
    AS_TOKEN_MISMATCH = 6
    AS_NOT_READY = 7


class ItemType(IntEnum):
    """Board item types matching KiCad's KiCadObjectType enum."""

    IT_TRACK = 0
    IT_VIA = 1
    IT_ZONE = 2
    IT_PAD = 3
    IT_FOOTPRINT = 4
    IT_TEXT = 5
    IT_ARC = 6


@dataclass
class Vector2(dict):
    """2D coordinate in KiCad internal units (nanometers)."""

    x_nm: int = 0
    y_nm: int = 0

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x_nm, "y": self.y_nm}

    @classmethod
    def from_dict(cls, d: dict) -> Vector2:
        return cls(x_nm=d.get("x", 0), y_nm=d.get("y", 0))


@dataclass
class KIID:
    """KiCad unique item identifier."""

    value: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"value": self.value}

    @classmethod
    def from_dict(cls, d: dict) -> KIID:
        return cls(value=d.get("value", ""))


@dataclass
class TrackSegment:
    """A PCB track segment."""

    start: Vector2 = field(default_factory=Vector2)
    end: Vector2 = field(default_factory=Vector2)
    width_nm: int = 250000  # 0.25mm default
    layer: str = "F.Cu"
    net: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.to_dict(),
            "end": self.end.to_dict(),
            "width": self.width_nm,
            "layer": self.layer,
            "net": self.net,
        }

    @classmethod
    def from_dict(cls, d: dict) -> TrackSegment:
        return cls(
            start=Vector2.from_dict(d.get("start", {})),
            end=Vector2.from_dict(d.get("end", {})),
            width_nm=d.get("width", 250000),
            layer=d.get("layer", "F.Cu"),
            net=d.get("net", 0),
        )


@dataclass
class Via:
    """A PCB via."""

    position: Vector2 = field(default_factory=Vector2)
    diameter_nm: int = 800000  # 0.8mm default
    drill_nm: int = 400000  # 0.4mm default
    net: int = 0
    start_layer: str = "F.Cu"
    end_layer: str = "B.Cu"

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position.to_dict(),
            "diameter": self.diameter_nm,
            "drill": self.drill_nm,
            "net": self.net,
            "start_layer": self.start_layer,
            "end_layer": self.end_layer,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Via:
        return cls(
            position=Vector2.from_dict(d.get("position", {})),
            diameter_nm=d.get("diameter", 800000),
            drill_nm=d.get("drill", 400000),
            net=d.get("net", 0),
            start_layer=d.get("start_layer", "F.Cu"),
            end_layer=d.get("end_layer", "B.Cu"),
        )


# --- API Envelope Messages ---


@dataclass
class ApiRequest:
    """Top-level API request envelope.

    Each request contains a ``command`` field identifying the operation
    and a ``params`` dict with command-specific parameters.
    """

    command: str = ""
    token: str = ""
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        msg: dict[str, Any] = {
            "command": self.command,
        }
        if self.token:
            msg["token"] = self.token
        if self.params:
            msg["params"] = self.params
        return msg


@dataclass
class ApiResponse:
    """Top-level API response envelope."""

    status: StatusCode = StatusCode.AS_OK
    message: str = ""
    result: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> ApiResponse:
        status_val = d.get("status", 0)
        if isinstance(status_val, int):
            status = StatusCode(status_val)
        else:
            status = StatusCode.AS_OK
        return cls(
            status=status,
            message=d.get("message", ""),
            result=d.get("result", {}),
        )

    @property
    def ok(self) -> bool:
        """Check if the response indicates success."""
        return self.status == StatusCode.AS_OK
