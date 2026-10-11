"""health_loader.py (オートメーション JSON 優先 + AutoSync 補完) のテスト。

実 iCloud / compression_tool は使わない:

* HAE Documents 風のツリーを tempdir に作る (オートメーション JSON は合成 fixture をコピー)
* AutoSync の ``.hae`` はダミー bytes を置くだけ (存在判定用)。解凍は
  ``metric_fetcher`` を差し替えた fake が「解凍済み JSON」を archive に書いて返す
* 現在時刻は ``now`` で注入する (既定は全 fixture の日付より後)。置くファイルの
  mtime は既定で「対象日の翌日 12:00」= 対象日の終了後に書かれた確定版
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from ihealth.automation import AutomationError, AutomationIndex, load_automation_json
from ihealth.automation_parser import HEALTH_FIELDS
from ihealth.config import Config
from ihealth.health_loader import (
    SOURCE_AUTOMATION,
    SOURCE_AUTOSYNC,
    SOURCE_NONE,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_NODATA,
    STATUS_PARTIAL,
    HealthLoader,
    HealthLoadResult,
    resolve_automation_root,
    resolve_autosync_dir,
)


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "automation"
_LOGGER = logging.getLogger("test_health_loader")
_MAC_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
_JST = timezone(timedelta(hours=9))

D = date(2030, 1, 15)          # aggregated fixture の日付
SEG_DAY = date(2030, 2, 2)     # segments fixture の日付


def _mac(d: date, hour: int, minute: int = 0) -> float:
    """JST の日時を Mac absolute time (AutoSync .hae のタイムスタンプ) にする。"""
    local = datetime(d.year, d.month, d.day, hour, minute, tzinfo=_JST)
    return (local - _MAC_EPOCH).total_seconds()


_FAIL = object()
_RAISE = object()

#: テストの既定の「現在時刻」(全 fixture の日付より後 = 対象日は終わっている)。
NOW = datetime(2030, 12, 31, 12, 0)


def _final_mtime(d: date) -> float:
    """``d`` の終了後 (翌日 12:00 ローカル) に書かれた = 確定版の mtime。"""
    return datetime.combine(d + timedelta(days=1), time(12, 0)).timestamp()


def _local(d: date, hour: int, minute: int = 0) -> float:
    return datetime.combine(d, time(hour, minute)).timestamp()


def _file_date(name: str) -> date:
    return date.fromisoformat(name[len("HealthAutoExport-"):len("HealthAutoExport-") + 10])


class FakeMetricFetcher:
    """``ihealth.source.fetch_metric_for_date`` の fake。

    ``payloads[(metric_dir, date)]`` を「解凍済み JSON」として archive に書いて path を返す。
    値が ``_FAIL`` なら解凍失敗 (None) を返し、``_RAISE`` なら OSError を投げる
    (archive への書き込み失敗などを模す)。
    """

    def __init__(self, payloads: "dict[tuple[str, date], object]") -> None:
        self.payloads = payloads
        self.calls: "list[tuple[str, date]]" = []

    def __call__(self, *, health_export_dir: Path, metric_dir_name: str, target_date: date,
                 archive_root: Path, logger: logging.Logger) -> "Path | None":
        self.calls.append((metric_dir_name, target_date))
        payload = self.payloads.get((metric_dir_name, target_date), _FAIL)
        if payload is _FAIL:
            return None
        if payload is _RAISE:
            raise OSError(28, "No space left on device")
        dst = archive_root / target_date.isoformat() / f"{metric_dir_name}.json"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(json.dumps(payload), encoding="utf-8")
        return dst


class RecordingJsonLoader:
    """``load_automation_json`` をそのまま呼びつつ、読んだ path を記録する。"""

    def __init__(self) -> None:
        self.paths: "list[Path]" = []

    def __call__(self, path: Path, logger: logging.Logger) -> dict:
        self.paths.append(path)
        return load_automation_json(path, logger)


class LoaderTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.docs = self.tmp / "Documents"
        self.docs.mkdir()
        self.archive = self.tmp / "archive"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # --- helpers ---

    def config(self, export_dir: "Path | None" = None) -> Config:
        return Config(
            notion_secret=None,
            database_id=None,
            health_export_dir=export_dir or self.docs,
            archive_root=self.archive,
            birth_date=date(1990, 6, 1),
        )

    def put_automation(self, fixture: str, folder: str = "新しいオートメーション",
                       *, mtime: "float | None" = None) -> Path:
        src = FIXTURES / fixture
        dst = self.docs / folder / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        stamp = mtime if mtime is not None else _final_mtime(_file_date(src.name))
        os.utime(dst, (stamp, stamp))
        return dst

    def write_automation(self, d: date, payload: object, folder: str = "新しいオートメーション",
                         *, text: "str | None" = None, mtime: "float | None" = None) -> Path:
        dst = self.docs / folder / f"HealthAutoExport-{d.isoformat()}.json"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text if text is not None else json.dumps(payload), encoding="utf-8")
        stamp = mtime if mtime is not None else _final_mtime(d)
        os.utime(dst, (stamp, stamp))
        return dst

    def put_hae(self, metric_dir: str, d: date, *, mtime: "float | None" = None) -> Path:
        name = d.strftime("%Y%m%d") + ".hae"
        path = self.docs / "AutoSync" / "HealthMetrics" / metric_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-lzfse")
        stamp = mtime if mtime is not None else _final_mtime(d)
        os.utime(path, (stamp, stamp))
        return path

    def loader(self, fetcher: "FakeMetricFetcher | None" = None, *,
               now: "datetime | None" = None, **kwargs) -> HealthLoader:
        if fetcher is not None:
            kwargs["metric_fetcher"] = fetcher
        current = now or NOW
        return HealthLoader(self.config(kwargs.pop("export_dir", None)), _LOGGER,
                            now=lambda: current, **kwargs)


class TestResolveDirectories(LoaderTestBase):
    def test_documents_root_with_autosync(self):
        (self.docs / "AutoSync" / "HealthMetrics").mkdir(parents=True)
        self.assertEqual(resolve_autosync_dir(self.docs), self.docs / "AutoSync")
        self.assertEqual(resolve_automation_root(self.docs), self.docs)

    def test_export_dir_is_autosync_itself(self):
        autosync = self.docs / "AutoSync"
        (autosync / "HealthMetrics").mkdir(parents=True)
        self.assertEqual(resolve_autosync_dir(autosync), autosync)
        # AutoSync を指していたら親 (Documents root) からオートメーションを探す
        self.assertEqual(resolve_automation_root(autosync), self.docs)

    def test_no_autosync(self):
        self.assertIsNone(resolve_autosync_dir(self.docs))
        self.assertEqual(resolve_automation_root(self.docs), self.docs)

    def test_autosync_folder_without_health_metrics_still_searches_parent(self):
        # AutoSync を止めて HealthMetrics を消した後も HEALTH_EXPORT_DIR=…/AutoSync のまま
        # (.env.example の既定値) だと、隣のオートメーションを探さずに全日 nodata を
        # 黙って返していた。フォルダ名 AutoSync でも親を探索 root にする
        autosync = self.docs / "AutoSync"
        (autosync / "Workouts").mkdir(parents=True)
        self.assertIsNone(resolve_autosync_dir(autosync))
        self.assertEqual(resolve_automation_root(autosync), self.docs)
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json", folder="MyAutomation")
        loader = self.loader(export_dir=autosync)
        self.assertEqual(loader.automation_root, self.docs)
        self.assertIsNone(loader.autosync_dir)
        result = loader.load(D)
        self.assertEqual(result.source, SOURCE_AUTOMATION)
        self.assertEqual(result.status, STATUS_COMPLETE)
        self.assertEqual(result.health.step_count, 501)

    def test_other_folder_names_are_searched_as_is(self):
        # AutoSync 以外の名前 (HealthMetrics も無い) は従来どおりそのフォルダを探索 root にする
        other = self.docs / "Exports"
        other.mkdir()
        self.assertEqual(resolve_automation_root(other), other)

    def test_loader_finds_automation_in_sibling_folder_when_pointed_at_autosync(self):
        autosync = self.docs / "AutoSync"
        (autosync / "HealthMetrics").mkdir(parents=True)
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json")
        loader = self.loader(export_dir=autosync)
        self.assertEqual(loader.automation_root, self.docs)
        self.assertEqual(loader.autosync_dir, autosync)
        result = loader.load(D)
        self.assertEqual(result.source, SOURCE_AUTOMATION)
        self.assertEqual(result.health.step_count, 501)


@unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root は権限を無視する")
class TestUnreadableDirectories(LoaderTestBase):
    """読めないディレクトリを「ファイルが無い = nodata」と取り違えない。"""

    def test_unlistable_root_is_failed_not_nodata(self):
        # stat はできるが一覧できない root (launchd の python が TCC で拒否された状況)
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json")
        self.docs.chmod(0o311)
        try:
            with self.assertLogs(_LOGGER, level="WARNING"):
                result = self.loader().load(D)
        finally:
            self.docs.chmod(0o755)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertTrue(any("探索" in error for error in result.errors))

    def test_untraversable_root_does_not_raise_from_constructor(self):
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json")
        self.docs.chmod(0)
        try:
            loader = self.loader()           # コンストラクタはファイルシステムに触れない
            with self.assertLogs(_LOGGER, level="WARNING"):
                result = loader.load(D)
        finally:
            self.docs.chmod(0o755)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertTrue(result.errors)

    def test_unreadable_automation_folder_is_failed(self):
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json", folder="auto")
        locked = self.docs / "auto"
        locked.chmod(0o300)
        try:
            with self.assertLogs(_LOGGER, level="WARNING"):
                result = self.loader().load(D)
        finally:
            locked.chmod(0o755)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertTrue(any("auto" in error for error in result.errors))


class TestIndex(LoaderTestBase):
    def test_index_is_discovered_lazily_once(self):
        calls = []

        def discover(root, *, max_depth, logger):
            calls.append((root, max_depth))
            return AutomationIndex()

        loader = self.loader(discover=discover)
        self.assertEqual(calls, [])
        loader.index()
        loader.index()
        loader.load(D)
        self.assertEqual(calls, [(self.docs, 4)])

    def test_injected_index_is_used(self):
        path = self.put_automation("aggregated/HealthAutoExport-2030-01-15.json", folder="x")
        index = AutomationIndex(files={D: (path,)})

        def discover(*args, **kwargs):  # pragma: no cover — 呼ばれてはいけない
            raise AssertionError("discover は呼ばれない")

        loader = self.loader(index=index, discover=discover)
        self.assertIs(loader.index(), index)
        self.assertEqual(loader.load(D).health.step_count, 501)


class TestAutomationPath(LoaderTestBase):
    def test_complete_day(self):
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json")
        result = self.loader().load(D)
        self.assertEqual(result.source, SOURCE_AUTOMATION)
        self.assertEqual(result.status, STATUS_COMPLETE)
        self.assertEqual(result.errors, ())
        # fixture (現役設定) はワークアウトを出力しない → アクティビティ詳細は判断できず書かない
        self.assertIsNone(result.activity_detail)
        self.assertIn("ワークアウト未出力", result.detail)
        h = result.health
        self.assertEqual(h.step_count, 501)
        self.assertAlmostEqual(h.active_energy_kcal, 300.0)
        self.assertAlmostEqual(h.sleep_hours, 7.0)
        # max_hr は config (Tanaka 式, 1990-06-01 生 → 39 歳 → 179.085) から: 60/100/140 → 0/1/2
        self.assertEqual(h.exercise_intensity_score, 3.0)
        # aggregated 睡眠は昼寝を分離できないので nap_hours だけ欠落 (AutoSync も無い)
        self.assertEqual(result.missing_fields, ("nap_hours",))
        self.assertEqual(result.supplemented_fields, ())
        self.assertIn("HealthAutoExport-2030-01-15.json", result.detail)

    def test_today_is_provisional(self):
        # 対象日がまだ終わっていない (今日) → その時点までの暫定値を返す (§4.6)
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json", mtime=_local(D, 21))
        result = self.loader(now=datetime.combine(D, time(22, 0))).load(D)
        self.assertTrue(result.provisional)
        self.assertEqual(result.status, STATUS_COMPLETE)
        self.assertEqual(result.health.step_count, 501)
        self.assertEqual(result.withheld_fields, ())

    def test_ended_day_from_file_written_before_day_end_is_withheld(self):
        # 対象日は終わったのに、ファイルは 21:00 の途中経過のまま (HAE が未再出力)
        # → 過少の恐れがある値は書かない (既存値を保持)。想定内の遅延なので通知不要 = nodata
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json", mtime=_local(D, 21))
        result = self.loader(now=datetime.combine(D + timedelta(days=1), time(7, 0))).load(D)
        self.assertIsNone(result.health)
        self.assertIsNone(result.activity_detail)
        self.assertEqual(result.status, STATUS_NODATA)
        self.assertFalse(result.provisional)
        self.assertIn("step_count", result.withheld_fields)
        self.assertIn("sleep_hours", result.withheld_fields)
        self.assertNotIn("nap_hours", result.withheld_fields)   # 元から値が無い
        self.assertIn("途中まで", result.detail)

    def test_not_provisional_when_written_after_day_end(self):
        late = datetime.combine(D + timedelta(days=1), time(7, 0)).timestamp()
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json", mtime=late)
        result = self.loader().load(D)
        self.assertFalse(result.provisional)
        self.assertEqual(result.status, STATUS_COMPLETE)
        self.assertEqual(result.withheld_fields, ())

    def test_broken_best_candidate_falls_back_to_next(self):
        good = self.put_automation("aggregated/HealthAutoExport-2030-01-15.json",
                                   folder="旧オートメーション", mtime=1_900_000_000)
        broken = self.write_automation(D, None, folder="新オートメーション",
                                       text='{"data": {"metrics": [', mtime=1_900_100_000)
        loader = self.loader()
        self.assertEqual(loader.index().candidates(D), (broken, good))
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = loader.load(D)
        self.assertEqual(result.source, SOURCE_AUTOMATION)
        self.assertEqual(result.health.step_count, 501)
        # 最良候補が読めなかった → 取れた値は確実だが partial
        self.assertEqual(result.status, STATUS_PARTIAL)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("新オートメーション", str(broken))
        self.assertIn(broken.name, result.errors[0])

    def test_stale_older_candidate_after_broken_best_is_not_used(self):
        # 最良候補 (新しい再出力) が読めず、残る候補は対象日の途中 (08:46) に書かれた古い出力
        # → 既存の正しい値を古い途中経過で上書きしないよう値を返さない (failed で通知)
        self.write_automation(D, None, folder="新オートメーション", text="{",
                              mtime=_final_mtime(D + timedelta(days=1)))
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json",
                            folder="旧オートメーション", mtime=_local(D, 8, 46))
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader().load(D)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertIsNone(result.health)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("古い候補のため不採用", result.detail)

    def test_no_fallback_to_older_candidate_for_today(self):
        # 今日の分は全候補が途中経過 → 読めなかった新しい候補より古い値は使わない
        self.write_automation(D, None, folder="新オートメーション", text="{",
                              mtime=_local(D, 20))
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json",
                            folder="旧オートメーション", mtime=_local(D, 9))
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(now=datetime.combine(D, time(21, 0))).load(D)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertIsNone(result.health)

    def test_candidate_without_target_day_data_does_not_shadow_older_one(self):
        # 新しい候補に対象日の record が無い (前日付の metric だけ) → 古い候補を使う
        self.write_automation(D, {"data": {"metrics": [{
            "name": "apple_sleeping_wrist_temperature", "units": "degC",
            "data": [{"date": "2030-01-14 22:30:00 +0900", "qty": 36.1}]}]}},
            folder="new", mtime=_final_mtime(D) + 3600)
        self.write_automation(D, {"data": {"metrics": [{
            "name": "step_count", "units": "count",
            "data": [{"date": "2030-01-15 10:00:00 +0900", "qty": 5000}]}]}},
            folder="old", mtime=_final_mtime(D))
        result = self.loader().load(D)
        self.assertEqual(result.status, STATUS_COMPLETE)
        self.assertEqual(result.health.step_count, 5000)

    def test_unparseable_dates_are_failed_not_nodata(self):
        # 日時の書式が変わった (12 時間表記など) → 黙って nodata にせず failed で通知する
        self.write_automation(D, {"data": {"metrics": [
            {"name": "step_count", "units": "count",
             "data": [{"date": "2030-01-15 7:00:00 PM +0900", "qty": 10}]},
        ]}})
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader().load(D)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertIsNone(result.health)
        self.assertTrue(any("step_count" in error for error in result.errors))

    def test_uninterpretable_mindful_is_partial_without_activity(self):
        self.write_automation(D, {"data": {"metrics": [
            {"name": "step_count", "units": "count",
             "data": [{"date": "2030-01-15 10:00:00 +0900", "qty": 100}]},
            {"name": "mindful_minutes", "units": "minutes",
             "data": [{"date": "2030-01-15 06:00:00 +0900", "qty": 10}]},
        ]}})
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader().load(D)
        self.assertEqual(result.status, STATUS_PARTIAL)
        self.assertEqual(result.health.step_count, 100)
        self.assertIsNone(result.health.mindful_sessions)
        self.assertIsNone(result.activity_detail)

    def test_all_candidates_broken_without_autosync_is_failed(self):
        self.write_automation(D, None, text="not json")
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader().load(D)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertEqual(result.source, SOURCE_AUTOMATION)
        self.assertIsNone(result.health)
        self.assertIsNone(result.activity_detail)
        self.assertEqual(result.missing_fields, HEALTH_FIELDS)
        self.assertEqual(len(result.errors), 1)

    def test_materialize_failure_is_failed_not_raised(self):
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json")

        def failing_loader(path, logger):
            raise AutomationError("materialize に失敗しました: timeout")

        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(json_loader=failing_loader).load(D)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertIn("materialize", result.errors[0])

    def test_nodata_when_nothing_exists(self):
        result = self.loader().load(D)
        self.assertEqual(result.status, STATUS_NODATA)
        self.assertEqual(result.source, SOURCE_NONE)
        self.assertIsNone(result.health)
        self.assertIsNone(result.activity_detail)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.missing_fields, HEALTH_FIELDS)

    def test_file_without_metrics_is_nodata(self):
        self.write_automation(D, {"data": {"metrics": []}})
        result = self.loader().load(D)
        self.assertEqual(result.status, STATUS_NODATA)
        self.assertIsNone(result.health)
        self.assertIn("metric", result.detail)

    def test_file_without_metrics_falls_through_to_next_candidate(self):
        self.write_automation(D, {"data": {"metrics": []}}, folder="workouts-only",
                              mtime=1_900_100_000)
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json",
                            folder="metrics", mtime=1_900_000_000)
        result = self.loader().load(D)
        self.assertEqual(result.status, STATUS_COMPLETE)
        self.assertEqual(result.health.step_count, 501)

    def test_metrics_without_target_date_records_is_nodata(self):
        payload = {"data": {"metrics": [{"name": "step_count", "units": "count", "data": [
            {"date": "2030-01-14 10:00:00 +0900", "qty": 10},
        ]}]}}
        self.write_automation(D, payload)
        result = self.loader().load(D)
        self.assertEqual(result.status, STATUS_NODATA)
        self.assertIsNone(result.health)
        self.assertIsNone(result.activity_detail)

    def test_prev_day_file_is_read_only_for_segment_sleep(self):
        prev = self.put_automation("segments/HealthAutoExport-2030-02-01.json")
        day = self.put_automation("segments/HealthAutoExport-2030-02-02.json")
        recorder = RecordingJsonLoader()
        result = self.loader(json_loader=recorder).load(SEG_DAY)
        self.assertEqual(recorder.paths, [day, prev])
        self.assertAlmostEqual(result.health.sleep_hours, 7.25)
        self.assertAlmostEqual(result.health.nap_hours, 1.0)
        self.assertEqual(result.status, STATUS_COMPLETE)
        self.assertIn(prev.name, result.detail)

    def test_prev_day_file_is_not_read_for_aggregated_sleep(self):
        # 前日ファイルがあっても aggregated なら読まない (dataless の無駄な materialize 回避)
        self.write_automation(D - timedelta(days=1), {"data": {"metrics": []}})
        day = self.put_automation("aggregated/HealthAutoExport-2030-01-15.json")
        recorder = RecordingJsonLoader()
        self.loader(json_loader=recorder).load(D)
        self.assertEqual(recorder.paths, [day])

    def test_broken_prev_day_file_withholds_sleep_and_is_partial(self):
        self.write_automation(date(2030, 2, 1), None, text="{oops")
        self.put_automation("segments/HealthAutoExport-2030-02-02.json")
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader().load(SEG_DAY)
        # 前日ファイルが読めない → 前夜 22:30-23:30 を取りこぼした過少値 (6.25h) を
        # 書かないよう夜主睡眠は None (既存値保持)。昼寝 (当日 14-20 時に終わる) と
        # 他のフィールドは前日ファイルに依存しないので残す
        self.assertIsNone(result.health.sleep_hours)
        self.assertAlmostEqual(result.health.nap_hours, 1.0)
        self.assertEqual(result.health.step_count, 2000)
        self.assertEqual(result.status, STATUS_PARTIAL)
        self.assertTrue(any("前日" in error for error in result.errors))
        self.assertEqual(result.withheld_fields, ("sleep_hours",))

    def test_missing_prev_day_file_withholds_night_that_may_start_before_midnight(self):
        # 前日ファイルが「無い」のは失敗ではない (エラーなし = complete) が、0 時前後に
        # 始まる夜は前日ファイルにしか無い segment (22:30-23:30) を取りこぼしている
        # 恐れがある → 過少値 6.25h を確定値として書かない
        self.put_automation("segments/HealthAutoExport-2030-02-02.json")
        result = self.loader().load(SEG_DAY)
        self.assertIsNone(result.health.sleep_hours)
        self.assertEqual(result.withheld_fields, ("sleep_hours",))
        self.assertEqual(result.status, STATUS_COMPLETE)
        self.assertEqual(result.errors, ())
        self.assertIn("前日=なし", result.detail)
        self.assertAlmostEqual(result.health.nap_hours, 1.0)

    def test_missing_prev_day_file_keeps_night_that_starts_late(self):
        # 夜が 02:00 より後に始まっていれば前日ファイルの segment は繋がりえない → 確定
        self.write_automation(SEG_DAY, {"data": {"metrics": [{
            "name": "sleep_analysis", "units": "hr", "data": [
                {"startDate": "2030-02-02 03:00:00 +0900", "endDate": "2030-02-02 09:00:00 +0900",
                 "value": "Core", "source": "Sample Watch"},
            ]}]}})
        result = self.loader().load(SEG_DAY)
        self.assertAlmostEqual(result.health.sleep_hours, 6.0)
        self.assertEqual(result.withheld_fields, ())
        self.assertEqual(result.status, STATUS_COMPLETE)

    def test_prev_day_file_written_before_the_night_ended_withholds_sleep(self):
        # 前日ファイルの最終更新が夜主睡眠の終了 (06:10 +0900) より前 = 前夜の segment が
        # まだ記録されていない時点の出力 → 結合しても過少の恐れ。
        # 比較相手 (segment の終了) は record 自身の offset を持つ絶対時刻なので、mtime も
        # 絶対時刻 (05:00 +0900) で置く (実行環境の TZ に依存させない。naive なローカル
        # 05:00 だと UTC の CI では 14:00 JST になり、夜明け後の確定版と区別できない)
        self.put_automation("segments/HealthAutoExport-2030-02-01.json",
                            mtime=datetime(2030, 2, 2, 5, 0, tzinfo=_JST).timestamp())
        self.put_automation("segments/HealthAutoExport-2030-02-02.json")
        result = self.loader().load(SEG_DAY)
        self.assertIsNone(result.health.sleep_hours)
        self.assertEqual(result.withheld_fields, ("sleep_hours",))
        self.assertIn("より前に書かれています", result.detail)

    def test_prev_day_in_aggregated_form_cannot_complete_a_segment_night(self):
        # aggregateSleep を切り替えた日: 前日ファイルは集計形式で segment を結合できない
        self.write_automation(date(2030, 2, 1), {"data": {"metrics": [{
            "name": "sleep_analysis", "units": "hr", "data": [
                {"date": "2030-02-01 00:00:00 +0900", "totalSleep": 7.0,
                 "sleepEnd": "2030-02-01 06:00:00 +0900", "source": "Sample Watch"},
            ]}]}})
        self.put_automation("segments/HealthAutoExport-2030-02-02.json")
        result = self.loader().load(SEG_DAY)
        self.assertIsNone(result.health.sleep_hours)
        self.assertEqual(result.withheld_fields, ("sleep_hours",))
        self.assertIn("集計形式", result.detail)

    def test_prev_day_is_read_when_day_file_has_no_sleep(self):
        # 当日ファイルに睡眠 metric が無い (夜全体が前日ファイルの segment) → 前日を読む
        prev = self.write_automation(date(2030, 2, 1), {"data": {"metrics": [{
            "name": "sleep_analysis", "units": "hr", "data": [
                {"startDate": "2030-02-01 22:00:00 +0900", "endDate": "2030-02-02 06:00:00 +0900",
                 "value": "Asleep", "source": "Sample Watch"},
            ]}]}})
        day = self.write_automation(SEG_DAY, {"data": {"metrics": [{
            "name": "step_count", "units": "count", "data": [
                {"date": "2030-02-02 12:00:00 +0900", "qty": 2000, "source": "Sample Phone"},
            ]}]}})
        recorder = RecordingJsonLoader()
        result = self.loader(json_loader=recorder).load(SEG_DAY)
        self.assertEqual(recorder.paths, [day, prev])
        self.assertAlmostEqual(result.health.sleep_hours, 8.0)
        self.assertEqual(result.status, STATUS_COMPLETE)

    def test_stale_prev_candidate_after_broken_best_is_not_used(self):
        # 前日の最良候補が壊れていて、残る候補は前日の途中経過 → 結合に使わない
        self.write_automation(date(2030, 2, 1), None, folder="新", text="{oops",
                              mtime=_final_mtime(SEG_DAY))
        self.put_automation("segments/HealthAutoExport-2030-02-01.json", folder="旧",
                            mtime=_local(date(2030, 2, 1), 23))
        self.put_automation("segments/HealthAutoExport-2030-02-02.json")
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader().load(SEG_DAY)
        self.assertIsNone(result.health.sleep_hours)
        self.assertEqual(result.status, STATUS_PARTIAL)

    def test_unexpected_exception_becomes_failed(self):
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json")

        def buggy_loader(path, logger):
            raise RuntimeError("bug")

        with self.assertLogs(_LOGGER, level="ERROR") as logs:
            result = self.loader(json_loader=buggy_loader).load(D)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertIsNone(result.health)
        self.assertIn("RuntimeError", result.detail)
        self.assertTrue(any("Traceback" in line for line in logs.output))

    def test_missing_export_dir_is_failed(self):
        loader = self.loader(export_dir=self.tmp / "nope")
        with self.assertLogs(_LOGGER, level="WARNING") as logs:
            result = loader.load(D)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertIn("HEALTH_EXPORT_DIR", result.detail)
        self.assertIn("status=failed", logs.output[-1])


class TestSupplementFromAutoSync(LoaderTestBase):
    """同日に JSON と AutoSync .hae の両方があるとき、JSON の None だけを .hae で補う。"""

    def _sleep_hae_payload(self) -> dict:
        source = [{"identifier": "com.example.sleeptracker"}]
        return {"data": [
            # 夜主睡眠 (JSON 側に 7.0 があるので使われない)
            {"asleep": 6.0, "start": _mac(D - timedelta(days=1), 23), "end": _mac(D, 5),
             "sources": source},
            # 昼寝 15:00-16:15 (end 16 時台 → nap)
            {"asleep": 1.25, "start": _mac(D, 15), "end": _mac(D, 16, 15), "sources": source},
        ]}

    def test_nap_hours_is_supplemented(self):
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json")
        self.put_hae("sleep_analysis", D)
        self.put_hae("step_count", D)  # 存在するが JSON に値があるので読まれない
        fetcher = FakeMetricFetcher({("sleep_analysis", D): self._sleep_hae_payload()})
        result = self.loader(fetcher).load(D)
        self.assertEqual(result.source, SOURCE_AUTOMATION)
        self.assertEqual(result.status, STATUS_COMPLETE)
        self.assertAlmostEqual(result.health.nap_hours, 1.25)
        self.assertAlmostEqual(result.health.sleep_hours, 7.0)   # JSON (総睡眠) のまま
        self.assertEqual(result.health.step_count, 501)          # JSON のまま
        self.assertEqual(result.supplemented_fields, ("nap_hours",))
        self.assertEqual(result.missing_fields, ())
        # JSON で欠けたフィールドの .hae だけを解凍する (前日 sleep は .hae が無いので呼ばない)
        self.assertEqual(fetcher.calls, [("sleep_analysis", D)])
        self.assertIn("補完", result.detail)

    def test_mindful_supplement_rebuilds_activity_detail(self):
        # ワークアウトも書き出す設定 (data.workouts あり) なら、AutoSync で補った瞑想で組み直す
        payload = {"data": {"metrics": [{"name": "step_count", "units": "count", "data": [
            {"date": "2030-01-15 10:00:00 +0900", "qty": 100, "source": "Sample Phone"},
        ]}], "workouts": []}}
        self.write_automation(D, payload)
        self.put_hae("mindful_minutes", D)
        fetcher = FakeMetricFetcher({("mindful_minutes", D): {"data": [
            {"start": _mac(D, 7), "end": _mac(D, 7, 10), "qty": 10, "unit": "min"},
        ]}})
        result = self.loader(fetcher).load(D)
        self.assertEqual(result.health.mindful_sessions, 1)
        self.assertAlmostEqual(result.health.mindful_minutes, 10.0)
        self.assertEqual(result.activity_detail, "瞑想10分")
        self.assertEqual(result.supplemented_fields, ("mindful_sessions", "mindful_minutes"))

    def test_mindful_supplement_without_workouts_key_keeps_activity_unknown(self):
        # ワークアウトを出力しない設定では、瞑想を補っても「瞑想10分」と断定しない
        payload = {"data": {"metrics": [{"name": "step_count", "units": "count", "data": [
            {"date": "2030-01-15 10:00:00 +0900", "qty": 100, "source": "Sample Phone"},
        ]}]}}
        self.write_automation(D, payload)
        self.put_hae("mindful_minutes", D)
        fetcher = FakeMetricFetcher({("mindful_minutes", D): {"data": [
            {"start": _mac(D, 7), "end": _mac(D, 7, 10), "qty": 10, "unit": "min"},
        ]}})
        result = self.loader(fetcher).load(D)
        self.assertAlmostEqual(result.health.mindful_minutes, 10.0)
        self.assertIsNone(result.activity_detail)

    def test_supplement_failure_is_partial_but_keeps_json_values(self):
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json")
        self.put_hae("sleep_analysis", D)
        fetcher = FakeMetricFetcher({("sleep_analysis", D): _FAIL})
        with self.assertLogs(_LOGGER, level="WARNING") as logs:
            result = self.loader(fetcher).load(D)
        self.assertIn("status=partial", logs.output[-1])
        self.assertEqual(result.status, STATUS_PARTIAL)
        self.assertIsNone(result.health.nap_hours)
        self.assertEqual(result.health.step_count, 501)
        self.assertTrue(any("sleep_analysis" in error for error in result.errors))

    def test_supplement_skips_nap_dated_to_next_day_by_hae(self):
        # aggregateSleep は 18 時以降に終わる睡眠を翌日付にして翌日の totalSleep に含める。
        # 17:30-18:30 の昼寝を当日の nap にも数えると日をまたいで二重計上になる
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json")
        self.put_hae("sleep_analysis", D)
        source = [{"identifier": "com.example.sleeptracker"}]
        fetcher = FakeMetricFetcher({("sleep_analysis", D): {"data": [
            {"asleep": 1.25, "start": _mac(D, 15), "end": _mac(D, 16, 15), "sources": source},
            {"asleep": 1.0, "start": _mac(D, 17, 30), "end": _mac(D, 18, 30), "sources": source},
        ]}})
        result = self.loader(fetcher).load(D)
        self.assertAlmostEqual(result.health.nap_hours, 1.25)

    def test_truncated_hae_is_not_used_for_supplement(self):
        # 補完元の .hae も対象日の途中 (17:55) までの出力なら使わない
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json")
        self.put_hae("sleep_analysis", D, mtime=_local(D, 17, 55))
        fetcher = FakeMetricFetcher({("sleep_analysis", D): self._sleep_hae_payload()})
        result = self.loader(fetcher).load(D)
        self.assertEqual(fetcher.calls, [])          # 解凍もしない
        self.assertIsNone(result.health.nap_hours)
        self.assertEqual(result.withheld_fields, ("nap_hours",))
        self.assertEqual(result.status, STATUS_COMPLETE)
        self.assertEqual(result.supplemented_fields, ())

    def test_supplement_exception_keeps_json_values(self):
        # 解凍側の OSError (archive 書込失敗など) で JSON の値まで失わない
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json")
        self.put_hae("sleep_analysis", D)
        fetcher = FakeMetricFetcher({("sleep_analysis", D): _RAISE})
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(fetcher).load(D)
        self.assertEqual(result.status, STATUS_PARTIAL)
        self.assertEqual(result.health.step_count, 501)
        self.assertIsNone(result.health.nap_hours)
        self.assertTrue(any("OSError" in error for error in result.errors))

    def test_supplemented_value_dropped_by_validation_is_not_reported(self):
        payload = {"data": {"metrics": [{"name": "step_count", "units": "count", "data": [
            {"date": "2030-01-15 10:00:00 +0900", "qty": 100, "source": "Sample Phone"},
        ]}]}}
        self.write_automation(D, payload)
        self.put_hae("body_fat_percentage", D)
        fetcher = FakeMetricFetcher({("body_fat_percentage", D): {"data": [
            {"qty": 95.0, "unit": "%", "start": _mac(D, 7), "end": _mac(D, 7)},
        ]}})
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(fetcher).load(D)
        self.assertIsNone(result.health.body_fat_percentage)
        self.assertEqual(result.supplemented_fields, ())
        self.assertIn("妥当性違反", result.detail)

    def test_no_supplement_when_autosync_has_no_file_for_the_day(self):
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json")
        self.put_hae("sleep_analysis", D - timedelta(days=3))
        fetcher = FakeMetricFetcher({})
        result = self.loader(fetcher).load(D)
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(result.status, STATUS_COMPLETE)


class TestAutoSyncOnlyPath(LoaderTestBase):
    """オートメーション JSON が無い日 (オートメーション導入前など) は AutoSync .hae だけで集計する。"""

    def _payloads(self) -> "dict[tuple[str, date], object]":
        prev = D - timedelta(days=1)
        src = [{"identifier": "com.example.sleeptracker"}]
        # 前日 22:00 → 当日 01:00 のセッションは前日ファイルと当日ファイルの両方に入る
        cross_midnight = {"asleep": 3.0, "start": _mac(prev, 22), "end": _mac(D, 1), "sources": src}
        return {
            ("step_count", D): {"data": [
                {"qty": 1200, "unit": "count", "start": _mac(D, 9), "end": _mac(D, 9, 30)},
                {"qty": 800, "unit": "count", "start": _mac(D, 18), "end": _mac(D, 18, 30)},
                # 他の日の record (AutoSync の取りこぼし回収で混入した想定) は除外する
                {"qty": 5000, "unit": "count", "start": _mac(D + timedelta(days=2), 9),
                 "end": _mac(D + timedelta(days=2), 10)},
            ]},
            ("heart_rate", D): {"data": [
                {"avg": 60, "max": 65, "min": 55, "start": _mac(D, 3), "end": _mac(D, 3)},
                {"avg": 100, "max": 120, "min": 90, "start": _mac(D, 9), "end": _mac(D, 9)},
            ]},
            ("sleep_analysis", D): {"data": [
                cross_midnight,
                {"asleep": 4.0, "start": _mac(D, 1, 30), "end": _mac(D, 5, 30), "sources": src},
                {"asleep": 0.5, "start": _mac(D, 15), "end": _mac(D, 15, 30), "sources": src},
            ]},
            ("sleep_analysis", prev): {"data": [cross_midnight]},
            ("mindful_minutes", D): {"data": []},
        }

    def _put_all(self) -> None:
        for metric_dir, d in self._payloads():
            self.put_hae(metric_dir, d)

    def test_autosync_only_day(self):
        self._put_all()
        fetcher = FakeMetricFetcher(self._payloads())
        with self.assertLogs(_LOGGER, level="WARNING") as logs:
            result = self.loader(fetcher).load(D)
        self.assertEqual(result.source, SOURCE_AUTOSYNC)
        self.assertEqual(result.status, STATUS_COMPLETE)
        h = result.health
        self.assertEqual(h.step_count, 2000)   # 他日の 5000 は除外
        self.assertTrue(any("他の日付" in line for line in logs.output))
        self.assertAlmostEqual(h.heart_rate_avg, 80.0)
        self.assertAlmostEqual(h.heart_rate_max, 120.0)
        # 日付をまたぐ 3.0h は前日・当日の両ファイルにあるが 1 回だけ: 3.0 + 4.0
        self.assertAlmostEqual(h.sleep_hours, 7.0)
        self.assertAlmostEqual(h.nap_hours, 0.5)
        self.assertIsNone(h.mindful_minutes)
        # AutoSync のワークアウト (AutoSync/Workouts/) は読まないので、瞑想が空でも
        # 「なし」と断定しない (その日にあったワークアウトを含む既存の値を消さない)
        self.assertIsNone(result.activity_detail)
        self.assertIn(("sleep_analysis", D - timedelta(days=1)), fetcher.calls)

    def test_activity_detail_unknown_when_mindful_file_missing(self):
        payloads = self._payloads()
        del payloads[("mindful_minutes", D)]
        for metric_dir, d in payloads:
            self.put_hae(metric_dir, d)
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(FakeMetricFetcher(payloads)).load(D)
        self.assertIsNone(result.activity_detail)
        self.assertEqual(result.status, STATUS_COMPLETE)

    def test_partial_when_some_metric_fails(self):
        self._put_all()
        payloads = self._payloads()
        payloads[("heart_rate", D)] = _FAIL
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(FakeMetricFetcher(payloads)).load(D)
        self.assertEqual(result.status, STATUS_PARTIAL)
        self.assertEqual(result.health.step_count, 2000)
        self.assertIsNone(result.health.heart_rate_avg)
        self.assertTrue(any("heart_rate" in error for error in result.errors))

    def test_failed_when_every_metric_fails(self):
        self._put_all()
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(FakeMetricFetcher({})).load(D)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertEqual(result.source, SOURCE_AUTOSYNC)
        self.assertIsNone(result.health)

    def test_truncated_hae_values_are_withheld(self):
        # 停止済み AutoSync の .hae が対象日の途中 (10:00) までの出力 → 二度と更新されない
        # 過少値なので書かない。確定版 (翌日書き出し) の metric はそのまま使う
        payloads = self._payloads()
        for metric_dir, d in payloads:
            self.put_hae(metric_dir, d)
        self.put_hae("step_count", D, mtime=_local(D, 10))
        self.put_hae("mindful_minutes", D, mtime=_local(D, 14))
        fetcher = FakeMetricFetcher(payloads)
        result = self.loader(fetcher).load(D)
        self.assertIsNone(result.health.step_count)
        self.assertIsNone(result.health.mindful_sessions)
        # 途中までの瞑想ファイルから「なし」とは言えない
        self.assertIsNone(result.activity_detail)
        self.assertEqual(
            result.withheld_fields, ("step_count", "mindful_sessions", "mindful_minutes"),
        )
        self.assertAlmostEqual(result.health.heart_rate_avg, 80.0)
        self.assertEqual(result.status, STATUS_COMPLETE)
        self.assertFalse(result.provisional)
        self.assertNotIn(("step_count", D), fetcher.calls)

    def test_all_truncated_hae_is_nodata(self):
        payloads = self._payloads()
        for metric_dir, d in payloads:
            self.put_hae(metric_dir, d, mtime=_local(d, 11))
        result = self.loader(FakeMetricFetcher(payloads)).load(D)
        self.assertEqual(result.status, STATUS_NODATA)
        self.assertIsNone(result.health)
        self.assertIn("step_count", result.withheld_fields)

    def test_today_autosync_values_are_provisional(self):
        payloads = self._payloads()
        for metric_dir, d in payloads:
            self.put_hae(metric_dir, d)
        self.put_hae("step_count", D, mtime=_local(D, 10))
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(FakeMetricFetcher(payloads),
                                 now=datetime.combine(D, time(11, 0))).load(D)
        self.assertEqual(result.health.step_count, 2000)
        self.assertTrue(result.provisional)
        self.assertEqual(result.withheld_fields, ())

    def test_failed_day_sleep_file_does_not_fall_back_to_previous_day_only(self):
        # 当日の sleep .hae を読めない → 前日ファイルの日付またぎ分 (3.0h) だけの
        # 過少値を夜主睡眠にしない
        payloads = self._payloads()
        for metric_dir, d in payloads:
            self.put_hae(metric_dir, d)
        payloads[("sleep_analysis", D)] = _FAIL
        fetcher = FakeMetricFetcher(payloads)
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(fetcher).load(D)
        self.assertIsNone(result.health.sleep_hours)
        self.assertIsNone(result.health.nap_hours)
        self.assertEqual(result.status, STATUS_PARTIAL)
        self.assertEqual(result.health.step_count, 2000)
        self.assertNotIn(("sleep_analysis", D - timedelta(days=1)), fetcher.calls)

    def test_failed_previous_day_sleep_file_is_partial_but_keeps_day_values(self):
        # 当日の .hae はその日に重なる全 record を含むので値は残す。読込失敗は報告する
        payloads = self._payloads()
        for metric_dir, d in payloads:
            self.put_hae(metric_dir, d)
        payloads[("sleep_analysis", D - timedelta(days=1))] = _FAIL
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(FakeMetricFetcher(payloads)).load(D)
        self.assertEqual(result.status, STATUS_PARTIAL)
        self.assertAlmostEqual(result.health.sleep_hours, 7.0)
        self.assertTrue(any(error.startswith("前日 ") for error in result.errors))

    def test_previous_day_sleep_file_written_before_its_day_ended_is_ignored(self):
        payloads = self._payloads()
        for metric_dir, d in payloads:
            self.put_hae(metric_dir, d)
        self.put_hae("sleep_analysis", D - timedelta(days=1), mtime=_local(D - timedelta(days=1), 20))
        fetcher = FakeMetricFetcher(payloads)
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(fetcher).load(D)
        self.assertEqual(result.status, STATUS_COMPLETE)
        self.assertAlmostEqual(result.health.sleep_hours, 7.0)
        self.assertNotIn(("sleep_analysis", D - timedelta(days=1)), fetcher.calls)

    def test_broken_automation_with_autosync_fallback_is_partial(self):
        self._put_all()
        self.write_automation(D, None, text="{")
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(FakeMetricFetcher(self._payloads())).load(D)
        self.assertEqual(result.source, SOURCE_AUTOSYNC)
        self.assertEqual(result.status, STATUS_PARTIAL)
        self.assertEqual(result.health.step_count, 2000)


class TestHealthLoadResult(unittest.TestCase):
    def test_defaults(self):
        result = HealthLoadResult(health=None, activity_detail=None, source=SOURCE_NONE, detail="x")
        self.assertEqual(result.status, STATUS_NODATA)
        self.assertEqual(result.missing_fields, HEALTH_FIELDS)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.supplemented_fields, ())
        self.assertFalse(result.provisional)
        self.assertEqual(result.withheld_fields, ())


if __name__ == "__main__":
    unittest.main()
