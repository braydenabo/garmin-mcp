"""Print the answer key for evals/questions.md, computed from a demo archive.

Usage: eval_key.py FOLDER   (a folder made by `garmin-connect-personal-mcp demo`)

The expected numbers come from the server's own tool functions, so the key
shows what a model should find if it uses the tools well. Questions are
relative ("last night", "the last 30 days") to the demo's end date, which is
today when the demo is made without --end; run the evals the same day.
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from statistics import fmean
from typing import Any

from garmin_connect_personal_mcp import demo, server
from garmin_connect_personal_mcp.config import DataPaths


def _mean(rows: list[list[Any]]) -> float:
    return round(fmean(float(r[0]) for r in rows if r[0] is not None), 1)


def answer_key(folder: Path) -> dict[str, dict[str, Any]]:
    """Expected facts per question id, for a demo archive in folder."""
    marker = json.loads((folder / "DEMO.json").read_text(encoding="utf-8"))
    if not marker.get("demo"):
        raise SystemExit(f"{folder} isn't a demo archive.")
    paths = DataPaths(folder)
    end = date.fromisoformat(marker["end"])

    def back(days: int) -> date:
        return end - timedelta(days=days)

    def query(sql: str) -> list[list[Any]]:
        return server.run_query(paths, sql).rows

    key: dict[str, dict[str, Any]] = {}

    night = server.get_day(paths, end).sleep or {}
    key["sleep-last-night"] = {
        "date": end.isoformat(),
        "sleep_hours": night.get("sleep_hours"),
        "sleep_score": night.get("sleep_score"),
    }

    sleep = server.get_baseline(paths, "sleep_hours", end, 30, end)
    key["sleep-normal"] = {
        "value": sleep.value,
        "baseline_mean": sleep.baseline.mean,
        "usual_range": [sleep.baseline.usual_low, sleep.baseline.usual_high],
        "position": sleep.position,
    }

    trend = server.get_trend(paths, "resting_hr_bpm", back(27), end, "day")
    key["resting-hr-trend"] = {
        "range": [back(27).isoformat(), end.isoformat()],
        "mean": trend.stats.mean,
        "min": trend.stats.min,
        "max": trend.stats.max,
        "days_with_data": trend.stats.days_with_data,
    }

    sick = sorted(back(d) for d in demo.SICK_SPELL)
    before = (sick[0] - timedelta(days=14), sick[0] - timedelta(days=1))
    during_sql = f"BETWEEN '{sick[0]}' AND '{sick[-1]}'"
    before_sql = f"BETWEEN '{before[0]}' AND '{before[1]}'"
    key["sick-spell"] = {
        "dates": [sick[0].isoformat(), sick[-1].isoformat()],
        "resting_hr_bpm": {
            "during": _mean(query(f"SELECT resting_hr_bpm FROM daily WHERE date {during_sql}")),
            "two_weeks_before": _mean(
                query(f"SELECT resting_hr_bpm FROM daily WHERE date {before_sql}")
            ),
        },
        "hrv_last_night_ms": {
            "during": _mean(query(f"SELECT hrv_last_night_ms FROM daily WHERE date {during_sql}")),
            "two_weeks_before": _mean(
                query(f"SELECT hrv_last_night_ms FROM daily WHERE date {before_sql}")
            ),
        },
        "workouts_during": query(f"SELECT count(*) FROM activities WHERE date {during_sql}")[0][0],
    }

    block = (back(max(demo.TRAINING_BLOCK)), back(min(demo.TRAINING_BLOCK)))
    length = (block[1] - block[0]).days + 1
    earlier = (block[0] - timedelta(days=length), block[0] - timedelta(days=1))
    hrv = server.compare_periods(paths, "hrv_last_night_ms", block, earlier)
    block_sql = f"BETWEEN '{block[0]}' AND '{block[1]}'"
    earlier_sql = f"BETWEEN '{earlier[0]}' AND '{earlier[1]}'"
    run = "activity_type = 'running'"
    key["training-block"] = {
        "block": [block[0].isoformat(), block[1].isoformat()],
        "runs": {
            "block": query(f"SELECT count(*) FROM activities WHERE {run} AND date {block_sql}")[0][
                0
            ],
            "before": query(f"SELECT count(*) FROM activities WHERE {run} AND date {earlier_sql}")[
                0
            ][0],
        },
        "hrv_mean_ms": {"block": hrv.period.mean, "before": hrv.compare_period.mean},
        "hrv_difference_ms": hrv.difference,
    }

    last30 = f"date > '{back(30)}'"
    count, total = query(
        f"SELECT count(*), round(sum(distance_km), 1) FROM activities WHERE {run} AND {last30}"
    )[0]
    key["runs-last-30-days"] = {"runs": count, "total_km": total}

    longest = query(
        f"SELECT date, distance_km FROM activities WHERE {run} AND date > '{back(60)}' "
        "ORDER BY distance_km DESC LIMIT 1"
    )[0]
    key["longest-run"] = {"date": longest[0], "distance_km": longest[1]}

    long_run = query(
        "SELECT activity_id, date FROM activities WHERE name = 'Long run' "
        "ORDER BY date DESC LIMIT 1"
    )[0]
    stream = server.get_activity_stream(paths, long_run[0], interval_seconds=60)
    hr = [v for v in stream.heart_rate_bpm or [] if v is not None]
    quarter = max(len(hr) // 4, 1)
    key["hr-drift"] = {
        "activity_date": long_run[1],
        "first_quarter_hr_bpm": round(fmean(hr[:quarter])),
        "last_quarter_hr_bpm": round(fmean(hr[-quarter:])),
    }

    window = [back(d) for d in range(89, -1, -1)]
    missing = [
        d.isoformat()
        for d in window
        if set(server.get_day(paths, d).status.values()) == {"no_data"}
    ]
    key["not-worn"] = {"range": [window[0].isoformat(), end.isoformat()], "dates": missing}

    weekly = query(
        "SELECT CASE WHEN strftime('%w', date) IN ('0', '6') THEN 'weekend' ELSE 'weekday' END, "
        f"round(avg(sleep_hours), 2) FROM daily WHERE date > '{back(90)}' GROUP BY 1 ORDER BY 1"
    )
    key["weekend-sleep"] = {row[0]: row[1] for row in weekly}

    steps = server.compare_periods(paths, "steps", (back(13), end), (back(27), back(14)))
    key["steps-fortnight"] = {
        "last_14_days_mean": steps.period.mean,
        "previous_14_days_mean": steps.compare_period.mean,
        "difference": steps.difference,
    }
    return key


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    print(json.dumps(answer_key(Path(argv[0]).expanduser()), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
