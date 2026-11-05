"""Read the per-second samples from an activity's FIT file.

Sync stores each activity's original file as Garmin sends it: a ZIP holding
one FIT file. This module decodes just enough of the FIT format to pull the
`record` messages (one per second or so) out of it: heart rate, speed,
cadence, power, altitude, distance and temperature.

GPS positions are deliberately never decoded, so nothing built on this module
can send a route (and with it, where the user lives) to an assistant.

The decoder is small and dependency-free. It handles normal and compressed-
timestamp headers, both byte orders, developer fields (skipped) and chained
FIT files. It does not check CRCs; a damaged file raises FitError or yields
the samples read before the damage.
"""

import io
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path

FIT_EPOCH = 631065600  # 1989-12-31T00:00:00Z as a Unix timestamp
MAX_FIT_BYTES = 50_000_000  # an hours-long activity is a few MB; refuse anything absurd

RECORD = 20  # global message number of `record`
TIMESTAMP = 253  # field number of `timestamp` in every message

# Base type number -> (struct code, invalid value). Size comes from the struct code.
_BASE_TYPES: dict[int, tuple[str, int | None]] = {
    0x00: ("B", 0xFF),  # enum
    0x01: ("b", 0x7F),  # sint8
    0x02: ("B", 0xFF),  # uint8
    0x03: ("h", 0x7FFF),  # sint16
    0x04: ("H", 0xFFFF),  # uint16
    0x05: ("i", 0x7FFFFFFF),  # sint32
    0x06: ("I", 0xFFFFFFFF),  # uint32
    0x08: ("f", None),  # float32 (invalid is all-ones bits, checked as NaN)
    0x09: ("d", None),  # float64
    0x0A: ("B", 0x00),  # uint8z
    0x0B: ("H", 0x0000),  # uint16z
    0x0C: ("I", 0x00000000),  # uint32z
    0x0D: ("B", 0xFF),  # byte
    0x0E: ("q", 0x7FFFFFFFFFFFFFFF),  # sint64
    0x0F: ("Q", 0xFFFFFFFFFFFFFFFF),  # uint64
    0x10: ("Q", 0x0000000000000000),  # uint64z
}

# record field number -> (sample attribute, scale, offset). value = raw / scale - offset
_RECORD_FIELDS: dict[int, tuple[str, float, float]] = {
    2: ("altitude_m", 5, 500),
    3: ("heart_rate_bpm", 1, 0),
    4: ("cadence", 1, 0),
    5: ("distance_m", 100, 0),
    6: ("speed_mps", 1000, 0),
    7: ("power_w", 1, 0),
    13: ("temperature_c", 1, 0),
    53: ("fractional_cadence", 128, 0),
    73: ("enhanced_speed_mps", 1000, 0),
    78: ("enhanced_altitude_m", 5, 500),
}


class FitError(Exception):
    """The file isn't a FIT file this decoder can read."""


@dataclass(slots=True)
class Sample:
    """One `record` message. Missing fields are None, never zero."""

    timestamp: int  # Unix seconds, UTC
    heart_rate_bpm: float | None = None
    speed_mps: float | None = None
    cadence: float | None = None  # as recorded: revolutions (or strides) per minute
    power_w: float | None = None
    altitude_m: float | None = None
    distance_m: float | None = None
    temperature_c: float | None = None


@dataclass(slots=True)
class _Definition:
    global_num: int
    endian: str  # "<" or ">"
    fields: list[tuple[int, int, int]]  # (field number, size in bytes, base type number)
    dev_bytes: int  # total size of developer fields, skipped


def read_fit_file(path: Path) -> list[Sample]:
    """Samples from a stored activity file: a ZIP holding a FIT file, or a bare FIT file."""
    data = path.read_bytes()
    if zipfile.is_zipfile(io.BytesIO(data)):
        data = _unzip_fit(data)
    return decode(data)


def _unzip_fit(data: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = [m for m in archive.infolist() if m.filename.lower().endswith(".fit")]
        if not members:
            raise FitError("The activity's ZIP file holds no FIT file.")
        member = members[0]
        if member.file_size > MAX_FIT_BYTES:
            raise FitError("The FIT file is too large to read.")
        with archive.open(member) as f:
            return f.read(MAX_FIT_BYTES + 1)[:MAX_FIT_BYTES]


def decode(data: bytes) -> list[Sample]:
    """Samples from raw FIT bytes, in file order. Handles chained FIT files."""
    samples: list[Sample] = []
    pos = 0
    while pos < len(data):
        if len(data) - pos < 12:
            break  # trailing padding
        header_size = data[pos]
        if header_size < 12 or data[pos + 8 : pos + 12] != b".FIT":
            if not samples and pos == 0:
                raise FitError("Not a FIT file.")
            break
        (data_size,) = struct.unpack_from("<I", data, pos + 4)
        start = pos + header_size
        end = min(start + data_size, len(data))
        _decode_records(data, start, end, samples)
        pos = start + data_size + 2  # skip the file CRC
    return samples


def _decode_records(data: bytes, pos: int, end: int, samples: list[Sample]) -> None:
    definitions: dict[int, _Definition] = {}
    last_timestamp: int | None = None
    try:
        while pos < end:
            header = data[pos]
            pos += 1
            if header & 0x80:  # compressed timestamp header: always a data message
                local = (header >> 5) & 0x03
                offset = header & 0x1F
                if last_timestamp is None:
                    raise FitError("Compressed timestamp before any full timestamp.")
                timestamp = (last_timestamp & ~0x1F) + offset
                if offset < (last_timestamp & 0x1F):
                    timestamp += 0x20
                last_timestamp = timestamp
                pos = _read_data(data, pos, definitions, local, timestamp, samples)
            elif header & 0x40:  # definition message
                local = header & 0x0F
                pos = _read_definition(data, pos, bool(header & 0x20), definitions, local)
            else:
                pos, stamp = _read_data_with_timestamp(
                    data, pos, definitions, header & 0x0F, samples
                )
                if stamp is not None:
                    last_timestamp = stamp
    except (struct.error, IndexError):
        return  # truncated file: keep what was read


def _read_definition(
    data: bytes, pos: int, has_dev: bool, definitions: dict[int, _Definition], local: int
) -> int:
    architecture = data[pos + 1]
    endian = ">" if architecture == 1 else "<"
    (global_num,) = struct.unpack_from(endian + "H", data, pos + 2)
    count = data[pos + 4]
    pos += 5
    fields = []
    for _ in range(count):
        fields.append((data[pos], data[pos + 1], data[pos + 2] & 0x1F))
        pos += 3
    dev_bytes = 0
    if has_dev:
        dev_count = data[pos]
        pos += 1
        for _ in range(dev_count):
            dev_bytes += data[pos + 1]
            pos += 3
    definitions[local] = _Definition(global_num, endian, fields, dev_bytes)
    return pos


def _value(data: bytes, pos: int, size: int, base: int, endian: str) -> float | None:
    spec = _BASE_TYPES.get(base)
    if spec is None:
        return None  # strings and unknown types aren't needed
    code, invalid = spec
    if struct.calcsize(code) != size:
        return None  # arrays aren't needed either
    (value,) = struct.unpack_from(endian + code, data, pos)
    if invalid is None:
        return None if value != value else float(value)  # NaN is float32/64's invalid
    return None if value == invalid else float(value)


def _fields(data: bytes, pos: int, definition: _Definition) -> tuple[int, dict[int, float | None]]:
    values: dict[int, float | None] = {}
    for number, size, base in definition.fields:
        if pos + size > len(data):
            raise IndexError
        values[number] = _value(data, pos, size, base, definition.endian)
        pos += size
    return pos + definition.dev_bytes, values


def _definition(definitions: dict[int, _Definition], local: int) -> _Definition:
    definition = definitions.get(local)
    if definition is None:
        raise FitError(f"Data for local message {local} before its definition.")
    return definition


def _read_data_with_timestamp(
    data: bytes, pos: int, definitions: dict[int, _Definition], local: int, samples: list[Sample]
) -> tuple[int, int | None]:
    definition = _definition(definitions, local)
    pos, values = _fields(data, pos, definition)
    raw_stamp = values.get(TIMESTAMP)
    stamp = None if raw_stamp is None else int(raw_stamp)
    if definition.global_num == RECORD and stamp is not None:
        samples.append(_sample(stamp, values))
    return pos, stamp


def _read_data(
    data: bytes,
    pos: int,
    definitions: dict[int, _Definition],
    local: int,
    timestamp: int,
    samples: list[Sample],
) -> int:
    definition = _definition(definitions, local)
    pos, values = _fields(data, pos, definition)
    if definition.global_num == RECORD:
        samples.append(_sample(timestamp, values))
    return pos


def _sample(fit_timestamp: int, values: dict[int, float | None]) -> Sample:
    found: dict[str, float] = {}
    for number, (name, scale, offset) in _RECORD_FIELDS.items():
        raw = values.get(number)
        if raw is not None:
            found[name] = raw / scale - offset
    sample = Sample(timestamp=fit_timestamp + FIT_EPOCH)
    # Newer devices write the enhanced fields; they win over the 16-bit ones.
    sample.speed_mps = found.get("enhanced_speed_mps", found.get("speed_mps"))
    sample.altitude_m = found.get("enhanced_altitude_m", found.get("altitude_m"))
    sample.heart_rate_bpm = found.get("heart_rate_bpm")
    sample.power_w = found.get("power_w")
    sample.distance_m = found.get("distance_m")
    sample.temperature_c = found.get("temperature_c")
    if "cadence" in found:
        sample.cadence = found["cadence"] + found.get("fractional_cadence", 0.0)
    return sample
