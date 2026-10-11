"""Obsidian 日記パイプライン: 健康 (HAE) + 気象庁の天気 + GitHub 活動を日付ごとに書き込む.

``python3 -m ihealth --publisher obsidian`` (または ``.env`` の
``IHEALTH_PUBLISHER=obsidian``) の本体 (SPEC §2-E / v2 §4.1 / §4.6)。

## 処理の流れ

1. 日記 publisher (:class:`ihealth.publishers.obsidian.ObsidianPublisher`) を組む。
   vault / 日記ディレクトリ不在・キー衝突などは :class:`ConfigAppError` (exit 3)
2. 起動直後 (スリープ復帰直後) は Wi-Fi が未接続のことがあるので、ネットワークに
   繋がるまで最大 ``IHEALTH_NETWORK_GRACE_SEC`` (既定 60) 秒待つ (:class:`NetworkGate`)。
   猶予内に消えた接続エラーは失敗として扱わない (通知しない)
3. GitHub 活動を対象期間まとめて 1 回収集する
4. 日付ごとに、健康 (:class:`ihealth.health_loader.HealthLoader`) と天気
   (:func:`ihealth.weather.fetch_weather`, 1 リクエスト / 秒以下) を **独立に** 取得し、
   :class:`ihealth.records.DiaryRecord` にまとめて日記ノートに書く。1 source の失敗は
   他の source・他の日付に影響しない
5. 実行レポート (日付 × キーごとの action と source の状態) を
   ``logs/diary-report-<UTC>.json`` に保存し、失敗を 1 通の Reminders 通知に集約する

## 何を書くか (完全性ステータス, SPEC v2 §4.1)

各 source の結果は ``complete`` / ``partial`` / ``failed`` / ``nodata`` のいずれか。
publisher には **確実に取れた値だけ** を渡す (渡さないキーは既存値を保持):

- 健康: ``HealthLoadResult.health`` の非 None フィールドと ``activity_detail``。
  ``failed`` は書かない + 通知、``partial`` は取れた値を書いて通知、``nodata``
  (HAE 未出力 / 出力が対象日の途中まで) は静かに見送る。今日の値は暫定値
  (``provisional``) として書き、後の実行で確定値に上書きされる (下記「暫定値の台帳」)
- 天気: 日が完成していればそのキー (欠測キーは書かない)。当日・公開待ち
  (``nodata``) は書かない。確定済みのはずの日の未完成 (``partial`` かつ未完成) は
  書かず、**2 日以上連続** したときだけ失敗として通知する (1 日だけなら気象庁の欠測)
- GitHub: 収集が完全 (``complete``) なときだけ書く。活動が無い日は ``"該当なし"``。
  一部の repo の取得に失敗した (``partial``) / 何も取れない (``failed``) なら書かずに通知。
  今日の分は暫定値 (その時点までの活動)

## 暫定値の台帳 (``data/diary-state.json``)

今日 (まだ終わっていない日) の健康・アクティビティ詳細・GitHub の値は暫定値で、確定値に
置き換わらないまま既定 window (7 日) の外へ出ると、確定値のような顔で残り続ける
(HAE のオートメーションが何日も止まると、その日の再出力は window より後に来る)。
そこで暫定値として書いたキーと値を日付ごとに台帳へ記録し、

- 引数なしの実行 (launchd) では、台帳にある日を ``PROVISIONAL_REVISIT_DAYS`` (30) 日前まで
  既定 window に加えて処理し直す。window の外から加えた日は fill-only で書く (空のキーを
  埋め、暫定値のままのキーを確定値に置き換えるだけ。利用者が書いた値は上書きしない)。
  確定値を書けた (または既存値が確定値と同じ) キーは台帳から消える。利用者が値を
  書き換えたキー (fill-only で保持された) も消す
- ``--fill-empty-only`` でも、台帳のキーで **既存値が台帳の暫定値のまま** のものは
  確定値に置き換える (利用者が書き換えた値は保持)
- 日付から ``PROVISIONAL_OVERDUE_DAYS`` (3) 日を過ぎても確定しない日は通知する
  (1 日 1 回。台帳から外れた (30 日超) 日も最後に 1 回知らせる)
- ``--dry-run`` は台帳を読むだけで書かない

## 終了コード

- 0: 失敗なし (``nodata`` は失敗ではない。ノートが無く作らなかった日 (今日・昨日、
  作成しない設定) も失敗ではない)
- 11 (:class:`ObsidianAppError`): 日記ノートの書き込み失敗、または全日付で全 source が失敗
- 12 (:class:`DiaryPartialFailureError`): 一部の source / 日付で取得に失敗、または
  利用者の対応が要るノート (Templater の未処理構文が残る / テンプレートを使えない)

## 通知

- 失敗は source と日付をまとめて 1 run 1 通知。同じ失敗内容の通知は同じ日には
  1 回だけ (``logs/.failure_notified``)。1 日 3 回の定時実行で同じリマインダーが
  積み上がらないようにするため。内容が変われば (別の失敗) 改めて通知する
- データの鮮度の通知 (1 日 1 回, ``logs/.stale_notified``。終了コードは変えない):
  HAE オートメーションの最新出力日が今日 - 3 日より古い (停止の疑い) / オートメーションの
  出力が 1 件も見つからず健康データを 1 日も取れない (設定不備の疑い) / 暫定値が確定しない
- リマインダーを作れなかった (osascript の失敗・タイムアウト) 通知は「通知済み」と
  記録しない (次の実行で改めて通知する)
- ``--dry-run`` はリマインダーを作らず、送るはずの通知をログとレポートに残すだけ
  (通知済みの記録も書かない。本番の実行の通知を抑止しないため)
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from ihealth import workflow
from ihealth.config import JST, SOURCE_NAMES, Config
from ihealth.errors import AppError, DiaryPartialFailureError, ObsidianAppError
from ihealth.frontmatter import (
    ACTION_INSERT,
    ACTION_KEPT,
    ACTION_UNCHANGED,
    ACTION_UPDATE,
    render_scalar,
)
from ihealth.github_activity import (
    GITHUB_GRAPHQL_ENDPOINT,
    GitHubClient,
    collect_github_activity,
    resolve_github_token,
)
from ihealth.health_loader import HealthLoader, HealthLoadResult
from ihealth.notifier import notify_failure
from ihealth.publishers.obsidian import (
    HEALTH_FIELDS as _OBSIDIAN_HEALTH_FIELDS,
    REASON_TEMPLATE_UNAVAILABLE,
    REASON_UNPROCESSED_TEMPLATE,
    NotePublishOutcome,
    ObsidianPublishError,
)
from ihealth.records import WEATHER_FIELDS, DiaryRecord
from ihealth.weather import JMA_HOURLY_ENDPOINT, WeatherError, WeatherSummary, fetch_weather


SOURCE_HEALTH, SOURCE_WEATHER, SOURCE_GITHUB = SOURCE_NAMES
#: 書き込み失敗を失敗一覧に載せるときの source 名。
SOURCE_OBSIDIAN = "obsidian"

STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_NODATA = "nodata"
#: source を選択していない (``--sources`` / ``IHEALTH_SOURCES``)。
STATUS_SKIPPED = "skipped"

#: レポートのキー単位 action (frontmatter の insert / update / unchanged / kept に加えて)。
KEY_MISSING = "missing"   # source は動いたが値が無い (未出力・欠測・未確定・公開待ち)
KEY_FAILED = "failed"     # source の取得失敗 / 書き込み失敗
KEY_SKIPPED = "skipped"   # source 非選択 / ノートが無く作らなかった / 安全に書けないノート

EXIT_OK = 0

#: オートメーションの最新出力日がこれより古ければ停止を疑う (今日 - 3 日より前)。
STALE_AFTER_DAYS = 3
#: 暫定値を書いた日を、既定 window の外でも処理し直す期間 (日)。これより古い日は台帳から外す。
PROVISIONAL_REVISIT_DAYS = 30
#: 暫定値を書いた日から、この日数を過ぎても確定しなければ通知する。
PROVISIONAL_OVERDUE_DAYS = 3
#: 気象庁へのリクエスト間隔の下限 (秒)。
WEATHER_MIN_INTERVAL_SEC = 1.0
#: 指定できる期間の上限 (日)。年の打ち間違い (0026 年など) で延々と回るのを防ぐ。
MAX_RANGE_DAYS = 3700

#: 接続エラーで source を取り直す回数の上限 (初回を含む)。
_MAX_WEATHER_NETWORK_ATTEMPTS = 4
_MAX_GITHUB_NETWORK_ATTEMPTS = 3
#: ネットワーク待ちの probe 間隔 (秒) と 1 回の接続タイムアウト (秒)。
_PROBE_INTERVAL_SEC = 5.0
_PROBE_TIMEOUT_SEC = 3.0
_ICLOUD_PROBE_HOST = "www.icloud.com"

_REPORT_PREFIX = "diary-report-"
_REPORT_RE = re.compile(r"^diary-report-(\d{8}T\d{6}Z)(?:-\d+)?\.json$")
_REPORT_RETENTION_DAYS = 30
_STALE_MARKER = ".stale_notified"
_FAILURE_MARKER = ".failure_notified"
_LEDGER_VERSION = 1
#: 台帳に記録できる値の型 (frontmatter に書く値 = int / float / str)。
_LEDGER_VALUE_TYPES = (int, float, str)
#: 通知本文に載せる失敗グループ数 / メッセージ長の上限。
_MAX_NOTIFY_GROUPS = 8
_MAX_MESSAGE_LEN = 240

#: 通知本文・レポートでの source の並び。
SOURCE_ORDER: "Mapping[str, int]" = {
    SOURCE_HEALTH: 0, SOURCE_WEATHER: 1, SOURCE_GITHUB: 2, SOURCE_OBSIDIAN: 3,
}

#: :class:`DiaryFailure` の種類。
FAILURE_SOURCE = "source"        # source の取得失敗 (exit 12, 全滅なら 11)
FAILURE_WRITE = "write"          # 日記ノートの書き込み失敗 (exit 11)
FAILURE_ATTENTION = "attention"  # ノートが書ける状態でない・利用者の対応が要る (exit 12)

#: 書かなかった理由のうち、利用者の対応が要るもの (通知する)。
#: too_recent / recently_modified / nothing_to_write / create_disabled は想定内なので通知しない。
_ATTENTION_REASONS: "Mapping[str, str]" = {
    REASON_UNPROCESSED_TEMPLATE: (
        "日記ノートに Templater の未処理構文が残っているため書き込めません "
        "(Obsidian でノートを開いてテンプレートを適用してください)"
    ),
    REASON_TEMPLATE_UNAVAILABLE: (
        "テンプレートを使えないため日記ノートを作成 (初期化) できません "
        "(OBSIDIAN_TEMPLATE のパスと内容を確認してください)"
    ),
}


def _host_of(url: str) -> str:
    return urllib.parse.urlsplit(url).hostname or url


JMA_HOST = _host_of(JMA_HOURLY_ENDPOINT)
GITHUB_HOST = _host_of(GITHUB_GRAPHQL_ENDPOINT)


# ======================================================================
# 期間
# ======================================================================


def diary_today(clock: "Callable[[], datetime]", tz: Any) -> date:
    """日記の基準 tz (``IHEALTH_TZ_OFFSET``) での今日。"""
    now = clock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(tz).date()


def resolve_dates(
    *,
    today: date,
    date_arg: "date | None" = None,
    from_date: "date | None" = None,
    to_date: "date | None" = None,
    lookback_days: int = 7,
    include_today: bool = True,
) -> "tuple[date, ...]":
    """処理する日付 (昇順) を決める (SPEC §2-E / v2 §4.6)。

    - ``--date D``: その 1 日
    - ``--from D1 [--to D2]``: D1..D2 (両端含む)。``--to`` 省略時は昨日まで
    - どちらも無い: 昨日から ``lookback_days`` 日分 (確定値) + 今日 (暫定値,
      ``include_today``)

    Raises:
        ValueError: ``--date`` と ``--from/--to`` の併用 / ``--to`` だけ / 未来の日付 /
            開始 > 終了 / 期間が :data:`MAX_RANGE_DAYS` を超える。
    """
    if date_arg is not None and (from_date is not None or to_date is not None):
        raise ValueError("--date と --from / --to は同時に指定できません")
    if date_arg is not None:
        if date_arg > today:
            raise ValueError(f"未来の日付は指定できません: {date_arg} (今日 {today})")
        return (date_arg,)
    if to_date is not None and from_date is None:
        raise ValueError("--to は --from と一緒に指定してください")
    if from_date is not None:
        end = to_date if to_date is not None else today - timedelta(days=1)
        if end > today:
            raise ValueError(f"未来の日付は指定できません: --to {end} (今日 {today})")
        if from_date > end:
            raise ValueError(f"期間の開始が終了より後です: {from_date} > {end}")
        days = (end - from_date).days + 1
        if days > MAX_RANGE_DAYS:
            raise ValueError(f"期間が長すぎます ({days} 日 > {MAX_RANGE_DAYS} 日)")
        return tuple(from_date + timedelta(days=i) for i in range(days))
    if lookback_days < 0:
        raise ValueError(f"lookback_days は 0 以上: {lookback_days}")
    dates = [today - timedelta(days=i) for i in range(lookback_days, 0, -1)]
    if include_today:
        dates.append(today)
    if not dates:
        raise ValueError("対象日がありません (lookback 0 日かつ今日を含めない設定)")
    return tuple(dates)


# ======================================================================
# ネットワーク待ち
# ======================================================================


def tcp_probe(host: str, *, port: int = 443, timeout: float = _PROBE_TIMEOUT_SEC) -> bool:
    """``host:port`` に TCP 接続できるか (名前解決を含む)。安い疎通確認用。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def is_connection_error(exc: BaseException) -> bool:
    """接続レベルの失敗 (名前解決・接続・タイムアウト・応答途中の切断) か。

    HTTP 応答が返った失敗 (``HTTPError`` = 4xx/5xx) は接続できているので False。
    例外の ``__cause__`` / ``__context__`` をたどって判定する
    (:func:`ihealth.weather.fetch_weather` は元の例外を ``__cause__`` に残す)。
    """
    seen: "set[int]" = set()
    current: "BaseException | None" = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, urllib.error.HTTPError):
            return False
        if isinstance(current, (urllib.error.URLError, OSError, http.client.HTTPException)):
            return True
        if current.__cause__ is not None:
            current = current.__cause__
        elif current.__suppress_context__:
            current = None
        else:
            current = current.__context__
    return False


class NetworkGate:
    """起動直後のネットワーク未接続を、run 全体で合計 ``grace_sec`` 秒まで待つ.

    launchd はスリープ復帰の直後に起動することがあり、その時点では Wi-Fi が
    まだ繋がっていない。接続エラーをすぐ失敗として通知すると、数十秒後には
    消える一過性の失敗でリマインダーが積み上がるので、

    - run の最初に 1 回、接続できるまで待つ (:meth:`wait`)
    - source の取得で接続エラーが出たら、残りの猶予の範囲で待ってから取り直す

    猶予は run 全体で共有 (待機時間と、接続エラーで失敗した取得にかかった時間を消費)。
    ``grace_sec <= 0`` なら待たない (:meth:`wait` は常に True, 取り直しもしない)。
    """

    def __init__(
        self,
        *,
        grace_sec: float,
        logger: logging.Logger,
        probe: "Callable[[str], bool]" = tcp_probe,
        monotonic: "Callable[[], float]" = time.monotonic,
        sleep: "Callable[[float], None]" = time.sleep,
        interval_sec: float = _PROBE_INTERVAL_SEC,
    ) -> None:
        self._grace = max(0.0, float(grace_sec))
        self._logger = logger
        self._probe = probe
        self._monotonic = monotonic
        self._sleep = sleep
        self._interval = max(0.1, float(interval_sec))
        self._spent = 0.0
        self._ready: "set[str]" = set()

    @property
    def enabled(self) -> bool:
        return self._grace > 0

    def remaining(self) -> float:
        """残りの猶予 (秒)。"""
        return max(0.0, self._grace - self._spent)

    def can_retry(self) -> bool:
        """接続エラーの取り直しに使える猶予が残っているか。"""
        return self.enabled and self.remaining() > 0

    def charge(self, seconds: float) -> None:
        """接続エラーで失敗した取得にかかった時間を猶予から差し引く。"""
        self._spent += max(0.0, float(seconds))

    def wait(self, host: str, *, recheck: bool = False) -> bool:
        """``host`` に接続できるまで残りの猶予の範囲で待つ。接続できたら True。

        Args:
            host: 疎通確認する host (443/tcp)
            recheck: 一度接続を確認した host でも probe し直す (接続エラーの後)
        """
        if not self.enabled:
            return True
        if host in self._ready and not recheck:
            return True
        start = self._monotonic()
        attempt = 0
        try:
            while True:
                attempt += 1
                if self._probe(host):
                    self._ready.add(host)
                    if attempt > 1:
                        self._logger.info(
                            "ネットワーク接続を確認しました (%s, %.0f 秒待機)",
                            host, self._monotonic() - start,
                        )
                    return True
                self._ready.discard(host)
                left = self._grace - self._spent - (self._monotonic() - start)
                if left <= 0:
                    self._logger.warning(
                        "ネットワークに接続できません (%s)。待機の上限 %.0f 秒に達しました",
                        host, self._grace,
                    )
                    return False
                if attempt == 1:
                    self._logger.info(
                        "ネットワークに接続できません (%s)。接続を待ちます (残り最大 %.0f 秒)",
                        host, left,
                    )
                self._sleep(min(self._interval, left))
        finally:
            self._spent += max(0.0, self._monotonic() - start)


# ======================================================================
# 結果型
# ======================================================================


@dataclass(frozen=True)
class DiaryOptions:
    """1 回の実行の指定。

    Attributes:
        dates: 処理する日付 (:func:`resolve_dates` の結果)
        today: 日記の基準 tz での今日 (暫定値・公開待ち・停止検知の基準)
        sources: 処理する source (正規順)
        dry_run: True なら日記に一切書かない (差分ログのみ)。レポートは書く。
            通知 (Reminders) も送らず、暫定値の台帳・通知済みの記録も書かない
        fill_empty_only: 既存の非空値を保持する (過去分 backfill 用)。ただし台帳にある
            暫定値のままのキーは確定値に置き換える
        revisit_unresolved: 暫定値の台帳にある日 (:data:`PROVISIONAL_REVISIT_DAYS` 日以内)
            も ``dates`` に加えて処理する (引数なしの既定 window の実行で使う)
    """

    dates: "tuple[date, ...]"
    today: date
    sources: "tuple[str, ...]" = SOURCE_NAMES
    dry_run: bool = False
    fill_empty_only: bool = False
    revisit_unresolved: bool = False


@dataclass(frozen=True)
class DiaryFailure:
    """通知・終了コードの対象になる失敗 1 件。``date`` が None なら期間全体。

    ``kind``: :data:`FAILURE_SOURCE` (取得失敗) / :data:`FAILURE_WRITE` (書き込み失敗) /
    :data:`FAILURE_ATTENTION` (ノートが書ける状態でない)。
    """

    date: "date | None"
    source: str
    message: str
    kind: str = FAILURE_SOURCE


@dataclass(frozen=True)
class DayResult:
    """1 日分の結果 (レポートの ``dates[]`` と同じ内容)。

    Attributes:
        sources: source 名 → 完全性ステータス (``skipped`` = 非選択)
        note_status: :mod:`ihealth.publishers.obsidian` の status。書き込みに失敗したら
            ``"failed"``
        note_reason: status が missing / skipped のときの理由
        written_keys: insert / update した (dry-run なら、する予定の) frontmatter キー
        keys: frontmatter キー → レポート項目 (``action`` / ``old`` / ``new`` / ``reason`` 等)
        provisional: 今日 (まだ終わっていない日) の暫定値を含む
        source_details: source 名 → 詳細 (``status`` / ``errors`` / 各 source 固有の情報)
        backup_path: 書込前バックアップの保存先
        write_error: 書き込み失敗の説明
        provisional_keys: この実行の後も暫定値のまま (台帳で追跡中) のキー
    """

    date: date
    sources: "Mapping[str, str]"
    note_status: str
    note_reason: "str | None"
    note_path: "Path | None"
    written_keys: "tuple[str, ...]"
    keys: "Mapping[str, Mapping[str, Any]]"
    provisional: bool = False
    source_details: "Mapping[str, Mapping[str, Any]]" = field(default_factory=dict)
    backup_path: "Path | None" = None
    write_error: "str | None" = None
    provisional_keys: "tuple[str, ...]" = ()


@dataclass(frozen=True)
class DiaryRunResult:
    """:func:`run_diary` の結果。"""

    exit_code: int
    dates: "tuple[date, ...]"
    days: "tuple[DayResult, ...]"
    failures: "tuple[DiaryFailure, ...]"
    report: "Mapping[str, Any]"
    report_path: "Path | None"
    notifications: "tuple[tuple[str, str], ...]"


# ======================================================================
# 依存性
# ======================================================================


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _urlopen_bytes(request: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (https 固定)
        return response.read()


class _ConnectionTracker:
    """GitHub の opener を包み、接続レベルの失敗の回数を数える (取り直し判定用)。"""

    def __init__(self, inner: "Callable[[urllib.request.Request, float], bytes]") -> None:
        self._inner = inner
        self.connection_errors = 0

    def reset(self) -> None:
        self.connection_errors = 0

    def __call__(self, request: urllib.request.Request, timeout: float) -> bytes:
        try:
            return self._inner(request, timeout)
        except urllib.error.HTTPError:
            raise
        except (OSError, http.client.HTTPException):
            self.connection_errors += 1
            raise


@dataclass
class DiaryDeps:
    """:func:`run_diary` の依存性 (テストで差し替える)。None は既定実装。

    既定実装は呼び出し時に本モジュールの名前を引く (``patch("ihealth.diary.fetch_weather")``
    等で差し替えられる)。
    """

    #: ``(config, logger, now) -> HealthLoader 互換`` (``load(d)`` / ``index()``)
    health_loader_factory: "Callable[..., Any] | None" = None
    #: :func:`ihealth.weather.fetch_weather` 互換
    weather_fetcher: "Callable[..., WeatherSummary] | None" = None
    #: ``(env_token, logger) -> token | None``
    github_token_resolver: "Callable[..., Optional[str]] | None" = None
    #: ``(token, logger, opener) -> GitHubClient 互換``
    github_client_factory: "Callable[..., Any] | None" = None
    #: :func:`ihealth.github_activity.collect_github_activity` 互換
    github_collector: "Callable[..., Any] | None" = None
    #: GitHub の HTTP opener (``(request, timeout) -> bytes``)
    github_opener: "Callable[[urllib.request.Request, float], bytes] | None" = None
    #: ``(config, logger, dry_run, fill_only_empty, clock, sleep) -> ObsidianPublisher 互換``
    publisher_factory: "Callable[..., Any] | None" = None
    #: ``(title, body) -> bool | None``。``False`` を返したら「作れなかった」(通知済みと
    #: 記録せず、次回改めて通知する)。``None`` / ``True`` は送れた扱い
    notifier: "Callable[[str, str], Optional[bool]] | None" = None
    #: ``(host) -> bool``
    network_probe: "Callable[[str], bool] | None" = None
    #: 現在時刻 (aware)。None なら UTC の現在時刻
    clock: "Callable[[], datetime] | None" = None
    #: 経過時間計測。None なら :func:`time.monotonic`
    monotonic: "Callable[[], float] | None" = None
    #: 待機。None なら :func:`time.sleep`
    sleep: "Callable[[float], None] | None" = None

    def resolved(self) -> "DiaryDeps":
        """時計・待機の既定を埋めた複製 (既定は呼び出し時に引くので patch できる)。"""
        return replace(
            self,
            clock=self.clock or _utc_now,
            monotonic=self.monotonic or time.monotonic,
            sleep=self.sleep or time.sleep,
        )


def _default_health_loader(config: Config, logger: logging.Logger, now: "Callable[[], datetime]") -> Any:
    return HealthLoader(config, logger, now=now)


def _default_github_client(token: str, logger: logging.Logger, opener: Any) -> Any:
    return GitHubClient(token, logger=logger, opener=opener)


def _default_publisher(
    config: Config,
    logger: logging.Logger,
    *,
    dry_run: bool,
    fill_only_empty: bool,
    clock: "Callable[[], datetime]",
    sleep: "Callable[[float], None]",
) -> Any:
    return workflow.build_obsidian_publisher(
        config, logger=logger, dry_run=dry_run, fill_only_empty=fill_only_empty,
        clock=clock, sleep=sleep,
    )


# ======================================================================
# source の取得
# ======================================================================


@dataclass
class _SourceState:
    """1 日 × 1 source の結果 (レポート用の情報を含む)。"""

    status: str
    errors: "list[str]" = field(default_factory=list)
    info: "dict[str, Any]" = field(default_factory=dict)

    def as_dict(self) -> "dict[str, Any]":
        item: "dict[str, Any]" = {"status": self.status}
        if self.errors:
            item["errors"] = list(self.errors)
        item.update(self.info)
        return item


def _short(text: object, limit: int = _MAX_MESSAGE_LEN) -> str:
    message = " ".join(str(text).split())
    return message if len(message) <= limit else message[: limit - 1] + "…"


def _field_source(name: str) -> "str | None":
    """publisher の内部名 → source 名。"""
    if name in _OBSIDIAN_HEALTH_FIELDS or name == "activity_detail":
        return SOURCE_HEALTH
    if name in WEATHER_FIELDS:
        return SOURCE_WEATHER
    if name == "github":
        return SOURCE_GITHUB
    return None


class _DiaryRun:
    """:func:`run_diary` の 1 回分の状態。"""

    def __init__(
        self,
        config: Config,
        options: DiaryOptions,
        logger: logging.Logger,
        deps: DiaryDeps,
    ) -> None:
        self.config = config
        self.options = options
        self.logger = logger
        self.deps = deps
        self.today = options.today
        # 気象庁の日付 (JST) での今日: 公開待ち判定と当日 skip に使う
        self.today_jst = diary_today(deps.clock, JST)
        self.failures: "list[DiaryFailure]" = []
        self.gate = NetworkGate(
            grace_sec=config.network_grace_sec,
            logger=logger,
            probe=deps.network_probe or tcp_probe,
            monotonic=deps.monotonic,
            sleep=deps.sleep,
        )
        self._last_weather_request: "float | None" = None
        self.github_summary: "dict[str, Any]" = {"status": STATUS_SKIPPED}
        self.github_activity: Any = None
        self.github_failed_message: "str | None" = None
        self.loader: Any = None
        #: 暫定値の台帳 (日付 → frontmatter キー → 暫定値)。実行開始時に読んだ内容
        self.ledger: "dict[date, dict[str, object]]" = {}
        #: この実行で処理した日の台帳の新しい内容 (空 dict = 確定済み / 追跡終了)
        self.ledger_updates: "dict[date, dict[str, object]]" = {}
        #: 台帳から加えた (既定 window の外の) 日。空のキーを埋めることと、暫定値のままの
        #: キーを確定値に置き換えることだけをする (利用者が書き換えた値は保持)
        self.revisit_only: "frozenset[date]" = frozenset()

    # ---------- 共通 ----------

    def selected(self, source: str) -> bool:
        return source in self.options.sources

    def fail(
        self, d: "date | None", source: str, message: str, *, kind: str = FAILURE_SOURCE,
    ) -> None:
        self.failures.append(DiaryFailure(d, source, _short(message), kind))

    def _local_now(self) -> datetime:
        """HealthLoader 用の現在時刻 (システムのローカル時刻, naive)。"""
        now = self.deps.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone().replace(tzinfo=None)

    def warm_up_network(self) -> None:
        """run の最初に 1 回、ネットワーク接続を待つ (スリープ復帰直後の対策)。"""
        if not self.gate.enabled:
            return
        if self.selected(SOURCE_GITHUB):
            host = GITHUB_HOST
        elif self.selected(SOURCE_WEATHER):
            host = JMA_HOST
        elif self.selected(SOURCE_HEALTH):
            # dataless (iCloud 未ダウンロード) のファイルを materialize するのに通信が要る
            host = _ICLOUD_PROBE_HOST
        else:
            return
        self.gate.wait(host)

    # ---------- GitHub ----------

    def collect_github(self) -> None:
        """対象期間の GitHub 活動を 1 回で収集する (接続エラーは猶予内で取り直す)。"""
        start, end = self.options.dates[0], self.options.dates[-1]
        resolver = self.deps.github_token_resolver or resolve_github_token
        try:
            token = resolver(self.config.github_token, self.logger)
        except Exception as exc:  # noqa: BLE001 — token 解決の想定外失敗も source 失敗に
            self.logger.exception("GitHub token の解決で想定外のエラー")
            token = None
            reason = f"GitHub token の解決に失敗しました ({type(exc).__name__})"
        else:
            reason = (
                "GitHub token を取得できません (.env の GITHUB_TOKEN 未設定 / "
                "gh auth token 失敗。gh auth login を確認してください)"
            )
        if not token:
            self._github_failed(reason, kind="config")
            return

        tracker = _ConnectionTracker(self.deps.github_opener or _urlopen_bytes)
        factory = self.deps.github_client_factory or _default_github_client
        collector = self.deps.github_collector or collect_github_activity
        try:
            client = factory(token, self.logger, tracker)
        except ValueError as exc:
            # token の形式不正など。メッセージに token は含まれない (GitHubClient の契約)
            self._github_failed(f"GitHub クライアントを作れません: {exc}", kind="config")
            return

        attempt = 0
        while True:
            attempt += 1
            tracker.reset()
            started = self.deps.monotonic()
            try:
                activity = collector(
                    client, start, end,
                    tz=self.config.diary_tz,
                    owners=self.config.github_owners,
                    exclude_repos=self.config.github_exclude_repos or (),
                    logger=self.logger,
                )
            except (ValueError, TypeError) as exc:
                # GITHUB_OWNERS / GITHUB_EXCLUDE_REPOS の書式不正 (API を呼ぶ前に検出される)
                self._github_failed(
                    f"GitHub の設定が不正です (GITHUB_OWNERS / GITHUB_EXCLUDE_REPOS): {exc}",
                    kind="config",
                )
                return
            except Exception as exc:  # noqa: BLE001 — 想定外も source 失敗として続行
                self.logger.exception("GitHub 活動の収集で想定外のエラー")
                self._github_failed(f"GitHub 活動の収集で想定外のエラー: {type(exc).__name__}: {exc}")
                return
            status = activity.status
            if (
                status == STATUS_COMPLETE
                or tracker.connection_errors == 0
                or attempt >= _MAX_GITHUB_NETWORK_ATTEMPTS
                or not self.gate.can_retry()
            ):
                break
            self.gate.charge(self.deps.monotonic() - started)
            self.logger.warning(
                "GitHub API への接続エラー (%d 回) のため、ネットワーク復帰を待って取り直します",
                tracker.connection_errors,
            )
            if not self.gate.wait(GITHUB_HOST, recheck=True):
                break

        self.github_activity = activity
        errors = [_short(message) for message in activity.errors]
        self.github_summary = {
            "status": status,
            "errors": errors,
            "repos_scanned": activity.repos_scanned,
            "repos_failed": activity.repos_failed,
            "repos_listed": activity.repos_listed,
            "owners_listed": activity.owners_listed,
            "requests": activity.requests,
            "cost": activity.cost,
            "attempts": attempt,
        }
        if status != STATUS_COMPLETE:
            head = "; ".join(errors[:3]) if errors else "原因不明"
            more = f" ほか {len(errors) - 3} 件" if len(errors) > 3 else ""
            label = "一部の repo を取得できませんでした" if status == STATUS_PARTIAL else "活動を取得できませんでした"
            self.github_failed_message = f"GitHub: {label} ({head}{more})"
            self.fail(None, SOURCE_GITHUB, self.github_failed_message)

    def _github_failed(self, message: str, *, kind: str = "error") -> None:
        self.logger.warning("GitHub: %s", message)
        self.github_activity = None
        self.github_failed_message = message
        self.github_summary = {"status": STATUS_FAILED, "errors": [_short(message)], "kind": kind}
        self.fail(None, SOURCE_GITHUB, message)

    def github_for(self, d: date) -> "tuple[str | None, _SourceState]":
        if not self.selected(SOURCE_GITHUB):
            return None, _SourceState(STATUS_SKIPPED)
        activity = self.github_activity
        if activity is None:
            return None, _SourceState(STATUS_FAILED, [self.github_failed_message or "GitHub 取得失敗"])
        text = activity.text_for(d)
        status = activity.status
        state = _SourceState(status)
        if status != STATUS_COMPLETE:
            state.errors.append(self.github_failed_message or "GitHub 取得が不完全")
        else:
            state.info["items"] = len(activity.items_for(d))
        return text, state

    # ---------- 健康 ----------

    def health_for(self, d: date) -> "tuple[HealthLoadResult | None, _SourceState]":
        if not self.selected(SOURCE_HEALTH):
            return None, _SourceState(STATUS_SKIPPED)
        if self.loader is None:
            factory = self.deps.health_loader_factory or _default_health_loader
            self.loader = factory(self.config, self.logger, self._local_now)
        try:
            result = self.loader.load(d)
        except Exception as exc:  # noqa: BLE001 — load は例外を投げない契約だが念のため
            self.logger.exception("健康データの読み込みで想定外のエラー (date=%s)", d)
            message = f"健康データの読み込みで想定外のエラー: {type(exc).__name__}: {exc}"
            self.fail(d, SOURCE_HEALTH, message)
            return None, _SourceState(STATUS_FAILED, [_short(message)])
        state = _SourceState(result.status, [_short(e) for e in result.errors])
        state.info.update({
            "source": result.source,
            "detail": _short(result.detail, 600),
            "provisional": result.provisional,
            "missing_fields": list(result.missing_fields),
            "withheld_fields": list(result.withheld_fields),
            "supplemented_fields": list(result.supplemented_fields),
        })
        if result.status in (STATUS_FAILED, STATUS_PARTIAL):
            label = "読み込めません" if result.status == STATUS_FAILED else "一部を読み込めません"
            head = "; ".join(state.errors[:2]) if state.errors else result.detail
            self.fail(d, SOURCE_HEALTH, f"健康データを{label}: {head}")
        return result, state

    # ---------- 天気 ----------

    def weather_for(self, d: date) -> "tuple[WeatherSummary | None, _SourceState]":
        if not self.selected(SOURCE_WEATHER):
            return None, _SourceState(STATUS_SKIPPED)
        if d >= self.today_jst:
            # 当日 (と未来) のページは公開されていない (気象庁は前日分まで)
            state = _SourceState(STATUS_NODATA)
            state.info["reason"] = "not_published"
            return None, state
        try:
            summary = self._fetch_weather(d)
        except WeatherError as exc:
            self.logger.warning("天気 %s: 取得に失敗しました: %s", d, exc)
            message = f"天気を取得できません: {exc}"
            self.fail(d, SOURCE_WEATHER, message)
            return None, _SourceState(STATUS_FAILED, [_short(message)])
        except Exception as exc:  # noqa: BLE001 — 1 日の失敗で run を止めない
            self.logger.exception("天気 %s: 想定外のエラー", d)
            message = f"天気の取得で想定外のエラー: {type(exc).__name__}: {exc}"
            self.fail(d, SOURCE_WEATHER, message)
            return None, _SourceState(STATUS_FAILED, [_short(message)])
        state = _SourceState(summary.status)
        state.info.update({
            "is_complete": summary.is_complete,
            "pending": summary.pending,
            "observed_hours": summary.observed_hours,
            "missing_keys": list(summary.missing_keys),
        })
        if not summary.is_complete:
            # nodata (公開待ち) / partial (確定済みのはずの日が未完成): どちらも書かない。
            # partial が 2 日以上続いたら finish() で失敗にする
            return None, state
        return summary, state

    def _fetch_weather(self, d: date) -> WeatherSummary:
        fetcher = self.deps.weather_fetcher or fetch_weather
        attempt = 0
        while True:
            attempt += 1
            self._throttle_weather()
            started = self.deps.monotonic()
            try:
                return fetcher(
                    d,
                    prec_no=self.config.jma_prec_no,
                    block_no=self.config.jma_block_no,
                    logger=self.logger,
                    today=self.today_jst,
                )
            except WeatherError as exc:
                if (
                    not is_connection_error(exc)
                    or attempt >= _MAX_WEATHER_NETWORK_ATTEMPTS
                    or not self.gate.can_retry()
                ):
                    raise
                self.gate.charge(self.deps.monotonic() - started)
                self.logger.warning(
                    "天気 %s: 気象庁に接続できません (%s)。ネットワーク復帰を待って取り直します",
                    d, exc,
                )
                if not self.gate.wait(JMA_HOST, recheck=True):
                    raise
            finally:
                self._last_weather_request = self.deps.monotonic()

    def _throttle_weather(self) -> None:
        """気象庁へのリクエストを 1 秒以上空ける (前身と同じ配慮)。"""
        if self._last_weather_request is None:
            return
        wait = WEATHER_MIN_INTERVAL_SEC - (self.deps.monotonic() - self._last_weather_request)
        if wait > 0:
            self.deps.sleep(wait)


# ======================================================================
# 本体
# ======================================================================


def run_diary(
    config: Config,
    options: DiaryOptions,
    logger: logging.Logger,
    deps: "DiaryDeps | None" = None,
) -> DiaryRunResult:
    """日付ごとに 3 source を独立に取得して日記ノートに書く。

    source の失敗・書き込み失敗では例外を投げず、終了コードと通知に集約する。

    Raises:
        ConfigAppError: vault / 日記ディレクトリ不在・``config.toml`` 不正・キー衝突
            (publisher を組み立てられない = 何も始められない設定不備)。
    """
    deps = (deps or DiaryDeps()).resolved()
    if not options.dates:
        raise ValueError("options.dates が空です")
    ledger = _load_ledger(config.diary_state_path(), logger)
    dates = set(options.dates)
    revisit: "list[date]" = []
    if options.revisit_unresolved:
        revisit = sorted(
            d for d in ledger
            if d not in dates and 0 <= (options.today - d).days <= PROVISIONAL_REVISIT_DAYS
        )
        if revisit:
            logger.info(
                "暫定値がまだ確定していない日も処理します (空のキーを埋め、暫定値のままの"
                "キーを確定値に置き換えるだけ): %s", _format_dates(revisit),
            )
            dates.update(revisit)
    if tuple(sorted(dates)) != tuple(options.dates):
        options = replace(options, dates=tuple(sorted(dates)))
    started_at = deps.clock()
    publisher_factory = deps.publisher_factory or _default_publisher
    publisher = publisher_factory(
        config, logger,
        dry_run=options.dry_run,
        fill_only_empty=options.fill_empty_only,
        clock=deps.clock,
        sleep=deps.sleep,
    )
    run = _DiaryRun(config, options, logger, deps)
    run.ledger = ledger
    run.revisit_only = frozenset(revisit)
    run.warm_up_network()
    if run.selected(SOURCE_GITHUB):
        run.collect_github()

    days = [_process_day(run, publisher, d) for d in options.dates]
    _check_weather_persistence(run, days)
    stale = _check_stale(run, days)
    ledger_info = _commit_ledger(run)
    return _finish(run, days, stale, ledger_info, started_at)


def _process_day(run: _DiaryRun, publisher: Any, d: date) -> DayResult:
    health_result, health_state = run.health_for(d)
    weather, weather_state = run.weather_for(d)
    github_text, github_state = run.github_for(d)

    health = health_result.health if health_result is not None else None
    activity_detail = health_result.activity_detail if health_result is not None else None
    provisional = d >= run.today or bool(
        health_result is not None and health_result.provisional and health is not None
    )
    # キーごとの暫定判定: 健康 (アクティビティ詳細を含む) は対象日がまだ終わっていない間、
    # GitHub は日記の今日の分 (その時点までの活動)。天気は完成した日しか書かない
    provisional_sources: "set[str]" = set()
    if d >= run.today or bool(health_result is not None and health_result.provisional):
        provisional_sources.add(SOURCE_HEALTH)
    if d >= run.today:
        provisional_sources.add(SOURCE_GITHUB)
    tracked = run.ledger.get(d, {})

    note_status = "failed"
    note_reason: "str | None" = None
    note_path: "Path | None" = None
    outcome: "NotePublishOutcome | None" = None
    updates: "dict[str, object]" = {}
    write_error: "str | None" = None
    try:
        record = DiaryRecord(
            date=d, health=health, activity_detail=activity_detail,
            weather=weather, github=github_text,
        )
        updates = publisher.build_updates(record)
        if d in run.revisit_only:
            # 既定 window の外: 空のキーを埋め、以前に書いた暫定値のままのキーを確定値に
            # 置き換えるだけ (window の外の日は自動では更新しないという約束を保つ)
            outcome = publisher.publish_record(
                record, replaceable=tracked, fill_only_empty=True,
            )
        elif run.options.fill_empty_only and tracked:
            # --fill-empty-only でも、以前に書いた暫定値のままのキーは確定値に置き換える
            outcome = publisher.publish_record(record, replaceable=tracked)
        else:
            outcome = publisher.publish_record(record)
    except ObsidianPublishError as exc:
        write_error = str(exc)
        run.logger.error("日記 %s: 書き込みに失敗しました: %s", d, exc)
    except Exception as exc:  # noqa: BLE001 — 1 日の失敗で run を止めない (exit 11 で報告)
        write_error = f"{type(exc).__name__}: {exc}"
        run.logger.exception("日記 %s: 書き込みで想定外のエラー", d)
    if write_error is not None:
        run.fail(d, SOURCE_OBSIDIAN, f"日記ノートに書き込めません: {write_error}", kind=FAILURE_WRITE)
    else:
        assert outcome is not None
        note_status = outcome.status
        note_reason = outcome.reason
        note_path = outcome.path
        attention = _ATTENTION_REASONS.get(note_reason or "")
        if attention is not None and updates:
            run.fail(d, SOURCE_OBSIDIAN, attention, kind=FAILURE_ATTENTION)

    states = {
        SOURCE_HEALTH: health_state,
        SOURCE_WEATHER: weather_state,
        SOURCE_GITHUB: github_state,
    }
    keys = _key_report(
        publisher, updates, outcome, states, health_result, write_error,
    )
    if note_status == "missing":
        # ノートが無い = 暫定値が残っているノートも無い (追跡をやめる)
        entry: "dict[str, object]" = {}
    else:
        entry = _next_ledger_entry(tracked, keys, updates, provisional_sources)
    if entry or tracked:
        run.ledger_updates[d] = entry
    for key, item in keys.items():
        if key in entry:
            item["provisional"] = True
        elif key in tracked:
            item["provisional_resolved"] = True
    written = outcome.written_keys if outcome is not None else ()
    source_statuses = {name: states[name].status for name in SOURCE_NAMES}
    run.logger.info(
        "日記 %s: note=%s%s 書込=%d キー%s / health=%s weather=%s github=%s%s",
        d, note_status, f"({note_reason})" if note_reason else "", len(written),
        f" ({', '.join(written)})" if written else "",
        source_statuses[SOURCE_HEALTH], source_statuses[SOURCE_WEATHER],
        source_statuses[SOURCE_GITHUB], " [暫定]" if provisional else "",
    )
    return DayResult(
        date=d,
        sources=source_statuses,
        note_status=note_status,
        note_reason=note_reason,
        note_path=note_path,
        written_keys=tuple(written),
        keys=keys,
        provisional=provisional,
        source_details={name: states[name].as_dict() for name in SOURCE_NAMES},
        backup_path=outcome.backup_path if outcome is not None else None,
        write_error=write_error,
        provisional_keys=tuple(key for key in keys if key in entry),
    )


def _key_report(
    publisher: Any,
    updates: "Mapping[str, object]",
    outcome: "NotePublishOutcome | None",
    states: "Mapping[str, _SourceState]",
    health_result: "HealthLoadResult | None",
    write_error: "str | None",
) -> "dict[str, dict[str, Any]]":
    """frontmatter キーごとの action (SPEC v2 §4.6 の実行レポート)。"""
    changes = {change.key: change for change in (outcome.changes if outcome is not None else ())}
    withheld = set(health_result.withheld_fields) if health_result is not None else set()
    report: "dict[str, dict[str, Any]]" = {}
    for name, key in publisher.property_map.items():
        source = _field_source(name)
        entry: "dict[str, Any]" = {"field": name, "source": source}
        state = states.get(source) if source is not None else None
        change = changes.get(key)
        if change is not None:
            entry["action"] = change.action
            entry["old"] = change.old
            entry["new"] = change.new
            if change.warning:
                entry["warning"] = change.warning
        elif key in updates:
            # 値はあったが書かなかった (ノートが無い / 安全に書けない / 書き込み失敗)
            entry["new"] = render_scalar(updates[key])
            if write_error is not None:
                entry["action"] = KEY_FAILED
                entry["reason"] = "write_failed"
            else:
                entry["action"] = KEY_SKIPPED
                entry["reason"] = outcome.reason if outcome is not None else None
        elif state is None or state.status == STATUS_SKIPPED:
            entry["action"] = KEY_SKIPPED
            entry["reason"] = "source_not_selected"
        elif state.status == STATUS_FAILED:
            entry["action"] = KEY_FAILED
            entry["reason"] = "source_failed"
        elif source == SOURCE_GITHUB and state.status != STATUS_COMPLETE:
            # 一部の repo の取得に失敗した日は取りこぼしの恐れがあるので書かない
            entry["action"] = KEY_FAILED
            entry["reason"] = "source_incomplete"
        else:
            entry["action"] = KEY_MISSING
            if name in withheld:
                entry["reason"] = "not_final"
            elif state.status == STATUS_NODATA:
                entry["reason"] = state.info.get("reason", "nodata")
            elif source == SOURCE_WEATHER and not state.info.get("is_complete", True):
                entry["reason"] = "incomplete_day"
            else:
                entry["reason"] = "no_value"
        report[key] = entry
    return report


def _check_weather_persistence(run: _DiaryRun, days: "Sequence[DayResult]") -> None:
    """確定済みのはずの日の天気が未完成 (partial) で 2 日以上連続 → 失敗にする。

    1 日だけなら気象庁の欠測でありうるので警告ログ (レポート) だけ。続くなら天気欄の
    形式変更や目視観測の終了の可能性があるので知らせる (weather 担当の申し送り)。
    """
    incomplete: "set[date]" = set()
    for day in days:
        detail = day.source_details.get(SOURCE_WEATHER, {})
        if detail.get("status") == STATUS_PARTIAL and not detail.get("is_complete", True):
            incomplete.add(day.date)
    flagged: "set[date]" = set()
    for d in incomplete:
        if d + timedelta(days=1) in incomplete:
            flagged.add(d)
            flagged.add(d + timedelta(days=1))
    for d in sorted(flagged):
        run.fail(
            d, SOURCE_WEATHER,
            "確定済みのはずの日の天気が 2 日以上続けて未完成です "
            "(気象庁ページの天気欄の形式変更 / 目視観測の終了の可能性)",
        )


def _check_stale(run: _DiaryRun, days: "Sequence[DayResult]" = ()) -> "dict[str, Any] | None":
    """HAE オートメーションの最新出力日が今日 - 3 日より古いか (停止の疑い)。

    オートメーションの出力が 1 件も見つからず、処理したどの日の健康データも取れなかった
    (全日 nodata) ときは「出力が見つからない」(``missing``) として知らせる
    (``HEALTH_EXPORT_DIR`` の誤り・オートメーション未設定のまま健康のキーが黙って
    書かれなくなるのを防ぐ)。AutoSync .hae だけで書けた日がある (オートメーション以前の
    backfill 等) / 健康の取得失敗として通知済みのときは知らせない。
    """
    if not run.selected(SOURCE_HEALTH) or run.loader is None:
        return None
    try:
        index = run.loader.index()
    except Exception:  # noqa: BLE001 — 停止検知は best-effort
        run.logger.exception("オートメーション索引を確認できません (停止検知を省略)")
        return None
    latest = index.latest_date()
    info: "dict[str, Any]" = {
        "latest_date": latest.isoformat() if latest is not None else None,
        "index_errors": len(index.errors),
        "stale": False,
        "missing": False,
    }
    if index.errors:
        # 読めないディレクトリがある = 最新日が不確か。失敗としては各日付で報告済み
        return info
    if latest is None:
        statuses = [day.sources.get(SOURCE_HEALTH) for day in days]
        if statuses and all(status == STATUS_NODATA for status in statuses):
            info["missing"] = True
            info["search_root"] = str(getattr(run.loader, "automation_root", "") or "") or None
            run.logger.warning(
                "HAE オートメーションの出力 (HealthAutoExport-YYYY-MM-DD.json) が見つからず、"
                "健康データを 1 日も取れませんでした。HEALTH_EXPORT_DIR (%s) と iPhone の"
                "オートメーション設定を確認してください",
                run.config.health_export_dir,
            )
        else:
            run.logger.info("HAE オートメーション JSON が見つかりません (停止検知の対象外)")
        return info
    threshold = run.today - timedelta(days=STALE_AFTER_DAYS)
    if latest < threshold:
        info["stale"] = True
        info["days_since"] = (run.today - latest).days
        run.logger.warning(
            "HAE オートメーションの最新出力が %s (%d 日前) です。iPhone 側のオートメーション停止を疑ってください",
            latest, (run.today - latest).days,
        )
    return info


# ======================================================================
# 暫定値の台帳 (data/diary-state.json)
# ======================================================================


def _next_ledger_entry(
    tracked: "Mapping[str, object]",
    keys: "Mapping[str, Mapping[str, Any]]",
    updates: "Mapping[str, object]",
    provisional_sources: "set[str] | frozenset[str]",
) -> "dict[str, object]":
    """この日の処理結果から、台帳に残す (= まだ暫定値の) キーと値を求める。

    - 暫定の source の値を書いた / 既存値が同じだった → 暫定値として記録 (値を更新)
    - 確定した source の値を書いた / 既存値が確定値と同じだった → 台帳から外す
    - ``--fill-empty-only`` で保持された (= 既存値が台帳の暫定値と違う。利用者が
      書き換えた) → 台帳から外す
    - 値が無い / 取得失敗 / 書き込み失敗 → そのまま (次の実行で確定を待つ)
    """
    entry = dict(tracked)
    for key, item in keys.items():
        action = item.get("action")
        if action in (ACTION_INSERT, ACTION_UPDATE, ACTION_UNCHANGED):
            if item.get("source") in provisional_sources and key in updates:
                entry[key] = updates[key]
            else:
                entry.pop(key, None)
        elif action == ACTION_KEPT:
            entry.pop(key, None)
    return entry


def _load_ledger(path: Path, logger: logging.Logger) -> "dict[date, dict[str, object]]":
    """台帳を読む。無い / 壊れている → 空 (壊れていたら warning。次の保存で作り直す)。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("暫定値の台帳を読めません (空として続行): %s (%s)", path, exc)
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        logger.warning("暫定値の台帳が壊れています (作り直します): %s (%s)", path, exc)
        return {}
    entries = data.get("dates") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        logger.warning("暫定値の台帳の形式が不正です (作り直します): %s", path)
        return {}
    ledger: "dict[date, dict[str, object]]" = {}
    for raw_date, item in entries.items():
        try:
            d = date.fromisoformat(str(raw_date))
        except ValueError:
            continue
        values = item.get("keys") if isinstance(item, dict) else None
        if not isinstance(values, dict):
            continue
        kept = {
            key: value for key, value in values.items()
            if isinstance(key, str)
            and isinstance(value, _LEDGER_VALUE_TYPES)
            and not isinstance(value, bool)
        }
        if kept:
            ledger[d] = kept
    return ledger


def _ledger_payload(ledger: "Mapping[date, Mapping[str, object]]", now: datetime) -> str:
    return json.dumps(
        {
            "version": _LEDGER_VERSION,
            "updated_at": now.astimezone(timezone.utc).isoformat(),
            "dates": {
                d.isoformat(): {"keys": dict(ledger[d])} for d in sorted(ledger)
            },
        },
        ensure_ascii=False,
        indent=1,
    )


def _commit_ledger(run: _DiaryRun) -> "dict[str, Any]":
    """この実行の結果を台帳に反映して保存し、確定しないまま古くなった日を返す。

    保存直前に読み直し、この実行で処理した日だけを書き換える (並行して走った実行が
    記録した他の日を消さない)。:data:`PROVISIONAL_REVISIT_DAYS` を超えた日は台帳から外す。
    ``--dry-run`` では保存しない (見込みだけ返す)。保存に失敗したら失敗として報告する
    (暫定値の追跡が黙って止まるのを防ぐ)。
    """
    path = run.config.diary_state_path()
    today = run.today
    if run.options.dry_run:
        current = dict(run.ledger)
    else:
        current = _load_ledger(path, run.logger)
    merged = dict(current)
    for d, entry in run.ledger_updates.items():
        if entry:
            merged[d] = dict(entry)
        else:
            merged.pop(d, None)
    # キーの並びは記録した順 (= 日記テンプレートの順) のまま
    expired = {
        d: list(values) for d, values in merged.items()
        if (today - d).days > PROVISIONAL_REVISIT_DAYS
    }
    for d in expired:
        merged.pop(d, None)
        run.logger.warning(
            "日記 %s の暫定値 (%s) は %d 日以内に確定しなかったため、自動の再処理をやめます。"
            "確定値で上書きするには --date %s を実行してください",
            d, ", ".join(expired[d]), PROVISIONAL_REVISIT_DAYS, d,
        )
    overdue = {
        d: list(values) for d, values in merged.items()
        if (today - d).days > PROVISIONAL_OVERDUE_DAYS
    }
    info: "dict[str, Any]" = {
        "path": str(path),
        "pending": {d.isoformat(): list(values) for d, values in sorted(merged.items())},
        "overdue": {d.isoformat(): keys for d, keys in sorted(overdue.items())},
        "expired": {d.isoformat(): keys for d, keys in sorted(expired.items())},
        "saved": False,
    }
    if run.options.dry_run:
        return info
    if merged == current and (merged or not path.exists()):
        return info
    try:
        _write_text_atomic(path, _ledger_payload(merged, run.deps.clock()))
    except OSError as exc:
        run.logger.error("暫定値の台帳を保存できません: %s (%s)", path, exc)
        info["error"] = _short(f"{type(exc).__name__}: {exc}")
        run.fail(
            None, SOURCE_OBSIDIAN,
            f"暫定値の台帳 ({path.name}) を保存できません。今日の暫定値が確定値に"
            f"置き換わらない恐れがあります: {type(exc).__name__}: {exc}",
        )
        return info
    info["saved"] = True
    return info


# ======================================================================
# 仕上げ: 終了コード / 通知 / レポート
# ======================================================================


def _format_dates(dates: "Sequence[date]") -> str:
    """日付列を ``2026-10-04〜2026-10-06, 2026-10-09`` のように縮める。"""
    ordered = sorted(set(dates))
    if not ordered:
        return "期間全体"
    spans: "list[tuple[date, date]]" = []
    for d in ordered:
        if spans and d == spans[-1][1] + timedelta(days=1):
            spans[-1] = (spans[-1][0], d)
        else:
            spans.append((d, d))
    parts = [
        s.isoformat() if s == e else f"{s.isoformat()}〜{e.isoformat()}" for s, e in spans
    ]
    return ", ".join(parts)


def _failure_body(
    failures: "Sequence[DiaryFailure]", run_dates: "Sequence[date]", report_path: "Path | None",
) -> str:
    groups: "dict[tuple[str, str], list[date]]" = {}
    for failure in failures:
        groups.setdefault((failure.source, failure.message), [])
        if failure.date is not None:
            groups[(failure.source, failure.message)].append(failure.date)
    lines: "list[str]" = []
    for index, ((source, message), dates) in enumerate(sorted(
        groups.items(), key=lambda item: (SOURCE_ORDER.get(item[0][0], 9), item[0][1]),
    )):
        if index >= _MAX_NOTIFY_GROUPS:
            lines.append(f"…ほか {len(groups) - _MAX_NOTIFY_GROUPS} 種類")
            break
        where = _format_dates(dates) if dates else f"{_format_dates(run_dates)} (期間全体)"
        lines.append(f"[{source}] {where}")
        lines.append(f"  {message}")
    lines.append("")
    lines.append("失敗した source のキーは書かず、既存の値を残しています。")
    if report_path is not None:
        lines.append(f"詳細: {report_path}")
    lines.append("ログ: logs/run.log")
    return "\n".join(lines)


def _decide_exit(run: _DiaryRun, days: "Sequence[DayResult]") -> "tuple[int, AppError | None]":
    failures = run.failures
    if not failures:
        return EXIT_OK, None
    span = _format_dates(run.options.dates)
    write_failures = [f for f in failures if f.kind == FAILURE_WRITE]
    selected = [s for s in SOURCE_NAMES if run.selected(s)]
    all_failed = bool(selected) and all(
        day.sources.get(source) == STATUS_FAILED for day in days for source in selected
    )
    if write_failures:
        error: AppError = ObsidianAppError(
            f"日記ノートへの書き込みに失敗 ({len(write_failures)} 日)",
            title=f"ihealth-relay: 日記の書き込み失敗 ({_format_dates([f.date for f in write_failures if f.date])})",
        )
    elif all_failed:
        error = ObsidianAppError(
            f"全日付で全 source の取得に失敗 ({span})",
            title=f"ihealth-relay: 日記の取得がすべて失敗 ({span})",
        )
    elif all(f.kind == FAILURE_ATTENTION for f in failures):
        where = _format_dates([f.date for f in failures if f.date])
        error = DiaryPartialFailureError(
            f"書き込めない日記ノートがあります ({where})",
            title=f"ihealth-relay: 日記ノートの確認が必要 ({where})",
        )
    else:
        sources = sorted({f.source for f in failures}, key=lambda s: SOURCE_ORDER.get(s, 9))
        error = DiaryPartialFailureError(
            f"一部の source の取得に失敗 ({', '.join(sources)})",
            title=f"ihealth-relay: 日記の一部取得失敗 ({', '.join(sources)})",
        )
    return error.exit_code, error


def _failure_signature(failures: "Sequence[DiaryFailure]") -> str:
    material = sorted(
        (f.date.isoformat() if f.date else "", f.source, f.message) for f in failures
    )
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _read_marker(path: Path) -> "dict[str, Any] | None":
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _already_notified(logs_dir: "Path | None", today: date, signature: str) -> bool:
    """同じ失敗内容を今日すでに通知したか (best-effort: 読めなければ未通知扱い)。"""
    if logs_dir is None:
        return False
    data = _read_marker(logs_dir / _FAILURE_MARKER)
    if not data or data.get("date") != today.isoformat():
        return False
    signatures = data.get("signatures")
    return isinstance(signatures, list) and signature in signatures


def _remember_notified(logs_dir: "Path | None", today: date, signature: str, logger: logging.Logger) -> None:
    if logs_dir is None:
        return
    path = logs_dir / _FAILURE_MARKER
    data = _read_marker(path) or {}
    signatures = data.get("signatures") if data.get("date") == today.isoformat() else None
    if not isinstance(signatures, list):
        signatures = []
    if signature not in signatures:
        signatures.append(signature)
    try:
        _write_text_atomic(
            path, json.dumps({"date": today.isoformat(), "signatures": signatures[-50:]}),
        )
    except OSError as exc:
        logger.warning("通知済みの記録を書けません (次回も通知されます): %s (%s)", path, exc)


def _stale_already_notified(logs_dir: "Path | None", today: date) -> bool:
    if logs_dir is None:
        return False
    try:
        return (logs_dir / _STALE_MARKER).read_text(encoding="utf-8").strip() == today.isoformat()
    except OSError:
        return False


def _remember_stale(logs_dir: "Path | None", today: date, logger: logging.Logger) -> None:
    if logs_dir is None:
        return
    try:
        _write_text_atomic(logs_dir / _STALE_MARKER, today.isoformat() + "\n")
    except OSError as exc:
        logger.warning("停止検知の通知記録を書けません: %s", exc)


def _report_path(logs_dir: Path, now: datetime) -> Path:
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = logs_dir / f"{_REPORT_PREFIX}{stamp}.json"
    n = 1
    while candidate.exists():
        candidate = logs_dir / f"{_REPORT_PREFIX}{stamp}-{n}.json"
        n += 1
    return candidate


def _prune_reports(logs_dir: Path, now: datetime, logger: logging.Logger) -> None:
    """保持期間 (30 日) より古い実行レポートを消す (best-effort)。"""
    threshold = now.astimezone(timezone.utc) - timedelta(days=_REPORT_RETENTION_DAYS)
    try:
        entries = list(logs_dir.iterdir())
    except OSError:
        return
    for path in entries:
        match = _REPORT_RE.match(path.name)
        if match is None:
            continue
        try:
            stamp = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if stamp < threshold:
            try:
                path.unlink()
            except OSError as exc:
                logger.debug("古い実行レポートを消せません: %s (%s)", path, exc)


def _day_report(day: DayResult) -> "dict[str, Any]":
    note: "dict[str, Any]" = {
        "status": day.note_status,
        "path": str(day.note_path) if day.note_path is not None else None,
    }
    if day.note_reason:
        note["reason"] = day.note_reason
    if day.backup_path is not None:
        note["backup_path"] = str(day.backup_path)
    if day.write_error:
        note["error"] = _short(day.write_error, 600)
    return {
        "date": day.date.isoformat(),
        "provisional": day.provisional,
        "provisional_keys": list(day.provisional_keys),
        "note": note,
        "sources": {name: dict(detail) for name, detail in day.source_details.items()},
        "written_keys": list(day.written_keys),
        "keys": {key: dict(entry) for key, entry in day.keys.items()},
    }


def _stale_body(stale: "Mapping[str, Any]") -> str:
    return (
        f"Health Auto Export のオートメーション出力が {stale['latest_date']} "
        f"({stale['days_since']} 日前) で止まっています。\n"
        "iPhone で Health Auto Export を開き、オートメーション (iCloud Drive / JSON) が"
        "有効か・iCloud Drive の同期が止まっていないか確認してください。"
    )


def _missing_automation_body(stale: "Mapping[str, Any]", health_export_dir: Path) -> str:
    root = stale.get("search_root") or str(health_export_dir)
    return (
        "Health Auto Export のオートメーション出力 (HealthAutoExport-YYYY-MM-DD.json) が"
        f"見つからず、健康データを 1 日も書けませんでした (探した場所: {root})。\n"
        ".env の HEALTH_EXPORT_DIR (Health Auto Export の Documents フォルダ) と、"
        "iPhone のオートメーション (iCloud Drive / JSON) が有効かを確認してください。"
    )


def _provisional_body(ledger_info: "Mapping[str, Any]") -> str:
    lines: "list[str]" = []
    overdue = ledger_info.get("overdue") or {}
    expired = ledger_info.get("expired") or {}
    if overdue:
        lines.append(
            f"今日の時点の暫定値を書いた日が {PROVISIONAL_OVERDUE_DAYS} 日を過ぎても"
            "確定値に置き換わっていません:"
        )
        for day, keys in list(overdue.items())[:_MAX_NOTIFY_GROUPS]:
            lines.append(f"  {day}: {', '.join(keys)}")
        if len(overdue) > _MAX_NOTIFY_GROUPS:
            lines.append(f"  …ほか {len(overdue) - _MAX_NOTIFY_GROUPS} 日")
        lines.append(
            "Health Auto Export がその日のファイルを日付が変わった後に書き直すと自動で"
            f"置き換わります ({PROVISIONAL_REVISIT_DAYS} 日以内)。iPhone で Health Auto Export を"
            "開き、オートメーションの書き出し期間に過去の日が含まれているか確認してください。"
        )
    if expired:
        lines.append(
            f"次の日は {PROVISIONAL_REVISIT_DAYS} 日以内に確定しなかったため自動の再処理を"
            "やめました (値は暫定値のままです。--date <日付> で上書きできます):"
        )
        for day, keys in list(expired.items())[:_MAX_NOTIFY_GROUPS]:
            lines.append(f"  {day}: {', '.join(keys)}")
    return "\n".join(lines)


def _freshness_notice(
    run: _DiaryRun, stale: "Mapping[str, Any] | None", ledger_info: "Mapping[str, Any]",
) -> "tuple[str, str] | None":
    """データの鮮度の通知 (停止の疑い / 出力が見つからない / 暫定値が確定しない) を 1 通に。"""
    sections: "list[str]" = []
    title: "str | None" = None
    if stale is not None and stale.get("stale"):
        title = "ihealth-relay: HAE オートメーション停止の疑い"
        sections.append(_stale_body(stale))
    if stale is not None and stale.get("missing"):
        title = title or "ihealth-relay: HAE オートメーション出力が見つかりません"
        sections.append(_missing_automation_body(stale, run.config.health_export_dir))
    if ledger_info.get("overdue") or ledger_info.get("expired"):
        title = title or "ihealth-relay: 日記の暫定値が確定していません"
        sections.append(_provisional_body(ledger_info))
    if title is None:
        return None
    sections.append("(この通知は 1 日 1 回までです。終了コードには影響しません)")
    return title, "\n\n".join(sections)


@dataclass
class _Notice:
    """送る (かもしれない) 通知 1 件。"""

    #: レポートの ``notifications`` のキー: ``"failure"`` (取得・書き込みの失敗) /
    #: ``"stale"`` (データの鮮度: 停止の疑い / 出力が見つからない / 暫定値が確定しない)
    kind: str
    title: str
    body: str
    suppressed: bool
    remember: "Callable[[], None]"  #: 送れたときに「通知済み」を記録する


def _deliver(
    notifier: "Callable[[str, str], Optional[bool]]",
    title: str,
    body: str,
    logger: logging.Logger,
) -> bool:
    """通知を送る。リマインダーを作れなかった (``False`` / 例外) ら False。"""
    try:
        delivered = notifier(title, body)
    except Exception:  # noqa: BLE001 — 通知の失敗で終了コードを変えない
        logger.exception("Reminders 通知に失敗しました: %s", title)
        return False
    if delivered is False:
        logger.warning(
            "Reminders のリマインダーを作成できませんでした (次の実行で改めて通知します): %s",
            title,
        )
        return False
    return True


def notify_once_per_day(
    title: str,
    body: str,
    *,
    logs_dir: "Path | None",
    today: date,
    logger: logging.Logger,
    notifier: "Callable[[str, str], Optional[bool]] | None" = None,
) -> str:
    """同じ内容 (title + body) の通知を 1 日 1 回に抑えて送る (``logs/.failure_notified``)。

    設定不備 (exit 3) のように毎回同じ内容で失敗する通知が、1 日 3 回の定時実行で
    積み上がらないようにする。リマインダーを作れなかったときは記録しない (次回再送)。

    Returns:
        ``"sent"`` / ``"suppressed"`` (今日すでに送った) / ``"failed"`` (作れなかった)
    """
    signature = hashlib.sha256(
        json.dumps(["notice", title, body], ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    if _already_notified(logs_dir, today, signature):
        logger.info("同じ内容は今日すでに通知済みのため Reminders 通知を省略: %s", title)
        return "suppressed"
    if not _deliver(notifier or notify_failure, title, body, logger):
        return "failed"
    _remember_notified(logs_dir, today, signature, logger)
    return "sent"


def _write_report(path: Path, report: "Mapping[str, Any]") -> None:
    _write_text_atomic(path, json.dumps(report, ensure_ascii=False, indent=1))


def _finish(
    run: _DiaryRun,
    days: "Sequence[DayResult]",
    stale: "dict[str, Any] | None",
    ledger_info: "dict[str, Any]",
    started_at: datetime,
) -> DiaryRunResult:
    """終了コードを決め、実行レポートを保存してから通知する。"""
    logger = run.logger
    options = run.options
    finished_at = run.deps.clock()
    exit_code, error = _decide_exit(run, days)
    logs_dir = run.config.logs_dir

    # ---- 送る通知 (同じ失敗は 1 日 1 回 / データの鮮度は 1 日 1 回) ----
    notices: "list[_Notice]" = []
    if error is not None:
        signature = _failure_signature(run.failures)
        notices.append(_Notice(
            kind="failure",
            title=error.title,
            body="",  # レポートの path を入れるので保存後に組む
            suppressed=_already_notified(logs_dir, run.today, signature),
            remember=lambda: _remember_notified(logs_dir, run.today, signature, logger),
        ))
    freshness = _freshness_notice(run, stale, ledger_info)
    if freshness is not None:
        notices.append(_Notice(
            kind="stale",
            title=freshness[0],
            body=freshness[1],
            suppressed=_stale_already_notified(logs_dir, run.today),
            remember=lambda: _remember_stale(logs_dir, run.today, logger),
        ))
    notification_info: "dict[str, Any]" = {}
    for notice in notices:
        item: "dict[str, Any]" = {"title": notice.title, "suppressed": notice.suppressed}
        if options.dry_run and not notice.suppressed:
            item["dry_run"] = True
        notification_info[notice.kind] = item

    # ---- 実行レポート (backfill の全数台帳) ----
    report: "dict[str, Any]" = {
        "version": 1,
        "started_at": started_at.astimezone(timezone.utc).isoformat(),
        "finished_at": finished_at.astimezone(timezone.utc).isoformat(),
        "publisher": "obsidian",
        "dry_run": options.dry_run,
        "fill_empty_only": options.fill_empty_only,
        "sources": list(options.sources),
        "today": run.today.isoformat(),
        "window": {
            "from": options.dates[0].isoformat(),
            "to": options.dates[-1].isoformat(),
            "days": len(options.dates),
        },
        "exit_code": exit_code,
        "github": run.github_summary,
        "stale_check": stale,
        "provisional": ledger_info,
        "failures": [
            {
                "date": f.date.isoformat() if f.date else None,
                "source": f.source,
                "kind": f.kind,
                "message": f.message,
            }
            for f in run.failures
        ],
        "notifications": notification_info,
        "dates": [_day_report(day) for day in days],
    }
    report_path: "Path | None" = None
    if logs_dir is not None:
        candidate = _report_path(logs_dir, finished_at)
        try:
            _write_report(candidate, report)
        except OSError as exc:
            logger.warning("実行レポートを書けません: %s (%s)", candidate, exc)
        else:
            report_path = candidate
            logger.info("実行レポート: %s", report_path)
        _prune_reports(logs_dir, finished_at, logger)

    # ---- 通知 (失敗しても終了コードは変えない) ----
    notifier = run.deps.notifier or notify_failure
    notifications: "list[tuple[str, str]]" = []
    due: "list[_Notice]" = []
    for notice in notices:
        if notice.kind == "failure":
            notice.body = _failure_body(run.failures, options.dates, report_path)
        if notice.suppressed:
            logger.info("同じ内容は今日すでに通知済みのため Reminders 通知を省略: %s", notice.title)
        else:
            due.append(notice)
    attempted = False
    if due:
        # 1 run 1 通: 失敗とデータの鮮度の通知が両方あれば 1 つのリマインダーにまとめる
        title = due[0].title
        body = "\n\n".join(
            notice.body if index == 0 else f"■ {notice.title}\n{notice.body}"
            for index, notice in enumerate(due)
        )
        if options.dry_run:
            # dry-run はリマインダーを作らず「通知済み」も記録しない (本番の実行の通知を
            # 抑止しないため。iPhone に同期される永続リマインダーを試行で作らない)
            logger.info("[dry-run] Reminders 通知 (送信しません): %s\n%s", title, body)
        else:
            attempted = True
            delivered = _deliver(notifier, title, body, logger)
            for notice in due:
                notification_info[notice.kind]["delivered"] = delivered
            if delivered:
                notifications.append((title, body))
                for notice in due:
                    notice.remember()
    if attempted and report_path is not None:
        try:
            _write_report(report_path, report)  # 通知の成否を反映
        except OSError as exc:
            logger.warning("実行レポートを更新できません: %s (%s)", report_path, exc)

    written_total = sum(len(day.written_keys) for day in days)
    summary_level = logging.INFO if exit_code == EXIT_OK else logging.WARNING
    logger.log(
        summary_level,
        "日記 完了: %d 日 (%s), 書込%s %d キー, 失敗 %d 件, exit=%d",
        len(days), _format_dates(options.dates),
        " 予定 (dry-run: 書き込みなし)" if options.dry_run else "",
        written_total, len(run.failures), exit_code,
    )
    return DiaryRunResult(
        exit_code=exit_code,
        dates=options.dates,
        days=tuple(days),
        failures=tuple(run.failures),
        report=report,
        report_path=report_path,
        notifications=tuple(notifications),
    )
