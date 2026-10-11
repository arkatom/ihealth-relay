"""Obsidian 日記パイプライン: 健康 (HAE) + 気象庁の天気 + GitHub 活動を日付ごとに書き込む.

``python3 -m ihealth --publisher obsidian`` (または ``.env`` の
``IHEALTH_PUBLISHER=obsidian``) の本体 (SPEC §2-E / v2 §4.1 / §4.6)。

## 処理の流れ

1. 日記 publisher (:class:`ihealth.publishers.obsidian.ObsidianPublisher`) を組む。
   vault / 日記ディレクトリ不在・キー衝突などは :class:`ConfigAppError` (exit 3)
2. 実行レポート ``logs/diary-report-<UTC>.json`` を **処理する予定の日付一覧を入れて**
   作り、日付ごとの結果を追記する journal ``logs/diary-report-<UTC>.jsonl`` を開く。
   どちらかを作れなければ、日記には一切触れずに終える (exit 12 + 通知。何を変えたかを
   記録できないまま日記を書き換えない)
3. 起動直後 (スリープ復帰直後) は Wi-Fi が未接続のことがあるので、ネットワークに
   繋がるまで最大 ``IHEALTH_NETWORK_GRACE_SEC`` (既定 60) 秒待つ (:class:`NetworkGate`)。
   猶予内に消えた接続エラーは失敗として扱わない (通知しない)
4. GitHub 活動を対象期間まとめて 1 回収集する
5. 日付ごとに、健康 (:class:`ihealth.health_loader.HealthLoader`) と天気
   (:func:`ihealth.weather.fetch_weather`, 1 リクエスト / 秒以下) を **独立に** 取得し、
   :class:`ihealth.records.DiaryRecord` にまとめて日記ノートに書く。1 source の失敗は
   他の source・他の日付に影響しない。1 日処理するごとに、その日の結果を journal に
   1 行追記して fsync する (途中でプロセスが落ちても、処理済みの日の結果が残る)。
   追記できなければ残りの日は処理しない (exit 12 + 通知)
6. 未確定の値の台帳 (``data/diary-state.json``, 下記) を更新する
7. 実行レポートを確定する (summary + 全日付の結果を書いて journal を消す)。確定できな
   ければ失敗 (exit 12 以上 + 通知。journal は残す)。失敗を 1 通の Reminders 通知に集約する

## 何を書くか (完全性ステータス, SPEC v2 §4.1)

各 source の結果は ``complete`` / ``partial`` / ``failed`` / ``nodata`` のいずれか。
publisher には **確実に取れた値だけ** を渡す (渡さないキーは既存値を保持):

- 健康: ``HealthLoadResult.health`` の非 None フィールドと ``activity_detail``。
  ``failed`` は書かない + 通知、``partial`` は取れた値を書いて通知、``nodata``
  (HAE 未出力) は静かに見送る。今日の値・日の途中で書き出された出力の値は暫定値
  (``provisional``) として書き、後の実行で確定値に上書きされる。データ源が確定して
  いないため書かなかったフィールド (``withheld_fields``) は保留 (下記「台帳」で追跡)
- 天気: 日が完成していればそのキー (欠測キーは書かない)。当日・公開待ち
  (``nodata``) は書かない。確定済みのはずの日の未完成 (``partial`` かつ未完成) も
  書かない。どちらも台帳で追跡し、3 日を超えて揃わなければ「要確認」として知らせる。
  未完成が **2 日以上連続** したときは失敗として通知する (天気欄の形式変更などの疑い)
- GitHub: 収集が完全 (``complete``) なときだけ書く。活動が無い日は ``"該当なし"``。
  一部の repo の取得に失敗した (``partial``) / 何も取れない (``failed``) なら書かずに通知
  (台帳でも追跡する)。今日の分は暫定値 (その時点までの活動)

## 未確定の値の台帳 (``data/diary-state.json``)

暫定値のまま、または値が取れないまま既定 window (7 日) の外へ出た日は、二度と処理されず
確定値のような顔で残る / 空のまま残る (HAE のオートメーションが何日も止まると、その日の
再出力は window より後に来る)。そこで **まだ確定していないキー** を、日付ごとに理由と
初回検出日時 (と、本ツールが書いた暫定値) とともに台帳へ記録する。理由:

- ``provisional``: 暫定値を書いた (今日の健康・アクティビティ詳細・GitHub /
  日の途中で書き出された HAE の出力 / segment 形式の夜主睡眠が前日ファイル不足
  (そのフィールドだけ, :attr:`HealthLoadResult.provisional_fields`)。書いた値も記録する)
- ``withheld``: データ源が確定していないため書かなかった (保留。打ち切られた
  AutoSync 補完値など, :attr:`HealthLoadResult.withheld_fields`)
- ``weather_pending``: 気象庁の公開待ち (当日・直近の日)
- ``weather_incomplete``: 確定済みのはずの日の天気が未完成 (気象庁の欠測の可能性)
- ``github_incomplete``: GitHub の収集が不完全 (一部 / 全部の repo の取得に失敗)
- ``unwritable``: 以前に書いた暫定値の entry にコメントが付いた / キーが重複した
  (W3-3 の ``kept`` + warning) ため、確定値に書き換えられない

そして、

- 引数なしの実行 (launchd) では、台帳にある日を ``PROVISIONAL_REVISIT_DAYS`` (30) 日前まで
  既定 window に加えて処理し直す。window の外から加えた日は fill-only で書く (空のキーを
  埋め、暫定値のままのキーを確定値に置き換えるだけ。利用者が書いた値は上書きしない)。
  確定値を書けた (または既存値が確定値と同じ) キーは台帳から消える。利用者が値を
  書き換えたキー (fill-only で保持された) も消す
- 書く値が無かった追跡中のキーは、ノートの現在値を読んで (読み取りのみ) 判断する:
  利用者が手で値を書いた (空でも本ツールの暫定値でもない = 再処理しても上書きしない値)
  なら消す。確定した source にその日の値が無い (完成した気象庁のページで欠測 / 完全に
  読めた健康データに無い) なら、本ツールの暫定値がノートに残っていなければ消す。
  source を設定 (``IHEALTH_SOURCES``) から外したら消す (``--sources`` で一時的に外した
  だけなら、次の定時実行が処理するので追跡を続ける)。消した理由はレポートの
  ``keys.<キー>.resolution`` (``user_value`` / ``no_value_final`` / ``source_deselected``)
- ``--fill-empty-only`` でも、台帳のキーで **既存値が台帳の暫定値のまま** のものは
  確定値に置き換える (利用者が書き換えた値は保持)
- 初回検出から ``PROVISIONAL_OVERDUE_DAYS`` (3) 日を超えても確定しないキーは「要確認」
  として通知する (1 日 1 回)。ただし自動ではまず確定しない ``withheld`` /
  ``weather_incomplete`` は 1 回だけ (台帳の ``notified``。その後は 30 日まで黙って追跡し、
  手で記入すれば追跡も終わる)。台帳から外れた (30 日超) 日も最後に 1 回知らせる
- 30 日より前の日付 (過去分の backfill など) で見つかった未確定のキーは、自動では処理し
  直せないので台帳に入れず、実行レポートの ``provisional.untracked`` に残して「要確認」の
  通知で 1 回知らせる (暫定値が確定値の顔で黙って残らないように)
- 一回きりの「要確認」(台帳から外れた日 / 30 日より前の未確定のキー) は、リマインダーを
  作れなかったら台帳の ``unreported`` に残し、次の実行で改めて送る (送れたら消す)
- ``--dry-run`` は台帳を読むだけで書かない

## 終了コード

- 0: 失敗なし (``nodata`` は失敗ではない。ノートが無く作らなかった日 (今日・昨日、
  作成しない設定) も失敗ではない)
- 11 (:class:`ObsidianAppError`): 日記ノートの書き込み失敗、または全日付で全 source が失敗
- 12 (:class:`DiaryPartialFailureError`): 一部の source / 日付で取得に失敗、
  利用者の対応が要るノート (Templater の未処理構文が残る / テンプレートを使えない)、
  または実行レポート・台帳を保存できない

## 通知

- 失敗は source と日付をまとめて 1 run 1 通知。同じ失敗内容の通知は同じ日には
  1 回だけ (``logs/.failure_notified``)。1 日 3 回の定時実行で同じリマインダーが
  積み上がらないようにするため。内容が変われば (別の失敗) 改めて通知する
- データの鮮度の通知 (終了コードは変えない): HAE オートメーションの最新出力日が今日 - 3 日
  より古い (停止の疑い) / オートメーションの出力が 1 件も見つからず健康データを 1 日も取れない
  (設定不備の疑い) / 3 日を超えて確定しない値がある (要確認) は 1 日 1 回
  (``logs/.stale_notified``)。一回きりの項目 (台帳から外れた日 / 30 日より前の未確定のキー /
  1 回だけ知らせる理由の要確認) は、今日まだ送っていないものを載せる
  (``logs/.freshness_notified`` に内容の署名を残す。朝に停止の疑いを送った日でも、後の
  backfill で見つかった項目は知らせる)
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
    EXISTING_EMPTY,
    EXISTING_OTHER,
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
    STATUS_CREATED as _NOTE_CREATED,
    STATUS_UNCHANGED as _NOTE_UNCHANGED,
    STATUS_UPDATED as _NOTE_UPDATED,
    STATUS_WOULD_UPDATE as _NOTE_WOULD_UPDATE,
    NotePublishOutcome,
    ObsidianPublishError,
)
from ihealth.records import WEATHER_FIELDS, DiaryRecord
from ihealth.weather import JMA_HOURLY_ENDPOINT, WeatherError, WeatherSummary, fetch_weather


SOURCE_HEALTH, SOURCE_WEATHER, SOURCE_GITHUB = SOURCE_NAMES
#: 書き込み失敗を失敗一覧に載せるときの source 名。
SOURCE_OBSIDIAN = "obsidian"
#: 実行レポート (logs/diary-report-*.json / .jsonl) を保存できない失敗の source 名。
SOURCE_REPORT = "report"

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
#: 未確定の値がある日を、既定 window の外でも処理し直す期間 (日)。これより古い日は台帳から外す
#: (それより古い日付で見つかった未確定の値は台帳に入れない)。
PROVISIONAL_REVISIT_DAYS = 30
#: 未確定のキーが初回検出から、この日数を超えても確定しなければ「要確認」として通知する
#: (初回検出日時が不明な形式 1 の台帳のキーは対象日から数える)。
PROVISIONAL_OVERDUE_DAYS = 3

#: 台帳 (と実行レポート) の未確定の理由。
UNRESOLVED_PROVISIONAL = "provisional"            # 暫定値を書いた
UNRESOLVED_WITHHELD = "withheld"                  # データ源が確定していないため書かなかった (保留)
UNRESOLVED_WEATHER_PENDING = "weather_pending"    # 気象庁の公開待ち (当日・直近の日)
UNRESOLVED_WEATHER_INCOMPLETE = "weather_incomplete"  # 確定済みのはずの日の天気が未完成
UNRESOLVED_GITHUB_INCOMPLETE = "github_incomplete"    # GitHub の収集が不完全
#: 以前に書いた暫定値の entry にコメントがある / キーが重複していて、確定値に書き換えられない
UNRESOLVED_UNWRITABLE = "unwritable"
UNRESOLVED_REASONS: "tuple[str, ...]" = (
    UNRESOLVED_PROVISIONAL,
    UNRESOLVED_WITHHELD,
    UNRESOLVED_WEATHER_PENDING,
    UNRESOLVED_WEATHER_INCOMPLETE,
    UNRESOLVED_GITHUB_INCOMPLETE,
    UNRESOLVED_UNWRITABLE,
)
#: 自動ではまず確定しない (停止済み AutoSync の途中までの出力 / 気象庁の欠測の疑い) ため、
#: 「要確認」を毎日ではなく 1 回だけ知らせる理由 (手で記入すれば追跡も終わる)。
NOTIFY_ONCE_REASONS: "frozenset[str]" = frozenset({
    UNRESOLVED_WITHHELD, UNRESOLVED_WEATHER_INCOMPLETE,
})
#: 通知本文での未確定の理由の呼び方。
_UNRESOLVED_LABELS: "Mapping[str, str]" = {
    UNRESOLVED_PROVISIONAL: "暫定値",
    UNRESOLVED_WITHHELD: "保留",
    UNRESOLVED_WEATHER_PENDING: "天気の公開待ち",
    UNRESOLVED_WEATHER_INCOMPLETE: "天気が未完成",
    UNRESOLVED_GITHUB_INCOMPLETE: "GitHub の取得が不完全",
    UNRESOLVED_UNWRITABLE: "コメント / 重複キーのため書き換えられない",
}
#: 書く値の無かった追跡中のキーが台帳から外れた理由 (レポートの ``resolution``)。
RESOLUTION_USER_VALUE = "user_value"            # ノートに利用者の値がある (再処理しても上書きしない値)
RESOLUTION_NO_VALUE = "no_value_final"          # 確定した source にその日の値が無い (欠測で確定)
RESOLUTION_SOURCE_DESELECTED = "source_deselected"  # source を設定 (IHEALTH_SOURCES) から外した
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
#: 実行レポート (.json) と、日付ごとの結果を追記する journal (.jsonl)。
_REPORT_RE = re.compile(r"^diary-report-(\d{8}T\d{6}Z)(?:-\d+)?\.jsonl?$")
_REPORT_RETENTION_DAYS = 30
_REPORT_VERSION = 2
#: 実行レポートの ``status``。
REPORT_RUNNING = "running"          # 処理中 (プロセスが落ちたらこのまま残る。日付ごとの結果は journal)
REPORT_FINISHED = "finished"        # 予定の全日付を処理した
REPORT_INTERRUPTED = "interrupted"  # レポートを保存できず、予定の日付を処理しきらずに止めた
_STALE_MARKER = ".stale_notified"
_FAILURE_MARKER = ".failure_notified"
#: 一回きりの「要確認」(台帳から外した日 / 追跡しない古い日 / 1 回だけ知らせる理由) のうち
#: 今日送ったものの署名 (同じ日に同じ内容を 2 回送らない)。
_FRESHNESS_MARKER = ".freshness_notified"
_MAX_MARKER_SIGNATURES = 2000
#: 一回きりの「要確認」の種類 (台帳の ``unreported`` のキーにも使う)。
ONE_SHOT_EXPIRED = "expired"          # 30 日以内に確定せず台帳から外した日
ONE_SHOT_UNTRACKED = "untracked"      # 30 日より前の日付のため台帳に入れなかった未確定のキー
ONE_SHOT_OVERDUE = "overdue_once"     # 1 回だけ知らせる理由 (NOTIFY_ONCE_REASONS) の「3 日超」
_PERSISTED_ONE_SHOTS = (ONE_SHOT_EXPIRED, ONE_SHOT_UNTRACKED)
#: 台帳の形式。1 = 日付 → キー → 暫定値 (読込のみ対応)、2 = 理由 / 初回検出日時 / 値。
_LEDGER_VERSION = 2
#: 台帳に記録できる値の型 (frontmatter に書く値 = int / float / str)。
_LEDGER_VALUE_TYPES = (int, float, str)
#: 通知本文に載せる失敗グループ数 / メッセージ長の上限。
_MAX_NOTIFY_GROUPS = 8
_MAX_MESSAGE_LEN = 240

#: 通知本文・レポートでの source の並び。
SOURCE_ORDER: "Mapping[str, int]" = {
    SOURCE_HEALTH: 0, SOURCE_WEATHER: 1, SOURCE_GITHUB: 2, SOURCE_OBSIDIAN: 3, SOURCE_REPORT: 4,
}

#: :class:`DiaryFailure` の種類。
FAILURE_SOURCE = "source"        # source の取得失敗 (exit 12, 全滅なら 11)
FAILURE_WRITE = "write"          # 日記ノートの書き込み失敗 (exit 11)
FAILURE_ATTENTION = "attention"  # ノートが書ける状態でない・利用者の対応が要る (exit 12)
FAILURE_RECORD = "record"        # 実行レポート / 台帳を保存できない (exit 12)

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
            通知 (Reminders) も送らず、未確定の値の台帳・通知済みの記録も書かない
        fill_empty_only: 既存の非空値を保持する (過去分 backfill 用)。ただし台帳にある
            暫定値のままのキーは確定値に置き換える
        revisit_unresolved: 未確定の値の台帳にある日 (:data:`PROVISIONAL_REVISIT_DAYS`
            日以内) も ``dates`` に加えて処理する (引数なしの既定 window の実行で使う)
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
        provisional_keys: この実行の後も暫定値のまま (本ツールが書いた暫定値が残る) のキー
        unresolved: この実行の後もまだ確定していないキー → 理由 (``UNRESOLVED_*``)。
            ``provisional_keys`` を含む
        tracked: この日が未確定の値の台帳で追跡されている (この実行の後)
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
    unresolved: "Mapping[str, str]" = field(default_factory=dict)
    tracked: bool = False


@dataclass(frozen=True)
class LedgerItem:
    """未確定の値の台帳の 1 キー分。

    Attributes:
        reason: 未確定の理由 (``UNRESOLVED_*``。直近の実行で判定したもの)
        since: 初回検出日時 (aware)。形式 1 の台帳から読んだものは不明 (``None``。
            対象日から数える)
        value: 本ツールが書いた暫定値 (``replaceable``: ``--fill-empty-only`` / window の外の
            処理でも、既存値がこの値のままなら確定値に置き換える)。書いていなければ ``None``
        notified: 1 回だけ知らせる理由 (:data:`NOTIFY_ONCE_REASONS`) の「要確認」を送った
            (以後は知らせず、確定するか 30 日を過ぎるまで黙って追跡する)
    """

    reason: str
    since: "datetime | None" = None
    value: "object | None" = None
    notified: bool = False


@dataclass(frozen=True)
class _OneShot:
    """一回きりの「要確認」の項目 (1 日分)。

    Attributes:
        kind: ``ONE_SHOT_*``
        date: 対象日
        keys: (frontmatter キー, 未確定の理由) の並び
        carried: 以前の実行で送れなかった (台帳の ``unreported`` から読んだ) もの
    """

    kind: str
    date: date
    keys: "tuple[tuple[str, str], ...]"
    carried: bool = False

    def signature(self) -> str:
        material = [self.kind, self.date.isoformat(), sorted([key, reason] for key, reason in self.keys)]
        return hashlib.sha256(
            json.dumps(material, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:32]


@dataclass(frozen=True)
class _NoticeItems:
    """データの鮮度の通知に載せる未確定の値 (:func:`_commit_ledger` が求める)。

    Attributes:
        daily_overdue: 3 日を超えて確定しない値のうち、毎日 (1 日 1 回) 知らせるもの
        one_shots: 一回きりの項目 (送れたら台帳から消す / 「知らせ済み」にする)
    """

    daily_overdue: "Mapping[date, Mapping[str, str]]" = field(default_factory=dict)
    one_shots: "tuple[_OneShot, ...]" = ()


@dataclass
class _LedgerState:
    """台帳ファイルの中身: 日付ごとの未確定のキーと、送れなかった一回きりの通知。"""

    dates: "dict[date, dict[str, LedgerItem]]" = field(default_factory=dict)
    #: ``ONE_SHOT_EXPIRED`` / ``ONE_SHOT_UNTRACKED`` → 日付 → キー → 理由
    unreported: "dict[str, dict[date, dict[str, str]]]" = field(default_factory=dict)


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
        #: 未確定の値の台帳 (日付 → frontmatter キー → :class:`LedgerItem`)。実行開始時に読んだ内容
        self.ledger: "dict[date, dict[str, LedgerItem]]" = {}
        #: 台帳にある、以前の実行で送れなかった一回きりの通知 (実行開始時に読んだ内容)
        self.ledger_unreported: "dict[str, dict[date, dict[str, str]]]" = {}
        #: データの鮮度の通知に載せる未確定の値 (:func:`_commit_ledger` が求める)
        self.notice_items = _NoticeItems()
        #: 設定 (``IHEALTH_SOURCES``) から外されていて、この実行でも選ばれていない source。
        #: その source のキーは今後も確定しないので追跡をやめる (``--sources`` で一時的に
        #: 外しただけの source は、次の定時実行が処理するので追跡を続ける)
        configured = tuple(getattr(config, "sources", SOURCE_NAMES) or ())
        self.deselected: "frozenset[str]" = frozenset(
            s for s in SOURCE_NAMES if s not in configured and s not in options.sources
        )
        #: この実行で処理した日の台帳の新しい内容 (空 dict = 確定済み / 追跡終了)
        self.ledger_updates: "dict[date, dict[str, LedgerItem]]" = {}
        #: 台帳から加えた (既定 window の外の) 日。空のキーを埋めることと、暫定値のままの
        #: キーを確定値に置き換えることだけをする (利用者が書き換えた値は保持)
        self.revisit_only: "frozenset[date]" = frozenset()
        #: 追跡期間 (:data:`PROVISIONAL_REVISIT_DAYS`) より古いため台帳に入れなかった
        #: 未確定のキー (日付 → キー → 理由。レポートにだけ残す)
        self.untracked: "dict[date, dict[str, str]]" = {}
        #: この実行の開始時刻 (台帳の初回検出日時に使う)
        self.started_at: datetime = deps.clock()

    # ---------- 共通 ----------

    def selected(self, source: str) -> bool:
        return source in self.options.sources

    def fail(
        self, d: "date | None", source: str, message: str, *, kind: str = FAILURE_SOURCE,
    ) -> None:
        self.failures.append(DiaryFailure(d, source, _short(message), kind))

    def _aware_now(self) -> datetime:
        """HealthLoader 用の現在時刻 (aware。naive な時計は UTC とみなす)。

        システムのローカル時刻 (naive) に直して渡すと、夏時間の切り替わり (同じ壁時計の
        時刻が 2 回ある 1 時間) で HealthLoader が別の瞬間と解釈してしまう (日の終わりの
        判定が Mac のローカル tz に依存する) ため、絶対時刻のまま渡す。
        """
        now = self.deps.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            now = now.replace(tzinfo=timezone.utc)
        return now

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
            self.loader = factory(self.config, self.logger, self._aware_now)
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
        fields = getattr(result, "provisional_fields", None)
        if result.provisional and fields is not None:
            # 暫定なのは一部のフィールドだけ (segment 形式の夜主睡眠など)
            state.info["provisional_fields"] = list(fields)
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
    実行レポートは開始時に予定の日付一覧で作り、1 日処理するごとに journal へ追記する
    (作れない / 追記できないときは、それ以上日記を書き換えずに exit 12 + 通知)。

    Raises:
        ConfigAppError: vault / 日記ディレクトリ不在・``config.toml`` 不正・キー衝突
            (publisher を組み立てられない = 何も始められない設定不備)。
    """
    deps = (deps or DiaryDeps()).resolved()
    if not options.dates:
        raise ValueError("options.dates が空です")
    ledger_state = _load_ledger_state(config.diary_state_path(), logger)
    ledger = ledger_state.dates
    dates = set(options.dates)
    revisit: "list[date]" = []
    if options.revisit_unresolved:
        revisit = sorted(
            d for d in ledger
            if d not in dates and 0 <= (options.today - d).days <= PROVISIONAL_REVISIT_DAYS
        )
        if revisit:
            logger.info(
                "値がまだ確定していない日も処理します (空のキーを埋め、暫定値のままの"
                "キーを確定値に置き換えるだけ): %s", _format_dates(revisit),
            )
            dates.update(revisit)
    if tuple(sorted(dates)) != tuple(options.dates):
        options = replace(options, dates=tuple(sorted(dates)))
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
    run.ledger_unreported = ledger_state.unreported
    run.revisit_only = frozenset(revisit)
    report = _RunReport(config.logs_dir, logger)
    if not report.start(_report_header(run, revisit), now=run.started_at):
        # 何を変えたかを記録できないまま日記を書き換えない (W3-5)
        run.fail(
            None, SOURCE_REPORT,
            f"実行レポートを作成できないため、日記を更新しませんでした: {report.error}",
            kind=FAILURE_RECORD,
        )
        return _finish(run, [], None, _ledger_info_unchanged(run), report)

    run.warm_up_network()
    if run.selected(SOURCE_GITHUB):
        run.collect_github()
        if not report.append({"type": "github", **run.github_summary}):
            _stop_for_report(run, report, processed=0)

    days: "list[DayResult]" = []
    if report.ok:
        for d in options.dates:
            day = _process_day(run, publisher, d)
            days.append(day)
            line = {
                "type": "day",
                "seq": len(days),
                **_day_report(day),
                "failures": [_failure_item(f) for f in run.failures if f.date == d],
            }
            if not report.append(line):
                _stop_for_report(run, report, processed=len(days))
                break
    _check_weather_persistence(run, days)
    stale = _check_stale(run, days)
    ledger_info = _commit_ledger(run)
    return _finish(run, days, stale, ledger_info, report)


def _stop_for_report(run: _DiaryRun, report: "_RunReport", *, processed: int) -> None:
    """journal に追記できない → 残りの日付は処理しない (記録できない書き換えをしない)。"""
    remaining = len(run.options.dates) - processed
    run.logger.error(
        "実行レポートの journal に追記できないため、残り %d 日の処理を中止します: %s",
        remaining, report.error,
    )
    run.fail(
        None, SOURCE_REPORT,
        f"実行レポートに結果を記録できないため、{len(run.options.dates)} 日中 {processed} 日"
        f"で処理を中止しました (残り {remaining} 日は未処理): {report.error}",
        kind=FAILURE_RECORD,
    )


def _process_day(run: _DiaryRun, publisher: Any, d: date) -> DayResult:
    health_result, health_state = run.health_for(d)
    weather, weather_state = run.weather_for(d)
    github_text, github_state = run.github_for(d)

    health = health_result.health if health_result is not None else None
    activity_detail = health_result.activity_detail if health_result is not None else None
    provisional = d >= run.today or bool(
        health_result is not None and health_result.provisional and health is not None
    )
    # キーごとの暫定判定 (内部名): 健康 (アクティビティ詳細を含む) は対象日がまだ終わって
    # いない間と途中経過の出力、segment 形式の夜主睡眠は前日ファイルが足りない間 (その
    # フィールドだけ)、GitHub は日記の今日の分 (その時点までの活動)。天気は完成した日しか書かない
    provisional_names = _provisional_names(d, run.today, health_result)
    tracked = run.ledger.get(d, {})
    # 以前に本ツールが書いた暫定値 (既存値がこれのままなら fill-only でも置き換えてよい)
    replaceable = {key: item.value for key, item in tracked.items() if item.value is not None}

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
                record, replaceable=replaceable, fill_only_empty=True,
            )
        elif run.options.fill_empty_only and replaceable:
            # --fill-empty-only でも、以前に書いた暫定値のままのキーは確定値に置き換える
            outcome = publisher.publish_record(record, replaceable=replaceable)
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
    resolutions: "dict[str, str]" = {}
    if note_status == "missing":
        # ノートが無い = 暫定値が残っているノートも、埋めるべきノートも無い (追跡をやめる)
        unresolved: "dict[str, LedgerItem]" = {}
    else:
        unresolved, resolutions = _unresolved_after(
            tracked, keys, updates, provisional_names, now=run.started_at,
            final_sources=_final_sources(d, run.today, states, health_result),
            deselected_sources=run.deselected,
            note_states=_probe_note(run, publisher, outcome, write_error, tracked, keys),
        )
    if (run.today - d).days > PROVISIONAL_REVISIT_DAYS and not tracked:
        # 自動では処理し直せない古い日付: 台帳に入れず、レポートにだけ残す
        entry: "dict[str, LedgerItem]" = {}
        if unresolved:
            run.untracked[d] = {key: item.reason for key, item in unresolved.items()}
    else:
        entry = unresolved
    if entry or tracked:
        run.ledger_updates[d] = entry
    for key, item in keys.items():
        current = unresolved.get(key)
        previous = tracked.get(key)
        if current is not None:
            item["unresolved"] = current.reason
            if current.reason == UNRESOLVED_PROVISIONAL:
                item["provisional"] = True
        elif previous is not None:
            if note_status == "missing":
                item["tracking_stopped"] = "note_missing"
            else:
                item["resolved"] = previous.reason
                if previous.reason == UNRESOLVED_PROVISIONAL:
                    item["provisional_resolved"] = True
                if key in resolutions:
                    item["resolution"] = resolutions[key]
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
        provisional_keys=tuple(
            key for key in keys
            if key in unresolved and unresolved[key].reason == UNRESOLVED_PROVISIONAL
        ),
        unresolved={key: unresolved[key].reason for key in keys if key in unresolved},
        tracked=bool(entry),
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
# 未確定の値の台帳 (data/diary-state.json)
# ======================================================================


def _pending_reason(item: "Mapping[str, Any]") -> "str | None":
    """書かなかったキーのレポート項目から、後の実行で確定しうる理由を求める (無ければ None)。

    - 健康の保留 (``not_final``: データ源が確定していないため書かなかった) → ``withheld``
    - 天気の公開待ち (当日 ``not_published`` / 公開待ちの ``nodata``) → ``weather_pending``
    - 確定済みのはずの日の天気が未完成 (``incomplete_day``) → ``weather_incomplete``
    - GitHub の収集が不完全 (一部失敗 ``source_incomplete`` / 全部失敗 ``source_failed``)
      → ``github_incomplete``

    健康・天気の取得失敗 (``source_failed``) は含めない (失敗として毎回通知し、既定 window の
    中で取り直す)。
    """
    action = item.get("action")
    source = item.get("source")
    reason = item.get("reason")
    if action == KEY_MISSING:
        if reason == "not_final":
            return UNRESOLVED_WITHHELD
        if source == SOURCE_WEATHER and reason in ("not_published", "nodata"):
            return UNRESOLVED_WEATHER_PENDING
        if source == SOURCE_WEATHER and reason == "incomplete_day":
            return UNRESOLVED_WEATHER_INCOMPLETE
    elif action == KEY_FAILED:
        if source == SOURCE_GITHUB and reason in ("source_incomplete", "source_failed"):
            return UNRESOLVED_GITHUB_INCOMPLETE
    return None


#: 健康 source の内部名 (アクティビティ詳細を含む)。
_HEALTH_NAMES: "frozenset[str]" = frozenset(_OBSIDIAN_HEALTH_FIELDS) | {"activity_detail"}


def _provisional_names(
    d: date, today: date, health_result: "HealthLoadResult | None",
) -> "frozenset[str]":
    """この実行で書く値のうち暫定値になる内部名。

    今日 (まだ終わっていない日) は健康 (アクティビティ詳細を含む) と GitHub の全部。
    終わった日は、健康の結果が日全体で暫定 (途中経過の出力) なら健康の全部、一部の
    フィールドだけ暫定 (segment 形式の夜主睡眠が前日ファイル不足) ならそのフィールドだけ。
    """
    if d >= today:
        return _HEALTH_NAMES | {"github"}
    if health_result is None or not health_result.provisional:
        return frozenset()
    fields = getattr(health_result, "provisional_fields", None)
    if fields is None:
        return _HEALTH_NAMES
    return frozenset(fields)


def _final_sources(
    d: date,
    today: date,
    states: "Mapping[str, _SourceState]",
    health_result: "HealthLoadResult | None",
) -> "frozenset[str]":
    """この日の結果が確定していて、値が無いキーは「欠測で確定」と言える source。

    - 健康: 終わった日で、取得が完全 (``complete``) かつ日全体が暫定ではない
      (``partial`` は読めなかったフィールドがありうるので含めない)
    - 天気: 気象庁のその日のページが完成している (欠測キーは今後も埋まらない)
    - GitHub: 終わった日で、収集が完全
    """
    final: "set[str]" = set()
    if d < today:
        health_state = states.get(SOURCE_HEALTH)
        whole_day_provisional = bool(
            health_result is not None and health_result.provisional
            and getattr(health_result, "provisional_fields", None) is None
        )
        if (
            health_state is not None and health_state.status == STATUS_COMPLETE
            and health_result is not None and not whole_day_provisional
        ):
            final.add(SOURCE_HEALTH)
        github_state = states.get(SOURCE_GITHUB)
        if github_state is not None and github_state.status == STATUS_COMPLETE:
            final.add(SOURCE_GITHUB)
    weather_state = states.get(SOURCE_WEATHER)
    if weather_state is not None and weather_state.info.get("is_complete") is True:
        final.add(SOURCE_WEATHER)
    return frozenset(final)


#: 書いた後のノートの現在値を確かめてよい (ノートがあり、書けた / 書く必要が無かった) status。
_PROBE_STATUSES = frozenset({_NOTE_UPDATED, _NOTE_UNCHANGED, _NOTE_CREATED, _NOTE_WOULD_UPDATE})


def _probe_note(
    run: _DiaryRun,
    publisher: Any,
    outcome: "NotePublishOutcome | None",
    write_error: "str | None",
    tracked: "Mapping[str, LedgerItem]",
    keys: "Mapping[str, Mapping[str, Any]]",
) -> "dict[str, str]":
    """追跡中のキーのうち、この実行で書く値が無かったもの / コメント・重複キーのため書き換え
    られなかった (``kept`` + warning) ものについて、ノートの現在値の状態を読む (読み取りのみ)。

    利用者が手で値を書いた (= 再処理しても上書きしない値がある) キーを台帳から外すのに使う。
    書き込みに失敗した / ノートが無い・書ける状態でない日は読まない (判定しない)。
    """
    if write_error is not None or outcome is None or outcome.path is None:
        return {}
    if outcome.status not in _PROBE_STATUSES:
        return {}
    probe = {
        key: item.value for key, item in tracked.items()
        if key in keys and (
            keys[key].get("action") in (KEY_MISSING, KEY_FAILED, KEY_SKIPPED)
            or (keys[key].get("action") == ACTION_KEPT and keys[key].get("warning"))
        )
    }
    if not probe:
        return {}
    reader = getattr(publisher, "existing_states", None)
    if reader is None:
        return {}
    try:
        return dict(reader(outcome.path, probe))
    except Exception:  # noqa: BLE001 — 判定できないだけ (追跡を続ける)
        run.logger.exception("日記ノートの現在値を確認できません: %s", outcome.path)
        return {}


def _carry_notified(previous: "LedgerItem | None", reason: str) -> bool:
    return previous is not None and previous.reason == reason and previous.notified


def _unresolved_after(
    tracked: "Mapping[str, LedgerItem]",
    keys: "Mapping[str, Mapping[str, Any]]",
    updates: "Mapping[str, object]",
    provisional_names: "frozenset[str] | set[str]",
    *,
    now: datetime,
    final_sources: "frozenset[str] | set[str]" = frozenset(),
    deselected_sources: "frozenset[str] | set[str]" = frozenset(),
    note_states: "Mapping[str, str] | None" = None,
) -> "tuple[dict[str, LedgerItem], dict[str, str]]":
    """この日の処理結果から、まだ確定していない (台帳で追跡する) キーを求める。

    書いた (insert / update / unchanged) キー:

    - 暫定の値を書いた / 既存値が同じだった → ``provisional`` (書いた値を記録)
    - 確定値を書いた / 既存値が確定値と同じだった → 確定 (台帳から外す)

    保持された (``kept``) キー:

    - コメント / 重複キーのため書き換えられず、以前に書いた暫定値がノートに残りうる
      (値が暫定値のまま / 重複で判定できない / 空) → ``unwritable`` (暫定値を引き継いで
      追跡を続け、3 日を超えたら要確認で知らせる)
    - それ以外 (fill-only で利用者の値を保持した / 利用者が値も書き換えた / 本ツールの値を
      書いていない entry) → 外す

    書く値が無かったキー (以前から追跡しているもの):

    - ノートに利用者の値がある (空でも本ツールの暫定値でもない。再処理しても fill-only で
      上書きしない値) → 外す (``user_value``。利用者が手で記入すれば追跡が終わる)
    - 保留 / 天気の公開待ち・未完成 / GitHub の収集が不完全 → その理由で記録 (以前に書いた
      暫定値があれば、後で置き換えられるよう値は引き継ぐ。新しく見つけたキーも記録する)
    - source を設定から外した → 外す (``source_deselected``。今後も確定しない)
    - 確定した source にその日の値が無い (欠測で確定) → 本ツールの暫定値がノートに残って
      いなければ外す (``no_value_final``)。残っていれば追跡を続ける
    - それ以外 (取得失敗 / 書き込み失敗 / 未出力 / ``--sources`` で一時的に外した等) →
      以前の記録のまま (次の実行で確定を待つ)

    初回検出日時 (``since``) は、以前から追跡しているキーなら引き継ぐ。日記のキーで
    なくなった (property の override を変えた) キーの記録は捨てる。

    Returns:
        ``(キー → 追跡する項目, 外したキー → 外した理由 (RESOLUTION_*))``。
        書いて確定したキーは外した理由に含めない。
    """
    result: "dict[str, LedgerItem]" = {}
    resolutions: "dict[str, str]" = {}
    states = note_states or {}
    for key, item in keys.items():
        previous = tracked.get(key)
        since = previous.since if previous is not None else now
        action = item.get("action")
        if action in (ACTION_INSERT, ACTION_UPDATE, ACTION_UNCHANGED):
            if item.get("field") in provisional_names and key in updates:
                result[key] = LedgerItem(UNRESOLVED_PROVISIONAL, since=since, value=updates[key])
            continue
        if action == ACTION_KEPT:
            if (
                item.get("warning") and previous is not None and previous.value is not None
                and states.get(key) != EXISTING_OTHER
            ):
                # 以前に書いた暫定値の entry が、コメント付き / 重複キーになって書き換えられない
                result[key] = LedgerItem(
                    UNRESOLVED_UNWRITABLE, since=since, value=previous.value,
                    notified=_carry_notified(previous, UNRESOLVED_UNWRITABLE),
                )
            elif previous is not None:
                resolutions[key] = RESOLUTION_USER_VALUE
            continue
        if previous is not None and states.get(key) == EXISTING_OTHER:
            resolutions[key] = RESOLUTION_USER_VALUE
            continue
        reason = _pending_reason(item)
        if reason is not None:
            value = previous.value if previous is not None else None
            result[key] = LedgerItem(
                reason, since=since, value=value, notified=_carry_notified(previous, reason),
            )
            continue
        if previous is None:
            continue
        source = item.get("source")
        if item.get("reason") == "source_not_selected" and source in deselected_sources:
            resolutions[key] = RESOLUTION_SOURCE_DESELECTED
            continue
        if item.get("reason") == "no_value" and source in final_sources and (
            previous.value is None or states.get(key) == EXISTING_EMPTY
        ):
            resolutions[key] = RESOLUTION_NO_VALUE
            continue
        result[key] = previous
    return result, resolutions


def _is_ledger_value(value: object) -> bool:
    return isinstance(value, _LEDGER_VALUE_TYPES) and not isinstance(value, bool)


def _parse_since(raw: object) -> "datetime | None":
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _ledger_item(raw: object) -> "LedgerItem | None":
    """台帳の 1 キー分を読む (形式 1 = 暫定値そのもの / 形式 2 = ``{reason, since, value}``)。"""
    if isinstance(raw, dict):
        reason = raw.get("reason")
        if not isinstance(reason, str) or not reason:
            return None
        value = raw.get("value")
        return LedgerItem(
            reason,
            since=_parse_since(raw.get("since")),
            value=value if _is_ledger_value(value) else None,
            notified=raw.get("notified") is True,
        )
    if _is_ledger_value(raw):
        # 形式 1 (暫定値だけを記録していた): 初回検出日時は不明 (対象日から数える)
        return LedgerItem(UNRESOLVED_PROVISIONAL, since=None, value=raw)
    return None


def _load_unreported(raw: object) -> "dict[str, dict[date, dict[str, str]]]":
    """台帳の ``unreported`` (送れなかった一回きりの通知) を読む。不正な部分は捨てる。"""
    result: "dict[str, dict[date, dict[str, str]]]" = {}
    if not isinstance(raw, dict):
        return result
    for kind in _PERSISTED_ONE_SHOTS:
        days = raw.get(kind)
        if not isinstance(days, dict):
            continue
        for raw_date, keys in days.items():
            try:
                d = date.fromisoformat(str(raw_date))
            except ValueError:
                continue
            if not isinstance(keys, dict):
                continue
            valid = {
                key: reason for key, reason in keys.items()
                if isinstance(key, str) and isinstance(reason, str) and reason
            }
            if valid:
                result.setdefault(kind, {})[d] = valid
    return result


def _load_ledger_state(path: Path, logger: logging.Logger) -> _LedgerState:
    """台帳を読む。無い / 壊れている → 空 (壊れていたら warning。次の保存で作り直す)。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _LedgerState()
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("未確定の値の台帳を読めません (空として続行): %s (%s)", path, exc)
        return _LedgerState()
    try:
        data = json.loads(raw)
    except ValueError as exc:
        logger.warning("未確定の値の台帳が壊れています (作り直します): %s (%s)", path, exc)
        return _LedgerState()
    entries = data.get("dates") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        logger.warning("未確定の値の台帳の形式が不正です (作り直します): %s", path)
        return _LedgerState()
    ledger: "dict[date, dict[str, LedgerItem]]" = {}
    for raw_date, entry in entries.items():
        try:
            d = date.fromisoformat(str(raw_date))
        except ValueError:
            continue
        values = entry.get("keys") if isinstance(entry, dict) else None
        if not isinstance(values, dict):
            continue
        kept: "dict[str, LedgerItem]" = {}
        for key, raw_item in values.items():
            item = _ledger_item(raw_item) if isinstance(key, str) else None
            if item is not None:
                kept[key] = item
        if kept:
            ledger[d] = kept
    return _LedgerState(dates=ledger, unreported=_load_unreported(data.get("unreported")))



def _iso_utc(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _ledger_item_json(item: LedgerItem) -> "dict[str, object]":
    data: "dict[str, object]" = {
        "reason": item.reason,
        "since": _iso_utc(item.since) if item.since is not None else None,
    }
    if item.value is not None:
        data["value"] = item.value
    if item.notified:
        data["notified"] = True
    return data


def _ledger_payload(
    ledger: "Mapping[date, Mapping[str, LedgerItem]]",
    now: datetime,
    unreported: "Mapping[str, Mapping[date, Mapping[str, str]]] | None" = None,
) -> str:
    document: "dict[str, object]" = {
        "version": _LEDGER_VERSION,
        "updated_at": _iso_utc(now),
        "dates": {
            d.isoformat(): {
                "keys": {key: _ledger_item_json(item) for key, item in ledger[d].items()},
            }
            for d in sorted(ledger)
        },
    }
    pending = {
        kind: {d.isoformat(): dict(days[d]) for d in sorted(days) if days[d]}
        for kind, days in (unreported or {}).items()
    }
    pending = {kind: days for kind, days in pending.items() if days}
    if pending:
        # 送れなかった (Reminders を作れなかった) 一回きりの通知: 次の実行で改めて送る
        document["unreported"] = pending
    return json.dumps(document, ensure_ascii=False, indent=1)


def _reasons(entry: "Mapping[str, LedgerItem]") -> "dict[str, str]":
    """台帳の 1 日分 → キー → 理由 (レポート・通知用。キーの並びは記録した順のまま)。"""
    return {key: item.reason for key, item in entry.items()}


def _is_overdue(item: LedgerItem, d: date, today: date, tz: Any) -> bool:
    """初回検出 (不明なら対象日) から :data:`PROVISIONAL_OVERDUE_DAYS` 日を超えたか (日記の tz の暦日で数える)。"""
    since_day = item.since.astimezone(tz).date() if item.since is not None else d
    return (today - since_day).days > PROVISIONAL_OVERDUE_DAYS


def _ledger_info_unchanged(run: _DiaryRun) -> "dict[str, Any]":
    """日記を処理しなかった (台帳を更新しない) 実行のレポート用の台帳の情報。"""
    return {
        "path": str(run.config.diary_state_path()),
        "unresolved": {d.isoformat(): _reasons(run.ledger[d]) for d in sorted(run.ledger)},
        "overdue": {},
        "expired": {},
        "untracked": {},
        "saved": False,
        "skipped": "not_processed",
    }


def _commit_ledger(run: _DiaryRun) -> "dict[str, Any]":
    """この実行の結果を台帳に反映して保存し、確定しないまま古くなったキーを返す。

    保存直前に読み直し、この実行で処理した日だけを書き換える (並行して走った実行が
    記録した他の日・送れなかった一回きりの通知を消さない)。:data:`PROVISIONAL_REVISIT_DAYS`
    を超えた日は台帳から外す。``--dry-run`` では保存しない (見込みだけ返す)。保存に失敗
    したら失敗として報告する (未確定の値の追跡が黙って止まるのを防ぐ)。

    データの鮮度の通知に載せる項目は ``run.notice_items`` に置く: 3 日を超えて確定しない値
    (1 回だけ知らせる理由 :data:`NOTIFY_ONCE_REASONS` は、まだ知らせていないものだけを
    一回きりの項目に)、台帳から外した日・30 日より前の未確定のキー (一回きり)、以前の実行で
    送れなかった一回きりの項目 (台帳の ``unreported``)。

    Returns:
        レポートの ``provisional``: ``unresolved`` (保存後の台帳: 日付 → キー → 理由) /
        ``overdue`` (初回検出から 3 日を超えたキー。1 回だけ知らせる理由で知らせ済みのものも
        含む) / ``expired`` (30 日を過ぎて外した日) / ``untracked`` (30 日より前の日付のため
        台帳に入れなかった未確定のキー) / ``carried_over`` (以前の実行で送れなかった一回きりの
        通知。あれば) / ``saved`` / ``error`` (保存失敗時)
    """
    path = run.config.diary_state_path()
    today = run.today
    if run.options.dry_run:
        current = _LedgerState(
            dates=dict(run.ledger),
            unreported={kind: dict(days) for kind, days in run.ledger_unreported.items()},
        )
    else:
        current = _load_ledger_state(path, run.logger)
    merged = dict(current.dates)
    for d, entry in run.ledger_updates.items():
        if entry:
            merged[d] = dict(entry)
        else:
            merged.pop(d, None)
    expired_entries = {
        d: entry for d, entry in merged.items() if (today - d).days > PROVISIONAL_REVISIT_DAYS
    }
    for d in sorted(expired_entries):
        merged.pop(d, None)
        run.logger.warning(
            "日記 %s の未確定の値 (%s) は %d 日以内に確定しなかったため、自動の再処理をやめます。"
            "確定値で上書きするには --date %s を実行してください",
            d, ", ".join(expired_entries[d]), PROVISIONAL_REVISIT_DAYS, d,
        )
    overdue: "dict[date, dict[str, LedgerItem]]" = {}
    for d, entry in merged.items():
        late = {
            key: item for key, item in entry.items()
            if _is_overdue(item, d, today, run.config.diary_tz)
        }
        if late:
            overdue[d] = late
    if run.untracked:
        run.logger.warning(
            "%d 日分の値がまだ確定していませんが、%d 日より前の日付のため自動では処理し直しません "
            "(実行レポートの provisional.untracked を参照): %s",
            len(run.untracked), PROVISIONAL_REVISIT_DAYS, _format_dates(list(run.untracked)),
        )
    run.notice_items = _notice_items(run, overdue, expired_entries, current.unreported)
    info: "dict[str, Any]" = {
        "path": str(path),
        "unresolved": {d.isoformat(): _reasons(merged[d]) for d in sorted(merged)},
        "overdue": {d.isoformat(): _reasons(overdue[d]) for d in sorted(overdue)},
        "expired": {d.isoformat(): _reasons(expired_entries[d]) for d in sorted(expired_entries)},
        "untracked": {d.isoformat(): dict(run.untracked[d]) for d in sorted(run.untracked)},
        "saved": False,
    }
    if current.unreported:
        info["carried_over"] = {
            kind: {d.isoformat(): dict(days[d]) for d in sorted(days)}
            for kind, days in current.unreported.items()
        }
    if run.options.dry_run:
        return info
    if merged == current.dates and (merged or current.unreported or not path.exists()):
        return info
    try:
        _write_text_atomic(path, _ledger_payload(merged, run.deps.clock(), current.unreported))
    except OSError as exc:
        run.logger.error("未確定の値の台帳を保存できません: %s (%s)", path, exc)
        info["error"] = _short(f"{type(exc).__name__}: {exc}")
        run.fail(
            None, SOURCE_OBSIDIAN,
            f"暫定値の台帳 ({path.name}) を保存できません。暫定値・保留した値・公開待ちの天気・"
            f"取得が不完全な GitHub が後の実行で確定値に置き換わらない恐れがあります: "
            f"{type(exc).__name__}: {exc}",
            kind=FAILURE_RECORD,
        )
        return info
    info["saved"] = True
    return info


def _notice_items(
    run: _DiaryRun,
    overdue: "Mapping[date, Mapping[str, LedgerItem]]",
    expired: "Mapping[date, Mapping[str, LedgerItem]]",
    unreported: "Mapping[str, Mapping[date, Mapping[str, str]]]",
) -> _NoticeItems:
    """データの鮮度の通知に載せる未確定の値を、毎日知らせるものと一回きりのものに分ける。"""
    daily: "dict[date, dict[str, str]]" = {}
    shots: "list[_OneShot]" = []
    for d in sorted(overdue):
        every_day = {
            key: item.reason for key, item in overdue[d].items()
            if item.reason not in NOTIFY_ONCE_REASONS
        }
        if every_day:
            daily[d] = every_day
        once = tuple(
            (key, item.reason) for key, item in overdue[d].items()
            if item.reason in NOTIFY_ONCE_REASONS and not item.notified
        )
        if once:
            shots.append(_OneShot(ONE_SHOT_OVERDUE, d, once))
    for d in sorted(expired):
        # 1 回だけ知らせる理由で知らせ済みのキーは、追跡をやめるときにはもう知らせない
        keys = tuple((key, item.reason) for key, item in expired[d].items() if not item.notified)
        if keys:
            shots.append(_OneShot(ONE_SHOT_EXPIRED, d, keys))
    for d in sorted(run.untracked):
        shots.append(_OneShot(ONE_SHOT_UNTRACKED, d, tuple(run.untracked[d].items())))
    index = {shot.signature(): position for position, shot in enumerate(shots)}
    for kind in _PERSISTED_ONE_SHOTS:
        days = unreported.get(kind) or {}
        for d in sorted(days):
            shot = _OneShot(kind, d, tuple(days[d].items()), carried=True)
            position = index.get(shot.signature())
            if position is None:
                index[shot.signature()] = len(shots)
                shots.append(shot)
            else:
                # この実行でも同じ内容を見つけた: 1 回だけ載せ、送れたら繰り越しも消す
                shots[position] = replace(shots[position], carried=True)
    return _NoticeItems(daily_overdue=daily, one_shots=tuple(shots))


def _settle_one_shots(
    run: _DiaryRun, *, due: "Sequence[_OneShot]", delivered: "bool | None",
) -> None:
    """一回きりの通知項目の後始末を台帳に反映する (``--dry-run`` では何もしない)。

    - 送れた項目と、今日すでに送っていたため今回は載せなかった項目: 以前の実行から繰り越した
      ものは台帳の ``unreported`` から消し、1 回だけ知らせる理由の「3 日超」は「知らせ済み」
      (:attr:`LedgerItem.notified`) にする
    - リマインダーを作れなかった (``delivered=False``) 項目: 台帳から外した日・30 日より前の
      未確定のキーは ``unreported`` に残して次の実行で改めて送る (それ以外の項目は台帳に
      残っているので、次の実行でまた載る)

    Args:
        due: 今回の通知に載せた一回きりの項目
        delivered: True = 送れた / False = 作れなかった / None = 送っていない
    """
    if run.options.dry_run:
        return
    shots = run.notice_items.one_shots
    if not shots:
        return
    due_signatures = {shot.signature() for shot in due}
    done = [
        shot for shot in shots
        if shot.signature() not in due_signatures or delivered is True
    ]
    failed = [
        shot for shot in shots
        if shot.signature() in due_signatures and delivered is False
    ]
    mark = [shot for shot in done if shot.kind == ONE_SHOT_OVERDUE]
    clear = [shot for shot in done if shot.carried]
    keep = [shot for shot in failed if shot.kind in _PERSISTED_ONE_SHOTS and not shot.carried]
    if not (mark or clear or keep):
        return
    path = run.config.diary_state_path()
    state = _load_ledger_state(path, run.logger)
    changed = False
    for shot in mark:
        entry = state.dates.get(shot.date)
        if entry is None:
            continue
        for key, reason in shot.keys:
            item = entry.get(key)
            if item is not None and item.reason == reason and not item.notified:
                entry[key] = replace(item, notified=True)
                changed = True
    for shot in clear:
        days = state.unreported.get(shot.kind) or {}
        keys = days.get(shot.date)
        if not keys:
            continue
        for key, _ in shot.keys:
            if keys.pop(key, None) is not None:
                changed = True
        if not keys:
            days.pop(shot.date, None)
    for shot in keep:
        bucket = state.unreported.setdefault(shot.kind, {}).setdefault(shot.date, {})
        for key, reason in shot.keys:
            if bucket.get(key) != reason:
                bucket[key] = reason
                changed = True
    if not changed:
        return
    try:
        _write_text_atomic(path, _ledger_payload(state.dates, run.deps.clock(), state.unreported))
    except OSError as exc:
        # 送れなかった一回きりの通知を残せないと失われる / 送れた通知を消せないと再送される
        run.logger.error(
            "未確定の値の台帳に通知の結果を記録できません: %s (%s)", path, exc,
        )


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
    failures: "Sequence[DiaryFailure]", run_dates: "Sequence[date]", report_hint: "str | None",
) -> str:
    """失敗の通知本文。``report_hint`` は実行レポートの場所 (書けなかったら途中経過の場所)。"""
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
    if report_hint is not None:
        lines.append(f"詳細: {report_hint}")
    lines.append("ログ: logs/run.log")
    return "\n".join(lines)


def _decide_exit(run: _DiaryRun, days: "Sequence[DayResult]") -> "tuple[int, AppError | None]":
    failures = run.failures
    if not failures:
        return EXIT_OK, None
    span = _format_dates(run.options.dates)
    write_failures = [f for f in failures if f.kind == FAILURE_WRITE]
    selected = [s for s in SOURCE_NAMES if run.selected(s)]
    all_failed = bool(selected) and bool(days) and all(
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
    elif all(f.kind == FAILURE_RECORD for f in failures):
        what = (
            "実行レポート" if any(f.source == SOURCE_REPORT for f in failures)
            else "未確定の値の台帳"
        )
        error = DiaryPartialFailureError(
            f"日記の{what}を保存できません",
            title=f"ihealth-relay: 日記の{what}を保存できません",
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
    """同じディレクトリの隠し tmp に書いて fsync し、``os.replace`` で置き換える。

    読む側からは「前の内容」か「新しい内容」のどちらかしか見えない (書きかけが残らない)。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass  # 置き換え済み (FileNotFoundError) / 消せない tmp は害が無い


def _append_jsonl(path: Path, item: "Mapping[str, Any]") -> None:
    """JSON 1 行を追記して fsync する (journal 用。行単位で追記するので、途中で落ちても
    それまでの行は壊れない。最後の行が書きかけになりうるだけ)。"""
    line = json.dumps(item, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
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


def _freshness_sent_today(logs_dir: "Path | None", today: date) -> "set[str]":
    """今日すでに送った一回きりの「要確認」の署名 (best-effort: 読めなければ空)。"""
    if logs_dir is None:
        return set()
    data = _read_marker(logs_dir / _FRESHNESS_MARKER)
    if not data or data.get("date") != today.isoformat():
        return set()
    signatures = data.get("signatures")
    if not isinstance(signatures, list):
        return set()
    return {item for item in signatures if isinstance(item, str)}


def _remember_freshness(
    logs_dir: "Path | None", today: date, signatures: "Sequence[str]", logger: logging.Logger,
) -> None:
    if logs_dir is None or not signatures:
        return
    path = logs_dir / _FRESHNESS_MARKER
    data = _read_marker(path) or {}
    sent = data.get("signatures") if data.get("date") == today.isoformat() else None
    if not isinstance(sent, list):
        sent = []
    for signature in signatures:
        if signature not in sent:
            sent.append(signature)
    try:
        _write_text_atomic(path, json.dumps({
            "date": today.isoformat(), "signatures": sent[-_MAX_MARKER_SIGNATURES:],
        }))
    except OSError as exc:
        logger.warning("通知済みの記録を書けません (同じ内容を再送することがあります): %s (%s)", path, exc)


def _create_exclusive(path: Path) -> bool:
    """空ファイルを排他的に作る (既にあれば False)。その他の OSError はそのまま投げる。"""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def _reserve_report_paths(
    logs_dir: Path, now: datetime, *, journal: bool = True,
) -> "tuple[Path, Path | None]":
    """実行レポート (``.json``) と journal (``.jsonl``) の名前を排他的に確保する。

    同じ秒に走った別の実行 (launchd と手動など) と同じ名前を掴まないよう、空ファイルを
    ``O_EXCL`` で作って予約する (使われていれば ``-1``, ``-2`` … の接尾辞)。

    Raises:
        OSError: logs ディレクトリを作れない / ファイルを作れない。
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    stamp = moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for n in range(1000):
        stem = f"{_REPORT_PREFIX}{stamp}" + (f"-{n}" if n else "")
        report_path = logs_dir / f"{stem}.json"
        if not _create_exclusive(report_path):
            continue
        if not journal:
            return report_path, None
        journal_path = logs_dir / f"{stem}.jsonl"
        try:
            created = _create_exclusive(journal_path)
        except OSError:
            _unlink_quietly(report_path)
            raise
        if created:
            return report_path, journal_path
        _unlink_quietly(report_path)
    raise OSError(f"実行レポートの名前を確保できません: {logs_dir}/{_REPORT_PREFIX}{stamp}-*.json")


def _prune_reports(logs_dir: Path, now: datetime, logger: logging.Logger) -> None:
    """保持期間 (30 日) より古い実行レポート (と残った journal) を消す (best-effort)。"""
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
        "unresolved": dict(day.unresolved),
        "tracked": day.tracked,
        "note": note,
        "sources": {name: dict(detail) for name, detail in day.source_details.items()},
        "written_keys": list(day.written_keys),
        "keys": {key: dict(entry) for key, entry in day.keys.items()},
    }


def _failure_item(failure: DiaryFailure) -> "dict[str, Any]":
    return {
        "date": failure.date.isoformat() if failure.date else None,
        "source": failure.source,
        "kind": failure.kind,
        "message": failure.message,
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


def _unresolved_label(keys: "Mapping[str, str]") -> str:
    """キー → 理由 を ``歩数 (歩), 睡眠時間 (分) (暫定値) / Github (GitHub の取得が不完全)`` に。"""
    groups: "dict[str, list[str]]" = {}
    for key, reason in keys.items():
        groups.setdefault(reason, []).append(key)
    return " / ".join(
        f"{', '.join(names)} ({_UNRESOLVED_LABELS.get(reason, reason)})"
        for reason, names in groups.items()
    )


def _unresolved_hints(reasons: "set[str]") -> "list[str]":
    """未確定の理由ごとの、利用者が確かめること。"""
    hints: "list[str]" = []
    if UNRESOLVED_PROVISIONAL in reasons:
        hints.append(
            "暫定値: Health Auto Export がその日のファイルを日付が変わった後に書き直すと自動で"
            f"置き換わります ({PROVISIONAL_REVISIT_DAYS} 日以内)。iPhone で Health Auto Export を"
            "開き、オートメーションの書き出し期間に過去の日が含まれているか確認してください。"
        )
    if UNRESOLVED_WITHHELD in reasons:
        hints.append(
            "保留: 停止済みの AutoSync にその日の途中までの出力しか無いため書いていません "
            "(AutoSync は再出力されないので、ほとんどの場合自動では埋まりません)。必要なら"
            "日記に手で記入してください (記入すると追跡をやめます)。この項目はこの 1 回だけ知らせます。"
        )
    if reasons & {UNRESOLVED_WEATHER_PENDING, UNRESOLVED_WEATHER_INCOMPLETE}:
        hints.append(
            "天気: 気象庁のページにその日の観測が揃うと自動で埋まります "
            f"({PROVISIONAL_REVISIT_DAYS} 日以内)。揃わないままなら気象庁の欠測の可能性が"
            "あります (必要なら手で記入してください。記入すると追跡をやめます)。"
        )
        if UNRESOLVED_WEATHER_INCOMPLETE in reasons:
            hints.append("天気が未完成の日は、この 1 回だけ知らせます。")
    if UNRESOLVED_GITHUB_INCOMPLETE in reasons:
        hints.append(
            "GitHub: 活動を取りきれていません (実行レポートの github.errors を確認して"
            f"ください)。取りきれると自動で埋まります ({PROVISIONAL_REVISIT_DAYS} 日以内)。"
        )
    if UNRESOLVED_UNWRITABLE in reasons:
        hints.append(
            "書き換えられない: 日記ノートのその項目にコメントがある、または同じ項目が 2 つ"
            "あるため、本ツールが書いた暫定値を確定値に置き換えられません。コメントを項目の上の"
            "行に移すか、項目を 1 つにまとめると、次の実行で置き換わります。"
        )
    return hints


def _day_lines(days: "Mapping[date, Mapping[str, str]]") -> "list[str]":
    lines = [
        f"  {d.isoformat()}: {_unresolved_label(days[d])}"
        for d in sorted(days)[:_MAX_NOTIFY_GROUPS]
    ]
    if len(days) > _MAX_NOTIFY_GROUPS:
        lines.append(f"  …ほか {len(days) - _MAX_NOTIFY_GROUPS} 日")
    return lines


def _unresolved_body(
    daily: "Mapping[date, Mapping[str, str]]", shots: "Sequence[_OneShot]",
) -> str:
    """「要確認」の通知本文: 3 日を超えて確定しない値 (毎日知らせるもの + 1 回だけ知らせる
    理由のまだ知らせていないもの) / 追跡をやめた (30 日超) 日 / 30 日より前の日付のため追跡
    しない未確定の値。"""
    lines: "list[str]" = []
    overdue: "dict[date, dict[str, str]]" = {d: dict(keys) for d, keys in daily.items()}
    expired: "dict[date, dict[str, str]]" = {}
    untracked: "dict[date, dict[str, str]]" = {}
    for shot in shots:
        target = {ONE_SHOT_OVERDUE: overdue, ONE_SHOT_EXPIRED: expired}.get(shot.kind, untracked)
        target.setdefault(shot.date, {}).update(shot.keys)
    if overdue:
        lines.append(
            f"要確認: 次の日の値が {PROVISIONAL_OVERDUE_DAYS} 日を過ぎても確定していません:"
        )
        lines.extend(_day_lines(overdue))
        lines.extend(_unresolved_hints({
            reason for keys in overdue.values() for reason in keys.values()
        }))
    if expired:
        lines.append(
            f"次の日は {PROVISIONAL_REVISIT_DAYS} 日以内に確定しなかったため自動の再処理を"
            "やめました (値は暫定値・未確定のままです。--date <日付> で処理し直せます):"
        )
        lines.extend(_day_lines(expired))
    if untracked:
        lines.append(
            f"次の日の値はまだ確定していませんが、{PROVISIONAL_REVISIT_DAYS} 日より前の日付のため"
            "自動では処理し直しません (確定したら --date <日付> で書けます。一覧は実行レポートの"
            " provisional.untracked):"
        )
        lines.extend(_day_lines(untracked))
    return "\n".join(lines)


@dataclass(frozen=True)
class _Freshness:
    """データの鮮度の通知の中身。"""

    title: str
    body: str
    #: 毎日知らせる部分 (停止の疑い / 出力が見つからない / 3 日超の値) を載せた
    include_recurring: bool
    #: 載せた一回きりの項目 (今日まだ送っていないもの)
    one_shots: "tuple[_OneShot, ...]"
    #: 載せるものが無い (どれも今日すでに送った)
    suppressed: bool


def _freshness_notice(
    run: _DiaryRun,
    stale: "Mapping[str, Any] | None",
    *,
    recurring_sent: bool,
    sent_signatures: "set[str]",
) -> "_Freshness | None":
    """データの鮮度の通知 (停止の疑い / 出力が見つからない / 確定しない値) を 1 通に組む。

    毎日知らせる部分は 1 日 1 回 (``recurring_sent`` = 今日すでに送った)、一回きりの項目は
    今日まだ送っていないもの (``sent_signatures`` に無いもの) だけを載せる。朝に停止の疑いを
    知らせた日でも、後の実行で見つかった一回きりの項目 (backfill した古い日の未確定の値など)
    は知らせる。
    """
    recurring: "list[str]" = []
    title: "str | None" = None
    if stale is not None and stale.get("stale"):
        title = "ihealth-relay: HAE オートメーション停止の疑い"
        recurring.append(_stale_body(stale))
    if stale is not None and stale.get("missing"):
        title = title or "ihealth-relay: HAE オートメーション出力が見つかりません"
        recurring.append(_missing_automation_body(stale, run.config.health_export_dir))
    items = run.notice_items
    has_recurring = bool(recurring) or bool(items.daily_overdue)
    if not has_recurring and not items.one_shots:
        return None
    include_recurring = has_recurring and not recurring_sent
    due = tuple(shot for shot in items.one_shots if shot.signature() not in sent_signatures)
    suppressed = not include_recurring and not due
    if suppressed:
        # 載せるものが無い (どれも今日すでに送った)。レポート用に全体から件名を決める
        daily: "Mapping[date, Mapping[str, str]]" = items.daily_overdue
        shown: "tuple[_OneShot, ...]" = items.one_shots
    else:
        daily = items.daily_overdue if include_recurring else {}
        shown = due
    reasons = {reason for keys in daily.values() for reason in keys.values()}
    reasons |= {reason for shot in shown for _, reason in shot.keys}
    if reasons <= {UNRESOLVED_PROVISIONAL}:
        fallback = "ihealth-relay: 日記の暫定値が確定していません (要確認)"
    else:
        fallback = "ihealth-relay: 日記の値が確定していません (要確認)"
    if suppressed:
        return _Freshness(title or fallback, "", False, (), True)
    sections = list(recurring) if include_recurring else []
    if not include_recurring:
        title = None  # 停止の疑い等は今日すでに知らせた: 一回きりの項目だけを送る
    if daily or due:
        sections.append(_unresolved_body(daily, due))
    sections.append("(同じ内容の通知は 1 日 1 回までです。終了コードには影響しません)")
    return _Freshness(title or fallback, "\n\n".join(sections), include_recurring, due, False)


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
    #: 載せた一回きりの項目 (データの鮮度の通知のみ。送れたかどうかを台帳に反映する)
    one_shots: "tuple[_OneShot, ...]" = ()


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


def _dump_report(document: "Mapping[str, Any]") -> str:
    return json.dumps(document, ensure_ascii=False, indent=1, allow_nan=False)


def _report_header(run: _DiaryRun, revisit: "Sequence[date]") -> "dict[str, Any]":
    """実行レポートの共通部分 (開始時に書く。処理する予定の日付一覧を含む)。"""
    options = run.options
    planned = [d.isoformat() for d in options.dates]
    return {
        "version": _REPORT_VERSION,
        "status": REPORT_RUNNING,
        "started_at": _iso_utc(run.started_at),
        "finished_at": None,
        "publisher": "obsidian",
        "dry_run": options.dry_run,
        "fill_empty_only": options.fill_empty_only,
        "sources": list(options.sources),
        "today": run.today.isoformat(),
        "window": {
            "from": planned[0],
            "to": planned[-1],
            "days": len(planned),
        },
        "planned_dates": planned,
        "revisit_dates": [d.isoformat() for d in sorted(revisit)],
        "processed": 0,
        "exit_code": None,
    }


class _RunReport:
    """実行レポート (``logs/diary-report-<UTC>.json``) と、日付ごとの結果の journal (``.jsonl``)。

    - :meth:`start`: 処理する予定の日付一覧を入れたレポート (``status="running"``) と
      journal を作る。作れなければ日記を書き換える前に止める (W3-5)
    - :meth:`append`: journal に 1 行追記して fsync する (1 日処理するごと。行単位なので、
      途中でプロセスが落ちても処理済みの日の結果は残る)
    - :meth:`write_final`: 全日付の結果と summary を入れたレポートを原子的に書き、
      journal を消す (中身はレポートに入った)。書けなければ journal を残す

    1 日ごとにレポート全体を書き直さないのは、長い backfill (数百日) で書き込み量が
    日数の 2 乗で増えるため。``logs_dir`` が None なら何も書かない (失敗にもしない)。
    書けないときは例外を投げず、``error`` / ``final_error`` に理由を残して False を返す。
    """

    def __init__(self, logs_dir: "Path | None", logger: logging.Logger) -> None:
        self.logs_dir = logs_dir
        self.logger = logger
        self.header: "dict[str, Any]" = {}
        self.path: "Path | None" = None
        self.journal_path: "Path | None" = None
        #: 開始 / 追記の失敗の説明 (以降は追記しない)
        self.error: "str | None" = None
        #: 最後のレポートを書けなかった理由
        self.final_error: "str | None" = None
        self.final_written = False
        self._now: "datetime | None" = None

    @property
    def enabled(self) -> bool:
        return self.logs_dir is not None

    @property
    def ok(self) -> bool:
        """開始・追記で失敗していない (書かない設定でも True)。"""
        return self.error is None

    def start(self, header: "Mapping[str, Any]", *, now: datetime) -> bool:
        self.header = dict(header)
        self._now = now
        if self.logs_dir is None:
            return True
        try:
            self.path, self.journal_path = _reserve_report_paths(self.logs_dir, now)
            assert self.journal_path is not None
            document = dict(self.header)
            document["journal"] = self.journal_path.name
            document["dates"] = []
            _write_text_atomic(self.path, _dump_report(document))
            _append_jsonl(self.journal_path, {
                "type": "start",
                "report": self.path.name,
                "started_at": self.header.get("started_at"),
                "planned_dates": self.header.get("planned_dates"),
            })
        except (OSError, TypeError, ValueError) as exc:
            self.error = _short(f"{type(exc).__name__}: {exc}")
            self.logger.error("実行レポートを作成できません: %s", self.error)
            for path in (self.path, self.journal_path):
                if path is not None:
                    _unlink_quietly(path)
            self.path = self.journal_path = None
            return False
        self.logger.info("実行レポート: %s (日付ごとの結果を %s に追記)", self.path, self.journal_path.name)
        return True

    def append(self, item: "Mapping[str, Any]") -> bool:
        if self.logs_dir is None:
            return True
        if self.error is not None or self.journal_path is None:
            return False
        try:
            _append_jsonl(self.journal_path, item)
        except (OSError, TypeError, ValueError) as exc:
            self.error = _short(f"{type(exc).__name__}: {exc}")
            self.logger.error("実行レポートの journal に追記できません: %s (%s)", self.journal_path, self.error)
            return False
        return True

    def write_final(self, document: "Mapping[str, Any]") -> bool:
        if self.logs_dir is None:
            return True
        reserved_here = False
        try:
            if self.path is None:
                self.path, _ = _reserve_report_paths(
                    self.logs_dir, self._now or datetime.now(timezone.utc), journal=False,
                )
                reserved_here = True
            _write_text_atomic(self.path, _dump_report(document))
        except (OSError, TypeError, ValueError) as exc:
            self.final_error = _short(f"{type(exc).__name__}: {exc}")
            self.logger.error("実行レポートを保存できません: %s (%s)", self.path, self.final_error)
            if reserved_here and self.path is not None:
                _unlink_quietly(self.path)
                self.path = None
            return False
        self.final_written = True
        if self.journal_path is not None:
            _unlink_quietly(self.journal_path)  # 全日付の結果はレポートに入った
            self.journal_path = None
        self.logger.info("実行レポート: %s", self.path)
        return True

    def rewrite(self, document: "Mapping[str, Any]") -> None:
        """確定したレポートを書き直す (通知の成否の反映。best-effort)。"""
        if self.logs_dir is None or not self.final_written or self.path is None:
            return
        try:
            _write_text_atomic(self.path, _dump_report(document))
        except (OSError, TypeError, ValueError) as exc:
            self.logger.warning("実行レポートを更新できません: %s (%s)", self.path, exc)

    def hint(self) -> "str | None":
        """通知本文に載せるレポートの場所 (確定できなかったら途中経過の場所)。"""
        if self.path is None:
            return None
        if not self.final_written and self.journal_path is not None:
            return f"{self.path} (保存途中。日付ごとの結果: {self.journal_path.name})"
        return str(self.path)


def _plan_notices(
    run: _DiaryRun,
    error: "AppError | None",
    stale: "Mapping[str, Any] | None",
) -> "list[_Notice]":
    """送る (かもしれない) 通知。

    同じ失敗は 1 日 1 回 (``.failure_notified``)。データの鮮度は、毎日知らせる部分を 1 日 1 回
    (``.stale_notified``)、一回きりの項目を「今日まだ送っていないもの」だけ
    (``.freshness_notified``) 載せる。
    """
    logs_dir = run.config.logs_dir
    logger = run.logger
    today = run.today
    notices: "list[_Notice]" = []
    if error is not None:
        signature = _failure_signature(run.failures)
        notices.append(_Notice(
            kind="failure",
            title=error.title,
            body="",  # レポートの場所を入れるので保存後に組む
            suppressed=_already_notified(logs_dir, today, signature),
            remember=lambda: _remember_notified(logs_dir, today, signature, logger),
        ))
    freshness = _freshness_notice(
        run, stale,
        recurring_sent=_stale_already_notified(logs_dir, today),
        sent_signatures=_freshness_sent_today(logs_dir, today),
    )
    if freshness is not None:
        include_recurring = freshness.include_recurring
        signatures = [shot.signature() for shot in freshness.one_shots]

        def remember_freshness() -> None:
            if include_recurring:
                _remember_stale(logs_dir, today, logger)
            _remember_freshness(logs_dir, today, signatures, logger)

        notices.append(_Notice(
            kind="stale",
            title=freshness.title,
            body=freshness.body,
            suppressed=freshness.suppressed,
            remember=remember_freshness,
            one_shots=freshness.one_shots,
        ))
    return notices


def _notification_info(notices: "Sequence[_Notice]", *, dry_run: bool) -> "dict[str, Any]":
    info: "dict[str, Any]" = {}
    for notice in notices:
        item: "dict[str, Any]" = {"title": notice.title, "suppressed": notice.suppressed}
        if dry_run and not notice.suppressed:
            item["dry_run"] = True
        info[notice.kind] = item
    return info


def _final_document(
    run: _DiaryRun,
    report: _RunReport,
    days: "Sequence[DayResult]",
    *,
    stale: "Mapping[str, Any] | None",
    ledger_info: "Mapping[str, Any]",
    finished_at: datetime,
    exit_code: int,
    notification_info: "Mapping[str, Any]",
) -> "dict[str, Any]":
    """実行レポートの最終形 (backfill の全数台帳: 予定 N 日 = 処理済み + 未処理)。"""
    done = {day.date for day in days}
    remaining = [d.isoformat() for d in run.options.dates if d not in done]
    document = dict(report.header)
    document.update({
        "status": REPORT_INTERRUPTED if remaining else REPORT_FINISHED,
        "finished_at": _iso_utc(finished_at),
        "processed": len(days),
        "remaining_dates": remaining,
        "journal": None,
        "exit_code": exit_code,
        "github": run.github_summary,
        "stale_check": stale,
        "provisional": ledger_info,
        "failures": [_failure_item(f) for f in run.failures],
        "notifications": notification_info,
        "dates": [_day_report(day) for day in days],
    })
    return document


def _finish(
    run: _DiaryRun,
    days: "Sequence[DayResult]",
    stale: "dict[str, Any] | None",
    ledger_info: "dict[str, Any]",
    report: _RunReport,
) -> DiaryRunResult:
    """終了コードを決め、実行レポートを確定してから通知する。

    レポートを確定できなければ失敗 (exit 12 以上) にして、そのことも通知に含める
    (日付ごとの結果は journal に残る)。
    """
    logger = run.logger
    options = run.options
    finished_at = run.deps.clock()
    logs_dir = run.config.logs_dir

    exit_code, error = _decide_exit(run, days)
    notices = _plan_notices(run, error, stale)
    notification_info = _notification_info(notices, dry_run=options.dry_run)
    document = _final_document(
        run, report, days, stale=stale, ledger_info=ledger_info, finished_at=finished_at,
        exit_code=exit_code, notification_info=notification_info,
    )
    if not report.write_final(document):
        if not any(f.source == SOURCE_REPORT for f in run.failures):
            kept = (
                f" (日付ごとの結果は {report.journal_path.name} に残っています)"
                if report.journal_path is not None else ""
            )
            run.fail(
                None, SOURCE_REPORT,
                f"実行レポートを保存できません{kept}: {report.final_error}",
                kind=FAILURE_RECORD,
            )
        exit_code, error = _decide_exit(run, days)
        notices = _plan_notices(run, error, stale)
        notification_info = _notification_info(notices, dry_run=options.dry_run)
        document = _final_document(
            run, report, days, stale=stale, ledger_info=ledger_info, finished_at=finished_at,
            exit_code=exit_code, notification_info=notification_info,
        )
        report.append({
            "type": "finish",
            "exit_code": exit_code,
            "failures": document["failures"],
        })  # best-effort (journal が使えれば最後の状態を残す)
    if logs_dir is not None:
        _prune_reports(logs_dir, finished_at, logger)

    # ---- 通知 (失敗しても終了コードは変えない) ----
    notifier = run.deps.notifier or notify_failure
    notifications: "list[tuple[str, str]]" = []
    due: "list[_Notice]" = []
    for notice in notices:
        if notice.kind == "failure":
            notice.body = _failure_body(run.failures, options.dates, report.hint())
        if notice.suppressed:
            logger.info("同じ内容は今日すでに通知済みのため Reminders 通知を省略: %s", notice.title)
        else:
            due.append(notice)
    attempted = False
    delivered: "bool | None" = None
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
    # 一回きりの「要確認」: 送れたら台帳の繰り越しを消し / 1 回だけ知らせる項目を「知らせ済み」に、
    # 送れなかったら台帳に残して次の実行で改めて送る
    _settle_one_shots(
        run,
        due=[shot for notice in due if notice.kind == "stale" for shot in notice.one_shots],
        delivered=delivered,
    )
    if attempted:
        report.rewrite(document)  # 通知の成否を反映

    written_total = sum(len(day.written_keys) for day in days)
    summary_level = logging.INFO if exit_code == EXIT_OK else logging.WARNING
    logger.log(
        summary_level,
        "日記 完了: %d / %d 日 (%s), 書込%s %d キー, 失敗 %d 件, exit=%d",
        len(days), len(options.dates), _format_dates(options.dates),
        " 予定 (dry-run: 書き込みなし)" if options.dry_run else "",
        written_total, len(run.failures), exit_code,
    )
    return DiaryRunResult(
        exit_code=exit_code,
        dates=options.dates,
        days=tuple(days),
        failures=tuple(run.failures),
        report=document,
        report_path=report.path,
        notifications=tuple(notifications),
    )
