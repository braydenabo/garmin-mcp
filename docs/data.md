# Your data

What sync downloads, where it's kept, and what the tools can and can't tell an assistant. For each tool's arguments, see [tools.md](tools.md).

## Where it lives

Everything stays on your computer, in one data folder:

| OS | Default folder |
| --- | --- |
| macOS | `~/Library/Application Support/garmin-connect-personal-mcp` |
| Linux | `~/.local/share/garmin-connect-personal-mcp` |
| Windows | `%LOCALAPPDATA%\garmin-connect-personal-mcp` |

Set `GARMIN_MCP_DATA_DIR` to use another folder. `garmin-connect-personal-mcp doctor` shows which one is in use.

| File | Contents |
| --- | --- |
| `garmin.db` | The archive: a SQLite database |
| `fit/<activity id>.zip` | Each activity's original file from Garmin, a ZIP holding one FIT file |
| `logs/sync.log` | Output of scheduled syncs |

On macOS and Linux the folder is created readable only by you. Your API key is stored separately, in Garmin's own key file (`~/.config/garmin/connect-personal-api-key`) or the `GARMIN_PERSONAL_API_KEY` environment variable.

## What sync stores

Each Garmin response is stored exactly as received, one record per dataset and local calendar date, so a field the tools don't use yet never needs a re-download. Sync resumes where it left off and re-checks the last two days, because watches often upload late.

| Dataset | What it holds | Used by the tools |
| --- | --- | --- |
| `daily_summary` | Steps, heart rate, stress, Body Battery, calories, intensity minutes, floors, breathing | Yes |
| `sleep` | Sleep stages, score, timing, heart rate and breathing while asleep | Yes |
| `hrv` | Overnight HRV, weekly average, personal range, status | Yes |
| `stress`, `steps`, `body_battery` | Garmin's separate summaries of the same areas | Not yet (the tools read these values from `daily_summary`) |
| `weight` | Weigh-ins from a Garmin scale or entered in Garmin Connect | Not yet |
| `activities` | One record per workout, plus its FIT file | Yes |

**How far back:** Garmin's Personal API returns at least 90 days of history. The first sync fetches 7 days; `sync --days-back N` fetches more (up to 3650). Days Garmin no longer returns can't be filled in later, which is why a [daily sync](../README.md#quickstart) is recommended.

**Days without data:** when the watch wasn't worn, Garmin still answers, with an empty record. The tools treat those days as missing, never as zeros.

## Daily metrics

These names are what `garmin_get_trend`, `garmin_get_baseline`, `garmin_compare_periods` and `garmin_show_chart` accept, and the column names of the `daily` table in `garmin_query`. Missing values are null.

**Sleep** (`sleep`)

| Metric | Unit | Meaning |
| --- | --- | --- |
| `sleep_hours` | hours | Total sleep |
| `deep_sleep_minutes` | minutes | Deep sleep |
| `light_sleep_minutes` | minutes | Light sleep |
| `rem_sleep_minutes` | minutes | REM sleep |
| `awake_minutes` | minutes | Time awake during the sleep window |
| `sleep_score` | 0-100 | Garmin sleep score |
| `sleep_avg_hr_bpm` | bpm | Average heart rate while asleep |
| `sleep_avg_respiration_brpm` | breaths/min | Average breathing rate while asleep |
| `sleep_start_local` | local time | When sleep started |
| `sleep_end_local` | local time | When sleep ended |

**HRV** (`hrv`)

| Metric | Unit | Meaning |
| --- | --- | --- |
| `hrv_last_night_ms` | ms | Overnight average HRV |
| `hrv_weekly_avg_ms` | ms | 7-day average HRV |
| `hrv_baseline_low_ms` | ms | Lower bound of the personal balanced HRV range |
| `hrv_baseline_high_ms` | ms | Upper bound of the personal balanced HRV range |
| `hrv_status` | label | Garmin HRV status (e.g. BALANCED, UNBALANCED, LOW) |

**Daily summary** (`daily_summary`)

| Metric | Unit | Meaning |
| --- | --- | --- |
| `resting_hr_bpm` | bpm | Resting heart rate |
| `min_hr_bpm` | bpm | Lowest heart rate of the day |
| `max_hr_bpm` | bpm | Highest heart rate of the day |
| `steps` | steps | Steps |
| `step_goal` | steps | Daily step goal |
| `distance_km` | km | Distance walked or run during the day |
| `floors_climbed` | floors | Floors climbed |
| `stress_avg` | 0-100 | Average stress level |
| `stress_max` | 0-100 | Highest stress level |
| `body_battery_high` | 0-100 | Highest Body Battery |
| `body_battery_low` | 0-100 | Lowest Body Battery |
| `body_battery_charged` | points | Body Battery gained |
| `body_battery_drained` | points | Body Battery used |
| `calories_total_kcal` | kcal | Total calories burned |
| `calories_active_kcal` | kcal | Active calories burned |
| `intensity_minutes_moderate` | minutes | Moderate intensity minutes |
| `intensity_minutes_vigorous` | minutes | Vigorous intensity minutes |
| `respiration_avg_brpm` | breaths/min | Average breathing rate |

`sleep_start_local`, `sleep_end_local` and `hrv_status` are text, so they appear in `garmin_get_day` and `garmin_query` but can't be charted or averaged.

A few values Garmin sends are dropped rather than passed on: negative "not enough data" codes (stress uses -1 and -2), and resting heart rates above 120 bpm, which Garmin occasionally reports in error.

## Activities

`garmin_list_activities` and `garmin_get_activity` return converted units: minutes, km, km/h, and min/km for running, walking and hiking. `garmin_get_activity` adds heart-rate zones, power, cadence, training effect and splits.

### Over time, from the FIT file

`garmin_get_activity_stream` reads the activity's FIT file and returns, over elapsed time:

| Field | Unit | Notes |
| --- | --- | --- |
| `heart_rate_bpm` | bpm | |
| `speed_kmh` | km/h | |
| `pace_min_per_km` | min/km | On-foot sports; empty when standing still |
| `cadence_spm` | steps/min | On-foot sports |
| `cadence_rpm` | per minute | Other sports: pedal revolutions, or swim strokes |
| `power_w` | W | Needs a power meter or a watch that estimates running power |
| `altitude_m` | m | Needs a barometric altimeter or GPS |
| `distance_km` | km | Cumulative |
| `temperature_c` | °C | The watch's sensor, warmed by your wrist; not air temperature |

Points are averages over equal intervals of elapsed time, so a paused timer shows as a jump. A field your device didn't record is null for the whole activity, and the response says so.

**What the FIT files contain.** FIT files from the Personal API look like the watch's own recordings: on the maintainer's watch they also hold running dynamics (vertical oscillation, ground contact time and balance, vertical ratio, step length), performance condition, and developer fields from Connect IQ apps. The stream tool doesn't read those yet. Which fields a file has depends on the device and its sensors, not on this project.

## What the tools never return

Garmin's records include details that aren't needed to answer health questions and could identify you or where you live. The tools leave them out, even though the archive keeps Garmin's full responses:

- your name and display name, and profile photo links
- GPS coordinates: activity start and end points, and every position in FIT files (the decoder skips them)

An activity's approximate place name, when Garmin has one, is returned by `garmin_get_activity`.

`garmin_query` can't reach them either: it runs on a throwaway in-memory copy containing only the cleaned `daily` and `activities` tables. Its full schema, with units, is in the `garmin://schema` resource.

## Not medical data

These are readings from a consumer wearable. They're useful for spotting your own trends, but they aren't medical measurements, and the tools don't diagnose anything. `garmin_get_baseline`'s "usual range" is your own recent history, not a clinical reference range.
