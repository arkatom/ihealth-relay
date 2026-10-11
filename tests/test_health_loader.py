"""health_loader.py (オートメーション JSON 優先 + AutoSync 補完) のテスト。

実 iCloud / compression_tool は使わない:

* HAE Documents 風のツリーを tempdir に作る (オートメーション JSON は合成 fixture をコピー)
* AutoSync の ``.hae`` はダミー bytes を置くだけ (存在判定用)。解凍は
  ``metric_fetcher`` を差し替えた fake が「解凍済み JSON」を archive に書いて返す
* 現在時刻は ``now`` で注入する (既定は全 fixture の日付より後)。置くファイルの
  mtime は既定で「対象日の翌日 12:00 JST」= 対象日の終わりより後に書かれた確定版
* 時刻は全て **aware (JST などの明示 offset)** で作る。実行環境のローカル tz
  (CI は UTC) に結果が依存しないこと自体も ``TestMachineTimezoneIndependence`` で検証する
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time as systime
import unittest
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterator

from ihealth.automation import AutomationError, AutomationIndex, load_automation_json
from ihealth.automation_parser import ACTIVITY_NONE, HEALTH_FIELDS
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
_EST = timezone(timedelta(hours=-5))

D = date(2030, 1, 15)          # aggregated fixture の日付
SEG_DAY = date(2030, 2, 2)     # segments fixture の日付


def _mac(d: date, hour: int, minute: int = 0) -> float:
    """JST の日時を Mac absolute time (AutoSync .hae のタイムスタンプ) にする。"""
    local = datetime(d.year, d.month, d.day, hour, minute, tzinfo=_JST)
    return (local - _MAC_EPOCH).total_seconds()


_FAIL = object()
_RAISE = object()

#: テストの既定の「現在時刻」(全 fixture の日付より後 = 対象日は終わっている)。
NOW = datetime(2030, 12, 31, 12, 0, tzinfo=_JST)


def _at(d: date, hour: int, minute: int = 0, tz: timezone = _JST) -> datetime:
    """``tz`` (既定 JST) の ``d`` ``hour:minute`` (aware)。"""
    return datetime.combine(d, time(hour, minute), tzinfo=tz)


def _jst(d: date, hour: int, minute: int = 0) -> float:
    """JST の ``d`` ``hour:minute`` の epoch 秒 (mtime 用。実行環境の tz に依存しない)。"""
    return _at(d, hour, minute).timestamp()


def _final_mtime(d: date) -> float:
    """``d`` の終わりより後 (翌日 12:00 JST) に書かれた = 確定版の mtime。"""
    return _jst(d + timedelta(days=1), 12)


@contextmanager
def _machine_tz(name: str) -> Iterator[None]:
    """プロセスのローカル tz を一時的に ``name`` にする (``TZ`` + ``time.tzset``)。"""
    saved = os.environ.get("TZ")
    os.environ["TZ"] = name
    systime.tzset()
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        systime.tzset()


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

    def config(self, export_dir: "Path | None" = None, *, diary_tz: "timezone | None" = None) -> Config:
        extra = {} if diary_tz is None else {"diary_tz": diary_tz}
        return Config(
            notion_secret=None,
            database_id=None,
            health_export_dir=export_dir or self.docs,
            archive_root=self.archive,
            birth_date=date(1990, 6, 1),
            **extra,
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
        config = self.config(kwargs.pop("export_dir", None), diary_tz=kwargs.pop("diary_tz", None))
        return HealthLoader(config, _LOGGER, now=lambda: current, **kwargs)


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
        self.assertFalse(result.provisional)
        # fixture (現役設定) はワークアウトを出力しない → W3-7: 瞑想 + エクササイズ時間で組む
        # (旧: 判断できないとして None。WAVE3 W3-7 で意図的に変更)
        self.assertEqual(result.activity_detail, "瞑想6分、エクササイズ3分")
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
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json", mtime=_jst(D, 21))
        result = self.loader(now=_at(D, 22)).load(D)
        self.assertTrue(result.provisional)
        self.assertEqual(result.status, STATUS_COMPLETE)
        self.assertEqual(result.health.step_count, 501)
        self.assertEqual(result.activity_detail, "瞑想6分、エクササイズ3分")
        self.assertEqual(result.withheld_fields, ())

    def test_ended_day_from_file_written_before_day_end_is_provisional(self):
        # 対象日は終わったのに、ファイルは 21:00 の途中経過のまま (HAE が未再出力)。
        # W3-2h: 全保留せず値を暫定値として返す (後の再出力で確定値に置き換わる。統合側が
        # 台帳で追跡する)。旧: health=None / nodata / withheld (WAVE3 W3-2h で意図的に変更)
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json", mtime=_jst(D, 21))
        result = self.loader(now=_at(D + timedelta(days=1), 7)).load(D)
        self.assertTrue(result.provisional)
        self.assertEqual(result.status, STATUS_COMPLETE)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.withheld_fields, ())     # 保留は停止済み AutoSync だけ
        self.assertEqual(result.health.step_count, 501)
        self.assertAlmostEqual(result.health.sleep_hours, 7.0)
        self.assertEqual(result.missing_fields, ("nap_hours",))
        self.assertEqual(result.activity_detail, "瞑想6分、エクササイズ3分")
        self.assertIn("途中経過", result.detail)
        self.assertIn("2030-01-16 00:00 +0900", result.detail)   # 日の終わり (record の offset)
        self.assertIn("2030-01-15 21:00 +0900", result.detail)   # 最終更新

    def test_not_provisional_when_written_after_day_end(self):
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json",
                            mtime=_jst(D + timedelta(days=1), 7))
        result = self.loader().load(D)
        self.assertFalse(result.provisional)
        self.assertEqual(result.status, STATUS_COMPLETE)
        self.assertEqual(result.withheld_fields, ())

    def test_written_exactly_at_day_end_is_final(self):
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json",
                            mtime=_jst(D + timedelta(days=1), 0))
        self.assertFalse(self.loader().load(D).provisional)
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json",
                            mtime=_jst(D + timedelta(days=1), 0) - 1)
        self.assertTrue(self.loader().load(D).provisional)

    def _steps_payload(self, offset: str) -> dict:
        return {"data": {"metrics": [{"name": "step_count", "units": "count", "data": [
            {"date": f"2030-01-15 10:00:00{offset}", "qty": 5000},
        ]}]}}

    def test_day_end_follows_record_offset_not_diary_tz(self):
        # record が -0500 (旅行先) の日: 日の終わりは 1/16 00:00 -0500 = 1/16 14:00 JST。
        # 1/16 05:00 JST の書き出しは (日記の基準 tz JST では日の終わり後でも) 途中経過
        self.write_automation(D, self._steps_payload(" -0500"),
                              mtime=_jst(D + timedelta(days=1), 5))
        result = self.loader().load(D)
        self.assertTrue(result.provisional)
        self.assertEqual(result.health.step_count, 5000)
        self.assertIn("2030-01-16 00:00 -0500", result.detail)
        # 日の終わり (14:00 JST) より後の書き出しなら確定
        self.write_automation(D, self._steps_payload(" -0500"),
                              mtime=_jst(D + timedelta(days=1), 15))
        self.assertFalse(self.loader().load(D).provisional)

    def test_record_offset_wins_over_diary_tz(self):
        # 日記の基準 tz が -0500 でも、record が +0900 なら日の終わりは 1/16 00:00 +0900
        self.write_automation(D, self._steps_payload(" +0900"),
                              mtime=_jst(D + timedelta(days=1), 1))
        result = self.loader(diary_tz=_EST).load(D)
        self.assertFalse(result.provisional)

    def test_day_end_falls_back_to_diary_tz_without_offsets(self):
        # offset の無い日時 (naive) しか無い → 日記の基準 tz (IHEALTH_TZ_OFFSET) の暦日
        self.write_automation(D, self._steps_payload(""), mtime=_jst(D + timedelta(days=1), 5))
        self.assertTrue(self.loader(diary_tz=_EST).load(D).provisional)    # 1/15 15:00 -0500
        self.assertFalse(self.loader().load(D).provisional)                  # 既定 JST
        self.write_automation(D, self._steps_payload(""), mtime=_jst(D + timedelta(days=1), 15))
        self.assertFalse(self.loader(diary_tz=_EST).load(D).provisional)   # 1/16 01:00 -0500

    def test_today_is_judged_by_record_offset(self):
        # 「対象日がもう終わったか」も record の暦日で判定する: 現在 1/16 05:00 JST =
        # 1/15 15:00 -0500 は record (-0500) の暦日ではまだ 1/15 → 暫定 (mtime が日の終わり
        # 14:00 JST より後でも。基準 tz JST で判定していたら「終わった日の確定版」になる)
        self.write_automation(D, self._steps_payload(" -0500"),
                              mtime=_jst(D + timedelta(days=1), 15))
        result = self.loader(now=_at(D + timedelta(days=1), 5)).load(D)
        self.assertTrue(result.provisional)
        self.assertEqual(result.health.step_count, 5000)
        self.assertFalse(self.loader(now=_at(D + timedelta(days=1), 15)).load(D).provisional)

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
                            folder="旧オートメーション", mtime=_jst(D, 8, 46))
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader().load(D)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertIsNone(result.health)
        self.assertFalse(result.provisional)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("古い候補のため不採用", result.detail)

    def test_final_older_candidate_after_broken_best_is_used(self):
        # 残る候補が対象日の終わり以降に書かれた確定版なら fallback してよい
        self.write_automation(D, None, folder="新オートメーション", text="{",
                              mtime=_final_mtime(D + timedelta(days=1)))
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json",
                            folder="旧オートメーション", mtime=_jst(D + timedelta(days=1), 0, 30))
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader().load(D)
        self.assertEqual(result.status, STATUS_PARTIAL)
        self.assertEqual(result.health.step_count, 501)
        self.assertFalse(result.provisional)

    def test_no_fallback_to_older_candidate_for_today(self):
        # 今日の分は全候補が途中経過 → 読めなかった新しい候補より古い値は使わない
        self.write_automation(D, None, folder="新オートメーション", text="{",
                              mtime=_jst(D, 20))
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json",
                            folder="旧オートメーション", mtime=_jst(D, 9))
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(now=_at(D, 21)).load(D)
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
            {"name": "apple_exercise_time", "units": "min",
             "data": [{"date": "2030-01-15 08:00:00 +0900", "qty": 21}]},
        ]}})
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader().load(D)
        self.assertEqual(result.status, STATUS_PARTIAL)
        self.assertEqual(result.health.step_count, 100)
        self.assertIsNone(result.health.mindful_sessions)
        # 瞑想を解釈できない → 「エクササイズ21分」だけの一覧も書かない
        self.assertIsNone(result.activity_detail)

    def test_exercise_only_day_from_automation(self):
        # 現役設定 (ワークアウト未出力) の典型: エクササイズ時間だけの日 → "エクササイズN分"
        self.write_automation(D, {"data": {"metrics": [
            {"name": "step_count", "units": "count",
             "data": [{"date": "2030-01-15 10:00:00 +0900", "qty": 700}]},
            {"name": "apple_exercise_time", "units": "min",
             "data": [{"date": "2030-01-15 21:42:00 +0900", "qty": 1}]},
        ]}})
        result = self.loader().load(D)
        self.assertEqual(result.activity_detail, "エクササイズ1分")
        self.assertAlmostEqual(result.health.exercise_minutes, 1.0)

    def test_no_exercise_data_means_no_activity_detail(self):
        # エクササイズ時間も瞑想も心拍も無い (Watch を着けていない等) → 「なし」と断定せず書かない
        self.write_automation(D, {"data": {"metrics": [
            {"name": "step_count", "units": "count",
             "data": [{"date": "2030-01-15 10:00:00 +0900", "qty": 700}]},
        ]}})
        result = self.loader().load(D)
        self.assertEqual(result.status, STATUS_COMPLETE)
        self.assertIsNone(result.activity_detail)
        # W3-7 改訂: 0 分の record があっても心拍が無ければ書かない (旧: 0 分の実測で「なし」。
        # 実際の HAE は 0 分の record を出さない。WAVE3 W3-7 改訂で意図的に変更)
        self.write_automation(D, {"data": {"metrics": [
            {"name": "step_count", "units": "count",
             "data": [{"date": "2030-01-15 10:00:00 +0900", "qty": 700}]},
            {"name": "apple_exercise_time", "units": "min",
             "data": [{"date": "2030-01-15 10:00:00 +0900", "qty": 0}]},
        ]}})
        self.assertIsNone(self.loader().load(D).activity_detail)

    def test_heart_rate_without_exercise_records_is_nashi(self):
        # 実際の HAE の運動 0 分の日: apple_exercise_time 自体が無く、心拍はある (Watch を
        # 着けていた) → 「なし」(W3-7 改訂。旧: 運動のデータが無いので None。意図的に変更)
        self.write_automation(D, {"data": {"metrics": [
            {"name": "step_count", "units": "count",
             "data": [{"date": "2030-01-15 10:00:00 +0900", "qty": 700}]},
            {"name": "heart_rate", "units": "count/min",
             "data": [{"date": "2030-01-15 10:00:00 +0900", "Avg": 72, "Max": 88}]},
        ]}})
        result = self.loader().load(D)
        self.assertEqual(result.status, STATUS_COMPLETE)
        self.assertIsNone(result.health.exercise_minutes)
        self.assertAlmostEqual(result.health.heart_rate_avg, 72.0)
        self.assertEqual(result.activity_detail, ACTIVITY_NONE)

    def test_exported_workouts_without_one_that_day_use_exercise_time(self):
        # ワークアウトを書き出す設定で当日 0 件 → エクササイズ時間で組む (W3-7 改訂。
        # 旧: ワークアウトの欄はワークアウトだけで None。意図的に変更)
        self.write_automation(D, {"data": {"metrics": [
            {"name": "apple_exercise_time", "units": "min",
             "data": [{"date": "2030-01-15 21:42:00 +0900", "qty": 12}]},
            {"name": "heart_rate", "units": "count/min",
             "data": [{"date": "2030-01-15 21:42:00 +0900", "Avg": 120, "Max": 140}]},
        ], "workouts": []}})
        result = self.loader().load(D)
        self.assertNotIn("ワークアウト未出力", result.detail)
        self.assertEqual(result.activity_detail, "エクササイズ12分")

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

    # --- segment 形式の夜主睡眠と前日ファイル ---
    # 0 時前後に始まる夜は前日ファイルにしか無い segment (22:30-23:30 = 1.0h) を結合できないと
    # 過少値 (6.25h、正しくは 7.25h) になる。W3-2h: 保留 (withheld) は停止済み AutoSync の
    # 途中までの出力に限定し、こちらは前日ファイルの再出力で確定する暫定値として返す
    # (旧: sleep_hours=None + withheld_fields=("sleep_hours",)。WAVE3 W3-2h で意図的に変更)。
    # 前日ファイルに依存しない昼寝・他のフィールドは従来どおり

    def test_broken_prev_day_file_makes_night_provisional_and_partial(self):
        self.write_automation(date(2030, 2, 1), None, text="{oops")
        self.put_automation("segments/HealthAutoExport-2030-02-02.json")
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader().load(SEG_DAY)
        self.assertAlmostEqual(result.health.sleep_hours, 6.25)
        self.assertTrue(result.provisional)
        self.assertEqual(result.withheld_fields, ())
        self.assertAlmostEqual(result.health.nap_hours, 1.0)
        self.assertEqual(result.health.step_count, 2000)
        self.assertEqual(result.status, STATUS_PARTIAL)
        self.assertTrue(any("前日" in error for error in result.errors))
        self.assertIn("過少の恐れ", result.detail)

    def test_missing_prev_day_file_makes_night_that_may_start_before_midnight_provisional(self):
        # 前日ファイルが「無い」のは失敗ではない (エラーなし = complete)
        self.put_automation("segments/HealthAutoExport-2030-02-02.json")
        result = self.loader().load(SEG_DAY)
        self.assertAlmostEqual(result.health.sleep_hours, 6.25)
        self.assertTrue(result.provisional)
        self.assertEqual(result.withheld_fields, ())
        self.assertEqual(result.status, STATUS_COMPLETE)
        self.assertEqual(result.errors, ())
        self.assertIn("前日=なし", result.detail)
        self.assertIn("前日ファイルがありません", result.detail)
        self.assertAlmostEqual(result.health.nap_hours, 1.0)

    def test_only_the_night_sleep_is_provisional_when_prev_day_file_is_missing(self):
        # finding: 夜主睡眠が過少の恐れがあるだけなのに、日全体 (歩数・心拍…) を暫定扱いにして
        # 台帳で 30 日追跡し、4 日目から毎日要確認を送っていた → 暫定はそのフィールドだけ
        self.put_automation("segments/HealthAutoExport-2030-02-02.json")
        result = self.loader().load(SEG_DAY)
        self.assertTrue(result.provisional)
        self.assertEqual(result.provisional_fields, ("sleep_hours",))
        # 日全体が暫定 (今日) なら「全部」(None)
        today = self.loader(now=_at(SEG_DAY, 23)).load(SEG_DAY)
        self.assertTrue(today.provisional)
        self.assertIsNone(today.provisional_fields)
        # 前日ファイルが揃えば暫定ではない
        self.put_automation("segments/HealthAutoExport-2030-02-01.json")
        final = self.loader().load(SEG_DAY)
        self.assertFalse(final.provisional)
        self.assertEqual(final.provisional_fields, ())

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
        self.assertFalse(result.provisional)
        self.assertEqual(result.status, STATUS_COMPLETE)

    def test_final_prev_day_file_makes_night_final(self):
        self.put_automation("segments/HealthAutoExport-2030-02-01.json")
        self.put_automation("segments/HealthAutoExport-2030-02-02.json")
        result = self.loader().load(SEG_DAY)
        self.assertAlmostEqual(result.health.sleep_hours, 7.25)
        self.assertFalse(result.provisional)

    def test_prev_day_file_written_before_the_night_ended_makes_night_provisional(self):
        # 前日ファイルの最終更新が夜主睡眠の終了 (06:10 +0900) より前 = 前夜の segment が
        # まだ記録されきっていない時点の出力 → 結合しても過少の恐れ (暫定)。
        # 比較相手 (segment の終了) は record 自身の offset を持つ絶対時刻なので、mtime も
        # 絶対時刻 (05:00 +0900) で置く (実行環境の TZ に依存させない)
        self.put_automation("segments/HealthAutoExport-2030-02-01.json",
                            mtime=_jst(SEG_DAY, 5))
        self.put_automation("segments/HealthAutoExport-2030-02-02.json")
        result = self.loader().load(SEG_DAY)
        # この合成データでは前日ファイルに 22:30-23:30 が入っているので値は 7.25h だが、
        # 夜が明ける前の出力なので確定とは言えない
        self.assertAlmostEqual(result.health.sleep_hours, 7.25)
        self.assertTrue(result.provisional)
        self.assertEqual(result.withheld_fields, ())
        self.assertIn("より前に書かれています", result.detail)
        self.assertIn("+0900", result.detail)

    def test_prev_day_in_aggregated_form_cannot_complete_a_segment_night(self):
        # aggregateSleep を切り替えた日: 前日ファイルは集計形式で segment を結合できない
        # (HAE が直近の日を再出力すれば segment 形式の前日ファイルで確定する)
        self.write_automation(date(2030, 2, 1), {"data": {"metrics": [{
            "name": "sleep_analysis", "units": "hr", "data": [
                {"date": "2030-02-01 00:00:00 +0900", "totalSleep": 7.0,
                 "sleepEnd": "2030-02-01 06:00:00 +0900", "source": "Sample Watch"},
            ]}]}})
        self.put_automation("segments/HealthAutoExport-2030-02-02.json")
        result = self.loader().load(SEG_DAY)
        self.assertAlmostEqual(result.health.sleep_hours, 6.25)
        self.assertTrue(result.provisional)
        self.assertEqual(result.withheld_fields, ())
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
        # 前日の最良候補が壊れていて、残る候補は前日の途中経過 → 結合に使わない。
        # 前日を結合できない夜主睡眠は過少の恐れがある暫定値 (旧: None。W3-2h で変更)
        self.write_automation(date(2030, 2, 1), None, folder="新", text="{oops",
                              mtime=_final_mtime(SEG_DAY))
        self.put_automation("segments/HealthAutoExport-2030-02-01.json", folder="旧",
                            mtime=_jst(date(2030, 2, 1), 23))
        self.put_automation("segments/HealthAutoExport-2030-02-02.json")
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader().load(SEG_DAY)
        self.assertAlmostEqual(result.health.sleep_hours, 6.25)   # 旧候補の 22:30-23:30 は使わない
        self.assertTrue(result.provisional)
        self.assertEqual(result.status, STATUS_PARTIAL)
        self.assertIn("古い候補のため不採用", result.detail)

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

    def test_mindful_supplement_without_workouts_key_uses_exercise_rule(self):
        # ワークアウトを出力しない設定: W3-7 で瞑想 + エクササイズ時間から組む
        # (旧: 瞑想を補っても None。WAVE3 W3-7 で意図的に変更)。エクササイズ時間が
        # JSON にも AutoSync にも無ければ瞑想だけ
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
        self.assertEqual(result.activity_detail, "瞑想10分")

    def test_exercise_supplement_is_used_for_activity_detail(self):
        # JSON のエクササイズ時間を解釈できない (未知の単位) 日に AutoSync の値で補えたら、
        # アクティビティ詳細もその値で組む
        payload = {"data": {"metrics": [
            {"name": "step_count", "units": "count", "data": [
                {"date": "2030-01-15 10:00:00 +0900", "qty": 100, "source": "Sample Phone"}]},
            {"name": "apple_exercise_time", "units": "fortnight", "data": [
                {"date": "2030-01-15 08:00:00 +0900", "qty": 1, "source": "Sample Watch"}]},
        ]}}
        self.write_automation(D, payload)
        self.put_hae("apple_exercise_time", D)
        fetcher = FakeMetricFetcher({("apple_exercise_time", D): {"data": [
            {"start": _mac(D, 8), "end": _mac(D, 8, 35), "qty": 35, "unit": "min"},
        ]}})
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(fetcher).load(D)
        self.assertEqual(result.supplemented_fields, ("exercise_minutes",))
        self.assertAlmostEqual(result.health.exercise_minutes, 35.0)
        self.assertEqual(result.activity_detail, "エクササイズ35分")
        self.assertEqual(result.status, STATUS_PARTIAL)   # JSON 側の解釈失敗は報告する

    def test_unresolved_exercise_failure_blocks_activity_detail(self):
        payload = {"data": {"metrics": [
            {"name": "mindful_minutes", "units": "min", "data": [
                {"date": "2030-01-15 06:00:00 +0900", "qty": 5, "source": "Sample Watch"}]},
            {"name": "apple_exercise_time", "units": "fortnight", "data": [
                {"date": "2030-01-15 08:00:00 +0900", "qty": 1, "source": "Sample Watch"}]},
        ]}}
        self.write_automation(D, payload)
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader().load(D)
        self.assertEqual(result.status, STATUS_PARTIAL)
        self.assertAlmostEqual(result.health.mindful_minutes, 5.0)
        # 「瞑想5分」だけではエクササイズを取りこぼした一覧になる → 書かない
        self.assertIsNone(result.activity_detail)

    # --- finding: JSON に無い入力の補完元 .hae が保留 / 読込失敗なら、アクティビティ詳細も
    #     確定値として書かない (AutoSync だけの経路 test_autosync_truncated_mindful_blocks_activity_detail
    #     と同じ規則。旧: 補完の失敗を無視して「エクササイズ35分」を確定値として返していた) ---

    def _json_with_exercise_only(self) -> None:
        self.write_automation(D, {"data": {"metrics": [
            {"name": "step_count", "units": "count", "data": [
                {"date": "2030-01-15 10:00:00 +0900", "qty": 100, "source": "Sample Phone"}]},
            {"name": "apple_exercise_time", "units": "min", "data": [
                {"date": "2030-01-15 08:00:00 +0900", "qty": 35, "source": "Sample Watch"}]},
        ]}})

    def test_truncated_mindful_supplement_blocks_activity_detail(self):
        self._json_with_exercise_only()
        self.put_hae("mindful_minutes", D, mtime=_jst(D, 12))   # 停止済み AutoSync の途中までの出力
        fetcher = FakeMetricFetcher({("mindful_minutes", D): {"data": [
            {"start": _mac(D, 7), "end": _mac(D, 7, 24), "qty": 24, "unit": "min"},
        ]}})
        result = self.loader(fetcher).load(D)
        self.assertEqual(result.withheld_fields, ("mindful_sessions", "mindful_minutes"))
        self.assertAlmostEqual(result.health.exercise_minutes, 35.0)
        self.assertFalse(result.provisional)
        # 「エクササイズ35分」だけでは、保留した瞑想を取りこぼした一覧を確定値として書いてしまう
        self.assertIsNone(result.activity_detail)

    def test_failed_exercise_supplement_blocks_activity_detail(self):
        self.write_automation(D, {"data": {"metrics": [
            {"name": "mindful_minutes", "units": "min", "data": [
                {"date": "2030-01-15 06:00:00 +0900", "qty": 5, "source": "Sample Watch"}]},
        ]}})
        self.put_hae("apple_exercise_time", D)
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(FakeMetricFetcher({("apple_exercise_time", D): _FAIL})).load(D)
        self.assertEqual(result.status, STATUS_PARTIAL)
        self.assertAlmostEqual(result.health.mindful_minutes, 5.0)
        self.assertIsNone(result.activity_detail)   # 「瞑想5分」だけの一覧は書かない

    def test_absent_supplement_file_is_not_unknown(self):
        # 補完元の .hae 自体が無い (その日の瞑想のデータが無い) なら従来どおり組む
        self._json_with_exercise_only()
        self.put_hae("sleep_analysis", D)   # AutoSync はその日を出力しているが瞑想の .hae は無い
        result = self.loader(FakeMetricFetcher({("sleep_analysis", D): {"data": []}})).load(D)
        self.assertEqual(result.withheld_fields, ())
        self.assertEqual(result.activity_detail, "エクササイズ35分")

    # --- W3-7 改訂: 「なし」の根拠の心拍は補完後の値で見る。補完元が不明なら「なし」と断定しない ---

    def _json_with_heart_rate_only(self) -> None:
        # 実際の HAE の運動 0 分の日 (apple_exercise_time / mindful_minutes が無く、心拍はある)
        self.write_automation(D, {"data": {"metrics": [
            {"name": "step_count", "units": "count", "data": [
                {"date": "2030-01-15 10:00:00 +0900", "qty": 100, "source": "Sample Phone"}]},
            {"name": "heart_rate", "units": "count/min", "data": [
                {"date": "2030-01-15 10:00:00 +0900", "Avg": 72, "Max": 88,
                 "source": "Sample Watch"}]},
        ]}})

    def test_heart_rate_supplement_counts_for_nashi(self):
        # JSON に心拍が無くても、AutoSync の .hae で補えた心拍は Watch を着けていた根拠になる
        self.write_automation(D, {"data": {"metrics": [
            {"name": "step_count", "units": "count", "data": [
                {"date": "2030-01-15 10:00:00 +0900", "qty": 100, "source": "Sample Phone"}]},
        ]}})
        self.put_hae("heart_rate", D)
        fetcher = FakeMetricFetcher({("heart_rate", D): {"data": [
            {"avg": 72, "max": 88, "min": 60, "start": _mac(D, 10), "end": _mac(D, 10)},
        ]}})
        result = self.loader(fetcher).load(D)
        self.assertIn("heart_rate_avg", result.supplemented_fields)
        self.assertEqual(result.activity_detail, ACTIVITY_NONE)

    def test_truncated_heart_rate_supplement_does_not_claim_nashi(self):
        # 補完元の心拍の .hae が途中までの出力 (保留) → 心拍が無いのと同じ → 書かない
        self.write_automation(D, {"data": {"metrics": [
            {"name": "step_count", "units": "count", "data": [
                {"date": "2030-01-15 10:00:00 +0900", "qty": 100, "source": "Sample Phone"}]},
        ]}})
        self.put_hae("heart_rate", D, mtime=_jst(D, 12))
        fetcher = FakeMetricFetcher({("heart_rate", D): {"data": [
            {"avg": 72, "max": 88, "min": 60, "start": _mac(D, 10), "end": _mac(D, 10)},
        ]}})
        result = self.loader(fetcher).load(D)
        self.assertIn("heart_rate_avg", result.withheld_fields)
        self.assertIsNone(result.activity_detail)

    def test_heart_rate_day_with_absent_supplement_files_is_nashi(self):
        # 補完元の瞑想 / エクササイズ時間の .hae 自体が無い (その日のデータが無い) → 「なし」
        self._json_with_heart_rate_only()
        self.put_hae("sleep_analysis", D)   # AutoSync はその日を出力しているが瞑想・運動の .hae は無い
        result = self.loader(FakeMetricFetcher({("sleep_analysis", D): {"data": []}})).load(D)
        self.assertEqual((result.withheld_fields, result.errors), ((), ()))
        self.assertEqual(result.activity_detail, ACTIVITY_NONE)

    def test_truncated_mindful_supplement_does_not_claim_nashi(self):
        # 心拍がある日でも、補完元の瞑想の .hae が途中までの出力 (保留) なら「なし」と断定しない
        # (保留した瞑想を取りこぼした「なし」を確定値として書かない)
        self._json_with_heart_rate_only()
        self.put_hae("mindful_minutes", D, mtime=_jst(D, 12))
        fetcher = FakeMetricFetcher({("mindful_minutes", D): {"data": [
            {"start": _mac(D, 7), "end": _mac(D, 7, 24), "qty": 24, "unit": "min"},
        ]}})
        result = self.loader(fetcher).load(D)
        self.assertEqual(result.withheld_fields, ("mindful_sessions", "mindful_minutes"))
        self.assertAlmostEqual(result.health.heart_rate_avg, 72.0)
        self.assertIsNone(result.activity_detail)

    def test_failed_exercise_supplement_does_not_claim_nashi(self):
        # 心拍がある日でも、補完元のエクササイズ時間の .hae を読めなければ「なし」と断定しない
        self._json_with_heart_rate_only()
        self.put_hae("apple_exercise_time", D)
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(FakeMetricFetcher({("apple_exercise_time", D): _FAIL})).load(D)
        self.assertEqual(result.status, STATUS_PARTIAL)
        self.assertAlmostEqual(result.health.heart_rate_avg, 72.0)
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
        # 補完元の .hae (停止済み AutoSync) が対象日の途中 (17:55) までの出力なら使わない
        # (再出力されないので確定しない) → 保留 (withheld)
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json")
        self.put_hae("sleep_analysis", D, mtime=_jst(D, 17, 55))
        fetcher = FakeMetricFetcher({("sleep_analysis", D): self._sleep_hae_payload()})
        result = self.loader(fetcher).load(D)
        self.assertEqual(fetcher.calls, [])          # 解凍もしない
        self.assertIsNone(result.health.nap_hours)
        self.assertEqual(result.withheld_fields, ("nap_hours",))
        self.assertEqual(result.status, STATUS_COMPLETE)
        self.assertEqual(result.supplemented_fields, ())
        self.assertFalse(result.provisional)
        self.assertIn("再出力されない", result.detail)

    def test_provisional_json_still_withholds_truncated_supplement(self):
        # 主ソースが途中経過 (暫定) でも、補完元の打ち切られた .hae は保留のまま
        self.put_automation("aggregated/HealthAutoExport-2030-01-15.json", mtime=_jst(D, 21))
        self.put_hae("sleep_analysis", D, mtime=_jst(D, 17, 55))
        fetcher = FakeMetricFetcher({("sleep_analysis", D): self._sleep_hae_payload()})
        result = self.loader(fetcher).load(D)
        self.assertTrue(result.provisional)
        self.assertEqual(result.health.step_count, 501)
        self.assertEqual(result.withheld_fields, ("nap_hours",))
        self.assertEqual(fetcher.calls, [])

    def test_supplement_hae_day_end_uses_record_offset(self):
        # 補完元 .hae の「日の終わり」も主ソースの record の offset で決める
        # (record が -0500 → 1/16 00:00 -0500 = 1/16 14:00 JST。10:00 JST の .hae は途中まで)
        payload = {"data": {"metrics": [
            {"name": "step_count", "units": "count", "data": [
                {"date": "2030-01-15 10:00:00 -0500", "qty": 100, "source": "Sample Phone"}]},
        ]}}
        self.write_automation(D, payload, mtime=_jst(D + timedelta(days=2), 0))
        self.put_hae("mindful_minutes", D, mtime=_jst(D + timedelta(days=1), 10))
        fetcher = FakeMetricFetcher({("mindful_minutes", D): {"data": [
            {"start": _mac(D, 7), "end": _mac(D, 7, 10), "qty": 10, "unit": "min"},
        ]}})
        result = self.loader(fetcher).load(D)
        self.assertEqual(result.withheld_fields, ("mindful_sessions", "mindful_minutes"))
        self.assertEqual(fetcher.calls, [])

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

    def _hae(self, metric_dir: str, d: date) -> Path:
        return self.docs / "AutoSync" / "HealthMetrics" / metric_dir / (d.strftime("%Y%m%d") + ".hae")

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
        # W3-7 改訂: AutoSync 経路も瞑想 + エクササイズ時間の規則。この日はエクササイズ時間の
        # .hae が無く瞑想も 0 件だが、心拍がある (Watch を着けていた) ので「なし」
        # (旧: 運動のデータが無いので None。WAVE3 W3-7 改訂で意図的に変更)
        self.assertEqual(result.activity_detail, ACTIVITY_NONE)
        self.assertFalse(result.provisional)
        self.assertIn(("sleep_analysis", D - timedelta(days=1)), fetcher.calls)

    def test_activity_detail_none_without_exercise_and_mindful_files(self):
        # 瞑想・エクササイズ時間の .hae が無い日: 心拍の .hae があれば「なし」(W3-7 改訂。
        # 旧: 常に None。意図的に変更)、心拍の .hae も無ければ「なし」と断定せず書かない
        payloads = self._payloads()
        del payloads[("mindful_minutes", D)]
        for metric_dir, d in payloads:
            self.put_hae(metric_dir, d)
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(FakeMetricFetcher(payloads)).load(D)
        self.assertEqual(result.activity_detail, ACTIVITY_NONE)
        self.assertEqual(result.status, STATUS_COMPLETE)

        self._hae("heart_rate", D).unlink()
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(FakeMetricFetcher(payloads)).load(D)
        self.assertIsNone(result.health.heart_rate_avg)
        self.assertIsNone(result.activity_detail)
        self.assertEqual(result.status, STATUS_COMPLETE)

    # --- W3-7: AutoSync 経路のアクティビティ詳細 (mindful .hae + apple_exercise_time .hae) ---

    def _exercise_payload(self, minutes: int) -> dict:
        # AutoSync の apple_exercise_time は秒粒度の瞬間サンプル (qty = 1/60 分) の集合
        return {"data": [
            {"qty": 1 / 60, "unit": "min", "start": _mac(D, 8) + i, "end": _mac(D, 8) + i}
            for i in range(minutes * 60)
        ]}

    def _put_with(self, extra: "dict[tuple[str, date], object]") -> "dict[tuple[str, date], object]":
        payloads = {**self._payloads(), **extra}
        for metric_dir, d in payloads:
            self.put_hae(metric_dir, d)
        return payloads

    def test_autosync_mindful_and_exercise(self):
        payloads = self._put_with({
            ("mindful_minutes", D): {"data": [
                {"start": _mac(D, 7), "end": _mac(D, 7, 24), "qty": 24, "unit": "min"},
            ]},
            ("apple_exercise_time", D): self._exercise_payload(35),
        })
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(FakeMetricFetcher(payloads)).load(D)
        self.assertAlmostEqual(result.health.exercise_minutes, 35.0)
        self.assertEqual(result.activity_detail, "瞑想24分、エクササイズ35分")
        self.assertEqual(result.status, STATUS_COMPLETE)

    def test_autosync_exercise_measured_zero_is_nashi(self):
        # Watch を着けていて (心拍がある) 運動がほぼゼロ (数秒 = round で 0 分) → 「なし」
        payloads = self._put_with({("apple_exercise_time", D): {"data": [
            {"qty": 1 / 60, "unit": "min", "start": _mac(D, 8), "end": _mac(D, 8)},
        ]}})
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(FakeMetricFetcher(payloads)).load(D)
        self.assertEqual(result.activity_detail, ACTIVITY_NONE)
        # W3-7 改訂: 「なし」の根拠は心拍。心拍の .hae が無ければ 0 分の実測があっても書かない
        # (旧: 0 分の実測で「なし」。WAVE3 W3-7 改訂で意図的に変更)
        self._hae("heart_rate", D).unlink()
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(FakeMetricFetcher(payloads)).load(D)
        self.assertAlmostEqual(result.health.exercise_minutes, 1 / 60)
        self.assertIsNone(result.activity_detail)

    def test_autosync_empty_exercise_file_follows_heart_rate(self):
        # 運動ゼロの日は data:[] (旧パーサは None)。W3-7 改訂: 心拍がある (Watch を着けていた)
        # なら「なし」(旧: 運動のデータが無いので常に None。意図的に変更)、無ければ書かない
        payloads = self._put_with({("apple_exercise_time", D): {"data": []}})
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(FakeMetricFetcher(payloads)).load(D)
        self.assertIsNone(result.health.exercise_minutes)
        self.assertEqual(result.activity_detail, ACTIVITY_NONE)

        payloads[("heart_rate", D)] = {"data": []}   # Watch 装着ゼロの日 (心拍も空)
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(FakeMetricFetcher(payloads)).load(D)
        self.assertIsNone(result.health.heart_rate_avg)
        self.assertIsNone(result.activity_detail)

    def test_autosync_heart_rate_not_resolved_does_not_claim_nashi(self):
        # 心拍の .hae を読めない / 途中までの出力 (保留) → Watch を着けていたか分からない → 書かない
        payloads = self._put_with({})
        payloads[("heart_rate", D)] = _FAIL
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(FakeMetricFetcher(payloads)).load(D)
        self.assertEqual(result.status, STATUS_PARTIAL)
        self.assertIsNone(result.activity_detail)

        payloads = self._payloads()
        self.put_hae("heart_rate", D, mtime=_jst(D, 12))
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(FakeMetricFetcher(payloads)).load(D)
        self.assertIn("heart_rate_avg", result.withheld_fields)
        self.assertIsNone(result.activity_detail)

    def test_autosync_unknown_inputs_do_not_claim_nashi_even_with_heart_rate(self):
        # 心拍がある日でも、瞑想 / エクササイズ時間の .hae が保留・読込失敗なら書かない
        payloads = self._put_with({("apple_exercise_time", D): _FAIL})
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(FakeMetricFetcher(payloads)).load(D)
        self.assertAlmostEqual(result.health.heart_rate_avg, 80.0)
        self.assertIsNone(result.activity_detail)

        payloads = self._put_with({("apple_exercise_time", D): {"data": []}})
        self.put_hae("mindful_minutes", D, mtime=_jst(D, 12))
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(FakeMetricFetcher(payloads)).load(D)
        self.assertEqual(result.withheld_fields, ("mindful_sessions", "mindful_minutes"))
        self.assertAlmostEqual(result.health.heart_rate_avg, 80.0)
        self.assertIsNone(result.activity_detail)

    def test_autosync_truncated_exercise_blocks_activity_detail(self):
        payloads = self._put_with({
            ("mindful_minutes", D): {"data": [
                {"start": _mac(D, 7), "end": _mac(D, 7, 24), "qty": 24, "unit": "min"},
            ]},
            ("apple_exercise_time", D): self._exercise_payload(35),
        })
        self.put_hae("apple_exercise_time", D, mtime=_jst(D, 12))   # 停止済みの途中までの出力
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(FakeMetricFetcher(payloads)).load(D)
        self.assertEqual(result.withheld_fields, ("exercise_minutes",))
        self.assertAlmostEqual(result.health.mindful_minutes, 24.0)
        # 「瞑想24分」だけの一覧は書かない (エクササイズ時間が確定しない)
        self.assertIsNone(result.activity_detail)

    def test_autosync_exercise_read_failure_blocks_activity_detail(self):
        payloads = self._put_with({
            ("mindful_minutes", D): {"data": [
                {"start": _mac(D, 7), "end": _mac(D, 7, 24), "qty": 24, "unit": "min"},
            ]},
            ("apple_exercise_time", D): _FAIL,
        })
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(FakeMetricFetcher(payloads)).load(D)
        self.assertEqual(result.status, STATUS_PARTIAL)
        self.assertIsNone(result.activity_detail)

    def test_autosync_truncated_mindful_blocks_activity_detail(self):
        payloads = self._put_with({
            ("mindful_minutes", D): {"data": [
                {"start": _mac(D, 7), "end": _mac(D, 7, 24), "qty": 24, "unit": "min"},
            ]},
            ("apple_exercise_time", D): self._exercise_payload(35),
        })
        self.put_hae("mindful_minutes", D, mtime=_jst(D, 12))
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(FakeMetricFetcher(payloads)).load(D)
        self.assertAlmostEqual(result.health.exercise_minutes, 35.0)
        self.assertIsNone(result.activity_detail)

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
        self.put_hae("step_count", D, mtime=_jst(D, 10))
        self.put_hae("mindful_minutes", D, mtime=_jst(D, 14))
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
            self.put_hae(metric_dir, d, mtime=_jst(d, 11))
        result = self.loader(FakeMetricFetcher(payloads)).load(D)
        self.assertEqual(result.status, STATUS_NODATA)
        self.assertIsNone(result.health)
        self.assertIn("step_count", result.withheld_fields)

    def test_today_autosync_values_are_provisional(self):
        payloads = self._payloads()
        for metric_dir, d in payloads:
            self.put_hae(metric_dir, d)
        self.put_hae("step_count", D, mtime=_jst(D, 10))
        with self.assertLogs(_LOGGER, level="WARNING"):
            result = self.loader(FakeMetricFetcher(payloads),
                                 now=_at(D, 11)).load(D)
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
        self.put_hae("sleep_analysis", D - timedelta(days=1), mtime=_jst(D - timedelta(days=1), 20))
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


@unittest.skipUnless(hasattr(systime, "tzset"), "time.tzset が無い環境")
class TestMachineTimezoneIndependence(LoaderTestBase):
    """W3-2h: 日の終わりの判定がプロセスのローカル tz (Mac の tz / CI の UTC) に依存しない。

    旧実装は naive なローカル時刻で比べていたため、同じファイルでも実行環境の tz で
    「途中経過 / 確定版」の判定が変わっていた (例: JST 21:00 の書き出しは UTC+14 の
    マシンでは「日の終わり後」、JST 翌 05:00 の書き出しは UTC のマシンでは「途中」)。
    """

    #: (tz 名, 2030 年 1 月の UTC offset 時間)。tzdata が無い環境では skip する
    ZONES = (("UTC", 0), ("Asia/Tokyo", 9), ("America/Los_Angeles", -8),
             ("Pacific/Kiritimati", 14))
    AUTOSYNC_DAY = date(2030, 1, 10)

    def _prepare(self) -> FakeMetricFetcher:
        self.json_path = self.put_automation("aggregated/HealthAutoExport-2030-01-15.json")
        a = self.AUTOSYNC_DAY
        payloads: "dict[tuple[str, date], object]" = {
            ("step_count", a): {"data": [
                {"qty": 1200, "unit": "count", "start": _mac(a, 9), "end": _mac(a, 9, 30)},
            ]},
            ("heart_rate", a): {"data": [
                {"avg": 60, "max": 65, "min": 55, "start": _mac(a, 3), "end": _mac(a, 3)},
            ]},
        }
        for metric_dir, d in payloads:
            self.put_hae(metric_dir, d)
        return FakeMetricFetcher(payloads)

    def _outcomes(self, fetcher: FakeMetricFetcher, *, naive_now: bool) -> "list[tuple]":
        a = self.AUTOSYNC_DAY
        later = _at(D + timedelta(days=3), 7)
        scenarios = (
            # (説明, 対象日, (mtime を変える path, mtime), 現在時刻)
            ("json-midday", D, (self.json_path, _jst(D, 21)), _at(D + timedelta(days=1), 7)),
            ("json-final", D, (self.json_path, _jst(D + timedelta(days=1), 5)),
             _at(D + timedelta(days=1), 7)),
            ("json-today", D, (self.json_path, _jst(D, 21)), _at(D, 22)),
            ("hae-final", a, (self._hae("step_count", a), _jst(a + timedelta(days=1), 5)), later),
            ("hae-truncated", a, (self._hae("step_count", a), _jst(a, 21)), later),
        )
        outcomes = []
        for label, d, (path, mtime), now in scenarios:
            os.utime(path, (mtime, mtime))
            current = now.astimezone().replace(tzinfo=None) if naive_now else now
            loader = HealthLoader(self.config(), _LOGGER, now=lambda c=current: c,
                                  metric_fetcher=fetcher)
            result = loader.load(d)
            outcomes.append((
                label, result.status, result.provisional, result.withheld_fields,
                result.health.step_count if result.health is not None else None,
                result.detail,
            ))
        return outcomes

    def _hae(self, metric_dir: str, d: date) -> Path:
        return self.docs / "AutoSync" / "HealthMetrics" / metric_dir / (d.strftime("%Y%m%d") + ".hae")

    def test_outcomes_do_not_depend_on_machine_timezone(self):
        fetcher = self._prepare()
        expected_flags = [
            ("json-midday", STATUS_COMPLETE, True, (), 501),
            ("json-final", STATUS_COMPLETE, False, (), 501),
            ("json-today", STATUS_COMPLETE, True, (), 501),
            ("hae-final", STATUS_COMPLETE, False, (), 1200),
            ("hae-truncated", STATUS_COMPLETE, False, ("step_count",), None),
        ]
        reference: "list[tuple] | None" = None
        probe = _jst(D, 12)
        for name, hours in self.ZONES:
            with self.subTest(zone=name), _machine_tz(name):
                if datetime.fromtimestamp(probe).astimezone().utcoffset() != timedelta(hours=hours):
                    self.skipTest(f"tz {name} を設定できない環境 (tzdata なし)")
                for naive_now in (False, True):
                    outcomes = self._outcomes(fetcher, naive_now=naive_now)
                    self.assertEqual([item[:5] for item in outcomes], expected_flags,
                                     f"zone={name} naive_now={naive_now}")
                    # detail (日の終わり / 最終更新の表記) も tz 非依存
                    if reference is None:
                        reference = outcomes
                    self.assertEqual(outcomes, reference)


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
