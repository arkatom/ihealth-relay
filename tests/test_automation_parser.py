"""automation_parser.py (オートメーション JSON の日次集計) のテスト。

fixture は ``tests/fixtures/automation/`` の合成データ (2030 年の架空の値)。
期待値はテスト内に手計算で書く。
"""

from __future__ import annotations

import json
import logging
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ihealth import automation_parser
from ihealth.automation_parser import (
    HEALTH_FIELDS,
    SLEEP_MODE_AGGREGATED,
    SLEEP_MODE_SEGMENTS,
    build_activity_detail,
    extract_workouts,
    has_metrics,
    needs_prev_payload,
    parse_automation_day,
    parse_automation_day_detailed,
    parse_hae_datetime,
    workout_name_ja,
)
from ihealth.models import DailyHealthData
from ihealth.parser import ParseContext


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "automation"
_LOGGER = logging.getLogger("test_automation_parser")


def _load_fixture(relative: str) -> dict:
    return json.loads((FIXTURES / relative).read_text(encoding="utf-8"))


def _ctx(d: date, max_hr: float = 180.0) -> ParseContext:
    return ParseContext(target_date=d, max_heart_rate=max_hr)


def _payload(*metrics: dict, workouts: "list | None" = None) -> dict:
    data: dict = {"metrics": list(metrics)}
    if workouts is not None:
        data["workouts"] = workouts
    return {"data": data}


def _metric(name: str, units: str, *records: dict) -> dict:
    return {"name": name, "units": units, "data": list(records)}


def _rec(when: str, **fields: object) -> dict:
    return {"date": when, "source": "Sample Watch", **fields}


D = date(2030, 1, 15)


def _parse(payload: dict, d: date = D, **kwargs):
    return parse_automation_day_detailed(payload, d, _ctx(d), logger=_LOGGER, **kwargs)


class TestParseHaeDatetime(unittest.TestCase):
    def test_hae_format_keeps_own_offset(self):
        dt = parse_hae_datetime("2030-01-15 05:02:00 +0900")
        self.assertEqual(dt, datetime(2030, 1, 15, 5, 2, tzinfo=timezone(timedelta(hours=9))))
        west = parse_hae_datetime("2030-01-15 23:30:00 -0500")
        self.assertEqual(west.date(), date(2030, 1, 15))  # JST に直すと 1/16 だが現地日付のまま
        self.assertEqual(west.utcoffset(), timedelta(hours=-5))

    def test_other_formats(self):
        self.assertEqual(
            parse_hae_datetime("2030-01-15T05:02:00+09:00"),
            datetime(2030, 1, 15, 5, 2, tzinfo=timezone(timedelta(hours=9))),
        )
        self.assertEqual(
            parse_hae_datetime("2030-01-15 05:02:00.500 +0900").microsecond, 500000,
        )
        self.assertEqual(parse_hae_datetime("2030-01-15 05:02:00"), datetime(2030, 1, 15, 5, 2))

    def test_invalid(self):
        for value in (None, 123, "", "   ", "yesterday", "2030-13-01 00:00:00 +0900"):
            with self.subTest(value=value):
                self.assertIsNone(parse_hae_datetime(value))


class TestAggregatedFixture(unittest.TestCase):
    """aggregateData=true / aggregateSleep=true (現役設定) の合成 1 日分。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = _load_fixture("aggregated/HealthAutoExport-2030-01-15.json")
        cls.result = parse_automation_day_detailed(
            cls.payload, D, _ctx(D, 180.0), logger=_LOGGER,
        )

    def test_values(self):
        h = self.result.health
        self.assertIsInstance(h, DailyHealthData)
        self.assertEqual(h.date, D)
        # 前日 23:59 の 999 歩は除外: 120.4 + 80.3 + 300 = 500.7 → 501
        self.assertEqual(h.step_count, 501)
        self.assertIsInstance(h.step_count, int)
        self.assertAlmostEqual(h.distance_km, 0.4)
        # kJ → kcal: (418.4 + 836.8) / 4.184 = 300
        self.assertAlmostEqual(h.active_energy_kcal, 300.0)
        self.assertAlmostEqual(h.exercise_minutes, 3.0)
        self.assertAlmostEqual(h.heart_rate_avg, 100.0)
        self.assertAlmostEqual(h.heart_rate_max, 150.0)
        # max_hr=180 → 中強度 90 / 高強度 126: 60→0, 100→1, 140→2
        self.assertEqual(h.exercise_intensity_score, 3.0)
        self.assertAlmostEqual(h.heart_rate_resting, 58.0)
        self.assertAlmostEqual(h.oxygen_saturation, 97.0)
        self.assertAlmostEqual(h.sleep_hours, 7.0)
        self.assertIsNone(h.nap_hours)  # aggregated は昼寝を分離できない
        self.assertEqual(h.mindful_sessions, 2)
        self.assertAlmostEqual(h.mindful_minutes, 5.6)
        self.assertAlmostEqual(h.body_mass_kg, 61.0)  # 20:00 の最新値 (BMI 21.0 ではない)
        self.assertAlmostEqual(h.body_fat_percentage, 18.5)

    def test_metadata(self):
        self.assertEqual(self.result.activity_detail, "瞑想6分")
        self.assertTrue(self.result.has_metrics)
        self.assertEqual(self.result.sleep_mode, SLEEP_MODE_AGGREGATED)
        self.assertEqual(self.result.errors, ())
        self.assertEqual(self.result.dropped_fields, ())
        self.assertFalse(needs_prev_payload(self.payload))
        self.assertTrue(has_metrics(self.payload))

    def test_tuple_api(self):
        health, activity = parse_automation_day(self.payload, D, _ctx(D, 180.0))
        self.assertEqual(health, self.result.health)
        self.assertEqual(activity, "瞑想6分")

    def test_other_date_gives_nothing(self):
        # ファイルの中身と違う日付を指定 → 対象日の record が無いので全 None
        result = parse_automation_day_detailed(
            self.payload, date(2030, 1, 20), _ctx(date(2030, 1, 20)), logger=_LOGGER,
        )
        for name in HEALTH_FIELDS:
            self.assertIsNone(getattr(result.health, name), name)
        # metric はあるが対象日のデータが無い → 「活動なし」とは言えないので None
        self.assertIsNone(result.activity_detail)


class TestRecordDateFiltering(unittest.TestCase):
    def test_record_offset_calendar_date_is_used(self):
        payload = _payload(_metric(
            "step_count", "count",
            _rec("2030-01-15 23:30:00 -0500", qty=100),   # 現地 1/15 (JST だと 1/16)
            _rec("2030-01-16 00:30:00 +0900", qty=7),     # 1/16
            _rec("2030-01-14 23:30:00 +0900", qty=5),     # 1/14
        ))
        self.assertEqual(_parse(payload).health.step_count, 100)

    def test_records_without_valid_date_are_skipped(self):
        payload = _payload(_metric(
            "step_count", "count",
            {"qty": 1000},
            {"date": None, "qty": 1000},
            {"date": 20300115, "qty": 1000},
            _rec("2030-01-15 10:00:00 +0900", qty=10),
        ))
        self.assertEqual(_parse(payload).health.step_count, 10)

    def test_start_date_key_is_accepted(self):
        payload = _payload(_metric(
            "step_count", "count", {"startDate": "2030-01-15 10:00:00 +0900", "qty": 4},
        ))
        self.assertEqual(_parse(payload).health.step_count, 4)


class TestTolerance(unittest.TestCase):
    def test_malformed_records_and_metrics_are_ignored(self):
        payload = {"data": {"metrics": [
            "not a dict",
            {"units": "count", "data": []},                  # name 無し
            {"name": "", "data": []},
            {"name": "heart_rate", "units": "count/min", "data": "oops"},
            _metric(
                "step_count", "count",
                "junk",
                _rec("2030-01-15 10:00:00 +0900", qty="12"),   # 文字列は数値扱いしない
                _rec("2030-01-15 10:01:00 +0900", qty=True),   # bool も不可
                _rec("2030-01-15 10:02:00 +0900", qty=float("nan")),
                _rec("2030-01-15 10:03:00 +0900"),             # qty 無し
                _rec("2030-01-15 10:04:00 +0900", qty=3),
            ),
        ]}}
        result = _parse(payload)
        self.assertEqual(result.health.step_count, 3)
        self.assertIsNone(result.health.heart_rate_avg)
        self.assertEqual(result.errors, ())

    def test_no_metrics(self):
        for payload in ({"data": {}}, {"data": {"metrics": []}}, {}, {"data": None}):
            with self.subTest(payload=payload):
                result = _parse(payload)
                self.assertFalse(result.has_metrics)
                self.assertIsNone(result.activity_detail)
                self.assertIsNone(result.sleep_mode)
                self.assertTrue(all(getattr(result.health, n) is None for n in HEALTH_FIELDS))
                self.assertFalse(has_metrics(payload))

    def test_metric_name_is_case_and_space_insensitive(self):
        payload = _payload(
            _metric(" Step_Count ", "count", _rec("2030-01-15 10:00:00 +0900", qty=5)),
        )
        self.assertEqual(_parse(payload).health.step_count, 5)

    def test_unexpected_data_error_is_contained_per_metric(self):
        payload = _payload(
            _metric("step_count", "count", _rec("2030-01-15 10:00:00 +0900", qty=5)),
            _metric("resting_heart_rate", "count/min", _rec("2030-01-15 00:01:00 +0900", qty=60)),
        )
        with patch.object(automation_parser, "_step_count", side_effect=TypeError("boom")):
            with self.assertLogs(_LOGGER, level="WARNING"):
                result = _parse(payload)
        self.assertIsNone(result.health.step_count)
        self.assertEqual(result.health.heart_rate_resting, 60.0)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("step_count", result.errors[0])

    def test_programming_errors_propagate(self):
        payload = _payload(_metric("step_count", "count", _rec("2030-01-15 10:00:00 +0900", qty=5)))
        with patch.object(automation_parser, "_step_count", side_effect=AttributeError("bug")):
            with self.assertRaises(AttributeError):
                _parse(payload)


class TestUnits(unittest.TestCase):
    def _distance(self, units: str, qty: float) -> "float | None":
        payload = _payload(_metric("walking_running_distance", units,
                                   _rec("2030-01-15 10:00:00 +0900", qty=qty)))
        return _parse(payload).health.distance_km

    def test_distance_units(self):
        self.assertAlmostEqual(self._distance("km", 1.5), 1.5)
        self.assertAlmostEqual(self._distance("mi", 1.0), 1.609344)
        self.assertAlmostEqual(self._distance("m", 2500), 2.5)
        self.assertAlmostEqual(self._distance("KM", 1.0), 1.0)

    def test_unknown_distance_unit_is_not_guessed(self):
        with self.assertLogs(_LOGGER, level="WARNING") as logs:
            self.assertIsNone(self._distance("furlong", 3.0))
        self.assertIn("未対応", logs.output[0])

    def _energy(self, units: str, qty: float) -> "float | None":
        payload = _payload(_metric("active_energy", units,
                                   _rec("2030-01-15 10:00:00 +0900", qty=qty)))
        return _parse(payload).health.active_energy_kcal

    def test_energy_units(self):
        self.assertAlmostEqual(self._energy("kcal", 250.0), 250.0)
        self.assertAlmostEqual(self._energy("Cal", 250.0), 250.0)
        self.assertAlmostEqual(self._energy("kJ", 418.4), 100.0)
        self.assertAlmostEqual(self._energy("kj", 418.4), 100.0)

    def test_small_calorie_is_not_treated_as_kcal(self):
        with self.assertLogs(_LOGGER, level="WARNING"):
            self.assertIsNone(self._energy("cal", 250000.0))

    def test_weight_units(self):
        for units, qty, expected in (("kg", 60.0, 60.0), ("lb", 132.0, 59.874193),
                                     ("g", 60500.0, 60.5)):
            with self.subTest(units=units):
                payload = _payload(_metric("weight_body_mass", units,
                                           _rec("2030-01-15 07:00:00 +0900", qty=qty)))
                self.assertAlmostEqual(_parse(payload).health.body_mass_kg, expected, places=5)

    def test_missing_units_use_default(self):
        payload = _payload({"name": "active_energy", "data": [
            _rec("2030-01-15 10:00:00 +0900", qty=12.5),
        ]})
        self.assertAlmostEqual(_parse(payload).health.active_energy_kcal, 12.5)

    def test_exercise_time_units(self):
        payload = _payload(_metric("apple_exercise_time", "hr",
                                   _rec("2030-01-15 10:00:00 +0900", qty=0.5)))
        self.assertAlmostEqual(_parse(payload).health.exercise_minutes, 30.0)


class TestDuplicateMetricNames(unittest.TestCase):
    """同名 metric の重複: 優先単位の 1 つだけ採用し、加算しない。"""

    def test_energy_prefers_kcal_regardless_of_order(self):
        kj = _metric("active_energy", "kJ", _rec("2030-01-15 10:00:00 +0900", qty=418.4))
        kcal = _metric("active_energy", "kcal", _rec("2030-01-15 10:00:00 +0900", qty=100.5))
        for order in ((kj, kcal), (kcal, kj)):
            with self.subTest(first=order[0]["units"]):
                with self.assertLogs(_LOGGER, level="WARNING") as logs:
                    value = _parse(_payload(*order)).health.active_energy_kcal
                self.assertAlmostEqual(value, 100.5)  # 100 + 100.5 にはしない
                self.assertIn("複数回出現", logs.output[0])

    def test_weight_prefers_kg_then_lb_then_g(self):
        lb = _metric("weight_body_mass", "lb", _rec("2030-01-15 07:00:00 +0900", qty=150.0))
        g = _metric("weight_body_mass", "g", _rec("2030-01-15 07:00:00 +0900", qty=99000.0))
        kg = _metric("weight_body_mass", "kg", _rec("2030-01-15 07:00:00 +0900", qty=61.0))
        with self.assertLogs(_LOGGER, level="WARNING"):
            self.assertAlmostEqual(_parse(_payload(g, lb, kg)).health.body_mass_kg, 61.0)
        with self.assertLogs(_LOGGER, level="WARNING"):
            self.assertAlmostEqual(_parse(_payload(g, lb)).health.body_mass_kg,
                                   150.0 * 0.45359237)

    def test_distance_prefers_km_then_m_then_mi(self):
        mi = _metric("walking_running_distance", "mi", _rec("2030-01-15 07:00:00 +0900", qty=1.0))
        m = _metric("walking_running_distance", "m", _rec("2030-01-15 07:00:00 +0900", qty=1600.0))
        km = _metric("walking_running_distance", "km", _rec("2030-01-15 07:00:00 +0900", qty=1.61))
        with self.assertLogs(_LOGGER, level="WARNING"):
            self.assertAlmostEqual(_parse(_payload(mi, m, km)).health.distance_km, 1.61)
        with self.assertLogs(_LOGGER, level="WARNING"):
            self.assertAlmostEqual(_parse(_payload(mi, m)).health.distance_km, 1.6)

    def test_metric_without_priority_keeps_first(self):
        a = _metric("step_count", "count", _rec("2030-01-15 07:00:00 +0900", qty=10))
        b = _metric("step_count", "count", _rec("2030-01-15 07:00:00 +0900", qty=20))
        with self.assertLogs(_LOGGER, level="WARNING"):
            self.assertEqual(_parse(_payload(a, b)).health.step_count, 10)


class TestHeartRate(unittest.TestCase):
    def _hr(self, *records: dict, max_hr: float = 180.0):
        payload = _payload(_metric("heart_rate", "count/min", *records))
        return parse_automation_day_detailed(payload, D, _ctx(D, max_hr), logger=_LOGGER).health

    def test_capitalized_keys(self):
        h = self._hr(_rec("2030-01-15 07:00:00 +0900", Avg=70, Min=60, Max=80),
                     _rec("2030-01-15 07:01:00 +0900", Avg=90, Min=85, Max=120))
        self.assertAlmostEqual(h.heart_rate_avg, 80.0)
        self.assertAlmostEqual(h.heart_rate_max, 120.0)

    def test_lowercase_and_qty_records(self):
        h = self._hr(_rec("2030-01-15 07:00:00 +0900", avg=70, min=60, max=80),
                     _rec("2030-01-15 07:01:00 +0900", qty=100))
        self.assertAlmostEqual(h.heart_rate_avg, 85.0)
        self.assertAlmostEqual(h.heart_rate_max, 100.0)

    def test_intensity_boundaries_are_inclusive(self):
        # max_hr=200 → 中強度 100.0 / 高強度 140.0 ちょうどで加点
        h = self._hr(_rec("2030-01-15 07:00:00 +0900", Avg=99.9, Max=99.9),
                     _rec("2030-01-15 07:01:00 +0900", Avg=100.0, Max=100.0),
                     _rec("2030-01-15 07:02:00 +0900", Avg=139.9, Max=139.9),
                     _rec("2030-01-15 07:03:00 +0900", Avg=140.0, Max=140.0),
                     max_hr=200.0)
        self.assertEqual(h.exercise_intensity_score, 0 + 1 + 1 + 2)

    def test_same_minute_records_are_averaged(self):
        # 同じ分の 2 record (秒違い / 別ソース) は bucket 平均: (100 + 140) / 2 = 120 → 1pt
        h = self._hr(_rec("2030-01-15 07:00:05 +0900", Avg=100, Max=100),
                     {"date": "2030-01-15 07:00:40 +0900", "Avg": 140, "Max": 140,
                      "source": "Sample Phone"})
        self.assertEqual(h.exercise_intensity_score, 1.0)

    def test_minute_bucket_is_absolute_time(self):
        # 同一時刻を別 offset で表した 2 record は同じ bucket (平均で判定)
        h = self._hr(_rec("2030-01-15 07:00:00 +0900", Avg=140, Max=140),
                     _rec("2030-01-14 22:00:00 +0000", Avg=140, Max=140))
        # 2 record 目は現地日付 1/14 なので対象外 → bucket 1 個、2pt
        self.assertEqual(h.exercise_intensity_score, 2.0)

    def test_zero_points_is_zero_not_none(self):
        h = self._hr(_rec("2030-01-15 03:00:00 +0900", Avg=55, Max=60))
        self.assertEqual(h.exercise_intensity_score, 0.0)

    def test_no_heart_rate_is_none(self):
        h = self._hr()
        self.assertIsNone(h.exercise_intensity_score)
        self.assertIsNone(h.heart_rate_avg)
        self.assertIsNone(h.heart_rate_max)

    def test_avg_above_max_drops_both(self):
        # Max が欠けた record では Avg を max 候補にするので通常は avg <= max。
        # 壊れた record (Avg > Max) は相互制約違反で両方 None (他は残る)。
        payload = _payload(
            _metric("heart_rate", "count/min",
                    _rec("2030-01-15 07:00:00 +0900", Avg=150, Max=90)),
            _metric("step_count", "count", _rec("2030-01-15 07:00:00 +0900", qty=10)),
        )
        with self.assertLogs("ihealth.automation_parser", level="WARNING"):
            result = parse_automation_day_detailed(payload, D, _ctx(D))
        self.assertIsNone(result.health.heart_rate_avg)
        self.assertIsNone(result.health.heart_rate_max)
        self.assertEqual(result.health.step_count, 10)
        self.assertEqual(set(result.dropped_fields), {"heart_rate_avg", "heart_rate_max"})


class TestSimpleMetrics(unittest.TestCase):
    def test_resting_heart_rate_mean(self):
        payload = _payload(_metric("resting_heart_rate", "count/min",
                                   _rec("2030-01-15 00:01:00 +0900", qty=60),
                                   _rec("2030-01-15 12:00:00 +0900", qty=64)))
        self.assertAlmostEqual(_parse(payload).health.heart_rate_resting, 62.0)

    def test_oxygen_fraction_is_converted_to_percent(self):
        payload = _payload(_metric("blood_oxygen_saturation", "%",
                                   _rec("2030-01-15 01:00:00 +0900", qty=0.96),
                                   _rec("2030-01-15 02:00:00 +0900", qty=0.98)))
        self.assertAlmostEqual(_parse(payload).health.oxygen_saturation, 97.0)

    def test_oxygen_percent_is_kept(self):
        payload = _payload(_metric("blood_oxygen_saturation", "%",
                                   _rec("2030-01-15 01:00:00 +0900", qty=95),
                                   _rec("2030-01-15 02:00:00 +0900", Avg=99)))
        self.assertAlmostEqual(_parse(payload).health.oxygen_saturation, 97.0)

    def test_body_fat_latest_and_fraction(self):
        payload = _payload(_metric("body_fat_percentage", "%",
                                   _rec("2030-01-15 21:00:00 +0900", qty=0.195),
                                   _rec("2030-01-15 07:00:00 +0900", qty=0.30)))
        self.assertAlmostEqual(_parse(payload).health.body_fat_percentage, 19.5)

    def test_weight_latest_by_time_not_order(self):
        payload = _payload(_metric("weight_body_mass", "kg",
                                   _rec("2030-01-15 21:00:00 +0900", qty=60.4),
                                   _rec("2030-01-15 07:00:00 +0900", qty=61.0)))
        self.assertAlmostEqual(_parse(payload).health.body_mass_kg, 60.4)

    def test_bmi_is_never_used_as_weight(self):
        payload = _payload(_metric("body_mass_index", "count",
                                   _rec("2030-01-15 07:00:00 +0900", qty=21.0)))
        self.assertIsNone(_parse(payload).health.body_mass_kg)


class TestMindful(unittest.TestCase):
    def test_sessions_and_minutes(self):
        payload = _payload(_metric("mindful_minutes", "min",
                                   _rec("2030-01-15 06:00:00 +0900", qty=10.0),
                                   _rec("2030-01-15 21:00:00 +0900", qty=3.4)))
        result = _parse(payload)
        self.assertEqual(result.health.mindful_sessions, 2)
        self.assertAlmostEqual(result.health.mindful_minutes, 13.4)
        self.assertEqual(result.activity_detail, "瞑想13分")

    def test_minute_merged_sources_count_as_separate_sessions(self):
        # 同じ分に始まった 2 ソースのセッションは 1 record (qty 合算, source が | 区切り)
        payload = _payload(_metric("mindful_minutes", "min",
                                   {"date": "2030-01-15 13:18:00 +0900", "qty": 27.0,
                                    "source": "Sample Mindfulness | Sample\u00a0Watch"},
                                   {"date": "2030-01-15 20:00:00 +0900", "qty": 1.0,
                                    "source": "Sample Mindfulness"}))
        result = _parse(payload)
        self.assertEqual(result.health.mindful_sessions, 3)
        self.assertAlmostEqual(result.health.mindful_minutes, 28.0)

    def test_duration_fallback_from_start_end(self):
        payload = _payload(_metric("mindful_minutes", "min", {
            "start": "2030-01-15 06:00:00 +0900", "end": "2030-01-15 06:12:30 +0900",
        }))
        result = _parse(payload)
        self.assertEqual(result.health.mindful_sessions, 1)
        self.assertAlmostEqual(result.health.mindful_minutes, 12.5)

    def test_under_one_minute_is_not_in_activity_detail(self):
        payload = _payload(_metric("mindful_minutes", "min",
                                   _rec("2030-01-15 06:00:00 +0900", qty=0.4)))
        result = _parse(payload)
        self.assertEqual(result.health.mindful_sessions, 1)
        self.assertAlmostEqual(result.health.mindful_minutes, 0.4)
        self.assertEqual(result.activity_detail, "なし")

    def test_no_mindful_but_other_metrics(self):
        payload = _payload(_metric("step_count", "count", _rec("2030-01-15 06:00:00 +0900", qty=5)))
        result = _parse(payload)
        self.assertIsNone(result.health.mindful_sessions)
        self.assertIsNone(result.health.mindful_minutes)
        self.assertEqual(result.activity_detail, "なし")


class TestAggregatedSleep(unittest.TestCase):
    def _sleep(self, *records: dict, units: str = "hr", prev: "dict | None" = None):
        payload = _payload(_metric("sleep_analysis", units, *records))
        return _parse(payload, prev_payload=prev)

    def test_record_date_takes_precedence_over_sleep_end(self):
        # date (= その睡眠が属する日) が 1/15、sleepEnd が翌 1/16 00:30 の record は 1/15 のもの
        records = (_rec("2030-01-15 00:00:00 +0900", totalSleep=6.0,
                        sleepStart="2030-01-15 18:00:00 +0900",
                        sleepEnd="2030-01-16 00:30:00 +0900"),)
        self.assertAlmostEqual(self._sleep(*records).health.sleep_hours, 6.0)
        payload = _payload(_metric("sleep_analysis", "hr", *records))
        next_day = date(2030, 1, 16)
        result = parse_automation_day_detailed(payload, next_day, _ctx(next_day))
        self.assertIsNone(result.health.sleep_hours)

    def test_date_missing_falls_back_to_sleep_end(self):
        result = self._sleep({"totalSleep": 6.5, "sleepStart": "2030-01-14 23:00:00 +0900",
                              "sleepEnd": "2030-01-15 06:00:00 +0900", "source": "Sample Watch"})
        self.assertAlmostEqual(result.health.sleep_hours, 6.5)
        self.assertEqual(result.sleep_mode, SLEEP_MODE_AGGREGATED)

    def test_total_zero_uses_stage_sum(self):
        result = self._sleep(_rec("2030-01-15 00:00:00 +0900", totalSleep=0, asleep=0.5,
                                  core=4.0, deep=1.0, rem=1.0, awake=0.4,
                                  sleepEnd="2030-01-15 07:00:00 +0900"))
        self.assertAlmostEqual(result.health.sleep_hours, 6.5)

    def test_all_zero_is_none(self):
        result = self._sleep(_rec("2030-01-15 00:00:00 +0900", totalSleep=0, asleep=0,
                                  core=0, deep=0, rem=0, inBed=7.0,
                                  sleepEnd="2030-01-15 07:00:00 +0900"))
        self.assertIsNone(result.health.sleep_hours)

    def test_multiple_sources_take_max_not_sum(self):
        result = self._sleep(
            _rec("2030-01-15 00:00:00 +0900", totalSleep=6.0, sleepEnd="2030-01-15 06:00:00 +0900"),
            {"date": "2030-01-15 00:00:00 +0900", "totalSleep": 7.2,
             "sleepEnd": "2030-01-15 06:30:00 +0900", "source": "Sample Sleep App"},
        )
        self.assertAlmostEqual(result.health.sleep_hours, 7.2)

    def test_other_dates_ignored_and_prev_payload_harmless(self):
        prev = _payload(_metric("sleep_analysis", "hr",
                                _rec("2030-01-14 00:00:00 +0900", totalSleep=8.0,
                                     sleepEnd="2030-01-14 06:00:00 +0900")))
        result = self._sleep(
            _rec("2030-01-14 00:00:00 +0900", totalSleep=8.0, sleepEnd="2030-01-14 06:00:00 +0900"),
            _rec("2030-01-15 00:00:00 +0900", totalSleep=5.0, sleepEnd="2030-01-15 06:00:00 +0900"),
            prev=prev,
        )
        self.assertAlmostEqual(result.health.sleep_hours, 5.0)
        self.assertIsNone(result.health.nap_hours)

    def test_minutes_unit(self):
        result = self._sleep(_rec("2030-01-15 00:00:00 +0900", totalSleep=390,
                                  sleepEnd="2030-01-15 06:00:00 +0900"), units="min")
        self.assertAlmostEqual(result.health.sleep_hours, 6.5)


class TestSegmentSleepFixture(unittest.TestCase):
    """非集計 segment: 前日ファイル結合 / 重複除去 / セッション結合 / ソース最大 / 昼寝。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.prev = _load_fixture("segments/HealthAutoExport-2030-02-01.json")
        cls.day = _load_fixture("segments/HealthAutoExport-2030-02-02.json")
        cls.target = date(2030, 2, 2)

    def test_needs_prev_payload(self):
        self.assertTrue(needs_prev_payload(self.day))

    def test_with_prev_payload(self):
        result = parse_automation_day_detailed(
            self.day, self.target, _ctx(self.target), prev_payload=self.prev, logger=_LOGGER,
        )
        self.assertEqual(result.sleep_mode, SLEEP_MODE_SEGMENTS)
        # Sample Watch: 22:30-23:30 Core 1.0 + 23:45-00:30 Deep 0.75 (両ファイルに重複、1 回だけ)
        #   + 00:30-02:00 REM 1.5 + 02:10-06:10 Core 4.0 (qty 無し → end-start) = 7.25
        #   Awake / In Bed は除外、22:00 終了の Core 0.5 は翌日の夜扱いで対象外
        # Sample Sleep App: 00:00-05:00 Asleep 5.0 → ソース最大は 7.25
        self.assertAlmostEqual(result.health.sleep_hours, 7.25)
        # 昼寝: Watch 0.75 / Sleep App 1.0 → 最大 1.0
        self.assertAlmostEqual(result.health.nap_hours, 1.0)
        self.assertEqual(result.health.step_count, 2000)

    def test_without_prev_payload_misses_pre_midnight_segment(self):
        result = parse_automation_day_detailed(
            self.day, self.target, _ctx(self.target), logger=_LOGGER,
        )
        # 前日ファイルにしか無い 22:30-23:30 (1.0h) が抜ける: 0.75 + 1.5 + 4.0
        self.assertAlmostEqual(result.health.sleep_hours, 6.25)

    def test_prev_day_as_target(self):
        prev_target = date(2030, 2, 1)
        result = parse_automation_day_detailed(
            self.prev, prev_target, _ctx(prev_target), logger=_LOGGER,
        )
        # 13:00-13:40 (13 時台終了) は定義上「夜主睡眠」側 (0-14 時)。
        # 22:30〜 のセッションは 2/2 00:30 終了なので 2/1 には入らない
        self.assertAlmostEqual(result.health.sleep_hours, 40 / 60)
        self.assertIsNone(result.health.nap_hours)

    def test_long_awake_gap_splits_sessions(self):
        records = [
            # 21:00-23:00 (前日) → 3 時間の覚醒 → 02:00-06:00
            {"startDate": "2030-02-01 21:00:00 +0900", "endDate": "2030-02-01 23:00:00 +0900",
             "value": "Core", "source": "Sample Watch"},
            {"startDate": "2030-02-02 02:00:00 +0900", "endDate": "2030-02-02 06:00:00 +0900",
             "value": "Core", "source": "Sample Watch"},
        ]
        payload = _payload(_metric("sleep_analysis", "hr", *records))
        result = parse_automation_day_detailed(payload, self.target, _ctx(self.target))
        # 2 時間超の間隔は別セッション → 23:00 終了の前半は 2/2 に入らない
        self.assertAlmostEqual(result.health.sleep_hours, 4.0)

    def test_stage_names_are_case_insensitive(self):
        records = [
            {"startDate": "2030-02-02 00:00:00 +0900", "endDate": "2030-02-02 01:00:00 +0900",
             "value": "ASLEEP_UNSPECIFIED", "source": "S"},
            {"startDate": "2030-02-02 01:00:00 +0900", "endDate": "2030-02-02 02:00:00 +0900",
             "value": "inBed", "source": "S"},
            {"startDate": "2030-02-02 02:00:00 +0900", "endDate": "2030-02-02 03:00:00 +0900",
             "value": "rem", "source": "S"},
            {"startDate": "2030-02-02 03:00:00 +0900", "endDate": "2030-02-02 04:00:00 +0900",
             "value": "AWAKE", "source": "S"},
        ]
        payload = _payload(_metric("sleep_analysis", "hr", *records))
        result = parse_automation_day_detailed(payload, self.target, _ctx(self.target))
        self.assertAlmostEqual(result.health.sleep_hours, 2.0)


class TestWorkouts(unittest.TestCase):
    def test_fixture_activity_detail(self):
        payload = _load_fixture("workouts/HealthAutoExport-2030-03-10.json")
        target = date(2030, 3, 10)
        result = parse_automation_day_detailed(payload, target, _ctx(target), logger=_LOGGER)
        # Outdoor Walk 21 分 + Walking 10 分 は同じ「ウォーキング」に合算、
        # Hiking は duration 無し → end-start 90 分、未知名はそのまま、前日の Yoga は除外
        self.assertEqual(
            result.activity_detail,
            "瞑想3分、ウォーキング31分、ハイキング90分、Underwater Basket Weaving15分",
        )
        self.assertEqual(
            extract_workouts(payload, target),
            [("ウォーキング", 31.0), ("ハイキング", 90.0), ("Underwater Basket Weaving", 15.0)],
        )

    def test_workout_name_ja(self):
        self.assertEqual(workout_name_ja("Hiking"), "ハイキング")
        self.assertEqual(workout_name_ja("  traditional   strength training "), "筋力トレーニング")
        self.assertEqual(workout_name_ja("Other"), "その他")
        self.assertEqual(workout_name_ja("Quidditch"), "Quidditch")
        self.assertEqual(workout_name_ja(""), "その他")
        self.assertEqual(workout_name_ja(None), "その他")

    def test_workouts_only_day_still_has_detail(self):
        payload = _payload(
            _metric("step_count", "count", _rec("2030-01-10 10:00:00 +0900", qty=1)),  # 他日
            workouts=[{"name": "Yoga", "start": "2030-01-15 07:00:00 +0900", "duration": 1200}],
        )
        self.assertEqual(_parse(payload).activity_detail, "ヨガ20分")

    def test_malformed_workouts_are_ignored(self):
        payload = _payload(
            _metric("step_count", "count", _rec("2030-01-15 10:00:00 +0900", qty=1)),
            workouts=["x", {"name": "Yoga"},
                      {"name": "Yoga", "start": "2030-01-15 07:00:00 +0900"}],
        )
        self.assertEqual(_parse(payload).activity_detail, "なし")


class TestBuildActivityDetail(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(build_activity_detail(None), "なし")
        self.assertEqual(build_activity_detail(0.4), "なし")
        self.assertEqual(build_activity_detail(3.4), "瞑想3分")
        self.assertEqual(build_activity_detail(None, [("ヨガ", 20.2)]), "ヨガ20分")
        self.assertEqual(
            build_activity_detail(6.0, [("ウォーキング", 35.0), ("ヨガ", 0.2)]),
            "瞑想6分、ウォーキング35分",
        )


class TestValidationDrop(unittest.TestCase):
    def test_invalid_field_is_dropped_individually(self):
        payload = _payload(
            _metric("step_count", "count", _rec("2030-01-15 10:00:00 +0900", qty=1234)),
            _metric("resting_heart_rate", "count/min", _rec("2030-01-15 00:01:00 +0900", qty=300)),
            _metric("body_fat_percentage", "%", _rec("2030-01-15 07:00:00 +0900", qty=95)),
        )
        with self.assertLogs("ihealth.automation_parser", level="WARNING"):
            result = parse_automation_day_detailed(payload, D, _ctx(D))
        self.assertEqual(result.health.step_count, 1234)
        self.assertIsNone(result.health.heart_rate_resting)
        self.assertIsNone(result.health.body_fat_percentage)
        self.assertEqual(result.dropped_fields, ("heart_rate_resting", "body_fat_percentage"))


if __name__ == "__main__":
    unittest.main()
