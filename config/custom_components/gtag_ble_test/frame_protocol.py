"""Transport-independent G-Tag frame-session model.

Bluetooth, Zigbee, serial, etc. are adapters around this descriptor and the
four logical operations BEGIN / DATA(offset,payload) / COMMIT / STATUS.
"""
from __future__ import annotations

from dataclasses import dataclass
import secrets

from .frame_codec import EncodedFrame, encode_best

PROTOCOL_VERSION = 1

STATE_IDLE = 0
STATE_RECEIVING = 1
STATE_COMPLETE = 2
STATE_ERROR = 3

ERROR_NAMES = {
    0: "none",
    1: "not_receiving",
    2: "wrong_offset",
    3: "out_of_bounds",
    4: "incomplete",
    5: "crc_mismatch",
    6: "conflicting_duplicate",
    7: "unsupported_protocol",
    8: "unsupported_codec",
    9: "decode_error",
    10: "encoded_size",
}


@dataclass(frozen=True)
class FrameDescriptor:
    version: int
    codec: int
    encoded_size: int
    frame_id: int
    raw_crc32: int

    @classmethod
    def from_encoded(
        cls,
        encoded: EncodedFrame,
        *,
        frame_id: int | None = None,
    ) -> "FrameDescriptor":
        return cls(
            version=PROTOCOL_VERSION,
            codec=encoded.codec,
            encoded_size=encoded.encoded_size,
            frame_id=secrets.randbits(32) if frame_id is None else frame_id,
            raw_crc32=encoded.raw_crc32,
        )


@dataclass(frozen=True)
class PreparedFrame:
    descriptor: FrameDescriptor
    payload: bytes
    raw_size: int
    codec_name: str

    @classmethod
    def prepare(cls, raw: bytes) -> "PreparedFrame":
        encoded = encode_best(raw)
        return cls(
            descriptor=FrameDescriptor.from_encoded(encoded),
            payload=encoded.payload,
            raw_size=encoded.raw_size,
            codec_name=encoded.codec_name,
        )

    @property
    def bytes_saved(self) -> int:
        return self.raw_size - len(self.payload)

    @property
    def compression_ratio(self) -> float:
        return len(self.payload) / self.raw_size
