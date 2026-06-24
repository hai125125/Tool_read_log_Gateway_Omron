#!/usr/bin/env python3
"""Realtime MQTT UUID monitor for DDI Relay data.

Install:
    pip install paho-mqtt

Examples:
    python tools/mqtt_uuid_watch.py --host <MQTT_BROKER_IP>
    python tools/mqtt_uuid_watch.py --host <MQTT_BROKER_IP> --topic node_send
    python tools/mqtt_uuid_watch.py --config config/mqtt_uuid_watch_config.json --host <MQTT_BROKER_IP>
    python tools/mqtt_uuid_watch.py --list-devices
    python tools/mqtt_uuid_watch.py --validate-config
    python tools/mqtt_uuid_watch.py --host <MQTT_BROKER_IP> --topic "#"
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


DEFAULT_CONFIG_PATH = Path("config/mqtt_uuid_watch_config.json")
CHANGE_ME_HOST = "CHANGE_ME"
RAW_TEXT_LIMIT = 1000
CLI_EXTRA_EXPECTED_INTERVAL_SEC = 300


class ConfigError(ValueError):
    """Raised when the watcher config is invalid."""


@dataclass(frozen=True)
class DeviceConfig:
    uuid: str
    name: str
    device_type: str
    expected_interval_sec: float
    enabled: bool
    note: str = ""


@dataclass
class DeviceState:
    uuid: str
    name: str
    device_type: str
    expected_interval_sec: float
    first_seen_epoch: float | None = None
    first_seen_text: str = "-"
    last_seen_epoch: float | None = None
    last_seen_text: str = "-"
    count: int = 0
    last_payload: str = ""
    current_status: str = "NEVER"


@dataclass(frozen=True)
class WatchConfig:
    host: str
    port: int
    topic: str
    keepalive: int
    status_interval_sec: float
    online_factor: float
    offline_factor: float
    devices: list[DeviceConfig]


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Subscribe to MQTT and monitor UUIDs from a JSON config."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Config JSON path. Default: {DEFAULT_CONFIG_PATH}",
    )
    parser.add_argument("--host", help="Override mqtt.host from config.")
    parser.add_argument("--port", type=int, help="Override mqtt.port from config.")
    parser.add_argument("--topic", help="Override mqtt.topic from config.")
    parser.add_argument(
        "--status-interval",
        type=float,
        help="Override status.status_interval_sec from config.",
    )
    parser.add_argument(
        "--online-factor",
        type=float,
        help="Override status.online_factor from config.",
    )
    parser.add_argument(
        "--offline-factor",
        type=float,
        help="Override status.offline_factor from config.",
    )
    parser.add_argument(
        "--uuid",
        action="append",
        dest="extra_uuids",
        help=(
            "Extra UUID to monitor outside config. Can be repeated. "
            f"Ad-hoc UUIDs use {CLI_EXTRA_EXPECTED_INTERVAL_SEC}s interval."
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Print all MQTT messages, including messages without monitored UUIDs.",
    )
    parser.add_argument(
        "--json-pretty",
        action="store_true",
        help="Pretty print JSON payloads when payload is valid JSON.",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="Print devices from config and exit.",
    )
    parser.add_argument(
        "--validate-config",
        action="store_true",
        help="Validate config and exit.",
    )
    return parser.parse_args(argv)


def local_timestamp(epoch: float | None = None) -> str:
    if epoch is None:
        epoch = time.time()
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def normalize_uuid(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: missing or invalid uuid")
    return value.strip().upper()


def require_number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: expected a number")
    return float(value)


def require_positive_number(value: Any, where: str) -> float:
    number = require_number(value, where)
    if number <= 0:
        raise ConfigError(f"{where}: must be > 0")
    return number


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError("Config root must be a JSON object")
    return data


def load_config(path: Path) -> WatchConfig:
    data = load_json(path)

    mqtt_config = data.get("mqtt", {})
    if not isinstance(mqtt_config, dict):
        raise ConfigError("mqtt must be an object")
    status_config = data.get("status", {})
    if not isinstance(status_config, dict):
        raise ConfigError("status must be an object")
    raw_devices = data.get("devices", [])
    if not isinstance(raw_devices, list):
        raise ConfigError("devices must be an array")

    host = str(mqtt_config.get("host", CHANGE_ME_HOST)).strip()
    port = int(require_positive_number(mqtt_config.get("port", 1883), "mqtt.port"))
    topic = str(mqtt_config.get("topic", "node_send")).strip()
    keepalive = int(require_positive_number(mqtt_config.get("keepalive", 60), "mqtt.keepalive"))
    status_interval_sec = require_positive_number(
        status_config.get("status_interval_sec", 30),
        "status.status_interval_sec",
    )
    online_factor = require_positive_number(status_config.get("online_factor", 1.5), "status.online_factor")
    offline_factor = require_positive_number(status_config.get("offline_factor", 3.0), "status.offline_factor")
    if online_factor > offline_factor:
        raise ConfigError("status.online_factor must be <= status.offline_factor")
    if not topic:
        raise ConfigError("mqtt.topic must not be empty")

    devices: list[DeviceConfig] = []
    seen: dict[str, str] = {}
    for index, raw_device in enumerate(raw_devices):
        where = f"devices[{index}]"
        if not isinstance(raw_device, dict):
            raise ConfigError(f"{where}: must be an object")
        uuid = normalize_uuid(raw_device.get("uuid"), where)
        if uuid in seen:
            raise ConfigError(f"{where}: duplicate uuid {uuid}; first seen at {seen[uuid]}")
        seen[uuid] = where

        if "expected_interval_sec" not in raw_device:
            raise ConfigError(f"{where}: missing expected_interval_sec")
        interval = require_positive_number(
            raw_device.get("expected_interval_sec"),
            f"{where}.expected_interval_sec",
        )
        enabled = bool(raw_device.get("enabled", True))
        devices.append(
            DeviceConfig(
                uuid=uuid,
                name=str(raw_device.get("name") or uuid),
                device_type=str(raw_device.get("type") or "unknown"),
                expected_interval_sec=interval,
                enabled=enabled,
                note=str(raw_device.get("note") or ""),
            )
        )

    return WatchConfig(
        host=host,
        port=port,
        topic=topic,
        keepalive=keepalive,
        status_interval_sec=status_interval_sec,
        online_factor=online_factor,
        offline_factor=offline_factor,
        devices=devices,
    )


def apply_overrides(config: WatchConfig, args: argparse.Namespace) -> WatchConfig:
    return WatchConfig(
        host=args.host.strip() if args.host else config.host,
        port=args.port if args.port is not None else config.port,
        topic=args.topic.strip() if args.topic else config.topic,
        keepalive=config.keepalive,
        status_interval_sec=(
            args.status_interval
            if args.status_interval is not None
            else config.status_interval_sec
        ),
        online_factor=args.online_factor if args.online_factor is not None else config.online_factor,
        offline_factor=args.offline_factor if args.offline_factor is not None else config.offline_factor,
        devices=config.devices,
    )


def validate_runtime_config(
    config: WatchConfig,
    host_overridden: bool,
    allow_placeholder_host: bool = False,
) -> None:
    if config.host == CHANGE_ME_HOST and not host_overridden and not allow_placeholder_host:
        raise ConfigError(
            "mqtt.host is CHANGE_ME. Run with --host <MQTT_BROKER_IP> "
            "or update config/mqtt_uuid_watch_config.json locally."
        )
    if config.port <= 0:
        raise ConfigError("mqtt.port must be > 0")
    if not config.topic:
        raise ConfigError("mqtt.topic must not be empty")
    if config.status_interval_sec <= 0:
        raise ConfigError("status interval must be > 0")
    if config.online_factor <= 0 or config.offline_factor <= 0:
        raise ConfigError("online/offline factors must be > 0")
    if config.online_factor > config.offline_factor:
        raise ConfigError("online factor must be <= offline factor")


def build_states(config: WatchConfig, extra_uuids: list[str] | None) -> dict[str, DeviceState]:
    states: dict[str, DeviceState] = {}
    config_uuids = {device.uuid for device in config.devices}
    for device in config.devices:
        if not device.enabled:
            continue
        states[device.uuid] = DeviceState(
            uuid=device.uuid,
            name=device.name,
            device_type=device.device_type,
            expected_interval_sec=device.expected_interval_sec,
        )

    extra_seen: set[str] = set()
    for index, raw_uuid in enumerate(extra_uuids or [], start=1):
        uuid = normalize_uuid(raw_uuid, f"--uuid #{index}")
        if uuid in config_uuids:
            raise ConfigError(f"--uuid #{index}: duplicate uuid {uuid}; already exists in config")
        if uuid in extra_seen:
            raise ConfigError(f"--uuid #{index}: duplicate uuid {uuid}; already passed via --uuid")
        extra_seen.add(uuid)
        states[uuid] = DeviceState(
            uuid=uuid,
            name=f"cli_uuid_{index}",
            device_type="cli_extra",
            expected_interval_sec=CLI_EXTRA_EXPECTED_INTERVAL_SEC,
        )

    if not states:
        raise ConfigError("No enabled devices to monitor")
    return states


def truncate_text(text: str, limit: int = RAW_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated {len(text) - limit} chars]"


def decode_payload(payload: bytes) -> str:
    return payload.decode("utf-8", errors="replace")


def format_payload(payload_text: str, json_pretty: bool) -> str:
    try:
        parsed: Any = json.loads(payload_text)
    except json.JSONDecodeError:
        return truncate_text(payload_text)

    if json_pretty:
        formatted = json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True)
    else:
        formatted = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return truncate_text(formatted)


def compute_status(state: DeviceState, now: float, online_factor: float, offline_factor: float) -> str:
    if state.last_seen_epoch is None:
        return "NEVER"
    age = now - state.last_seen_epoch
    online_limit = state.expected_interval_sec * online_factor
    offline_limit = state.expected_interval_sec * offline_factor
    if age <= online_limit:
        return "ONLINE"
    if age <= offline_limit:
        return "WARNING"
    return "OFFLINE"


def update_statuses(
    states: dict[str, DeviceState],
    online_factor: float,
    offline_factor: float,
    now: float | None = None,
    emit_changes: bool = True,
) -> None:
    if now is None:
        now = time.time()
    for state in states.values():
        old_status = state.current_status
        new_status = compute_status(state, now, online_factor, offline_factor)
        if new_status != old_status:
            state.current_status = new_status
            if emit_changes:
                print(
                    f"[{local_timestamp(now)}] [STATUS CHANGE] "
                    f"{state.name} {state.uuid} {old_status} -> {new_status}",
                    flush=True,
                )


def find_matches(payload_text: str, states: dict[str, DeviceState]) -> list[DeviceState]:
    payload_upper = payload_text.upper()
    return [state for uuid, state in states.items() if uuid in payload_upper]


def age_text(state: DeviceState, now: float) -> str:
    if state.last_seen_epoch is None:
        return "-"
    return f"{now - state.last_seen_epoch:.1f}s"


def interval_text(seconds: float) -> str:
    if seconds.is_integer():
        return f"{int(seconds)}s"
    return f"{seconds:.1f}s"


def print_status_table(
    states: dict[str, DeviceState],
    online_factor: float,
    offline_factor: float,
    title: str = "Status",
) -> None:
    now = time.time()
    update_statuses(states, online_factor, offline_factor, now=now)
    print(f"\n[{local_timestamp(now)}] {title}")
    print("Name            | Type           | UUID                     | Interval | Count | Last Seen           | Age      | Status")
    print("----------------+----------------+--------------------------+----------+-------+---------------------+----------+--------")
    for state in states.values():
        print(
            f"{state.name[:14]:<14} | {state.device_type[:14]:<14} | {state.uuid:<24} | "
            f"{interval_text(state.expected_interval_sec):>8} | {state.count:>5} | "
            f"{state.last_seen_text:<19} | {age_text(state, now):>8} | {state.current_status}"
        )
    print("", flush=True)


def print_summary(
    states: dict[str, DeviceState],
    online_factor: float,
    offline_factor: float,
) -> None:
    now = time.time()
    update_statuses(states, online_factor, offline_factor, now=now, emit_changes=False)
    print(f"\n[{local_timestamp(now)}] Final summary")
    print("Name            | Type           | UUID                     | First Seen          | Last Seen           | Count | Final Status")
    print("----------------+----------------+--------------------------+---------------------+---------------------+-------+-------------")
    for state in states.values():
        print(
            f"{state.name[:14]:<14} | {state.device_type[:14]:<14} | {state.uuid:<24} | "
            f"{state.first_seen_text:<19} | {state.last_seen_text:<19} | "
            f"{state.count:>5} | {state.current_status}"
        )
    print("", flush=True)


def print_devices(config: WatchConfig) -> None:
    print("Enabled | Name            | Type           | UUID                     | Interval | Note")
    print("--------+-----------------+----------------+--------------------------+----------+-----")
    for device in config.devices:
        enabled = "yes" if device.enabled else "no"
        print(
            f"{enabled:<7} | {device.name[:15]:<15} | {device.device_type[:14]:<14} | "
            f"{device.uuid:<24} | {interval_text(device.expected_interval_sec):>8} | {device.note}"
        )


def build_client(
    args: argparse.Namespace,
    config: WatchConfig,
    states: dict[str, DeviceState],
    lock: threading.Lock,
    mqtt_module: Any,
) -> Any:
    mqtt = mqtt_module
    client = mqtt.Client()

    def on_connect(client: Any, userdata: object, flags: dict[str, int], rc: int, properties: object | None = None) -> None:
        if str(rc) not in {"0", "Success"}:
            print(f"MQTT connection failed: rc={rc}", file=sys.stderr, flush=True)
            return
        print(
            f"[{local_timestamp()}] Connected to MQTT broker {config.host}:{config.port}; "
            f"subscribing to topic '{config.topic}'",
            flush=True,
        )
        result, mid = client.subscribe(config.topic)
        if result != mqtt.MQTT_ERR_SUCCESS:
            print(
                f"Subscribe failed for topic '{config.topic}': result={result}, mid={mid}",
                file=sys.stderr,
                flush=True,
            )

    def on_disconnect(client: Any, userdata: object, rc: int, properties: object | None = None) -> None:
        if str(rc) not in {"0", "Success"}:
            print(f"[{local_timestamp()}] MQTT disconnected unexpectedly: rc={rc}", file=sys.stderr, flush=True)
        else:
            print(f"[{local_timestamp()}] MQTT disconnected.", flush=True)

    def on_message(client: Any, userdata: object, msg: Any) -> None:
        received_epoch = time.time()
        received_text = local_timestamp(received_epoch)
        payload_text = decode_payload(msg.payload)

        with lock:
            matches = find_matches(payload_text, states)
            for state in matches:
                if state.first_seen_epoch is None:
                    state.first_seen_epoch = received_epoch
                    state.first_seen_text = received_text
                state.last_seen_epoch = received_epoch
                state.last_seen_text = received_text
                state.count += 1
                state.last_payload = truncate_text(payload_text)
            update_statuses(states, config.online_factor, config.offline_factor, now=received_epoch)

        if not matches and not args.all:
            return

        payload = format_payload(payload_text, args.json_pretty)
        if matches:
            for state in matches:
                print(
                    f"\n[{received_text}] [MATCH] name={state.name} type={state.device_type} "
                    f"uuid={state.uuid} topic={msg.topic}"
                )
                print(payload, flush=True)
        else:
            print(f"\n[{received_text}] [NO MATCH] topic={msg.topic}")
            print(payload, flush=True)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    return client


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        loaded_config = load_config(args.config)
        config = apply_overrides(loaded_config, args)
        validate_runtime_config(
            config,
            host_overridden=args.host is not None,
            allow_placeholder_host=args.list_devices or args.validate_config,
        )
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    if args.list_devices:
        print_devices(config)
        return 0

    if args.validate_config:
        print(f"Config OK: {args.config}")
        print(f"Enabled devices: {sum(1 for device in config.devices if device.enabled)}")
        return 0

    try:
        states = build_states(config, args.extra_uuids)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print(
            "Missing dependency: paho-mqtt. Install it with: pip install paho-mqtt",
            file=sys.stderr,
        )
        return 2

    lock = threading.Lock()
    stop_event = threading.Event()

    def request_stop(signum: int, frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    client = build_client(args, config, states, lock, mqtt)

    try:
        print(
            f"[{local_timestamp()}] Connecting to MQTT broker {config.host}:{config.port} "
            f"topic='{config.topic}' devices={len(states)}",
            flush=True,
        )
        client.connect(config.host, config.port, keepalive=config.keepalive)
    except OSError as exc:
        print(f"MQTT connection error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Unexpected MQTT setup error: {exc}", file=sys.stderr)
        return 1

    client.loop_start()
    next_status_epoch = time.time() + config.status_interval_sec

    try:
        while not stop_event.is_set():
            now = time.time()
            if now >= next_status_epoch:
                with lock:
                    print_status_table(
                        states,
                        config.online_factor,
                        config.offline_factor,
                    )
                next_status_epoch = now + config.status_interval_sec
            time.sleep(0.2)
    finally:
        print("\nStopping MQTT watcher...", flush=True)
        client.loop_stop()
        client.disconnect()
        with lock:
            print_summary(states, config.online_factor, config.offline_factor)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
