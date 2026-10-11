"""Obsidian Publisher: 日記ノート ``日記/**/YYYY-MM-DD.md`` の frontmatter を部分更新する.

``MarkdownPublisher`` (iHealth 専有ディレクトリへの全上書き) と違い、こちらは
**ユーザーが日々書いている日記ノート** に値を差し込む. そのため安全側に倒した
設計にしている:

- **担当キー以外は 1 byte も変えない**: 書き換えは :mod:`ihealth.frontmatter` の
  byte 保存 upsert に任せる (本文・担当外 entry・コメント・改行コード・BOM 不変).
- **書込前バックアップ**: 既存ノートを書き換える直前の内容を
  ``backup_dir/<取得日 UTC>/<ノート名>.<UTC タイムスタンプ>.md`` に保存 (0600).
  ``backup_retention_days`` (既定 30) より古い取得日のディレクトリは best-effort で
  掃除する (本 publisher が作った名前形式のファイルだけを消す).
- **並行編集の検知**: 読込時の ``(st_mtime_ns, st_size, st_ino)`` と内容を記録し、
  隠し tmp に書いた後 ``os.replace`` の直前に stat と内容を再確認する. 不一致
  (= Obsidian / Obsidian Sync / 同じノートに書く他のツール等が同時に書いた) なら tmp を捨てて
  読み直しから再試行し (既定 3 回)、それでも不一致なら **書かずに**
  :class:`ObsidianPublishError` を raise する.

  注意: 再確認から ``os.replace`` までの μs 単位の窓で他のプログラムが書いた内容は
  原理的に失われうる (Obsidian や同じノートに書く他のツールとロックを協調する仕組みが無いため).
  書込前バックアップに入っているのは「本ツールが編集の基にした内容」であり、
  この窓で他のプログラムが書いた内容は含まれない. その場合は Obsidian の
  「ファイル復元」コアプラグインや Obsidian Sync のバージョン履歴から戻す.
  置換直後に読み直して本ツールが書いた内容と違えば warning を出す
  (= 置換の直後に誰かが保存した. こちらの値は次回実行で再適用される).
- **空ノート / Templater 未処理ノート**: 0 byte (空白のみ) のノートは「作成直後で
  テンプレート適用待ち」か「他のプログラムの書込途中 (truncate 直後)」なので、
  待って読み直す. 最後まで空なら、ノート不在時と同じ規則 (下記) で作成を許される
  場合だけテンプレートで初期化する (直近に変更された空ノートには書かない).
  frontmatter が無く Templater の ``<%`` が残るノート (テンプレート処理待ち /
  処理失敗) は frontmatter を前置すると Templater 処理後に二重 frontmatter になる
  ので書かない (status ``"skipped"``).
- **アトミック置換**: 同じディレクトリの隠し tmp (``.{name}.ihealth-{pid}-*.tmp``,
  Obsidian はドットファイルを無視する) に書いて fsync → ``os.replace`` →
  親ディレクトリ fsync. 元ファイルの permission を継承し、macOS では作成日時
  (birthtime) も best-effort で引き継ぐ (``utime`` で過去の mtime を設定すると
  APFS が birthtime を引き下げる挙動を利用).
- **ノート不在時**: ``create_missing`` に従う.

  - ``"never"``: 作らない (status ``"missing"``).
  - ``"template"`` (既定): ``template_path`` の Templater テンプレートを限定展開
    (先頭の ``<%* … %>`` ブロック除去, ``<% dateStr %>``, ``<% weekday %>`` のみ)
    して ``YYYY-MM-DD.md`` を作る. 未対応の ``<%`` が残る / 読めない /
    frontmatter が壊れている場合は **作成を見送る** (status ``"missing"``, warning).
  - ``"minimal"``: テンプレートが使えなければ ``---\\n日付: YYYY-MM-DD\\n---\\n`` の
    最小ノートを作る (明示設定時のみ).

  ただし対象日が「今日 - ``create_min_age_days``」より新しい (既定: 今日・昨日) ノートは
  作らない. Obsidian Sync は Obsidian アプリが起動している間しか同期しないので、
  Mac 側で Obsidian が閉じている間に本ツールが作ったノートと、iPhone 等で
  ユーザーが同じ日に作ったデイリーノートが「共通の祖先の無い同名ファイル」として
  衝突するのを避けるため (数日前のノートを他の端末で新規作成することは稀).

  作成先フォルダは、対象日の前後 ``±7`` 日で最も近い既存の日記ノート
  (``YYYY-MM-DD.md`` 完全一致, 隠しフォルダは除く. 同距離なら過去側) と同じフォルダ.
  見つからなければ日記ルート直下 (過去の月を ``日記/2026-Q1Q2Q3/`` のような
  サブフォルダに移す運用で、抜けていた日のノートを隣の日と同じ場所に作るため).

  新規作成は ``os.link`` による排他作成なので、同時に誰かが作ったノートを上書き
  しない (その場合は既存ノートの更新として再試行する).
- **ノート探索**: ``日記/`` 配下を再帰探索する. Obsidian と同じくシンボリックリンクの
  フォルダもたどる (循環は実パスで検出). シンボリックリンクのノート自体は更新しない.
- **dry-run**: ファイルを一切作らない/書かない. 変更予定を
  ``[dry-run] {path}: {key}: {old!r} -> {new!r}`` で info ログに出す.
"""

from __future__ import annotations

import logging
import math
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass, fields as dataclass_fields
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

from ihealth.frontmatter import (
    ACTION_INSERT,
    ACTION_KEPT,
    ACTION_UNCHANGED,
    ACTION_UPDATE,
    FieldChange,
    FrontmatterError,
    render_key,
    split_frontmatter,
    upsert_frontmatter,
)
from ihealth.models import DailyHealthData
from ihealth.records import WEATHER_FIELDS, DiaryRecord


# ---------------------------------------------------------------------------
# 内部名 → frontmatter キー / 値変換
# ---------------------------------------------------------------------------

#: 内部名 → 日記 frontmatter キー (日本語既定). 並びは日記テンプレート
#: (``テンプレート/毎日の習慣.md``) の並び = 新規キーを追記するときの順序.
_DEFAULT_PROPERTY_MAP_RAW: "dict[str, str]" = {
    "step_count":               "歩数 (歩)",
    "sleep_hours":              "睡眠時間 (分)",
    "nap_hours":                "昼寝時間",
    "active_energy_kcal":       "消費カロリー (kcal)",
    "distance_km":              "移動距離 (km)",
    "heart_rate_resting":       "安静時心拍数 (bpm)",
    "heart_rate_avg":           "平均心拍数 (bpm)",
    "heart_rate_max":           "最大心拍数 (bpm)",
    "oxygen_saturation":        "酸素飽和度 (%)",
    "exercise_intensity_score": "運動強度スコア",
    "mindful_minutes":          "瞑想時間 (分)",
    "mindful_sessions":         "瞑想回数 (回)",
    "exercise_minutes":         "アクティビティ時間 (分)",
    "body_mass_kg":             "体重 (kg)",
    "body_fat_percentage":      "体脂肪率 (%)",
    "temperature":              "気温",
    "humidity":                 "湿度",
    "pressure":                 "気圧",
    "weather":                  "天気",
    "sunshine":                 "日照時間",
    "precipitation":            "降水量",
    "activity_detail":          "アクティビティ詳細",
    "github":                   "Github",
}

#: 公開用の read-only view.
DEFAULT_PROPERTY_MAP: "Mapping[str, str]" = MappingProxyType(_DEFAULT_PROPERTY_MAP_RAW)

#: 文字列をそのまま書く内部名 (天気以外).
TEXT_FIELDS: "tuple[str, ...]" = ("activity_detail", "github")


def _as_number(raw: object) -> float:
    """数値 (int/float, bool 以外, 有限) を float で返す. それ以外は例外."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TypeError(f"数値 (int/float) である必要があります: {type(raw).__name__}")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"NaN/Inf は書けません: {value!r}")
    return value


def _to_int(raw: object) -> int:
    if isinstance(raw, int) and not isinstance(raw, bool):
        return int(raw)
    return int(round(_as_number(raw)))


def _round1(raw: object) -> float:
    return round(_as_number(raw), 1)


def _hours_to_minutes(raw: object) -> int:
    return int(round(_as_number(raw) * 60))


def _round_int(raw: object) -> int:
    return int(round(_as_number(raw)))


#: 健康 15 フィールドの値変換 (Notion PROPERTY_MAP と同じ丸め. ``round`` は
#: half-even). 整数値の float は frontmatter 上 int 表記になる (render_scalar).
VALUE_CONVERTERS: "Mapping[str, Callable[[object], object]]" = MappingProxyType({
    "step_count":               _to_int,
    "distance_km":              _round1,
    "active_energy_kcal":       _round1,
    "exercise_minutes":         _round_int,
    "exercise_intensity_score": _round1,
    "heart_rate_avg":           _round1,
    "heart_rate_max":           _round1,
    "heart_rate_resting":       _round1,
    "oxygen_saturation":        _round1,
    "sleep_hours":              _hours_to_minutes,
    "nap_hours":                _hours_to_minutes,
    "mindful_sessions":         _to_int,
    "mindful_minutes":          _round_int,
    "body_mass_kg":             _round1,
    "body_fat_percentage":      _round1,
})

#: 健康系の内部名 (DailyHealthData のフィールド名).
HEALTH_FIELDS: "tuple[str, ...]" = tuple(
    name for name in _DEFAULT_PROPERTY_MAP_RAW if name in VALUE_CONVERTERS
)


def _validate_maps() -> None:
    """import 時 sanity check: 健康フィールド / 天気 / テキストの内部名が過不足なく揃うこと."""
    model_fields = {f.name for f in dataclass_fields(DailyHealthData)} - {"date"}
    if set(VALUE_CONVERTERS) != model_fields:
        raise RuntimeError(
            "obsidian.VALUE_CONVERTERS と DailyHealthData のフィールドが一致しません: "
            f"only_in_model={sorted(model_fields - set(VALUE_CONVERTERS))}, "
            f"only_in_converters={sorted(set(VALUE_CONVERTERS) - model_fields)}"
        )
    expected = model_fields | set(WEATHER_FIELDS) | set(TEXT_FIELDS)
    if set(_DEFAULT_PROPERTY_MAP_RAW) != expected:
        raise RuntimeError(
            "obsidian.DEFAULT_PROPERTY_MAP の内部名が想定と一致しません: "
            f"missing={sorted(expected - set(_DEFAULT_PROPERTY_MAP_RAW))}, "
            f"extra={sorted(set(_DEFAULT_PROPERTY_MAP_RAW) - expected)}"
        )
    if len(set(_DEFAULT_PROPERTY_MAP_RAW.values())) != len(_DEFAULT_PROPERTY_MAP_RAW):
        raise RuntimeError("obsidian.DEFAULT_PROPERTY_MAP の frontmatter キーが重複しています")


_validate_maps()


# ---------------------------------------------------------------------------
# 結果型 / 例外
# ---------------------------------------------------------------------------

STATUS_UPDATED = "updated"
STATUS_UNCHANGED = "unchanged"
STATUS_CREATED = "created"
STATUS_MISSING = "missing"
STATUS_SKIPPED = "skipped"
STATUS_WOULD_UPDATE = "would_update"
STATUS_WOULD_CREATE = "would_create"

#: NotePublishOutcome.reason: 何も書かなかった理由 (status が missing / skipped のとき).
REASON_NOTHING_TO_WRITE = "nothing_to_write"          # 書く値が無い
REASON_CREATE_DISABLED = "create_disabled"            # create_missing="never"
REASON_TOO_RECENT = "too_recent"                      # 今日/昨日 (create_min_age_days 未満) は作らない
REASON_TEMPLATE_UNAVAILABLE = "template_unavailable"  # テンプレート未設定 / 読めない / 未対応構文
REASON_RECENTLY_MODIFIED = "recently_modified"        # 空ノートが直前に変更された (書込途中の可能性)
REASON_UNPROCESSED_TEMPLATE = "unprocessed_template"  # frontmatter 無し + Templater の '<%' が残る

CREATE_NEVER = "never"
CREATE_TEMPLATE = "template"
CREATE_MINIMAL = "minimal"
_CREATE_MODES = frozenset({CREATE_NEVER, CREATE_TEMPLATE, CREATE_MINIMAL})

#: 不在ノートの作成先を決めるときに探す近傍の日数 (対象日の前後).
_NEIGHBOR_WINDOW_DAYS = 7

#: 空ノートを初期化してよい最終更新からの経過秒数. これより新しい空ノートは
#: 他のプログラムの書込途中 (truncate 直後) / テンプレート適用待ちとみなして書かない.
_EMPTY_NOTE_SETTLE_SEC = 10.0


class ObsidianPublishError(RuntimeError):
    """日記ノートの書き込み失敗 / 設定不正 / 安全に書けない状況.

    統合層で ``ObsidianAppError`` (書込 I/O 系) や ``ConfigAppError`` (設定不正系)
    に包んで通知・終了コードに乗せる想定.
    """


class ObsidianConfigError(ObsidianPublishError):
    """設定不正 (vault / 日記 dir 不在, 未知の内部名, キー衝突, backup_dir が vault 配下 など).

    :class:`ObsidianPublishError` の subclass. 統合層は本クラスを ``ConfigAppError``
    (設定不備), それ以外の :class:`ObsidianPublishError` を書込失敗系に振り分けられる.
    """


class TemplateExpansionError(ValueError):
    """Templater テンプレートを限定展開できない (未対応構文が残る等)."""


@dataclass(frozen=True)
class NotePublishOutcome:
    """1 日分の publish 結果.

    Attributes:
        date: 対象日.
        path: 対象ノート. ``"missing"`` のときは ``None``.
            ``"would_create"`` のときは作成予定の path.
        status: ``"updated"`` / ``"unchanged"`` / ``"created"`` (新規作成. 既存の空ノートを
            テンプレートで初期化した場合も含む) / ``"missing"`` (ノートが無く作らなかった) /
            ``"skipped"`` (ノートはあるが安全に書けないので触らなかった) /
            ``"would_update"`` / ``"would_create"``.
        changes: キーごとの結果 (:class:`ihealth.frontmatter.FieldChange`).
        backup_path: 書込前バックアップの保存先 (取っていなければ ``None``).
        reason: ``"missing"`` / ``"skipped"`` の理由 (``REASON_*`` 定数). それ以外は ``None``.
    """

    date: date
    path: "Path | None"
    status: str
    changes: "tuple[FieldChange, ...]"
    backup_path: "Path | None" = None
    reason: "str | None" = None

    @property
    def written_keys(self) -> "tuple[str, ...]":
        """insert / update になった (dry-run なら、なる予定の) キー."""
        return tuple(
            c.key for c in self.changes if c.action in (ACTION_INSERT, ACTION_UPDATE)
        )


# ---------------------------------------------------------------------------
# Templater テンプレートの限定展開
# ---------------------------------------------------------------------------

_TEMPLATER_HEAD_RE = re.compile(r"\A<%[-_]?\*.*?(?P<trim>[-_]?)%>", re.S)
_TEMPLATER_VAR_RE = re.compile(r"<%\s*(dateStr|weekday)\s*%>")
_WEEKDAYS_JA = "月火水木金土日"


def expand_template(template_text: str, d: date) -> str:
    """日記テンプレートを限定的に展開する (Templater を実行せずに済む範囲だけ).

    対応構文:
        - 先頭の ``<%* … %>`` / ``<%* … -%>`` ブロック (JS) を除去. 直後の改行 1 つも
          除去する (``_%>`` は後続の空白をすべて除去).
        - ``<% dateStr %>`` → ``YYYY-MM-DD``
        - ``<% weekday %>`` → 日本語曜日 1 文字 (月火水木金土日)

    Raises:
        TemplateExpansionError: 展開後に ``<%`` が残る (= 未対応構文).
    """
    bom = "\ufeff" if template_text.startswith("\ufeff") else ""
    body = template_text[len(bom):]
    head = _TEMPLATER_HEAD_RE.match(body)
    if head is not None:
        rest = body[head.end():]
        if head.group("trim") == "_":
            rest = rest.lstrip(" \t\r\n")
        elif rest.startswith("\r\n"):
            rest = rest[2:]
        elif rest.startswith("\n"):
            rest = rest[1:]
        body = rest

    def _sub(match: "re.Match[str]") -> str:
        if match.group(1) == "dateStr":
            return d.isoformat()
        return _WEEKDAYS_JA[d.weekday()]

    body = _TEMPLATER_VAR_RE.sub(_sub, body)
    if "<%" in body:
        raise TemplateExpansionError("未対応の Templater 構文 '<%' が残っています")
    return bom + body


# ---------------------------------------------------------------------------
# ファイル操作の内部ヘルパー
# ---------------------------------------------------------------------------

_NOTE_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
_BACKUP_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BACKUP_FILE_RE = re.compile(r"^.+\.\d{8}T\d{6}Z(?:-\d+)?\.md$")


@dataclass(frozen=True)
class _Snapshot:
    """読込時点のノート内容と stat."""

    data: bytes
    mtime_ns: int
    size: int
    ino: int
    mode: int
    atime_ns: int
    birthtime: "float | None"

    def same_as(self, other: "_Snapshot") -> bool:
        return (
            self.mtime_ns == other.mtime_ns
            and self.size == other.size
            and self.ino == other.ino
            and self.data == other.data
        )


class _ChangedWhileReading(Exception):
    """読込中にファイルが変化した (読み直しで解決する)."""


def _current_umask() -> int:
    """プロセスの umask を取得する (新規ノートの permission 用)."""
    mask = os.umask(0o022)
    os.umask(mask)
    return mask


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


class ObsidianPublisher:
    """日記ノートの frontmatter に健康・天気・GitHub の値を書き込む Publisher.

    Args:
        vault_dir: Obsidian vault のルート (存在必須).
        diary_dir: vault 内の日記ディレクトリ (既定 ``日記``, 存在必須). 配下を再帰探索する.
        logger: 注入ロガー.
        property_overrides: 内部名 → frontmatter キーの上書き (``config.toml``
            ``[publishers.obsidian.fields.<内部名>] property``).
        template_path: ノート不在時に使う Templater テンプレート (vault 相対 or 絶対).
        dry_run: ``True`` ならファイルを一切作らない/書かない.
        fill_only_empty: ``True`` なら既存の非空値を保持 (過去分 backfill 用).
        date_property: 最小ノート生成時の日付キー (既定 ``日付``). 書き込み対象キーと
            衝突してはならない.
        target_date: 指定時は publish 対象の日付をこの日に限定する (取り違え防止).
        backup_dir: 書込前バックアップの保存先. ``None`` でバックアップ無効.
            vault 配下は不可 (バックアップが日記として同期・検索されてしまうため).
        create_missing: ``"never"`` / ``"template"`` (既定) / ``"minimal"``.
        backup_retention_days: バックアップ保持日数 (既定 30).
        max_attempts: 並行編集を検知したときの最大試行回数 (既定 3).
        retry_interval_sec: 再試行の待ち時間 (既定 0.2 秒).
        clock: 現在時刻 (aware datetime) を返す関数. テスト用.
        sleep: 待機関数. テスト用.
        create_min_age_days: ノートを新規作成 (空ノートの初期化を含む) してよい最小の
            経過日数. ``今日 - 対象日`` がこれ未満なら作らない (status ``"missing"`` /
            ``"skipped"``, reason ``"too_recent"``). 既定 2 = 今日・昨日・未来日のノートは
            作らない (別端末での同日ノート作成と Obsidian Sync で衝突させないため).
            0 で今日も作る.
        tz: 「今日」を決めるタイムゾーン. ``None`` ならシステムのローカルタイムゾーン.

    Raises:
        ObsidianConfigError: 設定不正 (vault / 日記 dir 不在, 未知の内部名,
            frontmatter キーの衝突, backup_dir が vault 配下 など).
    """

    def __init__(
        self,
        *,
        vault_dir: Path,
        diary_dir: str = "日記",
        logger: logging.Logger,
        property_overrides: "Mapping[str, str] | None" = None,
        template_path: "Path | None" = None,
        dry_run: bool = False,
        fill_only_empty: bool = False,
        date_property: str = "日付",
        target_date: "date | None" = None,
        backup_dir: "Path | None" = None,
        create_missing: str = CREATE_TEMPLATE,
        backup_retention_days: int = 30,
        max_attempts: int = 3,
        retry_interval_sec: float = 0.2,
        clock: "Callable[[], datetime] | None" = None,
        sleep: "Callable[[float], None] | None" = None,
        create_min_age_days: int = 2,
        tz: "tzinfo | None" = None,
    ) -> None:
        self._logger = logger
        self._vault_dir = Path(vault_dir).expanduser()
        if not self._vault_dir.is_dir():
            raise ObsidianConfigError(f"Obsidian vault が見つかりません: {self._vault_dir}")
        if not isinstance(diary_dir, str) or not diary_dir.strip():
            raise ObsidianConfigError(f"diary_dir は空でない文字列である必要があります: {diary_dir!r}")
        self._diary_root = self._vault_dir / diary_dir
        if not self._diary_root.is_dir():
            raise ObsidianConfigError(f"日記ディレクトリが見つかりません: {self._diary_root}")

        if create_missing not in _CREATE_MODES:
            raise ObsidianConfigError(
                f"create_missing が不正です: {create_missing!r} (許可値: {sorted(_CREATE_MODES)})"
            )
        if not isinstance(date_property, str) or not date_property.strip():
            raise ObsidianConfigError(f"date_property が空です: {date_property!r}")
        if max_attempts < 1:
            raise ObsidianConfigError(f"max_attempts は 1 以上: {max_attempts}")
        if backup_retention_days < 1:
            raise ObsidianConfigError(f"backup_retention_days は 1 以上: {backup_retention_days}")
        if (
            isinstance(create_min_age_days, bool)
            or not isinstance(create_min_age_days, int)
            or create_min_age_days < 0
        ):
            raise ObsidianConfigError(
                f"create_min_age_days は 0 以上の整数: {create_min_age_days!r}"
            )

        self._property_map = self._merge_property_map(property_overrides)
        if date_property in self._property_map.values():
            raise ObsidianConfigError(
                f"date_property {date_property!r} が書き込み対象キーと衝突しています"
            )

        if template_path is not None:
            template_path = Path(template_path).expanduser()
            if not template_path.is_absolute():
                template_path = self._vault_dir / template_path
        self._template_path = template_path

        if backup_dir is not None:
            backup_dir = Path(backup_dir).expanduser()
            vault_resolved = self._vault_dir.resolve()
            backup_resolved = backup_dir.resolve()
            if backup_resolved == vault_resolved or backup_resolved.is_relative_to(vault_resolved):
                raise ObsidianConfigError(
                    f"backup_dir を vault 配下に置くことはできません: {backup_dir}"
                )
        self._backup_dir = backup_dir

        self._dry_run = dry_run
        self._fill_only_empty = fill_only_empty
        self._date_property = date_property
        self._target_date = target_date
        self._create_missing = create_missing
        self._retention_days = backup_retention_days
        self._max_attempts = max_attempts
        self._retry_interval = retry_interval_sec
        self._clock = clock or _default_clock
        self._sleep = sleep or time.sleep
        self._create_min_age_days = create_min_age_days
        self._tz = tz
        self._index: "dict[str, list[Path]] | None" = None
        self._pruned = False
        #: 現在の publish_record で fill_only_empty でも置き換えてよい既存値
        self._replaceable: "dict[str, object]" = {}
        #: 現在の publish_record で使う fill_only_empty
        self._active_fill_only_empty = fill_only_empty

    # ------------------------------------------------------------------
    # 設定
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_property_map(overrides: "Mapping[str, str] | None") -> "Mapping[str, str]":
        merged = dict(_DEFAULT_PROPERTY_MAP_RAW)
        for name, key in (overrides or {}).items():
            if name not in merged:
                raise ObsidianConfigError(
                    f"property_overrides に未知の内部名があります: {name!r} "
                    f"(有効: {sorted(merged)})"
                )
            if not isinstance(key, str) or not key.strip():
                raise ObsidianConfigError(
                    f"property_overrides[{name!r}] は空でない文字列である必要があります: {key!r}"
                )
            merged[name] = key
        owners: "dict[str, str]" = {}
        for name, key in merged.items():
            if key in owners:
                raise ObsidianConfigError(
                    f"frontmatter キー {key!r} に内部名 {owners[key]!r} と {name!r} が"
                    "両方割り当てられています"
                )
            owners[key] = name
        return MappingProxyType(merged)

    @property
    def property_map(self) -> "Mapping[str, str]":
        """既定 + override を統合した内部名 → frontmatter キー (read-only)."""
        return self._property_map

    @property
    def diary_root(self) -> Path:
        return self._diary_root

    # ------------------------------------------------------------------
    # ノート探索
    # ------------------------------------------------------------------

    def _build_index(self) -> "dict[str, list[Path]]":
        """``diary_root`` 以下の ``YYYY-MM-DD.md`` を名前 → path 一覧にする.

        隠しディレクトリ (``.trash`` 等) は除外. Obsidian と同じくシンボリックリンクの
        フォルダもたどる (循環・同じ実体への重複は実パスで 1 回に絞る).
        """
        index: "dict[str, list[Path]]" = {}
        visited: "set[str]" = set()
        for dirpath, dirnames, filenames in os.walk(self._diary_root, followlinks=True):
            real = os.path.realpath(dirpath)
            if real in visited:
                dirnames[:] = []  # 循環 / 既に見た実体: たどらない
                continue
            visited.add(real)
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                if _NOTE_NAME_RE.match(name):
                    index.setdefault(name, []).append(Path(dirpath) / name)
        return index

    def _invalidate_index(self) -> None:
        self._index = None

    def _ordered_candidates(self, name: str, paths: "list[Path] | set[Path]") -> "list[Path]":
        """同名ノートの優先順: 日記ルート直下 → 浅い path → path 文字列順."""
        direct = self._diary_root / name
        return sorted(
            paths,
            key=lambda p: (p != direct, len(p.relative_to(self._diary_root).parts), str(p)),
        )

    def find_note(self, d: date) -> "Path | None":
        """``d`` の日記ノートを探す. 無ければ ``None``.

        ``diary_root/YYYY-MM-DD.md`` を最優先し、無ければ ``diary_root`` 以下を再帰探索して
        ファイル名が ``YYYY-MM-DD.md`` に **完全一致** するものを探す (隠しディレクトリ
        と ``.trash`` は除外. ``2025-09-07 週次振り返り ….md`` のような別ノートは掴まない.
        シンボリックリンクのフォルダはたどる). 複数ヒット時は浅い path を優先し warning を
        出す. 再帰探索の結果は publisher インスタンス内でキャッシュする (直下 path は毎回
        実ファイルを確認).
        """
        name = f"{d.isoformat()}.md"
        direct = self._diary_root / name
        if self._index is None:
            self._index = self._build_index()
        candidates = set(self._index.get(name, []))
        if direct.is_file() or direct.is_symlink():
            candidates.add(direct)
        if not candidates:
            return None
        ordered = self._ordered_candidates(name, candidates)
        if len(ordered) > 1:
            self._logger.warning(
                "日記ノートが複数見つかりました。%s を採用します (他: %s)",
                ordered[0], ", ".join(str(p) for p in ordered[1:]),
            )
        return ordered[0]

    def _creation_dir(self, d: date) -> Path:
        """不在ノート ``d`` を作るフォルダ.

        対象日の前後 ``_NEIGHBOR_WINDOW_DAYS`` 日で最も近い既存の日記ノート
        (``YYYY-MM-DD.md`` 完全一致, 隠しフォルダ除外) と同じフォルダ. 同距離なら過去側.
        見つからなければ日記ルート直下.
        """
        if self._index is None:
            self._index = self._build_index()
        for distance in range(1, _NEIGHBOR_WINDOW_DAYS + 1):
            for delta in (-distance, distance):  # 同距離なら過去側を優先
                try:
                    neighbor = d + timedelta(days=delta)
                except OverflowError:
                    continue
                name = f"{neighbor.isoformat()}.md"
                paths = self._index.get(name)
                if paths:
                    chosen = self._ordered_candidates(name, paths)[0]
                    self._logger.debug(
                        "新規ノートの作成先: %s と同じフォルダ (%s)", chosen, chosen.parent,
                    )
                    return chosen.parent
        return self._diary_root

    # ------------------------------------------------------------------
    # 値の組み立て
    # ------------------------------------------------------------------

    def build_updates(self, record: DiaryRecord) -> "dict[str, object]":
        """``record`` を frontmatter キー → 値の dict にする (``None``・空文字は含めない).

        並びは property map の順 (= 新規追記の順).

        Raises:
            ObsidianPublishError: 健康値が数値でない / NaN / 文字列フィールドが str でない.
        """
        values: "dict[str, object]" = {}
        health = record.health
        if health is not None:
            if health.date != record.date:
                raise ObsidianPublishError(
                    f"health.date ({health.date}) が record.date ({record.date}) と一致しません"
                )
            for name, converter in VALUE_CONVERTERS.items():
                raw = getattr(health, name)
                if raw is None:
                    continue
                try:
                    values[name] = converter(raw)
                except (TypeError, ValueError) as exc:
                    raise ObsidianPublishError(
                        f"{record.date} の {name} を変換できません: {exc}"
                    ) from exc
        if record.weather is not None:
            for name in WEATHER_FIELDS:
                self._put_text(values, name, getattr(record.weather, name, None), record.date)
        self._put_text(values, "activity_detail", record.activity_detail, record.date)
        self._put_text(values, "github", record.github, record.date)

        updates: "dict[str, object]" = {}
        for name, key in self._property_map.items():
            if name in values:
                updates[key] = values[name]
        return updates

    @staticmethod
    def _put_text(values: "dict[str, object]", name: str, value: object, d: date) -> None:
        if value is None:
            return
        if not isinstance(value, str):
            raise ObsidianPublishError(
                f"{d} の {name} は文字列である必要があります: {type(value).__name__}"
            )
        if value.strip() == "":
            return
        values[name] = value

    # ------------------------------------------------------------------
    # publish
    # ------------------------------------------------------------------

    def publish(self, health_data: DailyHealthData) -> None:
        """:class:`ihealth.publishers.base.Publisher` 互換 (健康データだけを書く)."""
        outcome = self.publish_record(DiaryRecord(date=health_data.date, health=health_data))
        if outcome.status in (STATUS_MISSING, STATUS_SKIPPED):
            self._logger.warning(
                "日記ノートに健康データを書き込めませんでした: date=%s status=%s reason=%s",
                health_data.date, outcome.status, outcome.reason,
            )

    def publish_record(
        self,
        record: DiaryRecord,
        *,
        replaceable: "Mapping[str, object] | None" = None,
        fill_only_empty: "bool | None" = None,
    ) -> NotePublishOutcome:
        """1 日分の値を日記ノートに書き込む.

        Args:
            record: 書き込む値.
            replaceable: ``fill_only_empty`` でも置き換えてよい既存値 (frontmatter キー →
                以前に本ツールが書いた暫定値). 既存値がこれと同じキーだけ書き換える
                (:func:`ihealth.frontmatter.upsert_frontmatter` の ``replaceable``).
            fill_only_empty: この 1 回だけ ``fill_only_empty`` を上書きする (None なら
                コンストラクタの設定).

        Raises:
            ObsidianPublishError: 書き込み失敗 / frontmatter が壊れている /
                並行編集が続いた / target_date 不一致 など.
        """
        d = record.date
        if self._target_date is not None and d != self._target_date:
            raise ObsidianPublishError(
                f"target_date 不整合: publisher={self._target_date} record={d}"
            )
        self._replaceable = dict(replaceable or {})
        self._active_fill_only_empty = (
            self._fill_only_empty if fill_only_empty is None else bool(fill_only_empty)
        )
        updates = self.build_updates(record)
        for attempt in range(1, self._max_attempts + 1):
            if attempt > 1:
                self._sleep(self._retry_interval)
                self._invalidate_index()
            path = self.find_note(d)
            if path is None:
                # 作成前に再帰探索をやり直す (実行中にサブフォルダへ移動・作成されたノートを
                # キャッシュ済みの索引が取りこぼして重複ノートを作らないため)
                self._invalidate_index()
                path = self.find_note(d)
            if path is None:
                outcome = self._publish_new(d, updates)
            else:
                outcome = self._publish_existing(
                    d, path, updates, attempt, last=attempt == self._max_attempts,
                )
            if outcome is not None:
                return outcome
        raise ObsidianPublishError(
            f"日記ノートへの並行編集が続いたため書き込みを見送りました "
            f"(date={d}, {self._max_attempts} 回試行)"
        )

    # ---- 既存ノートの更新 ----

    def _publish_existing(
        self, d: date, path: Path, updates: "dict[str, object]", attempt: int, *, last: bool,
    ) -> "NotePublishOutcome | None":
        """既存ノートを更新する. ``None`` は「読み直して再試行」."""
        if not updates:
            # 書く値が無いならノートの形は問わない (読みもしない)
            return NotePublishOutcome(d, path, STATUS_UNCHANGED, ())
        if path.is_symlink():
            raise ObsidianPublishError(f"シンボリックリンクの日記ノートは更新しません: {path}")
        try:
            snap = self._snapshot(path)
        except FileNotFoundError:
            self._logger.warning("日記ノートが読込直前に消えました (移動?): %s", path)
            return None
        except _ChangedWhileReading:
            self._logger.warning("日記ノートが読込中に変更されました: %s", path)
            return None
        except OSError as exc:
            raise ObsidianPublishError(f"日記ノートを読めません: {path} ({exc})") from exc
        try:
            text = snap.data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ObsidianPublishError(f"日記ノートが UTF-8 ではありません: {path}") from exc
        if text.strip() == "":
            return self._publish_uninitialized(d, path, snap, updates, attempt, last=last)
        try:
            if split_frontmatter(text) is None and "<%" in text:
                return self._skip_unprocessed_template(d, path, attempt, last=last)
            new_text, changes = upsert_frontmatter(
                text, updates, fill_only_empty=self._active_fill_only_empty, create_if_missing=True,
                replaceable=self._replaceable,
            )
        except FrontmatterError as exc:
            raise ObsidianPublishError(
                f"frontmatter を安全に解釈できないため更新しません: {path} ({exc})"
            ) from exc
        except (TypeError, ValueError) as exc:
            raise ObsidianPublishError(f"frontmatter 値を組み立てられません: {path} ({exc})") from exc
        self._log_warnings(path, changes)
        if new_text == text:
            self._logger.debug("日記ノートは最新です: %s", path)
            return NotePublishOutcome(d, path, STATUS_UNCHANGED, tuple(changes))
        if self._dry_run:
            self._log_dry_run(path, changes)
            return NotePublishOutcome(d, path, STATUS_WOULD_UPDATE, tuple(changes))
        return self._commit_replace(
            d, path, snap, new_text, changes, attempt, STATUS_UPDATED, "日記ノート更新",
        )

    def _publish_uninitialized(
        self, d: date, path: Path, snap: _Snapshot, updates: "dict[str, object]",
        attempt: int, *, last: bool,
    ) -> "NotePublishOutcome | None":
        """空 (0 byte / 空白のみ) の既存ノート. ``None`` は「読み直して再試行」.

        作成直後でテンプレート適用待ち (Daily notes コアプラグインはテンプレート未設定だと
        空ノートを作り、テンプレートは後から挿入される) か、他のプログラムの書込途中
        (truncate 直後) なので、まず待って読み直す. 最後の試行でも空なら、ノート不在時と
        同じ規則 (``create_missing`` / ``create_min_age_days``) で作成を許される場合だけ
        テンプレート (または最小ノート) で初期化する. 直近に変更された空ノートには書かない.
        frontmatter だけを書き込むと、後でテンプレートを挿入したときに二重 frontmatter に
        なるため.
        """
        if not last:
            self._logger.info(
                "日記ノートが空です。書込途中の可能性があるため読み直します (attempt %d/%d): %s",
                attempt, self._max_attempts, path,
            )
            return None
        reason = self._creation_block_reason(d)
        if reason is None and self._recently_modified(snap):
            reason = REASON_RECENTLY_MODIFIED
        initial = self._initial_note_text(d) if reason is None else None
        if initial is None:
            if reason is None:
                reason = REASON_TEMPLATE_UNAVAILABLE
            log = self._logger.info if reason == REASON_TOO_RECENT else self._logger.warning
            log("空の日記ノートには書き込みません (reason=%s): %s", reason, path)
            return NotePublishOutcome(d, path, STATUS_SKIPPED, (), reason=reason)
        try:
            new_text, changes = upsert_frontmatter(
                initial, updates, fill_only_empty=self._active_fill_only_empty, create_if_missing=True,
                replaceable=self._replaceable,
            )
        except (FrontmatterError, TypeError, ValueError) as exc:
            raise ObsidianPublishError(
                f"空ノートの初期内容を組み立てられません: {path} ({exc})"
            ) from exc
        if self._dry_run:
            self._log_dry_run(path, changes)
            return NotePublishOutcome(d, path, STATUS_WOULD_CREATE, tuple(changes))
        return self._commit_replace(
            d, path, snap, new_text, changes, attempt, STATUS_CREATED, "空の日記ノートを初期化",
        )

    def _skip_unprocessed_template(
        self, d: date, path: Path, attempt: int, *, last: bool,
    ) -> "NotePublishOutcome | None":
        """frontmatter が無く Templater の ``<%`` が残るノート (テンプレート処理待ち / 処理失敗).

        ここへ frontmatter を前置すると、Templater が後で処理したときにテンプレート側の
        frontmatter が 2 つ目のブロック (本文扱い) になり、Obsidian はこちらのキーしか
        プロパティとして表示しなくなる. 処理を待って読み直し、最後まで残っていれば触らない.
        """
        if not last:
            self._logger.info(
                "日記ノートに Templater の未処理構文があります。処理待ちの可能性があるため"
                "読み直します (attempt %d/%d): %s", attempt, self._max_attempts, path,
            )
            return None
        self._logger.warning(
            "frontmatter が無く Templater の未処理構文 '<%%' が残る日記ノートには書き込みません "
            "(テンプレートを適用してください): %s", path,
        )
        return NotePublishOutcome(
            d, path, STATUS_SKIPPED, (), reason=REASON_UNPROCESSED_TEMPLATE,
        )

    def _commit_replace(
        self, d: date, path: Path, snap: _Snapshot, new_text: str,
        changes: "list[FieldChange]", attempt: int, status: str, label: str,
    ) -> "NotePublishOutcome | None":
        """書込前バックアップ → 隠し tmp に書込 → 置換直前の再確認 → ``os.replace``.

        再確認で読込時 (``snap``) から stat か内容が変わっていれば tmp を捨てて ``None``
        (= 読み直して再試行).
        """
        new_data = new_text.encode("utf-8")
        backup_path = self._write_backup(path, snap.data)
        tmp = self._write_tmp(path, new_data, snap.mode)
        replaced = False
        try:
            self._preserve_birthtime(tmp, snap)
            try:
                current: "_Snapshot | None" = self._snapshot(path)
            except (FileNotFoundError, _ChangedWhileReading):
                current = None
            if current is None or not snap.same_as(current):
                self._logger.warning(
                    "日記ノートへの並行変更を検知したため読み直します (attempt %d/%d): %s",
                    attempt, self._max_attempts, path,
                )
                return None
            os.replace(tmp, path)
            replaced = True
        except OSError as exc:
            raise ObsidianPublishError(f"日記ノートの書き込みに失敗: {path} ({exc})") from exc
        finally:
            if not replaced:
                self._unlink_quietly(tmp)
        self._fsync_dir(path.parent)
        self._verify_written(path, new_data)
        self._log_summary(label, path, changes, backup_path)
        return NotePublishOutcome(d, path, status, tuple(changes), backup_path)

    # ---- ノート新規作成 ----

    def _publish_new(self, d: date, updates: "dict[str, object]") -> "NotePublishOutcome | None":
        if not updates:
            self._logger.info("書き込む値が無いため日記ノートは作成しません: date=%s", d)
            return NotePublishOutcome(d, None, STATUS_MISSING, (), reason=REASON_NOTHING_TO_WRITE)
        reason = self._creation_block_reason(d)
        if reason == REASON_TOO_RECENT:
            self._logger.info(
                "日記ノートがありません。直近 (今日から %d 日未満) のノートは別端末での作成と"
                "Obsidian Sync で衝突しうるため作成しません: date=%s",
                self._create_min_age_days, d,
            )
            return NotePublishOutcome(d, None, STATUS_MISSING, (), reason=reason)
        if reason is not None:
            self._logger.warning("日記ノートがありません (作成しない設定): date=%s", d)
            return NotePublishOutcome(d, None, STATUS_MISSING, (), reason=reason)
        initial = self._initial_note_text(d)
        if initial is None:
            return NotePublishOutcome(
                d, None, STATUS_MISSING, (), reason=REASON_TEMPLATE_UNAVAILABLE,
            )
        try:
            new_text, changes = upsert_frontmatter(
                initial, updates, fill_only_empty=self._active_fill_only_empty, create_if_missing=True,
                replaceable=self._replaceable,
            )
        except (FrontmatterError, TypeError, ValueError) as exc:
            raise ObsidianPublishError(f"新規ノートの frontmatter を組み立てられません: {d} ({exc})") from exc
        path = self._creation_dir(d) / f"{d.isoformat()}.md"
        if self._dry_run:
            self._log_dry_run(path, changes)
            return NotePublishOutcome(d, path, STATUS_WOULD_CREATE, tuple(changes))
        if not self._create_exclusive(path, new_text.encode("utf-8")):
            self._logger.warning("作成しようとした日記ノートが既に存在します (並行作成?): %s", path)
            return None
        self._log_summary("日記ノート作成", path, changes, None)
        return NotePublishOutcome(d, path, STATUS_CREATED, tuple(changes))

    def _today(self) -> date:
        """``tz`` (未指定ならシステムのローカルタイムゾーン) での今日."""
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if self._tz is not None:
            return now.astimezone(self._tz).date()
        return now.astimezone().date()

    def _creation_block_reason(self, d: date) -> "str | None":
        """ノートを作成 (空ノートの初期化を含む) してはいけない理由. 作ってよければ ``None``."""
        if self._create_missing == CREATE_NEVER:
            return REASON_CREATE_DISABLED
        if (self._today() - d).days < self._create_min_age_days:
            return REASON_TOO_RECENT
        return None

    def _recently_modified(self, snap: _Snapshot) -> bool:
        """読込時点の mtime が現在から ``_EMPTY_NOTE_SETTLE_SEC`` 以内か (未来の mtime も含む)."""
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.timestamp() - snap.mtime_ns / 1_000_000_000 < _EMPTY_NOTE_SETTLE_SEC

    def _initial_note_text(self, d: date) -> "str | None":
        """新規ノート (空ノートの初期化を含む) の初期内容. 作れない場合は ``None`` (理由は warning ログ)."""
        if self._create_missing == CREATE_NEVER:
            return None
        if self._template_path is not None:
            try:
                raw = self._template_path.read_bytes().decode("utf-8")
                content = expand_template(raw, d)
                # 壊れた / 無い frontmatter のテンプレートは使わない (JS ブロック内の '%>' で
                # 展開が途中で切れた場合などに、残骸の上へ frontmatter を作らないため)
                if split_frontmatter(content) is None:
                    raise TemplateExpansionError("展開結果の先頭に frontmatter がありません")
                return content
            except (OSError, UnicodeDecodeError, TemplateExpansionError, FrontmatterError) as exc:
                self._logger.warning(
                    "テンプレートから日記ノートを作れません: %s (%s)", self._template_path, exc,
                )
        if self._create_missing == CREATE_MINIMAL:
            self._logger.info("最小ノートの内容を使います: date=%s", d)
            return f"---\n{render_key(self._date_property)}: {d.isoformat()}\n---\n"
        self._logger.warning(
            "テンプレートが使えないため日記ノートを作成 (初期化) しません: date=%s", d,
        )
        return None

    # ------------------------------------------------------------------
    # ファイル操作
    # ------------------------------------------------------------------

    def _snapshot(self, path: Path) -> _Snapshot:
        """ノートを bytes で読み、読込前後で stat が変わっていないことを確認して返す."""
        with open(path, "rb") as fh:
            before = os.fstat(fh.fileno())
            data = fh.read()
            after = os.fstat(fh.fileno())
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise _ChangedWhileReading(str(path))
        if len(data) != after.st_size:
            raise _ChangedWhileReading(str(path))
        return _Snapshot(
            data=data,
            mtime_ns=after.st_mtime_ns,
            size=after.st_size,
            ino=after.st_ino,
            mode=stat.S_IMODE(after.st_mode),
            atime_ns=after.st_atime_ns,
            birthtime=getattr(after, "st_birthtime", None),
        )

    def _write_tmp(self, path: Path, data: bytes, mode: int) -> Path:
        """``path`` と同じディレクトリの隠し tmp に書いて fsync し、permission を設定."""
        try:
            fd, name = tempfile.mkstemp(
                dir=str(path.parent),
                prefix=f".{path.name}.ihealth-{os.getpid()}-",
                suffix=".tmp",
            )
        except OSError as exc:
            raise ObsidianPublishError(f"一時ファイルを作れません: {path.parent} ({exc})") from exc
        tmp = Path(name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, mode)
        except OSError as exc:
            self._unlink_quietly(tmp)
            raise ObsidianPublishError(f"一時ファイルへの書き込みに失敗: {tmp} ({exc})") from exc
        except BaseException:
            self._unlink_quietly(tmp)
            raise
        return tmp

    def _create_exclusive(self, path: Path, data: bytes) -> bool:
        """``path`` が存在しない場合だけ作成する. 既に存在したら ``False``."""
        mode = 0o666 & ~_current_umask()
        tmp = self._write_tmp(path, data, mode)
        try:
            try:
                os.link(tmp, path)
            except FileExistsError:
                return False
            except OSError as exc:
                # hard link 非対応 FS: O_EXCL で直接作成 (原子性は劣るが上書きはしない)
                self._logger.debug("os.link 不可のため O_EXCL で作成します: %s (%s)", path, exc)
                try:
                    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
                except FileExistsError:
                    return False
                try:
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(data)
                        fh.flush()
                        os.fsync(fh.fileno())
                except OSError:
                    self._unlink_quietly(path)
                    raise
        except OSError as exc:
            raise ObsidianPublishError(f"日記ノートの作成に失敗: {path} ({exc})") from exc
        finally:
            self._unlink_quietly(tmp)
        self._fsync_dir(path.parent)
        return True

    def _preserve_birthtime(self, tmp: Path, snap: _Snapshot) -> None:
        """macOS: 元ノートの作成日時 (birthtime) を tmp に引き継ぐ (best-effort).

        APFS は mtime を birthtime より過去に設定すると birthtime もそこまで下がるので、
        一度 mtime = 元の birthtime にしてから現在時刻に戻す.
        """
        if snap.birthtime is None:
            return
        try:
            birth_ns = int(snap.birthtime * 1_000_000_000)
            now_ns = time.time_ns()
            if birth_ns >= now_ns:
                return
            os.utime(tmp, ns=(snap.atime_ns, birth_ns))
            os.utime(tmp, ns=(now_ns, now_ns))
        except (OSError, OverflowError, ValueError) as exc:
            self._logger.debug("作成日時の引き継ぎに失敗 (無視): %s (%s)", tmp, exc)

    def _fsync_dir(self, directory: Path) -> None:
        try:
            dir_fd = os.open(str(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError as exc:
            self._logger.warning("親ディレクトリを開けず fsync をスキップ: %s (%s)", directory, exc)
            return
        try:
            os.fsync(dir_fd)
        except OSError as exc:
            self._logger.warning("ディレクトリ fsync 失敗: %s (%s)", directory, exc)
        finally:
            try:
                os.close(dir_fd)
            except OSError:
                pass

    def _verify_written(self, path: Path, expected: bytes) -> None:
        """置換直後の内容確認. 一致しなければ「置換の直後に誰かが保存した」として warning.

        その場合ノートにあるのは他のプログラムが保存した内容で、こちらの値が上書きされた
        可能性がある (次回実行で再適用される). 逆に「再確認から置換までの窓で他のプログラムが
        書いた内容」はここでは検出できない (置換後のファイルはこちらが書いた内容と一致する).
        """
        try:
            actual = path.read_bytes()
        except OSError as exc:
            self._logger.warning("書き込み後の確認読込に失敗: %s (%s)", path, exc)
            return
        if actual != expected:
            self._logger.warning(
                "書き込み直後に日記ノートが別の内容に変わっています (直後に他のプログラムが"
                "保存した可能性。こちらの値は次回実行で再適用されます): %s", path,
            )

    @staticmethod
    def _unlink_quietly(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    # ---- バックアップ ----

    def _write_backup(self, path: Path, data: bytes) -> "Path | None":
        """書き換え前の内容を保存する. 失敗したら書き込み自体を中止する (raise)."""
        if self._backup_dir is None:
            return None
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        day_dir = self._backup_dir / now.date().isoformat()
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        try:
            day_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            backup_path = None
            for n in range(0, 1000):
                suffix = "" if n == 0 else f"-{n}"
                candidate = day_dir / f"{path.stem}.{stamp}{suffix}.md"
                try:
                    fd = os.open(str(candidate), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    continue
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                backup_path = candidate
                break
            if backup_path is None:
                raise OSError("バックアップファイル名が枯渇しました")
        except OSError as exc:
            raise ObsidianPublishError(
                f"書込前バックアップに失敗したため書き込みを中止します: {path} ({exc})"
            ) from exc
        self._prune_backups(now.date())
        return backup_path

    def _prune_backups(self, today: date) -> None:
        """保持期間より古い取得日ディレクトリから本 publisher のバックアップだけを消す."""
        if self._pruned or self._backup_dir is None:
            return
        self._pruned = True
        threshold = today - timedelta(days=self._retention_days)
        try:
            day_dirs = [p for p in self._backup_dir.iterdir() if p.is_dir()]
        except OSError as exc:
            self._logger.warning("バックアップ掃除をスキップ: %s (%s)", self._backup_dir, exc)
            return
        for day_dir in day_dirs:
            if not _BACKUP_DAY_RE.match(day_dir.name):
                continue
            try:
                day = date.fromisoformat(day_dir.name)
            except ValueError:
                continue
            if day >= threshold:
                continue
            try:
                for child in day_dir.iterdir():
                    if child.is_file() and _BACKUP_FILE_RE.match(child.name):
                        child.unlink()
                if any(day_dir.iterdir()):
                    # 本 publisher が作っていないファイルが残っている → ディレクトリごとは消さない
                    self._logger.debug("バックアップ以外のファイルがあるため残します: %s", day_dir)
                else:
                    day_dir.rmdir()
            except OSError as exc:
                self._logger.warning("古いバックアップの削除に失敗 (無視): %s (%s)", day_dir, exc)

    # ---- ログ ----

    def _log_dry_run(self, path: Path, changes: "list[FieldChange]") -> None:
        for change in changes:
            if change.action in (ACTION_INSERT, ACTION_UPDATE):
                self._logger.info(
                    "[dry-run] %s: %s: %r -> %r", path, change.key, change.old, change.new,
                )
            elif change.action == ACTION_KEPT:
                self._logger.debug(
                    "[dry-run] %s: %s: kept %r (今回値 %r)", path, change.key, change.old, change.new,
                )

    def _log_warnings(self, path: Path, changes: "list[FieldChange]") -> None:
        for change in changes:
            if change.warning:
                self._logger.warning("%s: %s", path, change.warning)

    def _log_summary(
        self, label: str, path: Path, changes: "list[FieldChange]", backup_path: "Path | None",
    ) -> None:
        counts = {ACTION_INSERT: 0, ACTION_UPDATE: 0, ACTION_UNCHANGED: 0, ACTION_KEPT: 0}
        for change in changes:
            counts[change.action] = counts.get(change.action, 0) + 1
        self._logger.info(
            "%s: %s (insert=%d update=%d unchanged=%d kept=%d backup=%s)",
            label, path, counts[ACTION_INSERT], counts[ACTION_UPDATE],
            counts[ACTION_UNCHANGED], counts[ACTION_KEPT], backup_path,
        )
