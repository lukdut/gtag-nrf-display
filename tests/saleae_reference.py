"""Read this project's Saleae v4 capture; validate chunks before decoding words."""
from bisect import bisect_left, bisect_right
from pathlib import Path
import struct
import zipfile


def reference_words(path: Path) -> list[int]:
    channels = {}
    with zipfile.ZipFile(path) as archive:
        for channel in (3, 4, 5, 7):
            data = archive.read(f"digital-{channel}.bin")
            assert data[:8] == b"<SALEAE>"
            assert struct.unpack_from("<II", data, 8) == (4, 100)
            rate = struct.unpack_from("<d", data, 17)[0]
            assert rate == 12_000_000
            chunks = struct.unpack_from("<Q", data, 43)[0]
            offset, previous_end = 51, 0
            starts, values = [], []
            for _ in range(chunks):
                begin, end, value, length = struct.unpack_from("<QQHQ", data, offset)
                offset += 26
                assert begin == previous_end and value in (0, 1)
                stop, sample = offset + length, begin
                while offset < stop:
                    count = data[offset]
                    offset += 1
                    if count >= 64:
                        assert count & 192 == 64
                        count &= 63
                        while True:
                            assert offset < stop
                            byte = data[offset]
                            offset += 1
                            count = (count << 7) | (byte & 127)
                            if byte < 128:
                                break
                    if not values or value != values[-1]:
                        starts.append(sample)
                        values.append(value)
                    sample += count + 1
                    value ^= 1
                assert offset == stop and sample == end
                previous_end = end
            assert offset == len(data) and previous_end == 79_486_976
            channels[channel] = (starts, values)

    def transitions(channel, value):
        starts, values = channels[channel]
        return [s for s, v in zip(starts[1:], values[1:]) if v == value]

    reset_end = transitions(5, 1)[-1]
    clock = transitions(3, 1)
    cs_high = transitions(4, 1)
    dio_starts, dio_values = channels[7]
    words = []
    for start in transitions(4, 0):
        if start < reset_end:
            continue
        end = cs_high[bisect_right(cs_high, start)]
        edges = clock[bisect_left(clock, start):bisect_left(clock, end)]
        assert len(edges) == 9
        word = 0
        for edge in edges:
            word = (word << 1) | dio_values[bisect_right(dio_starts, edge) - 1]
        words.append(word)
    assert len(words) == 8272
    return words
