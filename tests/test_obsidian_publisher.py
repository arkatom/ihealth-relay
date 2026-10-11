"""``ihealth.publishers.obsidian.ObsidianPublisher`` の回帰テスト.

すべて ``tempfile.TemporaryDirectory`` 内の合成 vault で行う (実 vault・実 iCloud・
ネットワークには触れない). fixture の値・本文は合成.
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
import time
import unicodedata
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from unittest import mock

import ihealth.frontmatter as frontmatter_module
from ihealth.frontmatter import (
    WARNING_COMMENT_PRESENT,
    WARNING_DUPLICATE_KEY,
    FieldChange,
    split_frontmatter,
    upsert_frontmatter,
)
from ihealth.models import DailyHealthData
from ihealth.publishers.obsidian import (
    DEFAULT_PROPERTY_MAP,
    HEALTH_FIELDS,
    VALUE_CONVERTERS,
    EditInvariantError,
    NotePublishOutcome,
    ObsidianConfigError,
    ObsidianPublishError,
    ObsidianPublisher,
    TemplateExpansionError,
    expand_template,
    verify_edit_invariants,
)
from ihealth.records import WEATHER_FIELDS, DiaryRecord


D = date(2030, 1, 2)  # 水曜日
JST = timezone(timedelta(hours=9))
#: 「今日」= 2030-01-05 (JST 13:05). D は 3 日前なので既定の作成制限 (今日・昨日は作らない) に掛からない.
FIXED_NOW = datetime(2030, 1, 5, 4, 5, 6, tzinfo=timezone.utc)

#: 実テンプレート (テンプレート/毎日の習慣.md) の構造を模した合成テンプレート.
TEMPLATE = (
    "<%*\n"
    "// Templater: 合成テンプレート\n"
    "const m = tp.file.title.match(/^(\\d{4}-\\d{2}-\\d{2})$/);\n"
    "const dateStr = m ? m[1] : tp.date.now(\"YYYY-MM-DD\");\n"
    "const weekday = tp.date.now(\"ddd\", 0, dateStr, \"YYYY-MM-DD\");\n"
    "-%>\n"
    "---\n"
    "名前: 毎日の習慣 <% dateStr %>\n"
    "日付: <% dateStr %>\n"
    "日記: false\n"
    "瞑想: []\n"
    "\"歩数 (歩)\":\n"
    "\"睡眠時間 (分)\":\n"
    "昼寝時間:\n"
    "\"消費カロリー (kcal)\":\n"
    "\"移動距離 (km)\":\n"
    "\"安静時心拍数 (bpm)\":\n"
    "\"平均心拍数 (bpm)\":\n"
    "\"最大心拍数 (bpm)\":\n"
    "\"酸素飽和度 (%)\":\n"
    "運動強度スコア:\n"
    "\"瞑想時間 (分)\":\n"
    "\"瞑想回数 (回)\":\n"
    "\"アクティビティ時間 (分)\":\n"
    "\"体重 (kg)\":\n"
    "\"体脂肪率 (%)\":\n"
    "気温:\n"
    "湿度:\n"
    "気圧:\n"
    "天気:\n"
    "日照時間:\n"
    "降水量:\n"
    "アクティビティ詳細:\n"
    "参照URLs: \"[サンプル](https://example.com/a)\"\n"
    "アウトプット:\n"
    "Github:\n"
    "---\n"
    "# 毎日の習慣 <% dateStr %>（<% weekday %>）\n"
    "\n"
    "## TODO\n"
    "- [ ] \n"
)

#: Obsidian 再シリアライズ後の既存ノート (plain key, block list).
EXISTING_NOTE = (
    "---\n"
    "名前: 毎日の習慣 2030-01-02\n"
    "日付: 2030-01-02\n"
    "朝散歩:\n"
    "  - カーテンを開ける\n"
    "歩数 (歩):\n"
    "睡眠時間 (分):\n"
    "昼寝時間:\n"
    "体重 (kg):\n"
    "気温:\n"
    "天気:\n"
    "Github:\n"
    "---\n"
    "# 毎日の習慣 2030-01-02（水）\n"
    "\n"
    "- 合成の本文メモ\n"
    "\n"
    "# 🤖 2030-01-02の日報\n"
    "| 種類 | 時間 |\n"
)


def _logger() -> logging.Logger:
    return logging.getLogger("test-obsidian-publisher")


class _InfoLogsEnabled:
    """tests/__init__.py の ``logging.disable(INFO)`` を一時的に解除する (INFO ログの検証用)."""

    def __enter__(self) -> None:
        self._previous = logging.root.manager.disable
        logging.disable(logging.NOTSET)

    def __exit__(self, *exc_info: object) -> None:
        logging.disable(self._previous)


def _health(**values: object) -> DailyHealthData:
    return DailyHealthData(date=D, **values)  # type: ignore[arg-type]


def _weather(**overrides: object) -> SimpleNamespace:
    base = {
        "weather": "3時: 快晴☀️☀️, 6時: 曇☁️",
        "temperature": "朝:平均1.0℃, 昼:平均2.0℃, 夜:平均3.0℃（最高:4.0℃, 最低:0.5℃）",
        "humidity": "朝:平均50.0%, 昼:平均40.0%, 夜:平均60.0%（最高:70%, 最低:30%）",
        "precipitation": "朝:0.0mm, 昼:0.0mm, 夜:0.5mm",
        "pressure": "1-6時:平均1010.0hPa, 7-12時:平均1011.0hPa",
        "sunshine": "5.5時間",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _VaultCase(unittest.TestCase):
    """tempdir に合成 vault を作る共通基底."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.vault = self.root / "vault"
        self.diary = self.vault / "日記"
        self.diary.mkdir(parents=True)
        (self.vault / "テンプレート").mkdir()
        self.template = self.vault / "テンプレート" / "毎日の習慣.md"
        self.template.write_text(TEMPLATE, encoding="utf-8")
        self.backup_dir = self.root / "obsidian-backup"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make_publisher(self, **kwargs: object) -> ObsidianPublisher:
        params: "dict[str, object]" = {
            "vault_dir": self.vault,
            "logger": _logger(),
            "backup_dir": self.backup_dir,
            "clock": lambda: FIXED_NOW,
            "sleep": lambda _sec: None,
            "tz": JST,
        }
        params.update(kwargs)
        return ObsidianPublisher(**params)  # type: ignore[arg-type]

    def write_note(self, relative: str, text: str) -> Path:
        path = self.diary / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        return path

    def tree(self) -> "dict[str, bytes]":
        """root 以下の全ファイル (隠し含む) の内容."""
        result: "dict[str, bytes]" = {}
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                path = Path(dirpath) / name
                result[str(path.relative_to(self.root))] = path.read_bytes()
        return result


# ---------------------------------------------------------------------------
# マップ / 値変換
# ---------------------------------------------------------------------------

class TestPropertyMap(unittest.TestCase):
    def test_default_map_order_follows_template(self):
        self.assertEqual(
            list(DEFAULT_PROPERTY_MAP.values()),
            [
                "歩数 (歩)", "睡眠時間 (分)", "昼寝時間", "消費カロリー (kcal)", "移動距離 (km)",
                "安静時心拍数 (bpm)", "平均心拍数 (bpm)", "最大心拍数 (bpm)", "酸素飽和度 (%)",
                "運動強度スコア", "瞑想時間 (分)", "瞑想回数 (回)", "アクティビティ時間 (分)",
                "体重 (kg)", "体脂肪率 (%)", "気温", "湿度", "気圧", "天気", "日照時間", "降水量",
                "アクティビティ詳細", "Github",
            ],
        )

    def test_map_is_read_only(self):
        with self.assertRaises(TypeError):
            DEFAULT_PROPERTY_MAP["step_count"] = "x"  # type: ignore[index]

    def test_health_fields_match_model(self):
        from dataclasses import fields as dataclass_fields
        model = {f.name for f in dataclass_fields(DailyHealthData)} - {"date"}
        self.assertEqual(set(HEALTH_FIELDS), model)
        self.assertEqual(set(VALUE_CONVERTERS), model)


class TestBuildUpdates(_VaultCase):
    def test_health_conversions_and_order(self):
        pub = self.make_publisher()
        # 値はすべて合成 (実測値は使わない)
        health = _health(
            step_count=5555,
            sleep_hours=7.25,
            nap_hours=45 / 60,
            active_energy_kcal=1234.56,
            distance_km=3.06,
            heart_rate_resting=58.04,
            heart_rate_avg=71.26,
            heart_rate_max=150.0,
            oxygen_saturation=96.44,
            exercise_intensity_score=24.0,
            mindful_minutes=20.4,
            mindful_sessions=2,
            exercise_minutes=30.6,
            body_mass_kg=61.0,
            body_fat_percentage=15.84,
        )
        updates = pub.build_updates(DiaryRecord(date=D, health=health))
        self.assertEqual(
            updates,
            {
                "歩数 (歩)": 5555,
                "睡眠時間 (分)": 435,
                "昼寝時間": 45,
                "消費カロリー (kcal)": 1234.6,
                "移動距離 (km)": 3.1,
                "安静時心拍数 (bpm)": 58.0,
                "平均心拍数 (bpm)": 71.3,
                "最大心拍数 (bpm)": 150.0,
                "酸素飽和度 (%)": 96.4,
                "運動強度スコア": 24.0,
                "瞑想時間 (分)": 20,
                "瞑想回数 (回)": 2,
                "アクティビティ時間 (分)": 31,
                "体重 (kg)": 61.0,
                "体脂肪率 (%)": 15.8,
            },
        )
        self.assertEqual(list(updates), [DEFAULT_PROPERTY_MAP[n] for n in HEALTH_FIELDS])
        self.assertIsInstance(updates["睡眠時間 (分)"], int)
        self.assertIsInstance(updates["瞑想時間 (分)"], int)

    def test_none_fields_and_empty_strings_skipped(self):
        pub = self.make_publisher()
        record = DiaryRecord(
            date=D,
            health=_health(step_count=10),
            activity_detail="",
            weather=_weather(humidity=None, sunshine="  "),
            github=None,
        )
        updates = pub.build_updates(record)
        self.assertEqual(
            list(updates),
            ["歩数 (歩)", "気温", "気圧", "天気", "降水量"],
        )

    def test_text_fields_and_full_order(self):
        pub = self.make_publisher()
        record = DiaryRecord(
            date=D, health=_health(step_count=1), activity_detail="瞑想5分、ウォーキング20分",
            weather=_weather(), github="該当なし",
        )
        updates = pub.build_updates(record)
        self.assertEqual(
            list(updates),
            ["歩数 (歩)", "気温", "湿度", "気圧", "天気", "日照時間", "降水量", "アクティビティ詳細", "Github"],
        )
        self.assertEqual(updates["Github"], "該当なし")
        self.assertEqual(updates["日照時間"], "5.5時間")

    def test_weather_object_with_missing_attributes(self):
        pub = self.make_publisher()
        updates = pub.build_updates(DiaryRecord(date=D, weather=SimpleNamespace(weather="晴")))
        self.assertEqual(updates, {"天気": "晴"})

    def test_invalid_health_values_raise(self):
        pub = self.make_publisher()
        for name, bad in (("step_count", True), ("distance_km", float("nan")), ("body_mass_kg", "61")):
            health = _health()
            # parser バグで型違反/NaN が紛れ込んだ状況を模す (DailyHealthData の検証を迂回)
            object.__setattr__(health, name, bad)
            with self.assertRaises(ObsidianPublishError, msg=name):
                pub.build_updates(DiaryRecord(date=D, health=health))

    def test_non_string_text_raises(self):
        pub = self.make_publisher()
        with self.assertRaises(ObsidianPublishError):
            pub.build_updates(DiaryRecord(date=D, github=123))  # type: ignore[arg-type]

    def test_record_rejects_mismatched_health_date(self):
        with self.assertRaises(ValueError):
            DiaryRecord(date=D, health=DailyHealthData(date=D + timedelta(days=1)))

    def test_weather_fields_constant(self):
        self.assertEqual(
            set(WEATHER_FIELDS),
            {"weather", "temperature", "humidity", "precipitation", "pressure", "sunshine"},
        )


class TestConstructorValidation(_VaultCase):
    def test_overrides_applied(self):
        pub = self.make_publisher(property_overrides={"step_count": "Steps"})
        updates = pub.build_updates(DiaryRecord(date=D, health=_health(step_count=5)))
        self.assertEqual(updates, {"Steps": 5})
        self.assertEqual(pub.property_map["step_count"], "Steps")

    def test_unknown_override_rejected(self):
        with self.assertRaises(ObsidianPublishError):
            self.make_publisher(property_overrides={"steps": "Steps"})

    def test_override_collision_rejected(self):
        with self.assertRaises(ObsidianPublishError):
            self.make_publisher(property_overrides={"step_count": "Github"})
        with self.assertRaises(ObsidianPublishError):
            self.make_publisher(property_overrides={"github": "  "})

    def test_date_property_collision_rejected(self):
        with self.assertRaises(ObsidianPublishError):
            self.make_publisher(property_overrides={"step_count": "日付"})

    def test_missing_dirs_rejected(self):
        with self.assertRaises(ObsidianPublishError):
            self.make_publisher(vault_dir=self.root / "nope")
        with self.assertRaises(ObsidianPublishError):
            self.make_publisher(diary_dir="存在しない")

    def test_backup_dir_inside_vault_rejected(self):
        with self.assertRaises(ObsidianPublishError):
            self.make_publisher(backup_dir=self.vault / ".backup")
        with self.assertRaises(ObsidianPublishError):
            self.make_publisher(backup_dir=self.vault)

    def test_invalid_create_missing_rejected(self):
        with self.assertRaises(ObsidianPublishError):
            self.make_publisher(create_missing="always")

    def test_config_errors_use_config_subclass(self):
        # 統合層が ConfigAppError (設定不備) と書込失敗を区別できること
        cases = (
            {"vault_dir": self.root / "nope"},
            {"diary_dir": "存在しない"},
            {"property_overrides": {"steps": "Steps"}},
            {"property_overrides": {"step_count": "Github"}},
            {"backup_dir": self.vault / "bk"},
            {"create_missing": "always"},
        )
        for kwargs in cases:
            with self.assertRaises(ObsidianConfigError, msg=repr(kwargs)):
                self.make_publisher(**kwargs)
        self.assertTrue(issubclass(ObsidianConfigError, ObsidianPublishError))

    def test_write_errors_are_not_config_errors(self):
        self.write_note("2030-01-02.md", "---\n日付: 2030-01-02\n閉じ線なし\n")
        with self.assertRaises(ObsidianPublishError) as ctx:
            self.make_publisher().publish_record(DiaryRecord(date=D, github="x"))
        self.assertNotIsInstance(ctx.exception, ObsidianConfigError)


# ---------------------------------------------------------------------------
# ノート探索
# ---------------------------------------------------------------------------

class TestFindNote(_VaultCase):
    def test_direct_note(self):
        path = self.write_note("2030-01-02.md", EXISTING_NOTE)
        self.assertEqual(self.make_publisher().find_note(D), path)

    def test_recursive_exact_match_in_subfolder(self):
        path = self.write_note("2030/2030-01-02.md", EXISTING_NOTE)
        self.write_note("2030/2030-01-02 週次振り返り 2030年1月1日〜1月2日.md", "週次")
        self.write_note("2030/2030-01-02 (2).md", "dup")
        self.write_note("2030/2030-01-02.md.bak", "bak")
        self.assertEqual(self.make_publisher().find_note(D), path)

    def test_weekly_note_alone_is_not_matched(self):
        self.write_note("2030-01-02 週次振り返り.md", "週次")
        self.assertIsNone(self.make_publisher().find_note(D))

    def test_hidden_dirs_excluded(self):
        self.write_note(".trash/2030-01-02.md", EXISTING_NOTE)
        self.write_note("sub/.hidden/2030-01-02.md", EXISTING_NOTE)
        self.assertIsNone(self.make_publisher().find_note(D))

    def test_direct_preferred_and_duplicates_warned(self):
        direct = self.write_note("2030-01-02.md", EXISTING_NOTE)
        self.write_note("a/2030-01-02.md", EXISTING_NOTE)
        with self.assertLogs("test-obsidian-publisher", level="WARNING") as logs:
            found = self.make_publisher().find_note(D)
        self.assertEqual(found, direct)
        self.assertTrue(any("複数" in line for line in logs.output))

    def test_shallowest_subfolder_wins(self):
        shallow = self.write_note("b/2030-01-02.md", EXISTING_NOTE)
        self.write_note("a/deeper/2030-01-02.md", EXISTING_NOTE)
        with self.assertLogs("test-obsidian-publisher", level="WARNING"):
            self.assertEqual(self.make_publisher().find_note(D), shallow)


# ---------------------------------------------------------------------------
# 既存ノートの更新
# ---------------------------------------------------------------------------

class TestPublishExisting(_VaultCase):
    def _record(self) -> DiaryRecord:
        return DiaryRecord(
            date=D,
            health=_health(step_count=5555, sleep_hours=7.0, body_mass_kg=60.5),
            weather=_weather(),
            github="[🔀 sample:PR #1: t](https://example.com/1)\n[📝 sample:変更行数:+1-0 (1 commits)](https://example.com/c)",
        )

    def test_update_changes_only_target_entries(self):
        path = self.write_note("2030-01-02.md", EXISTING_NOTE)
        os.chmod(path, 0o640)
        pub = self.make_publisher()
        outcome = pub.publish_record(self._record())

        self.assertEqual(outcome.status, "updated")
        self.assertEqual(outcome.path, path)
        expected, _ = upsert_frontmatter(EXISTING_NOTE, pub.build_updates(self._record()))
        actual = path.read_bytes().decode("utf-8")
        self.assertEqual(actual, expected)
        # 期待値を手で組んだものとも全体一致 (upsert 自身に依存しない確認)
        manual = EXISTING_NOTE
        manual = manual.replace("歩数 (歩):\n", "歩数 (歩): 5555\n", 1)
        manual = manual.replace("睡眠時間 (分):\n", "睡眠時間 (分): 420\n", 1)
        manual = manual.replace("体重 (kg):\n", "体重 (kg): 60.5\n", 1)
        manual = manual.replace(
            "気温:\n", '気温: "朝:平均1.0℃, 昼:平均2.0℃, 夜:平均3.0℃（最高:4.0℃, 最低:0.5℃）"\n', 1,
        )
        manual = manual.replace("天気:\n", '天気: "3時: 快晴☀️☀️, 6時: 曇☁️"\n', 1)
        manual = manual.replace(
            "Github:\n",
            'Github: "[🔀 sample:PR #1: t](https://example.com/1)\\n'
            '[📝 sample:変更行数:+1-0 (1 commits)](https://example.com/c)"\n'
            '湿度: "朝:平均50.0%, 昼:平均40.0%, 夜:平均60.0%（最高:70%, 最低:30%）"\n'
            '気圧: "1-6時:平均1010.0hPa, 7-12時:平均1011.0hPa"\n'
            '日照時間: "5.5時間"\n'
            '降水量: "朝:0.0mm, 昼:0.0mm, 夜:0.5mm"\n',
            1,
        )
        self.assertEqual(actual, manual)
        # 本文は byte 不変
        self.assertEqual(split_frontmatter(actual)[1], split_frontmatter(EXISTING_NOTE)[1])  # type: ignore[index]
        # permission 継承
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)
        # tmp が残っていない
        self.assertEqual(sorted(p.name for p in self.diary.iterdir()), ["2030-01-02.md"])

    def test_backup_contains_original_bytes(self):
        path = self.write_note("2030-01-02.md", EXISTING_NOTE)
        outcome = self.make_publisher().publish_record(self._record())
        self.assertIsNotNone(outcome.backup_path)
        backup = outcome.backup_path
        assert backup is not None
        self.assertEqual(backup.read_bytes(), EXISTING_NOTE.encode("utf-8"))
        self.assertEqual(backup.parent, self.backup_dir / "2030-01-05")
        self.assertEqual(backup.name, "2030-01-02.20300105T040506Z.md")
        self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode), 0o600)
        self.assertNotEqual(path.read_bytes(), backup.read_bytes())

    def test_second_run_is_unchanged_without_write_or_backup(self):
        path = self.write_note("2030-01-02.md", EXISTING_NOTE)
        pub = self.make_publisher()
        pub.publish_record(self._record())
        before = path.stat()
        backups_before = sorted(self.backup_dir.rglob("*.md"))
        outcome = self.make_publisher().publish_record(self._record())
        self.assertEqual(outcome.status, "unchanged")
        self.assertIsNone(outcome.backup_path)
        self.assertEqual(outcome.written_keys, ())
        after = path.stat()
        self.assertEqual((before.st_mtime_ns, before.st_ino), (after.st_mtime_ns, after.st_ino))
        self.assertEqual(sorted(self.backup_dir.rglob("*.md")), backups_before)

    def test_fill_only_empty_keeps_existing_values(self):
        note = EXISTING_NOTE.replace("歩数 (歩):\n", "歩数 (歩): 123\n").replace(
            "天気:\n", '天気: "手入力の天気"\n',
        )
        path = self.write_note("2030-01-02.md", note)
        outcome = self.make_publisher(fill_only_empty=True).publish_record(self._record())
        text = path.read_text(encoding="utf-8")
        self.assertIn("歩数 (歩): 123\n", text)
        self.assertIn('天気: "手入力の天気"\n', text)
        self.assertIn("睡眠時間 (分): 420\n", text)
        actions = {c.key: c.action for c in outcome.changes}
        self.assertEqual(actions["歩数 (歩)"], "kept")
        self.assertEqual(actions["天気"], "kept")
        self.assertEqual(actions["睡眠時間 (分)"], "update")
        kept = [c for c in outcome.changes if c.key == "歩数 (歩)"][0]
        self.assertEqual((kept.old, kept.new), ("123", "5555"))

    def test_dry_run_writes_nothing(self):
        self.write_note("2030-01-02.md", EXISTING_NOTE)
        before = self.tree()
        with _InfoLogsEnabled(), self.assertLogs("test-obsidian-publisher", level="INFO") as logs:
            outcome = self.make_publisher(dry_run=True).publish_record(self._record())
        self.assertEqual(outcome.status, "would_update")
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.backup_dir.exists())
        self.assertTrue(any("[dry-run]" in line and "歩数 (歩)" in line for line in logs.output))
        self.assertIn("歩数 (歩)", outcome.written_keys)

    def test_unclosed_frontmatter_is_not_touched(self):
        broken = "---\n日付: 2030-01-02\n歩数 (歩):\n本文が続くが閉じ線が無い\n"
        path = self.write_note("2030-01-02.md", broken)
        with self.assertRaises(ObsidianPublishError):
            self.make_publisher().publish_record(self._record())
        self.assertEqual(path.read_text(encoding="utf-8"), broken)
        self.assertFalse(self.backup_dir.exists())

    def test_non_utf8_note_rejected(self):
        path = self.diary / "2030-01-02.md"
        path.write_bytes(b"---\n\xff\xfe\n---\n")
        with self.assertRaises(ObsidianPublishError):
            self.make_publisher().publish_record(self._record())
        self.assertEqual(path.read_bytes(), b"---\n\xff\xfe\n---\n")

    def test_symlink_note_rejected(self):
        real = self.root / "real.md"
        real.write_text(EXISTING_NOTE, encoding="utf-8")
        (self.diary / "2030-01-02.md").symlink_to(real)
        with self.assertRaises(ObsidianPublishError):
            self.make_publisher().publish_record(self._record())
        self.assertEqual(real.read_text(encoding="utf-8"), EXISTING_NOTE)

    def test_crlf_and_bom_note(self):
        text = "\ufeff---\r\n日付: 2030-01-02\r\n歩数 (歩):\r\n---\r\n本文\r\n"
        path = self.write_note("2030-01-02.md", text)
        self.make_publisher().publish_record(DiaryRecord(date=D, health=_health(step_count=7)))
        self.assertEqual(
            path.read_bytes(),
            "\ufeff---\r\n日付: 2030-01-02\r\n歩数 (歩): 7\r\n---\r\n本文\r\n".encode("utf-8"),
        )

    def test_note_in_subfolder_updated_in_place(self):
        path = self.write_note("2030/2030-01-02.md", EXISTING_NOTE)
        outcome = self.make_publisher().publish_record(DiaryRecord(date=D, github="該当なし"))
        self.assertEqual(outcome.path, path)
        self.assertIn('Github: "該当なし"\n', path.read_text(encoding="utf-8"))
        self.assertFalse((self.diary / "2030-01-02.md").exists())

    def test_publish_protocol_writes_health_only(self):
        path = self.write_note("2030-01-02.md", EXISTING_NOTE)
        self.make_publisher().publish(_health(step_count=55))
        text = path.read_text(encoding="utf-8")
        self.assertIn("歩数 (歩): 55\n", text)
        self.assertIn("Github:\n", text)

    def test_target_date_mismatch_rejected(self):
        self.write_note("2030-01-02.md", EXISTING_NOTE)
        pub = self.make_publisher(target_date=D + timedelta(days=1))
        with self.assertRaises(ObsidianPublishError):
            pub.publish_record(DiaryRecord(date=D, github="x"))

    def test_backup_failure_aborts_write(self):
        path = self.write_note("2030-01-02.md", EXISTING_NOTE)
        blocker = self.root / "blocker"
        blocker.write_text("not a dir", encoding="utf-8")
        pub = self.make_publisher(backup_dir=blocker)
        with self.assertRaises(ObsidianPublishError):
            pub.publish_record(self._record())
        self.assertEqual(path.read_text(encoding="utf-8"), EXISTING_NOTE)

    def test_no_backup_dir_means_no_backup(self):
        self.write_note("2030-01-02.md", EXISTING_NOTE)
        outcome = self.make_publisher(backup_dir=None).publish_record(self._record())
        self.assertEqual(outcome.status, "updated")
        self.assertIsNone(outcome.backup_path)
        self.assertFalse(self.backup_dir.exists())

    @unittest.skipUnless(hasattr(os.stat_result, "st_birthtime"), "birthtime 非対応 OS")
    def test_birthtime_preserved_best_effort(self):
        path = self.write_note("2030-01-02.md", EXISTING_NOTE)
        old = time.time() - 86400 * 40
        os.utime(path, (old, old))  # APFS: mtime を過去にすると birthtime も下がる
        original_birth = os.stat(path).st_birthtime
        if original_birth > old + 1:
            self.skipTest("この FS は birthtime の引き下げに対応していない")
        self.make_publisher().publish_record(self._record())
        self.assertAlmostEqual(os.stat(path).st_birthtime, original_birth, delta=1.0)
        self.assertGreater(os.stat(path).st_mtime, old + 86400)


class _ConcurrentEditPublisher(ObsidianPublisher):
    """再確認 (2 回目以降の _snapshot) の直前にファイルを書き換える fake.

    ``edits`` に入っている回数だけ、Obsidian の並行保存を模して本文に行を追記する.
    """

    def __init__(self, *args: object, edits: int, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.remaining_edits = edits
        self.snapshot_calls = 0

    def _snapshot(self, path: Path):  # type: ignore[override]
        self.snapshot_calls += 1
        # 偶数回目 = 置換直前の再確認
        if self.snapshot_calls % 2 == 0 and self.remaining_edits > 0:
            self.remaining_edits -= 1
            with open(path, "ab") as fh:
                fh.write(f"並行編集 {self.snapshot_calls}\n".encode("utf-8"))
        return super()._snapshot(path)


class TestConcurrentModification(_VaultCase):
    def _make(self, edits: int) -> _ConcurrentEditPublisher:
        return _ConcurrentEditPublisher(
            vault_dir=self.vault, logger=_logger(), backup_dir=self.backup_dir,
            clock=lambda: FIXED_NOW, sleep=lambda _s: None, edits=edits,
        )

    def test_conflict_detected_then_retry_merges_on_latest_content(self):
        path = self.write_note("2030-01-02.md", EXISTING_NOTE)
        pub = self._make(edits=1)
        with self.assertLogs("test-obsidian-publisher", level="WARNING") as logs:
            outcome = pub.publish_record(DiaryRecord(date=D, health=_health(step_count=5555)))
        self.assertEqual(outcome.status, "updated")
        text = path.read_text(encoding="utf-8")
        # 並行編集 (本文末尾の追記) も、こちらの値も両方残る
        self.assertIn("並行編集 2\n", text)
        self.assertIn("歩数 (歩): 5555\n", text)
        expected, _ = upsert_frontmatter(EXISTING_NOTE + "並行編集 2\n", {"歩数 (歩)": 5555})
        self.assertEqual(text, expected)
        self.assertTrue(any("並行変更" in line for line in logs.output))
        self.assertEqual([p.name for p in self.diary.iterdir()], ["2030-01-02.md"])

    def test_persistent_conflict_gives_up_without_writing(self):
        path = self.write_note("2030-01-02.md", EXISTING_NOTE)
        pub = self._make(edits=99)
        with self.assertLogs("test-obsidian-publisher", level="WARNING"):
            with self.assertRaises(ObsidianPublishError) as ctx:
                pub.publish_record(DiaryRecord(date=D, health=_health(step_count=5555)))
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("5555", text)  # こちらの値は一度も書かれていない
        # 試行は合計 3 回 (やり直しは 2 回): 3 回とも競合して諦める
        self.assertEqual(text.count("並行編集"), 3)
        self.assertIn("3 回試行", str(ctx.exception))
        self.assertEqual([p.name for p in self.diary.iterdir()], ["2030-01-02.md"])


class TestNoteMovedOrChangingDuringRead(_VaultCase):
    def test_note_deleted_before_read_is_rediscovered(self):
        # 読込直前にノートが消えた (サブフォルダへ移動された等) → 探索し直して移動先を更新する
        original = self.write_note("2030-01-02.md", EXISTING_NOTE)
        moved_to = self.diary / "2030" / "2030-01-02.md"
        test = self

        class _MovingPublisher(ObsidianPublisher):
            moved = False

            def _snapshot(self, path: Path):  # type: ignore[override]
                if not _MovingPublisher.moved:
                    _MovingPublisher.moved = True
                    moved_to.parent.mkdir()
                    os.replace(original, moved_to)
                return super()._snapshot(path)

        pub = _MovingPublisher(
            vault_dir=self.vault, logger=_logger(), backup_dir=self.backup_dir,
            clock=lambda: FIXED_NOW, sleep=lambda _s: None,
        )
        with self.assertLogs("test-obsidian-publisher", level="WARNING"):
            outcome = pub.publish_record(DiaryRecord(date=D, github="該当なし"))
        test.assertEqual(outcome.status, "updated")
        test.assertEqual(outcome.path, moved_to)
        test.assertFalse(original.exists())
        test.assertIn('Github: "該当なし"\n', moved_to.read_text(encoding="utf-8"))

    def test_change_while_reading_triggers_retry(self):
        from ihealth.publishers.obsidian import _ChangedWhileReading

        path = self.write_note("2030-01-02.md", EXISTING_NOTE)

        class _FlakyReadPublisher(ObsidianPublisher):
            calls = 0

            def _snapshot(self, p: Path):  # type: ignore[override]
                _FlakyReadPublisher.calls += 1
                if _FlakyReadPublisher.calls == 1:
                    raise _ChangedWhileReading(str(p))
                return super()._snapshot(p)

        pub = _FlakyReadPublisher(
            vault_dir=self.vault, logger=_logger(), backup_dir=self.backup_dir,
            clock=lambda: FIXED_NOW, sleep=lambda _s: None,
        )
        with self.assertLogs("test-obsidian-publisher", level="WARNING"):
            outcome = pub.publish_record(DiaryRecord(date=D, github="x"))
        self.assertEqual(outcome.status, "updated")
        self.assertIn('Github: "x"\n', path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# ノート新規作成 (テンプレート)
# ---------------------------------------------------------------------------

class TestExpandTemplate(unittest.TestCase):
    def test_head_block_removed_and_vars_expanded(self):
        out = expand_template(TEMPLATE, D)
        self.assertTrue(out.startswith("---\n名前: 毎日の習慣 2030-01-02\n日付: 2030-01-02\n"))
        self.assertIn("# 毎日の習慣 2030-01-02（水）\n", out)
        self.assertNotIn("<%", out)
        self.assertNotIn("tp.date", out)

    def test_weekday_mapping(self):
        mapping = {date(2030, 1, d): w for d, w in zip(range(7, 14), "月火水木金土日")}
        for d, w in mapping.items():
            self.assertEqual(expand_template("<% weekday %>", d), w)

    def test_trim_variants(self):
        self.assertEqual(expand_template("<%* x %>\n---\n", D), "---\n")
        self.assertEqual(expand_template("<%* x -%>\r\n---\n", D), "---\n")
        self.assertEqual(expand_template("<%* x _%>\n\n  ---\n", D), "---\n")
        self.assertEqual(expand_template("\ufeff<%* x -%>\n---\n", D), "\ufeff---\n")

    def test_unsupported_syntax_rejected(self):
        for text in (
            "---\n日付: <% tp.file.title %>\n---\n",
            "---\n<%* tR += 'x' %>\n---\n",     # 先頭以外の JS ブロック
            "<% dateStr -%>",
        ):
            with self.assertRaises(TemplateExpansionError, msg=text):
                expand_template(text, D)


class TestCreateMissingNote(_VaultCase):
    def test_created_from_template(self):
        pub = self.make_publisher(template_path=Path("テンプレート/毎日の習慣.md"))
        record = DiaryRecord(date=D, health=_health(step_count=5555), github="該当なし")
        outcome = pub.publish_record(record)
        path = self.diary / "2030-01-02.md"
        self.assertEqual(outcome.status, "created")
        self.assertEqual(outcome.path, path)
        expected, _ = upsert_frontmatter(
            expand_template(TEMPLATE, D), {"歩数 (歩)": 5555, "Github": "該当なし"},
        )
        self.assertEqual(path.read_text(encoding="utf-8"), expected)
        self.assertIn('"歩数 (歩)": 5555\n', expected)
        self.assertIn("# 毎日の習慣 2030-01-02（水）\n", expected)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        umask = os.umask(0o022)
        os.umask(umask)
        self.assertEqual(mode, 0o666 & ~umask)
        self.assertEqual(sorted(p.name for p in self.diary.iterdir()), ["2030-01-02.md"])
        self.assertIsNone(outcome.backup_path)

    def test_absolute_template_path(self):
        pub = self.make_publisher(template_path=self.template)
        outcome = pub.publish_record(DiaryRecord(date=D, github="x"))
        self.assertEqual(outcome.status, "created")

    def test_template_with_unsupported_syntax_is_skipped(self):
        self.template.write_text(TEMPLATE + "<% tp.file.cursor() %>\n", encoding="utf-8")
        pub = self.make_publisher(template_path=self.template)
        with self.assertLogs("test-obsidian-publisher", level="WARNING"):
            outcome = pub.publish_record(DiaryRecord(date=D, github="x"))
        self.assertEqual(outcome.status, "missing")
        self.assertIsNone(outcome.path)
        self.assertEqual(list(self.diary.iterdir()), [])

    def test_unreadable_template_is_skipped(self):
        pub = self.make_publisher(template_path=self.vault / "無いテンプレート.md")
        with self.assertLogs("test-obsidian-publisher", level="WARNING"):
            outcome = pub.publish_record(DiaryRecord(date=D, github="x"))
        self.assertEqual(outcome.status, "missing")
        self.assertEqual(list(self.diary.iterdir()), [])

    def test_template_with_broken_frontmatter_is_skipped(self):
        self.template.write_text("---\n日付: <% dateStr %>\n", encoding="utf-8")
        pub = self.make_publisher(template_path=self.template)
        with self.assertLogs("test-obsidian-publisher", level="WARNING"):
            outcome = pub.publish_record(DiaryRecord(date=D, github="x"))
        self.assertEqual(outcome.status, "missing")
        self.assertEqual(list(self.diary.iterdir()), [])

    def test_template_without_frontmatter_is_skipped(self):
        # JS ブロック内の '%>' で展開が途中で切れた等、先頭に frontmatter が無い展開結果は使わない
        self.template.write_text(
            "<%* const s = \"%>\"; -%>\n\"; const dateStr = 1;\n---\n日付: 1\n---\n",
            encoding="utf-8",
        )
        pub = self.make_publisher(template_path=self.template)
        with self.assertLogs("test-obsidian-publisher", level="WARNING"):
            outcome = pub.publish_record(DiaryRecord(date=D, github="x"))
        self.assertEqual(outcome.status, "missing")
        self.assertEqual(list(self.diary.iterdir()), [])

    def test_minimal_mode_falls_back_to_minimal_note(self):
        self.template.write_text("<% tp.unknown %>", encoding="utf-8")
        pub = self.make_publisher(template_path=self.template, create_missing="minimal")
        with self.assertLogs("test-obsidian-publisher", level="WARNING"):
            outcome = pub.publish_record(DiaryRecord(date=D, health=_health(step_count=3)))
        self.assertEqual(outcome.status, "created")
        self.assertEqual(
            (self.diary / "2030-01-02.md").read_text(encoding="utf-8"),
            "---\n日付: 2030-01-02\n歩数 (歩): 3\n---\n",
        )

    def test_minimal_mode_without_template(self):
        pub = self.make_publisher(create_missing="minimal")
        outcome = pub.publish_record(DiaryRecord(date=D, github="x"))
        self.assertEqual(outcome.status, "created")
        self.assertEqual(
            (self.diary / "2030-01-02.md").read_text(encoding="utf-8"),
            '---\n日付: 2030-01-02\nGithub: "x"\n---\n',
        )

    def test_no_template_means_missing(self):
        with self.assertLogs("test-obsidian-publisher", level="WARNING"):
            outcome = self.make_publisher().publish_record(DiaryRecord(date=D, github="x"))
        self.assertEqual(
            outcome, NotePublishOutcome(D, None, "missing", (), reason="template_unavailable"),
        )
        self.assertEqual(list(self.diary.iterdir()), [])

    def test_never_mode_ignores_template(self):
        pub = self.make_publisher(template_path=self.template, create_missing="never")
        with self.assertLogs("test-obsidian-publisher", level="WARNING"):
            outcome = pub.publish_record(DiaryRecord(date=D, github="x"))
        self.assertEqual(outcome.status, "missing")
        self.assertEqual(list(self.diary.iterdir()), [])

    def test_nothing_to_write_does_not_create(self):
        pub = self.make_publisher(template_path=self.template)
        outcome = pub.publish_record(DiaryRecord(date=D, health=_health(), github=""))
        self.assertEqual(outcome.status, "missing")
        self.assertEqual(list(self.diary.iterdir()), [])

    def test_dry_run_would_create_without_files(self):
        before = self.tree()
        pub = self.make_publisher(template_path=self.template, dry_run=True)
        with _InfoLogsEnabled(), self.assertLogs("test-obsidian-publisher", level="INFO") as logs:
            outcome = pub.publish_record(DiaryRecord(date=D, github="x"))
        self.assertEqual(outcome.status, "would_create")
        self.assertEqual(outcome.path, self.diary / "2030-01-02.md")
        self.assertEqual(self.tree(), before)
        self.assertTrue(any("[dry-run]" in line and "Github" in line for line in logs.output))

    def test_concurrently_created_note_is_updated_not_overwritten(self):
        test = self

        class _RacePublisher(ObsidianPublisher):
            def _initial_note_text(self, d: date):  # type: ignore[override]
                # テンプレート展開中に他のプロセス (Obsidian Sync 等) が同名ノートを作った
                test.write_note("2030-01-02.md", EXISTING_NOTE)
                return super()._initial_note_text(d)

        pub = _RacePublisher(
            vault_dir=self.vault, logger=_logger(), template_path=self.template,
            backup_dir=self.backup_dir, clock=lambda: FIXED_NOW, sleep=lambda _s: None,
        )
        with self.assertLogs("test-obsidian-publisher", level="WARNING"):
            outcome = pub.publish_record(DiaryRecord(date=D, health=_health(step_count=9)))
        self.assertEqual(outcome.status, "updated")
        expected, _ = upsert_frontmatter(EXISTING_NOTE, {"歩数 (歩)": 9})
        self.assertEqual((self.diary / "2030-01-02.md").read_text(encoding="utf-8"), expected)
        self.assertEqual([p.name for p in self.diary.iterdir()], ["2030-01-02.md"])


# ---------------------------------------------------------------------------
# バックアップの保持期間
# ---------------------------------------------------------------------------

class TestBackupRetention(_VaultCase):
    def test_old_backup_dirs_pruned_only_for_own_files(self):
        old_only_ours = self.backup_dir / "2029-11-01"
        old_mixed = self.backup_dir / "2029-11-02"
        recent = self.backup_dir / "2029-12-20"
        other = self.backup_dir / "notes"
        for directory in (old_only_ours, old_mixed, recent, other):
            directory.mkdir(parents=True)
        (old_only_ours / "2029-10-01.20291101T000000Z.md").write_text("a", encoding="utf-8")
        (old_mixed / "2029-10-02.20291102T000000Z-1.md").write_text("b", encoding="utf-8")
        (old_mixed / "user-file.txt").write_text("keep", encoding="utf-8")
        (recent / "2029-12-19.20291220T000000Z.md").write_text("c", encoding="utf-8")
        (other / "2029-01-01.20290101T000000Z.md").write_text("d", encoding="utf-8")

        self.write_note("2030-01-02.md", EXISTING_NOTE)
        self.make_publisher().publish_record(DiaryRecord(date=D, github="x"))

        self.assertFalse(old_only_ours.exists())
        self.assertTrue((old_mixed / "user-file.txt").exists())
        self.assertFalse((old_mixed / "2029-10-02.20291102T000000Z-1.md").exists())
        self.assertTrue((recent / "2029-12-19.20291220T000000Z.md").exists())
        self.assertTrue((other / "2029-01-01.20290101T000000Z.md").exists())
        self.assertTrue((self.backup_dir / "2030-01-05" / "2030-01-02.20300105T040506Z.md").exists())

    def test_retention_boundary_is_30_days(self):
        # 保持は 30 日: 取得日が「今日 (UTC) − 30 日」ちょうどのディレクトリは残し、31 日前は消す
        # (FIXED_NOW = 2030-01-05 UTC → 2029-12-06 は残る / 2029-12-05 は消える)
        kept = self.backup_dir / "2029-12-06"
        pruned = self.backup_dir / "2029-12-05"
        for directory, name in (
            (kept, "2029-12-01.20291206T000000Z.md"),
            (pruned, "2029-12-01.20291205T000000Z.md"),
        ):
            directory.mkdir(parents=True)
            (directory / name).write_text("x", encoding="utf-8")
        self.write_note("2030-01-02.md", EXISTING_NOTE)
        self.make_publisher().publish_record(DiaryRecord(date=D, github="x"))
        self.assertTrue((kept / "2029-12-01.20291206T000000Z.md").exists())
        self.assertFalse(pruned.exists())

    def test_backup_name_collision_gets_suffix(self):
        self.write_note("2030-01-02.md", EXISTING_NOTE)
        pub = self.make_publisher()
        first = pub.publish_record(DiaryRecord(date=D, github="x"))
        second = pub.publish_record(DiaryRecord(date=D, github="y"))
        assert first.backup_path is not None and second.backup_path is not None
        self.assertEqual(first.backup_path.name, "2030-01-02.20300105T040506Z.md")
        self.assertEqual(second.backup_path.name, "2030-01-02.20300105T040506Z-1.md")
        self.assertIn('Github: "x"', second.backup_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 新規作成の制限: 今日・昨日のノートは作らない (別端末での同日ノート作成と Sync 衝突させない)
# ---------------------------------------------------------------------------

def _at(text: str) -> "datetime":
    """ISO 8601 (offset 付き) → aware datetime."""
    return datetime.fromisoformat(text)


class TestCreationCutoff(_VaultCase):
    def _publish_at(self, now: datetime, d: date, **kwargs: object) -> NotePublishOutcome:
        pub = self.make_publisher(template_path=self.template, clock=lambda: now, **kwargs)
        return pub.publish_record(DiaryRecord(date=d, github="x"))

    def test_today_and_yesterday_are_not_created(self):
        now = _at("2030-01-02T22:00:00+00:00")  # JST 2030-01-03 07:00 (launchd の朝の実行)
        for d in (date(2030, 1, 3), date(2030, 1, 2), date(2030, 1, 4)):  # 今日 / 昨日 / 未来
            outcome = self._publish_at(now, d)
            self.assertEqual(
                (outcome.status, outcome.reason, outcome.path), ("missing", "too_recent", None), d,
            )
        self.assertEqual(list(self.diary.iterdir()), [])

    def test_two_days_ago_is_created(self):
        now = _at("2030-01-02T22:00:00+00:00")  # JST 2030-01-03
        outcome = self._publish_at(now, date(2030, 1, 1))
        self.assertEqual(outcome.status, "created")
        self.assertTrue((self.diary / "2030-01-01.md").is_file())

    def test_today_is_judged_in_the_given_timezone(self):
        now = _at("2030-01-02T15:30:00+00:00")  # JST では 2030-01-03 00:30, UTC では 01-02
        self.assertEqual(self._publish_at(now, date(2030, 1, 1), tz=JST).status, "created")
        (self.diary / "2030-01-01.md").unlink()
        outcome = self._publish_at(now, date(2030, 1, 1), tz=timezone.utc)
        self.assertEqual((outcome.status, outcome.reason), ("missing", "too_recent"))

    def test_min_age_zero_allows_today(self):
        now = _at("2030-01-02T22:00:00+00:00")
        outcome = self._publish_at(now, date(2030, 1, 3), create_min_age_days=0)
        self.assertEqual(outcome.status, "created")

    def test_dry_run_honours_the_cutoff(self):
        now = _at("2030-01-02T22:00:00+00:00")
        outcome = self._publish_at(now, date(2030, 1, 3), dry_run=True)
        self.assertEqual((outcome.status, outcome.reason), ("missing", "too_recent"))

    def test_existing_today_note_is_still_updated(self):
        path = self.write_note("2030-01-03.md", EXISTING_NOTE.replace("2030-01-02", "2030-01-03"))
        now = _at("2030-01-02T22:00:00+00:00")
        outcome = self._publish_at(now, date(2030, 1, 3))
        self.assertEqual(outcome.status, "updated")
        self.assertIn('Github: "x"\n', path.read_text(encoding="utf-8"))

    def test_invalid_min_age_rejected(self):
        for bad in (-1, True, 1.5):
            with self.assertRaises(ObsidianConfigError, msg=repr(bad)):
                self.make_publisher(create_min_age_days=bad)


# ---------------------------------------------------------------------------
# 新規作成の場所: 前後 7 日で最も近い既存ノートと同じフォルダ (同距離なら過去側)
# ---------------------------------------------------------------------------

class TestCreationFolder(_VaultCase):
    def _create(self, d: date = D, **kwargs: object) -> NotePublishOutcome:
        pub = self.make_publisher(template_path=self.template, **kwargs)
        return pub.publish_record(DiaryRecord(date=d, github="x"))

    def _neighbor(self, relative_dir: str, d: date) -> Path:
        name = f"{d.isoformat()}.md"
        return self.write_note(f"{relative_dir}/{name}" if relative_dir else name, "---\n---\n")

    def test_same_folder_as_previous_day(self):
        self._neighbor("2029-Q4", D - timedelta(days=1))
        outcome = self._create()
        self.assertEqual(outcome.path, self.diary / "2029-Q4" / "2030-01-02.md")
        self.assertTrue(outcome.path.is_file())  # type: ignore[union-attr]
        self.assertFalse((self.diary / "2030-01-02.md").exists())

    def test_nearest_wins_over_earlier(self):
        self._neighbor("A", D - timedelta(days=3))
        self._neighbor("B", D + timedelta(days=1))
        self.assertEqual(self._create().path, self.diary / "B" / "2030-01-02.md")

    def test_tie_prefers_earlier_date(self):
        self._neighbor("A", D - timedelta(days=2))
        self._neighbor("B", D + timedelta(days=2))
        self.assertEqual(self._create().path, self.diary / "A" / "2030-01-02.md")

    def test_tie_with_root_neighbor_prefers_earlier(self):
        self._neighbor("A", D - timedelta(days=1))
        self._neighbor("", D + timedelta(days=1))
        self.assertEqual(self._create().path, self.diary / "A" / "2030-01-02.md")

    def test_window_is_seven_days(self):
        self._neighbor("A", D - timedelta(days=7))
        self.assertEqual(self._create().path, self.diary / "A" / "2030-01-02.md")

    def test_window_seven_days_after(self):
        self._neighbor("B", D + timedelta(days=7))
        self.assertEqual(self._create().path, self.diary / "B" / "2030-01-02.md")

    def test_beyond_window_falls_back_to_root(self):
        self._neighbor("A", D - timedelta(days=8))
        self._neighbor("B", D + timedelta(days=8))
        self.assertEqual(self._create().path, self.diary / "2030-01-02.md")

    def test_hidden_folders_and_non_exact_names_are_ignored(self):
        self._neighbor(".trash", D - timedelta(days=1))
        self.write_note("W/2030-01-01 週次振り返り.md", "週次")
        self._neighbor("A", D - timedelta(days=5))
        self.assertEqual(self._create().path, self.diary / "A" / "2030-01-02.md")

    def test_only_hidden_or_non_exact_neighbors_means_root(self):
        self._neighbor(".trash", D - timedelta(days=1))
        self.write_note("W/2030-01-01 週次振り返り.md", "週次")
        self.assertEqual(self._create().path, self.diary / "2030-01-02.md")

    def test_dry_run_reports_the_chosen_folder(self):
        self._neighbor("A", D - timedelta(days=1))
        before = self.tree()
        outcome = self._create(dry_run=True)
        self.assertEqual(outcome.status, "would_create")
        self.assertEqual(outcome.path, self.diary / "A" / "2030-01-02.md")
        self.assertEqual(self.tree(), before)

    def test_backfill_of_consecutive_missing_days_stays_together(self):
        # 抜けた 2 日 (D, D+1) を順に作る: D+1 は「さっき作った D」(過去側, 距離 1) の隣になる
        self._neighbor("Q", D - timedelta(days=1))
        self._neighbor("", D + timedelta(days=2))
        pub = self.make_publisher(template_path=self.template)
        first = pub.publish_record(DiaryRecord(date=D, github="x"))
        second = pub.publish_record(DiaryRecord(date=D + timedelta(days=1), github="y"))
        self.assertEqual(first.path, self.diary / "Q" / "2030-01-02.md")
        self.assertEqual(second.path, self.diary / "Q" / "2030-01-03.md")


# ---------------------------------------------------------------------------
# 空ノート (0 byte / 空白のみ): 書込途中・テンプレート適用待ちの可能性
# ---------------------------------------------------------------------------

class TestEmptyNote(_VaultCase):
    def _record(self) -> DiaryRecord:
        return DiaryRecord(date=D, health=_health(step_count=10), github="該当なし")

    def _backups(self) -> "list[Path]":
        return sorted(self.backup_dir.rglob("*.md")) if self.backup_dir.exists() else []

    def test_stable_empty_old_note_is_initialized_from_template(self):
        path = self.write_note("2029-Q4/2030-01-02.md", "")
        sleeps: "list[float]" = []
        pub = self.make_publisher(template_path=self.template, sleep=sleeps.append)
        outcome = pub.publish_record(self._record())
        self.assertEqual(outcome.status, "created")
        self.assertEqual(outcome.path, path)
        expected, _ = upsert_frontmatter(
            expand_template(TEMPLATE, D), {"歩数 (歩)": 10, "Github": "該当なし"},
        )
        self.assertEqual(path.read_text(encoding="utf-8"), expected)  # 二重 frontmatter にならない
        self.assertEqual(len(sleeps), 2)  # 最後の試行まで待って読み直した
        self.assertEqual([p.read_bytes() for p in self._backups()], [b""])

    def test_whitespace_only_note_is_treated_as_empty(self):
        path = self.write_note("2030-01-02.md", "\n  \n")
        outcome = self.make_publisher(template_path=self.template).publish_record(self._record())
        self.assertEqual(outcome.status, "created")
        self.assertTrue(path.read_text(encoding="utf-8").startswith("---\n名前: 毎日の習慣 2030-01-02\n"))

    def test_empty_note_filled_while_waiting_is_updated_normally(self):
        # 他のプログラムの truncate → write の間に読んだ (0 byte) ケース: 読み直すと本文がある
        path = self.write_note("2030-01-02.md", "")

        def writer_finishes(_sec: float) -> None:
            if path.stat().st_size == 0:
                path.write_bytes(EXISTING_NOTE.encode("utf-8"))

        pub = self.make_publisher(template_path=self.template, sleep=writer_finishes)
        outcome = pub.publish_record(self._record())
        self.assertEqual(outcome.status, "updated")
        expected, _ = upsert_frontmatter(EXISTING_NOTE, {"歩数 (歩)": 10, "Github": "該当なし"})
        self.assertEqual(path.read_text(encoding="utf-8"), expected)
        # バックアップは「空の読込」ではなく本文入りの内容
        self.assertEqual([p.read_bytes() for p in self._backups()], [EXISTING_NOTE.encode("utf-8")])

    def test_empty_note_of_today_is_skipped(self):
        path = self.write_note("2030-01-02.md", "")
        pub = self.make_publisher(
            template_path=self.template, clock=lambda: _at("2030-01-02T03:00:00+00:00"),
        )
        outcome = pub.publish_record(self._record())
        self.assertEqual((outcome.status, outcome.reason, outcome.path), ("skipped", "too_recent", path))
        self.assertEqual(path.read_bytes(), b"")
        self.assertEqual(self._backups(), [])

    def test_recently_modified_empty_note_is_skipped(self):
        # 対象日は十分古い (3 日前) が、空ノート自体が 1 秒前に変更された = 書込途中の可能性
        path = self.write_note("2030-01-02.md", "")
        one_second_ago = (int(FIXED_NOW.timestamp()) - 1) * 1_000_000_000
        os.utime(path, ns=(one_second_ago, one_second_ago))
        pub = self.make_publisher(template_path=self.template)
        with self.assertLogs("test-obsidian-publisher", level="WARNING"):
            outcome = pub.publish_record(self._record())
        self.assertEqual((outcome.status, outcome.reason), ("skipped", "recently_modified"))
        self.assertEqual(path.read_bytes(), b"")
        self.assertEqual(self._backups(), [])

    def test_empty_note_not_initialized_without_usable_template(self):
        path = self.write_note("2030-01-02.md", "")
        for kwargs, reason in (
            ({}, "template_unavailable"),                                         # テンプレート未設定
            ({"template_path": self.vault / "無い.md"}, "template_unavailable"),  # 読めない
            ({"template_path": self.template, "create_missing": "never"}, "create_disabled"),
        ):
            with self.assertLogs("test-obsidian-publisher", level="WARNING"):
                outcome = self.make_publisher(**kwargs).publish_record(self._record())
            self.assertEqual((outcome.status, outcome.reason), ("skipped", reason), kwargs)
            self.assertEqual(path.read_bytes(), b"")
        self.assertEqual(self._backups(), [])

    def test_empty_note_minimal_mode(self):
        path = self.write_note("2030-01-02.md", "")
        outcome = self.make_publisher(create_missing="minimal").publish_record(self._record())
        self.assertEqual(outcome.status, "created")
        self.assertEqual(
            path.read_text(encoding="utf-8"),
            '---\n日付: 2030-01-02\n歩数 (歩): 10\nGithub: "該当なし"\n---\n',
        )

    def test_empty_note_dry_run_writes_nothing(self):
        self.write_note("2030-01-02.md", "")
        before = self.tree()
        pub = self.make_publisher(template_path=self.template, dry_run=True)
        outcome = pub.publish_record(self._record())
        self.assertEqual(outcome.status, "would_create")
        self.assertEqual(self.tree(), before)

    def test_single_attempt_decides_immediately(self):
        self.write_note("2030-01-02.md", "")
        sleeps: "list[float]" = []
        pub = self.make_publisher(template_path=self.template, max_attempts=1, sleep=sleeps.append)
        self.assertEqual(pub.publish_record(self._record()).status, "created")
        self.assertEqual(sleeps, [])

    def test_nothing_to_write_leaves_empty_note_alone(self):
        path = self.write_note("2030-01-02.md", "")
        outcome = self.make_publisher(template_path=self.template).publish_record(
            DiaryRecord(date=D),
        )
        self.assertEqual(outcome.status, "unchanged")
        self.assertEqual(path.read_bytes(), b"")


# ---------------------------------------------------------------------------
# Templater 未処理のノート (frontmatter 無し + '<%')
# ---------------------------------------------------------------------------

class TestUnprocessedTemplaterNote(_VaultCase):
    def test_raw_template_note_is_left_untouched(self):
        path = self.write_note("2030-01-02.md", TEMPLATE)
        sleeps: "list[float]" = []
        pub = self.make_publisher(template_path=self.template, sleep=sleeps.append)
        with self.assertLogs("test-obsidian-publisher", level="WARNING"):
            outcome = pub.publish_record(DiaryRecord(date=D, health=_health(step_count=10)))
        self.assertEqual(
            (outcome.status, outcome.reason, outcome.path), ("skipped", "unprocessed_template", path),
        )
        self.assertEqual(path.read_text(encoding="utf-8"), TEMPLATE)
        self.assertFalse(self.backup_dir.exists())
        self.assertEqual(len(sleeps), 2)  # Templater の処理を待って読み直した

    def test_note_processed_while_waiting_is_updated(self):
        path = self.write_note("2030-01-02.md", TEMPLATE)
        processed = expand_template(TEMPLATE, D)

        def templater_runs(_sec: float) -> None:
            path.write_text(processed, encoding="utf-8")

        pub = self.make_publisher(sleep=templater_runs)
        outcome = pub.publish_record(DiaryRecord(date=D, health=_health(step_count=10)))
        self.assertEqual(outcome.status, "updated")
        expected, _ = upsert_frontmatter(processed, {"歩数 (歩)": 10})
        self.assertEqual(path.read_text(encoding="utf-8"), expected)

    def test_templater_syntax_in_body_of_note_with_frontmatter_is_fine(self):
        note = EXISTING_NOTE + "メモ: `<% tp.date.now() %>` の書き方\n"
        path = self.write_note("2030-01-02.md", note)
        outcome = self.make_publisher().publish_record(DiaryRecord(date=D, github="x"))
        self.assertEqual(outcome.status, "updated")
        self.assertEqual(path.read_text(encoding="utf-8"), upsert_frontmatter(note, {"Github": "x"})[0])

    def test_note_without_frontmatter_gets_one_prepended(self):
        path = self.write_note("2030-01-02.md", "# 手書きのノート\n本文\n")
        outcome = self.make_publisher().publish_record(DiaryRecord(date=D, github="x"))
        self.assertEqual(outcome.status, "updated")
        self.assertEqual(path.read_text(encoding="utf-8"), '---\nGithub: "x"\n---\n# 手書きのノート\n本文\n')


# ---------------------------------------------------------------------------
# Obsidian と解釈が食い違う / 置換で壊れる frontmatter は publisher でも触らない
# ---------------------------------------------------------------------------

class TestRefusedShapes(_VaultCase):
    def test_refused_notes_are_untouched(self):
        for text in (
            "---\r日付: 2030-01-02\r---\r本文\r",               # CR だけの改行
            "--- \n日付: 2030-01-02\n---\n本文\n",              # 開始行の末尾空白
            "---\nGithub: &g 古い\n別名: *g\n---\n本文\n",      # 置換で消える anchor が参照されている
        ):
            path = self.write_note("2030-01-02.md", text)
            with self.assertRaises(ObsidianPublishError, msg=repr(text)):
                self.make_publisher().publish_record(DiaryRecord(date=D, github="x"))
            self.assertEqual(path.read_bytes(), text.encode("utf-8"))
            self.assertFalse(self.backup_dir.exists())


# ---------------------------------------------------------------------------
# シンボリックリンクのフォルダ (Obsidian はたどって表示する)
# ---------------------------------------------------------------------------

class TestSymlinkedFolders(_VaultCase):
    def test_note_in_symlinked_folder_is_found_and_updated(self):
        archive = self.root / "external-archive" / "2030"
        archive.mkdir(parents=True)
        real_note = archive / "2030-01-02.md"
        real_note.write_text(EXISTING_NOTE, encoding="utf-8")
        (self.diary / "2030").symlink_to(archive, target_is_directory=True)
        pub = self.make_publisher(template_path=self.template)
        self.assertEqual(pub.find_note(D), self.diary / "2030" / "2030-01-02.md")
        outcome = pub.publish_record(DiaryRecord(date=D, github="該当なし"))
        self.assertEqual(outcome.status, "updated")
        self.assertIn('Github: "該当なし"\n', real_note.read_text(encoding="utf-8"))
        self.assertFalse((self.diary / "2030-01-02.md").exists())  # 直下に重複ノートを作らない

    def test_symlink_cycle_does_not_loop(self):
        path = self.write_note("2030-01-02.md", EXISTING_NOTE)
        (self.diary / "loop").symlink_to(self.diary, target_is_directory=True)
        (self.diary / "sub").mkdir()
        (self.diary / "sub" / "up").symlink_to(self.diary / "sub", target_is_directory=True)
        pub = self.make_publisher()
        with self.assertLogs("test-obsidian-publisher", level="WARNING") as logs:
            _logger().warning("sentinel")  # assertNoLogs (3.10+) の代わり
            found = pub.find_note(D)
        self.assertEqual(found, path)
        # 循環先 (loop/2030-01-02.md, loop/loop/…) を別ノートとして数えない = 「複数」warning 無し
        self.assertFalse(any("複数" in line for line in logs.output), logs.output)

    def test_hidden_symlinked_folder_is_ignored(self):
        archive = self.root / "hidden-target"
        archive.mkdir()
        (archive / "2030-01-02.md").write_text(EXISTING_NOTE, encoding="utf-8")
        (self.diary / ".archive").symlink_to(archive, target_is_directory=True)
        self.assertIsNone(self.make_publisher().find_note(D))

    def test_new_note_goes_next_to_neighbor_in_symlinked_folder(self):
        archive = self.root / "external-archive" / "2030"
        archive.mkdir(parents=True)
        (archive / "2030-01-01.md").write_text("---\n---\n", encoding="utf-8")
        (self.diary / "2030").symlink_to(archive, target_is_directory=True)
        outcome = self.make_publisher(template_path=self.template).publish_record(
            DiaryRecord(date=D, github="x"),
        )
        self.assertEqual(outcome.path, self.diary / "2030" / "2030-01-02.md")
        self.assertTrue((archive / "2030-01-02.md").is_file())


# ---------------------------------------------------------------------------
# 並行編集の検知の各条件 (stat / 内容 / inode / 読込中の変化 / 作成前の再探索)
# ---------------------------------------------------------------------------

class _SnapshotHookPublisher(ObsidianPublisher):
    """``_snapshot`` の n 回目の直前に ``hooks[n]`` を呼ぶ fake (1 回目 = 読込, 2 回目 = 置換前の再確認)."""

    def __init__(self, *args: object, hooks: "dict[int, object]", **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.hooks = hooks
        self.snapshot_calls = 0

    def _snapshot(self, path: Path):  # type: ignore[override]
        self.snapshot_calls += 1
        hook = self.hooks.get(self.snapshot_calls)
        if hook is not None:
            hook(path)  # type: ignore[operator]
        return super()._snapshot(path)


class _FstatHookPublisher(ObsidianPublisher):
    """1 回目の ``_snapshot`` の間だけ ``os.fstat`` を差し替える fake (読込中の変化を注入)."""

    def __init__(self, *args: object, fake_fstat: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.fake_fstat = fake_fstat
        self.snapshot_calls = 0

    def _snapshot(self, path: Path):  # type: ignore[override]
        self.snapshot_calls += 1
        if self.snapshot_calls != 1:
            return super()._snapshot(path)
        with mock.patch.object(os, "fstat", self.fake_fstat):
            return super()._snapshot(path)


TASK_NOTE = EXISTING_NOTE + "- [ ] 合成タスク\n"


class TestConcurrencyDetectionDetails(_VaultCase):
    def _kwargs(self) -> "dict[str, object]":
        return {
            "vault_dir": self.vault, "logger": _logger(), "backup_dir": self.backup_dir,
            "clock": lambda: FIXED_NOW, "sleep": lambda _s: None, "tz": JST,
        }

    def _backups(self) -> "list[Path]":
        return sorted(self.backup_dir.rglob("*.md"))

    def test_same_size_content_edit_with_restored_mtime_is_detected(self):
        # mtime / size / inode が同じでも内容の比較で検知し、相手の編集を残したまま書く
        path = self.write_note("2030-01-02.md", TASK_NOTE)

        def check_task_in_place(p: Path) -> None:
            st = os.stat(p)
            data = p.read_bytes()
            edited = data.replace("- [ ] 合成タスク".encode("utf-8"), "- [x] 合成タスク".encode("utf-8"))
            assert len(edited) == len(data) and edited != data
            with open(p, "r+b") as fh:
                fh.write(edited)
            os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
            after = os.stat(p)
            assert (after.st_mtime_ns, after.st_size, after.st_ino) == (
                st.st_mtime_ns, st.st_size, st.st_ino,
            )

        pub = _SnapshotHookPublisher(hooks={2: check_task_in_place}, **self._kwargs())
        with self.assertLogs("test-obsidian-publisher", level="WARNING") as logs:
            outcome = pub.publish_record(DiaryRecord(date=D, github="x"))
        self.assertEqual(outcome.status, "updated")
        expected, _ = upsert_frontmatter(TASK_NOTE.replace("- [ ]", "- [x]"), {"Github": "x"})
        self.assertEqual(path.read_text(encoding="utf-8"), expected)
        self.assertTrue(any("並行変更" in line for line in logs.output))

    def test_atomic_replace_with_same_bytes_size_and_mtime_is_detected_by_inode(self):
        self.write_note("2030-01-02.md", TASK_NOTE)

        def replace_same_bytes(p: Path) -> None:
            st = os.stat(p)
            tmp = p.with_name(".concurrent-writer.tmp")
            tmp.write_bytes(p.read_bytes())
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
            os.replace(tmp, p)
            after = os.stat(p)
            assert after.st_ino != st.st_ino
            assert (after.st_mtime_ns, after.st_size) == (st.st_mtime_ns, st.st_size)

        pub = _SnapshotHookPublisher(hooks={2: replace_same_bytes}, **self._kwargs())
        with self.assertLogs("test-obsidian-publisher", level="WARNING") as logs:
            outcome = pub.publish_record(DiaryRecord(date=D, github="x"))
        self.assertEqual(outcome.status, "updated")
        self.assertTrue(any("並行変更" in line for line in logs.output))
        self.assertEqual(len(self._backups()), 2)  # 1 回目は再確認で中止 → 読み直して 2 回目で書いた

    def test_change_between_the_two_fstat_calls_retries_before_writing(self):
        # 読込 (read) の後・2 回目の fstat の前に同じサイズで書き換えられた = 破れた読込
        path = self.write_note("2030-01-02.md", TASK_NOTE)
        old = time.time() - 3600
        os.utime(path, (old, old))
        real_fstat = os.fstat
        calls = []

        def fstat_with_concurrent_edit(fd: int):
            calls.append(fd)
            if len(calls) == 2:
                data = path.read_bytes()
                with open(path, "r+b") as fh:
                    fh.write(data.replace("- [ ]".encode("utf-8"), "- [x]".encode("utf-8")))
            return real_fstat(fd)

        pub = _FstatHookPublisher(fake_fstat=fstat_with_concurrent_edit, **self._kwargs())
        with self.assertLogs("test-obsidian-publisher", level="WARNING") as logs:
            outcome = pub.publish_record(DiaryRecord(date=D, github="x"))
        self.assertEqual(outcome.status, "updated")
        self.assertTrue(any("読込中に変更" in line for line in logs.output))
        self.assertEqual(len(self._backups()), 1)  # 破れた読込を基にした書込準備はしていない
        expected, _ = upsert_frontmatter(TASK_NOTE.replace("- [ ]", "- [x]"), {"Github": "x"})
        self.assertEqual(path.read_text(encoding="utf-8"), expected)

    def test_read_length_mismatch_retries_before_writing(self):
        # fstat 上は変化が無いのに読めた長さとサイズが違う (防御的な検査)
        self.write_note("2030-01-02.md", TASK_NOTE)
        real_fstat = os.fstat

        def fstat_reporting_bigger_size(fd: int):
            st = real_fstat(fd)
            return SimpleNamespace(
                st_mtime_ns=st.st_mtime_ns, st_size=st.st_size + 1, st_ino=st.st_ino,
                st_mode=st.st_mode, st_atime_ns=st.st_atime_ns,
                st_birthtime=getattr(st, "st_birthtime", None),
            )

        pub = _FstatHookPublisher(fake_fstat=fstat_reporting_bigger_size, **self._kwargs())
        with self.assertLogs("test-obsidian-publisher", level="WARNING") as logs:
            outcome = pub.publish_record(DiaryRecord(date=D, github="x"))
        self.assertEqual(outcome.status, "updated")
        self.assertTrue(any("読込中に変更" in line for line in logs.output))
        self.assertEqual(len(self._backups()), 1)

    def test_note_appearing_in_subfolder_after_indexing_is_not_duplicated(self):
        # 索引 (キャッシュ) を作った後で別プロセスがサブフォルダにノートを作った/移した
        pub = self.make_publisher(template_path=self.template)
        self.assertIsNone(pub.find_note(D))
        moved = self.write_note("2030/2030-01-02.md", EXISTING_NOTE)
        outcome = pub.publish_record(DiaryRecord(date=D, github="該当なし"))
        self.assertEqual((outcome.status, outcome.path), ("updated", moved))
        self.assertFalse((self.diary / "2030-01-02.md").exists())


class TestPublishProtocolWarnings(_VaultCase):
    def test_skipped_note_is_warned(self):
        self.write_note("2030-01-02.md", TEMPLATE)
        with self.assertLogs("test-obsidian-publisher", level="WARNING") as logs:
            self.make_publisher().publish(_health(step_count=1))
        self.assertTrue(any("status=skipped" in line for line in logs.output))


# ---------------------------------------------------------------------------
# WAVE3 W3-1 (前半): 書込後の確認が一致しなければ成功扱いにせず、最新の内容に upsert し直す
# ---------------------------------------------------------------------------

class _OverwriteAfterWritePublisher(ObsidianPublisher):
    """書き込み (置換 / 新規作成) の直後・確認読込の前に ``overwrites`` を 1 つずつ実行する fake.

    = 置換の直後に Obsidian (古いバッファの保存) / Obsidian Sync / 他のツールがノートを
    保存・移動した状況.
    """

    def __init__(
        self, *args: object, overwrites: "list[Callable[[Path], None]]", **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.overwrites = list(overwrites)

    def _verify_written(self, path: Path, expected: bytes, attempt: int) -> bool:  # type: ignore[override]
        if self.overwrites:
            self.overwrites.pop(0)(path)
        return super()._verify_written(path, expected, attempt)


def _writer(text: str) -> "Callable[[Path], None]":
    def _write(path: Path) -> None:
        path.write_bytes(text.encode("utf-8"))
    return _write


class TestVerifyAfterWrite(_VaultCase):
    RECORD_UPDATES = {"歩数 (歩)": 5555, "Github": "x"}

    def _record(self) -> DiaryRecord:
        return DiaryRecord(date=D, health=_health(step_count=5555), github="x")

    def _make(self, overwrites: "list[Callable[[Path], None]]", **kwargs: object):
        params: "dict[str, object]" = {
            "vault_dir": self.vault, "logger": _logger(), "backup_dir": self.backup_dir,
            "clock": lambda: FIXED_NOW, "sleep": lambda _s: None, "tz": JST,
        }
        params.update(kwargs)
        return _OverwriteAfterWritePublisher(overwrites=overwrites, **params)

    def _backups(self) -> "list[Path]":
        return sorted(self.backup_dir.rglob("*.md")) if self.backup_dir.exists() else []

    def test_overwritten_right_after_replace_is_reapplied_on_latest_content(self):
        # 置換直後に Obsidian が「こちらの値を含まない古いバッファ + 利用者の追記」を保存した
        path = self.write_note("2030-01-02.md", EXISTING_NOTE)
        stale = EXISTING_NOTE + "Obsidian で追記した行\n"
        pub = self._make([_writer(stale)])
        with self.assertLogs("test-obsidian-publisher", level="WARNING") as logs:
            outcome = pub.publish_record(self._record())
        expected, _ = upsert_frontmatter(stale, self.RECORD_UPDATES)
        self.assertEqual(path.read_text(encoding="utf-8"), expected)  # 両方の内容が残る
        self.assertEqual(outcome.status, "updated")
        self.assertEqual(outcome.written_keys, ("歩数 (歩)", "Github"))
        # 書込前バックアップは 2 つ: 元の内容 / 2 回目の書き込みの基 (他のプログラムの保存内容)
        day_dir = self.backup_dir / "2030-01-05"
        first = day_dir / "2030-01-02.20300105T040506Z.md"
        second = day_dir / "2030-01-02.20300105T040506Z-1.md"
        self.assertEqual(self._backups(), sorted([first, second]))
        self.assertEqual(first.read_bytes(), EXISTING_NOTE.encode("utf-8"))
        self.assertEqual(second.read_bytes(), stale.encode("utf-8"))
        self.assertEqual(outcome.backup_path, second)
        self.assertTrue(any("やり直します" in line for line in logs.output), logs.output)
        self.assertFalse(any("次回実行" in line for line in logs.output))

    def test_persistent_overwrite_is_an_error_not_a_success(self):
        path = self.write_note("2030-01-02.md", EXISTING_NOTE)
        other = EXISTING_NOTE + "他のプログラムが保存した行\n"
        pub = self._make([_writer(other)] * 3)
        with self.assertLogs("test-obsidian-publisher", level="WARNING") as logs:
            with self.assertRaises(ObsidianPublishError) as ctx:
                pub.publish_record(self._record())
        self.assertNotIsInstance(ctx.exception, EditInvariantError)
        message = str(ctx.exception)
        self.assertIn("確認できません", message)
        self.assertIn("3 回試行", message)
        self.assertIn("書き込み 3 回", message)
        backups = self._backups()
        self.assertEqual(len(backups), 3)
        for backup in backups:
            self.assertIn(str(backup), message)  # どこから戻せるかがメッセージで分かる
        # 最後に保存されたのは他のプログラムの内容 (こちらの値は残っていない)
        self.assertEqual(path.read_text(encoding="utf-8"), other)
        self.assertTrue(any("試行回数の上限" in line for line in logs.output), logs.output)
        self.assertFalse(any("次回実行" in line for line in logs.output + [message]))

    def test_single_attempt_fails_immediately(self):
        self.write_note("2030-01-02.md", EXISTING_NOTE)
        pub = self._make([_writer(EXISTING_NOTE)], max_attempts=1)
        with self.assertLogs("test-obsidian-publisher", level="WARNING"):
            with self.assertRaises(ObsidianPublishError) as ctx:
                pub.publish_record(self._record())
        self.assertIn("1 回試行", str(ctx.exception))

    def test_overwrite_that_kept_our_values_is_reported_as_written(self):
        # 他のプログラムがこちらの書いた内容を読み込んでから保存し直した (本文に追記):
        # 最終試行では値が既に同じ (unchanged) だが、値を書いたのはこの実行なので update と報告
        path = self.write_note("2030-01-02.md", EXISTING_NOTE)

        def reload_and_append(p: Path) -> None:
            p.write_bytes(p.read_bytes() + "追記\n".encode("utf-8"))

        pub = self._make([reload_and_append])
        with self.assertLogs("test-obsidian-publisher", level="WARNING"):
            outcome = pub.publish_record(self._record())
        written, _ = upsert_frontmatter(EXISTING_NOTE, self.RECORD_UPDATES)
        self.assertEqual(path.read_text(encoding="utf-8"), written + "追記\n")
        self.assertEqual(outcome.status, "updated")
        self.assertEqual([c.action for c in outcome.changes], ["update", "update"])
        self.assertEqual(outcome.written_keys, ("歩数 (歩)", "Github"))
        # バックアップはこの実行の前の内容 (最終試行では書いていない)
        assert outcome.backup_path is not None
        self.assertEqual(outcome.backup_path.read_bytes(), EXISTING_NOTE.encode("utf-8"))
        self.assertEqual(len(self._backups()), 1)

    def test_overwrite_that_dropped_our_value_is_not_reported_as_written(self):
        # 直後に保存された内容ではこちらの値が消え、利用者のコメント付きの値になっていた
        # → 書き直せない (kept) ので「この実行で書いた」とは報告しない
        path = self.write_note("2030-01-02.md", EXISTING_NOTE)
        other = EXISTING_NOTE.replace("Github:\n", "Github: 手入力  # メモ\n", 1)
        pub = self._make([_writer(other)])
        with self.assertLogs("test-obsidian-publisher", level="WARNING"):
            outcome = pub.publish_record(DiaryRecord(date=D, github="x"))
        self.assertEqual(path.read_text(encoding="utf-8"), other)
        self.assertEqual(outcome.status, "unchanged")
        self.assertEqual([c.action for c in outcome.changes], ["kept"])
        self.assertTrue(outcome.changes[0].warning.startswith(WARNING_COMMENT_PRESENT))  # type: ignore[union-attr]
        self.assertEqual(outcome.written_keys, ())
        self.assertIsNone(outcome.backup_path)

    def test_note_moved_right_after_write_is_found_and_verified(self):
        # 確認読込に失敗 (直後にサブフォルダへ移動された) → 探し直し、移動先で値を確かめる
        original = self.write_note("2030-01-02.md", EXISTING_NOTE)
        moved = self.diary / "2030" / "2030-01-02.md"

        def move(p: Path) -> None:
            moved.parent.mkdir()
            os.replace(p, moved)

        pub = self._make([move])
        with self.assertLogs("test-obsidian-publisher", level="WARNING") as logs:
            outcome = pub.publish_record(DiaryRecord(date=D, github="x"))
        expected, _ = upsert_frontmatter(EXISTING_NOTE, {"Github": "x"})
        self.assertFalse(original.exists())
        self.assertEqual(moved.read_text(encoding="utf-8"), expected)
        self.assertEqual((outcome.status, outcome.path), ("updated", moved))
        self.assertEqual(outcome.written_keys, ("Github",))
        self.assertTrue(any("確認読込に失敗" in line for line in logs.output), logs.output)

    def test_unreadable_right_after_write_is_retried(self):
        path = self.write_note("2030-01-02.md", EXISTING_NOTE)
        real_read_bytes = Path.read_bytes
        calls: "list[Path]" = []

        def flaky_read_bytes(self_path: Path) -> bytes:
            calls.append(self_path)
            if len(calls) == 1:
                raise PermissionError("synthetic: denied")
            return real_read_bytes(self_path)

        class _FlakyVerifyPublisher(ObsidianPublisher):
            def _verify_written(self, p: Path, expected: bytes, attempt: int) -> bool:  # type: ignore[override]
                with mock.patch.object(Path, "read_bytes", flaky_read_bytes):
                    return super()._verify_written(p, expected, attempt)

        pub = _FlakyVerifyPublisher(
            vault_dir=self.vault, logger=_logger(), backup_dir=self.backup_dir,
            clock=lambda: FIXED_NOW, sleep=lambda _s: None, tz=JST,
        )
        with self.assertLogs("test-obsidian-publisher", level="WARNING") as logs:
            outcome = pub.publish_record(self._record())
        expected, _ = upsert_frontmatter(EXISTING_NOTE, self.RECORD_UPDATES)
        self.assertEqual(path.read_text(encoding="utf-8"), expected)
        self.assertEqual(outcome.status, "updated")
        self.assertEqual(outcome.written_keys, ("歩数 (歩)", "Github"))
        self.assertTrue(any("確認読込に失敗" in line for line in logs.output), logs.output)

    def test_created_note_overwritten_right_after_creation_is_updated(self):
        # 作った直後に Obsidian Sync が別端末の同名ノートで上書きした → そのノートに upsert し直す
        synced = EXISTING_NOTE + "別端末で書いた本文\n"
        pub = self._make([_writer(synced)], template_path=self.template)
        with self.assertLogs("test-obsidian-publisher", level="WARNING"):
            outcome = pub.publish_record(DiaryRecord(date=D, health=_health(step_count=9)))
        path = self.diary / "2030-01-02.md"
        expected, _ = upsert_frontmatter(synced, {"歩数 (歩)": 9})
        self.assertEqual(path.read_text(encoding="utf-8"), expected)
        self.assertEqual((outcome.status, outcome.path), ("updated", path))
        assert outcome.backup_path is not None
        self.assertEqual(outcome.backup_path.read_bytes(), synced.encode("utf-8"))
        self.assertEqual([p.name for p in self.diary.iterdir()], ["2030-01-02.md"])

    def test_normal_write_is_verified_without_warnings(self):
        self.write_note("2030-01-02.md", EXISTING_NOTE)
        pub = self._make([])
        with self.assertLogs("test-obsidian-publisher", level="WARNING") as logs:
            _logger().warning("sentinel")  # assertNoLogs (3.10+) の代わり
            outcome = pub.publish_record(self._record())
        self.assertEqual(outcome.status, "updated")
        self.assertEqual(logs.output, ["WARNING:test-obsidian-publisher:sentinel"])

    def test_note_emptied_right_after_write_is_an_error_not_a_benign_skip(self):
        # finding: 書いた直後に他のプログラムがノートを空にすると、やり直しの試行が「直前に
        # 変更された空ノート」として skipped (recently_modified) を返し、想定内の skip として
        # exit 0・通知なし・バックアップの場所も無しで終わっていた (元の内容はバックアップにしか無い)
        path = self.write_note("2030-01-02.md", EXISTING_NOTE)

        def empty_now(p: Path) -> None:
            p.write_bytes(b"")
            stamp = FIXED_NOW.timestamp()
            os.utime(p, (stamp, stamp))   # 今さっき変更された空ノート

        pub = self._make([empty_now], template_path=self.template)
        with self.assertLogs("test-obsidian-publisher", level="WARNING"):
            with self.assertRaises(ObsidianPublishError) as ctx:
                pub.publish_record(self._record())
        self.assertNotIsInstance(ctx.exception, EditInvariantError)
        message = str(ctx.exception)
        self.assertIn("確認できません", message)
        self.assertIn("status=skipped, reason=recently_modified", message)
        (backup,) = self._backups()
        self.assertIn(str(backup), message)   # どこから戻せるかがメッセージで分かる
        self.assertEqual(backup.read_bytes(), EXISTING_NOTE.encode("utf-8"))
        self.assertEqual(path.read_bytes(), b"", "空にされたノートにはそれ以上書かない")

    def test_note_moved_out_of_the_diary_right_after_write_is_an_error(self):
        path = self.write_note("2030-01-02.md", EXISTING_NOTE)
        outside = self.vault / "移動先.md"

        def move_out(p: Path) -> None:
            os.replace(p, outside)

        pub = self._make([move_out])   # テンプレート無し: 作り直せない (missing)
        with self.assertLogs("test-obsidian-publisher", level="WARNING"):
            with self.assertRaises(ObsidianPublishError) as ctx:
                pub.publish_record(self._record())
        self.assertIn("status=missing", str(ctx.exception))
        self.assertFalse(path.exists())
        self.assertTrue(outside.exists())


# ---------------------------------------------------------------------------
# WAVE3 W3-1 (後半): 書込前の独立した不変条件チェック (編集ロジックとは別実装)
# ---------------------------------------------------------------------------

#: 担当外の行・コメント・空行・block 値を含む合成ノート.
RICH_NOTE = (
    "---\n"
    "# 行頭コメント\n"
    "名前: 毎日の習慣 2030-01-02\n"
    "日付: 2030-01-02\n"
    "朝散歩:\n"
    "  - カーテンを開ける\n"
    "歩数 (歩): 100  # 手動で入れた値\n"
    "睡眠時間 (分):\n"
    "\n"
    "Github: |-\n"
    "  [a](https://example.com/1)\n"
    "  [b](https://example.com/2)\n"
    "\n"
    "# 末尾コメント\n"
    "天気:\n"
    "---\n"
    "# 毎日の習慣 2030-01-02（水）\n"
    "\n"
    "- 合成の本文メモ\n"
)


class TestVerifyEditInvariants(unittest.TestCase):
    """:func:`verify_edit_invariants` 単体: 正しい編集は通し、担当外の変更は必ず止める."""

    CORPUS = (
        EXISTING_NOTE,
        RICH_NOTE,
        expand_template(TEMPLATE, D),
        "---\n日付: 2030-01-04\n---\n\n# 🤖 2030-01-04の日報\n",
        "# frontmatter の無いノート\n本文\n",
        "﻿# BOM 付き・frontmatter 無し\n",
        "﻿---\r\n日付: 2030-01-02\r\n歩数 (歩):\r\n---\r\n本文\r\n",
        "---\n日付: 2030-01-02\n---",
        "---\n\"歩数 (歩)\": 1\n'天気': 晴\nGithub:\n  - a\n\n  - b\n# c\n\n---\nbody\n",
        "---\nGithub: \"[x](u)\n  続き\"\n天気: |+\n  晴\n\n\n---\nbody\n",
        "---\n日付: 1\r\n歩数 (歩):\r\n---\r\nbody\r\n",                 # 改行の混在
        "---\nGithub:\n---\n本文\n---\n歩数 (歩): 本文中の偽 frontmatter\n",  # 本文の key 風の行
        # 担当 entry の後ろの字下げ付きコメント行 (entry の外: 編集は残す)
        "---\n歩数 (歩): 100\n  # 手入力 (消さないで)\n\n  # もう 1 行\n天気: 晴\n---\nbody\n",
    )
    UPDATES = {"歩数 (歩)": 5555, "睡眠時間 (分)": 420, "Github": "y", "天気": "晴", "新キー": 1}
    KEY_SETS = (
        ("歩数 (歩)",), ("Github",), ("天気", "新キー"), tuple(UPDATES),
    )

    def test_real_editor_outputs_pass(self):
        checked = 0
        for old in self.CORPUS:
            for keys in self.KEY_SETS:
                for fill_only_empty in (False, True):
                    new, changes = upsert_frontmatter(
                        old, {k: self.UPDATES[k] for k in keys}, fill_only_empty=fill_only_empty,
                    )
                    changed = [c.key for c in changes if c.action in ("insert", "update")]
                    verify_edit_invariants(old.encode("utf-8"), new.encode("utf-8"), changed)
                    checked += new != old
        self.assertGreater(checked, 40)  # 実際に書き換わる組み合わせを十分に通している

    def _rich_pair(self) -> "tuple[bytes, str, list[str]]":
        new, changes = upsert_frontmatter(RICH_NOTE, {"睡眠時間 (分)": 420, "Github": "y", "新キー": 1})
        changed = [c.key for c in changes if c.action in ("insert", "update")]
        self.assertEqual(sorted(changed), ["Github", "新キー", "睡眠時間 (分)"])
        verify_edit_invariants(RICH_NOTE.encode("utf-8"), new.encode("utf-8"), changed)
        return RICH_NOTE.encode("utf-8"), new, changed

    def test_every_kind_of_collateral_change_is_detected(self):
        old, new, changed = self._rich_pair()
        mutations = {
            "担当外 entry を削除": lambda t: t.replace("日付: 2030-01-02\n", "", 1),
            "担当外 entry の値を変更": lambda t: t.replace("日付: 2030-01-02", "日付: 2030-01-03", 1),
            "担当外 entry の順序を入れ替え": lambda t: t.replace(
                "名前: 毎日の習慣 2030-01-02\n日付: 2030-01-02\n",
                "日付: 2030-01-02\n名前: 毎日の習慣 2030-01-02\n", 1,
            ),
            "担当外の継続行を削除": lambda t: t.replace("  - カーテンを開ける\n", "", 1),
            "行頭コメントを削除": lambda t: t.replace("# 行頭コメント\n", "", 1),
            "末尾コメントを削除": lambda t: t.replace("# 末尾コメント\n", "", 1),
            "entry の後ろの空行を削除": lambda t: t.replace("\n\n# 末尾コメント", "\n# 末尾コメント", 1),
            "書かなかった担当 entry (kept) を変更": lambda t: t.replace(
                "歩数 (歩): 100  # 手動で入れた値\n", "歩数 (歩): 5555\n", 1,
            ),
            "担当外の行を追加": lambda t: t.replace("天気:\n", "天気:\n余計: 1\n", 1),
            "担当キーを重複して追加": lambda t: t.replace('Github: "y"\n', 'Github: "y"\nGithub: "y"\n', 1),
            "本文を 1 文字変更": lambda t: t.replace("合成の本文メモ", "合成の本文メモ。", 1),
            "本文の末尾改行を削除": lambda t: t[:-1],
            "BOM を追加": lambda t: "﻿" + t,
            "区切り行の改行を CRLF に": lambda t: t.replace("---\n", "---\r\n", 1),
            "frontmatter を削除": lambda t: t[t.index("---\n# 毎日") + 4:],
        }
        for label, mutate in mutations.items():
            mutated = mutate(new)
            self.assertNotEqual(mutated, new, label)
            with self.assertRaises(EditInvariantError, msg=label) as ctx:
                verify_edit_invariants(old, mutated.encode("utf-8"), changed)
            # メッセージに日記の内容を出さない (行番号だけ)
            self.assertNotIn("カーテン", str(ctx.exception), label)
            self.assertNotIn("本文メモ", str(ctx.exception), label)

    def test_indented_comment_after_a_written_entry_is_protected(self):
        # finding: 字下げ付きのコメント行 (と直前の空行) は書き換えた entry の範囲とみなされ、
        # 編集の不具合で消えても / 書き換わっても検出できなかった (行頭のコメントは検出できた)
        old = "---\n歩数 (歩): 100\n\n  # 手入力: 万歩計の値 (消さないで)\n体重 (kg): 60\n---\n本文\n"
        new, changes = upsert_frontmatter(old, {"歩数 (歩)": 8123})
        self.assertIn("  # 手入力", new)   # 正しい編集はコメントを残す
        verify_edit_invariants(old.encode("utf-8"), new.encode("utf-8"), ["歩数 (歩)"])
        broken = {
            "字下げ付きコメントを削除": new.replace("  # 手入力: 万歩計の値 (消さないで)\n", "", 1),
            "字下げ付きコメントを書き換え": new.replace("# 手入力", "# 書換", 1),
            "コメントの前の空行を削除": new.replace("8123\n\n", "8123\n", 1),
            "entry とコメントを丸ごと 1 行に": new.replace(
                "8123\n\n  # 手入力: 万歩計の値 (消さないで)\n", "8123\n", 1),
        }
        for label, mutated in broken.items():
            self.assertNotEqual(mutated, new, label)
            with self.assertRaises(EditInvariantError, msg=label):
                verify_edit_invariants(old.encode("utf-8"), mutated.encode("utf-8"), ["歩数 (歩)"])
        # 継続行の間にコメントがある entry (正しい編集なら kept で書き換えない) を壊れた編集が
        # 1 行にした場合も、コメントとその直前の空行は残っていなければならない
        inner = "---\nGithub:\n  - a\n\n  # メモ\n  - b\n天気: 晴\n---\n"
        verify_edit_invariants(
            inner.encode("utf-8"), '---\nGithub: "x"\n\n  # メモ\n天気: 晴\n---\n'.encode("utf-8"),
            ["Github"],
        )
        with self.assertRaises(EditInvariantError):
            verify_edit_invariants(
                inner.encode("utf-8"), '---\nGithub: "x"\n  # メモ\n天気: 晴\n---\n'.encode("utf-8"),
                ["Github"],
            )

    def test_changed_key_must_end_up_with_exactly_one_entry(self):
        old = b"---\na: 1\n---\n"
        with self.assertRaises(EditInvariantError):
            verify_edit_invariants(old, old, ["b"])  # 書いたはずの b が無い
        with self.assertRaises(EditInvariantError):
            verify_edit_invariants(b"---\na: 1\na: 2\n---\n", b"---\na: 9\na: 2\n---\n", ["a"])
        verify_edit_invariants(old, b"---\na: 2\n---\n", ["a"])

    def test_note_without_frontmatter_may_only_gain_changed_keys(self):
        old = "本文\n".encode("utf-8")
        verify_edit_invariants(old, '---\nGithub: "x"\n---\n本文\n'.encode("utf-8"), ["Github"])
        with self.assertRaises(EditInvariantError):
            verify_edit_invariants(old, '---\nGithub: "x"\n他: 1\n---\n本文\n'.encode("utf-8"), ["Github"])
        with self.assertRaises(EditInvariantError):
            verify_edit_invariants(old, '---\nGithub: "x"\n---\n本文!\n'.encode("utf-8"), ["Github"])

    def test_quoted_and_nfd_keys_are_recognised(self):
        nfd = unicodedata.normalize("NFD", "アクティビティ時間 (分)")
        for line in ('"歩数 (歩)": 1\n', "'歩数 (歩)': 1\n", '"\\u6b69\\u6570 (\\u6b69)": 1\n'):
            old = f"---\n{line}b: 2\n---\n".encode("utf-8")
            new = f"---\n{line.split(':')[0]}: 5\nb: 2\n---\n".encode("utf-8")
            verify_edit_invariants(old, new, ["歩数 (歩)"])
        verify_edit_invariants(
            f"---\n{nfd}:\n---\n".encode("utf-8"), f"---\n{nfd}: 3\n---\n".encode("utf-8"),
            ["アクティビティ時間 (分)"],
        )


class TestIndependentCheckBlocksBrokenEditor(_VaultCase):
    """編集ロジックを壊しても、独立チェックが書く前に止めること (mutation テスト)."""

    def _broken_upsert(self, mutate: "Callable[[str], str]"):
        real = upsert_frontmatter

        def broken(text: str, updates, **kwargs):  # type: ignore[no-untyped-def]
            new_text, changes = real(text, updates, **kwargs)
            return mutate(new_text), changes

        return mock.patch("ihealth.publishers.obsidian.upsert_frontmatter", broken)

    def _assert_nothing_written(self, path: Path, before: bytes) -> None:
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.backup_dir.exists())  # バックアップも作らない (= 書く前に止めた)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), [path.name])  # tmp も無い

    def test_dropping_a_non_target_line_is_blocked_before_writing(self):
        path = self.write_note("2030-01-02.md", EXISTING_NOTE)
        drop = self._broken_upsert(lambda t: t.replace("日付: 2030-01-02\n", "", 1))
        with drop, self.assertRaises(EditInvariantError) as ctx:
            self.make_publisher().publish_record(DiaryRecord(date=D, github="x"))
        self._assert_nothing_written(path, EXISTING_NOTE.encode("utf-8"))
        self.assertIsInstance(ctx.exception, ObsidianPublishError)  # 書込失敗系 (exit 11)
        self.assertNotIsInstance(ctx.exception, ObsidianConfigError)
        self.assertIn(str(path), str(ctx.exception))

    def test_body_change_is_blocked_before_writing(self):
        path = self.write_note("2030-01-02.md", EXISTING_NOTE)
        with self._broken_upsert(lambda t: t.replace("合成の本文メモ", "合成の本文メモ!", 1)):
            with self.assertRaises(EditInvariantError):
                self.make_publisher().publish_record(DiaryRecord(date=D, github="x"))
        self._assert_nothing_written(path, EXISTING_NOTE.encode("utf-8"))

    def test_dry_run_reports_the_refusal_without_writing(self):
        self.write_note("2030-01-02.md", EXISTING_NOTE)
        before = self.tree()
        with self._broken_upsert(lambda t: t.replace("名前: 毎日の習慣 2030-01-02\n", "", 1)):
            with self.assertRaises(EditInvariantError):
                self.make_publisher(dry_run=True).publish_record(DiaryRecord(date=D, github="x"))
        self.assertEqual(self.tree(), before)

    def test_new_note_from_broken_editor_is_not_created(self):
        with self._broken_upsert(lambda t: t.replace("日記: false\n", "", 1)):
            with self.assertRaises(EditInvariantError):
                self.make_publisher(template_path=self.template).publish_record(
                    DiaryRecord(date=D, github="x"),
                )
        self.assertEqual(list(self.diary.iterdir()), [])

    def test_empty_note_is_not_initialised_by_broken_editor(self):
        path = self.write_note("2030-01-02.md", "")
        with self._broken_upsert(lambda t: t.replace("日記: false\n", "", 1)):
            with self.assertRaises(EditInvariantError):
                self.make_publisher(template_path=self.template).publish_record(
                    DiaryRecord(date=D, github="x"),
                )
        self._assert_nothing_written(path, b"")

    def test_entry_reported_as_kept_must_stay_byte_identical(self):
        note = EXISTING_NOTE.replace("歩数 (歩):\n", "歩数 (歩): 100  # 手動\n", 1)
        path = self.write_note("2030-01-02.md", note)
        overwrite_kept = self._broken_upsert(
            lambda t: t.replace("歩数 (歩): 100  # 手動\n", "歩数 (歩): 5555\n", 1),
        )
        with overwrite_kept, self.assertRaises(EditInvariantError):
            self.make_publisher().publish_record(
                DiaryRecord(date=D, health=_health(step_count=5555), github="x"),
            )
        self._assert_nothing_written(path, note.encode("utf-8"))

    def test_pre_w3_3_comment_dropping_edit_is_blocked(self):
        # W3-3 以前の編集 (コメントを挟んだ entry を丸ごと 1 行にしてコメントを消す) を模す
        note = EXISTING_NOTE.replace("Github:\n", "Github:\n  - a\n# メモ\n  - b\n", 1)
        path = self.write_note("2030-01-02.md", note)

        def old_behaviour(text: str, updates, **kwargs):  # type: ignore[no-untyped-def]
            new_text = text.replace("Github:\n  - a\n# メモ\n  - b\n", 'Github: "x"\n', 1)
            return new_text, [FieldChange("Github", "update", "\n  - a\n# メモ\n  - b", '"x"')]

        with mock.patch("ihealth.publishers.obsidian.upsert_frontmatter", old_behaviour):
            with self.assertRaises(EditInvariantError):
                self.make_publisher().publish_record(DiaryRecord(date=D, github="x"))
        self._assert_nothing_written(path, note.encode("utf-8"))

    def test_broken_editor_tokenizer_is_caught_by_the_independent_check(self):
        # 編集ロジック内部 (entry 範囲の計算) を壊す: 各 entry が次の行まで飲み込む.
        # 独立チェックはこの関数を使わないので、壊れた編集結果を見抜ける.
        real_end = frontmatter_module._multiline_end

        def swallow_next_line(lines, key_idx, close_idx, value_head):  # type: ignore[no-untyped-def]
            return max(real_end(lines, key_idx, close_idx, value_head), min(key_idx + 1, close_idx - 1))

        path = self.write_note("2030-01-02.md", EXISTING_NOTE)
        record = DiaryRecord(date=D, health=_health(step_count=5555))
        with mock.patch.object(frontmatter_module, "_multiline_end", swallow_next_line):
            broken, changes = frontmatter_module.upsert_frontmatter(EXISTING_NOTE, {"歩数 (歩)": 5555})
            # 前提の確認: 壊れた編集は担当外の「睡眠時間 (分):」を消し、それでも update と報告する
            self.assertNotIn("睡眠時間 (分):", broken)
            self.assertEqual([c.action for c in changes], ["update"])
            with self.assertRaises(EditInvariantError):
                self.make_publisher().publish_record(record)
            self._assert_nothing_written(path, EXISTING_NOTE.encode("utf-8"))
            # 対照: 独立チェックを外すと、同じ壊れた編集がそのまま書き込まれてしまう
            with mock.patch("ihealth.publishers.obsidian.verify_edit_invariants", lambda *a: None):
                self.make_publisher(backup_dir=None).publish_record(record)
        self.assertNotIn("睡眠時間 (分):", path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# WAVE3 W3-3: コメント付き / 重複した担当キーは publisher でも書かずにレポートに残す
# ---------------------------------------------------------------------------

class TestKeptForSafety(_VaultCase):
    def test_commented_and_duplicated_keys_are_kept_others_written(self):
        note = (
            EXISTING_NOTE
            .replace("歩数 (歩):\n", "歩数 (歩): 100  # 手動で入れた値\n", 1)
            .replace("気温:\n", "気温:\n気温: 重複\n", 1)
        )
        path = self.write_note("2030-01-02.md", note)
        record = DiaryRecord(
            date=D, health=_health(step_count=5555), weather=_weather(), github="x",
        )
        with self.assertLogs("test-obsidian-publisher", level="WARNING") as logs:
            outcome = self.make_publisher().publish_record(record)
        changes = {c.key: c for c in outcome.changes}
        self.assertEqual(changes["歩数 (歩)"].action, "kept")
        self.assertTrue(changes["歩数 (歩)"].warning.startswith(WARNING_COMMENT_PRESENT))  # type: ignore[union-attr]
        self.assertEqual(changes["気温"].action, "kept")
        self.assertTrue(changes["気温"].warning.startswith(WARNING_DUPLICATE_KEY))  # type: ignore[union-attr]
        self.assertEqual(changes["Github"].action, "update")
        text = path.read_text(encoding="utf-8")
        self.assertIn("歩数 (歩): 100  # 手動で入れた値\n", text)
        self.assertIn("気温:\n気温: 重複\n", text)
        self.assertIn('Github: "x"\n', text)
        self.assertEqual(outcome.status, "updated")
        self.assertTrue(any(WARNING_COMMENT_PRESENT in line for line in logs.output))
        self.assertTrue(any(WARNING_DUPLICATE_KEY in line for line in logs.output))

    def test_existing_states_reads_without_writing(self):
        # 未確定の値の台帳が「利用者が手で書いた値か」を判定するための読み取り
        note = EXISTING_NOTE.replace("歩数 (歩):\n", "歩数 (歩): 1200  # メモ\n", 1).replace(
            "体重 (kg):\n", "体重 (kg): 64.5\n", 1).replace("気温:\n", "気温:\n気温: 重複\n", 1)
        path = self.write_note("2030-01-02.md", note)
        before = path.stat()
        pub = self.make_publisher()
        states = pub.existing_states(path, {
            "歩数 (歩)": 1200, "体重 (kg)": 64.8, "睡眠時間 (分)": None, "天気": None,
            "気温": None, "Github": "x", "無いキー": None,
        })
        self.assertEqual(states, {
            "歩数 (歩)": "same", "体重 (kg)": "other", "睡眠時間 (分)": "empty", "天気": "empty",
            "気温": "unknown", "Github": "empty", "無いキー": "empty",
        })
        self.assertEqual(path.read_bytes(), note.encode("utf-8"))
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
        missing = pub.existing_states(self.diary / "無い.md", {"歩数 (歩)": 1})
        self.assertEqual(missing, {"歩数 (歩)": "unknown"})
        broken = self.write_note("2030-01-03.md", "---\n歩数 (歩): 1\n")   # 閉じ --- が無い
        self.assertEqual(pub.existing_states(broken, {"歩数 (歩)": 1}), {"歩数 (歩)": "unknown"})

    def test_note_with_only_kept_keys_is_not_rewritten(self):
        note = EXISTING_NOTE.replace("Github:\n", "Github:  # あとで書く\n", 1)
        path = self.write_note("2030-01-02.md", note)
        before = path.stat()
        with self.assertLogs("test-obsidian-publisher", level="WARNING"):
            outcome = self.make_publisher().publish_record(DiaryRecord(date=D, github="x"))
        self.assertEqual(outcome.status, "unchanged")
        self.assertEqual([c.action for c in outcome.changes], ["kept"])
        self.assertIsNone(outcome.backup_path)
        self.assertEqual(path.read_bytes(), note.encode("utf-8"))
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)


if __name__ == "__main__":
    unittest.main()
