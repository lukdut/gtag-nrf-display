"""Stable discovery schema, independent of the framebuffer protocol version."""
from __future__ import annotations
from dataclasses import asdict, dataclass
import re
import struct


@dataclass(frozen=True)
class FirmwareInfo:
    firmware_version: str | None
    schema: int = 1
    protocol_min: int = 1
    protocol_max: int = 1
    pixel_format: int = 1
    codecs: int = 3
    features: int = 1
    width: int = 256
    height: int = 128
    max_encoded_size: int = 4096
    max_chunk_size: int = 32
    legacy: bool = False

    @classmethod
    def legacy_v1(cls) -> "FirmwareInfo":
        # Only the original v1 RAW frame grammar is assumed. No freshness lease.
        return cls(None, schema=0, codecs=1, features=0, legacy=True)

    @classmethod
    def parse(cls, raw: bytes) -> "FirmwareInfo":
        if len(raw) < 40 or raw[0] != 1:
            raise ValueError("Unsupported or truncated GTag firmware-info schema; update the integration")
        version = raw[20:40].split(b"\0", 1)[0].decode("ascii")
        if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,18}", version):
            raise ValueError("Invalid GTag firmware version")
        fields = struct.unpack_from('<BBBBIIHHHH', raw)
        info = cls(version, *fields)
        if info.protocol_min == 0 or info.protocol_max < info.protocol_min or not info.max_chunk_size:
            raise ValueError("Invalid GTag firmware capabilities")
        return info

    @classmethod
    def from_dict(cls, value: dict) -> "FirmwareInfo":
        if not isinstance(value, dict) or not set(cls.__dataclass_fields__) <= set(value):
            raise ValueError("Invalid firmware metadata")
        info = cls(**{name: value[name] for name in cls.__dataclass_fields__})
        for name in ('schema', 'protocol_min', 'protocol_max', 'pixel_format', 'codecs', 'features',
                     'width', 'height', 'max_encoded_size', 'max_chunk_size'):
            if type(getattr(info, name)) is not int or not 0 <= getattr(info, name) <= 0xffffffff:
                raise ValueError("Invalid firmware metadata")
        if type(info.legacy) is not bool or (info.firmware_version is not None and
                (not isinstance(info.firmware_version, str) or not re.fullmatch(r'[0-9A-Za-z][0-9A-Za-z.+_-]{0,18}', info.firmware_version))):
            raise ValueError("Invalid firmware version")
        if info.legacy:
            if info != cls.legacy_v1():
                raise ValueError("Invalid legacy firmware metadata")
        elif (info.schema != 1 or info.firmware_version is None or not info.protocol_min
              or info.protocol_max < info.protocol_min or not info.max_chunk_size):
            raise ValueError("Invalid firmware metadata")
        return info

    @property
    def freshness(self) -> bool:
        return bool(self.features & 1)

    def validate_transfer(self, chunk_size: int = 1) -> None:
        if self.schema not in (0, 1) or not self.protocol_min <= 1 <= self.protocol_max:
            raise ValueError("No compatible GTag frame protocol; update the integration/converter or firmware")
        if (self.width, self.height, self.pixel_format) != (256, 128, 1):
            raise ValueError("Unsupported GTag display dimensions or pixel format")
        if not self.codecs & 3 or self.max_encoded_size < 1 or self.max_chunk_size < chunk_size:
            raise ValueError("No compatible GTag codec or transfer limits")

    def attributes(self) -> dict:
        return {"firmware_version": self.firmware_version,
                "firmware_codecs": [name for bit, name in enumerate(("raw", "white_rle_v1")) if self.codecs & (1 << bit)],
                "firmware_features": [name for bit, name in enumerate(("freshness", "battery_voltage", "battery_bar", "battery_protection")) if self.features & (1 << bit)],
                "firmware_capabilities": asdict(self),
                "firmware_legacy": self.legacy,
                "firmware_freshness_supported": self.freshness}
