"""Activity streams: averaging samples into points, and the garmin_get_activity_stream tool."""

import json
from datetime import date
from typing import Any

import anyio
import pytest
from fitfiles import simple_run, zipped
from mcp import Client

from garmin_connect_personal_mcp import config, db, streams
from garmin_connect_personal_mcp.fit import Sample
from garmin_connect_personal_mcp.server import build_server

T0 = 1_700_000_000


def samples(seconds: int, **fields: float | None) -> list[Sample]:
    return [Sample(timestamp=T0 + i, **fields) for i in range(seconds)]  # type: ignore[arg-type]


# --- building points


def test_auto_interval_keeps_points_near_target() -> None:
    assert streams.auto_interval(120) == 1
    assert streams.auto_interval(3600) == 15
    assert streams.auto_interval(6 * 3600) == 120
    assert streams.auto_interval(10**7) == 600


def test_points_average_their_interval() -> None:
    data = [
        Sample(timestamp=T0 + i, heart_rate_bpm=100 + i, speed_mps=3.0, distance_m=3.0 * i)
        for i in range(20)
    ]
    s = streams.build(1, "running", data, interval_seconds=10)
    assert s.point_count == 2 and s.elapsed_minutes == [0.0, 0.17]
    assert s.heart_rate_bpm == [104, 114]  # mean of 100-109 is 104.5, rounded to even
    assert s.speed_kmh == [10.8, 10.8]
    assert s.pace_min_per_km == [5.56, 5.56]
    assert s.distance_km == [0.03, 0.06]  # cumulative, at each point's end
    assert s.total_minutes == 0.32
    assert "average of 10 seconds" in s.notes[-1]


def test_running_cadence_is_steps_per_minute() -> None:
    s = streams.build(1, "trail_running", samples(5, cadence=85.5), interval_seconds=5)
    assert s.cadence_spm == [171] and s.cadence_rpm is None


def test_cycling_has_rpm_and_no_pace() -> None:
    s = streams.build(1, "cycling", samples(5, cadence=90, speed_mps=8.0), interval_seconds=5)
    assert s.cadence_rpm == [90] and s.cadence_spm is None
    assert s.pace_min_per_km is None and s.speed_kmh == [28.8]


def test_standing_still_has_no_pace() -> None:
    s = streams.build(1, "running", samples(5, speed_mps=0.1), interval_seconds=5)
    assert s.pace_min_per_km == [None]


def test_unrecorded_fields_are_null_and_noted() -> None:
    s = streams.build(1, "running", samples(5, heart_rate_bpm=120), interval_seconds=1)
    assert s.heart_rate_bpm == [120] * 5
    assert s.power_w is None and s.altitude_m is None and s.speed_kmh is None
    assert "power" in s.notes[0] and "heart rate" not in s.notes[0]


def test_paused_time_is_a_gap() -> None:
    data = samples(10, heart_rate_bpm=120) + [
        Sample(timestamp=T0 + 600 + i, heart_rate_bpm=130) for i in range(10)
    ]
    s = streams.build(1, "running", data, interval_seconds=10)
    assert s.elapsed_minutes == [0.0, 10.0]


def test_window() -> None:
    data = samples(600, heart_rate_bpm=120)
    s = streams.build(1, "running", data, interval_seconds=60, start_minute=2, end_minute=4)
    assert s.elapsed_minutes == [2.0, 3.0, 4.0]
    assert (s.start_minute, s.end_minute) == (2.0, 4.0)


def test_window_past_the_end() -> None:
    s = streams.build(1, "running", samples(60, heart_rate_bpm=1), start_minute=30)
    assert s.point_count == 0 and "No samples in that window" in s.notes[0]


def test_no_samples() -> None:
    s = streams.build(1, "running", [])
    assert s.point_count == 0 and s.heart_rate_bpm is None
    assert "no samples" in s.notes[0]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"interval_seconds": -1}, "interval_seconds"),
        ({"interval_seconds": 601}, "interval_seconds"),
        ({"start_minute": -1}, "negative"),
        ({"start_minute": 5, "end_minute": 5}, "after start_minute"),
        ({"interval_seconds": 1}, "over 600 points"),
    ],
)
def test_bad_arguments(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        streams.build(1, "running", samples(3600), **kwargs)


# --- the tool


def call(args: dict[str, Any]) -> tuple[bool, Any, str]:
    async def go() -> tuple[bool, Any, str]:
        async with Client(build_server(config.data_paths())) as client:
            result = await client.call_tool("garmin_get_activity_stream", args)
            text = result.content[0].text  # type: ignore[union-attr]
            return result.is_error, result.structured_content, text

    return anyio.run(go)


def store(fit_bytes: bytes | None, activity_id: int = 7, kind: str = "running") -> None:
    paths = config.data_paths()
    conn = db.connect(paths.db)
    db.upsert_activity(
        conn,
        activity_id,
        {"activityId": activity_id},
        calendar_date=date(2026, 3, 1),
        activity_type=kind,
    )
    if fit_bytes is not None:
        paths.fit_dir.mkdir(parents=True, exist_ok=True)
        (paths.fit_dir / f"{activity_id}.zip").write_bytes(fit_bytes)
        db.set_fit_path(conn, activity_id, f"{activity_id}.zip")
    conn.commit()
    conn.close()


def test_tool_returns_the_stream() -> None:
    store(zipped(simple_run(seconds=30)))
    error, stream, text = call({"activity_id": 7, "interval_seconds": 10})
    assert not error, text
    assert json.loads(text) == stream
    assert stream["point_count"] == 3 and stream["cadence_spm"] == [171, 171, 171]
    assert stream["heart_rate_bpm"][0] == 144  # mean of 140-149 is 144.5, rounded to even
    for word in ("lat", "long", "position", "123456789"):
        assert word not in text


@pytest.mark.parametrize(
    ("fit_bytes", "message"),
    [
        (None, "garmin_sync"),
        (b"not a zip or a FIT file", "Couldn't read"),
    ],
)
def test_tool_without_a_readable_file(fit_bytes: bytes | None, message: str) -> None:
    store(fit_bytes)
    error, _, text = call({"activity_id": 7})
    assert error and message in text and "garmin_get_activity" in text


def test_tool_file_missing_from_disk() -> None:
    store(zipped(simple_run()))
    (config.data_paths().fit_dir / "7.zip").unlink()
    error, _, text = call({"activity_id": 7})
    assert error and "missing" in text


def test_tool_unknown_activity_and_no_archive() -> None:
    error, _, text = call({"activity_id": 7})
    assert error and "sync" in text
    store(None)
    error, _, text = call({"activity_id": 8})
    assert error and "garmin_list_activities" in text


def test_tool_bad_window() -> None:
    store(zipped(simple_run()))
    error, _, text = call({"activity_id": 7, "start_minute": 3, "end_minute": 1})
    assert error and "end_minute" in text
