"""Build small synthetic FIT files for tests. Never use real activity files."""

import io
import struct
import zipfile
from collections.abc import Iterable, Mapping, Sequence

FIT_EPOCH = 631065600

# FIT base type bytes used by the record fields below.
UINT8, SINT8, UINT16, UINT32 = 0x02, 0x01, 0x84, 0x86

# record field number -> (base type, struct code, scale, offset)
RECORD_FIELDS = {
    253: (UINT32, "I", 1, 0),  # timestamp, seconds since the FIT epoch
    0: (0x85, "i", 1, 0),  # position_lat (semicircles): the decoder must ignore it
    1: (0x85, "i", 1, 0),  # position_long
    2: (UINT16, "H", 5, 500),  # altitude
    3: (UINT8, "B", 1, 0),  # heart_rate
    4: (UINT8, "B", 1, 0),  # cadence
    5: (UINT32, "I", 100, 0),  # distance
    6: (UINT16, "H", 1000, 0),  # speed
    7: (UINT16, "H", 1, 0),  # power
    13: (SINT8, "b", 1, 0),  # temperature
    53: (UINT8, "B", 128, 0),  # fractional_cadence
    73: (UINT32, "I", 1000, 0),  # enhanced_speed
    78: (UINT32, "I", 5, 500),  # enhanced_altitude
}
INVALID = {"B": 0xFF, "b": 0x7F, "H": 0xFFFF, "I": 0xFFFFFFFF, "i": 0x7FFFFFFF}


def unix(fit_seconds: int) -> int:
    return fit_seconds + FIT_EPOCH


def definition(
    local: int,
    global_num: int,
    fields: list[tuple[int, int, int]],
    *,
    big_endian: bool = False,
    dev_fields: Sequence[tuple[int, int, int]] = (),
) -> bytes:
    header = 0x40 | local | (0x20 if dev_fields else 0)
    endian = ">" if big_endian else "<"
    out = bytes([header, 0, 1 if big_endian else 0]) + struct.pack(endian + "H", global_num)
    out += bytes([len(fields)])
    for number, size, base in fields:
        out += bytes([number, size, base])
    if dev_fields:
        out += bytes([len(dev_fields)])
        for number, size, index in dev_fields:
            out += bytes([number, size, index])
    return out


def record_fields(names: Iterable[int]) -> list[tuple[int, int, int]]:
    return [(n, struct.calcsize(RECORD_FIELDS[n][1]), RECORD_FIELDS[n][0]) for n in names]


def record(
    local: int,
    values: Mapping[int, float | None],
    order: Iterable[int],
    *,
    big_endian: bool = False,
    compressed_offset: int | None = None,
    dev_bytes: bytes = b"",
) -> bytes:
    """A record data message. None writes the field's invalid value."""
    endian = ">" if big_endian else "<"
    if compressed_offset is not None:
        out = bytes([0x80 | (local << 5) | compressed_offset])
    else:
        out = bytes([local])
    for number in order:
        _, code, scale, offset = RECORD_FIELDS[number]
        value = values.get(number)
        raw = INVALID[code] if value is None else round((value + offset) * scale)
        out += struct.pack(endian + code, raw)
    return out + dev_bytes


def fit_file(*messages: bytes) -> bytes:
    body = b"".join(messages)
    header = struct.pack("<BBHI4s", 12, 0x20, 2132, len(body), b".FIT")
    return header + body + b"\x00\x00"  # the decoder doesn't check the CRC


def zipped(fit: bytes, name: str = "activity.fit") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, fit)
    return buffer.getvalue()


def simple_run(seconds: int = 5, start: int = 1_000_000_000) -> bytes:
    """A short synthetic run: one record per second with every common field."""
    order = [253, 0, 1, 3, 4, 53, 5, 73, 78, 7, 13]
    messages = [definition(0, 20, record_fields(order))]
    for i in range(seconds):
        messages.append(
            record(
                0,
                {
                    253: start + i,
                    0: 123456789,
                    1: -98765432,
                    3: 140 + i,
                    4: 85,
                    53: 0.5,
                    5: 3.0 * i,
                    73: 3.0,
                    78: 100 + i,
                    7: 250,
                    13: 18,
                },
                order,
            )
        )
    return fit_file(*messages)
