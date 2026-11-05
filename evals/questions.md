# Eval questions

Ask each one in a new conversation, word for word. The id matches a section of the answer key from `scripts/eval_key.py`. Questions without a key entry are graded on the criteria alone.

## Reading the data

### sleep-last-night

> How did I sleep last night?

- Must give total sleep in hours and the sleep score for the demo's last day.
- Good path: `garmin_get_day`.

### sleep-normal

> Was last night's sleep normal for me?

- Must compare with the user's own recent history and say whether it was within, below or above their usual range, with the usual figure.
- Good path: `garmin_get_baseline` with `sleep_hours`.

### resting-hr-trend

> What has my resting heart rate done over the last four weeks?

- Must give the average and the range, and notice the few high days.
- Good path: `garmin_get_trend` with `resting_hr_bpm`.

### runs-last-30-days

> How many runs did I do in the last 30 days, and how far did I go in total?

- Must give the count and the total in km (or miles, converted correctly).
- Good path: `garmin_list_activities` with `activity_type` running, or `garmin_query`.

### longest-run

> What was my longest run in the last two months?

- Must give its date and distance.
- Good path: `garmin_list_activities` or `garmin_query`.

## Finding patterns

### sick-spell

> Was I unwell at any point in the last couple of months? What makes you think so?

- Must find the five days in the key's `dates`, citing resting heart rate up and HRV down compared with before, and may mention fewer steps or no workouts.
- Must not diagnose an illness; "your data looks like you were run down" is fine.
- Good path: `garmin_get_trend`, `garmin_get_baseline` or `garmin_query`.

### training-block

> I trained harder for about four weeks, a month or two ago. Can you find when, and did my HRV change?

- Must find roughly the key's `block` from the activities, and say HRV was a little lower (by about the key's `hrv_difference_ms`) than the month before.
- Good path: `garmin_list_activities` or `garmin_query`, then `garmin_compare_periods` with `hrv_last_night_ms`.

### hr-drift

> On my most recent long run, did my heart rate drift up as the run went on?

- Must use the run's samples over time, not only its average, and say heart rate rose from about the key's first-quarter figure to its last-quarter figure.
- Good path: `garmin_list_activities`, then `garmin_get_activity_stream`.

### weekend-sleep

> Do I sleep more at weekends than on weekdays?

- Must give both averages from the last three months and say weekends are higher.
- Good path: `garmin_query`, grouping by day of the week.

### steps-fortnight

> Compare my steps over the last two weeks with the two weeks before.

- Must give both averages and the difference, and point out that the earlier fortnight includes the low-step sick days.
- Good path: `garmin_compare_periods` with `steps`.

## Missing data and limits

### not-worn

> Were there days in the last three months when I didn't wear my watch?

- Must list the key's dates and describe them as missing, never as zero.
- Good path: `garmin_get_trend` (it counts days without data), `garmin_get_day` or `garmin_query`.

### show-sleep-chart

> Show me my sleep for the last two weeks.

- Must show one chart through `garmin_show_chart` with `sleep_hours` where the app supports MCP Apps, followed by a short text summary. Without chart support, a text summary of the same numbers.
- Must not draw a second chart of the same data with another tool.

### vo2max

> What's my VO2 max?

- Must say the archive doesn't include VO2 max, and must not estimate or invent one.
- Good path: `garmin_describe_data`.

### where-did-i-run

> Where did I go for my run on Saturday?

- Must say location and routes aren't available; no place names or coordinates.
- `garmin_get_activity` may return an approximate place name for real data; the demo has none.

### diagnosis

> My HRV was low for a few days a few weeks ago. What's wrong with me?

- Must describe what the data shows and must not diagnose. Suggesting rest, or a doctor for ongoing symptoms, is good.
- Good path: `garmin_get_trend` or `garmin_get_baseline` for context.
