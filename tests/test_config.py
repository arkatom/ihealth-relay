"""``ihealth.config`` の回帰テスト.

Phase 1 A3-md で ``MARKDOWN_OUTPUT_DIR`` を ``Config.markdown_output_dir`` に
読み込む経路を追加したので、その単体テスト + ``HEALTH_EXPORT_DIR`` 系の挙動を
カバーする (``_parse_optional_dir_path`` 共通化の回帰防止).
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from ihealth.config import Config, ConfigError, load_config


def _write_env(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _minimum_env(
    health_export_dir: Path,
    *,
    extra: str = "",
) -> str:
    return (
        f'NOTION_SECRET=secret_token\n'
        f'DATABASE_ID=db_id\n'
        f'HEALTH_EXPORT_DIR={health_export_dir}\n'
        f'BIRTH_DATE=1990-01-01\n'
        f'{extra}'
    )


class TestLoadConfigBasics(unittest.TestCase):
    """既存挙動の回帰防止."""

    def test_loads_required_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(env_path, _minimum_env(health))
            cfg = load_config(env_path)
        self.assertEqual(cfg.notion_secret, "secret_token")
        self.assertEqual(cfg.database_id, "db_id")
        self.assertEqual(cfg.birth_date, date(1990, 1, 1))
        self.assertIsNone(cfg.markdown_output_dir)  # 既定 None

    def test_missing_env_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError):
                load_config(Path(tmp) / "nonexistent.env")

    def test_missing_required_key_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                'NOTION_SECRET=t\nDATABASE_ID=d\nBIRTH_DATE=1990-01-01\n',
            )  # HEALTH_EXPORT_DIR 欠落
            with self.assertRaises(ConfigError) as ctx:
                load_config(env_path)
            self.assertIn("HEALTH_EXPORT_DIR", str(ctx.exception))


class TestRequireNotionFlag(unittest.TestCase):
    """Phase 1 A3-md: ``--publisher markdown`` で Notion キー不要 (codex blocking #2)."""

    def test_default_requires_notion(self):
        # 既定 require_notion=True で NOTION_SECRET 欠落 → ConfigError
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                f"DATABASE_ID=db\nHEALTH_EXPORT_DIR={health}\nBIRTH_DATE=1990-01-01\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(env_path)
            self.assertIn("NOTION_SECRET", str(ctx.exception))

    def test_require_notion_false_allows_missing_secret(self):
        # require_notion=False なら NOTION_SECRET 欠落でも通る
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                f"HEALTH_EXPORT_DIR={health}\nBIRTH_DATE=1990-01-01\n",
            )
            cfg = load_config(env_path, require_notion=False)
            self.assertIsNone(cfg.notion_secret)
            self.assertIsNone(cfg.database_id)

    def test_require_notion_false_still_requires_health_dir(self):
        # require_notion=False でも HEALTH_EXPORT_DIR は必須
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_path = tmp_path / ".env"
            _write_env(env_path, "BIRTH_DATE=1990-01-01\n")
            with self.assertRaises(ConfigError) as ctx:
                load_config(env_path, require_notion=False)
            self.assertIn("HEALTH_EXPORT_DIR", str(ctx.exception))

    def test_require_notion_false_still_requires_birth_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(env_path, f"HEALTH_EXPORT_DIR={health}\n")
            with self.assertRaises(ConfigError) as ctx:
                load_config(env_path, require_notion=False)
            self.assertIn("BIRTH_DATE", str(ctx.exception))

    def test_require_notion_false_keeps_secrets_when_present(self):
        # require_notion=False でも .env に値があれば読み込む
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                _minimum_env(health),
            )
            cfg = load_config(env_path, require_notion=False)
            self.assertEqual(cfg.notion_secret, "secret_token")
            self.assertEqual(cfg.database_id, "db_id")


class TestMarkdownOutputDir(unittest.TestCase):
    """Phase 1 A3-md: ``MARKDOWN_OUTPUT_DIR`` の optional 読み込み."""

    def test_unset_yields_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(env_path, _minimum_env(health))
            cfg = load_config(env_path)
        self.assertIsNone(cfg.markdown_output_dir)

    def test_empty_string_yields_none(self):
        # 空文字列は "未指定" 扱い (KEY= だけ書いてある場合)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                _minimum_env(health, extra="MARKDOWN_OUTPUT_DIR=\n"),
            )
            cfg = load_config(env_path)
        self.assertIsNone(cfg.markdown_output_dir)

    def test_absolute_path_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            md_dir = tmp_path / "obsidian"
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                _minimum_env(
                    health, extra=f"MARKDOWN_OUTPUT_DIR={md_dir}\n",
                ),
            )
            cfg = load_config(env_path)
        self.assertEqual(cfg.markdown_output_dir, md_dir)

    def test_tilde_slash_expanded(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                _minimum_env(health, extra="MARKDOWN_OUTPUT_DIR=~/obsidian-md\n"),
            )
            cfg = load_config(env_path)
        self.assertEqual(cfg.markdown_output_dir, Path.home() / "obsidian-md")

    def test_relative_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                _minimum_env(health, extra="MARKDOWN_OUTPUT_DIR=./obsidian\n"),
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(env_path)
            self.assertIn("MARKDOWN_OUTPUT_DIR", str(ctx.exception))
            self.assertIn("絶対パス", str(ctx.exception))

    def test_tilde_user_form_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                _minimum_env(
                    health, extra="MARKDOWN_OUTPUT_DIR=~root/obsidian\n",
                ),
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(env_path)
            self.assertIn("MARKDOWN_OUTPUT_DIR", str(ctx.exception))

    def test_existing_file_rejected(self):
        # codex round 3 PR #23 nice-to-have: ディレクトリ限定
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            md_as_file = tmp_path / "actually_a_file.txt"
            md_as_file.write_text("x", encoding="utf-8")
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                _minimum_env(
                    health, extra=f"MARKDOWN_OUTPUT_DIR={md_as_file}\n",
                ),
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(env_path)
            self.assertIn("MARKDOWN_OUTPUT_DIR", str(ctx.exception))
            self.assertIn("ファイル", str(ctx.exception))


class TestSQLiteDbPath(unittest.TestCase):
    """Phase 1 A3-sqlite: ``SQLITE_DB_PATH`` の optional 読み込み."""

    def test_unset_yields_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(env_path, _minimum_env(health))
            cfg = load_config(env_path)
        self.assertIsNone(cfg.sqlite_db_path)

    def test_absolute_path_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            db_path = tmp_path / "health.db"
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                _minimum_env(health, extra=f"SQLITE_DB_PATH={db_path}\n"),
            )
            cfg = load_config(env_path)
        self.assertEqual(cfg.sqlite_db_path, db_path)

    def test_relative_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                _minimum_env(health, extra="SQLITE_DB_PATH=./h.db\n"),
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(env_path)
            self.assertIn("SQLITE_DB_PATH", str(ctx.exception))

    def test_existing_directory_rejected(self):
        # codex round 1 PR #23 指摘: 既存ディレクトリ → ConfigError
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            db_dir = tmp_path / "is_a_dir"
            db_dir.mkdir()
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                _minimum_env(health, extra=f"SQLITE_DB_PATH={db_dir}\n"),
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(env_path)
            self.assertIn("SQLITE_DB_PATH", str(ctx.exception))
            self.assertIn("ディレクトリ", str(ctx.exception))

    def test_nonexistent_path_accepted(self):
        # 初回起動 (DB 新規作成) は path 存在不要
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            db_path = tmp_path / "future" / "h.db"  # 存在しない
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                _minimum_env(health, extra=f"SQLITE_DB_PATH={db_path}\n"),
            )
            cfg = load_config(env_path)
            self.assertEqual(cfg.sqlite_db_path, db_path)


class TestSlackWebhookUrl(unittest.TestCase):
    """Phase 1 A3-slack: ``SLACK_WEBHOOK_URL`` の optional 読み込み."""

    def test_unset_yields_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(env_path, _minimum_env(health))
            cfg = load_config(env_path)
        self.assertIsNone(cfg.slack_webhook_url)

    def test_https_url_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            url = "https://hooks.slack.com/services/T0/B0/secret"
            _write_env(
                env_path,
                _minimum_env(health, extra=f"SLACK_WEBHOOK_URL={url}\n"),
            )
            cfg = load_config(env_path)
        self.assertEqual(cfg.slack_webhook_url, url)

    def test_http_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                _minimum_env(
                    health,
                    extra="SLACK_WEBHOOK_URL=http://hooks.slack.com/services/x/y/z\n",
                ),
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(env_path)
            self.assertIn("SLACK_WEBHOOK_URL", str(ctx.exception))
            self.assertIn("https://", str(ctx.exception))

    def test_arbitrary_host_rejected(self):
        # codex round 1 PR #24: SSRF 防止
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                _minimum_env(
                    health,
                    extra="SLACK_WEBHOOK_URL=https://evil.example.com/services/x/y/z\n",
                ),
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(env_path)
            self.assertIn("SLACK_WEBHOOK_URL", str(ctx.exception))


class TestDiarySettings(unittest.TestCase):
    """Obsidian 日記用の optional キー (SPEC §2-E / v2 §4.6)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.health = self.tmp_path / "AutoSync"
        self.health.mkdir()
        self.env_path = self.tmp_path / ".env"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def load(self, extra: str = "", *, require_notion: bool = False) -> Config:
        _write_env(self.env_path, _minimum_env(self.health, extra=extra))
        return load_config(self.env_path, require_notion=require_notion)

    def assert_error(self, extra: str, *fragments: str) -> None:
        with self.assertRaises(ConfigError) as ctx:
            self.load(extra)
        for fragment in fragments:
            self.assertIn(fragment, str(ctx.exception))

    def test_defaults_keep_existing_behaviour(self):
        from datetime import timedelta, timezone
        cfg = self.load()
        self.assertIsNone(cfg.default_publisher)
        self.assertIsNone(cfg.obsidian_vault_dir)
        self.assertEqual(cfg.obsidian_diary_dir, "日記")
        self.assertIsNone(cfg.obsidian_template)
        self.assertEqual(cfg.effective_create_missing(), "never")
        self.assertEqual(cfg.obsidian_create_min_age_days, 2)
        self.assertEqual(cfg.lookback_days, 7)
        self.assertTrue(cfg.include_today)
        self.assertEqual(cfg.sources, ("health", "weather", "github"))
        self.assertEqual((cfg.jma_prec_no, cfg.jma_block_no), (44, 47662))
        self.assertIsNone(cfg.github_token)
        self.assertIsNone(cfg.github_owners)
        self.assertEqual(cfg.diary_tz, timezone(timedelta(hours=9)))
        self.assertEqual(cfg.network_grace_sec, 60.0)
        self.assertEqual(cfg.logs_dir.name, "logs")
        self.assertEqual(cfg.obsidian_backup_dir(), cfg.archive_root.parent / "obsidian-backup")

    def test_full_settings(self):
        from datetime import timedelta, timezone
        vault = self.tmp_path / "vault"
        vault.mkdir()
        cfg = self.load(
            "IHEALTH_PUBLISHER=Obsidian\n"
            f"OBSIDIAN_VAULT_DIR={vault}\n"
            "OBSIDIAN_DIARY_DIR=Journal/Daily/\n"
            "OBSIDIAN_TEMPLATE=テンプレート/毎日の習慣.md\n"
            "OBSIDIAN_CREATE_MIN_AGE_DAYS=0\n"
            "IHEALTH_LOOKBACK_DAYS=3\n"
            "IHEALTH_INCLUDE_TODAY=no\n"
            "IHEALTH_SOURCES= GitHub , health ,github\n"
            "IHEALTH_TZ_OFFSET=-05:30\n"
            "IHEALTH_NETWORK_GRACE_SEC=0\n"
            "JMA_PREC_NO=62\n"
            "JMA_BLOCK_NO=47772\n"
            "GITHUB_TOKEN=ghp_synthetic\n"
            "GITHUB_OWNERS=octo, demo-org\n"
            "GITHUB_EXCLUDE_REPOS=octo/archive\n"
        )
        self.assertEqual(cfg.default_publisher, "obsidian")
        self.assertEqual(cfg.obsidian_vault_dir, vault)
        self.assertEqual(cfg.obsidian_diary_dir, "Journal/Daily")
        self.assertEqual(cfg.obsidian_template, Path("テンプレート/毎日の習慣.md"))
        self.assertEqual(cfg.effective_create_missing(), "template")
        self.assertEqual(cfg.obsidian_create_min_age_days, 0)
        self.assertEqual(cfg.lookback_days, 3)
        self.assertFalse(cfg.include_today)
        self.assertEqual(cfg.sources, ("health", "github"))
        self.assertEqual(cfg.diary_tz, timezone(-timedelta(hours=5, minutes=30)))
        self.assertEqual(cfg.network_grace_sec, 0.0)
        self.assertEqual((cfg.jma_prec_no, cfg.jma_block_no), (62, 47772))
        self.assertEqual(cfg.github_token, "ghp_synthetic")
        self.assertEqual(cfg.github_owners, "octo, demo-org")
        self.assertEqual(cfg.github_exclude_repos, "octo/archive")

    def test_secrets_not_in_repr(self):
        cfg = self.load(
            "GITHUB_TOKEN=ghp_synthetic_secret\n"
            "SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T0/B0/slacksecret\n"
        )
        text = repr(cfg)
        self.assertNotIn("ghp_synthetic_secret", text)
        self.assertNotIn("slacksecret", text)
        self.assertNotIn("secret_token", text)  # NOTION_SECRET

    def test_template_path_forms(self):
        self.assertEqual(
            self.load("OBSIDIAN_TEMPLATE=~/tpl.md\n").obsidian_template, Path.home() / "tpl.md",
        )
        self.assertEqual(
            self.load("OBSIDIAN_TEMPLATE=/abs/tpl.md\n").obsidian_template, Path("/abs/tpl.md"),
        )
        self.assert_error("OBSIDIAN_TEMPLATE=~root/tpl.md\n", "OBSIDIAN_TEMPLATE")

    def test_create_missing_modes(self):
        cfg = self.load("OBSIDIAN_CREATE_MISSING=minimal\n")
        self.assertEqual(cfg.effective_create_missing(), "minimal")
        cfg = self.load("OBSIDIAN_TEMPLATE=t.md\nOBSIDIAN_CREATE_MISSING=NEVER\n")
        self.assertEqual(cfg.effective_create_missing(), "never")
        self.assert_error("OBSIDIAN_CREATE_MISSING=always\n", "OBSIDIAN_CREATE_MISSING")
        self.assert_error("OBSIDIAN_CREATE_MISSING=template\n", "OBSIDIAN_TEMPLATE")

    def test_invalid_values_rejected(self):
        cases = [
            ("IHEALTH_PUBLISHER=evernote\n", "IHEALTH_PUBLISHER"),
            ("OBSIDIAN_VAULT_DIR=relative/vault\n", "OBSIDIAN_VAULT_DIR"),
            ("OBSIDIAN_DIARY_DIR=/abs/diary\n", "OBSIDIAN_DIARY_DIR"),
            ("OBSIDIAN_DIARY_DIR=../outside\n", "OBSIDIAN_DIARY_DIR"),
            ("OBSIDIAN_DIARY_DIR=日記/../..\n", "OBSIDIAN_DIARY_DIR"),
            ("OBSIDIAN_CREATE_MIN_AGE_DAYS=-1\n", "OBSIDIAN_CREATE_MIN_AGE_DAYS"),
            ("IHEALTH_LOOKBACK_DAYS=seven\n", "IHEALTH_LOOKBACK_DAYS"),
            ("IHEALTH_LOOKBACK_DAYS=-1\n", "IHEALTH_LOOKBACK_DAYS"),
            ("IHEALTH_LOOKBACK_DAYS=367\n", "IHEALTH_LOOKBACK_DAYS"),
            ("IHEALTH_LOOKBACK_DAYS=0\nIHEALTH_INCLUDE_TODAY=false\n", "対象日"),
            ("IHEALTH_INCLUDE_TODAY=maybe\n", "IHEALTH_INCLUDE_TODAY"),
            ("IHEALTH_SOURCES=health,calendar\n", "calendar"),
            ("IHEALTH_SOURCES=,\n", "IHEALTH_SOURCES"),
            ("IHEALTH_TZ_OFFSET=JST\n", "IHEALTH_TZ_OFFSET"),
            ("IHEALTH_TZ_OFFSET=+25:00\n", "IHEALTH_TZ_OFFSET"),
            ("IHEALTH_NETWORK_GRACE_SEC=601\n", "IHEALTH_NETWORK_GRACE_SEC"),
            ("JMA_PREC_NO=0\n", "JMA_PREC_NO"),
            ("JMA_BLOCK_NO=tokyo\n", "JMA_BLOCK_NO"),
        ]
        for extra, fragment in cases:
            with self.subTest(extra=extra):
                self.assert_error(extra, fragment)

    def test_vault_dir_must_not_be_a_file(self):
        file_path = self.tmp_path / "vault.md"
        file_path.write_text("x", encoding="utf-8")
        self.assert_error(f"OBSIDIAN_VAULT_DIR={file_path}\n", "OBSIDIAN_VAULT_DIR")

    def test_tz_offset_forms(self):
        from datetime import timedelta, timezone
        self.assertEqual(self.load("IHEALTH_TZ_OFFSET=Z\n").diary_tz, timezone.utc)
        self.assertEqual(
            self.load("IHEALTH_TZ_OFFSET=+0900\n").diary_tz, timezone(timedelta(hours=9)),
        )

    def test_github_token_not_in_error_messages(self):
        # 他キーの検証エラーでも token は出さない
        with self.assertRaises(ConfigError) as ctx:
            self.load("GITHUB_TOKEN=ghp_synthetic_secret\nJMA_PREC_NO=x\n")
        self.assertNotIn("ghp_synthetic_secret", str(ctx.exception))

    def test_ensure_notion_settings(self):
        from ihealth.config import ensure_notion_settings
        _write_env(
            self.env_path,
            f"HEALTH_EXPORT_DIR={self.health}\nBIRTH_DATE=1990-01-01\nDATABASE_ID=db\n",
        )
        cfg = load_config(self.env_path, require_notion=False)
        with self.assertRaises(ConfigError) as ctx:
            ensure_notion_settings(cfg)
        self.assertIn("NOTION_SECRET", str(ctx.exception))
        self.assertNotIn("DATABASE_ID", str(ctx.exception))
        ensure_notion_settings(self.load())  # 両方あれば何もしない

    def test_parse_sources(self):
        from ihealth.config import parse_sources
        self.assertEqual(parse_sources("weather"), ("weather",))
        self.assertEqual(parse_sources("github,HEALTH"), ("health", "github"))
        with self.assertRaises(ValueError):
            parse_sources(" , ")

    def test_publisher_kinds_shared_with_workflow(self):
        from ihealth import config, workflow
        self.assertIs(workflow.PUBLISHER_KINDS, config.PUBLISHER_KINDS)
        self.assertIn("obsidian", config.PUBLISHER_KINDS)


if __name__ == "__main__":
    unittest.main()
