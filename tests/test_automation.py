"""automation.py (オートメーション JSON の探索・読込) のテスト。

実 iCloud は触らない: 全て tempdir に HAE Documents 風のツリーを作って検証する。
dataless の materialize は ``ihealth.automation._ensure_materialized`` を patch して
呼び出し順序 / 失敗時の挙動だけを確認する。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from ihealth import automation
from ihealth.automation import (
    AUTOMATION_FILE_RE,
    DEFAULT_MAX_DEPTH,
    AutomationError,
    AutomationIndex,
    discover_automation_files,
    load_automation_json,
    validate_automation_payload,
)
from ihealth.source import SourceError


_LOGGER = logging.getLogger("test_automation")
_VALID_PAYLOAD = {"data": {"metrics": [{"name": "step_count", "units": "count", "data": []}]}}


def _write(path: Path, payload: object = None, *, mtime: "float | None" = None,
           text: "str | None" = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if text is None:
        text = json.dumps(_VALID_PAYLOAD if payload is None else payload)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class TestFileNamePattern(unittest.TestCase):
    def test_matches_daily_file(self):
        match = AUTOMATION_FILE_RE.match("HealthAutoExport-2030-01-15.json")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "2030-01-15")

    def test_rejects_conflict_copy_and_other_names(self):
        for name in (
            "HealthAutoExport-2030-01-15 2.json",   # iCloud 競合コピー
            "HealthAutoExport-2030-01-15.json.tmp",
            "HealthAutoExport-20300115.json",
            "healthautoexport-2030-01-15.json",
            "manifest.json",
            "20300115.hae",
        ):
            with self.subTest(name=name):
                self.assertIsNone(AUTOMATION_FILE_RE.match(name))


class TestAutomationIndex(unittest.TestCase):
    def test_empty_index(self):
        index = AutomationIndex()
        self.assertIsNone(index.best(date(2030, 1, 1)))
        self.assertEqual(index.candidates(date(2030, 1, 1)), ())
        self.assertIsNone(index.latest_date())
        self.assertIsNone(index.newest_mtime())
        self.assertEqual(index.dates(), ())

    def test_manual_construction_with_files_only(self):
        # 統合側テストが files だけで組めること (mtimes 無し → newest_mtime None)
        index = AutomationIndex(files={
            date(2030, 1, 2): (Path("/x/b.json"), Path("/y/b.json")),
            date(2030, 1, 1): (Path("/x/a.json"),),
            date(2030, 1, 3): (),
        })
        self.assertEqual(index.best(date(2030, 1, 2)), Path("/x/b.json"))
        self.assertEqual(index.candidates(date(2030, 1, 2)),
                         (Path("/x/b.json"), Path("/y/b.json")))
        # 候補 0 件の日付は dates / latest_date に含めない
        self.assertEqual(index.dates(), (date(2030, 1, 1), date(2030, 1, 2)))
        self.assertEqual(index.latest_date(), date(2030, 1, 2))
        self.assertIsNone(index.newest_mtime())

    def test_newest_mtime(self):
        index = AutomationIndex(mtimes={Path("/a"): 10.0, Path("/b"): 30.0})
        self.assertEqual(index.newest_mtime(), 30.0)
        self.assertEqual(index.errors, ())


class TestDiscover(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "Documents"
        self.root.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_finds_files_in_arbitrary_user_named_folders(self):
        a = _write(self.root / "新しいオートメーション" / "HealthAutoExport-2030-01-01.json")
        b = _write(self.root / "My Export" / "2030" / "HealthAutoExport-2030-01-02.json")
        _write(self.root / "新しいオートメーション" / "manifest.json")
        index = discover_automation_files(self.root)
        self.assertEqual(index.best(date(2030, 1, 1)), a)
        self.assertEqual(index.best(date(2030, 1, 2)), b)
        self.assertEqual(index.dates(), (date(2030, 1, 1), date(2030, 1, 2)))
        self.assertEqual(index.latest_date(), date(2030, 1, 2))
        self.assertEqual(index.errors, ())

    def test_file_directly_under_root_is_found(self):
        a = _write(self.root / "HealthAutoExport-2030-01-01.json")
        self.assertEqual(discover_automation_files(self.root).best(date(2030, 1, 1)), a)

    def test_autosync_dir_is_not_skipped_by_name(self):
        # v2: 名前による除外 (SKIP_DIR_NAMES) は撤廃。隠し dir 以外は全部降りる
        a = _write(self.root / "AutoSync" / "Export" / "HealthAutoExport-2030-01-01.json")
        self.assertEqual(discover_automation_files(self.root).best(date(2030, 1, 1)), a)

    def test_hidden_directories_are_skipped(self):
        _write(self.root / ".Trash" / "HealthAutoExport-2030-01-01.json")
        _write(self.root / "visible" / ".cache" / "HealthAutoExport-2030-01-02.json")
        index = discover_automation_files(self.root)
        self.assertEqual(index.dates(), ())

    def test_max_depth(self):
        # root 直下のディレクトリが深さ 1。DEFAULT_MAX_DEPTH=4 → 深さ 4 のディレクトリまで
        self.assertEqual(DEFAULT_MAX_DEPTH, 4)
        ok = _write(self.root / "d1" / "d2" / "d3" / "d4" / "HealthAutoExport-2030-01-04.json")
        _write(self.root / "d1" / "d2" / "d3" / "d4" / "d5" / "HealthAutoExport-2030-01-05.json")
        index = discover_automation_files(self.root)
        self.assertEqual(index.best(date(2030, 1, 4)), ok)
        self.assertIsNone(index.best(date(2030, 1, 5)))
        shallow = discover_automation_files(self.root, max_depth=1)
        self.assertEqual(shallow.dates(), ())

    def test_invalid_calendar_date_is_ignored(self):
        _write(self.root / "x" / "HealthAutoExport-2030-13-45.json")
        _write(self.root / "x" / "HealthAutoExport-2030-02-30.json")
        self.assertEqual(discover_automation_files(self.root).dates(), ())

    def test_same_date_sorted_by_newest_mtime(self):
        old = _write(self.root / "旧" / "HealthAutoExport-2030-01-01.json", mtime=1_000_000)
        new = _write(self.root / "新" / "HealthAutoExport-2030-01-01.json", mtime=2_000_000)
        index = discover_automation_files(self.root)
        self.assertEqual(index.best(date(2030, 1, 1)), new)
        self.assertEqual(index.candidates(date(2030, 1, 1)), (new, old))
        self.assertEqual(index.newest_mtime(), 2_000_000)

    def test_same_mtime_sorted_by_larger_size_then_path(self):
        small = _write(self.root / "a" / "HealthAutoExport-2030-01-01.json",
                       text='{"data": {}}', mtime=1_000_000)
        large = _write(self.root / "b" / "HealthAutoExport-2030-01-01.json",
                       text='{"data": {"metrics": []}}      ', mtime=1_000_000)
        self.assertEqual(
            discover_automation_files(self.root).candidates(date(2030, 1, 1)),
            (large, small),
        )
        # size も同じなら path 文字列順 (決定的)
        c1 = _write(self.root / "c1" / "HealthAutoExport-2030-01-02.json",
                    text="{}", mtime=1_000_000)
        c2 = _write(self.root / "c2" / "HealthAutoExport-2030-01-02.json",
                    text="{}", mtime=1_000_000)
        self.assertEqual(
            discover_automation_files(self.root).candidates(date(2030, 1, 2)),
            (c1, c2),
        )

    def test_symlinks_are_not_followed(self):
        outside = Path(self._tmp.name) / "outside"
        target = _write(outside / "HealthAutoExport-2030-01-01.json")
        try:
            (self.root / "linkdir").symlink_to(outside, target_is_directory=True)
            (self.root / "HealthAutoExport-2030-01-02.json").symlink_to(target)
        except (OSError, NotImplementedError):  # pragma: no cover — symlink 不可環境
            self.skipTest("symlink を作成できない環境")
        self.assertEqual(discover_automation_files(self.root).dates(), ())

    def test_symlink_loop_terminates(self):
        try:
            (self.root / "loop").symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError):  # pragma: no cover
            self.skipTest("symlink を作成できない環境")
        a = _write(self.root / "x" / "HealthAutoExport-2030-01-01.json")
        self.assertEqual(discover_automation_files(self.root).best(date(2030, 1, 1)), a)

    def test_missing_root_returns_empty_index_with_error(self):
        with self.assertLogs(_LOGGER, level="WARNING") as logs:
            index = discover_automation_files(self.root / "does-not-exist", logger=_LOGGER)
        self.assertEqual(index.dates(), ())
        self.assertIsNone(index.latest_date())
        self.assertIn("ディレクトリを読めません", logs.output[0])
        # 空の索引でも「ファイルが無い」とは区別できるよう理由を残す
        self.assertEqual(len(index.errors), 1)
        self.assertIn("does-not-exist", index.errors[0])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root は権限を無視する")
    def test_unreadable_subdirectory_is_skipped(self):
        ok = _write(self.root / "ok" / "HealthAutoExport-2030-01-01.json")
        locked = self.root / "locked"
        _write(locked / "HealthAutoExport-2030-01-02.json")
        locked.chmod(0)
        try:
            with self.assertLogs(_LOGGER, level="WARNING") as logs:
                index = discover_automation_files(self.root, logger=_LOGGER)
        finally:
            locked.chmod(0o755)
        self.assertEqual(index.best(date(2030, 1, 1)), ok)
        self.assertIsNone(index.best(date(2030, 1, 2)))
        # 読めないフォルダは探索を続けつつ errors に残す (WARNING も出す)
        self.assertEqual(len(index.errors), 1)
        self.assertIn("locked", index.errors[0])
        self.assertIn("locked", logs.output[0])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root は権限を無視する")
    def test_unlistable_root_is_recorded(self):
        _write(self.root / "x" / "HealthAutoExport-2030-01-01.json")
        self.root.chmod(0o311)       # stat / 移動はできるが一覧できない
        try:
            with self.assertLogs(_LOGGER, level="WARNING"):
                index = discover_automation_files(self.root, logger=_LOGGER)
        finally:
            self.root.chmod(0o755)
        self.assertEqual(index.dates(), ())
        self.assertEqual(len(index.errors), 1)

    def test_discovery_does_not_read_file_contents(self):
        # 名前と stat だけで索引を作る (dataless を materialize しない前提)。
        # 中身が壊れていても探索段階では候補に入る (読み込みは load で検証)。
        broken = _write(self.root / "x" / "HealthAutoExport-2030-01-01.json", text="{broken")
        with patch.object(automation, "_ensure_materialized") as materialize:
            index = discover_automation_files(self.root)
        materialize.assert_not_called()
        self.assertEqual(index.best(date(2030, 1, 1)), broken)


class TestValidatePayload(unittest.TestCase):
    def test_valid(self):
        payload = {"data": {"metrics": [], "workouts": []}}
        self.assertIs(validate_automation_payload(payload), payload)
        self.assertEqual(validate_automation_payload({"data": {}}), {"data": {}})

    def test_invalid_structures(self):
        for payload in (
            [],
            "text",
            {},
            {"data": []},
            {"data": {"metrics": {}}},
            {"data": {"metrics": [], "workouts": "x"}},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(AutomationError):
                    validate_automation_payload(payload)


class TestLoadAutomationJson(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_valid_file(self):
        path = _write(self.dir / "HealthAutoExport-2030-01-01.json")
        self.assertEqual(load_automation_json(path, _LOGGER), _VALID_PAYLOAD)

    def test_utf8_bom_is_tolerated(self):
        path = self.dir / "HealthAutoExport-2030-01-01.json"
        path.write_bytes(b"\xef\xbb\xbf" + json.dumps(_VALID_PAYLOAD).encode("utf-8"))
        self.assertEqual(load_automation_json(path, _LOGGER), _VALID_PAYLOAD)

    def test_materialize_is_called_before_reading(self):
        path = _write(self.dir / "HealthAutoExport-2030-01-01.json")
        calls = []

        def fake_materialize(p, log):
            calls.append(p)

        with patch.object(automation, "_ensure_materialized", side_effect=fake_materialize):
            load_automation_json(path, _LOGGER)
        self.assertEqual(calls, [path])

    def test_materialize_failure_becomes_automation_error(self):
        path = _write(self.dir / "HealthAutoExport-2030-01-01.json")
        with patch.object(automation, "_ensure_materialized",
                          side_effect=SourceError("materialize がタイムアウト")):
            with self.assertRaises(AutomationError) as ctx:
                load_automation_json(path, _LOGGER)
        self.assertIn("materialize", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, SourceError)

    def test_broken_json(self):
        path = _write(self.dir / "HealthAutoExport-2030-01-01.json", text='{"data": {')
        with self.assertRaises(AutomationError):
            load_automation_json(path, _LOGGER)

    def test_invalid_utf8(self):
        path = self.dir / "HealthAutoExport-2030-01-01.json"
        path.write_bytes(b'{"data": {"metrics": []}, "x": "\xff\xfe"}')
        with self.assertRaises(AutomationError):
            load_automation_json(path, _LOGGER)

    def test_structure_error(self):
        path = _write(self.dir / "HealthAutoExport-2030-01-01.json", payload=[1, 2, 3])
        with self.assertRaises(AutomationError):
            load_automation_json(path, _LOGGER)

    def test_missing_file(self):
        with self.assertRaises(AutomationError):
            load_automation_json(self.dir / "HealthAutoExport-2030-01-09.json", _LOGGER)

    def test_size_limit(self):
        path = _write(self.dir / "HealthAutoExport-2030-01-01.json")
        with patch.object(automation, "_MAX_JSON_SIZE_BYTES", 10):
            with self.assertRaises(AutomationError) as ctx:
                load_automation_json(path, _LOGGER)
        self.assertIn("サイズ上限", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
