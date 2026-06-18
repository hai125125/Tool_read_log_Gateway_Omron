# Tool Read Log Gateway Omron

Python tool for analyzing DDI gateway relay logs by UUID. It filters raw log
lines, groups nearby messages into report events, and exports summary/detail CSV
files for checking delivery rate, missing reports, and voltage drop behavior.

## Files

- `analyze_ddi_log.py`: main analysis script.
- `ddi_uuid_summary.csv`: summary output per UUID.
- `ddi_uuid_messages.csv`: matched raw messages per UUID.
- `ddi_uuid_messages_events.csv`: grouped report events per UUID.
- `ddi_uuid_analysis.csv`: additional analysis output kept with the project.

Raw log files like `ddi_relay.log.*` are ignored by Git because they can be
large and can be regenerated from the source device.

## Usage

Analyze one or more local log files:

```powershell
python analyze_ddi_log.py --log ddi_relay.log.2026-06-13 ddi_relay.log.2026-06-14 --uuid D5CAAF0615E18000FC8B3004 076AAD0615E18000FC8B3004
```

Analyze logs from a directory pattern:

```powershell
python analyze_ddi_log.py --log-dir . --pattern "ddi_relay.log.2026-06-*" --uuid D5CAAF0615E18000FC8B3004
```

Analyze UUIDs with different report intervals:

```powershell
python analyze_ddi_log.py --log-dir . --pattern "ddi_relay.log.2026-06-*" --uuid D5CAAF0615E18000FC8B3004 076AAD0615E18000FC8B3004 34394708333030314A002900 343947083330303142002E00 --expected-interval-by-uuid 34394708333030314A002900=5 343947083330303142002E00=5
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

Important summary columns:

- `expected_interval_minutes`: expected report interval used for that UUID.
- `expected_event_count`: expected number of report events from the observation
  window and expected interval.
- `report_event_count`: actual grouped report events found.
- `delivery_rate_percent`: `report_event_count / expected_event_count * 100`.
- `missing_event_count`: expected events minus actual events.
- `avg_daily_voltage_drop`: average voltage drop normalized to a 24-hour rate.

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
  `UUID=minutes`, for example `34394708333030314A002900=5`.
- `--alert-threshold-minutes`: gap threshold used for suspicious gap detection.
  Default: `45`.
- `--export-summary-only`: write only the summary CSV.
- `--out-dir`: output directory. Default: current directory.
