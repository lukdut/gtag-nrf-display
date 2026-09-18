"""Transport-independent framebuffer codecs for G-Tag.

No Bluetooth imports belong in this module.
"""
from __future__ import annotations

from dataclasses import dataclass
import zlib

RAW_FRAME_SIZE = 4096
CODEC_RAW = 0x00
CODEC_WHITE_RLE_V1 = 0x01

CODEC_NAMES = {
    CODEC_RAW: "raw",
    CODEC_WHITE_RLE_V1: "white_rle_v1",
}


def white_rle_v1_encode(raw: bytes) -> bytes:
    """Return the optimal encoding for WHITE_RLE_V1.

    Token format:
      0xxxxxxx -> literal block, len=(token&0x7f)+1, then literal bytes
      1xxxxxxx -> run of 0xff, len=(token&0x7f)+1

    Dynamic programming is used so isolated 0xff bytes do not accidentally
    make the stream larger by splitting otherwise cheap literal blocks.
    """
    n = len(raw)
    if n == 0:
        return b""

    # dp[i] = minimum encoded bytes needed for raw[i:]
    dp = [10**9] * (n + 1)
    choice: list[tuple[str, int] | None] = [None] * (n + 1)
    dp[n] = 0

    for i in range(n - 1, -1, -1):
        max_len = min(128, n - i)

        # Literal candidate.
        for count in range(1, max_len + 1):
            cost = 1 + count + dp[i + count]
            if cost < dp[i]:
                dp[i] = cost
                choice[i] = ("lit", count)

        # White-run candidate.
        if raw[i] == 0xFF:
            run = 0
            while run < max_len and raw[i + run] == 0xFF:
                run += 1
                cost = 1 + dp[i + run]
                if cost < dp[i]:
                    dp[i] = cost
                    choice[i] = ("white", run)

    out = bytearray()
    i = 0

    while i < n:
        item = choice[i]
        if item is None:
            raise RuntimeError("WHITE_RLE encoder internal error")

        kind, count = item

        if kind == "white":
            out.append(0x80 | (count - 1))
        else:
            out.append(count - 1)
            out.extend(raw[i:i + count])

        i += count

    return bytes(out)


def white_rle_v1_decode(encoded: bytes) -> bytes:
    out = bytearray()
    i = 0

    while i < len(encoded):
        token = encoded[i]
        i += 1
        count = (token & 0x7F) + 1

        if token & 0x80:
            out.extend(b"\xFF" * count)
        else:
            end = i + count
            if end > len(encoded):
                raise ValueError("truncated WHITE_RLE literal")
            out.extend(encoded[i:end])
            i = end

        if len(out) > RAW_FRAME_SIZE:
            raise ValueError("WHITE_RLE output overflow")

    if len(out) != RAW_FRAME_SIZE:
        raise ValueError(
            f"WHITE_RLE decoded to {len(out)} bytes, expected {RAW_FRAME_SIZE}"
        )

    return bytes(out)


@dataclass(frozen=True)
class EncodedFrame:
    codec: int
    payload: bytes
    raw_crc32: int
    raw_size: int = RAW_FRAME_SIZE

    @property
    def encoded_size(self) -> int:
        return len(self.payload)

    @property
    def bytes_saved(self) -> int:
        return self.raw_size - self.encoded_size

    @property
    def compression_ratio(self) -> float:
        return self.encoded_size / self.raw_size

    @property
    def codec_name(self) -> str:
        return CODEC_NAMES[self.codec]


def encode_best(raw: bytes, supported_codecs: int = 3, max_encoded_size: int = RAW_FRAME_SIZE) -> EncodedFrame:
    """Use WHITE_RLE only when it is strictly smaller than RAW."""
    if len(raw) != RAW_FRAME_SIZE:
        raise ValueError(f"frame must be exactly {RAW_FRAME_SIZE} bytes")

    crc = zlib.crc32(raw) & 0xFFFFFFFF
    candidates = []
    if supported_codecs & (1 << CODEC_RAW) and len(raw) <= max_encoded_size:
        candidates.append(EncodedFrame(CODEC_RAW, raw, crc))
    if supported_codecs & (1 << CODEC_WHITE_RLE_V1):
        compressed = white_rle_v1_encode(raw)
        if len(compressed) <= max_encoded_size:
            candidates.append(EncodedFrame(CODEC_WHITE_RLE_V1, compressed, crc))
    if not candidates:
        raise ValueError("No supported codec can encode this frame within the device limit")
    return min(candidates, key=lambda item: len(item.payload))
