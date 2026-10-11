"""``ihealth.__main__`` の引数処理 / ヘルパーの回帰テスト.

Phase 1 A3-md で ``--publisher`` / ``--markdown-out`` を追加したので
その引数経路を smoke test する.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ihealth.__main__ import (
    _parse_args, _resolve_markdown_out, _resolve_slack_webhook,
    _resolve_sqlite_path, _resolve_target_date, main,
)


class TestResolveMarkdownOut(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(_resolve_markdown_out(None))

    def test_absolute_path_passthrough(self):
        result = _resolve_markdown_out("/tmp/obsidian")
        self.assertEqual(result, Path("/tmp/obsidian"))

    def test_tilde_slash_expanded(self):
        result = _resolve_markdown_out("~/obsidian")
        self.assertEqual(result, Path.home() / "obsidian")

    def test_relative_path_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _resolve_markdown_out("./obsidian")
        self.assertIn("絶対パス", str(ctx.exception))

    def test_tilde_user_form_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _resolve_markdown_out("~root/obsidian")
        self.assertIn("~/", str(ctx.exception))

    def test_blank_string_raises(self):
        # codex round 1 PR #23 指摘: 空文字 / 空白だけは fail-fast
        with self.assertRaises(ValueError) as ctx:
            _resolve_markdown_out("")
        self.assertIn("空文字", str(ctx.exception))
        with self.assertRaises(ValueError):
            _resolve_markdown_out("   ")

    def test_existing_file_rejected(self):
        # codex round 3 PR #23 nice-to-have: --markdown-out はディレクトリ限定
        with tempfile.NamedTemporaryFile(suffix=".txt") as fp:
            with self.assertRaises(ValueError) as ctx:
                _resolve_markdown_out(fp.name)
            self.assertIn("ファイル", str(ctx.exception))


class TestResolveSlackWebhook(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(_resolve_slack_webhook(None))

    def test_hooks_slack_com_passthrough(self):
        url = "https://hooks.slack.com/services/T0/B0/secret"
        self.assertEqual(_resolve_slack_webhook(url), url)

    def test_http_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            _resolve_slack_webhook("http://hooks.slack.com/services/x/y/z")
        self.assertIn("https://", str(ctx.exception))

    def test_arbitrary_host_rejected(self):
        # codex round 1 PR #24: SSRF 防止のため hooks.slack.com 以外は reject
        with self.assertRaises(ValueError) as ctx:
            _resolve_slack_webhook("https://evil.example.com/services/x/y/z")
        self.assertIn("host", str(ctx.exception).lower())

    def test_userinfo_rejected(self):
        with self.assertRaises(ValueError):
            _resolve_slack_webhook(
                "https://user:pass@hooks.slack.com/services/x/y/z"
            )

    def test_path_outside_services_rejected(self):
        with self.assertRaises(ValueError):
            _resolve_slack_webhook("https://hooks.slack.com/api/incoming/x")

    def test_query_rejected(self):
        with self.assertRaises(ValueError):
            _resolve_slack_webhook(
                "https://hooks.slack.com/services/x/y/z?token=secret"
            )

    def test_blank_rejected(self):
        with self.assertRaises(ValueError):
            _resolve_slack_webhook("")
        with self.assertRaises(ValueError):
            _resolve_slack_webhook("  ")

    def test_invalid_scheme_rejected(self):
        with self.assertRaises(ValueError):
            _resolve_slack_webhook("file:///tmp/secret")


class TestResolveSqlitePath(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(_resolve_sqlite_path(None))

    def test_absolute_path_passthrough(self):
        self.assertEqual(_resolve_sqlite_path("/tmp/h.db"), Path("/tmp/h.db"))

    def test_tilde_slash_expanded(self):
        self.assertEqual(_resolve_sqlite_path("~/h.db"), Path.home() / "h.db")

    def test_relative_rejected(self):
        with self.assertRaises(ValueError):
            _resolve_sqlite_path("./h.db")

    def test_tilde_user_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            _resolve_sqlite_path("~root/h.db")
        self.assertIn("--sqlite-path", str(ctx.exception))

    def test_blank_string_rejected(self):
        with self.assertRaises(ValueError):
            _resolve_sqlite_path("")
        with self.assertRaises(ValueError):
            _resolve_sqlite_path("  ")

    def test_existing_directory_rejected(self):
        # codex round 2 PR #23 指摘: CLI 側にも file-path 意味論
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as ctx:
                _resolve_sqlite_path(tmp)
            self.assertIn("ディレクトリ", str(ctx.exception))


class TestResolveTargetDate(unittest.TestCase):
    """既存挙動の回帰防止 (Phase 1 A3-md で touch しない)."""

    def test_iso_date_parsed(self):
        from datetime import date as _date
        self.assertEqual(_resolve_target_date("2026-04-22"), _date(2026, 4, 22))

    def test_invalid_date_raises_value_error(self):
        with self.assertRaises(ValueError):
            _resolve_target_date("not-a-date")


class TestParseArgs(unittest.TestCase):
    """argparse の経路: ``--publisher`` / ``--markdown-out`` の追加."""

    def test_default_publisher_is_notion(self):
        args = _parse_args([])
        self.assertEqual(args.publisher, "notion")

    def test_publisher_markdown(self):
        args = _parse_args(["--publisher", "markdown"])
        self.assertEqual(args.publisher, "markdown")

    def test_publisher_dryrun(self):
        args = _parse_args(["--publisher", "dryrun"])
        self.assertEqual(args.publisher, "dryrun")

    def test_publisher_stdout(self):
        args = _parse_args(["--publisher", "stdout"])
        self.assertEqual(args.publisher, "stdout")

    def test_publisher_sqlite(self):
        args = _parse_args(["--publisher", "sqlite"])
        self.assertEqual(args.publisher, "sqlite")

    def test_publisher_slack(self):
        args = _parse_args(["--publisher", "slack"])
        self.assertEqual(args.publisher, "slack")

    def test_slack_webhook_passthrough(self):
        url = "https://hooks.slack.com/services/T0/B0/x"
        args = _parse_args(["--slack-webhook", url])
        self.assertEqual(args.slack_webhook, url)

    def test_slack_webhook_default_none(self):
        args = _parse_args([])
        self.assertIsNone(args.slack_webhook)

    def test_sqlite_path_passthrough(self):
        args = _parse_args(["--sqlite-path", "/tmp/h.db"])
        self.assertEqual(args.sqlite_path, "/tmp/h.db")

    def test_sqlite_path_default_none(self):
        args = _parse_args([])
        self.assertIsNone(args.sqlite_path)

    def test_publisher_invalid_choice_exits(self):
        # argparse は invalid choice で SystemExit (exit_code=2)
        with self.assertRaises(SystemExit) as ctx:
            _parse_args(["--publisher", "bogus"])
        self.assertEqual(ctx.exception.code, 2)

    def test_markdown_out_passthrough(self):
        args = _parse_args(["--markdown-out", "/tmp/x"])
        self.assertEqual(args.markdown_out, "/tmp/x")

    def test_markdown_out_default_none(self):
        args = _parse_args([])
        self.assertIsNone(args.markdown_out)

    def test_dry_run_and_publisher_coexist(self):
        # CLI レベルでは両方受け入れる (workflow 層で dry_run が勝つ)
        args = _parse_args(["--dry-run", "--publisher", "markdown"])
        self.assertTrue(args.dry_run)
        self.assertEqual(args.publisher, "markdown")


class TestMainEndToEnd(unittest.TestCase):
    """``main(argv)`` の end-to-end smoke test.

    fetcher / parser / publisher を具体的にモックして、 main() の終了コード /
    Reminders 経路までを 1 関数呼び出しで検証する.

    ログはこのテストの StringIO にだけ出す (``configure_logger`` をそのまま呼ぶと
    リポジトリの実 ``logs/run.log`` に WARNING 行を追記してしまうため).
    """

    def setUp(self) -> None:
        import io
        import logging
        from ihealth.logger import configure as _configure

        self.log_stream = io.StringIO()

        def _configure_to_stream(level: str) -> logging.Logger:
            return _configure(level, enable_file_handler=False, stream=self.log_stream)

        logger = logging.getLogger("ihealth")
        saved = (list(logger.handlers), logger.level, logger.propagate)

        def _restore_logger() -> None:
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
            for handler in saved[0]:
                logger.addHandler(handler)
            logger.setLevel(saved[1])
            logger.propagate = saved[2]

        self.addCleanup(_restore_logger)
        patcher = patch("ihealth.__main__.configure_logger", side_effect=_configure_to_stream)
        self.configure_logger = patcher.start()
        self.addCleanup(patcher.stop)

    def _setup_env(self, tmp: Path, *, with_notion: bool = True) -> Path:
        """テスト用の .env と HEALTH_EXPORT_DIR を作って返す.

        ``with_notion=False`` で markdown publisher 専用の最小 .env を組む.
        """
        health = tmp / "AutoSync"
        health.mkdir()
        env_path = tmp / ".env"
        lines = [
            f"HEALTH_EXPORT_DIR={health}",
            "BIRTH_DATE=1990-01-01",
        ]
        if with_notion:
            lines.insert(0, "NOTION_SECRET=t")
            lines.insert(1, "DATABASE_ID=db")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return env_path

    def test_dry_run_without_notion_secret_returns_0(self):
        """``--dry-run`` は NOTION_SECRET 不在でも exit 0 で終わる."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_path = self._setup_env(tmp_path, with_notion=False)

            with patch("ihealth.config._project_root", return_value=tmp_path), \
                 patch("ihealth.workflow.fetch_all_metrics") as mock_fetch, \
                 patch("ihealth.workflow.parse_all") as mock_parse:
                from datetime import date as _date
                from ihealth.models import DailyHealthData
                from ihealth.source import FetchResult
                mock_fetch.return_value = FetchResult(
                    target_date=_date(2026, 4, 22),
                    archive_dir=tmp_path / "archive" / "2026-04-22",
                    archived={}, missing=[],
                )
                mock_parse.return_value = DailyHealthData(
                    date=_date(2026, 4, 22), step_count=100,
                )
                rc = main(["--dry-run", "--date", "2026-04-22"])
            self.assertEqual(rc, 0)
            # ログはテストの StringIO にだけ出る (実 logs/run.log の FileHandler を付けない)
            import logging
            self.configure_logger.assert_called_once()
            handlers = logging.getLogger("ihealth").handlers
            self.assertTrue(handlers)
            self.assertFalse(any(isinstance(h, logging.FileHandler) for h in handlers))
            # 以前は実 logs/run.log に追記されていた WARNING 行が、テストの stream に出ている
            self.assertIn("[WARNING] ihealth", self.log_stream.getvalue())

    def test_markdown_without_notion_secret_writes_file(self):
        """``--publisher markdown`` は NOTION_SECRET 不在でも書き込み完了."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_path = self._setup_env(tmp_path, with_notion=False)
            md_out = tmp_path / "obsidian"

            with patch("ihealth.config._project_root", return_value=tmp_path), \
                 patch("ihealth.workflow.fetch_all_metrics") as mock_fetch, \
                 patch("ihealth.workflow.parse_all") as mock_parse:
                from datetime import date as _date
                from ihealth.models import DailyHealthData
                from ihealth.source import FetchResult
                mock_fetch.return_value = FetchResult(
                    target_date=_date(2026, 4, 22),
                    archive_dir=tmp_path / "archive" / "2026-04-22",
                    archived={}, missing=[],
                )
                mock_parse.return_value = DailyHealthData(
                    date=_date(2026, 4, 22), step_count=12345,
                )
                rc = main([
                    "--publisher", "markdown",
                    "--markdown-out", str(md_out),
                    "--date", "2026-04-22",
                ])
            self.assertEqual(rc, 0)
            written = md_out / "2026-04-22.md"
            self.assertTrue(written.is_file())
            content = written.read_text(encoding="utf-8")
            self.assertIn("step_count: 12345", content)

    def test_markdown_publish_error_returns_exit_7(self):
        """書き込み失敗時は exit code 7 (MarkdownAppError).

        codex round 3 PR #23 で --markdown-out が「ディレクトリ限定」になったので
        既存ファイル指定ではなく、有効なディレクトリを指定 + その中に
        ``2026-04-22.md`` という名前の **ディレクトリ** を作って ``os.replace``
        を失敗させる経路で検証する.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_path = self._setup_env(tmp_path, with_notion=False)
            md_out = tmp_path / "obsidian"
            md_out.mkdir()
            # 出力ファイル名と同じ名前のディレクトリを置く → os.replace 失敗
            (md_out / "2026-04-22.md").mkdir()

            with patch("ihealth.config._project_root", return_value=tmp_path), \
                 patch("ihealth.workflow.fetch_all_metrics") as mock_fetch, \
                 patch("ihealth.workflow.parse_all") as mock_parse:
                from datetime import date as _date
                from ihealth.models import DailyHealthData
                from ihealth.source import FetchResult
                mock_fetch.return_value = FetchResult(
                    target_date=_date(2026, 4, 22),
                    archive_dir=tmp_path / "archive" / "2026-04-22",
                    archived={}, missing=[],
                )
                mock_parse.return_value = DailyHealthData(
                    date=_date(2026, 4, 22), step_count=100,
                )
                # notify_failure は side-effect なしで返るように mock
                with patch("ihealth.__main__.notify_failure"):
                    rc = main([
                        "--publisher", "markdown",
                        "--markdown-out", str(md_out),
                        "--date", "2026-04-22",
                    ])
            self.assertEqual(rc, 7)

    def test_stdout_publisher_writes_json_to_stdout(self):
        """``--publisher stdout`` で JSON 1 行を stdout に書き、 exit 0."""
        import io
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._setup_env(tmp_path, with_notion=False)

            captured = io.StringIO()
            with patch("ihealth.config._project_root", return_value=tmp_path), \
                 patch("ihealth.workflow.fetch_all_metrics") as mock_fetch, \
                 patch("ihealth.workflow.parse_all") as mock_parse, \
                 patch("sys.stdout", captured):
                from datetime import date as _date
                from ihealth.models import DailyHealthData
                from ihealth.source import FetchResult
                mock_fetch.return_value = FetchResult(
                    target_date=_date(2026, 4, 22),
                    archive_dir=tmp_path / "archive" / "2026-04-22",
                    archived={}, missing=[],
                )
                mock_parse.return_value = DailyHealthData(
                    date=_date(2026, 4, 22), step_count=12345,
                )
                rc = main([
                    "--publisher", "stdout",
                    "--date", "2026-04-22",
                ])
            self.assertEqual(rc, 0)
            output = captured.getvalue()
            # JSON 1 行
            import json as _json
            parsed = _json.loads(output)
            self.assertEqual(parsed["date"], "2026-04-22")
            self.assertEqual(parsed["step_count"], 12345)

    def test_sqlite_publisher_writes_db(self):
        """``--publisher sqlite --sqlite-path`` で DB 作成 + 1 行 insert + exit 0."""
        import sqlite3 as _sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._setup_env(tmp_path, with_notion=False)
            db_path = tmp_path / "h.db"

            with patch("ihealth.config._project_root", return_value=tmp_path), \
                 patch("ihealth.workflow.fetch_all_metrics") as mock_fetch, \
                 patch("ihealth.workflow.parse_all") as mock_parse:
                from datetime import date as _date
                from ihealth.models import DailyHealthData
                from ihealth.source import FetchResult
                mock_fetch.return_value = FetchResult(
                    target_date=_date(2026, 4, 22),
                    archive_dir=tmp_path / "archive" / "2026-04-22",
                    archived={}, missing=[],
                )
                mock_parse.return_value = DailyHealthData(
                    date=_date(2026, 4, 22), step_count=12345,
                )
                rc = main([
                    "--publisher", "sqlite",
                    "--sqlite-path", str(db_path),
                    "--date", "2026-04-22",
                ])
            self.assertEqual(rc, 0)
            self.assertTrue(db_path.is_file())
            with _sqlite3.connect(str(db_path)) as conn:
                row = conn.execute(
                    "SELECT step_count FROM daily_health WHERE date = ?",
                    ("2026-04-22",),
                ).fetchone()
            self.assertEqual(row[0], 12345)

    def test_slack_publisher_posts_message(self):
        """``--publisher slack --slack-webhook`` で webhook に POST + exit 0."""
        from unittest.mock import MagicMock, patch as _patch
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._setup_env(tmp_path, with_notion=False)

            fake_resp = MagicMock()
            fake_resp.__enter__ = MagicMock(return_value=fake_resp)
            fake_resp.__exit__ = MagicMock(return_value=False)
            fake_resp.status = 200
            fake_resp.read = MagicMock(return_value=b"ok")

            with _patch("ihealth.config._project_root", return_value=tmp_path), \
                 _patch("ihealth.workflow.fetch_all_metrics") as mock_fetch, \
                 _patch("ihealth.workflow.parse_all") as mock_parse, \
                 _patch("urllib.request.urlopen", return_value=fake_resp) as mock_open:
                from datetime import date as _date
                from ihealth.models import DailyHealthData
                from ihealth.source import FetchResult
                mock_fetch.return_value = FetchResult(
                    target_date=_date(2026, 4, 22),
                    archive_dir=tmp_path / "archive" / "2026-04-22",
                    archived={}, missing=[],
                )
                mock_parse.return_value = DailyHealthData(
                    date=_date(2026, 4, 22), step_count=12345,
                )
                rc = main([
                    "--publisher", "slack",
                    "--slack-webhook", "https://hooks.slack.com/services/T0/B0/x",
                    "--date", "2026-04-22",
                ])
            self.assertEqual(rc, 0)
            self.assertEqual(mock_open.call_count, 1)

    def test_notion_publisher_missing_secret_returns_exit_3(self):
        """``--publisher notion`` (default) で NOTION_SECRET 欠落 → exit 3."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_path = self._setup_env(tmp_path, with_notion=False)
            with patch("ihealth.config._project_root", return_value=tmp_path), \
                 patch("ihealth.__main__.notify_failure"):
                rc = main(["--date", "2026-04-22"])
            self.assertEqual(rc, 3)


class TestPublisherSelection(unittest.TestCase):
    """publisher の決定順: ``--publisher`` → ``IHEALTH_PUBLISHER`` → notion."""

    def test_publisher_explicit_flag(self):
        self.assertFalse(_parse_args([]).publisher_explicit)
        self.assertTrue(_parse_args(["--publisher", "notion"]).publisher_explicit)

    def test_resolve_publisher_priority(self):
        from datetime import date as _date
        from ihealth.config import Config
        from ihealth.__main__ import _resolve_publisher

        base = Config(
            notion_secret=None, database_id=None, health_export_dir=Path("/x"),
            archive_root=Path("/y"), birth_date=_date(1990, 1, 1),
        )
        with_env = Config(
            notion_secret=None, database_id=None, health_export_dir=Path("/x"),
            archive_root=Path("/y"), birth_date=_date(1990, 1, 1),
            default_publisher="obsidian",
        )
        self.assertEqual(_resolve_publisher(_parse_args([]), base), "notion")
        self.assertEqual(_resolve_publisher(_parse_args([]), with_env), "obsidian")
        self.assertEqual(
            _resolve_publisher(_parse_args(["--publisher", "notion"]), with_env), "notion",
        )
        self.assertEqual(
            _resolve_publisher(_parse_args(["--publisher", "markdown"]), base), "markdown",
        )

    def test_obsidian_flags_parsed(self):
        args = _parse_args([
            "--publisher", "obsidian", "--from", "2025-01-01", "--to", "2025-01-03",
            "--sources", "health,github", "--fill-empty-only",
        ])
        self.assertEqual(args.from_date, "2025-01-01")
        self.assertEqual(args.to_date, "2025-01-03")
        self.assertEqual(args.sources, "health,github")
        self.assertTrue(args.fill_empty_only)


class ObsidianMainTestBase(unittest.TestCase):
    """``main()`` の obsidian 経路の土台 (合成 vault / 実ネットワーク・Reminders なし).

    パッチは tearDown ではなく ``addCleanup`` で外す。unittest は tearDown → cleanup の順に
    走るので、基底が tearDown で外すと、サブクラスが同じ対象に重ねたパッチ
    (``TestObsidianMainWiring`` の ``fetch_weather``) が後から外れて基底のモックを書き戻し、
    後続のテストまで残っていた。cleanup に揃えれば登録の逆順 (LIFO) で外れる。
    """

    def setUp(self) -> None:
        import logging as _logging
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "AutoSync").mkdir()
        self.vault = self.root / "vault"
        (self.vault / "日記").mkdir(parents=True)
        self.note = self.vault / "日記" / "2025-01-14.md"
        self.note_text = "---\n日付: 2025-01-14\n\"歩数 (歩)\":\n---\n本文\n"
        self.note.write_text(self.note_text, encoding="utf-8")
        self.log_stream = __import__("io").StringIO()
        from ihealth.logger import configure as _configure

        def _configure_to_stream(level: str):
            return _configure(level, enable_file_handler=False, stream=self.log_stream)

        self._logging = _logging
        self.patches = [
            patch("ihealth.config._project_root", return_value=self.root),
            patch("ihealth.config_file.find_config_path", return_value=None),
            patch("ihealth.__main__.configure_logger", side_effect=_configure_to_stream),
            patch("ihealth.__main__.notify_failure"),
            patch("ihealth.diary.notify_failure"),
            patch("ihealth.diary.tcp_probe", side_effect=AssertionError("network probe")),
            patch("ihealth.diary.fetch_weather", side_effect=AssertionError("network weather")),
            patch("ihealth.diary.resolve_github_token", side_effect=AssertionError("gh token")),
            patch("sys.stderr", new_callable=__import__("io").StringIO),
            patch("ihealth.diary.time.sleep"),  # 気象庁の 1 req/s 間隔を実時間で待たない
        ]
        # パッチより先に登録した cleanup は、サブクラスのものも含めて全パッチが外れた後に走る
        self.addCleanup(self._assert_fetch_weather_unpatched)
        self.mocks = []
        for p in self.patches:
            self.mocks.append(p.start())
            self.addCleanup(p.stop)
        self.stderr = self.mocks[-2]
        self.main_notify = self.mocks[3]
        self.diary_notify = self.mocks[4]

    def _assert_fetch_weather_unpatched(self) -> None:
        """パッチの外し漏れ (停止順の誤り) を、漏らしたテスト自身の失敗にする。"""
        import ihealth.diary as _diary
        import ihealth.weather as _weather
        self.assertIs(
            _diary.fetch_weather, _weather.fetch_weather,
            "ihealth.diary.fetch_weather がモックのまま残っている (パッチの停止順を確認)",
        )

    def write_env(self, *extra: str) -> None:
        lines = [
            f"HEALTH_EXPORT_DIR={self.root / 'AutoSync'}",
            "BIRTH_DATE=1990-01-01",
            f"OBSIDIAN_VAULT_DIR={self.vault}",
            "IHEALTH_NETWORK_GRACE_SEC=0",
            *extra,
        ]
        (self.root / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def enable_info_logs(self) -> None:
        """tests/__init__.py が止めている INFO ログをこのテストの間だけ通す。"""
        self._logging.disable(self._logging.NOTSET)
        self.addCleanup(self._logging.disable, self._logging.INFO)


class TestObsidianMain(ObsidianMainTestBase):
    """``main()`` の obsidian 経路 (合成 vault / 実ネットワーク・Reminders なし)."""

    def test_dry_run_logs_publisher_and_writes_nothing(self):
        self.write_env("IHEALTH_SOURCES=health")
        self._logging.disable(self._logging.NOTSET)
        try:
            rc = main([
                "--publisher", "obsidian", "--dry-run", "--date", "2025-01-14",
            ])
        finally:
            self._logging.disable(self._logging.INFO)
        self.assertEqual(rc, 0)
        output = self.log_stream.getvalue()
        self.assertIn("publisher=obsidian", output)
        self.assertIn("dry_run=True", output)
        self.assertEqual(self.note.read_text(encoding="utf-8"), self.note_text)
        reports = list((self.root / "logs").glob("diary-report-*.json"))
        self.assertEqual(len(reports), 1)
        self.main_notify.assert_not_called()
        self.diary_notify.assert_not_called()

    def test_ihealth_publisher_env_selects_obsidian(self):
        self.write_env("IHEALTH_PUBLISHER=obsidian", "IHEALTH_SOURCES=health")
        rc = main(["--date", "2025-01-14"])
        self.assertEqual(rc, 0)
        self.assertTrue(list((self.root / "logs").glob("diary-report-*.json")))

    def test_explicit_publisher_overrides_env(self):
        # IHEALTH_PUBLISHER=obsidian でも --publisher notion なら Notion 経路 (secret 無し → 3)
        self.write_env("IHEALTH_PUBLISHER=obsidian")
        rc = main(["--publisher", "notion", "--date", "2025-01-14"])
        self.assertEqual(rc, 3)
        # 日記パイプラインは走っていない (実行レポートが無い)
        self.assertEqual(list((self.root / "logs").glob("diary-report-*.json")), [])

    def test_default_publisher_without_env_is_notion(self):
        self.write_env()
        rc = main(["--date", "2025-01-14"])
        self.assertEqual(rc, 3)  # notion 経路: NOTION_SECRET 不足
        self.main_notify.assert_called_once()

    def test_obsidian_only_flags_rejected_for_other_publishers(self):
        self.write_env()
        for argv in (
            ["--publisher", "stdout", "--from", "2025-01-01"],
            ["--publisher", "stdout", "--sources", "health"],
            ["--publisher", "stdout", "--fill-empty-only"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(main(argv), 2)
        self.assertIn("--publisher obsidian 専用", self.stderr.getvalue())

    def test_argument_errors_exit_2(self):
        self.write_env("IHEALTH_PUBLISHER=obsidian")
        for argv in (
            ["--from", "2025-13-01"],
            ["--to", "2025-01-01"],
            ["--date", "2025-01-01", "--from", "2025-01-01"],
            ["--sources", "health,bogus"],
            ["--sources", ""],
            ["--date", "2999-01-01"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(main(argv), 2)
        self.main_notify.assert_not_called()
        self.assertIn("引数エラー", self.stderr.getvalue())

    def test_missing_vault_setting_exit_3(self):
        (self.root / ".env").write_text(
            f"HEALTH_EXPORT_DIR={self.root / 'AutoSync'}\nBIRTH_DATE=1990-01-01\n"
            "IHEALTH_PUBLISHER=obsidian\nIHEALTH_NETWORK_GRACE_SEC=0\n",
            encoding="utf-8",
        )
        rc = main(["--date", "2025-01-14"])
        self.assertEqual(rc, 3)
        self.main_notify.assert_called_once()
        self.assertIn("vault", self.main_notify.call_args[0][0])

    def test_source_failure_exit_12_with_one_notification(self):
        from ihealth.weather import WeatherError

        self.write_env("IHEALTH_SOURCES=health,weather")
        with patch(
            "ihealth.diary.fetch_weather",
            side_effect=WeatherError("気象庁ページの取得に失敗しました (HTTP 500): synthetic"),
        ):
            rc = main(["--publisher", "obsidian", "--from", "2025-01-13", "--to", "2025-01-14"])
        self.assertEqual(rc, 12)
        self.diary_notify.assert_called_once()
        self.main_notify.assert_not_called()
        title, body = self.diary_notify.call_args[0]
        self.assertIn("weather", title)
        self.assertIn("2025-01-13〜2025-01-14", body)
        self.assertEqual(self.note.read_text(encoding="utf-8"), self.note_text)


def _complete_weather(d, **_kwargs):
    from ihealth.weather import WeatherSummary
    return WeatherSummary(
        date=d,
        weather="3時: 晴れ☀️",
        temperature="朝:平均5.0℃, 昼:平均9.0℃, 夜:平均3.0℃（最高:10.0℃, 最低:1.0℃）",
        humidity="朝:平均60.0%, 昼:平均40.0%, 夜:平均70.0%（最高:80%, 最低:30%）",
        precipitation="朝:0.0mm, 昼:0.0mm, 夜:0.0mm",
        pressure="1-6時:平均1015.0hPa",
        sunshine="6.0時間",
        is_complete=True,
        observed_hours=24,
    )


class TestObsidianMainWiring(ObsidianMainTestBase):
    """CLI の引数・.env が run_diary まで届くか (値のある source で検証する)."""

    def setUp(self) -> None:
        super().setUp()
        self.note_text = '---\n日付: 2025-01-14\n天気: "既存"\n気温:\n---\n本文\n'
        self.note.write_text(self.note_text, encoding="utf-8")
        weather = patch("ihealth.diary.fetch_weather", side_effect=_complete_weather)
        weather.start()
        self.addCleanup(weather.stop)

    def test_cli_dry_run_writes_nothing_even_when_values_exist(self):
        self.write_env("IHEALTH_SOURCES=weather")
        rc = main(["--publisher", "obsidian", "--dry-run", "--date", "2025-01-14"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.note.read_text(encoding="utf-8"), self.note_text)
        self.assertFalse((self.root / "data" / "obsidian-backup").exists())
        report = json.loads(next((self.root / "logs").glob("diary-report-*.json")).read_text(encoding="utf-8"))
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["dates"][0]["note"]["status"], "would_update")

    def test_cli_fill_empty_only_keeps_existing_value(self):
        self.write_env("IHEALTH_SOURCES=weather")
        rc = main(["--publisher", "obsidian", "--fill-empty-only", "--date", "2025-01-14"])
        self.assertEqual(rc, 0)
        text = self.note.read_text(encoding="utf-8")
        self.assertIn('天気: "既存"\n', text)
        self.assertIn('気温: "朝:平均5.0℃', text)

    def test_cli_sources_flag_overrides_env(self):
        self.write_env("IHEALTH_SOURCES=health")
        rc = main(["--publisher", "obsidian", "--sources", "weather", "--date", "2025-01-14"])
        self.assertEqual(rc, 0)
        self.assertIn('気温: "朝:平均5.0℃', self.note.read_text(encoding="utf-8"))

    def test_unwritable_run_report_exit_12_without_touching_notes(self):
        # 実行レポート (全数台帳) を作れない → 書けるはずの天気も書かずに exit 12 + 通知 (W3-5)
        self.write_env("IHEALTH_SOURCES=weather", "IHEALTH_PUBLISHER=obsidian")
        (self.root / "logs").write_text("logs がファイルになっている", encoding="utf-8")
        rc = main(["--date", "2025-01-14"])
        self.assertEqual(rc, 12)
        self.assertEqual(self.note.read_text(encoding="utf-8"), self.note_text)
        self.assertFalse((self.root / "data" / "obsidian-backup").exists())
        self.diary_notify.assert_called_once()
        self.main_notify.assert_not_called()
        title, body = self.diary_notify.call_args[0]
        self.assertIn("実行レポート", title)
        self.assertIn("日記を更新しませんでした", body)
        # 同じ設定で logs が直れば、同じ日付が書かれる (中断で何も失われていない)
        (self.root / "logs").unlink()
        self.assertEqual(main(["--date", "2025-01-14"]), 0)
        self.assertIn('気温: "朝:平均5.0℃', self.note.read_text(encoding="utf-8"))


class TestObsidianMainDefaultWindow(ObsidianMainTestBase):
    """引数なし (launchd) の window: IHEALTH_LOOKBACK_DAYS / INCLUDE_TODAY / TZ_OFFSET."""

    def setUp(self) -> None:
        super().setUp()
        from datetime import datetime as _dt, timezone as _tz
        # 2025-01-15 20:00 UTC = 2025-01-16 05:00 JST = 2025-01-15 10:00 (-10:00)
        clock = patch(
            "ihealth.__main__._utc_now", return_value=_dt(2025, 1, 15, 20, 0, tzinfo=_tz.utc),
        )
        clock.start()
        self.addCleanup(clock.stop)

    def _report(self, *argv: str, env=()) -> dict:
        self.write_env("IHEALTH_SOURCES=health", "IHEALTH_PUBLISHER=obsidian", *env)
        for old in (self.root / "logs").glob("diary-report-*.json"):
            old.unlink()
        self.assertEqual(main(list(argv)), 0)
        report = next((self.root / "logs").glob("diary-report-*.json"))
        return json.loads(report.read_text(encoding="utf-8"))

    def test_lookback_and_include_today(self):
        data = self._report(env=("IHEALTH_LOOKBACK_DAYS=2", "IHEALTH_INCLUDE_TODAY=false"))
        self.assertEqual(
            (data["today"], data["window"]),
            ("2025-01-16", {"from": "2025-01-14", "to": "2025-01-15", "days": 2}),
        )

    def test_tz_offset_decides_today(self):
        data = self._report(env=("IHEALTH_LOOKBACK_DAYS=1", "IHEALTH_TZ_OFFSET=-10:00"))
        self.assertEqual(
            (data["today"], data["window"]),
            ("2025-01-15", {"from": "2025-01-14", "to": "2025-01-15", "days": 2}),
        )

    def test_default_window_revisits_unresolved_provisional_days(self):
        # 暫定値の台帳にある古い日は、引数なしの実行でだけ window に加わる
        state = self.root / "data" / "diary-state.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps(
            {"version": 1, "dates": {"2025-01-05": {"keys": {"歩数 (歩)": 10}}}},
        ), encoding="utf-8")
        data = self._report(env=("IHEALTH_LOOKBACK_DAYS=2",))
        self.assertEqual(data["window"]["from"], "2025-01-05")
        self.assertIn("2025-01-05", [day["date"] for day in data["dates"]])
        # 実行レポートは予定の日付一覧 (window + 台帳から加えた日) を持つ (W3-5)
        self.assertEqual(
            data["planned_dates"], ["2025-01-05", "2025-01-14", "2025-01-15", "2025-01-16"],
        )
        self.assertEqual(data["revisit_dates"], ["2025-01-05"])
        self.assertEqual((data["status"], data["processed"]), ("finished", 4))
        explicit = self._report("--date", "2025-01-14")
        self.assertEqual([day["date"] for day in explicit["dates"]], ["2025-01-14"])
        self.assertEqual(explicit["revisit_dates"], [])


class TestObsidianMainNotifications(ObsidianMainTestBase):
    """設定不備の通知は同じ内容なら 1 日 1 回 / dry-run は通知しない."""

    def test_obsidian_config_error_is_notified_once_per_day(self):
        self.write_env("IHEALTH_PUBLISHER=obsidian", f"OBSIDIAN_DIARY_DIR=無い日記")
        self.assertEqual(main(["--date", "2025-01-14"]), 3)
        self.assertEqual(main(["--date", "2025-01-14"]), 3)
        self.main_notify.assert_called_once()
        self.assertIn("Obsidian", self.main_notify.call_args[0][0])

    def test_env_error_is_notified_once_per_day(self):
        (self.root / ".env").write_text("BIRTH_DATE=1990-01-01\n", encoding="utf-8")
        for _ in range(3):
            self.assertEqual(main(["--date", "2025-01-14"]), 3)
        self.main_notify.assert_called_once()
        self.assertIn("設定不備", self.main_notify.call_args[0][0])

    def test_undelivered_config_notification_is_retried(self):
        self.main_notify.return_value = False   # osascript が失敗した
        self.write_env("IHEALTH_PUBLISHER=obsidian", f"OBSIDIAN_DIARY_DIR=無い日記")
        main(["--date", "2025-01-14"])
        main(["--date", "2025-01-14"])
        self.assertEqual(self.main_notify.call_count, 2)

    def test_dry_run_config_error_is_not_notified(self):
        self.write_env("IHEALTH_PUBLISHER=obsidian", f"OBSIDIAN_DIARY_DIR=無い日記")
        self.assertEqual(main(["--dry-run", "--date", "2025-01-14"]), 3)
        self.main_notify.assert_not_called()
        # 本番の実行は (dry-run に抑止されず) 通知される
        self.assertEqual(main(["--date", "2025-01-14"]), 3)
        self.main_notify.assert_called_once()

    def test_unexpected_error_is_notified_once_per_day(self):
        self.write_env("IHEALTH_PUBLISHER=obsidian", "IHEALTH_SOURCES=health")
        with patch("ihealth.__main__.run_diary", side_effect=RuntimeError("boom")):
            self.assertEqual(main(["--date", "2025-01-14"]), 1)
            self.assertEqual(main(["--date", "2025-01-14"]), 1)
        self.main_notify.assert_called_once()
        self.assertIn("想定外エラー", self.main_notify.call_args[0][0])


class TestLaunchdStderr(ObsidianMainTestBase):
    """launchd (標準エラーが端末でない) では標準エラーに INFO を出さない (launchd.log 肥大防止)."""

    def setUp(self) -> None:
        super().setUp()
        from ihealth.logger import configure as _configure
        self._configure = _configure
        # 標準エラー (= patch 済みの StringIO, 端末ではない) に出す本物の logger 設定
        self.patches[2].stop()
        restore = patch(
            "ihealth.__main__.configure_logger",
            side_effect=lambda level: _configure(level, enable_file_handler=False),
        )
        restore.start()
        self.addCleanup(restore.stop)

    def test_info_is_not_written_to_non_tty_stderr(self):
        # 定時実行 (--dry-run なし) は launchd.log に INFO を出さない
        self.enable_info_logs()
        self.write_env("IHEALTH_SOURCES=health")
        self.assertEqual(main(["--publisher", "obsidian", "--date", "2025-01-14"]), 0)
        output = self.stderr.getvalue()
        self.assertNotIn("ihealth-relay 起動", output)
        self.assertIn("[WARNING]", output, "警告は launchd.log にも出る")

    def test_dry_run_keeps_info_on_non_tty_stderr(self):
        # --dry-run は差分のログが出力そのもの: `> out.txt 2>&1` やパイプ (less / grep) の先にも
        # 起動行と [dry-run] 行を出す
        self.enable_info_logs()
        self.write_env("IHEALTH_SOURCES=weather")
        note_text = '---\n日付: 2025-01-14\n天気: "既存"\n気温:\n---\n本文\n'
        self.note.write_text(note_text, encoding="utf-8")
        with patch("ihealth.diary.fetch_weather", side_effect=_complete_weather):
            rc = main(["--publisher", "obsidian", "--dry-run", "--date", "2025-01-14"])
        self.assertEqual(rc, 0)
        lines = self.stderr.getvalue().splitlines()
        startup = [line for line in lines if "ihealth-relay 起動" in line]
        self.assertEqual(len(startup), 1, lines)
        self.assertIn("dry_run=True, publisher=obsidian", startup[0])
        planned = [line for line in lines if "[dry-run] " in line]
        self.assertTrue(any("気温" in line for line in planned), lines)
        self.assertEqual(self.note.read_text(encoding="utf-8"), note_text)

    def test_dryrun_publisher_keeps_info_on_non_tty_stderr(self):
        # obsidian 以外の --dry-run / --publisher dryrun も、組み立てた properties のログが出力
        from datetime import date as _date
        from ihealth.models import DailyHealthData
        from ihealth.source import FetchResult
        self.enable_info_logs()
        self.write_env()
        fetched = FetchResult(
            target_date=_date(2026, 4, 22), archive_dir=self.root / "archive" / "2026-04-22",
            archived={}, missing=[],
        )
        parsed = DailyHealthData(date=_date(2026, 4, 22), step_count=12345)
        for argv, summary in (
            (["--dry-run"], "dry_run=True, publisher=notion"),
            (["--publisher", "dryrun"], "dry_run=False, publisher=dryrun"),
        ):
            with self.subTest(argv=argv):
                start = len(self.stderr.getvalue())
                with patch("ihealth.workflow.fetch_all_metrics", return_value=fetched), \
                     patch("ihealth.workflow.parse_all", return_value=parsed):
                    self.assertEqual(main([*argv, "--date", "2026-04-22"]), 0)
                output = self.stderr.getvalue()[start:]
                self.assertIn(f"ihealth-relay 起動 (target=2026-04-22, {summary})", output)
                self.assertIn("--dry-run: Notion に送らず", output)

    def test_warnings_still_reach_stderr(self):
        from ihealth.weather import WeatherError
        self.enable_info_logs()
        self.write_env("IHEALTH_SOURCES=weather")
        with patch("ihealth.diary.fetch_weather",
                   side_effect=WeatherError("気象庁ページの取得に失敗しました (HTTP 500): synthetic")):
            self.assertEqual(main(["--publisher", "obsidian", "--date", "2025-01-14"]), 11)
        output = self.stderr.getvalue()
        self.assertIn("HTTP 500", output)
        self.assertNotIn("ihealth-relay 起動", output)

    def test_verbose_keeps_debug_output(self):
        self.enable_info_logs()
        self.write_env("IHEALTH_SOURCES=health")
        self.assertEqual(
            main(["--publisher", "obsidian", "--dry-run", "--verbose", "--date", "2025-01-14"]), 0,
        )
        self.assertIn("ihealth-relay 起動", self.stderr.getvalue())

    def test_tty_stderr_keeps_info(self):
        import io as _io
        import logging as _logging
        from ihealth.__main__ import _quiet_stderr_when_not_tty

        class _Tty(_io.StringIO):
            def isatty(self) -> bool:
                return True

        tty = _Tty()
        with patch("sys.stderr", tty):
            logger = self._configure("info", enable_file_handler=False)
            _quiet_stderr_when_not_tty(logger, verbose=False)
        handler = next(h for h in logger.handlers if isinstance(h, _logging.StreamHandler))
        self.assertEqual(handler.level, _logging.NOTSET)
        pipe = _io.StringIO()
        with patch("sys.stderr", pipe):
            logger = self._configure("info", enable_file_handler=False)
            _quiet_stderr_when_not_tty(logger, verbose=False)
        handler = next(h for h in logger.handlers if isinstance(h, _logging.StreamHandler))
        self.assertEqual(handler.level, _logging.WARNING)


if __name__ == "__main__":
    unittest.main()
