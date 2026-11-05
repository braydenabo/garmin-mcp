"""Comparisons against the user's own history.

A baseline is the user's usual level for one daily metric over the days just
before a date. The usual range is the mean plus or minus one standard
deviation, which holds about two days in three. These are descriptive
statistics about one person's wearable data, not clinical reference ranges.
"""

from datetime import date, timedelta
from statistics import fmean, median, stdev
from typing import Literal

from pydantic import BaseModel, Field

MIN_BASELINE_DAYS = 3  # fewer can't give a spread
SPARSE_BASELINE_DAYS = 7  # fewer gives a rough baseline, said so in a note
DEFAULT_BASELINE_DAYS = 30
MAX_BASELINE_DAYS = 365

Position = Literal["below", "within", "above"]


class BaselineStats(BaseModel):
    days_with_data: int
    mean: float | None
    median: float | None
    std_dev: float | None
    usual_low: float | None = Field(description="mean - 1 standard deviation")
    usual_high: float | None = Field(description="mean + 1 standard deviation")
    min: float | None
    max: float | None


class Baseline(BaseModel):
    metric: str
    unit: str
    description: str
    date: str = Field(description="The day compared with the baseline")
    value: float | None = Field(description="The metric on that day; null if not recorded")
    baseline_start: str
    baseline_end: str = Field(description="The day before date")
    baseline: BaselineStats
    difference: float | None = Field(description="value - baseline mean, in the metric's unit")
    difference_pct: float | None = Field(description="difference as a percent of the mean")
    z_score: float | None = Field(description="difference in standard deviations")
    percentile: float | None = Field(
        description="Percent of baseline days with a lower value (ties count half)"
    )
    position: Position | None = Field(description="Where value falls against the usual range")
    notes: list[str]


def _r(value: float) -> float:
    return round(value, 2)


def baseline_window(day: date, days: int) -> tuple[date, date]:
    """The `days` days before `day`, not including it."""
    return day - timedelta(days=days), day - timedelta(days=1)


def stats(values: list[float]) -> BaselineStats:
    if len(values) < MIN_BASELINE_DAYS:
        return BaselineStats(
            days_with_data=len(values),
            mean=None,
            median=None,
            std_dev=None,
            usual_low=None,
            usual_high=None,
            min=min(values) if values else None,
            max=max(values) if values else None,
        )
    mean = fmean(values)
    spread = stdev(values)
    return BaselineStats(
        days_with_data=len(values),
        mean=_r(mean),
        median=_r(median(values)),
        std_dev=_r(spread),
        usual_low=_r(mean - spread),
        usual_high=_r(mean + spread),
        min=min(values),
        max=max(values),
    )


def compare(
    metric: str,
    unit: str,
    description: str,
    day: date,
    days: int,
    value: float | None,
    history: list[float],
    *,
    today: date,
) -> Baseline:
    """How value on day compares with history (the metric on the days before it)."""
    start, end = baseline_window(day, days)
    base = stats(history)
    difference = difference_pct = z_score = percentile = None
    position: Position | None = None
    if value is not None and base.mean is not None and base.std_dev is not None:
        raw_mean = fmean(history)
        raw_sd = stdev(history)
        difference = _r(value - raw_mean)
        difference_pct = _r(100 * (value - raw_mean) / raw_mean) if raw_mean else None
        z_score = _r((value - raw_mean) / raw_sd) if raw_sd else None
        lower = sum(1 for v in history if v < value)
        ties = sum(1 for v in history if v == value)
        percentile = round(100 * (lower + ties / 2) / len(history), 1)
        if value < raw_mean - raw_sd:
            position = "below"
        elif value > raw_mean + raw_sd:
            position = "above"
        else:
            position = "within"

    notes: list[str] = []
    if len(history) < MIN_BASELINE_DAYS:
        notes.append(
            f"Only {len(history)} day(s) with data from {start} to {end}, too few for a "
            "baseline. Try more days, or garmin_sync with days_back to fetch older history."
        )
    elif len(history) < SPARSE_BASELINE_DAYS:
        notes.append(f"The baseline rests on only {len(history)} days, so treat it as rough.")
    if value is None:
        notes.append(f"No {metric} on {day}; the baseline is shown on its own.")
    if day == today:
        notes.append("Today isn't over: running totals such as steps may still go up.")
    notes.append("A personal baseline from wearable data, not a medical reference range.")

    return Baseline(
        metric=metric,
        unit=unit,
        description=description,
        date=day.isoformat(),
        value=value,
        baseline_start=start.isoformat(),
        baseline_end=end.isoformat(),
        baseline=base,
        difference=difference,
        difference_pct=difference_pct,
        z_score=z_score,
        percentile=percentile,
        position=position,
        notes=notes,
    )


MAX_PERIOD_DAYS = 731
UNEVEN_COVERAGE = 0.5  # one period's share of days with data under half the other's


class PeriodStats(BaseModel):
    start: str
    end: str
    days: int
    days_with_data: int
    mean: float | None
    median: float | None
    std_dev: float | None = Field(description="Needs at least 3 days with data")
    min: float | None
    max: float | None


class PeriodComparison(BaseModel):
    metric: str
    unit: str
    description: str
    period: PeriodStats = Field(description="The period asked about")
    compare_period: PeriodStats = Field(description="The period it is compared with")
    difference: float | None = Field(description="period mean - compare_period mean")
    difference_pct: float | None = Field(description="difference as a percent of compare_period")
    notes: list[str]


def period_stats(start: date, end: date, values: list[float]) -> PeriodStats:
    return PeriodStats(
        start=start.isoformat(),
        end=end.isoformat(),
        days=(end - start).days + 1,
        days_with_data=len(values),
        mean=_r(fmean(values)) if values else None,
        median=_r(median(values)) if values else None,
        std_dev=_r(stdev(values)) if len(values) >= MIN_BASELINE_DAYS else None,
        min=min(values) if values else None,
        max=max(values) if values else None,
    )


def compare_periods(
    metric: str,
    unit: str,
    description: str,
    period: tuple[date, date, list[float]],
    reference: tuple[date, date, list[float]],
) -> PeriodComparison:
    """Mean of one period against another. Each is (start, end, daily values with data)."""
    a = period_stats(*period)
    b = period_stats(*reference)
    difference = difference_pct = None
    if period[2] and reference[2]:
        mean_a, mean_b = fmean(period[2]), fmean(reference[2])
        difference = _r(mean_a - mean_b)
        difference_pct = _r(100 * (mean_a - mean_b) / mean_b) if mean_b else None

    notes: list[str] = []
    for label, s in (("period", a), ("compare_period", b)):
        if s.days_with_data == 0:
            notes.append(
                f"No {metric} data in {label} ({s.start} to {s.end}). garmin_describe_data "
                "shows which dates have data; garmin_sync with days_back fetches older days."
            )
        elif s.days_with_data < SPARSE_BASELINE_DAYS:
            notes.append(f"{label} has only {s.days_with_data} days with data; treat it as rough.")
    share_a = a.days_with_data / a.days
    share_b = b.days_with_data / b.days
    if (
        a.days_with_data
        and b.days_with_data
        and min(share_a, share_b) < UNEVEN_COVERAGE * max(share_a, share_b)
    ):
        notes.append(
            f"Coverage differs: data on {share_a:.0%} of days in period vs {share_b:.0%} in "
            "compare_period, so the means may not be like for like."
        )
    if period[0] <= reference[1] and reference[0] <= period[1]:
        notes.append("The two periods overlap.")
    notes.append("Means are of days with data; days without data are left out, not counted as 0.")
    return PeriodComparison(
        metric=metric,
        unit=unit,
        description=description,
        period=a,
        compare_period=b,
        difference=difference,
        difference_pct=difference_pct,
        notes=notes,
    )
