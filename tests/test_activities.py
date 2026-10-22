"""Activity summaries and the activity tools, from synthetic records shaped like Garmin's."""

import json
from datetime import date, timedelta
from typing import Any

import anyio
import pytest
from mcp import Client

from garmin_connect_personal_mcp import activities, config, db
from garmin_connect_personal_mcp.server import build_server

TODAY = date(2026, 3, 15)


def run_record(
    n: int = 1, when: date = TODAY, kind: str = "running", **extra: Any
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "activityId": n,
        "activityName": f"Morning {kind}",
        "activityType": {"typeKey": kind},
        "startTimeLocal": f"{when.isoformat()} 07:05:09",
        "startTimeGMT": f"{when.isoformat()} 12:05:09",
        "duration": 3000.0,
        "movingDuration": 2950.0,
        "elapsedDuration": 3100.0,
        "distance": 10000.0,
        "averageSpeed": 3.3333,  # m/s, i.e. 5:00 min/km
        "maxSpeed": 5.0,
        "averageHR": 151.4,
        "maxHR": 178.0,
        "calories": 702.6,
        "elevationGain": 87.2,
        "elevationLoss": 85.0,
        "averageRunningCadenceInStepsPerMinute": 171.6,
        "activityTrainingLoad": 120.4,
        "aerobicTrainingEffect": 3.4,
        "anaerobicTrainingEffect": 1.1,
        "trainingEffectLabel": "TEMPO",
        "steps": 8600,
        "lapCount": 10,
        "locationName": "Springfield",
        "hrTimeInZone_1": 60.0,
        "hrTimeInZone_2": 600.0,
        "hrTimeInZone_3": 1200.0,
        "hrTimeInZone_4": 1140.0,
        "hrTimeInZone_5": 0.0,
        "splitSummaries": [
            {"splitType": "RWD_RUN", "distance": 9800.0, "duration": 2900.0, "averageSpeed": 3.38}
        ],
        # Personal details the tools must never return:
        "ownerFullName": "Pat Example",
        "ownerDisplayName": "pat-example",
        "ownerProfileImageUrlSmall": "https://example.invalid/me.png",
        "startLatitude": 40.12345,
        "startLongitude": -75.12345,
        "endLatitude": 40.12346,
        "endLongitude": -75.12346,
    }
    raw.update(extra)
    return raw


def store(records: list[dict[str, Any]], fit: bool = False) -> None:
    conn = db.connect(config.data_paths().db)
    for raw in records:
        when = date.fromisoformat(raw["startTimeLocal"][:10])
        db.upsert_activity(
            conn,
            raw["activityId"],
            raw,
            start_time_utc=raw["startTimeGMT"],
            calendar_date=when,
            activity_type=raw["activityType"]["typeKey"],
        )
        if fit:
            db.set_fit_path(conn, raw["activityId"], f"{raw['activityId']}.zip")
    conn.commit()
    conn.close()


def call(name: str, args: dict[str, Any] | None = None) -> tuple[bool, Any, str]:
    server = build_server(config.data_paths(), today=lambda: TODAY)

    async def main() -> tuple[bool, Any, str]:
        async with Client(server) as client:
            result = await client.call_tool(name, args or {})
            text = result.content[0].text  # type: ignore[union-attr]
            return bool(result.is_error), result.structured_content, text

    return anyio.run(main)


# --- conversion


def test_summary_converts_units() -> None:
    s = activities.summarize(1, run_record())
    assert s.start_local == "2026-03-15T07:05"
    assert s.duration_minutes == 50.0
    assert s.distance_km == 10.0
    assert s.avg_pace_min_per_km == 5.0
    assert s.avg_speed_kmh == 12.0
    assert s.avg_hr_bpm == 151 and s.calories_kcal == 703
    assert s.elevation_gain_m == 87


def test_pace_only_for_on_foot_sports() -> None:
    assert activities.summarize(1, run_record(kind="trail_running")).avg_pace_min_per_km == 5.0
    assert activities.summarize(1, run_record(kind="walking")).avg_pace_min_per_km == 5.0
    swim = activities.summarize(1, run_record(kind="open_water_swimming"))
    assert swim.avg_pace_min_per_km is None and swim.avg_speed_kmh == 12.0
    assert activities.summarize(1, run_record(averageSpeed=0)).avg_pace_min_per_km is None


def test_detail_fields() -> None:
    d = activities.detail(1, run_record(), fit_saved=True)
    assert d.hr_zone_minutes == [1.0, 10.0, 20.0, 19.0, 0.0]
    assert d.avg_run_cadence_spm == 172
    assert d.splits == [
        activities.Split(
            split_type="RWD_RUN", distance_km=9.8, duration_minutes=48.3, avg_speed_kmh=12.2
        )
    ]
    assert d.fit_file_saved is True
    assert d.location_name == "Springfield"


def test_missing_and_odd_fields_are_none() -> None:
    raw = {
        "activityId": 2,
        "activityType": "running",
        "duration": "50",
        "steps": True,
        "splitSummaries": "x",
    }
    d = activities.detail(2, raw, fit_saved=False)
    assert d.activity_type is None and d.duration_minutes is None and d.steps is None
    assert d.splits == [] and d.start_local is None


def test_personal_details_never_leave_the_archive() -> None:
    text = activities.detail(1, run_record(), fit_saved=False).model_dump_json()
    for secret in ["Pat Example", "pat-example", "example.invalid", "40.123", "-75.123"]:
        assert secret not in text


# --- garmin_list_activities


def test_list_newest_first_with_paging() -> None:
    store([run_record(n, TODAY - timedelta(days=n)) for n in range(1, 6)])
    error, page, _ = call("garmin_list_activities", {"limit": 2})
    assert not error
    assert [a["activity_id"] for a in page["activities"]] == [1, 2]
    assert page["total"] == 5 and page["next_cursor"] == "2"
    _, page, _ = call("garmin_list_activities", {"limit": 2, "cursor": "4"})
    assert [a["activity_id"] for a in page["activities"]] == [5]
    assert page["next_cursor"] is None


def test_list_defaults_to_last_30_days() -> None:
    store([run_record(1, TODAY), run_record(2, TODAY - timedelta(days=40))])
    _, page, _ = call("garmin_list_activities")
    assert [a["activity_id"] for a in page["activities"]] == [1]


def test_list_type_filter_matches_partially() -> None:
    store(
        [
            run_record(1, kind="running"),
            run_record(2, kind="trail_running"),
            run_record(3, kind="cycling"),
        ]
    )
    _, page, _ = call("garmin_list_activities", {"activity_type": "Running"})
    assert sorted(a["activity_id"] for a in page["activities"]) == [1, 2]


def test_list_unknown_type_lists_what_exists() -> None:
    store([run_record(1, kind="running")])
    _, page, _ = call("garmin_list_activities", {"activity_type": "rowing"})
    assert page["total"] == 0
    assert "running" in page["notes"][0]


def test_list_empty_range_suggests_sync() -> None:
    db.connect(config.data_paths().db).close()
    _, page, _ = call("garmin_list_activities")
    assert "garmin_sync" in page["notes"][0]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"limit": 0}, "limit"),
        ({"limit": 101}, "limit"),
        ({"cursor": "abc"}, "cursor"),
        ({"start": "2026-03-10", "end": "2026-03-01"}, "before start"),
    ],
)
def test_list_errors(args: dict[str, Any], message: str) -> None:
    db.connect(config.data_paths().db).close()
    error, _, text = call("garmin_list_activities", args)
    assert error and message in text


# --- garmin_get_activity


def test_get_activity() -> None:
    store([run_record(7)], fit=True)
    error, detail, text = call("garmin_get_activity", {"activity_id": 7})
    assert not error
    assert detail["distance_km"] == 10.0 and detail["fit_file_saved"] is True
    assert json.loads(text) == detail
    assert "Pat Example" not in text and "40.123" not in text


def test_get_unknown_activity_points_to_list() -> None:
    db.connect(config.data_paths().db).close()
    error, _, text = call("garmin_get_activity", {"activity_id": 999})
    assert error and "garmin_list_activities" in text


def test_describe_data_counts_types() -> None:
    store([run_record(1), run_record(2), run_record(3, kind="cycling")])
    _, described, _ = call("garmin_describe_data")
    assert described["activity_types"] == {"running": 2, "cycling": 1}


@pytest.mark.parametrize(
    ("name", "args"), [("garmin_list_activities", {}), ("garmin_get_activity", {"activity_id": 1})]
)
def test_activity_tools_without_archive_suggest_sync(name: str, args: dict[str, Any]) -> None:
    error, _, text = call(name, args)
    assert error and "sync" in text
