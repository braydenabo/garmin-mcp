from pathlib import Path

import pytest
from fitfiles import (
    UINT8,
    definition,
    fit_file,
    record,
    record_fields,
    simple_run,
    unix,
    zipped,
)

from garmin_connect_personal_mcp.fit import FitError, Sample, decode, read_fit_file


def test_reads_every_record_field() -> None:
    samples = decode(simple_run(seconds=3, start=1_000_000_000))
    assert [s.timestamp for s in samples] == [unix(1_000_000_000 + i) for i in range(3)]
    first = samples[0]
    assert first.heart_rate_bpm == 140
    assert first.cadence == 85.5  # cadence plus fractional cadence
    assert first.speed_mps == 3.0
    assert first.altitude_m == pytest.approx(100)
    assert first.power_w == 250
    assert first.temperature_c == 18
    assert samples[2].distance_m == pytest.approx(6.0)


def test_never_exposes_gps() -> None:
    sample = decode(simple_run(seconds=1))[0]
    assert not any(
        "lat" in name or "long" in name or "position" in name for name in Sample.__slots__
    )  # type: ignore[attr-defined]
    assert 123456789 not in [v for v in vars_of(sample) if isinstance(v, float | int)]


def vars_of(sample: Sample) -> list[object]:
    return [getattr(sample, name) for name in Sample.__slots__]  # type: ignore[attr-defined]


def test_invalid_values_are_none_not_zero() -> None:
    order = [253, 3, 7]
    data = fit_file(
        definition(0, 20, record_fields(order)),
        record(0, {253: 500, 3: None, 7: None}, order),
    )
    sample = decode(data)[0]
    assert sample.heart_rate_bpm is None
    assert sample.power_w is None
    assert sample.speed_mps is None  # not in the file at all


def test_16_bit_speed_and_altitude_used_when_no_enhanced_fields() -> None:
    order = [253, 6, 2]
    data = fit_file(
        definition(0, 20, record_fields(order)), record(0, {253: 1, 6: 2.5, 2: 40}, order)
    )
    sample = decode(data)[0]
    assert sample.speed_mps == 2.5
    assert sample.altitude_m == pytest.approx(40)


def test_big_endian_messages() -> None:
    order = [253, 3, 6]
    data = fit_file(
        definition(0, 20, record_fields(order), big_endian=True),
        record(0, {253: 1000, 3: 150, 6: 4.2}, order, big_endian=True),
    )
    sample = decode(data)[0]
    assert sample.timestamp == unix(1000)
    assert sample.heart_rate_bpm == 150
    assert sample.speed_mps == pytest.approx(4.2)


def test_compressed_timestamps_roll_over() -> None:
    full = [253, 3]
    short = [3]
    data = fit_file(
        definition(0, 20, record_fields(full)),
        definition(1, 20, record_fields(short)),
        record(0, {253: 0x3E, 3: 100}, full),  # low five bits = 30
        record(1, {3: 101}, short, compressed_offset=31),  # 0x3F
        record(1, {3: 102}, short, compressed_offset=1),  # wraps to 0x41
    )
    assert [s.timestamp for s in decode(data)] == [unix(0x3E), unix(0x3F), unix(0x41)]


def test_developer_fields_are_skipped() -> None:
    order = [253, 3]
    data = fit_file(
        definition(0, 20, record_fields(order), dev_fields=[(0, 2, 0)]),
        record(0, {253: 10, 3: 120}, order, dev_bytes=b"\x01\x02"),
        record(0, {253: 11, 3: 121}, order, dev_bytes=b"\x03\x04"),
    )
    assert [s.heart_rate_bpm for s in decode(data)] == [120, 121]


def test_other_messages_are_ignored() -> None:
    order = [253, 3]
    data = fit_file(
        definition(2, 18, [(253, 4, 0x86), (5, 1, UINT8)]),  # a session message
        bytes([2]) + (1).to_bytes(4, "little") + bytes([1]),
        definition(0, 20, record_fields(order)),
        record(0, {253: 2, 3: 99}, order),
    )
    assert [s.heart_rate_bpm for s in decode(data)] == [99]


def test_chained_files() -> None:
    data = simple_run(seconds=2, start=100) + simple_run(seconds=2, start=200)
    assert [s.timestamp for s in decode(data)] == [unix(t) for t in (100, 101, 200, 201)]


def test_truncated_file_keeps_what_was_read() -> None:
    data = simple_run(seconds=5)
    assert len(decode(data[:-12])) == 4


def test_not_a_fit_file() -> None:
    with pytest.raises(FitError, match="Not a FIT file"):
        decode(b"hello, this is not a FIT file at all")


def test_data_before_definition() -> None:
    with pytest.raises(FitError, match="before its definition"):
        decode(fit_file(bytes([3, 1, 2, 3])))


def test_compressed_timestamp_needs_a_full_one_first() -> None:
    data = fit_file(
        definition(0, 20, record_fields([3])), record(0, {3: 90}, [3], compressed_offset=4)
    )
    with pytest.raises(FitError, match="Compressed timestamp"):
        decode(data)


def test_reads_zip_and_bare_files(tmp_path: Path) -> None:
    run = simple_run(seconds=2)
    (tmp_path / "a.zip").write_bytes(zipped(run, "123_ACTIVITY.fit"))
    (tmp_path / "b.bin").write_bytes(run)
    assert read_fit_file(tmp_path / "a.zip") == read_fit_file(tmp_path / "b.bin")
    assert len(read_fit_file(tmp_path / "a.zip")) == 2


def test_zip_without_fit_file(tmp_path: Path) -> None:
    (tmp_path / "a.zip").write_bytes(zipped(b"nope", "notes.txt"))
    with pytest.raises(FitError, match="holds no FIT file"):
        read_fit_file(tmp_path / "a.zip")
