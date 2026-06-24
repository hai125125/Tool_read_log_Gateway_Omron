from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from analyze_ddi_log import (
    DEVICE_RULES,
    build_gap_records,
    extract_temperature,
    safe_read_text,
    safe_read_text_with_stats,
)


BASE = datetime(2026, 6, 18, 12, 0, 0)


def events(previous_temp, gap_sec, current_temp=100.0):
    return [
        {"start_time": BASE, "temperature": previous_temp},
        {"start_time": BASE + timedelta(seconds=gap_sec), "temperature": current_temp},
    ]


class TemperatureIntervalGapTests(unittest.TestCase):
    def assert_status(self, uuid, previous_temp, gap_sec, expected_status):
        records = build_gap_records(
            uuid,
            events(previous_temp, gap_sec),
            DEVICE_RULES[uuid],
            default_interval_sec=1800,
        )
        self.assertEqual(records[0]["status"], expected_status)

    def test_japan_high_temp_400s_ok(self):
        self.assert_status("34394708333030314A002900", 130.0, 400, "OK")

    def test_japan_high_temp_1000s_delay(self):
        self.assert_status("34394708333030314A002900", 130.0, 1000, "DELAY")

    def test_japan_low_temp_1000s_ok(self):
        self.assert_status("34394708333030314A002900", 80.0, 1000, "OK")

    def test_omron_high_temp_400s_ok(self):
        self.assert_status("D5CAAF0615E18000FC8B3004", 190.0, 400, "OK")

    def test_omron_low_temp_1000s_ok(self):
        self.assert_status("D5CAAF0615E18000FC8B3004", 100.0, 1000, "OK")

    def test_missing_temperature_unknown(self):
        records = build_gap_records(
            "34394708333030314A002900",
            events(None, 1000),
            DEVICE_RULES["34394708333030314A002900"],
            default_interval_sec=1800,
        )
        self.assertEqual(records[0]["status"], "UNKNOWN_TEMP")

    def test_mcu_temperature_is_not_sensor_temperature(self):
        line = (
            "MQTT json Data = {'MCU': {'VDD': {'Unit': 'V', 'Value': 2.4}, "
            "'Temperature': {'Unit': '℃', 'Value': 31.9}}}"
        )
        self.assertIsNone(extract_temperature(line))


class SafeReadTextTests(unittest.TestCase):
    def write_bytes(self, data):
        temp_dir = tempfile.TemporaryDirectory()
        path = Path(temp_dir.name) / "sample.log"
        path.write_bytes(data)
        self.addCleanup(temp_dir.cleanup)
        return path

    def test_utf8_bom_is_removed(self):
        path = self.write_bytes("2026-06-18 12:00:00,000 UUID".encode("utf-8-sig"))
        text = safe_read_text(path)
        self.assertTrue(text.startswith("2026-06-18"))

    def test_cp932_auto_detect(self):
        path = self.write_bytes("2026-06-18 12:00:00,000 温度".encode("cp932"))
        text, stats = safe_read_text_with_stats(path)
        self.assertEqual(stats["encoding_used"], "cp932")
        self.assertIn("温度", text)

    def test_forced_utf8_replaces_bad_bytes(self):
        path = self.write_bytes(b"2026-06-18 12:00:00,000 \x80 UUID")
        text, stats = safe_read_text_with_stats(path, encoding="utf-8")
        self.assertIn("UUID", text)
        self.assertGreater(stats["decode_errors_count"], 0)


if __name__ == "__main__":
    unittest.main()
