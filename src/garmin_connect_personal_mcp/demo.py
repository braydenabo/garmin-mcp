"""A made-up archive for trying the server without a Garmin account, and for evals.

`demo` writes records shaped like Garmin's Personal API responses into a new
archive, plus a FIT file for each activity, so every tool works on it exactly
as on real data. Nothing in it describes a real person. The data is
deterministic for a given seed and end date, with a few patterns worth asking
about:

- weekends: more sleep, fewer steps
- a training block (weeks 5 to 8 before the end): more and longer runs,
  slightly lower HRV
- a sick spell (days 22 to 26 before the end): resting heart rate up, HRV,
  sleep score and steps down, no workouts
- a few days without the watch, which read as missing, never zero
"""

import io
import json
import random
import struct
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from garmin_connect_personal_mcp import db
from garmin_connect_personal_mcp.config import DataPaths, ensure_data_dir

DEFAULT_DAYS = 120
DEFAULT_SEED = 7
FIRST_ACTIVITY_ID = 900_000_001  # clearly synthetic
DAILY_DATASETS = ("sleep", "hrv", "daily_summary")

TRAINING_BLOCK = range(29, 57)  # days before the end
SICK_SPELL = range(22, 27)
NOT_WORN = {9, 47, 48, 83}

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=UTC)


class DemoError(Exception):
    """The demo archive can't be written here."""


@dataclass(frozen=True)
class Summary:
    days: int
    worn_days: int
    activities: int
    first_date: date
    last_date: date


def _ms(moment: datetime) -> int:
    """Garmin's '...Local' epoch milliseconds: local wall-clock time encoded as UTC."""
    return int(moment.replace(tzinfo=UTC).timestamp() * 1000)


def _day_records(day: date, back: int, rng: random.Random) -> dict[str, dict[str, Any]]:
    weekend = day.weekday() >= 5
    training = back in TRAINING_BLOCK
    sick = back in SICK_SPELL

    resting = 52 + rng.gauss(0, 1.2) + (8 if sick else 0) - (1 if training else 0)
    hrv = 64 + rng.gauss(0, 4) - (20 if sick else 0) - (4 if training else 0)
    sleep_h = (8.1 if weekend else 7.3) + rng.gauss(0, 0.35) - (0.6 if sick else 0)
    steps = int((7200 if weekend else 9800) + rng.gauss(0, 1200) - (5500 if sick else 0))
    stress = 28 + rng.gauss(0, 4) + (12 if sick else 0)

    seconds = int(sleep_h * 3600)
    deep, rem = int(seconds * 0.19), int(seconds * 0.23)
    awake = int(rng.uniform(10, 35) * 60)
    bedtime = datetime.combine(day - timedelta(days=1), time(22, 50)) + timedelta(
        minutes=rng.gauss(0, 20) + (40 if weekend else 0)
    )
    score = max(
        30, min(98, round(55 + 6 * (sleep_h - 6.5) + (hrv - 55) * 0.4 - (12 if sick else 0)))
    )

    sleep = {
        "calendarDate": day.isoformat(),
        "sleepTimeSeconds": seconds,
        "deepSleepSeconds": deep,
        "remSleepSeconds": rem,
        "lightSleepSeconds": seconds - deep - rem,
        "awakeSleepSeconds": awake,
        "sleepStartTimestampLocal": _ms(bedtime),
        "sleepEndTimestampLocal": _ms(bedtime + timedelta(seconds=seconds + awake)),
        "avgHeartRate": round(resting + 4 + rng.gauss(0, 1)),
        "averageRespirationValue": round(14.2 + rng.gauss(0, 0.4) + (1.5 if sick else 0), 1),
        "sleepScores": {"overall": {"value": score}},
    }
    weekly = round(hrv + rng.gauss(0, 1.5))
    hrv_record = {
        "calendarDate": day.isoformat(),
        "lastNightAvg": round(hrv),
        "weeklyAvg": weekly,
        "baseline": {"balancedLow": 55, "balancedUpper": 72},
        "status": "BALANCED" if 55 <= weekly <= 72 else ("LOW" if weekly < 55 else "UNBALANCED"),
    }
    summary = {
        "calendarDate": day.isoformat(),
        "heartRate": {
            "restingValue": round(resting),
            "minValue": round(resting - 4),
            "maxValue": round(rng.uniform(118, 135) if sick else rng.uniform(150, 182)),
        },
        "movement": {
            "steps": {
                "value": max(steps, 900),
                "goal": 10000,
                "distanceInMeters": round(max(steps, 900) * 0.78),
            },
            "floorsAscended": {"value": max(0, round(rng.gauss(9, 4)))},
        },
        "stress": {"avgLevel": round(stress), "maxLevel": round(stress + rng.uniform(40, 60))},
        "bodyBattery": {
            "maxValue": round(min(100, 85 + rng.gauss(0, 6) - (25 if sick else 0))),
            "minValue": round(max(5, 20 + rng.gauss(0, 6))),
            "chargedValue": round(60 + rng.gauss(0, 8) - (20 if sick else 0)),
            "drainedValue": round(65 + rng.gauss(0, 8)),
        },
        "calories": {
            "burnedTotal": round(2350 + steps * 0.04 + rng.gauss(0, 60)),
            "burnedActive": round(350 + steps * 0.04 + rng.gauss(0, 40)),
        },
        "intensityMinutes": {
            "moderate": max(0, round(rng.gauss(18, 8))),
            "vigorous": 0 if sick else max(0, round(rng.gauss(14, 9))),
        },
        "respiration": {"avgValue": round(14.8 + rng.gauss(0, 0.4), 1)},
    }
    return {"sleep": sleep, "hrv": hrv_record, "daily_summary": summary}


# --- activities


@dataclass(frozen=True)
class _Plan:
    kind: str
    name: str
    minutes: float
    speed_mps: float  # average
    hr_bpm: float  # starting heart rate
    drift_bpm: float  # extra heart rate by the end
    cadence: float  # as recorded in FIT: strides (foot) or revolutions per minute
    power_w: float | None
    climb_m: float  # height of the one hill


def _plan_for(day: date, back: int, rng: random.Random) -> _Plan | None:
    if back in SICK_SPELL or back in NOT_WORN:
        return None
    weekday = day.weekday()
    training = back in TRAINING_BLOCK
    if weekday in (1, 3):  # Tuesday, Thursday: runs
        return _Plan(
            "running",
            "Easy run",
            rng.uniform(32, 42) * (1.15 if training else 1),
            rng.uniform(2.75, 2.95),
            138,
            9,
            84,
            245,
            25,
        )
    if weekday == 5:  # Saturday: long run, longer during the training block
        return _Plan(
            "running",
            "Long run",
            rng.uniform(70, 80) * (1.35 if training else 1),
            rng.uniform(2.65, 2.8),
            140,
            16,
            83,
            238,
            60,
        )
    if weekday == 6:  # Sunday: ride
        return _Plan(
            "cycling",
            "Sunday ride",
            rng.uniform(80, 110),
            rng.uniform(7.2, 8.0),
            128,
            10,
            86,
            175,
            120,
        )
    if weekday == 0:  # Monday: strength
        return _Plan("strength_training", "Strength", rng.uniform(35, 45), 0, 105, 12, 0, None, 0)
    if weekday == 2 and training:  # Wednesday intervals during the block
        return _Plan("running", "Intervals", rng.uniform(45, 50), 3.1, 145, 20, 88, 290, 10)
    return None


def _samples(plan: _Plan, rng: random.Random) -> list[dict[str, float | None]]:
    """One sample per second: heart rate drifts up, and the hill sits in the middle."""
    total = int(plan.minutes * 60)
    out: list[dict[str, float | None]] = []
    distance = 0.0
    for t in range(total):
        progress = t / max(total - 1, 1)
        hill = max(0.0, 1 - abs(progress - 0.5) * 6)  # a bump in the middle third
        surge = 1.0
        if plan.name == "Intervals" and 0.2 < progress < 0.8:
            surge = 1.25 if int(progress * 20) % 2 == 0 else 0.8  # alternating reps
        speed = plan.speed_mps * surge * (1 - 0.12 * hill) + rng.gauss(0, 0.05)
        speed = max(speed, 0.0) if plan.speed_mps else 0.0
        distance += speed
        out.append(
            {
                "hr": plan.hr_bpm
                + plan.drift_bpm * progress
                + 12 * hill
                + 8 * (surge - 1) * 4
                + rng.gauss(0, 1.5),
                "speed": speed if plan.speed_mps else None,
                "distance": distance if plan.speed_mps else None,
                "cadence": plan.cadence + rng.gauss(0, 1) if plan.cadence else None,
                "power": plan.power_w * surge * (1 + 0.15 * hill) + rng.gauss(0, 6)
                if plan.power_w
                else None,
                "altitude": 40 + plan.climb_m * hill if plan.climb_m else None,
            }
        )
    return out


def _activity(
    activity_id: int, day: date, plan: _Plan, rng: random.Random
) -> tuple[dict[str, Any], bytes]:
    start = datetime.combine(day, time(7 if day.weekday() < 5 else 9, 10))
    samples = _samples(plan, rng)
    hrs = [s["hr"] for s in samples if s["hr"] is not None]
    seconds = len(samples)
    distance = samples[-1]["distance"] or 0.0
    raw: dict[str, Any] = {
        "activityId": activity_id,
        "activityName": plan.name,
        "activityType": {"typeKey": plan.kind},
        "startTimeLocal": start.strftime("%Y-%m-%d %H:%M:%S"),
        "startTimeGMT": start.strftime("%Y-%m-%d %H:%M:%S"),
        "duration": float(seconds),
        "movingDuration": float(seconds),
        "elapsedDuration": float(seconds),
        "averageHR": round(sum(hrs) / len(hrs), 1),
        "maxHR": round(max(hrs)),
        "calories": round(seconds / 60 * (11 if plan.kind != "strength_training" else 6)),
        "activityTrainingLoad": round(seconds / 60 * (1.6 if plan.name != "Easy run" else 1.1), 1),
        "aerobicTrainingEffect": round(2.4 + seconds / 7200 + rng.uniform(0, 0.4), 1),
    }
    if plan.speed_mps:
        raw |= {
            "distance": round(distance, 1),
            "averageSpeed": round(distance / seconds, 4),
            "maxSpeed": round(max(s["speed"] or 0 for s in samples), 3),
            "elevationGain": float(plan.climb_m),
            "elevationLoss": float(plan.climb_m),
        }
    if plan.power_w:
        raw["avgPower"] = round(sum(s["power"] or 0 for s in samples) / seconds)
    if plan.kind == "running":
        raw["averageRunningCadenceInStepsPerMinute"] = round(plan.cadence * 2, 1)
    return raw, _zip(fit_file(start, samples), f"{activity_id}_ACTIVITY.fit")


# --- a minimal FIT writer: file_id plus record messages, no GPS


_CRC_TABLE = (
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
)  # fmt: skip


def _crc(data: bytes, crc: int = 0) -> int:
    for byte in data:
        for nibble in (byte & 0xF, byte >> 4):
            tmp = _CRC_TABLE[crc & 0xF]
            crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ _CRC_TABLE[nibble]
    return crc


# record field: (number, size, base type, struct code, scale, offset)
_RECORD = {
    "time": (253, 4, 0x86, "I", 1, 0),
    "hr": (3, 1, 0x02, "B", 1, 0),
    "cadence": (4, 1, 0x02, "B", 1, 0),
    "distance": (5, 4, 0x86, "I", 100, 0),
    "power": (7, 2, 0x84, "H", 1, 0),
    "speed": (73, 4, 0x86, "I", 1000, 0),
    "altitude": (78, 4, 0x86, "I", 5, 500),
}
_INVALID = {"B": 0xFF, "H": 0xFFFF, "I": 0xFFFFFFFF}


def fit_file(start: datetime, samples: list[dict[str, float | None]]) -> bytes:
    """A small valid FIT activity file with one record per sample."""
    begin = int((start.replace(tzinfo=UTC) - FIT_EPOCH).total_seconds())
    body = bytearray()
    # file_id: type=activity(4), manufacturer=development(255), time_created
    body += (
        bytes([0x40, 0, 0]) + struct.pack("<H", 0) + bytes([3, 0, 1, 0x00, 1, 2, 0x84, 4, 4, 0x86])
    )
    body += bytes([0x00, 4]) + struct.pack("<HI", 255, begin)
    fields = list(_RECORD.values())
    body += bytes([0x41, 0, 0]) + struct.pack("<H", 20) + bytes([len(fields)])
    for number, size, base, *_ in fields:
        body += bytes([number, size, base])
    for i, sample in enumerate(samples):
        body.append(0x01)
        for name, (_, _, _, code, scale, offset) in _RECORD.items():
            value = begin + i if name == "time" else sample.get(name)
            raw = _INVALID[code] if value is None else max(0, round((value + offset) * scale))
            body += struct.pack("<" + code, min(raw, _INVALID[code] - 1))
    header = struct.pack("<BBHI4s", 14, 0x20, 2132, len(body), b".FIT")
    header += struct.pack("<H", _crc(header))
    data = header + bytes(body)
    return data + struct.pack("<H", _crc(data))


def _zip(content: bytes, name: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, content)
    return buffer.getvalue()


# --- writing the archive


def build(
    paths: DataPaths, *, days: int = DEFAULT_DAYS, end: date | None = None, seed: int = DEFAULT_SEED
) -> Summary:
    """Write a new demo archive into a new or empty folder; refuses anything else."""
    if not 14 <= days <= 3650:
        raise DemoError("days must be between 14 and 3650.")
    if paths.root.exists() and (not paths.root.is_dir() or any(paths.root.iterdir())):
        raise DemoError(
            f"{paths.root} isn't empty. The demo only writes to a new or empty folder, so it "
            "can never mix with real data."
        )
    end = end or date.today()
    rng = random.Random(seed)  # noqa: S311 - made-up data, not security
    ensure_data_dir(paths)
    conn = db.connect(paths.db)
    activity_id = FIRST_ACTIVITY_ID
    worn = activities = 0
    try:
        with conn:
            for back in range(days - 1, -1, -1):
                day = end - timedelta(days=back)
                records = _day_records(day, back, rng)  # drawn even when unused: stable output
                if back in NOT_WORN:
                    for dataset in DAILY_DATASETS:
                        db.record_sync(conn, dataset, day, "empty")
                else:
                    worn += 1
                    for dataset, raw in records.items():
                        db.upsert_daily(conn, dataset, day, raw)
                        db.record_sync(conn, dataset, day, "ok")
                db.record_sync(conn, "activities", day, "ok")
                plan = _plan_for(day, back, rng)
                if plan is None:
                    continue
                raw, fit_zip = _activity(activity_id, day, plan, rng)
                db.upsert_activity(
                    conn,
                    activity_id,
                    raw,
                    start_time_utc=raw["startTimeGMT"],
                    calendar_date=day,
                    activity_type=plan.kind,
                )
                name = f"{activity_id}.zip"
                (paths.fit_dir / name).write_bytes(fit_zip)
                db.set_fit_path(conn, activity_id, name)
                activity_id += 1
                activities += 1
    finally:
        conn.close()
    paths.demo_marker.write_text(
        json.dumps({"demo": True, "seed": seed, "days": days, "end": end.isoformat()}) + "\n",
        encoding="utf-8",
    )
    return Summary(days, worn, activities, end - timedelta(days=days - 1), end)
