#!/usr/bin/env python3
"""Analyze DDI relay logs by UUID.

Usage examples:
  python analyze_ddi_log.py --log ddi_relay.log.2026-06-13 --uuid D5CAAF0615E18000FC8B3004 076AAD0615E18000FC8B3004

The script focuses on parsing timestamps from each log line, filtering by UUID, and
producing a summary and per-message CSV outputs.
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any


DEVICE_RULES = {
    "34394708333030314A002900": {
        "name": "japan_sensor_1",
        "type": "japan_sensor",
        "threshold_temp": 120.0,
        "high_interval_sec": 300,
        "low_interval_sec": 1800,
    },
    "343947083330303142002E00": {
        "name": "japan_sensor_2",
        "type": "japan_sensor",
        "threshold_temp": 120.0,
        "high_interval_sec": 300,
        "low_interval_sec": 1800,
    },
    "D5CAAF0615E18000FC8B3004": {
        "name": "omron_1",
        "type": "omron",
        "threshold_temp": 180.0,
        "high_interval_sec": 300,
        "low_interval_sec": 1800,
    },
    "076AAD0615E18000FC8B3004": {
        "name": "omron_2",
        "type": "omron",
        "threshold_temp": 180.0,
        "high_interval_sec": 300,
        "low_interval_sec": 1800,
    },
}

TEMP_KEYS = {
    "temperature",
    "temp",
    "measured_value",
    "measuredvalue",
    "current_temp",
    "currenttemp",
}

AUTO_ENCODINGS = ("utf-8", "utf-8-sig", "cp932", "shift_jis", "latin1")
MOJIBAKE_PATTERNS = (
    "\ufffd",
    "Ã",
    "Â",
    "â€",
    "â„",
    "áº",
    "á»",
    "Ä",
    "Æ",
)


def parse_uuid_interval_overrides(values: Optional[List[str]]) -> Dict[str, int]:
    """Parse UUID=minutes values from CLI arguments."""
    overrides: Dict[str, int] = {}
    if not values:
        return overrides

    for value in values:
        if "=" not in value:
            raise ValueError(f"Expected UUID=minutes, got: {value}")
        uuid, minutes_text = value.split("=", 1)
        uuid = uuid.strip()
        try:
            minutes = int(minutes_text)
        except ValueError as exc:
            raise ValueError(f"Invalid minutes for {uuid}: {minutes_text}") from exc
        if not uuid:
            raise ValueError(f"Missing UUID in interval override: {value}")
        if minutes <= 0:
            raise ValueError(f"Interval minutes must be positive for {uuid}: {minutes}")
        overrides[uuid.upper()] = minutes

    return overrides


def count_mojibake_lines(text: str) -> int:
    return sum(
        1
        for line in text.splitlines()
        if any(pattern in line for pattern in MOJIBAKE_PATTERNS)
    )


def strip_leading_bom(text: str) -> str:
    if text.startswith("\ufeff"):
        return text[1:]
    return text


def decode_text_bytes(data: bytes, encoding: str = "auto") -> Tuple[str, Dict[str, Any]]:
    if encoding == "auto":
        for candidate in AUTO_ENCODINGS:
            try:
                text = strip_leading_bom(data.decode(candidate))
                return text, {
                    "encoding_used": candidate,
                    "decode_errors_count": 0,
                    "mojibake_lines_count": count_mojibake_lines(text),
                }
            except UnicodeDecodeError:
                continue
        text = strip_leading_bom(data.decode("utf-8", errors="replace"))
        return text, {
            "encoding_used": "utf-8-replace",
            "decode_errors_count": text.count("\ufffd"),
            "mojibake_lines_count": count_mojibake_lines(text),
        }

    text = strip_leading_bom(data.decode(encoding, errors="replace"))
    return text, {
        "encoding_used": encoding,
        "decode_errors_count": text.count("\ufffd"),
        "mojibake_lines_count": count_mojibake_lines(text),
    }


def safe_read_text_with_stats(path: Path, encoding: str = "auto") -> Tuple[str, Dict[str, Any]]:
    with path.open("rb") as fh:
        data = fh.read()
    return decode_text_bytes(data, encoding=encoding)


def safe_read_text(path: Path) -> str:
    text, _ = safe_read_text_with_stats(path, encoding="auto")
    return text


def normalize_rules(raw_rules: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    rules: Dict[str, Dict[str, Any]] = {}
    for uuid, rule in raw_rules.items():
        normalized_uuid = str(uuid).strip().upper()
        if not normalized_uuid:
            continue
        rules[normalized_uuid] = {
            "name": rule.get("name", normalized_uuid),
            "type": rule.get("type", rule.get("device_type", "unknown")),
            "threshold_temp": float(rule["threshold_temp"]),
            "high_interval_sec": int(rule["high_interval_sec"]),
            "low_interval_sec": int(rule["low_interval_sec"]),
        }
    return rules


def load_device_rules(config_path: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    """Load temperature rules from JSON, falling back to DEVICE_RULES."""
    if config_path is None:
        return normalize_rules(DEVICE_RULES)

    with config_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    if isinstance(data, dict) and "device_rules" in data:
        raw_rules = data["device_rules"]
        if not isinstance(raw_rules, dict):
            raise ValueError("config.device_rules must be an object")
        return normalize_rules(raw_rules)

    if isinstance(data, dict) and "devices" in data:
        raw_rules: Dict[str, Dict[str, Any]] = {}
        for item in data["devices"]:
            if not isinstance(item, dict) or not item.get("uuid"):
                continue
            if not {"threshold_temp", "high_interval_sec", "low_interval_sec"}.issubset(item):
                continue
            raw_rules[item["uuid"]] = item
        if raw_rules:
            return normalize_rules(raw_rules)
        raise ValueError(
            "config.devices does not contain temperature rules; "
            "each rule needs threshold_temp, high_interval_sec, and low_interval_sec"
        )

    if not isinstance(data, dict):
        raise ValueError("config root must be a JSON object")
    for uuid, rule in data.items():
        if not isinstance(rule, dict) or not {"threshold_temp", "high_interval_sec", "low_interval_sec"}.issubset(rule):
            raise ValueError(
                "config must be a UUID-to-rule object or contain device_rules; "
                "each rule needs threshold_temp, high_interval_sec, and low_interval_sec"
            )
    return normalize_rules(data)


def _to_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _looks_like_temperature(unit: Any = None, sensor_type: Any = None, key: str = "") -> bool:
    unit_text = str(unit or "").lower()
    type_text = str(sensor_type or "").lower()
    key_text = key.lower()
    return (
        "temp" in type_text
        or "temp" in key_text
        or "℃" in unit_text
        or "°c" in unit_text
        or unit_text in {"c", "degc", "celsius"}
    )


def _find_temperature(obj: Any, parent_key: str = "") -> Optional[float]:
    if parent_key.lower() in {"mcu", "vdd", "status"}:
        return None

    if isinstance(obj, dict):
        lower = {str(k).lower(): v for k, v in obj.items()}

        if "value" in lower and _looks_like_temperature(
            lower.get("unit"), lower.get("type"), parent_key
        ):
            value = _to_float(lower.get("value"))
            if value is not None:
                return value

        for key in TEMP_KEYS:
            if key in lower:
                direct = _to_float(lower[key])
                if direct is not None:
                    return direct
                nested = _find_temperature(lower[key], key)
                if nested is not None:
                    return nested

        for key, value in obj.items():
            nested = _find_temperature(value, str(key))
            if nested is not None:
                return nested

        if "value" in lower and (len(lower) == 1 or _looks_like_temperature(key=parent_key)):
            return _to_float(lower["value"])

    elif isinstance(obj, list):
        for item in obj:
            nested = _find_temperature(item, parent_key)
            if nested is not None:
                return nested

    return None


def _parse_payload_object(line: str) -> Optional[Any]:
    payload = line
    marker = "MQTT json Data ="
    if marker in line:
        payload = line.split(marker, 1)[1].strip()

    if not payload:
        return None

    for parser in (json.loads, ast.literal_eval):
        try:
            return parser(payload)
        except Exception:
            continue
    return None


def extract_temperature(text: str) -> Optional[float]:
    """Extract temperature from JSON-like payloads or text fallback."""
    payload_obj = _parse_payload_object(text)
    if payload_obj is not None:
        temperature = _find_temperature(payload_obj)
        if temperature is not None:
            return temperature

    m = re.search(
        r"\b(?:temp(?:erature)?|value)\s*[:=]\s*(-?\d+(?:\.\d+)?)",
        text,
        flags=re.IGNORECASE,
    )
    if m:
        return float(m.group(1))
    return None


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


def load_log(path: str, encoding: str = "auto") -> Tuple[List[str], Dict[str, Any]]:
    """Load and decode lines from a local log file."""
    log_path = Path(path)
    if not log_path.exists():
        raise FileNotFoundError(f"Log file not found: {path}")
    text, stats = safe_read_text_with_stats(log_path, encoding=encoding)
    return text.splitlines(), stats


def parse_message_metadata(line: str) -> Tuple[Optional[str], str, Optional[float], Optional[float], Optional[float]]:
    """Extract SEQ, data type, voltage, current, and temperature from a log line.

    Returns (seq, data_type, voltage, current, temperature)
    """
    seq = None
    voltage = None
    current = None
    temperature = extract_temperature(line)

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
    if temperature is not None:
        dtype = "Temperature"
    elif voltage is not None:
        dtype = "Voltage"
    elif current is not None:
        dtype = "Current"
    elif re.search(r"voltage|volt", low):
        dtype = "Voltage"
    elif re.search(r"current|amp", low):
        dtype = "Current"

    return seq, dtype, voltage, current, temperature


def filter_by_uuid(lines_with_source: List[Tuple[str, str]], uuid: str) -> List[Dict[str, Any]]:
    """Return list of dicts for lines containing uuid (case-insensitive).

    lines_with_source is list of (line, source_file)
    Each dict: {line, ts, seq, data_type, voltage, current, temperature, source_file}
    """
    results: List[Dict[str, Any]] = []
    u = uuid.lower()
    for ln, src in lines_with_source:
        if u in ln.lower():
            try:
                ts = parse_timestamp(ln)
            except ValueError as exc:
                raise ValueError(f"While filtering for UUID {uuid}: {exc}")
            seq, dtype, voltage, current, temperature = parse_message_metadata(ln)
            results.append({
                "line": ln,
                "ts": ts,
                "seq": seq,
                "data_type": dtype,
                "voltage": voltage,
                "current": current,
                "temperature": temperature,
                "source_file": src,
            })
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
        temperatures = []
        sources = set()
        for mm in msgs:
            if mm.get("seq") is not None:
                seqs.append(mm.get("seq"))
            types.add(mm.get("data_type", "Unknown"))
            if mm.get("voltage") is not None:
                voltages.append(mm.get("voltage"))
            if mm.get("current") is not None:
                currents.append(mm.get("current"))
            if mm.get("temperature") is not None:
                temperatures.append(mm.get("temperature"))
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
            "temperature_values": temperatures,
            "temperature": temperatures[0] if temperatures else None,
            "source_files": sorted(sources),
            "messages": msgs,
        })

    return enriched


def expected_interval_for_temperature(rule: Dict[str, Any], temperature: Optional[float]) -> Optional[int]:
    if temperature is None:
        return None
    if temperature > rule["threshold_temp"]:
        return int(rule["high_interval_sec"])
    return int(rule["low_interval_sec"])


def build_gap_records(
    uuid: str,
    events: List[Dict[str, Any]],
    rule: Optional[Dict[str, Any]],
    default_interval_sec: int,
    online_factor: float = 1.5,
    offline_factor: float = 3.0,
) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    if len(events) < 2:
        return records

    for prev_event, event in zip(events, events[1:]):
        prev_temp = prev_event.get("temperature")
        current_temp = event.get("temperature")
        gap_sec = (event["start_time"] - prev_event["start_time"]).total_seconds()

        if rule:
            expected_sec = expected_interval_for_temperature(rule, prev_temp)
            threshold_temp = rule["threshold_temp"]
            name = rule["name"]
            device_type = rule["type"]
            if expected_sec is None:
                status = "UNKNOWN_TEMP"
                reason = "previous temperature missing, cannot determine expected interval"
            else:
                comparator = ">" if prev_temp > threshold_temp else "<="
                reason = (
                    f"prev_temp {prev_temp} {comparator} threshold {threshold_temp}, "
                    f"expected {expected_sec}s"
                )
                if gap_sec <= expected_sec * online_factor:
                    status = "OK"
                elif gap_sec <= expected_sec * offline_factor:
                    status = "WARNING"
                else:
                    status = "DELAY"
        else:
            expected_sec = default_interval_sec
            threshold_temp = None
            name = uuid
            device_type = "fixed_interval"
            reason = f"no temperature rule, expected {expected_sec}s"
            if gap_sec <= expected_sec * online_factor:
                status = "OK"
            elif gap_sec <= expected_sec * offline_factor:
                status = "WARNING"
            else:
                status = "DELAY"

        records.append({
            "uuid": uuid,
            "name": name,
            "type": device_type,
            "timestamp": event["start_time"],
            "temperature": current_temp,
            "previous_timestamp": prev_event["start_time"],
            "previous_temperature": prev_temp,
            "gap_sec": gap_sec,
            "threshold_temp": threshold_temp,
            "expected_interval_sec": expected_sec,
            "status": status,
            "reason": reason,
        })

    return records


def summarize_gap_records(
    uuid: str,
    events: List[Dict[str, Any]],
    gap_records: List[Dict[str, Any]],
    rule: Optional[Dict[str, Any]],
    default_interval_sec: int,
) -> Dict[str, Any]:
    counts = {status: 0 for status in ("OK", "WARNING", "DELAY", "UNKNOWN_TEMP")}
    for record in gap_records:
        counts[record["status"]] = counts.get(record["status"], 0) + 1

    gaps = [record["gap_sec"] for record in gap_records]
    last_event = events[-1] if events else None
    last_temperature = last_event.get("temperature") if last_event else None
    if rule:
        current_expected = expected_interval_for_temperature(rule, last_temperature)
        name = rule["name"]
        device_type = rule["type"]
        threshold_temp = rule["threshold_temp"]
    else:
        current_expected = default_interval_sec
        name = uuid
        device_type = "fixed_interval"
        threshold_temp = None

    missing_from_gaps = 0
    for record in gap_records:
        expected_sec = record.get("expected_interval_sec")
        if expected_sec:
            missing_from_gaps += max(0, int(math.floor(record["gap_sec"] / expected_sec)) - 1)

    report_event_count = len(events)
    expected_event_count = report_event_count + missing_from_gaps
    delivery_rate = (
        report_event_count / expected_event_count * 100
        if expected_event_count > 0
        else 0.0
    )

    return {
        "uuid": uuid,
        "name": name,
        "type": device_type,
        "threshold_temp": threshold_temp,
        "total_messages": report_event_count,
        "ok_count": counts.get("OK", 0),
        "warning_count": counts.get("WARNING", 0),
        "delay_count": counts.get("DELAY", 0),
        "unknown_temp_count": counts.get("UNKNOWN_TEMP", 0),
        "max_gap_sec": max(gaps) if gaps else 0.0,
        "avg_gap_sec": (sum(gaps) / len(gaps)) if gaps else 0.0,
        "last_seen": last_event["start_time"] if last_event else None,
        "last_temperature": last_temperature,
        "current_expected_interval_sec": current_expected,
        "expected_event_count": expected_event_count,
        "delivery_rate_percent": delivery_rate,
        "missing_event_count": max(0, expected_event_count - report_event_count),
    }


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
        "name",
        "type",
        "threshold_temp",
        "expected_interval_minutes",
        "current_expected_interval_sec",
        "expected_event_count",
        "report_event_count",
        "delivery_rate_percent",
        "missing_event_count",
        "total_messages",
        "ok_count",
        "warning_count",
        "delay_count",
        "unknown_temp_count",
        "max_gap_sec",
        "avg_gap_sec",
        "last_seen",
        "last_temperature",
        "avg_daily_voltage_drop",
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for s in summaries:
            avg_drop = s.get("avg_daily_voltage_drop")
            # represent None as NA
            avg_drop_val = (round(avg_drop, 6) if isinstance(avg_drop, float) else "NA")
            last_seen = s.get("last_seen")
            w.writerow({
                "uuid": s["uuid"],
                "name": s.get("name", ""),
                "type": s.get("type", ""),
                "threshold_temp": s.get("threshold_temp", ""),
                "expected_interval_minutes": s.get("expected_interval_minutes", 0),
                "current_expected_interval_sec": s.get("current_expected_interval_sec", ""),
                "expected_event_count": s.get("expected_event_count", 0),
                "report_event_count": s.get("report_event_count", 0),
                "delivery_rate_percent": round(s.get("delivery_rate_percent", 0.0), 2),
                "missing_event_count": s.get("missing_event_count", 0),
                "total_messages": s.get("total_messages", s.get("report_event_count", 0)),
                "ok_count": s.get("ok_count", 0),
                "warning_count": s.get("warning_count", 0),
                "delay_count": s.get("delay_count", 0),
                "unknown_temp_count": s.get("unknown_temp_count", 0),
                "max_gap_sec": round(s.get("max_gap_sec", 0.0), 3),
                "avg_gap_sec": round(s.get("avg_gap_sec", 0.0), 3),
                "last_seen": last_seen.isoformat() if isinstance(last_seen, datetime) else "",
                "last_temperature": s.get("last_temperature", ""),
                "avg_daily_voltage_drop": avg_drop_val,
            })


def write_messages_csv(path: str, details: List[Tuple[str, List[Tuple[str, datetime]]]]):
    # details: list of (uuid, messages) where messages are list of dicts
    fieldnames = ["uuid", "timestamp", "seq", "data_type", "temperature", "line"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for uuid, msgs in details:
            for m in msgs:
                w.writerow({
                    "uuid": uuid,
                    "timestamp": m.get("ts").isoformat(),
                    "seq": m.get("seq") or "",
                    "data_type": m.get("data_type"),
                    "temperature": m.get("temperature", ""),
                    "line": m.get("line"),
                })


def write_events_csv(path: str, details: List[Tuple[str, List[Dict[str, Any]]]]):
    fieldnames = [
        "uuid",
        "event_index",
        "event_start_time",
        "event_end_time",
        "raw_message_count_in_event",
        "seq_list",
        "temperature_values",
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
                    "temperature_values": ";".join(map(str, e.get("temperature_values", []))),
                    "voltage_values": ";".join(map(str, e.get("voltage_values", []))),
                    "current_values": ";".join(map(str, e.get("current_values", []))),
                    "source_files": ";".join(e.get("source_files", [])),
                })


def write_gaps_csv(path: str, gap_details: List[Tuple[str, List[Dict[str, Any]]]]):
    fieldnames = [
        "uuid",
        "name",
        "type",
        "timestamp",
        "temperature",
        "previous_timestamp",
        "previous_temperature",
        "gap_sec",
        "threshold_temp",
        "expected_interval_sec",
        "status",
        "reason",
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for _, records in gap_details:
            for record in records:
                row = dict(record)
                for key in ("timestamp", "previous_timestamp"):
                    value = row.get(key)
                    row[key] = value.isoformat() if isinstance(value, datetime) else ""
                row["gap_sec"] = round(row.get("gap_sec", 0.0), 3)
                w.writerow(row)


def print_summary_console(summaries: List[dict]):
    # Try to use pandas for nice output if available
    try:
        import pandas as pd

        # Compact console: UUID | Expected | Actual | Rate% | Miss | Avg Voltage Drop/Day
        rows = []
        for s in summaries:
            rows.append({
                "UUID": s["uuid"],
                "Type": s.get("type", ""),
                "CurrentExpectedSec": s.get("current_expected_interval_sec", ""),
                "Expected": s.get("expected_event_count", 0),
                "Actual": s.get("report_event_count", 0),
                "Rate%": round(s.get("delivery_rate_percent", 0.0), 2),
                "Miss": s.get("missing_event_count", 0),
                "OK": s.get("ok_count", 0),
                "Warn": s.get("warning_count", 0),
                "Delay": s.get("delay_count", 0),
                "UnknownTemp": s.get("unknown_temp_count", 0),
                "AvgDrop/24h": (round(s.get("avg_daily_voltage_drop"), 6) if isinstance(s.get("avg_daily_voltage_drop"), float) else "NA"),
            })
        df = pd.DataFrame(rows)
        print(df.to_string(index=False))
    except Exception:
        # Plain text table
        hdr = ("UUID", "Type", "CurrentExpectedSec", "Expected", "Actual", "Rate%", "Miss", "OK", "Warn", "Delay", "UnknownTemp", "Avg Voltage Drop/24h")
        print(" | ".join(hdr))
        print("-" * 80)
        for s in summaries:
            avgdrop = (round(s.get("avg_daily_voltage_drop"), 6) if isinstance(s.get("avg_daily_voltage_drop"), float) else "NA")
            print(" | ".join([
                s["uuid"],
                str(s.get("type", "")),
                str(s.get("current_expected_interval_sec", "")),
                str(s.get("expected_event_count", 0)),
                str(s.get("report_event_count", 0)),
                f"{round(s.get('delivery_rate_percent',0.0),2)}",
                str(s.get("missing_event_count", 0)),
                str(s.get("ok_count", 0)),
                str(s.get("warning_count", 0)),
                str(s.get("delay_count", 0)),
                str(s.get("unknown_temp_count", 0)),
                str(avgdrop),
            ]))


def print_decode_summary(decode_stats: List[Dict[str, Any]]):
    if not decode_stats:
        return

    encodings = sorted({str(stat.get("encoding_used", "")) for stat in decode_stats if stat.get("encoding_used")})
    total_decode_errors = sum(int(stat.get("decode_errors_count", 0)) for stat in decode_stats)
    total_mojibake_lines = sum(int(stat.get("mojibake_lines_count", 0)) for stat in decode_stats)
    print("Decode stats:")
    print(f"  encoding_used: {', '.join(encodings) if encodings else 'unknown'}")
    print(f"  decode_errors_count: {total_decode_errors}")
    print(f"  mojibake_lines_count: {total_mojibake_lines}")


def main(argv=None):
    p = argparse.ArgumentParser(description="Analyze DDI relay log by UUID")
    p.add_argument("--log", nargs="*", help="Local log file paths (one or more)", required=False)
    p.add_argument("--log-dir", help="Directory to search for logs (use with --pattern)")
    p.add_argument("--pattern", help="Glob pattern to match logs in --log-dir, e.g. 'ddi_relay.log.2026-06-*'")
    p.add_argument("--uuid", nargs="+", required=True, help="One or more UUIDs to analyze")
    p.add_argument("--out-dir", default=".", help="Output directory")
    p.add_argument("--out-summary", default="ddi_uuid_summary.csv", help="Summary CSV output filename")
    p.add_argument("--out-messages", default="ddi_uuid_messages.csv", help="Per-message CSV output filename")
    p.add_argument("--out-gaps", default="ddi_uuid_gaps.csv", help="Per-gap CSV output filename")
    p.add_argument("--export-summary-only", action="store_true", help="Only export summary CSV and skip detailed files")
    p.add_argument("--merge-window-seconds", type=int, default=2, help="Merge window in seconds for grouping messages into events")
    p.add_argument("--expected-interval-minutes", type=int, default=30)
    p.add_argument(
        "--expected-interval-by-uuid",
        nargs="*",
        default=[],
        metavar="UUID=MINUTES",
        help="Override expected interval for specific UUIDs, e.g. UUID1=5 UUID2=30",
    )
    p.add_argument("--config", type=Path, help="Optional JSON device-rule config path")
    p.add_argument(
        "--encoding",
        choices=("auto", "utf-8", "cp932", "shift_jis"),
        default="auto",
        help="Log file encoding. Default: auto",
    )
    p.add_argument("--temperature-debug", action="store_true", help="Print lines where temperature could not be parsed")
    p.add_argument("--online-factor", type=float, default=1.5, help="OK limit multiplier for expected interval")
    p.add_argument("--offline-factor", type=float, default=3.0, help="DELAY limit multiplier for expected interval")
    p.add_argument("--alert-threshold-minutes", type=int, default=45)
    args = p.parse_args(argv)

    try:
        interval_overrides = parse_uuid_interval_overrides(args.expected_interval_by_uuid)
    except ValueError as exc:
        p.error(str(exc))
    if args.expected_interval_minutes <= 0:
        p.error("--expected-interval-minutes must be > 0")
    if args.online_factor <= 0 or args.offline_factor <= 0:
        p.error("--online-factor and --offline-factor must be > 0")
    if args.online_factor > args.offline_factor:
        p.error("--online-factor must be <= --offline-factor")
    try:
        device_rules = load_device_rules(args.config)
    except Exception as exc:
        p.error(f"Could not load config: {exc}")

    # Collect lines from local log files.
    lines_with_source: List[Tuple[str, str]] = []
    decode_stats: List[Dict[str, Any]] = []
    try:
        paths: List[str] = []
        if args.log:
            paths.extend(args.log)
        if args.log_dir and args.pattern:
            import glob
            patt = os.path.join(args.log_dir, args.pattern)
            paths.extend(sorted(glob.glob(patt)))
        if not paths:
            p.error("Provide --log (one or more files) or --log-dir with --pattern")
        # Read each file.
        for pth in paths:
            path_obj = Path(pth)
            if not path_obj.exists():
                print(f"Warning: log file not found: {pth}")
                continue
            raw, stats = load_log(str(path_obj), encoding=args.encoding)
            stats["source"] = str(path_obj)
            decode_stats.append(stats)
            if args.encoding == "auto" and stats.get("encoding_used") in {"cp932", "shift_jis"}:
                print(f"[INFO] detected encoding: {stats['encoding_used']}")
            src = path_obj.name
            for ln in raw:
                lines_with_source.append((ln, src))
    except Exception as exc:
        print(f"Error loading log: {exc}")
        sys.exit(2)

    summaries = []
    detail_rows = []
    events_details = []
    gap_details = []
    for u in args.uuid:
        expected_interval_minutes = interval_overrides.get(u.upper(), args.expected_interval_minutes)
        default_interval_sec = expected_interval_minutes * 60
        rule = device_rules.get(u.upper())
        try:
            msgs = filter_by_uuid(lines_with_source, u)
        except Exception as exc:
            print(f"Error while filtering for {u}: {exc}")
            msgs = []

        if args.temperature_debug:
            for msg in msgs:
                if msg.get("temperature") is None:
                    print(f"UNKNOWN_TEMP {u} {msg.get('ts').isoformat()} {msg.get('line')[:240]}")

        # group events with requested merge window
        events = group_report_events(msgs, max_interval_seconds=args.merge_window_seconds)
        analysis = analyze_uuid(msgs, expected_interval_minutes=expected_interval_minutes, alert_threshold_minutes=args.alert_threshold_minutes)
        # override events with chosen merge window events
        analysis["events"] = events
        analysis["report_event_count"] = len(events)
        gap_records = build_gap_records(
            u,
            events,
            rule,
            default_interval_sec=default_interval_sec,
            online_factor=args.online_factor,
            offline_factor=args.offline_factor,
        )
        dynamic_summary = summarize_gap_records(
            u,
            events,
            gap_records,
            rule,
            default_interval_sec=default_interval_sec,
        )
        analysis.update(dynamic_summary)
        analysis["expected_interval_minutes"] = expected_interval_minutes
        analysis["uuid"] = u
        summaries.append(analysis)
        detail_rows.append((u, msgs))
        events_details.append((u, analysis.get("events", [])))
        gap_details.append((u, gap_records))

    # Sort summaries: delivery_rate_percent asc, then avg_daily_voltage_drop desc
    def _sort_key(s):
        dr = s.get("delivery_rate_percent") if s.get("delivery_rate_percent") is not None else 0.0
        avg = s.get("avg_daily_voltage_drop")
        tie = -avg if isinstance(avg, float) else float("inf")
        return (dr, tie)

    summaries.sort(key=_sort_key)

    # Print (compact)
    print_summary_console(summaries)
    print_decode_summary(decode_stats)

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
            gaps_path = os.path.join(out_dir, args.out_gaps)
            write_events_csv(events_csv, events_details)
            write_gaps_csv(gaps_path, gap_details)
            print(f"Wrote messages CSV: {messages_path}")
            print(f"Wrote events CSV: {events_csv}")
            print(f"Wrote gaps CSV: {gaps_path}")
    except Exception as exc:
        print(f"Error writing CSVs: {exc}")


if __name__ == "__main__":
    main()
