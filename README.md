# Tool Read Log Gateway Omron

Python tool for analyzing DDI gateway relay logs by UUID. It filters raw log
lines, groups nearby messages into report events, and exports summary/detail CSV
files for checking delivery rate, missing reports, temperature-based send
intervals, and voltage drop behavior.

## Files

- `analyze_ddi_log.py`: main analysis script.
- `ddi_uuid_summary.csv`: summary output per UUID.
- `ddi_uuid_messages.csv`: matched raw messages per UUID.
- `ddi_uuid_messages_events.csv`: grouped report events per UUID.
- `ddi_uuid_gaps.csv`: gap-by-gap status output using temperature rules.
- `ddi_uuid_analysis.csv`: additional analysis output kept with the project.

Raw log files like `ddi_relay.log.*` are ignored by Git because they can be
large and can be regenerated from the source device.

## Dashboard

To inspect exported results interactively, install the dashboard dependencies
and start Streamlit:

```powershell
python -m pip install streamlit pandas plotly
streamlit run tools/ddi_log_dashboard.py
```

Upload `ddi_uuid_summary.csv` and, optionally, `ddi_uuid_gaps.csv`. The
dashboard keeps the summary, charts, timeline, and device-detail views aligned
with the active type, status, and device filters. When the gap CSV includes
temperature readings, the **Temperature Correlation** tab compares
`omron_1` with `japan_sensor_1`, and `omron_2` with `japan_sensor_2`, using
nearest-in-time readings within a configurable matching window.

## Usage

Analyze one or more local log files:

```powershell
python analyze_ddi_log.py --log ddi_relay.log.2026-06-13 ddi_relay.log.2026-06-14 --uuid D5CAAF0615E18000FC8B3004 076AAD0615E18000FC8B3004
```

Analyze logs from a directory pattern:

```powershell
python analyze_ddi_log.py --log-dir . --pattern "ddi_relay.log.2026-06-*" --uuid D5CAAF0615E18000FC8B3004
```

Force an encoding while debugging a log file:

```powershell
python analyze_ddi_log.py --log ddi_relay.log.2026-06-18 --encoding cp932 --uuid D5CAAF0615E18000FC8B3004
```

Analyze UUIDs with temperature-based report intervals:

```powershell
python analyze_ddi_log.py --log-dir . --pattern "ddi_relay.log.2026-06-*" --uuid D5CAAF0615E18000FC8B3004 076AAD0615E18000FC8B3004 34394708333030314A002900 343947083330303142002E00
```

Analyze a remote log over SSH:

```powershell
python analyze_ddi_log.py --ssh --host 192.84.91.113 --user pi --remote-log /var/ddirm/log/ddi_relay.log.2026-06-16 --uuid D5CAAF0615E18000FC8B3004
```

Use `DDI_SSH_PW` if you want to provide the SSH password through an environment
variable:

```powershell
$env:DDI_SSH_PW = "your_password"
python analyze_ddi_log.py --ssh --host 192.84.91.113 --user pi --remote-log /var/ddirm/log/ddi_relay.log.2026-06-16 --uuid D5CAAF0615E18000FC8B3004
```

## Outputs

By default, the script writes:

- `ddi_uuid_summary.csv`
- `ddi_uuid_messages.csv`
- `ddi_uuid_messages_events.csv`
- `ddi_uuid_gaps.csv`

Important summary columns:

- `current_expected_interval_sec`: expected next interval based on the last
  temperature.
- `expected_event_count`: actual grouped events plus estimated missing events
  from temperature-based gaps.
- `report_event_count`: actual grouped report events found.
- `delivery_rate_percent`: `report_event_count / expected_event_count * 100`.
- `missing_event_count`: expected events minus actual events.
- `ok_count`, `warning_count`, `delay_count`, `unknown_temp_count`: gap status
  counts.
- `last_seen`, `last_temperature`: latest observed event state.
- `avg_daily_voltage_drop`: average voltage drop normalized to a 24-hour rate.

## Temperature-Based Intervals

Built-in rules are included for the four known UUIDs:

- Japan sensors `34394708333030314A002900` and
  `343947083330303142002E00`: temperature `> 120 C` expects `300s`,
  otherwise `1800s`.
- Omron sensors `076AAD0615E18000FC8B3004` and
  `D5CAAF0615E18000FC8B3004`: temperature `> 180 C` expects `300s`,
  otherwise `1800s`.

For each gap, the expected interval is decided from the previous event
temperature because that state controls the next send interval:

```text
OK: gap_sec <= expected_interval_sec * online_factor
WARNING: gap_sec <= expected_interval_sec * offline_factor
DELAY: gap_sec > expected_interval_sec * offline_factor
UNKNOWN_TEMP: previous temperature is missing
```

Defaults are `online_factor=1.5` and `offline_factor=3.0`.

## Log Encoding

Log files are read with `safe_read_text(Path)` instead of Python's default text
encoding. Auto mode tries these encodings in order:

```text
utf-8
utf-8-sig
cp932
shift_jis
latin1
```

If decoding still needs recovery, invalid characters are replaced and analysis
continues. The final console output includes:

- `encoding_used`
- `decode_errors_count`
- `mojibake_lines_count`

When auto mode detects `cp932` or `shift_jis`, the tool prints an info line such
as `[INFO] detected encoding: cp932`.

## Voltage Drop Calculation

For each calendar day, the script takes the first and last voltage samples found
for that day:

```text
voltage_drop = first_voltage - last_voltage
observed_hours = hours between first and last voltage sample
voltage_drop_per_24h = voltage_drop / observed_hours * 24
```

Then `avg_daily_voltage_drop` is the average of `voltage_drop_per_24h` across
valid days. This means partial days are normalized to a 24-hour rate instead of
being treated as full 24-hour periods.

## Useful Options

- `--merge-window-seconds`: seconds used to group nearby raw messages into one
  report event. Default: `2`.
- `--expected-interval-minutes`: expected report interval. Default: `30`.
- `--expected-interval-by-uuid`: override interval for specific UUIDs using
  `UUID=minutes` for UUIDs without temperature rules.
- `--config`: optional JSON config path for future device rules.
- `--encoding`: log encoding. Use `auto`, `utf-8`, `cp932`, or `shift_jis`.
  Default: `auto`.
- `--temperature-debug`: print matched lines where temperature could not be
  parsed.
- `--online-factor`: OK multiplier. Default: `1.5`.
- `--offline-factor`: DELAY multiplier. Default: `3.0`.
- `--alert-threshold-minutes`: gap threshold used for suspicious gap detection.
  Default: `45`.
- `--export-summary-only`: write only the summary CSV.
- `--out-dir`: output directory. Default: current directory.
