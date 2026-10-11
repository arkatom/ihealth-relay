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
    ACTIVITY_NONE,
    HEALTH_FIELDS,
    METRIC_FIELDS,
    SLEEP_MODE_AGGREGATED,
    SLEEP_MODE_SEGMENTS,
    WORKOUTS_LABEL,
    compose_activity_detail,
    extract_workouts,
    has_data_for,
    has_metrics,
    needs_prev_payload,
    parse_automation_day,
    parse_automation_day_detailed,
    parse_hae_datetime,
    record_day_end,
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
        # 現役設定はワークアウトを出力しない (data.workouts が無い) → W3-7: アクティビティ詳細は
        # 瞑想 (2.6 + 3.0 = 5.6 → 6 分) + Apple のエクササイズ時間 (3 分) から組む
        # (旧: ワークアウトの有無を判断できないとして None。WAVE3 W3-7 で意図的に変更)
        self.assertEqual(self.result.activity_detail, "瞑想6分、エクササイズ3分")
        self.assertFalse(self.result.workouts_included)
        self.assertIsNone(self.result.workouts)
        self.assertTrue(self.result.workouts_known)
        self.assertNotIn(WORKOUTS_LABEL, self.result.failed_metrics)
        self.assertTrue(self.result.has_metrics)
        self.assertEqual(self.result.sleep_mode, SLEEP_MODE_AGGREGATED)
        self.assertEqual(self.result.errors, ())
        self.assertEqual(self.result.dropped_fields, ())
        self.assertEqual(self.result.unknown_fields, ())
        self.assertFalse(needs_prev_payload(self.payload))
        self.assertTrue(has_metrics(self.payload))

    def test_tuple_api(self):
        health, activity = parse_automation_day(self.payload, D, _ctx(D, 180.0))
        self.assertEqual(health, self.result.health)
        self.assertEqual(activity, "瞑想6分、エクササイズ3分")

    def test_activity_detail_when_workouts_are_exported(self):
        # ワークアウトも書き出す設定 (data.workouts あり) でその日は 0 件 → W3-7 改訂: 出力しない
        # 設定と同じくエクササイズ時間で組む (旧: ワークアウトの欄はワークアウトだけで "瞑想6分"。
        # ワークアウトを記録せずに運動した日のエクササイズ時間を取りこぼしていた。
        # WAVE3 W3-7 改訂で意図的に変更)
        payload = json.loads(json.dumps(self.payload))
        payload["data"]["workouts"] = []
        result = parse_automation_day_detailed(payload, D, _ctx(D, 180.0), logger=_LOGGER)
        self.assertTrue(result.workouts_included)
        self.assertEqual(result.workouts, ())
        self.assertEqual(result.activity_detail, "瞑想6分、エクササイズ3分")
        self.assertEqual(
            parse_automation_day(payload, D, _ctx(D, 180.0))[1], "瞑想6分、エクササイズ3分",
        )
        # その日のワークアウトがあればワークアウトの一覧 (エクササイズ時間は足さない)
        payload["data"]["workouts"] = [
            {"name": "Yoga", "start": "2030-01-15 07:00:00 +0900", "duration": 1200},
        ]
        result = parse_automation_day_detailed(payload, D, _ctx(D, 180.0), logger=_LOGGER)
        self.assertEqual(result.activity_detail, "瞑想6分、ヨガ20分")

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
        # 個々の壊れた record / 名前の無い metric は黙って skip (tolerant)
        payload = {"data": {"metrics": [
            "not a dict",
            {"units": "count", "data": []},                  # name 無し
            {"name": "", "data": []},
            {"name": "resting_heart_rate", "units": "count/min"},   # data 無し = record 0 件
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
        self.assertIsNone(result.health.heart_rate_resting)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.failed_metrics, ())

    def test_metric_whose_data_is_not_a_list_is_an_error(self):
        # 名前のある metric の data が配列でない = 書式変更。黙って None にせず報告する
        payload = _payload(
            {"name": "heart_rate", "units": "count/min", "data": "oops"},
            _metric("step_count", "count", _rec("2030-01-15 10:00:00 +0900", qty=3)),
        )
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = _parse(payload)
        self.assertIsNone(result.health.heart_rate_avg)
        self.assertEqual(result.health.step_count, 3)
        self.assertEqual(result.failed_metrics, ("heart_rate",))
        self.assertIn("配列ではありません", result.errors[0])

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
        payload = _payload(_metric("walking_running_distance", "furlong",
                                   _rec("2030-01-15 10:00:00 +0900", qty=3.0)))
        with self.assertLogs(_LOGGER, level="WARNING") as logs:
            result = _parse(payload)
        self.assertIsNone(result.health.distance_km)
        self.assertIn("未対応", logs.output[0])
        # 黙って「データなし」にせずエラーとして報告する (status が partial / failed になる)
        self.assertEqual(result.failed_metrics, ("walking_running_distance",))
        self.assertIn("furlong", result.errors[0])

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

    def test_unknown_weight_unit_is_an_error(self):
        payload = _payload(_metric("weight_body_mass", "st",
                                   _rec("2030-01-15 07:00:00 +0900", qty=9.5)))
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = _parse(payload)
        self.assertIsNone(result.health.body_mass_kg)
        self.assertEqual(result.failed_metrics, ("weight_body_mass",))

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
                                   _rec("2030-01-15 21:00:00 +0900", qty=3.4)),
                           workouts=[])
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
        # 0.4 分の瞑想は「瞑想0分」と書かない。心拍のデータが無い (Watch を着けていない) 日は
        # 「なし」と断定せず書かない (旧: "なし"。WAVE3 W3-7 で意図的に変更)
        payload = _payload(_metric("mindful_minutes", "min",
                                   _rec("2030-01-15 06:00:00 +0900", qty=0.4)),
                           workouts=[])
        result = _parse(payload)
        self.assertEqual(result.health.mindful_sessions, 1)
        self.assertAlmostEqual(result.health.mindful_minutes, 0.4)
        self.assertIsNone(result.activity_detail)
        # 心拍がある (Watch を着けていた) 日なら「なし」(W3-7 改訂。旧: エクササイズ時間 0 分の
        # record がある日だけ「なし」。実際の HAE は 0 分の record を出さない)
        payload["data"]["metrics"].append(
            _metric("heart_rate", "count/min", _rec("2030-01-15 09:00:00 +0900", Avg=70, Max=80)),
        )
        self.assertEqual(_parse(payload).activity_detail, ACTIVITY_NONE)

    def test_no_mindful_but_other_metrics(self):
        # 瞑想もワークアウトも無い日: 心拍が無ければ None (旧: "なし")。W3-7 改訂: 「なし」の根拠は
        # エクササイズ時間 0 分の record ではなく心拍 (旧: 0 分の record があれば「なし」。
        # WAVE3 W3-7 改訂で意図的に変更)
        payload = _payload(_metric("step_count", "count", _rec("2030-01-15 06:00:00 +0900", qty=5)),
                           workouts=[])
        result = _parse(payload)
        self.assertIsNone(result.health.mindful_sessions)
        self.assertIsNone(result.health.mindful_minutes)
        self.assertIsNone(result.activity_detail)
        payload["data"]["metrics"].append(
            _metric("apple_exercise_time", "min", _rec("2030-01-15 09:00:00 +0900", qty=0)),
        )
        self.assertIsNone(_parse(payload).activity_detail)   # 0 分の record だけでは書かない
        payload["data"]["metrics"].append(
            _metric("heart_rate", "count/min", _rec("2030-01-15 09:00:00 +0900", Avg=70, Max=80)),
        )
        self.assertEqual(_parse(payload).activity_detail, ACTIVITY_NONE)

    def test_without_workouts_key_uses_exercise_time(self):
        # ワークアウトを出力しない設定 (data.workouts が無い): W3-7 でアクティビティ詳細は
        # 瞑想 + エクササイズ時間から組む (旧: 一律 None。WAVE3 W3-7 で意図的に変更)。
        # 瞑想も運動も心拍も無い日は従来どおり書かない (None)
        cases = (
            ((_metric("step_count", "count", _rec("2030-01-15 06:00:00 +0900", qty=5)),), None),
            ((_metric("mindful_minutes", "min", _rec("2030-01-15 06:00:00 +0900", qty=24.0)),
              _metric("apple_exercise_time", "min", _rec("2030-01-15 07:00:00 +0900", qty=21.0))),
             "瞑想24分、エクササイズ21分"),
        )
        for metrics, expected in cases:
            with self.subTest(metrics=[m["name"] for m in metrics]):
                result = _parse(_payload(*metrics))
                self.assertEqual(result.activity_detail, expected)
                self.assertIsNone(result.workouts)
                self.assertFalse(result.workouts_included)
                self.assertEqual(result.errors, ())
                self.assertEqual(extract_workouts(_payload(*metrics), D), [])


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
        # 壊れたワークアウト (dict でない / 日時なし / 時間を求められない) は数えない。
        # 何も残らない日は、心拍がある (Watch を着けていた) なら「なし」、無ければ None
        # (旧: 常に "なし" → W3-7: エクササイズ時間 0 分の record があれば「なし」→
        # W3-7 改訂: 心拍があれば「なし」。いずれも意図的に変更)
        workouts = ["x", {"name": "Yoga"}, {"name": "Yoga", "start": "2030-01-15 07:00:00 +0900"}]
        payload = _payload(
            _metric("step_count", "count", _rec("2030-01-15 10:00:00 +0900", qty=1)),
            workouts=workouts,
        )
        result = _parse(payload)
        self.assertEqual(result.workouts, ())
        self.assertEqual(result.errors, ())
        self.assertIsNone(result.activity_detail)
        worn = _payload(
            _metric("step_count", "count", _rec("2030-01-15 10:00:00 +0900", qty=1)),
            _metric("heart_rate", "count/min", _rec("2030-01-15 10:00:00 +0900", Avg=70, Max=80)),
            workouts=workouts,
        )
        self.assertEqual(_parse(worn).activity_detail, ACTIVITY_NONE)

    def test_exported_workouts_take_the_slot_of_exercise_time(self):
        # ワークアウトを出力する設定では、ワークアウトの欄はワークアウトだけ
        # (エクササイズ時間 35 分は「エクササイズ35分」として足さない)
        payload = _payload(
            _metric("apple_exercise_time", "min", _rec("2030-01-15 08:00:00 +0900", qty=35)),
            workouts=[{"name": "Outdoor Walk", "start": "2030-01-15 08:00:00 +0900",
                       "duration": 1200}],
        )
        self.assertEqual(_parse(payload).activity_detail, "ウォーキング20分")


class TestComposeActivityDetail(unittest.TestCase):
    """W3-7 改訂の規則 (純関数)。旧 build_activity_detail (何も無ければ常に "なし") の置き換え。

    1. 瞑想 (round ≥ 1) → 2. 当日のワークアウトがあればその一覧、無ければ (出力しない設定 /
    当日 0 件) エクササイズ時間 (round ≥ 1) → 何も無い日は心拍があれば「なし」、無ければ None。
    結果が依存する入力が不明なら None。
    """

    def test_mindful_and_exercise_without_workouts(self):
        # 瞑想と運動の両方 / 運動だけ / 瞑想だけ (ワークアウトを出力しない設定)
        self.assertEqual(compose_activity_detail(3.4, None, 21.0), "瞑想3分、エクササイズ21分")
        self.assertEqual(compose_activity_detail(None, None, 1.0), "エクササイズ1分")
        # 運動のデータが無くても瞑想はある → 瞑想だけ書く
        self.assertEqual(compose_activity_detail(3.4, None, None), "瞑想3分")
        # 書く項目がある日は、心拍があっても「なし」にしない
        self.assertEqual(
            compose_activity_detail(3.4, None, None, heart_rate_present=True), "瞑想3分",
        )
        self.assertEqual(
            compose_activity_detail(None, None, 21.0, heart_rate_present=True), "エクササイズ21分",
        )

    def test_workouts_are_listed_when_exported(self):
        # 当日のワークアウトがある → その一覧 (エクササイズ時間 50 分は足さない)
        self.assertEqual(compose_activity_detail(None, [("ヨガ", 20.2)], None), "ヨガ20分")
        self.assertEqual(
            compose_activity_detail(6.0, [("ウォーキング", 35.0), ("ヨガ", 0.2)], 50.0),
            "瞑想6分、ウォーキング35分",
        )

    def test_exported_without_workouts_that_day_uses_exercise_time(self):
        # 出力していて当日 0 件 → 出力しない設定と同じくエクササイズ時間で組む
        # (旧: ワークアウトの欄は空のまま "瞑想6分" / None。WAVE3 W3-7 改訂で意図的に変更)
        self.assertEqual(compose_activity_detail(6.0, [], 50.0), "瞑想6分、エクササイズ50分")
        self.assertEqual(compose_activity_detail(None, [], 15.0), "エクササイズ15分")
        # 1 分未満 (round で 0 分) のワークアウトしか無い日も当日 0 件と同じ
        self.assertEqual(compose_activity_detail(None, [("ヨガ", 0.3)], 15.0), "エクササイズ15分")

    def test_nashi_only_with_heart_rate(self):
        # 何も無い日: 心拍がある (Watch を着けていた) なら「なし」、無ければ書かない。
        # エクササイズ時間 0 分の record があっても心拍が無ければ書かない
        # (旧: 0 分の record がある日だけ「なし」。WAVE3 W3-7 改訂で意図的に変更)
        for args in ((None, None, None), (None, None, 0.0), (0.4, None, 0.4), (None, [], None),
                     (None, [], 0.0), (None, [("ヨガ", 0.3)], 0.2)):
            with self.subTest(args=args):
                self.assertEqual(
                    compose_activity_detail(*args, heart_rate_present=True), ACTIVITY_NONE,
                )
                self.assertIsNone(compose_activity_detail(*args, heart_rate_present=False))
                self.assertIsNone(compose_activity_detail(*args))   # 既定は「なし」を書かない側

    def test_rounding_matches_written_minutes(self):
        # 日記の 瞑想時間 (分) / アクティビティ時間 (分) と同じ int(round(x))
        # (Python の round は偶数丸め: 2.5 → 2, 1.5 → 2, 0.5 → 0)
        self.assertEqual(compose_activity_detail(2.5, None, 2.5), "瞑想2分、エクササイズ2分")
        self.assertEqual(compose_activity_detail(1.5, None, 0.5), "瞑想2分")
        self.assertEqual(compose_activity_detail(None, [("ヨガ", 0.5)], 1.5), "エクササイズ2分")
        self.assertEqual(
            compose_activity_detail(None, None, 0.5, heart_rate_present=True), ACTIVITY_NONE,
        )

    def test_unknown_inputs(self):
        # 結果が依存する入力が不明なら、心拍があっても書かない (誤った「なし」も書かない)
        for hr in (False, True):
            with self.subTest(heart_rate_present=hr):
                for kwargs in ({"mindful_known": False}, {"workouts_known": False}):
                    self.assertIsNone(compose_activity_detail(
                        3.0, None, 21.0, heart_rate_present=hr, **kwargs))
                    self.assertIsNone(compose_activity_detail(
                        None, None, None, heart_rate_present=hr, **kwargs))
                # ワークアウトを出力しない設定でエクササイズ時間が不明 → 欄そのものが不明
                self.assertIsNone(compose_activity_detail(
                    3.0, None, None, heart_rate_present=hr, exercise_known=False))
                self.assertIsNone(compose_activity_detail(
                    None, None, None, heart_rate_present=hr, exercise_known=False))
                # 出力していて当日 0 件 (1 分未満だけを含む) → エクササイズ時間で組むので書かない
                self.assertIsNone(compose_activity_detail(
                    None, [], 0.0, heart_rate_present=hr, exercise_known=False))
                self.assertIsNone(compose_activity_detail(
                    6.0, [("ヨガ", 0.3)], None, heart_rate_present=hr, exercise_known=False))
                # ワークアウトの一覧が取れていればエクササイズ時間は不要
                self.assertEqual(
                    compose_activity_detail(
                        3.0, [("ヨガ", 20.0)], None, heart_rate_present=hr, exercise_known=False,
                    ),
                    "瞑想3分、ヨガ20分",
                )


class TestActivityDetailFromPayload(unittest.TestCase):
    """W3-7: payload 経由の規則 (エクササイズ時間 / 解釈できない入力)。"""

    def test_exercise_only(self):
        payload = _payload(_metric(
            "apple_exercise_time", "min",
            *(_rec(f"2030-01-15 08:{m:02d}:00 +0900", qty=1) for m in range(21)),
        ))
        result = _parse(payload)
        self.assertAlmostEqual(result.health.exercise_minutes, 21.0)
        self.assertEqual(result.activity_detail, "エクササイズ21分")

    def test_exercise_of_other_days_is_not_counted(self):
        payload = _payload(_metric(
            "apple_exercise_time", "min",
            _rec("2030-01-14 23:59:00 +0900", qty=30),
            _rec("2030-01-15 21:42:00 +0900", qty=1),
        ))
        self.assertEqual(_parse(payload).activity_detail, "エクササイズ1分")

    def test_unknown_exercise_unit_is_not_claimed(self):
        payload = _payload(
            _metric("mindful_minutes", "min", _rec("2030-01-15 06:00:00 +0900", qty=5)),
            _metric("apple_exercise_time", "fortnight", _rec("2030-01-15 08:00:00 +0900", qty=1)),
        )
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = _parse(payload)
        self.assertEqual(result.failed_metrics, ("apple_exercise_time",))
        self.assertEqual(result.unknown_fields, ("exercise_minutes",))
        # 「瞑想5分」だけ書くとエクササイズを取りこぼした一覧になる → 書かない
        self.assertIsNone(result.activity_detail)

    def test_exercise_dropped_by_validation_is_not_claimed(self):
        # 1 日 1440 分を超える (壊れた) エクササイズ時間は妥当性違反で落とす → 心拍がある
        # (Watch を着けていた) 日でも「なし」にしない
        payload = _payload(
            _metric("apple_exercise_time", "min", _rec("2030-01-15 08:00:00 +0900", qty=2000)),
            _metric("heart_rate", "count/min", _rec("2030-01-15 08:00:00 +0900", Avg=70, Max=80)),
        )
        with self.assertLogs("ihealth.automation_parser", level="WARNING"):
            result = parse_automation_day_detailed(payload, D, _ctx(D))
        self.assertIsNone(result.health.exercise_minutes)
        self.assertEqual(result.dropped_fields, ("exercise_minutes",))
        self.assertAlmostEqual(result.health.heart_rate_avg, 70.0)
        self.assertIsNone(result.activity_detail)

    def test_workouts_listed_even_if_exercise_unknown(self):
        payload = _payload(
            _metric("apple_exercise_time", "fortnight", _rec("2030-01-15 08:00:00 +0900", qty=1)),
            workouts=[{"name": "Yoga", "start": "2030-01-15 07:00:00 +0900", "duration": 1200}],
        )
        with self.assertLogs(_LOGGER, level="WARNING"):
            self.assertEqual(_parse(payload).activity_detail, "ヨガ20分")

    # --- W3-7 改訂: ワークアウト 0 件の日はエクササイズ時間、「なし」は心拍 (Watch を着けていた) ---

    def test_exported_workouts_without_one_that_day_use_exercise_time(self):
        # ワークアウトを書き出す設定でも、当日のワークアウトが無ければ (前日の分だけ)
        # エクササイズ時間で組む (旧: "瞑想4分"。WAVE3 W3-7 改訂で意図的に変更)
        payload = _payload(
            _metric("mindful_minutes", "min", _rec("2030-01-15 06:00:00 +0900", qty=4)),
            _metric("apple_exercise_time", "min", _rec("2030-01-15 08:00:00 +0900", qty=21)),
            workouts=[{"name": "Yoga", "start": "2030-01-14 07:00:00 +0900", "duration": 1200}],
        )
        result = _parse(payload)
        self.assertTrue(result.workouts_included)
        self.assertEqual(result.workouts, ())
        self.assertEqual(result.activity_detail, "瞑想4分、エクササイズ21分")

    def test_heart_rate_without_exercise_records_is_nashi(self):
        # 実際の HAE の運動 0 分の日: apple_exercise_time 自体が出力されず、心拍はある
        # → Watch を着けていて運動 0 分 = 「なし」(旧: 運動のデータが無いので None。
        # WAVE3 W3-7 改訂で意図的に変更)
        payload = _payload(
            _metric("step_count", "count", _rec("2030-01-15 10:00:00 +0900", qty=3000)),
            _metric("heart_rate", "count/min",
                    _rec("2030-01-15 03:00:00 +0900", Avg=55, Max=60),
                    _rec("2030-01-15 10:00:00 +0900", Avg=80, Max=95)),
        )
        result = _parse(payload)
        self.assertIsNone(result.health.exercise_minutes)
        self.assertEqual(result.health.exercise_intensity_score, 0.0)
        self.assertEqual(result.activity_detail, ACTIVITY_NONE)
        # 心拍の record が別の日のものだけ → 対象日は心拍が無い (Watch を着けていない) → 書かない
        other_day = _payload(
            _metric("step_count", "count", _rec("2030-01-15 10:00:00 +0900", qty=3000)),
            _metric("heart_rate", "count/min", _rec("2030-01-14 22:00:00 +0900", Avg=70, Max=80)),
        )
        self.assertIsNone(_parse(other_day).activity_detail)

    def test_unknown_inputs_do_not_claim_nashi_even_with_heart_rate(self):
        heart_rate = _metric(
            "heart_rate", "count/min", _rec("2030-01-15 10:00:00 +0900", Avg=70, Max=80),
        )
        cases = {
            "mindful_minutes": _payload(
                heart_rate,
                _metric("mindful_minutes", "minutes", _rec("2030-01-15 06:00:00 +0900", qty=5)),
            ),
            "apple_exercise_time": _payload(
                heart_rate,
                _metric("apple_exercise_time", "fortnight",
                        _rec("2030-01-15 08:00:00 +0900", qty=1)),
            ),
            WORKOUTS_LABEL: _payload(heart_rate, workouts=[{"name": "Yoga", "duration": 1200}]),
        }
        for failed, payload in cases.items():
            with self.subTest(failed=failed):
                with self.assertLogs(_LOGGER, level="WARNING"):
                    result = _parse(payload)
                self.assertEqual(result.failed_metrics, (failed,))
                self.assertAlmostEqual(result.health.heart_rate_avg, 70.0)
                self.assertIsNone(result.activity_detail)

    def test_uninterpretable_heart_rate_does_not_claim_nashi(self):
        # 心拍を解釈できない (書式変更) 日は Watch を着けていたか分からない → 「なし」と断定しない
        payload = _payload(
            _metric("step_count", "count", _rec("2030-01-15 10:00:00 +0900", qty=3000)),
            {"name": "heart_rate", "units": "count/min", "data": "oops"},
        )
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = _parse(payload)
        self.assertEqual(result.failed_metrics, ("heart_rate",))
        self.assertIsNone(result.activity_detail)


class TestUnknownFields(unittest.TestCase):
    def test_failed_metric_maps_to_its_fields(self):
        payload = _payload(
            {"name": "heart_rate", "units": "count/min", "data": "oops"},
            _metric("mindful_minutes", "minutes", _rec("2030-01-15 06:00:00 +0900", qty=10.0)),
        )
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = _parse(payload)
        self.assertEqual(
            result.unknown_fields,
            ("exercise_intensity_score", "heart_rate_avg", "heart_rate_max",
             "mindful_sessions", "mindful_minutes"),
        )

    def test_metric_fields_cover_every_health_field(self):
        covered = [name for fields in METRIC_FIELDS.values() for name in fields]
        self.assertEqual(sorted(covered), sorted(HEALTH_FIELDS))


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



class TestUninterpretableData(unittest.TestCase):
    """書式 / 単位のドリフトを「データなし」と取り違えず errors に残す。"""

    def test_unparseable_dates_are_errors_not_nodata(self):
        payload = _payload(
            _metric("step_count", "count", {"date": "2030-01-15 7:00:00 PM +0900", "qty": 10}),
            _metric("heart_rate", "count/min",
                    {"date": "2030-01-15 7:00:00 PM +0900", "Avg": 70, "Max": 80}),
        )
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = _parse(payload)
        self.assertTrue(all(getattr(result.health, n) is None for n in HEALTH_FIELDS))
        self.assertEqual(set(result.failed_metrics), {"step_count", "heart_rate"})
        self.assertTrue(all("日時" in error for error in result.errors))
        self.assertIsNone(result.activity_detail)

    def test_some_broken_records_are_still_skipped_silently(self):
        payload = _payload(_metric(
            "step_count", "count",
            {"date": "garbage", "qty": 1000},
            _rec("2030-01-15 10:00:00 +0900", qty=10),
        ))
        result = _parse(payload)
        self.assertEqual(result.health.step_count, 10)
        self.assertEqual(result.errors, ())

    def test_target_day_records_without_numeric_values_are_errors(self):
        payload = _payload(
            _metric("step_count", "count",
                    _rec("2030-01-15 10:00:00 +0900", qty="12"),
                    _rec("2030-01-15 10:01:00 +0900", value=3)),
            _metric("resting_heart_rate", "count/min",
                    _rec("2030-01-15 00:01:00 +0900", qty=None)),
            _metric("heart_rate", "count/min", _rec("2030-01-15 07:00:00 +0900", bpm=70)),
        )
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = _parse(payload)
        self.assertIsNone(result.health.step_count)
        self.assertIsNone(result.health.heart_rate_resting)
        self.assertIsNone(result.health.heart_rate_avg)
        self.assertEqual(
            set(result.failed_metrics), {"step_count", "resting_heart_rate", "heart_rate"},
        )

    def test_mindful_unknown_unit_does_not_claim_no_activity(self):
        payload = _payload(
            _metric("mindful_minutes", "minutes",
                    _rec("2030-01-15 06:00:00 +0900", qty=10.0),
                    _rec("2030-01-15 21:00:00 +0900", qty=5.0)),
            _metric("step_count", "count", _rec("2030-01-15 10:00:00 +0900", qty=100)),
        )
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = _parse(payload)
        self.assertIsNone(result.health.mindful_sessions)
        self.assertIsNone(result.health.mindful_minutes)
        # 「瞑想回数 2 / アクティビティ詳細 なし」のような矛盾を書かない
        self.assertIsNone(result.activity_detail)
        self.assertEqual(result.failed_metrics, ("mindful_minutes",))
        self.assertEqual(result.health.step_count, 100)

    def test_mindful_records_without_duration_are_errors(self):
        payload = _payload(_metric("mindful_minutes", "min", _rec("2030-01-15 06:00:00 +0900")))
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = _parse(payload)
        self.assertIsNone(result.health.mindful_sessions)
        self.assertEqual(result.failed_metrics, ("mindful_minutes",))
        self.assertIsNone(result.activity_detail)

    def test_aggregated_sleep_without_parseable_anchor_is_error(self):
        payload = _payload(_metric("sleep_analysis", "hr", {
            "date": "15/01/2030", "totalSleep": 7.0, "sleepEnd": "15/01/2030 06:00",
            "source": "Sample Watch",
        }))
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = _parse(payload)
        self.assertIsNone(result.health.sleep_hours)
        self.assertEqual(result.failed_metrics, ("sleep_analysis",))

    def test_sleep_records_of_unknown_shape_are_error(self):
        payload = _payload(_metric("sleep_analysis", "hr",
                                   _rec("2030-01-15 06:00:00 +0900", qty=7.0)))
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = _parse(payload)
        self.assertIsNone(result.health.sleep_hours)
        self.assertEqual(result.failed_metrics, ("sleep_analysis",))

    def test_unknown_sleep_unit_is_error(self):
        payload = _payload(_metric("sleep_analysis", "fortnight", _rec(
            "2030-01-15 00:00:00 +0900", totalSleep=7.0, sleepEnd="2030-01-15 06:00:00 +0900",
        )))
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = _parse(payload)
        self.assertIsNone(result.health.sleep_hours)
        self.assertEqual(result.failed_metrics, ("sleep_analysis",))

    def test_undated_workouts_make_activity_unknown(self):
        payload = _payload(
            _metric("step_count", "count", _rec("2030-01-15 10:00:00 +0900", qty=100)),
            workouts=[{"name": "Yoga", "duration": 1200}],
        )
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = _parse(payload)
        self.assertEqual(result.failed_metrics, (WORKOUTS_LABEL,))
        self.assertIsNone(result.workouts)
        self.assertIsNone(result.activity_detail)
        self.assertEqual(result.health.step_count, 100)
        # 公開関数は日付を判定できないワークアウトをどの日にも数えない
        self.assertEqual(extract_workouts(payload, D), [])


class TestEdgeRules(unittest.TestCase):
    """境界・分岐の固定 (変異テストで素通りした箇所)。"""

    def test_oxygen_fraction_is_converted_per_value(self):
        payload = _payload(_metric("blood_oxygen_saturation", "%",
                                   _rec("2030-01-15 01:00:00 +0900", qty=0.97),
                                   _rec("2030-01-15 02:00:00 +0900", qty=99)))
        self.assertAlmostEqual(_parse(payload).health.oxygen_saturation, 98.0)

    def test_oxygen_exactly_one_is_a_fraction(self):
        payload = _payload(_metric("blood_oxygen_saturation", "%",
                                   _rec("2030-01-15 01:00:00 +0900", qty=1.0)))
        self.assertAlmostEqual(_parse(payload).health.oxygen_saturation, 100.0)

    def test_body_fat_exactly_one_is_a_fraction(self):
        # 1.0 は 100% と解釈 → 妥当範囲 (<= 60%) 外として落とす (1% とは読まない)
        payload = _payload(_metric("body_fat_percentage", "%",
                                   _rec("2030-01-15 07:00:00 +0900", qty=1.0)))
        with self.assertLogs("ihealth.automation_parser", level="WARNING"):
            result = parse_automation_day_detailed(payload, D, _ctx(D))
        self.assertIsNone(result.health.body_fat_percentage)
        self.assertEqual(result.dropped_fields, ("body_fat_percentage",))

    def test_zero_mindful_total_is_none_not_zero(self):
        payload = _payload(_metric("mindful_minutes", "min",
                                   _rec("2030-01-15 06:00:00 +0900", qty=0.0)),
                           workouts=[])
        result = _parse(payload)
        self.assertEqual(result.health.mindful_sessions, 1)
        self.assertIsNone(result.health.mindful_minutes)
        # 心拍のデータが無い (Watch を着けていない) ので「なし」と断定しない
        # (旧: "なし"。WAVE3 W3-7 で意図的に変更)
        self.assertIsNone(result.activity_detail)

    def test_workout_belongs_to_its_start_date(self):
        # 前日 23:30 開始のヨガは前日の分。当日はワークアウト 0 件 → エクササイズ時間で組むが
        # 当日分は 0 分 (前日 23:30 の 30 分は前日の暦日) → 何も無く、心拍がある (Watch を
        # 着けていた) ので「なし」(W3-7 改訂: 「なし」の根拠は心拍。旧: 0 分の record)
        payload = _payload(
            _metric("step_count", "count",
                    _rec("2030-01-14 10:00:00 +0900", qty=1),
                    _rec("2030-01-15 10:00:00 +0900", qty=1)),
            _metric("apple_exercise_time", "min",
                    _rec("2030-01-14 23:30:00 +0900", qty=30),
                    _rec("2030-01-15 10:00:00 +0900", qty=0)),
            _metric("heart_rate", "count/min",
                    _rec("2030-01-14 23:40:00 +0900", Avg=110, Max=130),
                    _rec("2030-01-15 10:00:00 +0900", Avg=70, Max=80)),
            workouts=[{"name": "Yoga", "start": "2030-01-14 23:30:00 +0900",
                       "end": "2030-01-15 00:30:00 +0900", "duration": 3600}],
        )
        self.assertEqual(_parse(payload).activity_detail, ACTIVITY_NONE)
        prev_day = date(2030, 1, 14)
        self.assertEqual(_parse(payload, prev_day).activity_detail, "ヨガ60分")


def _seg(start: str, end: str, value: str = "Core", source: str = "Sample Watch",
         **extra: object) -> dict:
    return {"startDate": start, "endDate": end, "value": value, "source": source, **extra}


class TestSegmentClassification(unittest.TestCase):
    """非集計 segment: 夜主睡眠 / 昼寝は segment ごとの終了時刻で分類する。

    セッション (間隔 2h 以内) は 0 時前に終わる前半を翌朝へ繰り越す判定にだけ使い、
    後続の segment が先行 segment の分類を変えない。
    """

    DAY = date(2030, 2, 2)
    PREV = date(2030, 2, 1)

    def _parse_segments(self, records, target: date, prev_records=None):
        payload = _payload(_metric("sleep_analysis", "hr", *records))
        prev = (_payload(_metric("sleep_analysis", "hr", *prev_records))
                if prev_records is not None else None)
        return parse_automation_day_detailed(
            payload, target, _ctx(target), prev_payload=prev, logger=_LOGGER,
        )

    def test_nap_window_ends_at_20h(self):
        # SPEC §2: end.hour ∈ [14, 20) → 昼寝。19:59 に終わる睡眠は昼寝、20:xx に終わる睡眠は
        # (翌朝の夜主睡眠に繋がらなければ) どの日にも数えない
        nap = self._parse_segments([
            _seg("2030-02-02 19:00:00 +0900", "2030-02-02 19:59:00 +0900"),
        ], self.DAY)
        self.assertAlmostEqual(nap.health.nap_hours, 59 / 60)
        self.assertIsNone(nap.health.sleep_hours)
        evening = self._parse_segments([
            _seg("2030-02-02 19:30:00 +0900", "2030-02-02 20:00:00 +0900"),
            _seg("2030-02-02 20:05:00 +0900", "2030-02-02 20:59:00 +0900"),
        ], self.DAY)
        self.assertIsNone(evening.health.nap_hours)
        self.assertIsNone(evening.health.sleep_hours)

    def test_main_sleep_window_ends_at_14h(self):
        # end.hour ∈ [0, 14) → 夜主睡眠 (13:59 終了)、14:00 終了は昼寝
        main = self._parse_segments([
            _seg("2030-02-02 06:00:00 +0900", "2030-02-02 13:59:00 +0900"),
        ], self.DAY)
        self.assertAlmostEqual(main.health.sleep_hours, 7 + 59 / 60)
        self.assertIsNone(main.health.nap_hours)
        afternoon = self._parse_segments([
            _seg("2030-02-02 13:00:00 +0900", "2030-02-02 14:00:00 +0900"),
        ], self.DAY)
        self.assertAlmostEqual(afternoon.health.nap_hours, 1.0)
        self.assertIsNone(afternoon.health.sleep_hours)

    def test_long_morning_sleep_then_nap_is_not_merged_into_a_nap(self):
        result = self._parse_segments([
            _seg("2030-02-02 01:00:00 +0900", "2030-02-02 12:00:00 +0900"),
            _seg("2030-02-02 13:30:00 +0900", "2030-02-02 14:15:00 +0900"),
        ], self.DAY)
        self.assertAlmostEqual(result.health.sleep_hours, 11.0)
        self.assertAlmostEqual(result.health.nap_hours, 0.75)

    def test_awake_bridged_morning_then_afternoon_nap(self):
        result = self._parse_segments([
            _seg("2030-02-02 03:00:00 +0900", "2030-02-02 07:00:00 +0900"),
            _seg("2030-02-02 07:00:00 +0900", "2030-02-02 07:10:00 +0900", "Awake"),
            _seg("2030-02-02 07:10:00 +0900", "2030-02-02 13:30:00 +0900"),
            _seg("2030-02-02 15:00:00 +0900", "2030-02-02 16:00:00 +0900"),
        ], self.DAY)
        self.assertAlmostEqual(result.health.sleep_hours, 4.0 + 6 + 20 / 60)
        self.assertAlmostEqual(result.health.nap_hours, 1.0)

    def test_late_main_sleep_and_nap_after_short_gap(self):
        result = self._parse_segments([
            _seg("2030-02-02 03:00:00 +0900", "2030-02-02 12:30:00 +0900"),
            _seg("2030-02-02 14:00:00 +0900", "2030-02-02 15:00:00 +0900"),
        ], self.DAY)
        self.assertAlmostEqual(result.health.sleep_hours, 9.5)
        self.assertAlmostEqual(result.health.nap_hours, 1.0)

    def test_previous_day_nap_is_not_absorbed_into_the_next_night(self):
        records = [
            _seg("2030-02-01 15:00:00 +0900", "2030-02-01 16:00:00 +0900"),
            _seg("2030-02-01 17:30:00 +0900", "2030-02-02 06:00:00 +0900"),
        ]
        prev_day = self._parse_segments(records, self.PREV)
        self.assertAlmostEqual(prev_day.health.nap_hours, 1.0)
        self.assertIsNone(prev_day.health.sleep_hours)
        day = self._parse_segments(records, self.DAY)
        self.assertAlmostEqual(day.health.sleep_hours, 12.5)
        self.assertIsNone(day.health.nap_hours)

    def test_pre_midnight_part_is_carried_to_the_wake_up_day(self):
        records = [
            _seg("2030-02-01 22:00:00 +0900", "2030-02-01 23:30:00 +0900"),
            _seg("2030-02-01 23:30:00 +0900", "2030-02-01 23:50:00 +0900", "Awake"),
            _seg("2030-02-01 23:50:00 +0900", "2030-02-02 06:00:00 +0900", "Deep"),
        ]
        day = self._parse_segments(records, self.DAY)
        self.assertAlmostEqual(day.health.sleep_hours, 1.5 + 6 + 10 / 60)
        self.assertIsNone(self._parse_segments(records, self.PREV).health.sleep_hours)

    def test_main_sleep_window_flags(self):
        near_midnight = self._parse_segments([
            _seg("2030-02-02 00:30:00 +0900", "2030-02-02 06:00:00 +0900"),
        ], self.DAY)
        self.assertTrue(near_midnight.main_sleep_needs_prev)
        self.assertEqual(near_midnight.main_sleep_end,
                         parse_hae_datetime("2030-02-02 06:00:00 +0900"))
        late_start = self._parse_segments([
            _seg("2030-02-02 03:00:00 +0900", "2030-02-02 09:00:00 +0900"),
        ], self.DAY)
        self.assertFalse(late_start.main_sleep_needs_prev)
        self.assertAlmostEqual(late_start.health.sleep_hours, 6.0)
        # 0 時台の Awake で始まるセッションも前日の segment が繋がりうる
        awake_first = self._parse_segments([
            _seg("2030-02-02 00:10:00 +0900", "2030-02-02 02:30:00 +0900", "Awake"),
            _seg("2030-02-02 02:30:00 +0900", "2030-02-02 07:00:00 +0900"),
        ], self.DAY)
        self.assertTrue(awake_first.main_sleep_needs_prev)
        nap_only = self._parse_segments([
            _seg("2030-02-02 15:00:00 +0900", "2030-02-02 16:00:00 +0900"),
        ], self.DAY)
        self.assertFalse(nap_only.main_sleep_needs_prev)
        self.assertIsNone(nap_only.main_sleep_end)

    def test_switch_day_from_aggregated_to_segments(self):
        # 前日ファイルは aggregateSleep=true (翌日付の途中経過レコードも含む)、当日から segment
        prev_records = [
            _rec("2030-02-01 00:00:00 +0900", totalSleep=7.0,
                 sleepStart="2030-01-31 23:00:00 +0900", sleepEnd="2030-02-01 06:00:00 +0900"),
            _rec("2030-02-02 00:00:00 +0900", totalSleep=0.5,
                 sleepStart="2030-02-01 17:30:00 +0900", sleepEnd="2030-02-01 18:00:00 +0900"),
        ]
        result = self._parse_segments([
            _seg("2030-02-02 00:30:00 +0900", "2030-02-02 06:30:00 +0900"),
        ], self.DAY, prev_records=prev_records)
        self.assertEqual(result.sleep_mode, SLEEP_MODE_SEGMENTS)
        self.assertAlmostEqual(result.health.sleep_hours, 6.0)

    def test_previous_day_aggregated_record_is_never_used_for_the_day(self):
        # 当日ファイルに睡眠が無い日、前日ファイルの「翌日付」集計レコード (途中経過) は使わない
        payload = _payload(_metric("step_count", "count", _rec("2030-02-02 10:00:00 +0900", qty=5)))
        prev = _payload(_metric("sleep_analysis", "hr", _rec(
            "2030-02-02 00:00:00 +0900", totalSleep=0.5,
            sleepStart="2030-02-01 17:30:00 +0900", sleepEnd="2030-02-01 18:00:00 +0900",
        )))
        result = parse_automation_day_detailed(
            payload, self.DAY, _ctx(self.DAY), prev_payload=prev, logger=_LOGGER,
        )
        self.assertIsNone(result.health.sleep_hours)
        self.assertIsNone(result.sleep_mode)

    def test_night_only_in_previous_day_file(self):
        # 当日ファイルに睡眠 metric が無く、夜全体が前日ファイルの 1 segment に入っている
        payload = _payload(_metric("step_count", "count", _rec("2030-02-02 10:00:00 +0900", qty=5)))
        self.assertTrue(needs_prev_payload(payload))
        prev = _payload(_metric("sleep_analysis", "hr", _seg(
            "2030-02-01 22:00:00 +0900", "2030-02-02 06:00:00 +0900", "Asleep",
        )))
        result = parse_automation_day_detailed(
            payload, self.DAY, _ctx(self.DAY), prev_payload=prev, logger=_LOGGER,
        )
        self.assertAlmostEqual(result.health.sleep_hours, 8.0)
        self.assertTrue(result.main_sleep_needs_prev)


class TestCandidateHelpers(unittest.TestCase):
    def test_needs_prev_payload(self):
        aggregated = _payload(_metric("sleep_analysis", "hr", _rec(
            "2030-01-16 00:00:00 +0900", totalSleep=0.5, sleepEnd="2030-01-15 18:30:00 +0900",
        )))
        self.assertFalse(needs_prev_payload(aggregated))
        no_sleep = _payload(_metric("step_count", "count", _rec("2030-01-15 10:00:00 +0900", qty=1)))
        self.assertTrue(needs_prev_payload(no_sleep))
        segments = _payload(_metric("sleep_analysis", "hr", _seg(
            "2030-01-15 01:00:00 +0900", "2030-01-15 06:00:00 +0900",
        )))
        self.assertTrue(needs_prev_payload(segments))

    def test_has_data_for(self):
        other_day_only = _payload(_metric(
            "apple_sleeping_wrist_temperature", "degC",
            _rec("2030-01-14 22:30:00 +0900", qty=36.1),
        ))
        self.assertFalse(has_data_for(other_day_only, D))
        self.assertTrue(has_data_for(other_day_only, date(2030, 1, 14)))
        self.assertFalse(has_data_for({"data": {"metrics": []}}, D))
        self.assertFalse(has_data_for({}, D))
        # ワークアウトだけのファイルは metric を持つ候補を隠さない
        self.assertFalse(has_data_for(
            _payload(workouts=[{"name": "Yoga", "start": "2030-01-15 07:00:00 +0900"}]), D,
        ))
        # 日付をまたぐ segment は終了日のデータでもある
        self.assertTrue(has_data_for(_payload(_metric("sleep_analysis", "hr", _seg(
            "2030-01-14 23:00:00 +0900", "2030-01-15 01:00:00 +0900",
        ))), D))

    def test_has_data_for_treats_uninterpretable_as_maybe(self):
        # 日時を解釈できない record / 配列でない data は「ありうる」(集計側でエラー報告させる)
        self.assertTrue(has_data_for(_payload(_metric(
            "step_count", "count", {"date": "15/01/2030", "qty": 1},
        )), D))
        self.assertTrue(has_data_for(
            _payload({"name": "step_count", "units": "count", "data": "oops"}), D,
        ))


_TZ_JST = timezone(timedelta(hours=9))
_TZ_EST = timezone(timedelta(hours=-5))


class TestRecordDayEnd(unittest.TestCase):
    """日の終わり = 対象日の record 自身の offset の暦日の翌日 0:00 (W3-2h)。"""

    def test_uses_record_offset(self):
        payload = _payload(_metric("step_count", "count", _rec("2030-01-15 10:00:00 +0900", qty=1)))
        end = record_day_end(payload, D, _TZ_EST)   # 既定 tz より record の offset が優先
        self.assertEqual(end, datetime(2030, 1, 16, 0, 0, tzinfo=_TZ_JST))
        self.assertEqual(end.utcoffset(), timedelta(hours=9))

    def test_western_offset(self):
        payload = _payload(_metric("step_count", "count", _rec("2030-01-15 22:00:00 -0500", qty=1)))
        end = record_day_end(payload, D, _TZ_JST)
        self.assertEqual(end, datetime(2030, 1, 16, 0, 0, tzinfo=_TZ_EST))
        # 絶対時刻では JST の 1/16 14:00
        self.assertEqual(end.timestamp(), datetime(2030, 1, 16, 14, 0, tzinfo=_TZ_JST).timestamp())

    def test_mixed_offsets_take_the_latest_end(self):
        # 旅行で offset が混在 → どの offset の暦日でも終わった時点 (最も遅い終わり)
        payload = _payload(
            _metric("step_count", "count",
                    _rec("2030-01-15 08:00:00 +0900", qty=1),
                    _rec("2030-01-15 20:00:00 -0500", qty=1)),
        )
        self.assertEqual(record_day_end(payload, D, _TZ_JST),
                         datetime(2030, 1, 16, 0, 0, tzinfo=_TZ_EST))

    def test_records_of_other_days_are_ignored(self):
        payload = _payload(_metric(
            "step_count", "count",
            _rec("2030-01-14 23:59:00 -1000", qty=1),   # 前日の record (offset は使わない)
            _rec("2030-01-15 10:00:00 +0900", qty=1),
        ))
        self.assertEqual(record_day_end(payload, D, _TZ_EST),
                         datetime(2030, 1, 16, 0, 0, tzinfo=_TZ_JST))

    def test_sleep_and_workout_keys_are_considered(self):
        sleep = _payload(_metric("sleep_analysis", "hr", {
            "totalSleep": 7.0, "sleepStart": "2030-01-14 23:00:00 -0300",
            "sleepEnd": "2030-01-15 06:00:00 -0300", "source": "Sample Watch",
        }))
        self.assertEqual(record_day_end(sleep, D, _TZ_JST),
                         datetime(2030, 1, 16, 0, 0, tzinfo=timezone(timedelta(hours=-3))))
        workouts_only = _payload(workouts=[{"name": "Yoga", "start": "2030-01-15 07:00:00 -0800"}])
        self.assertEqual(record_day_end(workouts_only, D, _TZ_JST),
                         datetime(2030, 1, 16, 0, 0, tzinfo=timezone(timedelta(hours=-8))))

    def test_falls_back_to_default_tz(self):
        naive = _payload(_metric("step_count", "count", {"date": "2030-01-15 10:00:00", "qty": 1}))
        for payload in (naive, {"data": {"metrics": []}}, {}, None, {"data": "x"}):
            with self.subTest(payload=payload):
                end = record_day_end(payload, D, _TZ_EST)
                self.assertEqual(end, datetime(2030, 1, 16, 0, 0, tzinfo=_TZ_EST))
                self.assertIsNotNone(end.tzinfo)


if __name__ == "__main__":
    unittest.main()
