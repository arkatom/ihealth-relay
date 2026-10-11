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
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from ihealth.frontmatter import split_frontmatter, upsert_frontmatter
from ihealth.models import DailyHealthData
from ihealth.publishers.obsidian import (
    DEFAULT_PROPERTY_MAP,
    HEALTH_FIELDS,
    VALUE_CONVERTERS,
    NotePublishOutcome,
    ObsidianConfigError,
    ObsidianPublishError,
    ObsidianPublisher,
    TemplateExpansionError,
    expand_template,
)
from ihealth.records import WEATHER_FIELDS, DiaryRecord


D = date(2030, 1, 2)  # 水曜日
FIXED_NOW = datetime(2030, 1, 3, 4, 5, 6, tzinfo=timezone.utc)

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
        "sunshine": "8.9時間",
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
        health = _health(
            step_count=4009,
            sleep_hours=7.25,
            nap_hours=92 / 60,
            active_energy_kcal=1899.44,
            distance_km=3.06,
            heart_rate_resting=58.04,
            heart_rate_avg=66.66,
            heart_rate_max=150.0,
            oxygen_saturation=96.44,
            exercise_intensity_score=36.0,
            mindful_minutes=20.4,
            mindful_sessions=2,
            exercise_minutes=30.6,
            body_mass_kg=66.0,
            body_fat_percentage=15.84,
        )
        updates = pub.build_updates(DiaryRecord(date=D, health=health))
        self.assertEqual(
            updates,
            {
                "歩数 (歩)": 4009,
                "睡眠時間 (分)": 435,
                "昼寝時間": 92,
                "消費カロリー (kcal)": 1899.4,
                "移動距離 (km)": 3.1,
                "安静時心拍数 (bpm)": 58.0,
                "平均心拍数 (bpm)": 66.7,
                "最大心拍数 (bpm)": 150.0,
                "酸素飽和度 (%)": 96.4,
                "運動強度スコア": 36.0,
                "瞑想時間 (分)": 20,
                "瞑想回数 (回)": 2,
                "アクティビティ時間 (分)": 31,
                "体重 (kg)": 66.0,
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
            date=D, health=_health(step_count=1), activity_detail="瞑想3分、その他31分",
            weather=_weather(), github="該当なし",
        )
        updates = pub.build_updates(record)
        self.assertEqual(
            list(updates),
            ["歩数 (歩)", "気温", "湿度", "気圧", "天気", "日照時間", "降水量", "アクティビティ詳細", "Github"],
        )
        self.assertEqual(updates["Github"], "該当なし")
        self.assertEqual(updates["日照時間"], "8.9時間")

    def test_weather_object_with_missing_attributes(self):
        pub = self.make_publisher()
        updates = pub.build_updates(DiaryRecord(date=D, weather=SimpleNamespace(weather="晴")))
        self.assertEqual(updates, {"天気": "晴"})

    def test_invalid_health_values_raise(self):
        pub = self.make_publisher()
        for name, bad in (("step_count", True), ("distance_km", float("nan")), ("body_mass_kg", "66")):
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
            health=_health(step_count=4009, sleep_hours=7.0, body_mass_kg=60.5),
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
        manual = manual.replace("歩数 (歩):\n", "歩数 (歩): 4009\n", 1)
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
            '日照時間: "8.9時間"\n'
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
        self.assertEqual(backup.parent, self.backup_dir / "2030-01-03")
        self.assertEqual(backup.name, "2030-01-02.20300103T040506Z.md")
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
        self.assertEqual((kept.old, kept.new), ("123", "4009"))

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
            outcome = pub.publish_record(DiaryRecord(date=D, health=_health(step_count=4009)))
        self.assertEqual(outcome.status, "updated")
        text = path.read_text(encoding="utf-8")
        # 並行編集 (本文末尾の追記) も、こちらの値も両方残る
        self.assertIn("並行編集 2\n", text)
        self.assertIn("歩数 (歩): 4009\n", text)
        expected, _ = upsert_frontmatter(EXISTING_NOTE + "並行編集 2\n", {"歩数 (歩)": 4009})
        self.assertEqual(text, expected)
        self.assertTrue(any("並行変更" in line for line in logs.output))
        self.assertEqual([p.name for p in self.diary.iterdir()], ["2030-01-02.md"])

    def test_persistent_conflict_gives_up_without_writing(self):
        path = self.write_note("2030-01-02.md", EXISTING_NOTE)
        pub = self._make(edits=99)
        with self.assertLogs("test-obsidian-publisher", level="WARNING"):
            with self.assertRaises(ObsidianPublishError):
                pub.publish_record(DiaryRecord(date=D, health=_health(step_count=4009)))
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("4009", text)  # こちらの値は一度も書かれていない
        self.assertEqual(text.count("並行編集"), 3)  # max_attempts=3 回とも競合
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
        record = DiaryRecord(date=D, health=_health(step_count=4009), github="該当なし")
        outcome = pub.publish_record(record)
        path = self.diary / "2030-01-02.md"
        self.assertEqual(outcome.status, "created")
        self.assertEqual(outcome.path, path)
        expected, _ = upsert_frontmatter(
            expand_template(TEMPLATE, D), {"歩数 (歩)": 4009, "Github": "該当なし"},
        )
        self.assertEqual(path.read_text(encoding="utf-8"), expected)
        self.assertIn('"歩数 (歩)": 4009\n', expected)
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
        self.assertEqual(outcome, NotePublishOutcome(D, None, "missing", ()))
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
        self.assertTrue((self.backup_dir / "2030-01-03" / "2030-01-02.20300103T040506Z.md").exists())

    def test_backup_name_collision_gets_suffix(self):
        self.write_note("2030-01-02.md", EXISTING_NOTE)
        pub = self.make_publisher()
        first = pub.publish_record(DiaryRecord(date=D, github="x"))
        second = pub.publish_record(DiaryRecord(date=D, github="y"))
        assert first.backup_path is not None and second.backup_path is not None
        self.assertEqual(first.backup_path.name, "2030-01-02.20300103T040506Z.md")
        self.assertEqual(second.backup_path.name, "2030-01-02.20300103T040506Z-1.md")
        self.assertIn('Github: "x"', second.backup_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
