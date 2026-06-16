#!/usr/bin/env python3
"""Analyze DDI relay logs by UUID.

Usage examples:
  python analyze_ddi_log.py --log ddi_relay.log.2026-06-13 --uuid D5CAAF0615E18000FC8B3004 076AAD0615E18000FC8B3004
  python analyze_ddi_log.py --ssh --host 192.84.91.113 --user pi --remote-log /var/ddirm/log/ddi_relay.log.2026-06-16 --uuid ...

The script focuses on parsing timestamps from each log line, filtering by UUID, and
producing a summary and per-message CSV outputs.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from datetime import datetime, timedelta
from typing import List, Optional, Tuple, Dict, Any


def parse_timestamp(line: str) -> datetime:
    """Try to detect and parse a timestamp from the given log line.

    Raises ValueError with a clear message if no known timestamp format is found.
    """
    # Common per-line prefix format: 2026-06-13 13:01:48,690
    m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[,\.]\d+)", line)
    if m:
        ts = m.group(1)
        for fmt in ("%Y-%m-%d %H:%M:%S,%f", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(ts, fmt)
            except Exception:
                continue

    # Try to find an ISO timestamp inside JSON-like fields, e.g. 2026-06-13T13:02:09.751718+09:00
    iso = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[\.,]\d+)?(?:[+-]\d{2}:?\d{2})?", line)
    if iso:
        s = iso.group(0)
        try:
            # Python 3.7+ supports fromisoformat for many ISO forms
            return datetime.fromisoformat(s.replace('Z', '+00:00'))
        except Exception:
            # Fallback: remove timezone and parse fractional seconds
            s2 = re.sub(r"[+-]\d{2}:?\d{2}$", "", s)
            for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
                try:
                    return datetime.strptime(s2, fmt)
                except Exception:
                    continue

    raise ValueError(f"Could not parse timestamp from line: {line.strip()[:120]}")


def load_log(path: str, ssh_opts: Optional[dict] = None) -> List[str]:
    """Load log lines from local file path or via SSH when ssh_opts provided.

    ssh_opts can be a dict with keys: host, user, remote_log, password (optional).
    """
    if ssh_opts:
        try:
            import paramiko
        except Exception:
            raise RuntimeError("paramiko is required for SSH mode. Install with: pip install paramiko")

        host = ssh_opts.get("host")
        user = ssh_opts.get("user")
        remote_log = ssh_opts.get("remote_log")
        password = ssh_opts.get("password")

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=host, username=user, password=password, timeout=10)
        sftp = client.open_sftp()
        try:
            with sftp.open(remote_log, "r") as fh:
                return fh.read().splitlines()
        finally:
            sftp.close()
            client.close()

    # Local file
    if not os.path.exists(path):
        raise FileNotFoundError(f"Log file not found: {path}")
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read().splitlines()


def parse_message_metadata(line: str) -> Tuple[Optional[str], str, Optional[float], Optional[float]]:
    """Extract SEQ, data type, voltage, and current from a log line.

    Returns (seq, data_type, voltage, current)
    """
    seq = None
    voltage = None
    current = None

    # Try MQTT JSON 'SEQ': number
    m = re.search(r"'SEQ'\s*:\s*(\d+)", line)
    if m:
        seq = m.group(1)
    else:
        # Try to parse COM:D style where a hex or decimal token follows the UUID
        m2 = re.search(r"COM:D:[^,]+,([0-9A-Fa-f]+),", line)
        if m2:
            tok = m2.group(1)
            try:
                if re.search(r"[A-Fa-f]", tok):
                    seq = str(int(tok, 16))
                else:
                    seq = str(int(tok))
            except Exception:
                seq = tok

    low = line.lower()
    dtype = "Unknown"

    # Parse JSON-like voltage/current patterns: 'Unit': 'V' and 'Value': number, or 'Voltage': number
    # Attempt to find patterns like "'Unit': 'V', 'Value': 3.10" or JSON "\"Unit\": \"V\""
    # Find all occurrences of Unit/Value pairs
    for m in re.finditer(r"('Unit'|\"Unit\")\s*[:=]\s*['\"]?([vVaA])['\"]?[,\}\s\[]+[^\n]{0,80}?('Value'|\"Value\")\s*[:=]\s*([0-9]+\.?[0-9]*)", line):
        u = m.group(2).lower()
        val = float(m.group(4))
        if u == 'v':
            voltage = val
        elif u == 'a':
            current = val

    # Find explicit voltage keywords followed by numbers, prefer patterns with 'v' or 'volt'
    if voltage is None:
        mv = re.search(r"(?:voltage|volt|v)\W{0,6}([0-9]+\.?[0-9]*)", low)
        if mv:
            # ensure not matching 'v' in hex strings; basic guard: value should be reasonable (0-1000)
            try:
                val = float(mv.group(1))
                if 0 < val < 10000:
                    voltage = val
            except Exception:
                pass

    # Find explicit current
    if current is None:
        mc = re.search(r"(?:current|amp|a)\W{0,6}([0-9]+\.?[0-9]*)", low)
        if mc:
            try:
                val = float(mc.group(1))
                if 0 <= val < 100000:
                    current = val
            except Exception:
                pass

    # Data type detection heuristics
    if voltage is not None:
        dtype = "Voltage"
    elif current is not None:
        dtype = "Current"
    elif re.search(r"voltage|volt", low):
        dtype = "Voltage"
    elif re.search(r"current|amp", low):
        dtype = "Current"

    return seq, dtype, voltage, current


def filter_by_uuid(lines_with_source: List[Tuple[str, str]], uuid: str) -> List[Dict[str, Any]]:
    """Return list of dicts for lines containing uuid (case-insensitive).

    lines_with_source is list of (line, source_file)
    Each dict: {line, ts, seq, data_type, voltage, current, source_file}
    """
    results: List[Dict[str, Any]] = []
    u = uuid.lower()
    for ln, src in lines_with_source:
        if u in ln.lower():
            try:
                ts = parse_timestamp(ln)
            except ValueError as exc:
                raise ValueError(f"While filtering for UUID {uuid}: {exc}")
            seq, dtype, voltage, current = parse_message_metadata(ln)
            results.append({"line": ln, "ts": ts, "seq": seq, "data_type": dtype, "voltage": voltage, "current": current, "source_file": src})
    results.sort(key=lambda x: x["ts"])
    return results


def group_report_events(messages: List[Dict[str, Any]], max_interval_seconds: int = 2) -> List[Dict[str, Any]]:
    """Group nearby messages (<= max_interval_seconds) into report events.

    Each event contains: start_time, end_time, duration_ms, raw_count, seqs(list), data_types(list), messages(list)
    """
    events: List[Dict[str, Any]] = []
    if not messages:
        return events

    cur_event = {"start": messages[0]["ts"], "end": messages[0]["ts"], "messages": [messages[0]]}
    for m in messages[1:]:
        delta = (m["ts"] - cur_event["end"]).total_seconds()
        if delta <= max_interval_seconds:
            cur_event["messages"].append(m)
            cur_event["end"] = m["ts"]
        else:
            events.append(cur_event)
            cur_event = {"start": m["ts"], "end": m["ts"], "messages": [m]}
    events.append(cur_event)

    # enrich events
    enriched: List[Dict[str, Any]] = []
    for e in events:
        msgs = e["messages"]
        seqs = []
        types = set()
        voltages = []
        currents = []
        sources = set()
        for mm in msgs:
            if mm.get("seq") is not None:
                seqs.append(mm.get("seq"))
            types.add(mm.get("data_type", "Unknown"))
            if mm.get("voltage") is not None:
                voltages.append(mm.get("voltage"))
            if mm.get("current") is not None:
                currents.append(mm.get("current"))
            if mm.get("source_file"):
                sources.add(mm.get("source_file"))
        enriched.append({
            "start_time": e["start"],
            "end_time": e["end"],
            "duration_ms": int((e["end"] - e["start"]).total_seconds() * 1000),
            "raw_message_count": len(msgs),
            "seqs": sorted(set(seqs), key=lambda x: int(x) if str(x).isdigit() else x),
            "data_types": sorted(types),
            "voltage_values": voltages,
            "current_values": currents,
            "source_files": sorted(sources),
            "messages": msgs,
        })

    return enriched


def analyze_uuid(messages: List[Dict[str, Any]], expected_interval_minutes: int = 30, alert_threshold_minutes: int = 45) -> dict:
    """Analyze parsed messages for a UUID and return summary including events.

    messages: list of dicts with keys line, ts, seq, data_type
    """
    raw_count = len(messages)
    seqs = sorted({m.get("seq") for m in messages if m.get("seq") is not None}, key=lambda x: int(x) if str(x).isdigit() else x)
    unique_seq_count = len(seqs)

    # events will be grouped later with provided merge window by caller; default to 2s here
    events = group_report_events(messages, max_interval_seconds=2)
    report_event_count = len(events)

    if report_event_count == 0:
        # still compute voltage empty stats
        return {"count": 0, "raw_message_count": raw_count, "unique_seq_count": unique_seq_count, "report_event_count": 0,
            "first": None, "last": None, "total_observed": timedelta(0), "events": [],
            "daily_voltage": {}, "first_voltage": None, "last_voltage": None, "min_voltage": None, "max_voltage": None, "avg_daily_voltage_drop": None}

    first_event = events[0]["start_time"]
    last_event = events[-1]["end_time"]
    total_observed = last_event - first_event
    expected_interval = timedelta(minutes=expected_interval_minutes)
    expected_n = int(total_observed.total_seconds() // expected_interval.total_seconds()) + 1

    delivery_rate = (report_event_count / expected_n * 100) if expected_n > 0 else 0.0

    # gaps between consecutive event starts
    starts = [e["start_time"] for e in events]
    gaps = [(b - a).total_seconds() for a, b in zip(starts, starts[1:])] if len(starts) > 1 else []
    avg_gap = (sum(gaps) / len(gaps)) if gaps else 0.0
    max_gap = max(gaps) if gaps else 0.0

    # suspicious gaps > alert threshold
    threshold = alert_threshold_minutes * 60
    suspicious = []
    for i, s in enumerate(gaps):
        if s > threshold:
            suspicious.append({"index": i, "gap_seconds": s, "from": starts[i], "to": starts[i + 1]})
    # Voltage per-message aggregation
    voltage_samples = [(m["ts"].date(), m["ts"], m.get("voltage")) for m in messages if m.get("voltage") is not None]
    daily_voltage: Dict[str, Dict[str, Any]] = {}
    for d, ts, v in voltage_samples:
        key = d.isoformat()
        if key not in daily_voltage:
            daily_voltage[key] = {"samples": []}
        daily_voltage[key]["samples"].append((ts, v))

    # compute per-day stats. The daily drop is normalized to a 24-hour rate
    # based on the actual observed span within each calendar day.
    for key, info in daily_voltage.items():
        samples = sorted(info["samples"], key=lambda x: x[0])
        vals = [v for (_, v) in samples]
        if len(vals) >= 1:
            first_ts = samples[0][0]
            last_ts = samples[-1][0]
            first_v = vals[0]
            last_v = vals[-1]
            min_v = min(vals)
            max_v = max(vals)
            avg_v = sum(vals) / len(vals)
            voltage_drop = first_v - last_v
            observed_hours = (last_ts - first_ts).total_seconds() / 3600
            voltage_drop_per_24h = (voltage_drop / observed_hours * 24) if observed_hours > 0 else None
            info.update({
                "date": key,
                "first_voltage": first_v,
                "last_voltage": last_v,
                "voltage_drop": voltage_drop,
                "observed_hours": observed_hours,
                "voltage_drop_per_24h": voltage_drop_per_24h,
                "min_voltage": min_v,
                "max_voltage": max_v,
                "avg_voltage": avg_v,
                "voltage_sample_count": len(vals),
            })
        else:
            info.update({
                "date": key,
                "first_voltage": None,
                "last_voltage": None,
                "voltage_drop": None,
                "observed_hours": None,
                "voltage_drop_per_24h": None,
                "min_voltage": None,
                "max_voltage": None,
                "avg_voltage": None,
                "voltage_sample_count": 0,
            })

    # compute average daily voltage drop as a normalized 24-hour rate
    # (only days with at least 2 samples and non-zero observed time)
    drops = [
        info["voltage_drop_per_24h"]
        for info in daily_voltage.values()
        if info.get("voltage_sample_count", 0) >= 2 and info.get("voltage_drop_per_24h") is not None
    ]
    avg_daily_voltage_drop = (sum(drops) / len(drops)) if drops else None

    # overall first/last/min/max voltage
    all_voltage_values = sorted([(m["ts"], m.get("voltage")) for m in messages if m.get("voltage") is not None], key=lambda x: x[0])
    first_voltage = all_voltage_values[0][1] if all_voltage_values else None
    last_voltage = all_voltage_values[-1][1] if all_voltage_values else None
    min_voltage = min([v for (_, v) in all_voltage_values]) if all_voltage_values else None
    max_voltage = max([v for (_, v) in all_voltage_values]) if all_voltage_values else None

    return {
        "first": first_event,
        "last": last_event,
        "total_observed": total_observed,
        "expected_interval_minutes": expected_interval_minutes,
        "raw_message_count": raw_count,
        "unique_seq_count": unique_seq_count,
        "report_event_count": report_event_count,
        "expected_event_count": expected_n,
        "delivery_rate_percent": delivery_rate,
        "missing_event_count": max(0, expected_n - report_event_count),
        "avg_gap_seconds": avg_gap,
        "max_gap_seconds": max_gap,
        "suspicious_gaps": suspicious,
        "events": events,
        "daily_voltage": daily_voltage,
        "avg_daily_voltage_drop": avg_daily_voltage_drop,
        "first_voltage": first_voltage,
        "last_voltage": last_voltage,
        "min_voltage": min_voltage,
        "max_voltage": max_voltage,
    }



def write_summary_csv(path: str, summaries: List[dict]):
    # New summary columns per user request
    fieldnames = [
        "uuid",
        "expected_event_count",
        "report_event_count",
        "delivery_rate_percent",
        "missing_event_count",
        "avg_daily_voltage_drop",
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for s in summaries:
            avg_drop = s.get("avg_daily_voltage_drop")
            # represent None as NA
            avg_drop_val = (round(avg_drop, 6) if isinstance(avg_drop, float) else "NA")
            w.writerow({
                "uuid": s["uuid"],
                "expected_event_count": s.get("expected_event_count", 0),
                "report_event_count": s.get("report_event_count", 0),
                "delivery_rate_percent": round(s.get("delivery_rate_percent", 0.0), 2),
                "missing_event_count": s.get("missing_event_count", 0),
                "avg_daily_voltage_drop": avg_drop_val,
            })


def write_messages_csv(path: str, details: List[Tuple[str, List[Tuple[str, datetime]]]]):
    # details: list of (uuid, messages) where messages are list of dicts
    fieldnames = ["uuid", "timestamp", "seq", "data_type", "line"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for uuid, msgs in details:
            for m in msgs:
                w.writerow({"uuid": uuid, "timestamp": m.get("ts").isoformat(), "seq": m.get("seq") or "", "data_type": m.get("data_type"), "line": m.get("line")})


def write_events_csv(path: str, details: List[Tuple[str, List[Dict[str, Any]]]]):
    fieldnames = [
        "uuid",
        "event_index",
        "event_start_time",
        "event_end_time",
        "raw_message_count_in_event",
        "seq_list",
        "voltage_values",
        "current_values",
        "source_files",
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for uuid, events in details:
            for idx, e in enumerate(events):
                w.writerow({
                    "uuid": uuid,
                    "event_index": idx,
                    "event_start_time": e.get("start_time").isoformat(),
                    "event_end_time": e.get("end_time").isoformat(),
                    "raw_message_count_in_event": e.get("raw_message_count"),
                    "seq_list": ";".join(map(str, e.get("seqs", []))),
                    "voltage_values": ";".join(map(str, e.get("voltage_values", []))),
                    "current_values": ";".join(map(str, e.get("current_values", []))),
                    "source_files": ";".join(e.get("source_files", [])),
                })


def print_summary_console(summaries: List[dict]):
    # Try to use pandas for nice output if available
    try:
        import pandas as pd

        # Compact console: UUID | Expected | Actual | Rate% | Miss | Avg Voltage Drop/Day
        rows = []
        for s in summaries:
            rows.append({
                "UUID": s["uuid"],
                "Expected": s.get("expected_event_count", 0),
                "Actual": s.get("report_event_count", 0),
                "Rate%": round(s.get("delivery_rate_percent", 0.0), 2),
                "Miss": s.get("missing_event_count", 0),
                "AvgDrop/24h": (round(s.get("avg_daily_voltage_drop"), 6) if isinstance(s.get("avg_daily_voltage_drop"), float) else "NA"),
            })
        df = pd.DataFrame(rows)
        print(df.to_string(index=False))
    except Exception:
        # Plain text table
        hdr = ("UUID", "Expected", "Actual", "Rate%", "Miss", "Avg Voltage Drop/24h")
        print(" | ".join(hdr))
        print("-" * 80)
        for s in summaries:
            avgdrop = (round(s.get("avg_daily_voltage_drop"), 6) if isinstance(s.get("avg_daily_voltage_drop"), float) else "NA")
            print(" | ".join([
                s["uuid"],
                str(s.get("expected_event_count", 0)),
                str(s.get("report_event_count", 0)),
                f"{round(s.get('delivery_rate_percent',0.0),2)}",
                str(s.get("missing_event_count", 0)),
                str(avgdrop),
            ]))


def main(argv=None):
    p = argparse.ArgumentParser(description="Analyze DDI relay log by UUID")
    p.add_argument("--log", nargs="*", help="Local log file paths (one or more)", required=False)
    p.add_argument("--log-dir", help="Directory to search for logs (use with --pattern)")
    p.add_argument("--pattern", help="Glob pattern to match logs in --log-dir, e.g. 'ddi_relay.log.2026-06-*'")
    p.add_argument("--uuid", nargs="+", required=True, help="One or more UUIDs to analyze")
    p.add_argument("--ssh", action="store_true", help="Read log via SSH instead of local file")
    p.add_argument("--host", help="SSH host")
    p.add_argument("--user", help="SSH user")
    p.add_argument("--remote-log", help="Remote log path when using SSH")
    p.add_argument("--out-dir", default=".", help="Output directory")
    p.add_argument("--out-summary", default="ddi_uuid_summary.csv", help="Summary CSV output filename")
    p.add_argument("--out-messages", default="ddi_uuid_messages.csv", help="Per-message CSV output filename")
    p.add_argument("--export-summary-only", action="store_true", help="Only export summary CSV and skip detailed files")
    p.add_argument("--merge-window-seconds", type=int, default=2, help="Merge window in seconds for grouping messages into events")
    p.add_argument("--expected-interval-minutes", type=int, default=30)
    p.add_argument("--alert-threshold-minutes", type=int, default=45)
    args = p.parse_args(argv)

    ssh_opts = None
    if args.ssh:
        if not (args.host and args.user and args.remote_log):
            p.error("--ssh requires --host, --user and --remote-log")
        # Prompt for password if needed (do not hard-code)
        from getpass import getpass

        pw = None
        try:
            pw = os.environ.get("DDI_SSH_PW")
        except Exception:
            pw = None
        if not pw:
            pw = getpass(f"SSH password for {args.user}@{args.host} (empty to attempt key auth): ")
            if pw == "":
                pw = None
        ssh_opts = {"host": args.host, "user": args.user, "remote_log": args.remote_log, "password": pw}

    # Collect lines from multiple files or SSH
    lines_with_source: List[Tuple[str, str]] = []
    try:
        if ssh_opts:
            raw = load_log("", ssh_opts=ssh_opts)
            lines_with_source = [(ln, args.remote_log or "remote") for ln in raw]
        else:
            paths: List[str] = []
            if args.log:
                paths.extend(args.log)
            if args.log_dir and args.pattern:
                import glob
                patt = os.path.join(args.log_dir, args.pattern)
                paths.extend(sorted(glob.glob(patt)))
            if not paths:
                p.error("Provide --log (one or more files) or --log-dir with --pattern, or use --ssh")
            # read each file
            for pth in paths:
                if not os.path.exists(pth):
                    print(f"Warning: log file not found: {pth}")
                    continue
                raw = load_log(pth)
                src = os.path.basename(pth)
                for ln in raw:
                    lines_with_source.append((ln, src))
    except Exception as exc:
        print(f"Error loading log: {exc}")
        sys.exit(2)

    summaries = []
    detail_rows = []
    events_details = []
    for u in args.uuid:
        try:
            msgs = filter_by_uuid(lines_with_source, u)
        except Exception as exc:
            print(f"Error while filtering for {u}: {exc}")
            msgs = []

        # group events with requested merge window
        events = group_report_events(msgs, max_interval_seconds=args.merge_window_seconds)
        analysis = analyze_uuid(msgs, expected_interval_minutes=args.expected_interval_minutes, alert_threshold_minutes=args.alert_threshold_minutes)
        # override events with chosen merge window events
        analysis["events"] = events
        analysis["report_event_count"] = len(events)
        # recompute expected counts and delivery rate using events
        if analysis.get("first") and analysis.get("last"):
            first_event = events[0]["start_time"] if events else None
            last_event = events[-1]["end_time"] if events else None
            if first_event and last_event:
                total_observed = last_event - first_event
                expected_n = int(total_observed.total_seconds() // (args.expected_interval_minutes * 60)) + 1
            else:
                expected_n = 0
        else:
            expected_n = analysis.get("expected_event_count", 0)
        analysis["expected_event_count"] = expected_n
        analysis["delivery_rate_percent"] = (analysis.get("report_event_count", 0) / expected_n * 100) if expected_n > 0 else 0.0
        analysis["missing_event_count"] = max(0, expected_n - analysis.get("report_event_count", 0))
        analysis["uuid"] = u
        summaries.append(analysis)
        detail_rows.append((u, msgs))
        events_details.append((u, analysis.get("events", [])))

    # Sort summaries: delivery_rate_percent asc, then avg_daily_voltage_drop desc
    def _sort_key(s):
        dr = s.get("delivery_rate_percent") if s.get("delivery_rate_percent") is not None else 0.0
        avg = s.get("avg_daily_voltage_drop")
        tie = -avg if isinstance(avg, float) else float("inf")
        return (dr, tie)

    summaries.sort(key=_sort_key)

    # Print (compact)
    print_summary_console(summaries)

    # Prepare output paths
    out_dir = args.out_dir or "."
    os.makedirs(out_dir, exist_ok=True)
    summary_path = os.path.join(out_dir, args.out_summary)

    # Write CSVs
    try:
        write_summary_csv(summary_path, summaries)
        print(f"Wrote summary CSV: {summary_path}")

        if not args.export_summary_only:
            messages_path = os.path.join(out_dir, args.out_messages)
            write_messages_csv(messages_path, detail_rows)
            events_csv = os.path.splitext(messages_path)[0] + "_events.csv"
            write_events_csv(events_csv, events_details)
            print(f"Wrote messages CSV: {messages_path}")
            print(f"Wrote events CSV: {events_csv}")
    except Exception as exc:
        print(f"Error writing CSVs: {exc}")


if __name__ == "__main__":
    main()
