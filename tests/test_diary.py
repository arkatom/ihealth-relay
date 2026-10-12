"""``ihealth.diary`` (Obsidian 日記パイプライン) の回帰テスト.

すべて ``tempfile.TemporaryDirectory`` 内の合成 vault / logs で行う。健康・天気・GitHub は
fake を注入し、ネットワーク・実 iCloud・実 vault・Reminders には触れない。値は合成。
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import plistlib
import shutil
import socket
import subprocess
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from unittest import mock

from ihealth.automation import AutomationIndex
from ihealth.config import JST, Config
from ihealth.diary import (
    PROVISIONAL_REVISIT_DAYS,
    DiaryDeps,
    DiaryOptions,
    NetworkGate,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_NODATA,
    STATUS_PARTIAL,
    STATUS_SKIPPED,
    diary_today,
    is_connection_error,
    notify_once_per_day,
    resolve_dates,
    run_diary,
)
from ihealth.errors import ConfigAppError
from ihealth.github_activity import GitHubActivity, GitHubItem
from ihealth.health_loader import HealthLoadResult
from ihealth.models import DailyHealthData
from ihealth.weather import WeatherError, WeatherSummary


PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUTOMATION_FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "automation"

#: 「今日」= 2030-01-16 (JST 08:00)。
NOW = datetime(2030, 1, 15, 23, 0, tzinfo=timezone.utc)
TODAY = date(2030, 1, 16)
D1 = date(2030, 1, 12)
D2 = date(2030, 1, 13)
D3 = date(2030, 1, 14)
YESTERDAY = date(2030, 1, 15)

LOGGER = logging.getLogger("test-diary")


def note_text(d: date, *, steps: str = "", weather: str = "", github: str = "") -> str:
    """実ノート (テンプレート展開後) を模した合成ノート。"""
    return (
        "---\n"
        f"名前: 毎日の習慣 {d.isoformat()}\n"
        f"日付: {d.isoformat()}\n"
        "日記: false\n"
        "瞑想: []\n"
        f"\"歩数 (歩)\":{(' ' + steps) if steps else ''}\n"
        "\"睡眠時間 (分)\":\n"
        "気温:\n"
        "湿度:\n"
        "気圧:\n"
        f"天気:{(' ' + weather) if weather else ''}\n"
        "日照時間:\n"
        "降水量:\n"
        "アクティビティ詳細:\n"
        "参照URLs: \"[サンプル](https://example.com/a)\"\n"
        f"Github:{(' ' + github) if github else ''}\n"
        "---\n"
        f"# 毎日の習慣 {d.isoformat()}\n"
        "\n"
        "## なんでもメモ\n"
        "- 合成メモ\n"
    )


def body_of(text: str) -> str:
    return text.split("\n---\n", 1)[1]


def health_ok(d: date, *, steps: int = 4321, provisional: bool = False) -> HealthLoadResult:
    return HealthLoadResult(
        health=DailyHealthData(date=d, step_count=steps, sleep_hours=7.5),
        activity_detail="瞑想5分",
        source="automation",
        detail="synthetic",
        status=STATUS_COMPLETE,
        provisional=provisional,
    )


def health_failed(d: date) -> HealthLoadResult:
    return HealthLoadResult(
        health=None, activity_detail=None, source="automation",
        detail="synthetic failure", status=STATUS_FAILED,
        errors=("automation HealthAutoExport-x.json: JSON 構文エラー",),
    )


def health_nodata(d: date) -> HealthLoadResult:
    return HealthLoadResult(
        health=None, activity_detail=None, source="none",
        detail="no file", status=STATUS_NODATA,
    )


def weather_ok(d: date) -> WeatherSummary:
    return WeatherSummary(
        date=d,
        weather="3時: 晴れ☀️, 6時: 曇☁️",
        temperature="朝:平均5.0℃, 昼:平均9.0℃, 夜:平均3.0℃（最高:10.0℃, 最低:1.0℃）",
        humidity="朝:平均60.0%, 昼:平均40.0%, 夜:平均70.0%（最高:80%, 最低:30%）",
        precipitation="朝:0.0mm, 昼:0.0mm, 夜:0.5mm",
        pressure="1-6時:平均1015.0hPa, 7-12時:平均1014.0hPa",
        sunshine="6.0時間",
        is_complete=True,
        observed_hours=24,
    )


def weather_incomplete(d: date, *, pending: bool) -> WeatherSummary:
    return WeatherSummary(
        date=d, weather=None, temperature=None, humidity=None, precipitation=None,
        pressure=None, sunshine=None, is_complete=False, observed_hours=20, pending=pending,
    )


def github_ok(dates: "list[date]") -> GitHubActivity:
    days = {d: [] for d in dates}
    days[dates[0]] = [GitHubItem(
        kind="pr", repo="demo", url="https://example.com/demo/pull/1", number=1,
        title="Synthetic change",
    )]
    return GitHubActivity(days=days, complete=True, repos_scanned=1, owners_listed=1)


def github_failed(dates: "list[date]") -> GitHubActivity:
    return GitHubActivity(
        days={d: [] for d in dates}, complete=False,
        errors=["viewer / organizations の取得に失敗: GitHub API HTTP 401"],
        owners_listed=0,
    )


class FakeLoader:
    def __init__(
        self,
        results: "dict[date, HealthLoadResult] | Callable[[date], HealthLoadResult]",
        *,
        latest: "date | None" = TODAY,
        index_errors: "tuple[str, ...]" = (),
    ) -> None:
        self.results = results
        self.latest = latest
        self.index_errors = index_errors
        self.calls: "list[date]" = []

    def load(self, d: date) -> HealthLoadResult:
        self.calls.append(d)
        if callable(self.results):
            return self.results(d)
        return self.results.get(d, health_nodata(d))

    def index(self) -> AutomationIndex:
        files = {}
        if self.latest is not None:
            files[self.latest] = (Path(f"/synthetic/HealthAutoExport-{self.latest}.json"),)
        return AutomationIndex(files=files, errors=self.index_errors)


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: "list[float]" = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class DiaryTestBase(unittest.TestCase):
    """合成 vault + fake source で run_diary を動かす土台。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.vault = self.root / "vault"
        self.diary = self.vault / "日記"
        self.diary.mkdir(parents=True)
        (self.diary / "2030-Q1").mkdir()
        self.logs = self.root / "logs"
        self.archive = self.root / "data" / "health"
        self.archive.mkdir(parents=True)
        # 既定値と違う値にしておく (配線の検査が「既定値どうしの比較」で素通りしないように)
        self.config = Config(
            notion_secret=None,
            database_id=None,
            health_export_dir=self.root / "hae",
            archive_root=self.archive,
            birth_date=date(1990, 1, 1),
            obsidian_vault_dir=self.vault,
            logs_dir=self.logs,
            github_token="synthetic-token",
            jma_prec_no=11,
            jma_block_no=47401,
            github_owners="sample-owner,sample-org",
            github_exclude_repos="sample-owner/skip-me",
        )
        self.time = FakeTime()
        #: 気象庁の日付 (JST) での今日 (fake の weather_fetcher が受け取るはずの値)
        self.weather_today = TODAY
        self.notifications: "list[tuple[str, str]]" = []
        self.probe_calls: "list[str]" = []
        self.probe_results: "list[bool]" = []
        self.weather_calls: "list[date]" = []
        self.collector_calls: "list[tuple[date, date]]" = []
        # config.toml は実リポジトリのものを読まない
        patcher = mock.patch("ihealth.config_file.find_config_path", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---- helpers ----

    def write_note(self, d: date, text: "str | None" = None, *, sub: bool = False) -> Path:
        folder = self.diary / "2030-Q1" if sub else self.diary
        path = folder / f"{d.isoformat()}.md"
        path.write_text(text if text is not None else note_text(d), encoding="utf-8")
        return path

    def probe(self, host: str) -> bool:
        self.probe_calls.append(host)
        if self.probe_results:
            return self.probe_results.pop(0)
        return True

    def notify(self, title: str, body: str) -> None:
        self.notifications.append((title, body))

    def deps(
        self,
        *,
        loader: "FakeLoader | None" = None,
        weather: "Callable[..., WeatherSummary] | None" = None,
        github: "Callable[..., GitHubActivity] | None" = None,
        token: "str | None" = "synthetic-token",
    ) -> DiaryDeps:
        # 既定は全日付「取得成功」(個別のテストで失敗・nodata を差し込む)
        loader = loader or FakeLoader(lambda d: health_ok(d, provisional=d >= TODAY))

        def weather_fetcher(d: date, **kwargs: Any) -> WeatherSummary:
            self.weather_calls.append(d)
            self.assertEqual(kwargs["today"], self.weather_today)
            self.assertEqual(kwargs["prec_no"], self.config.jma_prec_no)
            self.assertEqual(kwargs["block_no"], self.config.jma_block_no)
            if weather is None:
                return weather_ok(d)
            return weather(d, **kwargs)

        def collector(client: Any, start: date, end: date, **kwargs: Any) -> GitHubActivity:
            self.collector_calls.append((start, end))
            self.assertEqual(kwargs["tz"], self.config.diary_tz)
            self.assertEqual(kwargs["owners"], self.config.github_owners)
            self.assertEqual(kwargs["exclude_repos"], self.config.github_exclude_repos)
            dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
            if github is None:
                return github_ok(dates)
            return github(client, start, end, **kwargs)

        return DiaryDeps(
            health_loader_factory=lambda config, logger, now: loader,
            weather_fetcher=weather_fetcher,
            github_token_resolver=lambda env_token, logger: token,
            github_client_factory=lambda tok, logger, opener: object(),
            github_collector=collector,
            notifier=self.notify,
            network_probe=self.probe,
            clock=lambda: NOW,
            monotonic=self.time.monotonic,
            sleep=self.time.sleep,
        )

    def run_diary(
        self,
        dates: "tuple[date, ...]",
        deps: DiaryDeps,
        *,
        sources: "tuple[str, ...]" = ("health", "weather", "github"),
        dry_run: bool = False,
        fill_empty_only: bool = False,
        config: "Config | None" = None,
    ):
        options = DiaryOptions(
            dates=dates, today=TODAY, sources=sources, dry_run=dry_run,
            fill_empty_only=fill_empty_only,
        )
        return run_diary(config or self.config, options, LOGGER, deps)

    def read(self, d: date, *, sub: bool = False) -> str:
        folder = self.diary / "2030-Q1" if sub else self.diary
        return (folder / f"{d.isoformat()}.md").read_text(encoding="utf-8")


# ======================================================================
# 期間
# ======================================================================


class ResolveDatesTest(unittest.TestCase):
    def test_default_window_is_lookback_plus_today(self):
        dates = resolve_dates(today=TODAY, lookback_days=3, include_today=True)
        self.assertEqual(dates, (date(2030, 1, 13), D3, YESTERDAY, TODAY))

    def test_default_window_without_today(self):
        dates = resolve_dates(today=TODAY, lookback_days=2, include_today=False)
        self.assertEqual(dates, (D3, YESTERDAY))

    def test_default_lookback_seven(self):
        dates = resolve_dates(today=TODAY)
        self.assertEqual(len(dates), 8)
        self.assertEqual(dates[0], TODAY - timedelta(days=7))
        self.assertEqual(dates[-2:], (YESTERDAY, TODAY))

    def test_single_date(self):
        self.assertEqual(resolve_dates(today=TODAY, date_arg=D1), (D1,))
        self.assertEqual(resolve_dates(today=TODAY, date_arg=TODAY), (TODAY,))

    def test_range_inclusive(self):
        self.assertEqual(
            resolve_dates(today=TODAY, from_date=D1, to_date=D3), (D1, D2, D3),
        )

    def test_from_without_to_runs_until_yesterday(self):
        self.assertEqual(
            resolve_dates(today=TODAY, from_date=D3), (D3, YESTERDAY),
        )

    def test_errors(self):
        cases = [
            dict(date_arg=TODAY + timedelta(days=1)),
            dict(date_arg=D1, from_date=D1),
            dict(date_arg=D1, to_date=D2),
            dict(to_date=D2),
            dict(from_date=D3, to_date=D1),
            dict(from_date=D1, to_date=TODAY + timedelta(days=1)),
            dict(from_date=TODAY),  # --to 省略 = 昨日まで → 空
            dict(from_date=date(26, 1, 1), to_date=D1),  # 年の打ち間違い
            dict(lookback_days=0, include_today=False),
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    resolve_dates(today=TODAY, **kwargs)

    def test_diary_today_uses_configured_tz(self):
        clock = lambda: datetime(2030, 1, 15, 15, 30, tzinfo=timezone.utc)  # noqa: E731
        self.assertEqual(diary_today(clock, JST), TODAY)
        self.assertEqual(diary_today(clock, timezone.utc), YESTERDAY)


# ======================================================================
# 3 source × 成功 / 失敗
# ======================================================================


class SourceCombinationTest(DiaryTestBase):
    """health / weather / github の成功・失敗 8 通りで、成功した source だけが書かれる。"""

    def test_eight_combinations(self):
        dates = (D2, D3)
        for health_ok_, weather_ok_, github_ok_ in itertools.product((True, False), repeat=3):
            with self.subTest(health=health_ok_, weather=weather_ok_, github=github_ok_):
                for d in dates:
                    self.write_note(d)
                self.notifications.clear()
                for marker in self.logs.glob(".*notified"):
                    marker.unlink()
                loader = FakeLoader(
                    lambda d: health_ok(d) if health_ok_ else health_failed(d),
                )

                def weather(d: date, **kwargs: Any) -> WeatherSummary:
                    if not weather_ok_:
                        raise WeatherError("気象庁ページの取得に失敗しました (HTTP 500): synthetic")
                    return weather_ok(d)

                def github(client: Any, start: date, end: date, **kwargs: Any) -> GitHubActivity:
                    span = [start + timedelta(days=i) for i in range((end - start).days + 1)]
                    return github_ok(span) if github_ok_ else github_failed(span)

                result = self.run_diary(dates, self.deps(loader=loader, weather=weather, github=github))

                for d in dates:
                    text = self.read(d)
                    self.assertEqual(body_of(text), body_of(note_text(d)), "本文は不変")
                    self.assertIn("日記: false\n", text)
                    self.assertIn('参照URLs: "[サンプル](https://example.com/a)"\n', text)
                    self.assertEqual('"歩数 (歩)": 4321\n' in text, health_ok_)
                    self.assertEqual('アクティビティ詳細: "瞑想5分"\n' in text, health_ok_)
                    self.assertEqual('日照時間: "6.0時間"\n' in text, weather_ok_)
                    self.assertEqual("Github: " in text, github_ok_)
                day = result.days[0]
                self.assertEqual(day.sources["health"], STATUS_COMPLETE if health_ok_ else STATUS_FAILED)
                self.assertEqual(day.sources["weather"], STATUS_COMPLETE if weather_ok_ else STATUS_FAILED)
                self.assertEqual(day.sources["github"], STATUS_COMPLETE if github_ok_ else STATUS_FAILED)

                failed_sources = {f.source for f in result.failures}
                expected_failed = {
                    name for name, ok in (
                        ("health", health_ok_), ("weather", weather_ok_), ("github", github_ok_),
                    ) if not ok
                }
                self.assertEqual(failed_sources, expected_failed)
                if not expected_failed:
                    self.assertEqual(result.exit_code, 0)
                    self.assertEqual(self.notifications, [])
                elif len(expected_failed) == 3:
                    self.assertEqual(result.exit_code, 11)  # 全日付・全 source が失敗
                    self.assertEqual(len(self.notifications), 1)
                else:
                    self.assertEqual(result.exit_code, 12)
                    self.assertEqual(len(self.notifications), 1)
                if expected_failed:
                    title, body = self.notifications[0]
                    for name in expected_failed:
                        self.assertIn(f"[{name}]", body)
                # レポート: 失敗した source のキーは failed
                keys = result.days[0].keys
                self.assertEqual(keys["歩数 (歩)"]["action"], "update" if health_ok_ else "failed")
                self.assertEqual(keys["天気"]["action"], "update" if weather_ok_ else "failed")
                self.assertEqual(keys["Github"]["action"], "update" if github_ok_ else "failed")

    def test_github_text_and_format(self):
        self.write_note(D3)
        result = self.run_diary((D3,), self.deps(), sources=("github",))
        self.assertEqual(result.exit_code, 0)
        self.assertIn(
            'Github: "[🔀 demo:PR #1: Synthetic change](https://example.com/demo/pull/1)"\n',
            self.read(D3),
        )
        self.assertEqual(self.collector_calls, [(D3, D3)])

    def test_github_no_activity_writes_gaitou_nashi(self):
        self.write_note(D2)
        self.write_note(D3)
        result = self.run_diary((D2, D3), self.deps(), sources=("github",))
        self.assertEqual(result.exit_code, 0)
        self.assertIn('Github: "該当なし"\n', self.read(D3))
        self.assertEqual(self.collector_calls, [(D2, D3)], "期間で 1 回だけ収集")


# ======================================================================
# 個別の振る舞い
# ======================================================================


class DiaryBehaviourTest(DiaryTestBase):
    def test_note_in_subfolder_is_updated_in_place(self):
        path = self.write_note(D2, sub=True)
        self.run_diary((D2,), self.deps(), sources=("health",))
        self.assertIn('"歩数 (歩)": 4321\n', path.read_text(encoding="utf-8"))
        self.assertFalse((self.diary / f"{D2.isoformat()}.md").exists())

    def test_backup_written_outside_vault(self):
        self.write_note(D2)
        result = self.run_diary((D2,), self.deps(), sources=("health",))
        backup = result.days[0].backup_path
        self.assertIsNotNone(backup)
        self.assertTrue(str(backup).startswith(str(self.root / "data" / "obsidian-backup")))
        self.assertEqual(Path(backup).read_text(encoding="utf-8"), note_text(D2))

    def test_today_is_provisional_and_weather_not_fetched(self):
        self.write_note(YESTERDAY)
        self.write_note(TODAY)
        loader = FakeLoader({
            YESTERDAY: health_ok(YESTERDAY, steps=8000),
            TODAY: health_ok(TODAY, steps=1200, provisional=True),
        })
        result = self.run_diary((YESTERDAY, TODAY), self.deps(loader=loader))
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.weather_calls, [YESTERDAY], "当日の天気ページは取りに行かない")
        today = result.days[1]
        self.assertTrue(today.provisional)
        self.assertEqual(today.sources["weather"], STATUS_NODATA)
        self.assertEqual(today.keys["天気"]["action"], "missing")
        self.assertEqual(today.keys["天気"]["reason"], "not_published")
        self.assertIn('"歩数 (歩)": 1200\n', self.read(TODAY))
        self.assertIn('天気: "3時: 晴れ☀️, 6時: 曇☁️"\n', self.read(YESTERDAY))
        self.assertNotIn("天気: \"", self.read(TODAY))

    def test_missing_recent_note_is_not_created(self):
        # 今日のノートが無い: 作成制限 (今日・昨日は作らない) で skipped 扱い、失敗ではない
        template = self.vault / "テンプレート.md"
        template.write_text("---\n日付: <% dateStr %>\n---\n", encoding="utf-8")
        config = replace(self.config, obsidian_template=Path("テンプレート.md"))
        loader = FakeLoader({TODAY: health_ok(TODAY, provisional=True)})
        result = self.run_diary((TODAY,), self.deps(loader=loader), sources=("health",), config=config)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.days[0].note_status, "missing")
        self.assertEqual(result.days[0].note_reason, "too_recent")
        self.assertEqual(result.days[0].keys["歩数 (歩)"]["action"], "skipped")
        self.assertEqual(result.days[0].keys["歩数 (歩)"]["new"], "4321")
        self.assertFalse((self.diary / f"{TODAY.isoformat()}.md").exists())

    def test_missing_old_note_created_from_template_next_to_neighbour(self):
        self.write_note(D1, sub=True)
        template = self.vault / "テンプレート.md"
        template.write_text(
            "<%*\nconst x = 1;\n-%>\n---\n日付: <% dateStr %>\n\"歩数 (歩)\":\n---\n# <% dateStr %>（<% weekday %>）\n",
            encoding="utf-8",
        )
        config = replace(self.config, obsidian_template=Path("テンプレート.md"))
        result = self.run_diary((D2,), self.deps(), sources=("health",), config=config)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.days[0].note_status, "created")
        created = self.read(D2, sub=True)
        self.assertIn(f"日付: {D2.isoformat()}\n", created)
        self.assertIn('"歩数 (歩)": 4321\n', created)

    def test_without_template_missing_note_is_reported_not_failed(self):
        loader = FakeLoader({D1: health_ok(D1)})
        result = self.run_diary((D1,), self.deps(loader=loader), sources=("health",))
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.days[0].note_status, "missing")
        self.assertEqual(result.days[0].note_reason, "create_disabled")
        self.assertEqual(self.notifications, [])

    def test_unprocessed_template_note_needs_attention(self):
        raw = "<%* const x = 1; -%>\n# <% tp.file.title %>\n本文\n"
        self.write_note(D2, raw)
        result = self.run_diary((D2,), self.deps(), sources=("health",))
        self.assertEqual(result.exit_code, 12)
        self.assertEqual(result.days[0].note_status, "skipped")
        self.assertEqual(result.days[0].note_reason, "unprocessed_template")
        self.assertEqual(self.read(D2), raw, "未処理テンプレートのノートには触らない")
        self.assertEqual([f.kind for f in result.failures], ["attention"])
        self.assertEqual(len(self.notifications), 1)
        self.assertIn("確認が必要", self.notifications[0][0])
        self.assertIn("Templater", self.notifications[0][1])

    def test_unusable_template_needs_attention(self):
        config = replace(self.config, obsidian_template=Path("テンプレート/無い.md"))
        result = self.run_diary((D1,), self.deps(), sources=("health",), config=config)
        self.assertEqual(result.days[0].note_status, "missing")
        self.assertEqual(result.days[0].note_reason, "template_unavailable")
        self.assertEqual(result.exit_code, 12)
        self.assertIn("OBSIDIAN_TEMPLATE", self.notifications[0][1])

    def test_health_partial_writes_values_and_reports_failure(self):
        self.write_note(D2)
        partial = HealthLoadResult(
            health=DailyHealthData(date=D2, step_count=500),
            activity_detail=None,
            source="automation",
            detail="synthetic",
            status=STATUS_PARTIAL,
            errors=("heart_rate: 未知の単位",),
            withheld_fields=("sleep_hours",),
        )
        result = self.run_diary((D2,), self.deps(loader=FakeLoader({D2: partial})), sources=("health",))
        self.assertEqual(result.exit_code, 12)
        self.assertIn('"歩数 (歩)": 500\n', self.read(D2))
        self.assertEqual(result.days[0].keys["睡眠時間 (分)"]["reason"], "not_final")
        self.assertEqual(len(self.notifications), 1)
        self.assertIn("未知の単位", self.notifications[0][1])

    def test_nodata_is_quiet(self):
        self.write_note(D2)
        result = self.run_diary((D2,), self.deps(loader=FakeLoader({})), sources=("health",))
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.notifications, [])
        self.assertEqual(self.read(D2), note_text(D2))
        self.assertEqual(result.days[0].keys["歩数 (歩)"]["action"], "missing")

    def test_sources_not_selected_are_skipped(self):
        self.write_note(D2)
        result = self.run_diary((D2,), self.deps(), sources=("weather",))
        self.assertEqual(result.days[0].sources["health"], STATUS_SKIPPED)
        self.assertEqual(result.days[0].sources["github"], STATUS_SKIPPED)
        self.assertEqual(result.days[0].keys["歩数 (歩)"]["action"], "skipped")
        self.assertEqual(result.days[0].keys["歩数 (歩)"]["reason"], "source_not_selected")
        self.assertEqual(self.collector_calls, [])
        self.assertNotIn('"歩数 (歩)": 4321', self.read(D2))

    def test_weather_requests_are_spaced(self):
        for d in (D1, D2, D3):
            self.write_note(d)
        self.run_diary((D1, D2, D3), self.deps(), sources=("weather",))
        self.assertEqual(self.weather_calls, [D1, D2, D3])
        self.assertEqual(len(self.time.sleeps), 2)
        self.assertTrue(all(s >= 1.0 for s in self.time.sleeps))

    def test_weather_partial_complete_day_writes_present_keys(self):
        self.write_note(D2)

        def weather(d: date, **kwargs: Any) -> WeatherSummary:
            return replace(weather_ok(d), sunshine=None)

        result = self.run_diary((D2,), self.deps(weather=weather), sources=("weather",))
        self.assertEqual(result.exit_code, 0, "欠測キーは失敗ではない")
        text = self.read(D2)
        self.assertIn('天気: "3時: 晴れ☀️, 6時: 曇☁️"\n', text)
        self.assertIn("日照時間:\n", text)
        self.assertEqual(result.days[0].keys["日照時間"]["action"], "missing")

    def test_single_incomplete_weather_day_is_not_a_failure(self):
        for d in (D1, D2, D3):
            self.write_note(d)

        def weather(d: date, **kwargs: Any) -> WeatherSummary:
            return weather_incomplete(d, pending=False) if d == D2 else weather_ok(d)

        result = self.run_diary((D1, D2, D3), self.deps(weather=weather), sources=("weather",))
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.days[1].sources["weather"], STATUS_PARTIAL)
        self.assertEqual(result.days[1].keys["天気"]["reason"], "incomplete_day")
        self.assertEqual(self.notifications, [])

    def test_two_consecutive_incomplete_weather_days_fail(self):
        for d in (D1, D2, D3):
            self.write_note(d)

        def weather(d: date, **kwargs: Any) -> WeatherSummary:
            return weather_incomplete(d, pending=False) if d in (D2, D3) else weather_ok(d)

        result = self.run_diary((D1, D2, D3), self.deps(weather=weather), sources=("weather",))
        self.assertEqual(result.exit_code, 12)
        self.assertEqual({f.date for f in result.failures}, {D2, D3})
        self.assertEqual(len(self.notifications), 1)
        self.assertIn("2 日以上続けて", self.notifications[0][1])

    def test_incomplete_day_is_never_written(self):
        # 未完成の日は (万一値が入っていても) 書かない: 途中までの集計で既存値を上書きしない
        self.write_note(D2)

        def weather(d: date, **kwargs: Any) -> WeatherSummary:
            return replace(weather_ok(d), is_complete=False, pending=False)

        result = self.run_diary((D2,), self.deps(weather=weather), sources=("weather",))
        self.assertEqual(self.read(D2), note_text(D2))
        self.assertEqual(result.days[0].keys["天気"]["action"], "missing")

    def test_pending_weather_is_quiet(self):
        self.write_note(YESTERDAY)

        def weather(d: date, **kwargs: Any) -> WeatherSummary:
            return weather_incomplete(d, pending=True)

        result = self.run_diary((YESTERDAY,), self.deps(weather=weather), sources=("weather",))
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.days[0].sources["weather"], STATUS_NODATA)
        self.assertEqual(self.read(YESTERDAY), note_text(YESTERDAY))

    def test_github_partial_is_not_written_and_fails(self):
        self.write_note(D2, note_text(D2, github='"既存の値"'))

        def github(client: Any, start: date, end: date, **kwargs: Any) -> GitHubActivity:
            return GitHubActivity(
                days={start: []}, complete=False, errors=["owner/repo: HTTP 502"],
                repos_scanned=3, repos_failed=1, owners_listed=1,
            )

        result = self.run_diary((D2,), self.deps(github=github), sources=("github",))
        self.assertEqual(result.exit_code, 12)
        self.assertIn('Github: "既存の値"\n', self.read(D2))
        self.assertEqual(result.days[0].keys["Github"]["action"], "failed")
        self.assertEqual(result.days[0].keys["Github"]["reason"], "source_incomplete")
        self.assertEqual(result.report["github"]["repos_failed"], 1)

    def test_github_config_error_is_reported(self):
        self.write_note(D2)

        def github(client: Any, start: date, end: date, **kwargs: Any) -> GitHubActivity:
            raise ValueError("GITHUB_OWNERS に不正な owner があります: 'a b'")

        result = self.run_diary((D2,), self.deps(github=github), sources=("github",))
        self.assertEqual(result.exit_code, 11)  # 選択した唯一の source が全日付で失敗
        self.assertEqual(result.days[0].sources["github"], STATUS_FAILED)
        self.assertIn("GITHUB_OWNERS", self.notifications[0][1])

    def test_github_missing_token_fails_without_collecting(self):
        self.write_note(D2)
        result = self.run_diary((D2,), self.deps(token=None), sources=("health", "github"))
        self.assertEqual(result.exit_code, 12)
        self.assertEqual(self.collector_calls, [])
        self.assertEqual(result.report["github"]["status"], STATUS_FAILED)
        self.assertIn("token", self.notifications[0][1])
        self.assertNotIn("synthetic-token", json.dumps(result.report, ensure_ascii=False))

    def test_write_failure_exit_11_and_other_days_continue(self):
        self.write_note(D2, "---\n日付: x\n")  # 閉じ --- 欠落 → 安全に書けない
        self.write_note(D3)
        result = self.run_diary((D2, D3), self.deps(), sources=("health",))
        self.assertEqual(result.exit_code, 11)
        self.assertEqual(result.days[0].note_status, "failed")
        self.assertEqual(result.days[0].keys["歩数 (歩)"]["action"], "failed")
        self.assertEqual(result.days[0].keys["歩数 (歩)"]["reason"], "write_failed")
        self.assertEqual(self.read(D2), "---\n日付: x\n", "壊れたノートには触らない")
        self.assertIn('"歩数 (歩)": 4321\n', self.read(D3))
        self.assertEqual(len(self.notifications), 1)
        self.assertIn("書き込み失敗", self.notifications[0][0])

    def test_unexpected_source_exception_is_isolated(self):
        self.write_note(D2)

        def weather(d: date, **kwargs: Any) -> WeatherSummary:
            raise RuntimeError("boom")

        with self.assertLogs("test-diary", level="ERROR") as logs:
            result = self.run_diary((D2,), self.deps(weather=weather))
        self.assertIn("RuntimeError: boom", "\n".join(logs.output))
        self.assertEqual(result.exit_code, 12)
        self.assertIn('"歩数 (歩)": 4321\n', self.read(D2))
        self.assertEqual(result.days[0].sources["weather"], STATUS_FAILED)

    def test_vault_missing_is_config_error(self):
        config = replace(self.config, obsidian_vault_dir=self.root / "no-vault")
        with self.assertRaises(ConfigAppError):
            self.run_diary((D2,), self.deps(), config=config)

    def test_vault_unset_is_config_error(self):
        config = replace(self.config, obsidian_vault_dir=None)
        with self.assertRaises(ConfigAppError):
            self.run_diary((D2,), self.deps(), config=config)


# ======================================================================
# dry-run / fill-empty-only / レポート
# ======================================================================


class DryRunAndReportTest(DiaryTestBase):
    def test_dry_run_writes_nothing_to_vault(self):
        path = self.write_note(D2)
        before = path.read_bytes()
        mtime = path.stat().st_mtime_ns
        result = self.run_diary((D2,), self.deps(), dry_run=True)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        self.assertEqual(sorted(p.name for p in self.diary.iterdir()), ["2030-01-13.md", "2030-Q1"])
        self.assertFalse((self.root / "data" / "obsidian-backup").exists())
        self.assertEqual(result.days[0].note_status, "would_update")
        self.assertIn("歩数 (歩)", result.days[0].written_keys)
        self.assertTrue(result.report["dry_run"])
        self.assertEqual(result.report["dates"][0]["keys"]["歩数 (歩)"]["action"], "update")

    def test_dry_run_does_not_create_notes(self):
        template = self.vault / "テンプレート.md"
        template.write_text("---\n日付: <% dateStr %>\n---\n", encoding="utf-8")
        config = replace(self.config, obsidian_template=Path("テンプレート.md"))
        result = self.run_diary((D1,), self.deps(), dry_run=True, config=config)
        self.assertEqual(result.days[0].note_status, "would_create")
        self.assertFalse((self.diary / f"{D1.isoformat()}.md").exists())

    def test_fill_empty_only_keeps_and_reports_both_values(self):
        self.write_note(D2, note_text(D2, steps="999", weather='"既存の天気"'))
        result = self.run_diary((D2,), self.deps(), fill_empty_only=True)
        text = self.read(D2)
        self.assertIn('"歩数 (歩)": 999\n', text)
        self.assertIn('天気: "既存の天気"\n', text)
        self.assertIn('気温: "朝:平均5.0℃', text, "空のキーは埋める")
        keys = result.report["dates"][0]["keys"]
        self.assertEqual(keys["歩数 (歩)"]["action"], "kept")
        self.assertEqual(keys["歩数 (歩)"]["old"], "999")
        self.assertEqual(keys["歩数 (歩)"]["new"], "4321")
        self.assertEqual(keys["天気"]["action"], "kept")
        self.assertEqual(keys["天気"]["old"], '"既存の天気"')
        self.assertEqual(keys["気温"]["action"], "update")

    def test_report_file_structure(self):
        self.write_note(D2)
        result = self.run_diary((D2,), self.deps())
        self.assertIsNotNone(result.report_path)
        self.assertEqual(result.report_path.parent, self.logs)
        self.assertTrue(result.report_path.name.startswith("diary-report-20300115T230000Z"))
        data = json.loads(result.report_path.read_text(encoding="utf-8"))
        self.assertEqual(data["publisher"], "obsidian")
        self.assertEqual(data["window"], {"from": "2030-01-13", "to": "2030-01-13", "days": 1})
        self.assertEqual(data["exit_code"], 0)
        day = data["dates"][0]
        self.assertEqual(day["note"]["status"], "updated")
        self.assertEqual(set(day["sources"]), {"health", "weather", "github"})
        self.assertEqual(day["sources"]["health"]["status"], "complete")
        self.assertEqual(day["keys"]["歩数 (歩)"]["action"], "update")
        self.assertEqual(day["keys"]["歩数 (歩)"]["source"], "health")
        self.assertEqual(len(day["keys"]), 23)

    def test_unchanged_on_second_run(self):
        self.write_note(D2)
        self.run_diary((D2,), self.deps())
        first = self.read(D2)
        result = self.run_diary((D2,), self.deps())
        self.assertEqual(self.read(D2), first)
        self.assertEqual(result.days[0].note_status, "unchanged")
        self.assertEqual(result.days[0].written_keys, ())

    def test_old_reports_are_pruned(self):
        self.logs.mkdir(parents=True)
        old = self.logs / "diary-report-20291201T000000Z.json"
        old.write_text("{}", encoding="utf-8")
        recent = self.logs / "diary-report-20300110T000000Z.json"
        recent.write_text("{}", encoding="utf-8")
        other = self.logs / "run.log"
        other.write_text("x", encoding="utf-8")
        self.write_note(D2)
        self.run_diary((D2,), self.deps(), sources=("health",))
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(other.exists())

    def test_report_name_collision_gets_suffix(self):
        self.write_note(D2)
        first = self.run_diary((D2,), self.deps(), sources=("health",))
        second = self.run_diary((D2,), self.deps(), sources=("health",))
        self.assertNotEqual(first.report_path, second.report_path)
        self.assertTrue(second.report_path.name.endswith("-1.json"))


# ======================================================================
# 通知の集約・抑制 / 停止検知
# ======================================================================


class NotificationTest(DiaryTestBase):
    def _failing_deps(self) -> DiaryDeps:
        loader = FakeLoader({D2: health_failed(D2), D3: health_failed(D3)})

        def weather(d: date, **kwargs: Any) -> WeatherSummary:
            raise WeatherError("気象庁ページの取得に失敗しました (HTTP 503): synthetic")

        return self.deps(loader=loader, weather=weather)

    def test_failures_aggregate_into_one_notification(self):
        self.write_note(D2)
        self.write_note(D3)
        result = self.run_diary((D2, D3), self._failing_deps())
        self.assertEqual(result.exit_code, 12)
        self.assertEqual(len(self.notifications), 1)
        title, body = self.notifications[0]
        self.assertIn("health", title)
        self.assertIn("weather", title)
        self.assertIn("[health] 2030-01-13〜2030-01-14", body)
        self.assertIn("[weather] 2030-01-13〜2030-01-14", body)
        self.assertIn(str(result.report_path), body)
        self.assertEqual(result.notifications, tuple(self.notifications))

    def test_same_failure_notified_once_per_day(self):
        self.write_note(D2)
        self.write_note(D3)
        self.run_diary((D2, D3), self._failing_deps())
        second = self.run_diary((D2, D3), self._failing_deps())
        self.assertEqual(len(self.notifications), 1, "同じ内容は同じ日に 2 回通知しない")
        self.assertEqual(second.exit_code, 12, "通知を省略しても終了コードは失敗のまま")
        self.assertTrue(second.report["notifications"]["failure"]["suppressed"])

    def test_different_failure_is_notified_again(self):
        self.write_note(D2)
        self.write_note(D3)
        self.run_diary((D2, D3), self._failing_deps())
        self.run_diary((D2,), self._failing_deps())
        self.assertEqual(len(self.notifications), 2)

    def test_notification_failure_does_not_change_exit_code(self):
        self.write_note(D2)
        deps = self._failing_deps()

        def broken_notifier(title: str, body: str) -> None:
            raise OSError("osascript missing")

        deps.notifier = broken_notifier
        with self.assertLogs("test-diary", level="ERROR"):
            result = self.run_diary((D2,), deps)
        self.assertEqual(result.exit_code, 12)
        self.assertEqual(result.notifications, ())
        self.assertFalse((self.logs / ".failure_notified").exists())

    def test_stale_automation_notified_once_per_day(self):
        self.write_note(D3)
        loader = FakeLoader({D3: health_ok(D3)}, latest=TODAY - timedelta(days=4))
        result = self.run_diary((D3,), self.deps(loader=loader), sources=("health",))
        self.assertEqual(result.exit_code, 0, "停止検知は終了コードを変えない")
        self.assertEqual(len(self.notifications), 1)
        self.assertIn("停止の疑い", self.notifications[0][0])
        self.assertIn("2030-01-12", self.notifications[0][1])
        self.assertEqual((self.logs / ".stale_notified").read_text(encoding="utf-8").strip(), "2030-01-16")
        self.run_diary((D3,), self.deps(loader=loader), sources=("health",))
        self.assertEqual(len(self.notifications), 1)

    def test_recent_automation_is_not_stale(self):
        self.write_note(D3)
        loader = FakeLoader({D3: health_ok(D3)}, latest=TODAY - timedelta(days=3))
        result = self.run_diary((D3,), self.deps(loader=loader), sources=("health",))
        self.assertEqual(self.notifications, [])
        self.assertFalse(result.report["stale_check"]["stale"])

    def test_stale_check_skipped_when_index_has_errors(self):
        self.write_note(D3)
        loader = FakeLoader(
            {D3: health_ok(D3)}, latest=TODAY - timedelta(days=10),
            index_errors=("ディレクトリを読めません: /synthetic",),
        )
        result = self.run_diary((D3,), self.deps(loader=loader), sources=("health",))
        self.assertEqual(self.notifications, [])
        self.assertFalse(result.report["stale_check"]["stale"])

    def test_stale_check_needs_health_source(self):
        self.write_note(D3)
        loader = FakeLoader({}, latest=TODAY - timedelta(days=30))
        result = self.run_diary((D3,), self.deps(loader=loader), sources=("weather",))
        self.assertIsNone(result.report["stale_check"])
        self.assertEqual(self.notifications, [])


# ======================================================================
# ネットワーク待ち (スリープ復帰直後)
# ======================================================================


def _connection_error() -> WeatherError:
    try:
        try:
            raise urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided"))
        except urllib.error.URLError as exc:
            raise WeatherError("気象庁ページの取得に失敗しました (synthetic)") from exc
    except WeatherError as error:
        return error


class NetworkReadinessTest(DiaryTestBase):
    def test_waits_for_network_before_sources(self):
        self.write_note(D2)
        self.probe_results = [False, False, True]
        result = self.run_diary((D2,), self.deps())
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.probe_calls[:3], ["api.github.com"] * 3)
        self.assertEqual(self.time.sleeps[:2], [5.0, 5.0])
        self.assertEqual(self.notifications, [])

    def test_transient_weather_connection_error_is_retried_and_not_notified(self):
        self.write_note(D2)
        attempts = {"n": 0}

        def weather(d: date, **kwargs: Any) -> WeatherSummary:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise _connection_error()
            return weather_ok(d)

        self.probe_results = [True, False, True]  # 起動時 OK → 接続エラー後に一度落ちて復帰
        result = self.run_diary((D2,), self.deps(weather=weather), sources=("weather",))
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(attempts["n"], 2)
        self.assertEqual(self.notifications, [])
        self.assertIn('日照時間: "6.0時間"\n', self.read(D2))

    def test_persistent_connection_failure_gives_up_after_grace(self):
        self.write_note(D2)

        def weather(d: date, **kwargs: Any) -> WeatherSummary:
            self.time.now += 3.0  # 1 回の取得 (内部 retry 込み) に 3 秒かかる
            raise _connection_error()

        self.probe_results = [True] + [False] * 100
        result = self.run_diary((D2,), self.deps(weather=weather), sources=("weather",))
        self.assertEqual(result.exit_code, 11)
        self.assertEqual(len(self.notifications), 1)
        waited = sum(self.time.sleeps)
        self.assertLessEqual(waited, 60.0)
        self.assertGreater(waited, 40.0)

    def test_http_error_is_not_retried(self):
        self.write_note(D2)
        attempts = {"n": 0}

        def weather(d: date, **kwargs: Any) -> WeatherSummary:
            attempts["n"] += 1
            try:
                raise urllib.error.HTTPError("https://example.invalid", 503, "busy", {}, None)
            except urllib.error.HTTPError as exc:
                raise WeatherError("気象庁ページの取得に失敗しました (HTTP 503)") from exc

        result = self.run_diary((D2,), self.deps(weather=weather), sources=("weather",))
        self.assertEqual(attempts["n"], 1)
        self.assertEqual(result.exit_code, 11)

    def test_github_connection_errors_trigger_one_recollection(self):
        self.write_note(D2)
        calls = {"n": 0}
        deps = self.deps()

        def collector(client: Any, start: date, end: date, **kwargs: Any) -> GitHubActivity:
            calls["n"] += 1
            if calls["n"] == 1:
                # opener が接続エラーを数えた状況を再現
                with self.assertRaises(urllib.error.URLError):
                    client(None, 1.0)
                return github_failed([start])
            return github_ok([start])

        def opener(request: Any, timeout: float) -> bytes:
            raise urllib.error.URLError(socket.gaierror(8, "synthetic"))

        deps.github_collector = collector
        deps.github_opener = opener
        deps.github_client_factory = lambda token, logger, tracked_opener: tracked_opener
        result = self.run_diary((D2,), deps, sources=("github",))
        self.assertEqual(calls["n"], 2)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.notifications, [])
        self.assertEqual(result.report["github"]["attempts"], 2)

    def test_github_failure_without_connection_errors_is_not_retried(self):
        self.write_note(D2)
        result = self.run_diary(
            (D2,), self.deps(github=lambda c, s, e, **k: github_failed([s])), sources=("github",),
        )
        self.assertEqual(len(self.collector_calls), 1)
        self.assertEqual(result.exit_code, 11)

    def test_grace_zero_disables_probe(self):
        self.write_note(D2)
        config = replace(self.config, network_grace_sec=0.0)
        self.run_diary((D2,), self.deps(), config=config)
        self.assertEqual(self.probe_calls, [])

    def test_network_down_for_whole_grace_still_runs_sources(self):
        self.write_note(D2)
        self.probe_results = [False] * 100
        result = self.run_diary((D2,), self.deps(), sources=("health",))
        self.assertEqual(result.exit_code, 0, "probe が通らなくても取得は試みる (proxy 環境など)")
        self.assertLessEqual(sum(self.time.sleeps), 60.0)
        self.assertEqual(self.probe_calls[0], "www.icloud.com")


class NetworkGateUnitTest(unittest.TestCase):
    def test_budget_is_shared_across_waits(self):
        fake = FakeTime()
        gate = NetworkGate(
            grace_sec=12, logger=LOGGER, probe=lambda host: False,
            monotonic=fake.monotonic, sleep=fake.sleep, interval_sec=5,
        )
        self.assertFalse(gate.wait("a"))
        self.assertEqual(sum(fake.sleeps), 12)
        self.assertFalse(gate.can_retry())
        self.assertFalse(gate.wait("b"))
        self.assertEqual(sum(fake.sleeps), 12, "猶予を使い切ったら待たない")

    def test_ready_host_is_cached(self):
        calls: "list[str]" = []
        gate = NetworkGate(
            grace_sec=60, logger=LOGGER, probe=lambda host: calls.append(host) or True,
            monotonic=FakeTime().monotonic, sleep=lambda s: None,
        )
        self.assertTrue(gate.wait("a"))
        self.assertTrue(gate.wait("a"))
        self.assertEqual(calls, ["a"])
        self.assertTrue(gate.wait("a", recheck=True))
        self.assertEqual(calls, ["a", "a"])

    def test_charge_consumes_budget(self):
        gate = NetworkGate(
            grace_sec=10, logger=LOGGER, probe=lambda host: True,
            monotonic=FakeTime().monotonic, sleep=lambda s: None,
        )
        gate.charge(4)
        self.assertEqual(gate.remaining(), 6)
        gate.charge(7)
        self.assertFalse(gate.can_retry())


class IsConnectionErrorTest(unittest.TestCase):
    def test_classification(self):
        self.assertTrue(is_connection_error(_connection_error()))
        self.assertTrue(is_connection_error(socket.timeout("timed out")))
        self.assertTrue(is_connection_error(ConnectionResetError()))
        self.assertFalse(is_connection_error(WeatherError("layout changed")))
        self.assertFalse(is_connection_error(ValueError("x")))
        try:
            try:
                raise urllib.error.HTTPError("https://example.invalid", 500, "x", {}, None)
            except urllib.error.HTTPError as exc:
                raise WeatherError("HTTP 500") from exc
        except WeatherError as error:
            self.assertFalse(is_connection_error(error))


# ======================================================================
# 既定の依存 (DiaryDeps 未指定) の配線
# ======================================================================


class DefaultWiringTest(DiaryTestBase):
    def test_default_dependencies_are_module_level_and_patchable(self):
        self.write_note(D2)
        config = replace(self.config, network_grace_sec=0.0)
        with mock.patch("ihealth.diary.HealthLoader") as loader_cls, \
             mock.patch("ihealth.diary.fetch_weather", side_effect=lambda d, **k: weather_ok(d)) as fetch, \
             mock.patch("ihealth.diary.resolve_github_token", return_value=None) as token, \
             mock.patch("ihealth.diary.collect_github_activity") as collect, \
             mock.patch("ihealth.diary.notify_failure") as notify, \
             mock.patch("ihealth.diary.tcp_probe", side_effect=AssertionError("probe しない")):
            loader_cls.return_value = FakeLoader({D2: health_ok(D2)})
            options = DiaryOptions(dates=(D2,), today=TODAY)
            result = run_diary(config, options, LOGGER, DiaryDeps(clock=lambda: NOW))
        fetch.assert_called_once()
        token.assert_called_once_with("synthetic-token", LOGGER)
        collect.assert_not_called()  # token が無いので収集しない
        notify.assert_called_once()  # github 失敗の 1 通
        self.assertEqual(result.exit_code, 12)
        self.assertIn('"歩数 (歩)": 4321\n', self.read(D2))


# ======================================================================
# 配線: 既定値と違う設定が source に届く
# ======================================================================


class ConfigWiringTest(DiaryTestBase):
    def test_non_default_tz_reaches_github_and_station_reaches_weather(self):
        self.write_note(D2)
        tz = timezone(timedelta(hours=-5))
        config = replace(self.config, diary_tz=tz, network_grace_sec=0.0)
        seen: "dict[str, Any]" = {}
        deps = self.deps()

        def collector(client: Any, start: date, end: date, **kwargs: Any) -> GitHubActivity:
            seen.update(kwargs)
            return github_ok([start + timedelta(days=i) for i in range((end - start).days + 1)])

        def fetcher(d: date, **kwargs: Any) -> WeatherSummary:
            seen["prec_no"], seen["block_no"] = kwargs["prec_no"], kwargs["block_no"]
            return weather_ok(d)

        deps.github_collector = collector
        deps.weather_fetcher = fetcher
        result = self.run_diary((D2,), deps, config=config)
        self.assertEqual(result.exit_code, 0)
        self.assertIs(seen["tz"], tz)
        self.assertEqual(seen["owners"], "sample-owner,sample-org")
        self.assertEqual(seen["exclude_repos"], "sample-owner/skip-me")
        self.assertEqual((seen["prec_no"], seen["block_no"]), (11, 47401))


# ======================================================================
# 暫定値の台帳: 今日の値が確定値に置き換わるまで追跡する
# ======================================================================


#: 暫定値を書く日 (= その実行の「今日」)。
PROV_DAY = date(2030, 1, 16)


class ProvisionalLedgerTest(DiaryTestBase):
    """今日の暫定値が lookback window の外へ出ても確定値に置き換わる (finding: 暫定値の永続化)。"""

    def setUp(self) -> None:
        super().setUp()
        self.config = replace(self.config, network_grace_sec=0.0)
        self.ledger_path = self.root / "data" / "diary-state.json"

    def _run(
        self,
        today: date,
        dates: "tuple[date, ...]",
        loader: FakeLoader,
        *,
        github: "Callable[..., GitHubActivity] | None" = None,
        hour: int = 12,
        fill_empty_only: bool = False,
        dry_run: bool = False,
        revisit: bool = True,
        sources: "tuple[str, ...]" = ("health", "github"),
    ):
        deps = self.deps(loader=loader, github=github)
        deps.clock = lambda: datetime.combine(today, time(hour, 0), tzinfo=JST)
        options = DiaryOptions(
            dates=dates, today=today, sources=sources, dry_run=dry_run,
            fill_empty_only=fill_empty_only, revisit_unresolved=revisit,
        )
        return run_diary(self.config, options, LOGGER, deps)

    @staticmethod
    def _default_window(today: date) -> "tuple[date, ...]":
        return resolve_dates(today=today, lookback_days=7, include_today=True)

    def _ledger(self) -> dict:
        return json.loads(self.ledger_path.read_text(encoding="utf-8"))

    def _write_provisional(self) -> None:
        """PROV_DAY の 21 時: 途中までの値 (暫定) と今日の GitHub を書く。"""
        self.write_note(PROV_DAY)

        def github_none(client: Any, start: date, end: date, **kwargs: Any) -> GitHubActivity:
            return GitHubActivity(
                days={start + timedelta(days=i): [] for i in range((end - start).days + 1)},
                complete=True, repos_scanned=1, owners_listed=1,
            )

        loader = FakeLoader({PROV_DAY: health_ok(PROV_DAY, steps=468, provisional=True)},
                            latest=PROV_DAY)
        result = self._run(PROV_DAY, (PROV_DAY,), loader, github=github_none, hour=21)
        self.assertEqual(result.exit_code, 0)
        text = self.read(PROV_DAY)
        self.assertIn('"歩数 (歩)": 468\n', text)
        self.assertIn('Github: "該当なし"\n', text)
        self.assertEqual(
            set(result.days[0].provisional_keys),
            {"歩数 (歩)", "睡眠時間 (分)", "アクティビティ詳細", "Github"},
        )
        # 台帳の形式 2 (W3-2d): キーごとに理由・初回検出日時 (この実行の開始 = 21 時 JST)・
        # 本ツールが書いた暫定値 (形式 1 は値だけだった)
        ledger = self._ledger()
        self.assertEqual(ledger["version"], 2)
        entry = ledger["dates"][PROV_DAY.isoformat()]["keys"]
        self.assertEqual(
            entry["歩数 (歩)"],
            {"reason": "provisional", "since": "2030-01-16T12:00:00+00:00", "value": 468},
        )
        self.assertEqual(entry["Github"]["value"], "該当なし")
        self.assertEqual(entry["Github"]["reason"], "provisional")

    def _final_loader(self, today: date) -> FakeLoader:
        return FakeLoader({PROV_DAY: health_ok(PROV_DAY, steps=9000)}, latest=today)

    @staticmethod
    def _github_final(client: Any, start: date, end: date, **kwargs: Any) -> GitHubActivity:
        days = {start + timedelta(days=i): [] for i in range((end - start).days + 1)}
        days[PROV_DAY] = [GitHubItem(
            kind="pr", repo="demo", url="https://example.com/demo/pull/7", number=7,
            title="Evening change",
        )]
        return GitHubActivity(days=days, complete=True, repos_scanned=1, owners_listed=1)

    def test_provisional_day_outside_window_is_revisited_and_finalized(self):
        self._write_provisional()
        later = PROV_DAY + timedelta(days=8)       # 7 日の window からは外れている
        window = self._default_window(later)
        self.assertNotIn(PROV_DAY, window)
        result = self._run(later, window, self._final_loader(later), github=self._github_final)
        self.assertIn(PROV_DAY, result.dates, "台帳の日は window の外でも処理する")
        text = self.read(PROV_DAY)
        self.assertIn('"歩数 (歩)": 9000\n', text)
        self.assertIn("Evening change", text)
        day = next(day for day in result.days if day.date == PROV_DAY)
        self.assertFalse(day.provisional)
        self.assertEqual(day.provisional_keys, ())
        self.assertTrue(day.keys["歩数 (歩)"]["provisional_resolved"])
        self.assertEqual(self._ledger()["dates"], {}, "確定したら台帳から外れる")
        self.assertEqual(result.exit_code, 0)

    def test_revisited_day_outside_window_keeps_values_the_user_wrote(self):
        # window の外の日は「空のキーを埋める + 暫定値のままのキーを確定値に置き換える」だけ。
        # 利用者が書いた値 (ここでは天気) は上書きしない
        self._write_provisional()
        path = self.diary / f"{PROV_DAY.isoformat()}.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace("天気:\n", '天気: "手で書いた天気"\n'),
            encoding="utf-8",
        )
        later = PROV_DAY + timedelta(days=8)
        self.weather_today = later
        result = self._run(
            later, self._default_window(later), self._final_loader(later),
            github=self._github_final, sources=("health", "weather", "github"),
        )
        text = self.read(PROV_DAY)
        self.assertIn('"歩数 (歩)": 9000\n', text)
        self.assertIn('天気: "手で書いた天気"\n', text)
        self.assertIn('気温: "朝:平均5.0℃', text, "空のキーは埋める")
        day = next(day for day in result.days if day.date == PROV_DAY)
        self.assertEqual(day.keys["天気"]["action"], "kept")
        self.assertEqual(self._ledger()["dates"], {})

    def test_fill_empty_only_still_replaces_untouched_provisional_values(self):
        self._write_provisional()
        later = PROV_DAY + timedelta(days=8)
        result = self._run(
            later, (PROV_DAY,), self._final_loader(later), github=self._github_final,
            fill_empty_only=True, revisit=False,
        )
        text = self.read(PROV_DAY)
        self.assertIn('"歩数 (歩)": 9000\n', text, "暫定値のままのキーは --fill-empty-only でも置き換える")
        self.assertIn("Evening change", text)
        keys = result.days[0].keys
        self.assertEqual(keys["歩数 (歩)"]["action"], "update")
        self.assertEqual(self._ledger()["dates"], {})

    def test_fill_empty_only_keeps_values_the_user_changed(self):
        self._write_provisional()
        path = self.diary / f"{PROV_DAY.isoformat()}.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace('"歩数 (歩)": 468', '"歩数 (歩)": 5555'),
            encoding="utf-8",
        )
        later = PROV_DAY + timedelta(days=8)
        result = self._run(
            later, (PROV_DAY,), self._final_loader(later), github=self._github_final,
            fill_empty_only=True, revisit=False,
        )
        self.assertIn('"歩数 (歩)": 5555\n', self.read(PROV_DAY), "利用者が書き換えた値は保持")
        self.assertEqual(result.days[0].keys["歩数 (歩)"]["action"], "kept")
        self.assertNotIn("歩数 (歩)", self._ledger()["dates"].get(PROV_DAY.isoformat(), {}).get("keys", {}))

    def test_without_revisit_other_dates_are_not_added(self):
        self._write_provisional()
        later = PROV_DAY + timedelta(days=8)
        self.write_note(later - timedelta(days=1))
        result = self._run(
            later, (later - timedelta(days=1),), self._final_loader(later), revisit=False,
        )
        self.assertEqual(result.dates, (later - timedelta(days=1),), "--date / --from の実行は指定日だけ")
        self.assertIn('"歩数 (歩)": 468\n', self.read(PROV_DAY))
        self.assertIn(PROV_DAY.isoformat(), self._ledger()["dates"])

    def test_unresolved_day_stays_tracked_and_is_notified_after_three_days(self):
        self._write_provisional()
        # 翌朝: 前日のファイルはまだ日付が変わる前の書き出しのまま → 保留 (nodata)
        next_morning = PROV_DAY + timedelta(days=1)
        withheld = FakeLoader({}, latest=next_morning)
        result = self._run(next_morning, self._default_window(next_morning), withheld, hour=7)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.notifications, [])
        self.assertIn('"歩数 (歩)": 468\n', self.read(PROV_DAY))
        self.assertIn(PROV_DAY.isoformat(), self._ledger()["dates"])
        # 4 日後もまだ確定しない → 「暫定値が確定していません」を 1 日 1 回
        later = PROV_DAY + timedelta(days=4)
        loader = FakeLoader({}, latest=later)
        self._run(later, self._default_window(later), loader)
        self.assertEqual(len(self.notifications), 1)
        title, body = self.notifications[0]
        self.assertIn("暫定値が確定していません", title)
        self.assertIn(PROV_DAY.isoformat(), body)
        self.assertIn("歩数 (歩)", body)
        self._run(later, self._default_window(later), loader)
        self.assertEqual(len(self.notifications), 1, "同じ日には 1 回だけ")

    def test_entries_past_revisit_period_are_dropped_with_a_notice(self):
        self._write_provisional()
        later = PROV_DAY + timedelta(days=PROVISIONAL_REVISIT_DAYS + 1)
        result = self._run(later, self._default_window(later), FakeLoader({}, latest=later))
        self.assertNotIn(PROV_DAY, result.dates)
        self.assertEqual(self._ledger()["dates"], {})
        self.assertEqual(len(self.notifications), 1)
        self.assertIn("自動の再処理をやめました", self.notifications[0][1])
        self.assertIn(PROV_DAY.isoformat(), result.report["provisional"]["expired"])

    def test_dry_run_reads_but_never_writes_the_ledger(self):
        self._write_provisional()
        before = self.ledger_path.read_bytes()
        later = PROV_DAY + timedelta(days=8)
        result = self._run(
            later, self._default_window(later), self._final_loader(later),
            github=self._github_final, dry_run=True,
        )
        self.assertIn(PROV_DAY, result.dates)
        self.assertEqual(self.ledger_path.read_bytes(), before)
        self.assertIn('"歩数 (歩)": 468\n', self.read(PROV_DAY))

    def test_ledger_save_failure_is_reported(self):
        self.write_note(PROV_DAY)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path.mkdir()  # ファイルを置けない (os.replace が失敗する)
        loader = FakeLoader({PROV_DAY: health_ok(PROV_DAY, provisional=True)}, latest=PROV_DAY)
        result = self._run(PROV_DAY, (PROV_DAY,), loader, sources=("health",))
        self.assertEqual(result.exit_code, 12)
        self.assertIn("暫定値の台帳", self.notifications[0][1])
        self.assertIn("error", result.report["provisional"])

    def test_corrupt_ledger_is_rebuilt(self):
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path.write_text("{broken", encoding="utf-8")
        self.write_note(PROV_DAY)
        loader = FakeLoader({PROV_DAY: health_ok(PROV_DAY, provisional=True)}, latest=PROV_DAY)
        result = self._run(PROV_DAY, (PROV_DAY,), loader, sources=("health",))
        self.assertEqual(result.exit_code, 0)
        self.assertIn(PROV_DAY.isoformat(), self._ledger()["dates"])

    def test_missing_note_stops_tracking(self):
        self._write_provisional()
        (self.diary / f"{PROV_DAY.isoformat()}.md").unlink()
        later = PROV_DAY + timedelta(days=2)
        self._run(later, (PROV_DAY,), self._final_loader(later), revisit=False)
        self.assertEqual(self._ledger()["dates"], {})


# ======================================================================
# 通知: dry-run / 送れなかった通知 / データの鮮度
# ======================================================================


class NotificationDeliveryTest(DiaryTestBase):
    def _failing_weather(self) -> DiaryDeps:
        def weather(d: date, **kwargs: Any) -> WeatherSummary:
            raise WeatherError("気象庁ページの取得に失敗しました (HTTP 503): synthetic")

        return self.deps(weather=weather)

    def test_dry_run_sends_nothing_and_does_not_silence_the_real_run(self):
        self.write_note(D2)
        result = self.run_diary((D2,), self._failing_weather(), sources=("weather",), dry_run=True)
        self.assertEqual(result.exit_code, 11)
        self.assertEqual(self.notifications, [])
        self.assertEqual(result.notifications, ())
        self.assertFalse((self.logs / ".failure_notified").exists())
        self.assertTrue(result.report["notifications"]["failure"]["dry_run"])
        # 同じ日の本番の実行は通知される (dry-run が「通知済み」を記録していない)
        self.run_diary((D2,), self._failing_weather(), sources=("weather",))
        self.assertEqual(len(self.notifications), 1)

    def test_dry_run_stale_notice_is_not_sent_or_remembered(self):
        self.write_note(D3)
        loader = FakeLoader({D3: health_ok(D3)}, latest=TODAY - timedelta(days=5))
        self.run_diary((D3,), self.deps(loader=loader), sources=("health",), dry_run=True)
        self.assertEqual(self.notifications, [])
        self.assertFalse((self.logs / ".stale_notified").exists())
        self.run_diary((D3,), self.deps(loader=loader), sources=("health",))
        self.assertEqual(len(self.notifications), 1)

    def test_undelivered_notification_is_not_remembered(self):
        self.write_note(D2)
        attempts: "list[str]" = []
        deps = self._failing_weather()
        deps.notifier = lambda title, body: attempts.append(title) or False
        first = self.run_diary((D2,), deps, sources=("weather",))
        self.assertEqual(first.notifications, ())
        self.assertFalse((self.logs / ".failure_notified").exists())
        self.assertIs(first.report["notifications"]["failure"]["delivered"], False)
        saved = json.loads(first.report_path.read_text(encoding="utf-8"))
        self.assertIs(saved["notifications"]["failure"]["delivered"], False)
        self.run_diary((D2,), deps, sources=("weather",))
        self.assertEqual(len(attempts), 2, "作れなかった通知は次の実行で改めて送る")

    def test_real_notifier_failure_is_retried_next_run(self):
        # 実 notify_failure (osascript が権限エラーで失敗) を通しても「通知済み」にしない
        self.write_note(D2)
        deps = self._failing_weather()
        deps.notifier = None
        error = subprocess.CalledProcessError(
            returncode=1, cmd=["/usr/bin/osascript"], stderr=b"not authorized (-1743)",
        )
        with mock.patch("ihealth.notifier.os.path.exists", return_value=True), \
             mock.patch("ihealth.notifier.subprocess.run", side_effect=error) as run:
            for _ in range(2):
                result = self.run_diary((D2,), deps, sources=("weather",))
                self.assertEqual(result.exit_code, 11)
                self.assertEqual(result.notifications, ())
        self.assertEqual(run.call_count, 2)
        self.assertFalse((self.logs / ".failure_notified").exists())

    def test_failure_and_stale_are_merged_into_one_reminder(self):
        self.write_note(D2)
        loader = FakeLoader({D2: health_ok(D2)}, latest=TODAY - timedelta(days=5))
        deps = self._failing_weather()
        deps.health_loader_factory = lambda config, logger, now: loader
        result = self.run_diary((D2,), deps, sources=("health", "weather"))
        self.assertEqual(result.exit_code, 12)
        self.assertEqual(len(self.notifications), 1, "1 run 1 通")
        title, body = self.notifications[0]
        self.assertIn("weather", title)
        self.assertIn("停止の疑い", body)
        self.assertTrue((self.logs / ".failure_notified").exists())
        self.assertTrue((self.logs / ".stale_notified").exists())

    def test_notify_once_per_day_helper(self):
        sent: "list[str]" = []
        for _ in range(2):
            status = notify_once_per_day(
                "t", "b", logs_dir=self.logs, today=TODAY, logger=LOGGER,
                notifier=lambda title, body: sent.append(title),
            )
        self.assertEqual(sent, ["t"])
        self.assertEqual(status, "suppressed")
        failed = notify_once_per_day(
            "other", "b", logs_dir=self.logs, today=TODAY, logger=LOGGER,
            notifier=lambda title, body: False,
        )
        self.assertEqual(failed, "failed")
        self.assertEqual(
            notify_once_per_day(
                "other", "b", logs_dir=self.logs, today=TODAY, logger=LOGGER,
                notifier=lambda title, body: True,
            ),
            "sent",
        )


class MissingAutomationTest(DiaryTestBase):
    """オートメーションの出力が 1 件も無く健康データを 1 日も取れない → 1 日 1 回知らせる。"""

    def test_no_automation_and_no_health_data_is_notified_once(self):
        self.write_note(D2)
        loader = FakeLoader({}, latest=None)
        result = self.run_diary((D2, D3), self.deps(loader=loader), sources=("health",))
        self.assertEqual(result.exit_code, 0, "終了コードは変えない (停止検知と同じ)")
        self.assertEqual(len(self.notifications), 1)
        self.assertIn("出力が見つかりません", self.notifications[0][0])
        self.assertTrue(result.report["stale_check"]["missing"])
        self.run_diary((D2, D3), self.deps(loader=loader), sources=("health",))
        self.assertEqual(len(self.notifications), 1)

    def test_autosync_only_days_are_not_reported_as_missing(self):
        self.write_note(D2)
        autosync = replace(health_ok(D2), source="autosync")
        loader = FakeLoader({D2: autosync}, latest=None)
        result = self.run_diary((D2, D3), self.deps(loader=loader), sources=("health",))
        self.assertEqual(self.notifications, [])
        self.assertFalse(result.report["stale_check"]["missing"])

    def test_health_failures_are_not_double_reported(self):
        self.write_note(D2)
        loader = FakeLoader({D2: health_failed(D2)}, latest=None)
        result = self.run_diary((D2,), self.deps(loader=loader), sources=("health",))
        self.assertEqual(len(self.notifications), 1)
        self.assertNotIn("出力が見つかりません", self.notifications[0][1])
        self.assertFalse(result.report["stale_check"]["missing"])

    def test_real_loader_pointed_at_autosync_without_health_metrics(self):
        # HEALTH_EXPORT_DIR=…/AutoSync (HealthMetrics を消した後) でも隣のオートメーションを読む
        docs = self.root / "Documents"
        (docs / "AutoSync" / "Workouts").mkdir(parents=True)
        folder = docs / "Sample Automation"
        folder.mkdir()
        fixture = AUTOMATION_FIXTURES / "aggregated" / "HealthAutoExport-2030-01-15.json"
        shutil.copyfile(fixture, folder / fixture.name)
        # 対象日の終了後に書かれた出力 (tz 付きで指定して実行環境の tz に依存させない)
        final = datetime(2030, 1, 16, 12, 0, tzinfo=timezone.utc).timestamp()
        os.utime(folder / fixture.name, (final, final))
        self.write_note(YESTERDAY)
        config = replace(self.config, health_export_dir=docs / "AutoSync", network_grace_sec=0.0)
        deps = self.deps()
        deps.health_loader_factory = None   # 実 HealthLoader
        deps.clock = lambda: datetime(2030, 1, 20, 3, 0, tzinfo=timezone.utc)
        options = DiaryOptions(dates=(YESTERDAY,), today=date(2030, 1, 20), sources=("health",))
        result = run_diary(config, options, LOGGER, deps)
        self.assertEqual(result.days[0].sources["health"], STATUS_COMPLETE)
        self.assertIn('"歩数 (歩)": 501\n', self.read(YESTERDAY))
        self.assertFalse(result.report["stale_check"]["missing"])


class WeatherZeroRowsTest(DiaryTestBase):
    def test_two_consecutive_old_days_without_rows_fail(self):
        # 観測行 0 件の過去日 (休止した観測所 / 番号の誤り) が続く → 黙って nodata にしない
        for d in (D1, D2, D3):
            self.write_note(d)

        def weather(d: date, **kwargs: Any) -> WeatherSummary:
            return replace(weather_incomplete(d, pending=False), observed_hours=0)

        result = self.run_diary((D1, D2, D3), self.deps(weather=weather), sources=("weather",))
        self.assertEqual(result.days[0].sources["weather"], STATUS_PARTIAL)
        self.assertEqual(result.exit_code, 12)
        self.assertEqual(len(self.notifications), 1)
        self.assertIn("2 日以上続けて", self.notifications[0][1])


class ActivityDetailWithoutWorkoutsTest(DiaryTestBase):
    """ワークアウトを出力しないオートメーション (W3-7)。

    wave 2 までは「ワークアウト未出力ならアクティビティ詳細を書かない」だったが、W3-7 で
    「Apple のエクササイズ時間 (apple_exercise_time) を "エクササイズN分" として書く」に変わり、
    W3-7 改訂で「瞑想も運動も無い日は、心拍がある (Watch を着けていた) なら "なし"、心拍も無い
    日だけ書かない (既存値を保持)」になった。実 HealthLoader + 実パーサで確かめる。
    """

    FIXTURE = AUTOMATION_FIXTURES / "aggregated" / "HealthAutoExport-2030-01-15.json"
    ORIGINAL_DETAIL = 'アクティビティ詳細: "ジョギング34分"\n'

    def _run_with_payload(self, payload: dict):
        docs = self.root / "Documents"
        folder = docs / "Sample Automation"
        folder.mkdir(parents=True)
        path = folder / self.FIXTURE.name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        # 対象日の終了後に書かれた (確定の) 出力。tz 付きで指定して実行環境の tz に依存させない
        final = datetime(2030, 1, 16, 12, 0, tzinfo=timezone.utc).timestamp()
        os.utime(path, (final, final))
        self.write_note(
            YESTERDAY, note_text(YESTERDAY).replace("アクティビティ詳細:\n", self.ORIGINAL_DETAIL),
        )
        config = replace(self.config, health_export_dir=docs, network_grace_sec=0.0)
        deps = self.deps()
        deps.health_loader_factory = None   # 実 HealthLoader + 実パーサ
        deps.clock = lambda: datetime(2030, 1, 20, 3, 0, tzinfo=timezone.utc)
        options = DiaryOptions(dates=(YESTERDAY,), today=date(2030, 1, 20), sources=("health",))
        return run_diary(config, options, LOGGER, deps)

    def _payload(self) -> dict:
        return json.loads(self.FIXTURE.read_text(encoding="utf-8"))

    def test_exercise_time_stands_in_for_unexported_workouts(self):
        # fixture: 瞑想 2.6 + 3.0 分, エクササイズ時間 3 分, workouts なし
        result = self._run_with_payload(self._payload())
        text = self.read(YESTERDAY)
        self.assertIn('"歩数 (歩)": 501\n', text)
        self.assertIn('アクティビティ詳細: "瞑想6分、エクササイズ3分"\n', text)
        self.assertEqual(result.days[0].keys["アクティビティ詳細"]["action"], "update")

    def test_existing_activity_detail_is_kept_without_any_activity_data(self):
        # 瞑想・エクササイズ時間に加えて心拍も無い (Watch を着けていない) 日 → 書かない (既存値を保持)。
        # W3-7 改訂で「なし」の根拠が心拍になったので、心拍も取り除く (心拍がある日は下の
        # test_heart_rate_without_activity_records_writes_nashi)
        payload = self._payload()
        payload["data"]["metrics"] = [
            metric for metric in payload["data"]["metrics"]
            if metric["name"] not in ("mindful_minutes", "apple_exercise_time", "heart_rate")
        ]
        result = self._run_with_payload(payload)
        text = self.read(YESTERDAY)
        self.assertIn('"歩数 (歩)": 501\n', text, "他の健康キーは書く")
        self.assertIn(self.ORIGINAL_DETAIL, text, "瞑想も運動も心拍も無ければ書かない")
        self.assertEqual(result.days[0].keys["アクティビティ詳細"]["action"], "missing")

    def test_heart_rate_without_activity_records_writes_nashi(self):
        # 実際の HAE の運動 0 分の日 (apple_exercise_time / mindful_minutes の出力なし、心拍あり)
        # → Watch を着けていて運動 0 分 = 「なし」を書く (既定 window の中なので既存値を上書きする。
        # 旧: 運動のデータが無いので書かない。WAVE3 W3-7 改訂で意図的に変更)
        payload = self._payload()
        payload["data"]["metrics"] = [
            metric for metric in payload["data"]["metrics"]
            if metric["name"] not in ("mindful_minutes", "apple_exercise_time")
        ]
        result = self._run_with_payload(payload)
        text = self.read(YESTERDAY)
        self.assertIn('"歩数 (歩)": 501\n', text)
        self.assertIn('アクティビティ詳細: "なし"\n', text)
        self.assertEqual(result.days[0].keys["アクティビティ詳細"]["action"], "update")
        self.assertEqual(result.days[0].keys["アクティビティ詳細"]["old"], '"ジョギング34分"')

    def test_meditation_without_exercise_data_writes_meditation_only(self):
        # W3-7 改訂の規則 1 (瞑想 ≥1 分なら "瞑想N分") どおり、エクササイズ時間のデータが無い日でも
        # 瞑想は書く (既定 window の中なので既存値を上書きする)。「書かない (None)」は瞑想も運動も
        # 無く心拍も無い日だけの規則 (リーダー決定で確定)
        payload = self._payload()
        payload["data"]["metrics"] = [
            metric for metric in payload["data"]["metrics"]
            if metric["name"] != "apple_exercise_time"
        ]
        result = self._run_with_payload(payload)
        self.assertIn('アクティビティ詳細: "瞑想6分"\n', self.read(YESTERDAY))
        self.assertEqual(result.days[0].keys["アクティビティ詳細"]["action"], "update")
        self.assertEqual(result.days[0].keys["アクティビティ詳細"]["old"], '"ジョギング34分"')


# ======================================================================
# 未確定の値の台帳 (W3-2d): 暫定値だけでなく、保留・天気の公開待ち / 未完成・
# GitHub の取得不完全も、確定するまで追跡する
# ======================================================================


#: 台帳のテストの「今日」。
T0 = date(2030, 1, 16)


class UnresolvedLedgerTest(DiaryTestBase):
    """暫定値以外の未確定 (保留 / 天気 / GitHub) も台帳で追跡し、window の外でも埋める。"""

    def setUp(self) -> None:
        super().setUp()
        self.config = replace(self.config, network_grace_sec=0.0)
        self.ledger_path = self.root / "data" / "diary-state.json"

    def _run(
        self,
        today: date,
        dates: "tuple[date, ...]",
        *,
        loader: "FakeLoader | None" = None,
        weather: "Callable[..., WeatherSummary] | None" = None,
        github: "Callable[..., GitHubActivity] | None" = None,
        sources: "tuple[str, ...]" = ("weather",),
        revisit: bool = True,
        hour: int = 12,
    ):
        self.weather_today = today
        deps = self.deps(loader=loader, weather=weather, github=github)
        deps.clock = lambda: datetime.combine(today, time(hour, 0), tzinfo=JST)
        options = DiaryOptions(
            dates=dates, today=today, sources=sources, revisit_unresolved=revisit,
        )
        return run_diary(self.config, options, LOGGER, deps)

    @staticmethod
    def _window(today: date) -> "tuple[date, ...]":
        return resolve_dates(today=today, lookback_days=7, include_today=True)

    def _ledger_dates(self) -> dict:
        return json.loads(self.ledger_path.read_text(encoding="utf-8"))["dates"]

    @staticmethod
    def _day(result, d: date):
        return next(day for day in result.days if day.date == d)

    def test_pending_weather_is_tracked_and_filled_after_the_window(self):
        day = T0 - timedelta(days=1)
        self.write_note(day)

        def pending(d: date, **kwargs: Any) -> WeatherSummary:
            return weather_incomplete(d, pending=True)

        first = self._run(T0, self._window(T0), weather=pending)
        self.assertEqual(first.exit_code, 0, "公開待ちは失敗ではない")
        self.assertEqual(self.notifications, [])
        keys = self._ledger_dates()[day.isoformat()]["keys"]
        self.assertEqual(set(keys), {"気温", "湿度", "気圧", "天気", "日照時間", "降水量"})
        self.assertEqual(
            keys["気温"], {"reason": "weather_pending", "since": "2030-01-16T03:00:00+00:00"},
        )
        self.assertEqual(self._day(first, day).unresolved["天気"], "weather_pending")
        self.assertTrue(self._day(first, day).tracked)

        later = T0 + timedelta(days=8)   # day は 7 日の window の外
        self.assertNotIn(day, self._window(later))
        second = self._run(later, self._window(later))
        self.assertIn(day, second.dates, "未確定の天気がある日は window の外でも処理する")
        self.assertIn('天気: "3時: 晴れ☀️, 6時: 曇☁️"\n', self.read(day))
        self.assertEqual(self._day(second, day).keys["天気"]["resolved"], "weather_pending")
        self.assertNotIn(day.isoformat(), self._ledger_dates(), "埋まったら台帳から外れる")

    def test_incomplete_weather_day_needs_attention_after_three_days_unresolved(self):
        # 「3 日超」は初回検出から数える: 対象日 (T0 の 3 日前) から数えると、初めて見つけた
        # 実行でいきなり要確認になってしまう (backfill した直近の日など)
        day = D2
        self.write_note(day)

        def incomplete(d: date, **kwargs: Any) -> WeatherSummary:
            return weather_incomplete(d, pending=False) if d == day else weather_ok(d)

        first = self._run(T0, (day,), weather=incomplete, revisit=False)
        self.assertEqual(first.exit_code, 0, "1 日だけの未完成は失敗ではない")
        self.assertEqual(self._day(first, day).unresolved["気温"], "weather_incomplete")
        self.assertEqual(first.report["provisional"]["overdue"], {})
        self.assertEqual(self.notifications, [])

        three_days = T0 + timedelta(days=3)
        not_yet = self._run(three_days, self._window(three_days), weather=incomplete)
        self.assertEqual(self.notifications, [], "初回検出から 3 日ちょうどではまだ知らせない")
        self.assertEqual(not_yet.report["provisional"]["overdue"], {})
        self.assertEqual(
            self._ledger_dates()[day.isoformat()]["keys"]["気温"]["since"],
            "2030-01-16T03:00:00+00:00", "初回検出日時は引き継ぐ",
        )

        four_days = T0 + timedelta(days=4)
        result = self._run(four_days, self._window(four_days), weather=incomplete)
        self.assertEqual(result.exit_code, 0, "要確認の通知は終了コードを変えない")
        self.assertEqual(len(self.notifications), 1)
        title, body = self.notifications[0]
        self.assertIn("要確認", title)
        self.assertIn(day.isoformat(), body)
        self.assertIn("天気が未完成", body)
        self.assertIn("気温", body)
        self.assertIn("気象庁", body, "理由に応じて確かめることを書く")
        self.assertEqual(
            result.report["provisional"]["overdue"][day.isoformat()]["天気"], "weather_incomplete",
        )
        self._run(four_days, self._window(four_days), weather=incomplete)
        self.assertEqual(len(self.notifications), 1, "同じ日には 1 回だけ")

    def test_incomplete_github_is_tracked_until_collection_completes(self):
        self.write_note(D2, note_text(D2, github='"既存の値"'))

        def partial(client: Any, start: date, end: date, **kwargs: Any) -> GitHubActivity:
            return GitHubActivity(
                days={start + timedelta(days=i): [] for i in range((end - start).days + 1)},
                complete=False, errors=["owner/repo: HTTP 502"], repos_scanned=3,
                repos_failed=1, owners_listed=1,
            )

        first = self._run(T0, (D2,), github=partial, sources=("github",), revisit=False)
        self.assertEqual(first.exit_code, 12)
        self.assertIn('Github: "既存の値"\n', self.read(D2))
        self.assertEqual(
            self._ledger_dates()[D2.isoformat()]["keys"]["Github"],
            {"reason": "github_incomplete", "since": "2030-01-16T03:00:00+00:00"},
        )

        later = T0 + timedelta(days=8)
        second = self._run(later, self._window(later), sources=("github",))
        self.assertIn(D2, second.dates)
        # window の外は fill-only: 本ツールが書いた暫定値ではない既存値は上書きしない
        self.assertIn('Github: "既存の値"\n', self.read(D2))
        self.assertEqual(self._day(second, D2).keys["Github"]["action"], "kept")
        self.assertNotIn(D2.isoformat(), self._ledger_dates(), "取りきれたら台帳から外れる")

    def test_failed_github_collection_is_tracked_as_incomplete(self):
        self.write_note(D2)
        result = self._run(T0, (D2,), github=lambda c, s, e, **k: github_failed([s]),
                           sources=("github",), revisit=False)
        self.assertEqual(result.days[0].unresolved, {"Github": "github_incomplete"})
        self.assertIn(D2.isoformat(), self._ledger_dates())

    def test_withheld_health_keys_are_tracked_and_filled_when_final(self):
        self.write_note(D2)
        withheld = HealthLoadResult(
            health=DailyHealthData(date=D2, step_count=500),
            activity_detail=None, source="automation", detail="synthetic",
            status=STATUS_COMPLETE, withheld_fields=("sleep_hours",),
        )
        first = self._run(T0, (D2,), loader=FakeLoader({D2: withheld}),
                          sources=("health",), revisit=False)
        self.assertEqual(first.exit_code, 0, "保留は失敗ではない")
        self.assertEqual(first.days[0].unresolved, {"睡眠時間 (分)": "withheld"})
        self.assertEqual(
            self._ledger_dates()[D2.isoformat()]["keys"],
            {"睡眠時間 (分)": {"reason": "withheld", "since": "2030-01-16T03:00:00+00:00"}},
        )
        self.assertIn('"睡眠時間 (分)":\n', self.read(D2))

        later = T0 + timedelta(days=8)
        second = self._run(later, self._window(later), loader=FakeLoader({D2: health_ok(D2)}),
                           sources=("health",))
        self.assertIn(D2, second.dates)
        self.assertIn('"睡眠時間 (分)": 450\n', self.read(D2))
        self.assertEqual(self._day(second, D2).keys["睡眠時間 (分)"]["resolved"], "withheld")
        self.assertEqual(self._ledger_dates(), {})

    def test_unresolved_keys_older_than_revisit_period_are_reported_not_tracked(self):
        # 30 日より前 (過去分の backfill): 自動では処理し直せないので台帳には入れず、1 回知らせる
        old = T0 - timedelta(days=PROVISIONAL_REVISIT_DAYS + 10)
        self.write_note(old)

        def incomplete(d: date, **kwargs: Any) -> WeatherSummary:
            return weather_incomplete(d, pending=False)

        result = self._run(T0, (old,), weather=incomplete, revisit=False)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.days[0].tracked)
        self.assertEqual(result.days[0].unresolved["気温"], "weather_incomplete")
        self.assertEqual(
            result.report["provisional"]["untracked"][old.isoformat()]["気温"], "weather_incomplete",
        )
        self.assertEqual(result.report["provisional"]["expired"], {}, "追跡していないので「やめた」ではない")
        self.assertFalse(self.ledger_path.exists())
        self.assertEqual(len(self.notifications), 1)
        title, body = self.notifications[0]
        self.assertIn("要確認", title)
        self.assertIn(old.isoformat(), body)
        self.assertIn("自動では処理し直しません", body)
        self._run(T0, (old,), weather=incomplete, revisit=False)
        self.assertEqual(len(self.notifications), 1, "同じ日には 1 回だけ")

    def test_format_1_ledger_is_read_and_rewritten_as_format_2(self):
        # wave 2 までの台帳 (キー → 暫定値) も読める。初回検出日時は不明なので日付から数える
        day = T0 - timedelta(days=10)
        self.write_note(day, note_text(day, steps="468").replace(
            "Github:\n", 'Github: "該当なし"\n'))
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path.write_text(json.dumps({
            "version": 1,
            "dates": {day.isoformat(): {"keys": {"歩数 (歩)": 468, "Github": "該当なし"}}},
        }, ensure_ascii=False), encoding="utf-8")
        loader = FakeLoader({day: health_ok(day, steps=9000)})
        result = self._run(T0, self._window(T0), loader=loader, sources=("health",))
        self.assertIn(day, result.dates)
        self.assertIn('"歩数 (歩)": 9000\n', self.read(day), "形式 1 の暫定値のままなら置き換える")
        saved = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["version"], 2)
        self.assertEqual(
            saved["dates"][day.isoformat()]["keys"],
            {"Github": {"reason": "provisional", "since": None, "value": "該当なし"}},
            "GitHub は今回処理していないので追跡を続ける",
        )
        self.assertEqual(
            result.report["provisional"]["overdue"][day.isoformat()], {"Github": "provisional"},
        )


class PartialDayExportLedgerTest(DiaryTestBase):
    """日の途中で書き出された HAE の出力 (W3-2h: 終わった日でも暫定値として返る) を、
    実 HealthLoader 経由で暫定値として書き、確定した出力が来たら台帳から外す。"""

    def test_partial_export_of_a_past_day_is_tracked_until_final(self):
        docs = self.root / "Documents"
        folder = docs / "Sample Automation"
        folder.mkdir(parents=True)
        fixture = AUTOMATION_FIXTURES / "aggregated" / "HealthAutoExport-2030-01-15.json"
        target = folder / fixture.name
        shutil.copyfile(fixture, target)
        # 対象日 (record の offset +09:00) の終わりより前 = 途中経過の出力
        partial = datetime(2030, 1, 15, 21, 0, tzinfo=JST).timestamp()
        os.utime(target, (partial, partial))
        self.write_note(YESTERDAY)
        config = replace(self.config, health_export_dir=docs, network_grace_sec=0.0)
        later_today = date(2030, 1, 17)
        deps = self.deps()
        deps.health_loader_factory = None   # 実 HealthLoader + 実パーサ
        deps.clock = lambda: datetime(2030, 1, 17, 3, 0, tzinfo=timezone.utc)
        options = DiaryOptions(dates=(YESTERDAY,), today=later_today, sources=("health",))
        ledger_path = self.root / "data" / "diary-state.json"

        first = run_diary(config, options, LOGGER, deps)
        day = first.days[0]
        self.assertTrue(day.provisional)
        self.assertIn('"歩数 (歩)": 501\n', self.read(YESTERDAY), "途中経過でも値は書く")
        self.assertEqual(day.unresolved["歩数 (歩)"], "provisional")
        self.assertIn("歩数 (歩)", day.provisional_keys)
        keys = json.loads(ledger_path.read_text(encoding="utf-8"))["dates"]["2030-01-15"]["keys"]
        self.assertEqual(keys["歩数 (歩)"]["value"], 501)
        self.assertEqual(first.exit_code, 0)

        # 日付が変わった後に書き直された (確定の) 出力が来た
        final = datetime(2030, 1, 16, 1, 0, tzinfo=JST).timestamp()
        os.utime(target, (final, final))
        second = run_diary(config, options, LOGGER, deps)
        self.assertFalse(second.days[0].provisional)
        self.assertEqual(second.days[0].unresolved, {})
        self.assertEqual(second.days[0].keys["歩数 (歩)"]["resolved"], "provisional")
        self.assertEqual(json.loads(ledger_path.read_text(encoding="utf-8"))["dates"], {})


# ======================================================================
# 台帳から外れる条件 (wave 3 レビュー指摘): 利用者が書いた値 / 欠測で確定 / source を外した /
# 書き換えられない暫定値 / 1 回だけ知らせる理由 / 一回きりの通知の取りこぼし
# ======================================================================


class _LedgerRuns:
    """台帳のテストの共通処理 (:class:`DiaryTestBase` と組み合わせる)。"""

    root: Path
    config: Config

    def _ledger_setup(self) -> None:
        self.config = replace(self.config, network_grace_sec=0.0)
        self.ledger_path = self.root / "data" / "diary-state.json"

    def _run(
        self,
        today: date,
        dates: "tuple[date, ...]",
        *,
        loader: "FakeLoader | None" = None,
        weather: "Callable[..., WeatherSummary] | None" = None,
        github: "Callable[..., GitHubActivity] | None" = None,
        sources: "tuple[str, ...]" = ("weather",),
        revisit: bool = True,
        hour: int = 12,
        config: "Config | None" = None,
        notifier: "Callable[[str, str], Any] | None" = None,
    ):
        self.weather_today = today  # type: ignore[attr-defined]
        deps = self.deps(loader=loader, weather=weather, github=github)  # type: ignore[attr-defined]
        deps.clock = lambda: datetime.combine(today, time(hour, 0), tzinfo=JST)
        if notifier is not None:
            deps.notifier = notifier
        options = DiaryOptions(
            dates=dates, today=today, sources=sources, revisit_unresolved=revisit,
        )
        return run_diary(config or self.config, options, LOGGER, deps)

    @staticmethod
    def _window(today: date) -> "tuple[date, ...]":
        return resolve_dates(today=today, lookback_days=7, include_today=True)

    def _ledger(self) -> dict:
        return json.loads(self.ledger_path.read_text(encoding="utf-8"))

    @staticmethod
    def _day(result, d: date):
        return next(day for day in result.days if day.date == d)

    def _edit_note(self, d: date, old: str, new: str) -> None:
        path = self.diary / f"{d.isoformat()}.md"  # type: ignore[attr-defined]
        text = path.read_text(encoding="utf-8")
        assert old in text, old
        path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _withheld_sleep(d: date) -> HealthLoadResult:
    """睡眠だけ保留 (停止済み AutoSync の途中までの出力) した確定の結果。"""
    return HealthLoadResult(
        health=DailyHealthData(date=d, step_count=500), activity_detail=None,
        source="automation", detail="synthetic", status=STATUS_COMPLETE,
        withheld_fields=("sleep_hours",),
    )


class LedgerResolutionTest(_LedgerRuns, DiaryTestBase):
    def setUp(self) -> None:
        super().setUp()
        self._ledger_setup()

    def test_pending_weather_key_that_stays_missing_on_the_completed_page_is_dropped(self):
        # finding: 公開待ちで追跡した天気が、完成したページで欠測 (降水量に ']' がある日など)
        # だった → 台帳に 30 日残り、4 日目から毎日「天気の公開待ち」の要確認を送り続けていた
        day = T0
        self.write_note(day)

        def weather(d: date, **kwargs: Any) -> WeatherSummary:
            if d >= kwargs["today"]:
                return weather_incomplete(d, pending=True)
            return replace(weather_ok(d), precipitation=None)  # 完成した日。降水量だけ欠測

        self._run(day, (day,), weather=weather, revisit=False)
        self.assertEqual(
            self._ledger()["dates"][day.isoformat()]["keys"]["降水量"]["reason"], "weather_pending",
        )
        nxt = day + timedelta(days=1)
        second = self._run(nxt, self._window(nxt), weather=weather)
        item = self._day(second, day).keys["降水量"]
        self.assertEqual((item["action"], item["reason"]), ("missing", "no_value"))
        self.assertEqual((item["resolved"], item["resolution"]), ("weather_pending", "no_value_final"))
        self.assertIn('天気: "3時: 晴れ☀️, 6時: 曇☁️"\n', self.read(day))
        self.assertIn("降水量:\n", self.read(day))
        self.assertNotIn(day.isoformat(), self._ledger()["dates"])
        for offset in range(2, 9):
            later = day + timedelta(days=offset)
            self._run(later, self._window(later), weather=weather)
        self.assertEqual(self.notifications, [], "欠測で確定したキーの要確認を送り続けない")

    def test_withheld_key_filled_by_hand_stops_being_tracked(self):
        # finding: 保留のキーは自動では埋まらないのに、手で記入しても毎日要確認が来ていた
        self.write_note(D2)
        self._run(T0, (D2,), loader=FakeLoader({D2: _withheld_sleep(D2)}, latest=T0),
                  sources=("health",), revisit=False)
        self.assertEqual(
            self._ledger()["dates"][D2.isoformat()]["keys"]["睡眠時間 (分)"]["reason"], "withheld",
        )
        self._edit_note(D2, '"睡眠時間 (分)":\n', '"睡眠時間 (分)": 420\n')
        later = T0 + timedelta(days=1)
        result = self._run(later, self._window(later),
                           loader=FakeLoader({D2: _withheld_sleep(D2)}, latest=later),
                           sources=("health",))
        item = self._day(result, D2).keys["睡眠時間 (分)"]
        self.assertEqual((item["resolved"], item["resolution"]), ("withheld", "user_value"))
        self.assertEqual(self._ledger()["dates"], {})
        self.assertIn('"睡眠時間 (分)": 420\n', self.read(D2), "手で書いた値には触らない")

    def test_withheld_key_is_reported_once_then_tracked_quietly(self):
        # finding: 保留のキーに 4 日目から 30 日目まで毎日要確認を送っていた → 1 回だけ知らせ、
        # その後は黙って追跡を続ける (オートメーションが値を出せば埋まる)
        self.write_note(D2)

        def run(offset: int, result: "HealthLoadResult | None" = None):
            today = T0 + timedelta(days=offset)
            loader = FakeLoader({D2: result or _withheld_sleep(D2)}, latest=today)
            window = (D2,) if offset == 0 else self._window(today)
            return self._run(today, window, loader=loader, sources=("health",), revisit=offset > 0)

        for offset in range(0, 4):
            run(offset)
        self.assertEqual(self.notifications, [], "3 日ちょうどまでは知らせない")
        fourth = run(4)
        self.assertEqual(len(self.notifications), 1)
        title, body = self.notifications[0]
        self.assertIn("要確認", title)
        self.assertIn(f"{D2.isoformat()}: 睡眠時間 (分) (保留)", body)
        self.assertIn("この 1 回だけ", body)
        self.assertEqual(
            fourth.report["provisional"]["overdue"][D2.isoformat()], {"睡眠時間 (分)": "withheld"},
        )
        self.assertIs(
            self._ledger()["dates"][D2.isoformat()]["keys"]["睡眠時間 (分)"]["notified"], True,
        )
        for offset in (5, 6, 10):
            later = run(offset)
            self.assertEqual(
                later.report["provisional"]["overdue"][D2.isoformat()], {"睡眠時間 (分)": "withheld"},
                "レポートには残す",
            )
        self.assertEqual(len(self.notifications), 1, "保留の要確認は毎日は送らない")
        self.assertIn(D2.isoformat(), self._ledger()["dates"], "追跡は続ける")
        run(12, health_ok(D2))  # その後オートメーションが値を出した (window の外: fill-only)
        self.assertIn('"睡眠時間 (分)": 450\n', self.read(D2))
        self.assertEqual(self._ledger()["dates"], {})

    def test_incomplete_weather_day_is_reported_once_and_cleared_by_a_manual_fill(self):
        # 確定済みのはずの日の天気が未完成 (気象庁の欠測の疑い) も、自動ではまず埋まらないので
        # 要確認は 1 回だけ。手で記入したキーは追跡をやめる
        self.write_note(D2)

        def incomplete(d: date, **kwargs: Any) -> WeatherSummary:
            return weather_incomplete(d, pending=False) if d == D2 else weather_ok(d)

        self._run(T0, (D2,), weather=incomplete, revisit=False)
        for offset in (4, 5, 6):
            today = T0 + timedelta(days=offset)
            self._run(today, self._window(today), weather=incomplete)
        self.assertEqual(len(self.notifications), 1)
        self.assertIn("天気が未完成の日は、この 1 回だけ", self.notifications[0][1])
        self._edit_note(D2, "気温:\n", "気温: 手で記入\n")
        today = T0 + timedelta(days=7)
        result = self._run(today, self._window(today), weather=incomplete)
        item = self._day(result, D2).keys["気温"]
        self.assertEqual((item["resolved"], item["resolution"]), ("weather_incomplete", "user_value"))
        self.assertNotIn("気温", self._ledger()["dates"][D2.isoformat()]["keys"])
        self.assertIn("湿度", self._ledger()["dates"][D2.isoformat()]["keys"])
        self.assertEqual(len(self.notifications), 1)

    def test_source_removed_from_settings_stops_tracking_its_keys(self):
        # finding: source を外した後も、その source の未確定のキーを 30 日追跡して要確認を送っていた
        self.write_note(D2)
        self._run(T0, (D2,), github=lambda c, s, e, **k: github_failed([s]),
                  sources=("github",), revisit=False)
        self.assertIn(D2.isoformat(), self._ledger()["dates"])
        # 一時的に --sources weather で走らせただけ (設定は github を含む): 次の定時実行が
        # 処理するので追跡を続ける
        nxt = T0 + timedelta(days=1)
        one_off = self._run(nxt, self._window(nxt), sources=("weather",))
        self.assertEqual(self._day(one_off, D2).unresolved, {"Github": "github_incomplete"})
        # IHEALTH_SOURCES から github を外した: 今後も確定しないので追跡をやめる
        config = replace(self.config, sources=("health", "weather"))
        later = T0 + timedelta(days=2)
        result = self._run(later, self._window(later), sources=("weather",), config=config)
        item = self._day(result, D2).keys["Github"]
        self.assertEqual((item["action"], item["reason"]), ("skipped", "source_not_selected"))
        self.assertEqual((item["resolved"], item["resolution"]),
                         ("github_incomplete", "source_deselected"))
        self.assertEqual(self._ledger()["dates"], {})

    def test_provisional_value_without_a_final_value_stays_tracked_until_cleared(self):
        # 確定した出力にその値が無い: 本ツールの暫定値がノートに残っている間は追跡を続け
        # (確定値の顔で黙って残さない)、利用者が消したら (埋める値も無いので) 追跡をやめる
        self.write_note(T0)
        self._run(T0, (T0,), loader=FakeLoader({T0: health_ok(T0, steps=1200, provisional=True)},
                                                latest=T0),
                  sources=("health",), revisit=False)
        final = HealthLoadResult(
            health=DailyHealthData(date=T0, sleep_hours=7.5), activity_detail="瞑想5分",
            source="automation", detail="synthetic", status=STATUS_COMPLETE,
        )
        nxt = T0 + timedelta(days=1)
        result = self._run(nxt, self._window(nxt), loader=FakeLoader({T0: final}, latest=nxt),
                           sources=("health",))
        self.assertEqual(self._day(result, T0).unresolved, {"歩数 (歩)": "provisional"})
        self._edit_note(T0, '"歩数 (歩)": 1200\n', '"歩数 (歩)":\n')
        later = T0 + timedelta(days=2)
        result = self._run(later, self._window(later), loader=FakeLoader({T0: final}, latest=later),
                           sources=("health",))
        item = self._day(result, T0).keys["歩数 (歩)"]
        self.assertEqual((item["resolved"], item["resolution"]), ("provisional", "no_value_final"))
        self.assertEqual(self._ledger()["dates"], {})

    def test_provisional_entry_that_became_unwritable_stays_tracked_and_is_reported(self):
        # finding (+ e2e の報告): 暫定値の行が重複 (Obsidian Sync のマージ) / コメント付きに
        # なると kept + warning で書けないのに「解決」として台帳から消え、暫定値が確定値の顔で
        # 残っていた
        self.write_note(T0)
        self._run(T0, (T0,), loader=FakeLoader({T0: health_ok(T0, steps=1200, provisional=True)},
                                                latest=T0),
                  sources=("health",), revisit=False)
        self._edit_note(T0, '"歩数 (歩)": 1200\n', '"歩数 (歩)": 1200\n歩数 (歩): 1200\n')
        later = T0 + timedelta(days=4)
        result = self._run(later, self._window(later),
                           loader=FakeLoader({T0: health_ok(T0, steps=9876)}, latest=later),
                           sources=("health",))
        item = self._day(result, T0).keys["歩数 (歩)"]
        self.assertEqual(item["action"], "kept")
        self.assertTrue(item["warning"].startswith("duplicate key"), item["warning"])
        self.assertEqual(item["unresolved"], "unwritable")
        self.assertNotIn("resolved", item)
        self.assertEqual(
            self._ledger()["dates"][T0.isoformat()]["keys"],
            {"歩数 (歩)": {"reason": "unwritable", "since": "2030-01-16T03:00:00+00:00", "value": 1200}},
        )
        self.assertEqual(len(self.notifications), 1)
        title, body = self.notifications[0]
        self.assertIn("要確認", title)
        self.assertIn("歩数 (歩) (コメント / 重複キーのため書き換えられない)", body)
        self.assertIn("1 つにまとめる", body)
        # 利用者が 1 つにまとめた → 次の実行で確定値に置き換わり台帳から外れる
        self._edit_note(T0, '"歩数 (歩)": 1200\n歩数 (歩): 1200\n', '"歩数 (歩)": 1200\n')
        nxt = later + timedelta(days=1)
        self._run(nxt, self._window(nxt), loader=FakeLoader({T0: health_ok(T0, steps=9876)}, latest=nxt),
                  sources=("health",))
        self.assertIn('"歩数 (歩)": 9876\n', self.read(T0))
        self.assertEqual(self._ledger()["dates"], {})

    def test_comment_added_to_a_provisional_value_depends_on_the_value(self):
        # 暫定値の行にコメントが付いた: 値が暫定値のままなら書き換えられない (unwritable)、
        # 利用者が値も書き換えていたら利用者の値 (追跡をやめる)
        cases = (
            (T0, "1200  # メモ", "unwritable"),
            (T0 + timedelta(days=40), "5000  # 手で数えた", None),   # 別の日 (前の日は台帳の外)
        )
        for day, written, expected in cases:
            with self.subTest(written=written):
                self.write_note(day)
                self._run(day, (day,),
                          loader=FakeLoader({day: health_ok(day, steps=1200, provisional=True)},
                                            latest=day),
                          sources=("health",), revisit=False)
                self._edit_note(day, '"歩数 (歩)": 1200\n', f'"歩数 (歩)": {written}\n')
                nxt = day + timedelta(days=1)
                result = self._run(nxt, (day,),
                                   loader=FakeLoader({day: health_ok(day, steps=9876)}, latest=nxt),
                                   sources=("health",), revisit=False)
                item = self._day(result, day).keys["歩数 (歩)"]
                self.assertEqual(item["action"], "kept")
                self.assertTrue(item["warning"].startswith("comment present"), item["warning"])
                self.assertEqual(item.get("unresolved"), expected)
                entry = self._ledger()["dates"].get(day.isoformat(), {}).get("keys", {})
                if expected is None:
                    self.assertEqual(item["resolution"], "user_value")
                    self.assertNotIn("歩数 (歩)", entry)
                else:
                    self.assertEqual(entry["歩数 (歩)"]["reason"], expected)
                    self.assertEqual(entry["歩数 (歩)"]["value"], 1200)

    def test_comment_on_a_value_the_user_wrote_is_not_tracked(self):
        # 本ツールの暫定値が無い entry のコメント (W3-3 の kept + warning) は従来どおりレポートだけ
        self.write_note(D2, note_text(D2, steps="777  # 手で測った"))
        result = self._run(T0, (D2,), loader=FakeLoader({D2: health_ok(D2)}, latest=T0),
                           sources=("health",), revisit=False)
        item = result.days[0].keys["歩数 (歩)"]
        self.assertEqual(item["action"], "kept")
        self.assertTrue(item["warning"].startswith("comment present"), item["warning"])
        self.assertNotIn("unresolved", item)
        self.assertFalse(self.ledger_path.exists(), "追跡するものが無い")

    def test_only_the_night_sleep_of_a_segment_day_is_tracked_as_provisional(self):
        # finding: segment 形式の夜主睡眠が前日ファイル不足で暫定なだけなのに、その日の健康の
        # キー全部を暫定値として台帳で追跡していた (実 HealthLoader + 実パーサ)
        docs = self.root / "Documents"
        folder = docs / "Sample Automation"
        folder.mkdir(parents=True)
        src = AUTOMATION_FIXTURES / "segments" / "HealthAutoExport-2030-02-02.json"
        shutil.copyfile(src, folder / src.name)   # 前日 (02-01) のファイルは無い
        final = datetime(2030, 2, 3, 12, 0, tzinfo=timezone.utc).timestamp()
        os.utime(folder / src.name, (final, final))
        day = date(2030, 2, 2)
        self.write_note(day, note_text(day).replace('"睡眠時間 (分)":\n', '"睡眠時間 (分)":\n昼寝時間:\n'))
        config = replace(self.config, health_export_dir=docs)
        deps = self.deps()
        deps.health_loader_factory = None   # 実 HealthLoader
        deps.clock = lambda: datetime(2030, 2, 5, 3, 0, tzinfo=timezone.utc)
        options = DiaryOptions(dates=(day,), today=date(2030, 2, 5), sources=("health",))
        result = run_diary(config, options, LOGGER, deps)
        (report,) = result.days
        self.assertTrue(report.provisional)
        self.assertEqual(report.source_details["health"]["provisional_fields"], ["sleep_hours"])
        self.assertEqual(report.unresolved, {"睡眠時間 (分)": "provisional"})
        self.assertIn('"歩数 (歩)": 2000\n', self.read(day))
        self.assertEqual(
            list(self._ledger()["dates"][day.isoformat()]["keys"]), ["睡眠時間 (分)"],
        )

    def test_health_loader_gets_an_aware_clock(self):
        # e2e の報告: Mac のローカル時刻 (naive) に直して渡していたため、夏時間の切り替わり
        # (同じ壁時計の時刻が 2 回ある 1 時間) で日の終わりの判定がずれていた
        self.write_note(D2)
        seen: "list[datetime]" = []
        moment = datetime(2030, 4, 6, 15, 20, tzinfo=timezone.utc)

        def factory(config: Config, logger: logging.Logger, now: "Callable[[], datetime]") -> FakeLoader:
            seen.append(now())
            return FakeLoader({D2: health_ok(D2)})

        deps = self.deps()
        deps.health_loader_factory = factory
        deps.clock = lambda: moment
        self.run_diary((D2,), deps, sources=("health",))
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(seen[0].utcoffset(), "aware のまま渡す")
        self.assertEqual(seen[0], moment)
        naive = self.deps()
        naive.health_loader_factory = factory
        naive.clock = lambda: moment.replace(tzinfo=None)   # naive な時計は UTC とみなす
        self.run_diary((D2,), naive, sources=("health",))
        self.assertEqual(seen[1], moment)
        self.assertIsNotNone(seen[1].utcoffset())


class OneShotNoticeTest(_LedgerRuns, DiaryTestBase):
    """一回きりの「要確認」(台帳から外した日 / 追跡しない古い日) を取りこぼさない。"""

    def setUp(self) -> None:
        super().setUp()
        self._ledger_setup()

    @staticmethod
    def _incomplete(d: date, **kwargs: Any) -> WeatherSummary:
        return weather_incomplete(d, pending=False)

    def test_untracked_days_found_after_an_earlier_notice_today_are_still_reported(self):
        # finding: 朝の実行がデータの鮮度の通知 (停止の疑い) を送った日は、後の backfill で
        # 見つかった 30 日より前の未確定の値 (台帳に入れず 1 回だけ知らせる約束) が日付だけの
        # 抑止で通知されずに失われていた
        self.write_note(T0)
        stale = FakeLoader({T0: health_ok(T0, provisional=True)}, latest=T0 - timedelta(days=5))
        self._run(T0, (T0,), loader=stale, sources=("health",), revisit=False, hour=7)
        self.assertEqual(len(self.notifications), 1)
        self.assertIn("停止の疑い", self.notifications[0][0])
        old = T0 - timedelta(days=PROVISIONAL_REVISIT_DAYS + 10)
        self.write_note(old)
        result = self._run(T0, (old,), weather=self._incomplete, revisit=False, hour=10)
        self.assertEqual(len(self.notifications), 2, "朝の通知とは別の内容なので送る")
        title, body = self.notifications[1]
        self.assertIn("要確認", title)
        self.assertIn(old.isoformat(), body)
        self.assertIn("自動では処理し直しません", body)
        self.assertNotIn("停止の疑い", body, "今日すでに送った部分は繰り返さない")
        self.assertIs(result.report["notifications"]["stale"]["delivered"], True)
        again = self._run(T0, (old,), weather=self._incomplete, revisit=False, hour=11)
        self.assertEqual(len(self.notifications), 2, "同じ内容は同じ日に 2 回送らない")
        self.assertTrue(again.report["notifications"]["stale"]["suppressed"])

    def test_expired_days_are_reported_again_when_the_reminder_could_not_be_created(self):
        # finding: 台帳から外した (30 日超) 日を、リマインダーを作れなかった実行の前に台帳から
        # 消していたため、次の実行で知らせる内容が無くなっていた
        day = T0 - timedelta(days=PROVISIONAL_REVISIT_DAYS + 1)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path.write_text(json.dumps({"version": 2, "dates": {day.isoformat(): {"keys": {
            "気温": {"reason": "weather_incomplete", "since": "2029-12-10T00:00:00+00:00"},
        }}}}), encoding="utf-8")
        attempts: "list[str]" = []
        yesterday = (T0 - timedelta(days=1),)
        first = self._run(T0, yesterday, notifier=lambda title, body: attempts.append(body) or False)
        self.assertEqual(len(attempts), 1)
        self.assertIn(day.isoformat(), attempts[0])
        self.assertIs(first.report["notifications"]["stale"]["delivered"], False)
        saved = self._ledger()
        self.assertEqual(saved["dates"], {})
        self.assertEqual(
            saved["unreported"], {"expired": {day.isoformat(): {"気温": "weather_incomplete"}}},
            "送れなかった一回きりの通知は台帳に残す",
        )
        second = self._run(T0, yesterday, hour=13)
        self.assertEqual(len(self.notifications), 1)
        self.assertIn(day.isoformat(), self.notifications[0][1])
        self.assertIn("自動の再処理をやめました", self.notifications[0][1])
        self.assertEqual(
            second.report["provisional"]["carried_over"],
            {"expired": {day.isoformat(): {"気温": "weather_incomplete"}}},
        )
        self.assertNotIn("unreported", self._ledger(), "送れたら台帳から消す")
        self._run(T0, yesterday, hour=14)
        self.assertEqual(len(self.notifications), 1)

    def test_untracked_days_survive_a_failed_reminder(self):
        old = T0 - timedelta(days=PROVISIONAL_REVISIT_DAYS + 10)
        self.write_note(old)
        self._run(T0, (old,), weather=self._incomplete, revisit=False,
                  notifier=lambda title, body: False)
        self.assertEqual(
            set(self._ledger()["unreported"]["untracked"][old.isoformat()]),
            {"気温", "湿度", "気圧", "天気", "日照時間", "降水量"},
        )
        self.assertEqual(self._ledger()["dates"], {}, "台帳 (追跡) には入れない")
        nxt = T0 + timedelta(days=1)
        self._run(nxt, (nxt - timedelta(days=1),), weather=lambda d, **k: weather_ok(d))
        self.assertEqual(len(self.notifications), 1)
        self.assertIn(old.isoformat(), self.notifications[0][1])
        self.assertNotIn("unreported", self._ledger())

    def test_carried_item_found_again_is_sent_once_and_cleared(self):
        # 送れなかった項目と同じ内容をこの実行でも見つけた (同じ古い日の backfill をやり直した)
        old = T0 - timedelta(days=PROVISIONAL_REVISIT_DAYS + 10)
        self.write_note(old)
        self._run(T0, (old,), weather=self._incomplete, revisit=False,
                  notifier=lambda title, body: False)
        self.assertIn("unreported", self._ledger())
        nxt = T0 + timedelta(days=1)
        self._run(nxt, (old,), weather=self._incomplete, revisit=False)
        self.assertEqual(len(self.notifications), 1)
        self.assertEqual(self.notifications[0][1].count(old.isoformat()), 1, "同じ日を 2 回並べない")
        self.assertNotIn("unreported", self._ledger(), "送れたら繰り越しも消す")
        later = T0 + timedelta(days=2)
        self._run(later, (later - timedelta(days=1),), weather=lambda d, **k: weather_ok(d))
        self.assertEqual(len(self.notifications), 1)

    def test_dry_run_does_not_touch_unreported_items(self):
        day = T0 - timedelta(days=PROVISIONAL_REVISIT_DAYS + 1)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": 2, "dates": {}, "unreported": {"expired": {
            day.isoformat(): {"気温": "weather_incomplete"},
        }}})
        self.ledger_path.write_text(payload, encoding="utf-8")
        self.weather_today = T0
        deps = self.deps()
        deps.clock = lambda: datetime.combine(T0, time(12, 0), tzinfo=JST)
        options = DiaryOptions(dates=(T0 - timedelta(days=1),), today=T0, sources=("weather",),
                               dry_run=True, revisit_unresolved=True)
        result = run_diary(self.config, options, LOGGER, deps)
        self.assertEqual(self.notifications, [])
        self.assertTrue(result.report["notifications"]["stale"]["dry_run"])
        self.assertEqual(self.ledger_path.read_text(encoding="utf-8"), payload)
        # 今日すでに送った (台帳から消すのに失敗して残っている) 項目でも、dry-run は台帳を書かない
        from ihealth.diary import ONE_SHOT_EXPIRED, _OneShot
        sent = _OneShot(ONE_SHOT_EXPIRED, day, (("気温", "weather_incomplete"),)).signature()
        self.logs.mkdir(parents=True, exist_ok=True)
        (self.logs / ".freshness_notified").write_text(
            json.dumps({"date": T0.isoformat(), "signatures": [sent]}), encoding="utf-8",
        )
        again = run_diary(self.config, options, LOGGER, deps)
        self.assertTrue(again.report["notifications"]["stale"]["suppressed"])
        self.assertEqual(self.ledger_path.read_text(encoding="utf-8"), payload)


# ======================================================================
# 実行レポート (W3-5): 開始時に予定の日付一覧で作り、日付ごとに journal へ追記する
# ======================================================================


class RunReportTest(DiaryTestBase):
    """途中で落ちても / 保存に失敗しても、全数台帳 (予定 = 処理済み + 未処理) が残る。"""

    def _files(self) -> "tuple[list[Path], list[Path]]":
        return (
            sorted(self.logs.glob("diary-report-*.json")),
            sorted(self.logs.glob("diary-report-*.jsonl")),
        )

    @staticmethod
    def _lines(path: Path) -> "list[dict]":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_finished_report_lists_planned_dates_and_drops_the_journal(self):
        for d in (D2, D3):
            self.write_note(d)
        result = self.run_diary((D2, D3), self.deps(), sources=("health",))
        reports, journals = self._files()
        self.assertEqual(reports, [result.report_path])
        self.assertEqual(journals, [], "確定したら journal は消す (中身はレポートに入った)")
        data = json.loads(result.report_path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 2)
        self.assertEqual(data["status"], "finished")
        self.assertEqual(data["planned_dates"], ["2030-01-13", "2030-01-14"])
        self.assertEqual(data["processed"], 2)
        self.assertEqual(data["remaining_dates"], [])
        self.assertEqual([day["date"] for day in data["dates"]], ["2030-01-13", "2030-01-14"])

    def test_crash_mid_run_leaves_planned_dates_and_processed_results(self):
        for d in (D1, D2, D3):
            self.write_note(d)

        def load(d: date) -> HealthLoadResult:
            if d == D2:
                raise KeyboardInterrupt  # 途中でプロセスが止まった
            return health_ok(d)

        with self.assertRaises(KeyboardInterrupt):
            self.run_diary((D1, D2, D3), self.deps(loader=FakeLoader(load)), sources=("health",))
        reports, journals = self._files()
        self.assertEqual(len(reports), 1)
        self.assertEqual(len(journals), 1)
        started = json.loads(reports[0].read_text(encoding="utf-8"))
        self.assertEqual(started["status"], "running")
        self.assertEqual(started["planned_dates"], ["2030-01-12", "2030-01-13", "2030-01-14"])
        self.assertEqual(started["journal"], journals[0].name)
        lines = self._lines(journals[0])
        self.assertEqual([line["type"] for line in lines], ["start", "day"])
        self.assertEqual(lines[0]["planned_dates"], started["planned_dates"])
        self.assertEqual(lines[1]["date"], "2030-01-12")
        self.assertEqual(lines[1]["keys"]["歩数 (歩)"]["action"], "update")
        self.assertIn('"歩数 (歩)": 4321\n', self.read(D1))
        self.assertEqual(self.read(D3), note_text(D3))

    def test_journal_append_failure_stops_before_more_notes_are_changed(self):
        for d in (D1, D2, D3):
            self.write_note(d)
        real_append = __import__("ihealth.diary", fromlist=["_append_jsonl"])._append_jsonl
        calls = {"n": 0}

        def flaky_append(path: Path, item: Any) -> None:
            calls["n"] += 1
            if calls["n"] == 3:   # start, D1 の後は書けたが D2 の後で容量不足
                raise OSError(28, "No space left on device")
            real_append(path, item)

        with mock.patch("ihealth.diary._append_jsonl", side_effect=flaky_append):
            result = self.run_diary((D1, D2, D3), self.deps(), sources=("health",))
        self.assertEqual(result.exit_code, 12)
        self.assertEqual([day.date for day in result.days], [D1, D2])
        self.assertEqual(self.read(D3), note_text(D3), "記録できない書き換えはしない")
        data = json.loads(result.report_path.read_text(encoding="utf-8"))
        self.assertEqual(data["status"], "interrupted")
        self.assertEqual(data["processed"], 2)
        self.assertEqual(data["remaining_dates"], ["2030-01-14"])
        # 全数台帳: 予定 N 日 = 処理済み (結果あり) + 未処理 (重複も漏れも無い)
        processed = [day["date"] for day in data["dates"]]
        self.assertEqual(processed, ["2030-01-12", "2030-01-13"])
        self.assertEqual(sorted(processed + data["remaining_dates"]), data["planned_dates"])
        self.assertEqual(len(self.notifications), 1)
        title, body = self.notifications[0]
        self.assertIn("実行レポート", title)
        self.assertIn("残り 1 日は未処理", body)
        self.assertIn("No space left on device", body)

    def test_report_that_cannot_be_created_blocks_every_write(self):
        self.write_note(D2)
        self.logs.write_text("logs はファイルになっている", encoding="utf-8")
        with self.assertLogs("test-diary", level="ERROR"):
            result = self.run_diary((D2,), self.deps(), sources=("health", "github"))
        self.assertEqual(result.exit_code, 12)
        self.assertEqual(result.days, ())
        self.assertIsNone(result.report_path)
        self.assertEqual(self.read(D2), note_text(D2), "日記には一切触れない")
        self.assertEqual(self.collector_calls, [], "ネットワーク待ち・GitHub の収集もしない")
        self.assertEqual(self.probe_calls, [])
        self.assertFalse((self.root / "data" / "obsidian-backup").exists())
        self.assertFalse((self.root / "data" / "diary-state.json").exists())
        self.assertEqual(len(self.notifications), 1)
        title, body = self.notifications[0]
        self.assertIn("実行レポートを保存できません", title)
        self.assertIn("日記を更新しませんでした", body)
        self.assertEqual(result.report["remaining_dates"], ["2030-01-13"])

    def test_final_report_failure_is_a_failure_and_keeps_the_journal(self):
        self.write_note(D2)
        diary_module = __import__("ihealth.diary", fromlist=["_write_text_atomic"])
        real_write = diary_module._write_text_atomic
        report_writes: "list[str]" = []

        def failing_final(path: Path, text: str) -> None:
            if path.name.startswith("diary-report-") and path.suffix == ".json":
                report_writes.append(path.name)
                if len(report_writes) > 1:   # 開始時は書けたが、確定で容量不足
                    raise OSError(28, "No space left on device")
            real_write(path, text)

        with mock.patch("ihealth.diary._write_text_atomic", side_effect=failing_final):
            with self.assertLogs("test-diary", level="ERROR"):
                result = self.run_diary((D2,), self.deps(), sources=("health",))
        self.assertEqual(result.exit_code, 12, "台帳なしで成功終了しない")
        self.assertIn('"歩数 (歩)": 4321\n', self.read(D2))
        reports, journals = self._files()
        self.assertEqual(len(journals), 1, "確定できなければ journal を残す")
        lines = self._lines(journals[0])
        self.assertEqual([line["type"] for line in lines], ["start", "day", "finish"])
        self.assertEqual(lines[-1]["exit_code"], 12)
        self.assertEqual(
            json.loads(reports[0].read_text(encoding="utf-8"))["status"], "running",
        )
        title, body = self.notifications[0]
        self.assertIn("実行レポートを保存できません", title)
        self.assertIn(journals[0].name, body)


class LaunchAgentScheduleTest(unittest.TestCase):
    """LaunchAgent: obsidian は 07:00 / 12:00 / 21:00 (SPEC §2-E / v2 §4.6)、それ以外は 07:00 のみ。"""

    PLIST = "com.ihealthrelay.daemon.plist"

    def test_schedule(self):
        with open(PROJECT_ROOT / "LaunchAgent" / self.PLIST, "rb") as fh:
            plist = plistlib.load(fh)
        self.assertEqual(
            plist["StartCalendarInterval"],
            [{"Hour": 7, "Minute": 0}, {"Hour": 12, "Minute": 0}, {"Hour": 21, "Minute": 0}],
        )
        # 起動するのは専用ランチャー (install-launchd.sh が置換。tests/test_launcher.py 参照)
        self.assertEqual(plist["ProgramArguments"], ["__PROGRAM__"])
        self.assertFalse(plist["RunAtLoad"])

    @unittest.skipUnless(shutil.which("bash"), "bash が無い環境")
    def test_installer_schedule_follows_ihealth_publisher(self):
        # Notion / Slack 等の単日 sink は従来どおり 1 日 1 回 (重複投稿・通知の 3 倍化を防ぐ)。
        # launchctl には触れない --print-plist で、置換後の plist だけを確かめる
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "proj"
            (project / "bin" / "launcher").mkdir(parents=True)
            (project / "LaunchAgent").mkdir()
            for name in ("install-launchd.sh", "ihealth-run", "launcher/ihealth-launcher.c"):
                shutil.copy2(PROJECT_ROOT / "bin" / name, project / "bin" / name)
            shutil.copy2(PROJECT_ROOT / "LaunchAgent" / self.PLIST, project / "LaunchAgent" / self.PLIST)
            home = root / "home"
            home.mkdir()
            # C コンパイラがあれば専用ランチャー、無ければ bin/ihealth-run (install-launchd.sh と同じ順で探す)。
            # installer はプロジェクトの実体のパスを使う (macOS の一時ディレクトリは /var → /private/var)
            has_cc = (os.path.isfile("/usr/bin/cc") and os.access("/usr/bin/cc", os.X_OK)) \
                or shutil.which("cc", path="/usr/bin:/bin") is not None
            program = (
                home / "Library" / "Application Support" / "ihealth-relay" / "ihealth-launcher"
                if has_cc else project.resolve() / "bin" / "ihealth-run"
            )
            cases = (
                ("IHEALTH_PUBLISHER=obsidian\n", [7, 12, 21]),
                ('IHEALTH_PUBLISHER="Obsidian"\r\n', [7, 12, 21]),
                ("IHEALTH_PUBLISHER=notion\n", [7]),
                ("IHEALTH_PUBLISHER=slack\n", [7]),
                ("#IHEALTH_PUBLISHER=obsidian\n", [7]),
                (None, [7]),
            )
            for env_text, hours in cases:
                with self.subTest(env=env_text):
                    env_file = project / ".env"
                    if env_text is None:
                        if env_file.exists():
                            env_file.unlink()
                    else:
                        env_file.write_text(env_text, encoding="utf-8")
                    proc = subprocess.run(
                        ["bash", str(project / "bin" / "install-launchd.sh"), "--print-plist"],
                        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
                        capture_output=True, timeout=60, check=True,
                    )
                    plist = plistlib.loads(proc.stdout)
                    self.assertEqual([entry["Hour"] for entry in plist["StartCalendarInterval"]], hours)
                    self.assertEqual(plist["ProgramArguments"], [str(program)])
                    self.assertEqual(
                        plist["StandardErrorPath"], f"{home}/Library/Logs/ihealth-relay/launchd.log",
                    )
            self.assertEqual(list(home.iterdir()), [], "--print-plist は何も作らない")


if __name__ == "__main__":
    unittest.main()
