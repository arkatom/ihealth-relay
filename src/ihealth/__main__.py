"""エントリポイント: ``python3 -m ihealth``

起動確認 → 設定読込 → AutoSync 配下の ``.hae`` 解凍 →
``data/health/YYYY-MM-DD/{metric}.json`` にアーカイブ → 14 フィールド集計 →
``--publisher`` で選択した sink (notion / markdown / stdout / sqlite / slack /
dryrun) に転記 → 失敗時 Reminders.app 通知までを実行する。

``--publisher obsidian`` (または ``.env`` の ``IHEALTH_PUBLISHER=obsidian``) は
健康 (HAE オートメーション JSON / AutoSync) + 気象庁の天気 + GitHub 活動を
Obsidian 日記ノートの frontmatter に書く複数日パイプライン (:mod:`ihealth.diary`)。

使い方::

    PYTHONPATH=src /usr/bin/python3 -m ihealth                      # 昨日分
    PYTHONPATH=src /usr/bin/python3 -m ihealth --date 2026-04-22    # 指定日
    PYTHONPATH=src /usr/bin/python3 -m ihealth --dry-run --verbose  # 検証用

    # Obsidian 日記 (既定: 昨日から IHEALTH_LOOKBACK_DAYS 日分 + 今日の暫定値)
    PYTHONPATH=src /usr/bin/python3 -m ihealth --publisher obsidian
    PYTHONPATH=src /usr/bin/python3 -m ihealth --publisher obsidian --dry-run      # 差分ログのみ
    PYTHONPATH=src /usr/bin/python3 -m ihealth --publisher obsidian \
        --from 2025-11-10 --to 2026-10-10 --fill-empty-only --sources health   # backfill

publisher の決定順: ``--publisher`` → ``.env`` の ``IHEALTH_PUBLISHER`` → ``notion``。

終了コード::

    0: 正常終了
    2: ``--date`` / ``--from`` / ``--to`` / ``--sources`` / ``--markdown-out`` /
       ``--sqlite-path`` / ``--slack-webhook`` の形式エラー
    3: ``.env`` / ``config.toml`` の設定不備 (ConfigAppError)
    4: ``HEALTH_EXPORT_DIR`` 非存在 (HealthExportDirError)
    5: ``.hae`` 取得失敗 (SourceAppError)
    6: Notion API エラー / 日記ページ未作成 (NotionAppError)
    7: Markdown 出力失敗 (MarkdownAppError, Phase 1 A3-md)
    8: Stdout 出力失敗 (StdoutAppError, Phase 1 A3-stdout)
    9: SQLite 書き込み失敗 (SQLiteAppError, Phase 1 A3-sqlite)
    10: Slack 通知失敗 (SlackAppError, Phase 1 A3-slack)
    11: Obsidian 日記の書き込み失敗 / 全 source の取得失敗 (ObsidianAppError)
    12: Obsidian 日記の一部 source の取得失敗 (DiaryPartialFailureError)

終了コード 3 以上の失敗はすべて Reminders.app にリマインダーを追加する
(``--dry-run`` 指定時も通知ルートは同じ、ただし通常は dry-run でエラーは起こらない)。
例外: obsidian の ``--dry-run`` はリマインダーを作らない (送るはずの通知はログに残す)。
obsidian の失敗通知は 1 run 1 通に集約し、同じ内容は 1 日 1 回まで。``.env`` の
設定不備 (exit 3) と obsidian の設定不備・想定外エラー (exit 3 / 1) も、同じ内容は
1 日 1 回まで (``logs/.failure_notified``。launchd は 1 日 3 回起動するため)。

launchd 経由 (標準エラーが端末でない) では、標準エラーに出すログを WARNING 以上に絞る
(launchd.log はローテーションされない。全ログは ``logs/run.log``)。``--verbose`` 時は絞らない。

**Issue #16 リファクタ後**: 本モジュールは **引数処理 + 設定読込 +
AppError → 終了コード + Reminders 通知 のマッピング** だけに責務を絞る。
本体パイプラインは :mod:`ihealth.workflow` に分離してテスト容易性を確保した。
"""

from __future__ import annotations

import sys

_MIN_PYTHON = (3, 9)

if sys.version_info < _MIN_PYTHON:
    sys.stderr.write(
        f"Python {_MIN_PYTHON[0]}.{_MIN_PYTHON[1]}+ が必要です "
        f"(現在: {sys.version_info.major}.{sys.version_info.minor})\n"
    )
    sys.exit(2)

import argparse
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ihealth.config import (
    Config,
    ConfigError,
    default_logs_dir,
    ensure_notion_settings,
    load_config,
    parse_sources,
)
from ihealth.diary import (
    DiaryOptions,
    diary_today,
    notify_once_per_day,
    resolve_dates,
    run_diary,
)
from ihealth.errors import AppError, ConfigAppError
from ihealth.logger import configure as configure_logger
from ihealth.notifier import notify_failure
from ihealth.workflow import PUBLISHER_KINDS, build_default_deps, run_pipeline

#: ``--publisher`` の既定値 (``IHEALTH_PUBLISHER`` も無いとき)。
_DEFAULT_PUBLISHER = "notion"


class _StoreExplicit(argparse.Action):
    """値を保存し、明示指定されたことを ``<dest>_explicit`` に記録する.

    ``--publisher`` の既定値は ``notion`` のまま (互換) だが、明示指定が無いときは
    ``.env`` の ``IHEALTH_PUBLISHER`` を優先したいので「指定されたか」を区別する。
    """

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: "str | None" = None,
    ) -> None:
        setattr(namespace, self.dest, values)
        setattr(namespace, f"{self.dest}_explicit", True)


def _parse_args(argv: "list[str] | None" = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ihealth",
        description=(
            "Apple Health データを集計し選択した publisher (Notion / Markdown / "
            "Obsidian 日記 等) に転記する"
        ),
    )
    parser.add_argument(
        "--date", metavar="YYYY-MM-DD",
        help=(
            "対象日付 (未指定なら昨日。obsidian では未指定なら昨日から "
            "IHEALTH_LOOKBACK_DAYS 日分 + 今日)"
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help=(
            "書き込まず、取得・変換結果をログに出すだけ "
            "(obsidian は日記ノートへの差分を表示)"
        ),
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="DEBUG レベルのログを出力",
    )
    parser.add_argument(
        "--publisher",
        choices=PUBLISHER_KINDS,
        default=_DEFAULT_PUBLISHER,
        action=_StoreExplicit,
        help=(
            "出力先 publisher を選択 (未指定なら .env の IHEALTH_PUBLISHER、"
            "それも無ければ notion). obsidian 以外では --dry-run が指定されたら"
            "本選択は無視される."
        ),
    )
    parser.add_argument(
        "--from", dest="from_date", metavar="YYYY-MM-DD", default=None,
        help="(obsidian) 期間の開始日 (--to と組で範囲指定。--to 省略時は昨日まで)",
    )
    parser.add_argument(
        "--to", dest="to_date", metavar="YYYY-MM-DD", default=None,
        help="(obsidian) 期間の終了日 (両端含む)",
    )
    parser.add_argument(
        "--sources", metavar="LIST", default=None,
        help=(
            "(obsidian) 処理する source をカンマ区切りで指定 "
            "(health,weather,github の一部。既定は .env の IHEALTH_SOURCES / 全部)"
        ),
    )
    parser.add_argument(
        "--fill-empty-only", action="store_true",
        help="(obsidian) 既存の非空値を上書きしない (過去分の backfill 用)",
    )
    parser.set_defaults(publisher_explicit=False)
    parser.add_argument(
        "--markdown-out",
        metavar="PATH",
        default=None,
        help=(
            "--publisher markdown のときに使う出力ディレクトリ. "
            "未指定なら .env の MARKDOWN_OUTPUT_DIR を使う."
        ),
    )
    parser.add_argument(
        "--sqlite-path",
        metavar="PATH",
        default=None,
        help=(
            "--publisher sqlite のときに使う DB ファイルパス. "
            "未指定なら .env の SQLITE_DB_PATH を使う."
        ),
    )
    parser.add_argument(
        "--slack-webhook",
        metavar="URL",
        default=None,
        help=(
            "--publisher slack のときに使う Incoming Webhook URL. "
            "未指定なら .env の SLACK_WEBHOOK_URL を使う."
        ),
    )
    return parser.parse_args(argv)


def _resolve_slack_webhook(raw: "str | None") -> "str | None":
    """``--slack-webhook`` の値を ``str`` に変換. 未指定なら ``None``.

    検証は :func:`ihealth.publishers.slack.validate_webhook_url` に委譲して
    config 側と同一基準にする (https / host allow-list / userinfo 禁止 /
    /services/ path / query 禁止). secret token を含むためエラーメッセージ
    に URL 本文は出さない (validator が source_label しか出さない).
    """
    if raw is None:
        return None
    if not raw.strip():
        raise ValueError(
            "--slack-webhook は空文字を指定できません (env 展開ミスの可能性). "
            "オプションを外すか、URL を明示してください"
        )
    from ihealth.publishers.slack import validate_webhook_url
    return validate_webhook_url(raw.strip(), source_label="--slack-webhook")


def _resolve_markdown_out(raw: "str | None") -> "Path | None":
    """``--markdown-out`` の値を ``Path`` (絶対パス) に変換. 未指定なら ``None``.

    既存パスがファイル (= ディレクトリ
    ではない) のときは ``ValueError`` で reject. ``--sqlite-path`` の "ファイル
    限定" と対称な「ディレクトリ限定」契約を CLI / .env の両側で揃える.
    """
    path = _resolve_cli_path(raw, flag_name="--markdown-out")
    if path is not None and path.exists() and not path.is_dir():
        raise ValueError(
            f"--markdown-out はディレクトリの絶対パスを指定してください "
            f"(現在ファイル): {path}"
        )
    return path


def _resolve_sqlite_path(raw: "str | None") -> "Path | None":
    """``--sqlite-path`` の値を ``Path`` (絶対パス) に変換. 未指定なら ``None``.

    既存ディレクトリは ``ValueError``
    で reject. ``Config._parse_optional_file_path`` と CLI 側で fail-fast
    意味論を揃え、SQLite publisher 内部での遅延エラーを防ぐ.
    """
    path = _resolve_cli_path(raw, flag_name="--sqlite-path")
    if path is not None and path.exists() and path.is_dir():
        raise ValueError(
            f"--sqlite-path はファイルの絶対パスを指定してください "
            f"(現在ディレクトリ): {path}"
        )
    return path


def _resolve_cli_path(raw: "str | None", *, flag_name: str) -> "Path | None":
    """``~/`` のみ展開し、絶対パス検証して ``Path`` を返す共通ヘルパー.

    ``Config._parse_optional_path`` と同じ規則を CLI 引数にも適用. 不正値は
    ``ValueError`` で stderr 経由 exit 2 (argparse 標準と同等).

    空文字は ``None`` ではなく
    ``ValueError`` で reject. ``--sqlite-path "$SQLITE_DB_PATH"`` の env 展開
    ミスで空文字が来た場合に ``.env`` 既定値へ silent fallback して誤った
    DB に書く事故を防ぐ (= fail-fast).
    """
    if raw is None:
        return None
    if not raw.strip():
        raise ValueError(
            f"{flag_name} は空文字を指定できません (env 展開ミスの可能性). "
            "オプションを外すか、絶対パスを明示してください"
        )
    raw = raw.strip()
    if raw.startswith("~/"):
        path = Path.home() / raw[2:]
    elif raw.startswith("~"):
        raise ValueError(
            f"{flag_name} は ~/... または絶対パスで指定してください "
            "(~user/... のような user 指定は未対応)"
        )
    else:
        path = Path(raw)
    if not path.is_absolute():
        raise ValueError(f"{flag_name} は絶対パスで指定してください")
    return path


def _resolve_target_date(date_arg: "str | None") -> date:
    """``--date`` 引数を ``datetime.date`` に変換。未指定なら昨日。"""
    if date_arg is None:
        return date.today() - timedelta(days=1)
    try:
        return date.fromisoformat(date_arg)
    except ValueError as exc:
        raise ValueError(
            f"--date の形式が不正です (YYYY-MM-DD): {date_arg!r}"
        ) from exc


def _parse_cli_date(raw: "str | None", *, flag: str) -> "date | None":
    """``--date`` / ``--from`` / ``--to`` を ``date`` に (未指定なら ``None``)。"""
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        raise ValueError(f"{flag} の形式が不正です (YYYY-MM-DD): {raw!r}") from None


def _resolve_publisher(args: argparse.Namespace, config: Config) -> str:
    """publisher の決定: ``--publisher`` → ``IHEALTH_PUBLISHER`` → ``notion``。"""
    if getattr(args, "publisher_explicit", False):
        return str(args.publisher)
    return config.default_publisher or _DEFAULT_PUBLISHER


def _obsidian_only_options_used(args: argparse.Namespace) -> "list[str]":
    """obsidian 専用オプション (``--from`` / ``--to`` / ``--sources`` / ``--fill-empty-only``)
    のうち指定されたもの (他の publisher で指定されたら引数エラーにする)。"""
    used: "list[str]" = []
    if args.from_date is not None:
        used.append("--from")
    if args.to_date is not None:
        used.append("--to")
    if args.sources is not None:
        used.append("--sources")
    if args.fill_empty_only:
        used.append("--fill-empty-only")
    return used


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _notify_config_failure(
    title: str, body: str, *, logs_dir: "Path | None", today: date,
) -> None:
    """設定不備などの通知を、同じ内容なら 1 日 1 回に抑えて送る。

    launchd は 1 日 3 回起動するので、直すまで毎回同じ設定エラーで止まる状況で
    リマインダーが積み上がらないようにする (:func:`ihealth.diary.notify_once_per_day`)。
    通知手段は本モジュールの :func:`notify_failure` (テストで差し替える)。
    """
    notify_once_per_day(
        title, body, logs_dir=logs_dir, today=today,
        logger=logging.getLogger("ihealth"), notifier=notify_failure,
    )


def _quiet_stderr_when_not_tty(logger: logging.Logger, *, verbose: bool) -> None:
    """標準エラーが端末でない (launchd) とき、標準エラーへのログを WARNING 以上に絞る。

    launchd は標準エラーを ``~/Library/Logs/ihealth-relay/launchd.log`` に追記し、
    このファイルはローテーションされない。INFO まで出すと 1 日 3 回の実行で
    ``logs/run.log`` (日次ローテーション) と同じ内容が際限なく溜まるので、
    launchd.log は「起動失敗・警告・エラー」だけにする。``--verbose`` 指定時は絞らない。
    """
    if verbose:
        return
    for handler in logger.handlers:
        if not isinstance(handler, logging.StreamHandler) or isinstance(handler, logging.FileHandler):
            continue
        stream = getattr(handler, "stream", None)
        if stream is not sys.stderr:
            continue
        try:
            interactive = bool(stream.isatty())
        except (AttributeError, ValueError, OSError):
            interactive = False
        if not interactive:
            handler.setLevel(max(handler.level, logging.WARNING))


def _run_obsidian(args: argparse.Namespace, config: Config, log: logging.Logger) -> int:
    """``--publisher obsidian``: 日記パイプライン (:func:`ihealth.diary.run_diary`) を実行する。

    ``--date`` / ``--from`` / ``--to`` / ``--sources`` の不正は exit 2。source の失敗は
    :func:`run_diary` が 1 通の通知と終了コード (11 / 12) に集約する。設定不備 (exit 3) と
    想定外エラー (exit 1) の通知は同じ内容なら 1 日 1 回 (``--dry-run`` は送らずログだけ)。
    日付の指定が無い (launchd の) 実行では、暫定値がまだ確定していない日も処理し直す
    (``DiaryOptions.revisit_unresolved``)。
    """
    today = diary_today(_utc_now, config.diary_tz)
    try:
        dates = resolve_dates(
            today=today,
            date_arg=_parse_cli_date(args.date, flag="--date"),
            from_date=_parse_cli_date(args.from_date, flag="--from"),
            to_date=_parse_cli_date(args.to_date, flag="--to"),
            lookback_days=config.lookback_days,
            include_today=config.include_today,
        )
        sources = parse_sources(args.sources) if args.sources is not None else config.sources
    except ValueError as exc:
        sys.stderr.write(f"引数エラー: {exc}\n")
        return 2

    log.info(
        "ihealth-relay 起動 (dates=%s..%s (%d 日), today=%s, dry_run=%s, publisher=obsidian, "
        "sources=%s, fill_empty_only=%s)",
        dates[0], dates[-1], len(dates), today, args.dry_run, ",".join(sources),
        args.fill_empty_only,
    )
    log.debug(
        "設定: vault=%s diary_dir=%s template=%s health_export_dir=%s",
        config.obsidian_vault_dir, config.obsidian_diary_dir, config.obsidian_template,
        config.health_export_dir,
    )
    default_window = args.date is None and args.from_date is None and args.to_date is None
    options = DiaryOptions(
        dates=dates,
        today=today,
        sources=tuple(sources),
        dry_run=args.dry_run,
        fill_empty_only=args.fill_empty_only,
        # 引数なし (launchd) の実行では、暫定値がまだ確定していない日も処理し直す
        revisit_unresolved=default_window,
    )

    def notify(title: str, body: str) -> None:
        if args.dry_run:
            # dry-run はリマインダーを作らない (本番の実行の通知を抑止しないため)
            log.info("[dry-run] Reminders 通知 (送信しません): %s\n%s", title, body)
            return
        _notify_config_failure(title, body, logs_dir=config.logs_dir, today=today)

    try:
        result = run_diary(config, options, log)
    except AppError as exc:
        log.error("%s", exc.user_message)
        notify(exc.title, exc.body)
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001 - top-level safety net
        log.exception("想定外エラーで日記パイプライン失敗")
        notify(
            "ihealth-relay: 想定外エラー",
            f"logs/run.log を確認してください。\n{type(exc).__name__}: {exc}",
        )
        return 1
    return result.exit_code


def main(argv: "list[str] | None" = None) -> int:
    """エントリポイント本体。終了コードを返す。

    例外フロー:
    - argparse 不正 → argparse が exit 2
    - --date / --from / --to / --sources 不正 → ValueError → sys.stderr + return 2
    - ConfigError → ConfigAppError に変換 → AppError catch 経路
    - HealthExportDirError / SourceAppError / NotionAppError → AppError catch 経路
    - obsidian: source の失敗は :func:`ihealth.diary.run_diary` が通知と終了コード
      (11 / 12) に集約。設定不備 (vault 不在など) は ConfigAppError (3)
    - 想定外の例外 → traceback + return 1 (catch-all)
    """
    args = _parse_args(argv)

    # 設定読込を最初に: ConfigError 時は logger が未初期化なので stderr に書きつつ通知。
    # OSError / PermissionError / UnicodeDecodeError も同じ扱い (.env アクセス失敗 =
    # 設定系の早期エラー) にマップして exit 3 + 通知。
    # publisher は .env の IHEALTH_PUBLISHER でも決まるので、まず Notion 系を必須に
    # せずに読み、notion を使うと決まった時点で NOTION_SECRET / DATABASE_ID を検証する
    # (dry-run / markdown / obsidian では Notion なしで .env が組めるべき).
    # .env を読めないときは Config が無いので、通知の重複抑止は既定の logs/ と
    # システムの日付で行う (同じ設定エラーで 1 日 3 回リマインダーを作らない)
    try:
        config = load_config(require_notion=False)
    except ConfigError as exc:
        sys.stderr.write(f"設定エラー: {exc}\n")
        _notify_config_failure(
            "ihealth-relay: 設定不備",
            f".env の読み込みに失敗: {exc}",
            logs_dir=default_logs_dir(), today=date.today(),
        )
        return ConfigAppError.exit_code
    except (OSError, UnicodeDecodeError) as exc:
        # .env 読めない (permission / encoding 不正 / デバイス I/O エラー) も
        # 設定不備として扱う。logger 未初期化なので stderr + Reminders のみ。
        sys.stderr.write(f"設定エラー (.env アクセス失敗): {exc}\n")
        _notify_config_failure(
            "ihealth-relay: 設定不備 (.env アクセス失敗)",
            f".env の読み込みに失敗: {type(exc).__name__}: {exc}",
            logs_dir=default_logs_dir(), today=date.today(),
        )
        return ConfigAppError.exit_code

    publisher = _resolve_publisher(args, config)
    # 注意: --dry-run のとき build_default_deps は NotionClient を生成しないので
    # 実 secret が欠けていても問題ない (obsidian は自前の dry-run を持つ).
    effective_kind = "dryrun" if args.dry_run and publisher != "obsidian" else publisher
    if effective_kind == "notion":
        try:
            ensure_notion_settings(config)
        except ConfigError as exc:
            sys.stderr.write(f"設定エラー: {exc}\n")
            _notify_config_failure(
                "ihealth-relay: 設定不備",
                f".env の読み込みに失敗: {exc}",
                logs_dir=config.logs_dir, today=date.today(),
            )
            return ConfigAppError.exit_code

    log_level = "debug" if args.verbose else config.log_level
    logger = configure_logger(log_level)
    _quiet_stderr_when_not_tty(logger, verbose=args.verbose)

    if publisher == "obsidian":
        return _run_obsidian(args, config, logger)

    obsidian_only = _obsidian_only_options_used(args)
    if obsidian_only:
        sys.stderr.write(
            f"引数エラー: {', '.join(obsidian_only)} は --publisher obsidian 専用です\n"
        )
        return 2

    try:
        target_date = _resolve_target_date(args.date)
    except ValueError as exc:
        sys.stderr.write(f"引数エラー: {exc}\n")
        return 2

    try:
        markdown_out_override = _resolve_markdown_out(args.markdown_out)
        sqlite_path_override = _resolve_sqlite_path(args.sqlite_path)
        slack_webhook_override = _resolve_slack_webhook(args.slack_webhook)
    except ValueError as exc:
        sys.stderr.write(f"引数エラー: {exc}\n")
        return 2

    logger.info(
        "ihealth-relay 起動 (target=%s, dry_run=%s, publisher=%s)",
        target_date, args.dry_run, publisher,
    )
    masked_db = f"...{config.database_id[-8:]}" if config.database_id else "(unset)"
    logger.debug(
        "設定: database_id=%s, health_export_dir=%s",
        masked_db, config.health_export_dir,
    )

    # build_default_deps は config.toml を読み込んで Publisher を組み立てる際に
    # ConfigAppError を raise しうる.
    # 既知エラー経路として exit code + Reminders 通知に乗せるため try/except 内で呼ぶ.
    try:
        deps = build_default_deps(
            config,
            dry_run=args.dry_run,
            target_date=target_date,
            logger=logger,
            publisher_kind=publisher,
            markdown_output_dir_override=markdown_out_override,
            sqlite_db_path_override=sqlite_path_override,
            slack_webhook_url_override=slack_webhook_override,
        )
        run_pipeline(config, target_date, deps, logger)
    except AppError as exc:
        logger.error("%s", exc.user_message)
        notify_failure(exc.title, exc.body)
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001 - top-level safety net
        # 想定外の例外 (parser 内の assertion 失敗、Publisher の素例外など) も
        # silent に traceback で終わらせず通知する。exit code 1 で運用者に
        # 「既知エラー (3-6) ではない何か」が起きたと区別させる。
        logger.exception("想定外エラーでパイプライン失敗")
        notify_failure(
            "ihealth-relay: 想定外エラー",
            f"logs/run.log を確認してください。\n{type(exc).__name__}: {exc}",
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
