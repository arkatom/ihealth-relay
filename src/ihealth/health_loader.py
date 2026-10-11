"""1 日分の健康データを「オートメーション JSON 優先 + AutoSync .hae 補完」で読み込む。

データ源は 2 系統ある:

1. **オートメーション JSON** (現役): ``<HAE Documents>/<名前>/HealthAutoExport-YYYY-MM-DD.json``
   (:mod:`ihealth.automation` / :mod:`ihealth.automation_parser`)
2. **AutoSync .hae** (旧経路。HAE の AutoSync を止めた後も過去分の backfill に使う):
   ``<HAE Documents>/AutoSync/HealthMetrics/<metric>/YYYYMMDD.hae``
   (:mod:`ihealth.source` / :mod:`ihealth.parser`)

:class:`HealthLoader` は日付ごとに

* オートメーション JSON があればそれを主とし (壊れていれば次の候補へ fallback)、
  JSON で ``None`` のフィールドだけを同日の AutoSync .hae で補う (フィールド単位補完)。
  特に JSON の睡眠が aggregateSleep (昼寝を分離できない) の日は ``nap_hours`` を
  .hae から採る。``sleep_hours`` は JSON の totalSleep (= 昼寝込みの総睡眠) のまま
* JSON が無い日は AutoSync .hae だけで集計する (旧パイプラインと同じ集計関数)
* どちらも無ければ ``nodata``、読めなければ ``failed``

を返す。各結果には完全性ステータス (``complete`` / ``partial`` / ``failed`` /
``nodata``) を付け、統合側 (diary) が「書く / 既存値を保持する / 通知する」を
判断できるようにする。

**暫定値と保留** (WAVE3 W3-2h): HAE はファイルを書き出した時点までのデータしか
含めない。ファイルの最終更新 (mtime) が対象日の終わりより前なら、そのファイルは
対象日の途中までしか含まない。

* オートメーション JSON (主ソース) が対象日の途中までの出力 → 値は **返し**
  ``provisional=True`` (暫定値)。対象日が既に終わっていても同じ: HAE の
  オートメーションは直近の日のファイルを毎回書き直すので、再出力されれば後の実行で
  確定値に置き換わる (統合側が暫定値の台帳で確定まで追跡する)。対象日がまだ
  終わっていない (今日) も ``provisional=True`` (§4.6)
* segment 形式の夜主睡眠で、前日ファイルが無い / 読めない / 夜が明ける前の古い
  出力 / 集計形式のため 0 時前の segment を結合できない → 過少の恐れがある値を
  返し ``provisional=True`` (前日ファイルが再出力されれば確定値に置き換わる)。
  暫定なのは夜主睡眠だけなので ``provisional_fields=("sleep_hours",)`` (統合側は
  他のフィールドを確定値として扱う)
* AutoSync .hae (補完 / 旧経路) が対象日の途中までの出力で、対象日が既に終わって
  いる → そのファイル由来の値は返さず ``withheld_fields`` に載せる (AutoSync は
  停止済みで二度と書き直されない = 確定しない過少値を書かない)。``withheld_fields``
  に載るのはこの場合だけ。今日の分なら暫定値として返す
* 最良候補を読めず古い候補へ fallback するときは、その候補が対象日の終わり以降に
  書かれたもの (確定) である場合に限る (読めなかった新しい候補より古い途中経過で
  既存値を上書きしないため。今日の分は fallback しない)

**日の終わり** = 対象日の翌日 0:00。オートメーション JSON はその日の **record 自身の
offset** の暦日 (:func:`ihealth.automation_parser.record_day_end`。offset が混在すれば
最も遅い終わり)、offset が無い / AutoSync .hae は日記の基準 tz (``IHEALTH_TZ_OFFSET``,
:attr:`Config.diary_tz`) で求める。mtime (epoch 秒) と現在時刻はどちらも絶対時刻で
比べるので Mac のローカル tz に依存しない。

**アクティビティ詳細** (WAVE3 W3-7 改訂): 両経路とも
:func:`ihealth.automation_parser.compose_activity_detail` の規則 (瞑想 → ワークアウト /
エクササイズ時間。何も無い日は、補完後の最終的な値に心拍 (平均 / 最大心拍数・運動強度
スコア) がある = Watch を着けていた日なら ``"なし"``、無ければ書かない)。AutoSync
経路はワークアウトを読まないので「ワークアウトを出力しない設定」と同じく
``mindful_minutes`` / ``apple_exercise_time`` / ``heart_rate`` の .hae から組む。

``HEALTH_EXPORT_DIR`` は HAE の Documents root でも AutoSync ディレクトリでも動く:

* ``<dir>/HealthMetrics`` がある → ``<dir>`` は AutoSync。オートメーション探索
  root はその親 (= Documents root。AutoSync 自体も配下に含まれる)
* ``<dir>`` の名前が ``AutoSync`` (``HealthMetrics`` を消した後も) → AutoSync なし、
  探索 root はその親 (隣のオートメーションのフォルダを探す)
* ``<dir>/AutoSync/HealthMetrics`` がある → AutoSync は ``<dir>/AutoSync``、探索 root は ``<dir>``
* どれでもない → AutoSync なし、探索 root は ``<dir>``
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Callable, Iterable

from ihealth.automation import (
    DEFAULT_MAX_DEPTH,
    AutomationError,
    AutomationIndex,
    discover_automation_files,
    load_automation_json,
)
from ihealth.automation_parser import (
    HAE_AGGREGATED_SLEEP_DAY_END_HOUR,
    HEALTH_FIELDS,
    MINDFUL_FIELDS,
    SLEEP_MODE_SEGMENTS,
    compose_activity_detail,
    has_data_for,
    has_heart_rate,
    needs_prev_payload,
    parse_automation_day_detailed,
    record_day_end,
)
from ihealth.config import JST, Config
from ihealth.models import DailyHealthData
from ihealth.parser import (
    FIELD_SOURCES,
    ParseContext,
    _jst_datetime,
    _rebuild_with_validation_drop,
    _sum_asleep_by_source,
    drop_invalid_fields,
)
from ihealth.source import METRIC_DIRS, fetch_metric_for_date


SOURCE_AUTOMATION = "automation"
SOURCE_AUTOSYNC = "autosync"
SOURCE_NONE = "none"

#: 全部取れた (欠けているフィールドは「その日そのデータが無い / 確定していない」だけ)
STATUS_COMPLETE = "complete"
#: 一部の読込・集計が失敗した (取れたフィールドは確実)
STATUS_PARTIAL = "partial"
#: 取得不能 (ファイルはあるが読めない / 想定外エラー / 設定不備)
STATUS_FAILED = "failed"
#: 正常に探したが対象日のデータが存在しない (HAE 未出力 / 停止済み AutoSync の途中までの
#: 出力しか無く全部保留した。保留したフィールドは ``withheld_fields`` に載る)
STATUS_NODATA = "nodata"

_SLEEP_DIR = "sleep_analysis"
#: AutoSync だけで集計する日の昼寝の終了時刻上限 (旧パイプラインと同じ 14-20 時)。
_LEGACY_NAP_END_HOUR = 20
_SLEEP_FIELDS = ("sleep_hours", "nap_hours")

#: AutoSync の 1 メトリクス解凍関数 (``ihealth.source.fetch_metric_for_date`` 互換)。
MetricFetcher = Callable[..., "Path | None"]


@dataclass(frozen=True)
class HealthLoadResult:
    """:meth:`HealthLoader.load` の結果。

    統合側の使い方:

    * ``health`` の **非 None フィールドだけを書く** (None = 書かない = 既存値を保持)。
      非 None の値は status が partial でも「取れた値」: 失敗した / 停止済み AutoSync の
      途中までの出力に由来するフィールドは None に落としてある
    * ``provisional=True`` の値は暫定値 (後で増えうる / 確定値に置き換わる前提。§4.6):
      対象日がまだ終わっていない (今日)、主ソースのオートメーション JSON が対象日の
      終わりより前に書き出された途中経過 (日が既に終わっていても)、segment 形式の
      夜主睡眠が前日ファイルの不足で過少の恐れがある、のいずれか。統合側は暫定値の
      台帳で確定するまで追跡する
    * ``failed`` は通知対象 (``errors`` に理由)。``partial`` は取れた値を書いたうえで
      ``errors`` を報告する。``nodata`` (ファイル未出力 / 停止済み AutoSync の途中までの
      出力しか無い) はエラーではない

    Attributes:
        health: 集計結果。1 フィールドも取れなかった日は ``None``
        activity_detail: ``"瞑想3分、エクササイズ21分"`` / ``"なし"`` 等
            (:func:`ihealth.automation_parser.compose_activity_detail` の規則)。
            瞑想も運動も心拍も無い (Watch を着けていない) / 判断できない日は ``None`` (書かない)
        source: ``"automation"`` / ``"autosync"`` / ``"none"`` (主に使ったデータ源)
        detail: ログ用の説明 (採用 path / 補完 / 暫定 / 保留 / 失敗理由)
        status: ``complete`` / ``partial`` / ``failed`` / ``nodata``
        missing_fields: ``None`` のまま (= 書かない) のフィールド名 (定義順)
        errors: 失敗の説明 (partial / failed の根拠。通知本文用)
        supplemented_fields: オートメーション JSON の欠落を AutoSync .hae で補ったフィールド
        provisional: 値が暫定 (上記のいずれか)。値が 1 つも無い (``health=None``) なら False
        withheld_fields: 返さなかった (保留した) フィールド (``missing_fields`` の部分集合)。
            **停止済み AutoSync の .hae が対象日の途中までの出力** (再出力されないので
            確定しない) だったフィールドだけ。エラーではない (status に影響しない) が、
            run レポートの欠落理由・未解決キーの追跡に使う
        provisional_fields: ``provisional=True`` のうち、暫定なのが特定のフィールドだけの
            場合のその一覧 (segment 形式の夜主睡眠が前日ファイルの不足で過少の恐れがある →
            ``("sleep_hours",)``。他のフィールドとアクティビティ詳細は確定値)。``None`` は
            日全体が暫定 (今日 / 途中経過の出力: アクティビティ詳細を含む全部の値)。
            ``provisional=False`` なら空
    """

    health: "DailyHealthData | None"
    activity_detail: "str | None"
    source: str
    detail: str
    status: str = STATUS_NODATA
    missing_fields: "tuple[str, ...]" = HEALTH_FIELDS
    errors: "tuple[str, ...]" = ()
    supplemented_fields: "tuple[str, ...]" = ()
    provisional: bool = False
    withheld_fields: "tuple[str, ...]" = ()
    provisional_fields: "tuple[str, ...] | None" = None


def _is_dir(path: Path) -> bool:
    """``Path.is_dir`` と同じだが、権限エラー等の OSError でも例外にせず False。

    (読めないディレクトリは探索側 :func:`discover_automation_files` が errors に記録する)
    """
    try:
        return path.is_dir()
    except OSError:
        return False


def resolve_autosync_dir(health_export_dir: Path) -> "Path | None":
    """``HEALTH_EXPORT_DIR`` から AutoSync ディレクトリ (``HealthMetrics`` の親) を求める。"""
    if _is_dir(health_export_dir / "HealthMetrics"):
        return health_export_dir
    candidate = health_export_dir / "AutoSync"
    if _is_dir(candidate / "HealthMetrics"):
        return candidate
    return None


#: Health Auto Export が AutoSync の出力に使う固定のフォルダ名 (Documents 直下)。
AUTOSYNC_DIR_NAME = "AutoSync"


def resolve_automation_root(health_export_dir: Path) -> Path:
    """オートメーション JSON の探索 root。AutoSync を指していればその親 (Documents root)。

    AutoSync を指しているかは ``<dir>/HealthMetrics`` の有無 **または** フォルダ名
    (``AutoSync``) で判定する。AutoSync を止めて ``HealthMetrics`` を消した (iCloud の
    容量を空けた等) 後も ``HEALTH_EXPORT_DIR=…/AutoSync`` のままだと、隣の
    オートメーションのフォルダを探さずに「データなし」を黙って返し続けるため。
    """
    if _is_dir(health_export_dir / "HealthMetrics") or health_export_dir.name == AUTOSYNC_DIR_NAME:
        return health_export_dir.parent
    return health_export_dir


def _day_end(d: date, tz: tzinfo) -> datetime:
    """``d`` の終わり (``tz`` の暦日の翌日 0:00、aware)。"""
    return datetime.combine(d + timedelta(days=1), time.min, tzinfo=tz)


def _system_now() -> datetime:
    """既定の現在時刻 (aware, UTC)。"""
    return datetime.now(timezone.utc)


def _as_aware(value: datetime) -> datetime:
    """naive な日時をシステムのローカル時刻とみなして aware にする。

    ``now`` の注入関数は aware を返すこと (統合側 :mod:`ihealth.diary` は aware な時計を
    渡す)。naive (``datetime.now()`` 等) はシステムのローカル時刻として解釈する。ただし
    夏時間の切り替わりで同じ壁時計の時刻が 2 回ある 1 時間は、1 回目の瞬間とみなされる
    (naive では区別できない) ので、正確に判定したい呼び出し側は aware を渡すこと。
    """
    if value.tzinfo is not None and value.utcoffset() is not None:
        return value
    return value.astimezone()


def _config_tz(config: Config) -> tzinfo:
    """日記の基準 tz (``IHEALTH_TZ_OFFSET``)。属性が無い古い設定オブジェクトは JST。"""
    tz = getattr(config, "diary_tz", None)
    return tz if isinstance(tz, tzinfo) else JST


def _mtime(path: Path) -> "float | None":
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _is_final(mtime: "float | None", day_end: datetime) -> bool:
    """最終更新が ``day_end`` 以降 = 対象日の全データを含みうる出力か。不明なら False。

    mtime (epoch 秒) と aware な日の終わりを絶対時刻で比べる (ローカル tz 非依存)。
    """
    return mtime is not None and mtime >= day_end.timestamp()


def _format_instant(stamp: "float | None", tz: "tzinfo | None") -> str:
    """epoch 秒を ``tz`` の ``YYYY-MM-DD HH:MM +HHMM`` で表す (ログ / detail 用)。"""
    if stamp is None:
        return "不明"
    try:
        return datetime.fromtimestamp(stamp, tz or timezone.utc).strftime("%Y-%m-%d %H:%M %z")
    except (OverflowError, OSError, ValueError):
        return "不明"


def _format_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M %z")


def _missing(health: "DailyHealthData | None") -> "tuple[str, ...]":
    if health is None:
        return HEALTH_FIELDS
    return tuple(name for name in HEALTH_FIELDS if getattr(health, name) is None)


def _populated(values: "Mapping[str, object]") -> int:
    return sum(1 for name in HEALTH_FIELDS if values.get(name) is not None)


@dataclass(frozen=True)
class _Chosen:
    """採用したオートメーション JSON 候補。"""

    path: Path
    payload: dict
    mtime: "float | None"
    #: 対象日の終わり (record 自身の offset の暦日の翌日 0:00、aware)
    day_end: datetime


@dataclass(frozen=True)
class _HaeRead:
    """AutoSync .hae 1 ファイルの読込結果。"""

    payload: "dict | None" = None
    error: "str | None" = None
    withheld: bool = False
    note: "str | None" = None


@dataclass(frozen=True)
class _AutoSyncFields:
    """AutoSync .hae から求めたフィールド値。"""

    values: "dict[str, object]"
    errors: "tuple[str, ...]"
    used_dirs: "frozenset[str]"
    withheld: "tuple[str, ...]"
    notes: "tuple[str, ...]"
    #: 値を決められなかったフィールド (読込 / 解凍 / 集計の失敗、保留)。
    #: 「その日のデータが無い」(None) と区別する (アクティビティ詳細で「なし」と断定しない)
    unknown: "frozenset[str]" = frozenset()


def _activity_detail(
    health: DailyHealthData,
    workouts: "Iterable[tuple[str, float]] | None",
    unknown: "Iterable[str]",
    *,
    workouts_known: bool = True,
) -> "str | None":
    """最終的なフィールド値からアクティビティ詳細を組む (W3-7 改訂の規則、両経路共通)。

    「なし」の根拠の心拍も最終的な値 (補完・妥当性検証の後) で判定する。
    """
    unknown_set = set(unknown)
    return compose_activity_detail(
        health.mindful_minutes,
        None if workouts is None else tuple(workouts),
        health.exercise_minutes,
        heart_rate_present=has_heart_rate(health),
        mindful_known=not (unknown_set & MINDFUL_FIELDS),
        workouts_known=workouts_known,
        exercise_known="exercise_minutes" not in unknown_set,
    )


class HealthLoader:
    """日付 → :class:`HealthLoadResult` のローダー (オートメーション探索結果はキャッシュ)。

    コンストラクタはファイルシステムに触れない (権限エラー等は :meth:`load` の中で
    ``failed`` として返す)。

    Args:
        config: ``health_export_dir`` / ``archive_root`` / ``max_heart_rate`` /
            ``diary_tz`` (日の終わりの既定 tz) を使う
        logger: ロガー
        index: 事前に作った :class:`AutomationIndex` (テスト / 再利用)。None なら
            初回 :meth:`index` で探索する
        discover: 探索関数 (DI)。既定 :func:`discover_automation_files`
        json_loader: JSON 読込関数 (DI)。既定 :func:`load_automation_json`
        metric_fetcher: AutoSync 1 メトリクス解凍関数 (DI)。既定
            :func:`ihealth.source.fetch_metric_for_date`
        max_depth: オートメーション探索の深さ上限
        now: 現在時刻を返す関数 (DI)。「対象日が終わったか」の判定に使う。aware を
            返すのが望ましい (naive はシステムのローカル時刻とみなす)。既定は UTC の現在時刻
    """

    def __init__(
        self,
        config: Config,
        logger: logging.Logger,
        *,
        index: "AutomationIndex | None" = None,
        discover: "Callable[..., AutomationIndex]" = discover_automation_files,
        json_loader: "Callable[[Path, logging.Logger], dict]" = load_automation_json,
        metric_fetcher: MetricFetcher = fetch_metric_for_date,
        max_depth: int = DEFAULT_MAX_DEPTH,
        now: "Callable[[], datetime]" = _system_now,
    ) -> None:
        self._config = config
        self._logger = logger
        self._index = index
        self._discover = discover
        self._json_loader = json_loader
        self._metric_fetcher = metric_fetcher
        self._max_depth = max_depth
        self._now = now
        self._tz = _config_tz(config)
        self._resolved: "tuple[Path, Path | None] | None" = None
        #: 「ワークアウト未出力」の案内を 1 回だけ出したか
        self._noted_no_workouts = False

    def _dirs(self) -> "tuple[Path, Path | None]":
        if self._resolved is None:
            export_dir = self._config.health_export_dir
            self._resolved = (
                resolve_automation_root(export_dir),
                resolve_autosync_dir(export_dir),
            )
        return self._resolved

    @property
    def automation_root(self) -> Path:
        """オートメーション JSON の探索 root。"""
        return self._dirs()[0]

    @property
    def autosync_dir(self) -> "Path | None":
        """AutoSync ディレクトリ (``HealthMetrics`` の親)。無ければ None。"""
        return self._dirs()[1]

    def index(self) -> AutomationIndex:
        """オートメーション JSON の索引 (初回だけ探索し、以降はキャッシュ)。

        読めなかったディレクトリは ``index().errors`` に残る (停止検知の
        ``latest_date()`` を見るときは errors も確認すること)。
        """
        if self._index is None:
            self._index = self._discover(
                self.automation_root, max_depth=self._max_depth, logger=self._logger,
            )
        return self._index

    def load(self, d: date) -> HealthLoadResult:
        """``d`` の健康データを読み込む。例外は投げず、失敗は ``status="failed"`` で返す。"""
        try:
            result = self._load(d)
        except Exception as exc:  # noqa: BLE001 — 1 日の失敗で run 全体を止めない
            # 想定外 (実装バグ等) も traceback 付きでログに残し、failed として返す。
            # 統合側が source 失敗として通知 + exit≠0 にするので黙殺にはならない。
            self._logger.exception("健康データの読み込みで想定外のエラー (date=%s)", d)
            result = HealthLoadResult(
                health=None,
                activity_detail=None,
                source=SOURCE_NONE,
                detail=f"想定外エラー: {type(exc).__name__}: {exc}",
                status=STATUS_FAILED,
                errors=(f"{type(exc).__name__}: {exc}",),
            )
        populated = len(HEALTH_FIELDS) - len(result.missing_fields)
        log_level = (
            logging.WARNING if result.status in (STATUS_FAILED, STATUS_PARTIAL)
            else logging.INFO
        )
        self._logger.log(
            log_level,
            "健康データ %s: source=%s status=%s 取得=%d/%d%s%s%s (%s)",
            d, result.source, result.status, populated, len(HEALTH_FIELDS),
            f" 補完={','.join(result.supplemented_fields)}" if result.supplemented_fields else "",
            f" 保留={','.join(result.withheld_fields)}" if result.withheld_fields else "",
            " 暫定" if result.provisional else "",
            result.detail,
        )
        return result

    # ---------- 内部実装 ----------

    def _load(self, d: date) -> HealthLoadResult:
        export_dir = self._config.health_export_dir
        try:
            exists = export_dir.is_dir()
        except OSError as exc:
            message = (
                f"HEALTH_EXPORT_DIR を確認できません: {export_dir}: "
                f"{type(exc).__name__}: {exc}"
            )
            return HealthLoadResult(
                health=None, activity_detail=None, source=SOURCE_NONE,
                detail=message, status=STATUS_FAILED, errors=(message,),
            )
        if not exists:
            message = f"HEALTH_EXPORT_DIR が存在しないかディレクトリではありません: {export_dir}"
            return HealthLoadResult(
                health=None, activity_detail=None, source=SOURCE_NONE,
                detail=message, status=STATUS_FAILED, errors=(message,),
            )

        ctx = ParseContext(
            target_date=d, max_heart_rate=self._config.max_heart_rate(as_of=d),
        )
        now = _as_aware(self._now())
        # 探索で読めなかったディレクトリがある → 候補を取りこぼしている可能性がある。
        # 「無い」を nodata と言い切れないので全日付の結果に失敗として載せる
        errors: "list[str]" = [f"探索: {message}" for message in self.index().errors]
        notes: "list[str]" = []
        chosen = self._select_automation(d, errors, notes)
        if chosen is not None:
            return self._from_automation(d, ctx, chosen, errors, notes, now)
        return self._from_autosync_only(d, ctx, errors, notes, now)

    def _select_automation(
        self, d: date, errors: "list[str]", notes: "list[str]"
    ) -> "_Chosen | None":
        """``d`` の候補を最良 (mtime 降順) から順に読み、対象日のデータを含む最初の 1 つを返す。

        * 壊れた候補 (materialize 失敗 / JSON 不正) は errors に記録して次へ
        * 壊れた候補より後ろ (= 古い) の候補は、対象日の終わり以降に書かれた **確定** の
          ものだけを使う (読めなかった新しい候補より古い途中経過で既存値を上書きしない)。
          日の終わりはその候補の record 自身の offset で決める (読んでから判定する)
        * 対象日の record を含まない候補 (他の日のデータだけ等) は skip
        """
        load_failed = False
        for path in self.index().candidates(d):
            mtime = _mtime(path)
            try:
                payload = self._json_loader(path, self._logger)
            except AutomationError as exc:
                self._logger.warning(
                    "オートメーション JSON を読めません。次の候補へ: %s", exc,
                )
                errors.append(f"automation {path.name}: {exc}")
                load_failed = True
                continue
            if not has_data_for(payload, d):
                note = f"{path} に対象日 ({d}) の metric データがありません"
                self._logger.info("オートメーション JSON を skip: %s", note)
                notes.append(note)
                continue
            day_end = record_day_end(payload, d, self._tz)
            if load_failed and not _is_final(mtime, day_end):
                note = (
                    f"{path} (最終更新 {_format_instant(mtime, day_end.tzinfo)}) は"
                    f"対象日の終わり ({_format_time(day_end)}) より前に書かれた"
                    "古い候補のため不採用 (より新しい候補を読めなかった)"
                )
                self._logger.warning("オートメーション JSON: %s", note)
                notes.append(note)
                continue
            return _Chosen(path=path, payload=payload, mtime=mtime, day_end=day_end)
        return None

    def _from_automation(
        self,
        d: date,
        ctx: ParseContext,
        chosen: _Chosen,
        errors: "list[str]",
        notes: "list[str]",
        now: datetime,
    ) -> HealthLoadResult:
        day_end = chosen.day_end
        day_ended = now >= day_end
        # 対象日がまだ終わっていない (今日) なら値は途中経過 = 暫定
        provisional = not day_ended
        if day_ended and not _is_final(chosen.mtime, day_end):
            # 日は終わったが、ファイルは終わる前に書き出された途中経過のまま (HAE が未再出力)。
            # 値は暫定値として返す (全保留しない)。オートメーションが直近の日を再出力すれば
            # 後の実行で確定値に置き換わる (統合側が台帳で追跡する)
            provisional = True
            notes.append(
                f"{chosen.path.name} は対象日の終わり ({_format_time(day_end)}) より前 "
                f"(最終更新 {_format_instant(chosen.mtime, day_end.tzinfo)}) の途中経過の"
                "出力のため暫定値 (再出力されたら確定値に置き換わる)"
            )

        #: 値のうち特定のフィールドだけが暫定のもの (日全体が暫定なら使わない)
        provisional_fields: "list[str]" = []
        prev: "_Chosen | None" = None
        prev_errors: "list[str]" = []
        prev_note = "不要"
        if needs_prev_payload(chosen.payload):
            prev_notes: "list[str]" = []
            prev = self._select_automation(d - timedelta(days=1), prev_errors, prev_notes)
            prev_note = prev.path.name if prev is not None else "なし"
            notes.extend(f"前日 {note}" for note in prev_notes)
        parsed = parse_automation_day_detailed(
            chosen.payload, d, ctx,
            prev_payload=prev.payload if prev is not None else None,
            logger=self._logger,
        )
        errors.extend(f"前日 {message}" for message in prev_errors)
        values: "dict[str, object]" = {name: getattr(parsed.health, name) for name in HEALTH_FIELDS}
        if (
            parsed.sleep_mode == SLEEP_MODE_SEGMENTS
            and parsed.main_sleep_needs_prev
            and values["sleep_hours"] is not None
        ):
            problem = self._prev_problem(prev, parsed.main_sleep_end, prev_errors)
            if problem is not None:
                # 0 時前の segment (前日ファイルにしか無い) を取りこぼした過少値かもしれない。
                # 前日ファイルが再出力されれば確定するので、保留せず暫定値として返す。
                # 暫定なのは夜主睡眠だけ (昼寝・歩数などは前日ファイルに依存しない)
                provisional_fields.append("sleep_hours")
                notes.append(f"夜主睡眠は過少の恐れがある暫定値: {problem}")
        errors.extend(f"automation {message}" for message in parsed.errors)
        if not parsed.workouts_included:
            # ワークアウトを出力しない設定 → アクティビティ詳細はエクササイズ時間から組む
            notes.append("ワークアウト未出力のためアクティビティ詳細はエクササイズ時間から組む")
            if not self._noted_no_workouts:
                self._noted_no_workouts = True
                self._logger.info(
                    "オートメーション JSON にワークアウト (data.workouts) が含まれていないため、"
                    "アクティビティ詳細は瞑想とエクササイズ時間 (apple_exercise_time) から"
                    "組みます (Health Auto Export のオートメーションでワークアウトも書き出す"
                    "設定にすると種目別に書けます): %s", chosen.path.name,
                )

        # フィールド単位補完: JSON で None のものを同日の AutoSync .hae で埋める
        supplemented: "list[str]" = []
        withheld: "list[str]" = []
        #: 補完元の .hae があるのに値を決められなかった (読込失敗 / 保留) フィールド
        supplement_unknown: "set[str]" = set()
        missing_now = [name for name in HEALTH_FIELDS if values[name] is None]
        if missing_now and self._autosync_has_date(d):
            # aggregateSleep の totalSleep は 18 時前に終わる昼寝を含み、18 時以降に
            # 終わるものは翌日付になる。補う昼寝も 18 時前に終わるものに限る
            # (翌日の totalSleep と二重に数えない)
            nap_end_hour = (
                _LEGACY_NAP_END_HOUR if parsed.sleep_mode == SLEEP_MODE_SEGMENTS
                else HAE_AGGREGATED_SLEEP_DAY_END_HOUR
            )
            filled = self._load_autosync_fields(
                d, ctx, missing_now, day_ended=day_ended, tz=day_end.tzinfo or self._tz,
                nap_end_hour=nap_end_hour,
            )
            errors.extend(filled.errors)
            notes.extend(filled.notes)
            withheld.extend(name for name in filled.withheld if name not in withheld)
            supplement_unknown = set(filled.unknown)
            for name in missing_now:
                value = filled.values.get(name)
                if value is not None:
                    values[name] = value
                    supplemented.append(name)

        dropped = list(parsed.dropped_fields)
        if supplemented:
            cleaned = drop_invalid_fields(values, logger=self._logger)
            dropped.extend(
                name for name in HEALTH_FIELDS
                if values.get(name) is not None and cleaned.get(name) is None
            )
            supplemented = [name for name in supplemented if cleaned.get(name) is not None]
            values = cleaned
        health = _rebuild_with_validation_drop(d, values)

        # アクティビティ詳細 (W3-7 改訂): JSON で決められなかった (集計失敗 / 妥当性違反) 入力と、
        # JSON に無く補完元の AutoSync .hae があるのに値を決められなかった (読込失敗 /
        # 途中までの出力で保留) 入力は、AutoSync で補えた場合を除き「不明」(瞑想やエクササイズを
        # 取りこぼした一覧や誤った「なし」を確定値として書かない。AutoSync だけの経路と同じ規則)。
        # JSON にも .hae にも無い (= その日のデータが無い) 入力は不明ではない。「なし」の根拠の
        # 心拍は補完後の値で見る (JSON に無く .hae で補えた心拍も Watch を着けていた根拠になる)
        unknown = (
            set(parsed.unknown_fields) | set(dropped) | supplement_unknown
        ) - set(supplemented)
        activity_detail = (
            _activity_detail(
                health, parsed.workouts, unknown, workouts_known=parsed.workouts_known,
            )
            if parsed.has_metrics
            else None
        )

        detail = f"automation={chosen.path} 前日={prev_note}"
        if supplemented:
            detail += f" 補完(autosync)={','.join(supplemented)}"
        if dropped:
            detail += f" 妥当性違反で除外={','.join(dropped)}"
        if notes:
            detail += f" ({'; '.join(notes)})"
        return self._finish(
            health=health,
            activity_detail=activity_detail,
            source=SOURCE_AUTOMATION,
            detail=detail,
            errors=errors,
            supplemented=supplemented,
            withheld=withheld,
            provisional=provisional,
            provisional_fields=provisional_fields,
        )

    def _prev_problem(
        self,
        prev: "_Chosen | None",
        main_sleep_end: "datetime | None",
        prev_errors: "Iterable[str]",
    ) -> "str | None":
        """segment 形式の夜主睡眠に前日ファイルを結合できたか。問題があれば理由を返す。"""
        if prev is None:
            return "前日ファイルを読めませんでした" if list(prev_errors) else "前日ファイルがありません"
        if not needs_prev_payload(prev.payload):
            return f"前日ファイル {prev.path.name} の睡眠が集計形式で segment を結合できません"
        end = main_sleep_end
        if end is not None and (end.tzinfo is None or end.utcoffset() is None):
            # offset の無い日時は日記の基準 tz とみなす (Mac のローカル tz に依存させない)
            end = end.replace(tzinfo=self._tz)
        if end is None or prev.mtime is None or prev.mtime < end.timestamp():
            tz = end.tzinfo if end is not None else prev.day_end.tzinfo
            return (
                f"前日ファイル {prev.path.name} (最終更新 {_format_instant(prev.mtime, tz)}) は"
                f"夜主睡眠の終了 ({_format_time(end) if end is not None else '不明'}) より前に"
                "書かれています"
            )
        return None

    def _from_autosync_only(
        self,
        d: date,
        ctx: ParseContext,
        errors: "list[str]",
        notes: "list[str]",
        now: datetime,
    ) -> HealthLoadResult:
        # .hae の日時 (Mac absolute time) は offset を持たないので、日の終わりは日記の
        # 基準 tz (IHEALTH_TZ_OFFSET) の暦日で判定する
        day_ended = now >= _day_end(d, self._tz)
        if not self._autosync_has_date(d):
            if notes:
                detail = "; ".join(notes)
            elif errors:
                detail = "オートメーション JSON を読めず、AutoSync .hae もありません"
            else:
                detail = "対象日のオートメーション JSON / AutoSync .hae がありません"
            # 失敗 (候補が全部読めない) なら source=automation、nodata なら _finish が none にする
            return self._finish(
                health=None, activity_detail=None, source=SOURCE_AUTOMATION,
                detail=detail, errors=errors, supplemented=(), withheld=(),
                provisional=False,
            )

        filled = self._load_autosync_fields(
            d, ctx, HEALTH_FIELDS, day_ended=day_ended, tz=self._tz,
            nap_end_hour=_LEGACY_NAP_END_HOUR,
        )
        errors.extend(filled.errors)
        notes.extend(filled.notes)
        health = _rebuild_with_validation_drop(d, filled.values)
        dropped = [
            name for name in HEALTH_FIELDS
            if filled.values.get(name) is not None and getattr(health, name) is None
        ]
        # アクティビティ詳細 (W3-7 改訂): AutoSync のワークアウト (AutoSync/Workouts/) は読まないので
        # 「ワークアウトを出力しない設定」と同じ規則 (瞑想 + エクササイズ時間) で組む。何も無い日の
        # 「なし」は heart_rate の .hae に当日の心拍がある (保留・読込失敗でない) 日だけ
        activity_detail = _activity_detail(
            health, None, set(filled.unknown) | set(dropped),
        )
        detail = (
            f"autosync={self.autosync_dir} "
            f"dirs={len(filled.used_dirs)}/{len(set(METRIC_DIRS.values()))}"
        )
        if dropped:
            detail += f" 妥当性違反で除外={','.join(dropped)}"
        if notes:
            detail += f" ({'; '.join(notes)})"
        return self._finish(
            health=health, activity_detail=activity_detail, source=SOURCE_AUTOSYNC,
            detail=detail, errors=errors, supplemented=(), withheld=filled.withheld,
            provisional=not day_ended,
        )

    def _finish(
        self,
        *,
        health: "DailyHealthData | None",
        activity_detail: "str | None",
        source: str,
        detail: str,
        errors: "Iterable[str]",
        supplemented: "Iterable[str]",
        withheld: "Iterable[str]",
        provisional: bool,
        provisional_fields: "Iterable[str]" = (),
    ) -> HealthLoadResult:
        """完全性ステータスを決めて結果を組む。

        * 1 フィールドも取れず活動詳細も無い → health=None、エラーがあれば failed / 無ければ nodata
          (停止済み AutoSync の途中までの出力を保留しただけなら nodata)
        * 取れたがエラーもある → partial
        * それ以外 → complete

        ``provisional`` は日全体の値が暫定 (今日 / 途中経過の出力)、``provisional_fields`` は
        特定のフィールドだけが暫定 (segment 形式の夜主睡眠)。日全体が暫定なら
        ``HealthLoadResult.provisional_fields`` は None (全フィールドが暫定)。
        """
        error_tuple = tuple(errors)
        if health is not None and activity_detail is None and _populated(
            {name: getattr(health, name) for name in HEALTH_FIELDS}
        ) == 0:
            health = None
        withheld_set = set(withheld)
        withheld_tuple = tuple(
            name for name in HEALTH_FIELDS
            if name in withheld_set and (health is None or getattr(health, name) is None)
        )
        field_set = set(provisional_fields)
        field_tuple = tuple(
            name for name in HEALTH_FIELDS
            if name in field_set and health is not None and getattr(health, name) is not None
        )
        if health is None:
            status = STATUS_FAILED if error_tuple else STATUS_NODATA
            return HealthLoadResult(
                health=None,
                activity_detail=None,
                source=source if status == STATUS_FAILED else SOURCE_NONE,
                detail=detail,
                status=status,
                missing_fields=HEALTH_FIELDS,
                errors=error_tuple,
                withheld_fields=withheld_tuple,
            )
        return HealthLoadResult(
            health=health,
            activity_detail=activity_detail,
            source=source,
            detail=detail,
            status=STATUS_PARTIAL if error_tuple else STATUS_COMPLETE,
            missing_fields=_missing(health),
            errors=error_tuple,
            supplemented_fields=tuple(supplemented),
            provisional=provisional or bool(field_tuple),
            withheld_fields=withheld_tuple,
            provisional_fields=None if provisional else field_tuple,
        )

    # ---------- AutoSync (.hae) ----------

    def _hae_path(self, dir_name: str, d: date) -> "Path | None":
        autosync = self.autosync_dir
        if autosync is None:
            return None
        return autosync / "HealthMetrics" / dir_name / (d.strftime("%Y%m%d") + ".hae")

    def _autosync_has_date(self, d: date) -> bool:
        """``d`` の .hae が 1 メトリクスでも存在するか (stat のみ。dataless でも安全)。

        存在を確認できない (権限エラー等) ものは「ありうる」として True を返し、
        読み込み側でエラーとして報告させる。
        """
        if self.autosync_dir is None:
            return False
        for dir_name in sorted(set(METRIC_DIRS.values())):
            path = self._hae_path(dir_name, d)
            if path is None:
                continue
            try:
                os.stat(path)
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError:
                return True
            return True
        return False

    def _read_autosync_metric(
        self, dir_name: str, d: date, *, require_final: bool, day_end: datetime
    ) -> _HaeRead:
        """1 メトリクスの .hae を解凍して payload を返す。

        * .hae が存在しない → 空の結果 (データなし)
        * ``require_final`` で、最終更新が ``day_end`` (``d`` の終わり) より前 → 解凍せず
          ``withheld`` (停止済み AutoSync は再出力されないので確定しない過少値)
        * 存在するのに stat / 解凍 / 読込に失敗 → ``error`` (例外は投げない。
          補完は best-effort で、失敗してもオートメーション JSON の値は残す)
        """
        src = self._hae_path(dir_name, d)
        if src is None:
            return _HaeRead()
        label = f"autosync {dir_name}/{src.name}"
        try:
            mtime: "float | None" = os.stat(src).st_mtime
        except (FileNotFoundError, NotADirectoryError):
            return _HaeRead()
        except OSError as exc:
            return _HaeRead(error=f"{label}: stat に失敗: {type(exc).__name__}: {exc}")
        if require_final and not _is_final(mtime, day_end):
            return _HaeRead(
                withheld=True,
                note=(
                    f"{label} は対象日の終わり ({_format_time(day_end)}) より前 (最終更新 "
                    f"{_format_instant(mtime, day_end.tzinfo)}) の途中までの出力 "
                    "(AutoSync は停止済みで再出力されない) のため保留"
                ),
            )
        try:
            archived = self._metric_fetcher(
                health_export_dir=self.autosync_dir,
                metric_dir_name=dir_name,
                target_date=d,
                archive_root=self._config.archive_root,
                logger=self._logger,
            )
        except Exception as exc:  # noqa: BLE001 — I/O 境界。失敗は error として報告する
            self._logger.warning("%s の解凍で例外が発生しました", label, exc_info=True)
            return _HaeRead(error=f"{label}: 解凍に失敗: {type(exc).__name__}: {exc}")
        if archived is None:
            return _HaeRead(error=f"{label}: materialize / 解凍に失敗")
        try:
            payload = json.loads(Path(archived).read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
            return _HaeRead(error=f"{label}: 読込失敗: {type(exc).__name__}: {exc}")
        if not isinstance(payload, dict):
            return _HaeRead(error=f"{label}: JSON の構造が不正です")
        return _HaeRead(payload=payload)

    def _load_autosync_fields(
        self,
        d: date,
        ctx: ParseContext,
        fields: "Iterable[str]",
        *,
        day_ended: bool,
        tz: tzinfo,
        nap_end_hour: int,
    ) -> _AutoSyncFields:
        """必要なフィールドの .hae だけを解凍し、:data:`ihealth.parser.FIELD_SOURCES` の
        集計関数 (旧パイプラインと同一) で値を求める。

        * 対象日が終わっているのに終わりより前に書かれた .hae は使わない (``withheld``)。
          日の終わりは ``tz`` の暦日 (.hae の日時は offset を持たない)
        * sleep は Issue #14 と同じく当日 + 前日の ``sleep_analysis`` を結合して渡す。
          ただし当日の .hae を使えない (無い / 失敗 / 保留) ときは前日分だけで
          集計しない (日付をまたぐセッションしか入っていない過少値になる)。
          当日の .hae は「その日に重なる全 record」を含むので、前日の .hae は
          確定版だけを補助的に結合し、その読込失敗はエラーとして報告するが当日の値は残す
        * 昼寝は終了時刻 [14, ``nap_end_hour``) の asleep
        * 読込 / 解凍 / 集計に失敗した、または保留したフィールドは ``unknown`` に載せる
        """
        wanted = [name for name in fields if name in FIELD_SOURCES]
        dirs: "list[str]" = []
        for name in wanted:
            dir_name = FIELD_SOURCES[name][0]
            if dir_name not in dirs:
                dirs.append(dir_name)

        day_end = _day_end(d, tz)
        errors: "list[str]" = []
        notes: "list[str]" = []
        payloads: "dict[str, dict]" = {}
        withheld_dirs: "set[str]" = set()
        failed_dirs: "set[str]" = set()
        failed_fields: "set[str]" = set()
        for dir_name in dirs:
            read = self._read_autosync_metric(
                dir_name, d, require_final=day_ended, day_end=day_end,
            )
            if read.error is not None:
                errors.append(read.error)
                failed_dirs.add(dir_name)
                continue
            if read.withheld:
                withheld_dirs.add(dir_name)
                if read.note:
                    notes.append(read.note)
                continue
            if read.payload is None:
                continue
            payload = read.payload
            if dir_name != _SLEEP_DIR:
                # 睡眠以外の旧集計関数は日付で絞らないので、他の日の record を除く
                # (sleep は parser 側が end の日付で絞るうえ前日分も必要なので対象外)
                payload, foreign = _filter_autosync_records_by_date(payload, d)
                if foreign:
                    self._logger.warning(
                        "autosync %s/%s に他の日付の record が %d 件含まれていたため除外しました",
                        dir_name, d.strftime("%Y%m%d"), foreign,
                    )
            payloads[dir_name] = payload

        if _SLEEP_DIR in payloads:
            prev_read = self._read_autosync_metric(
                _SLEEP_DIR, d - timedelta(days=1), require_final=True,
                day_end=_day_end(d - timedelta(days=1), tz),
            )
            if prev_read.error is not None:
                errors.append(f"前日 {prev_read.error}")
            prev_data = _data_list(prev_read.payload)
            if prev_data:
                payloads[_SLEEP_DIR] = {
                    "metric": "Sleep Analysis (merged)",
                    "data": [*_data_list(payloads[_SLEEP_DIR]), *prev_data],
                }

        values: "dict[str, object]" = {}
        for name in wanted:
            dir_name, func = FIELD_SOURCES[name]
            payload = payloads.get(dir_name)
            if payload is None:
                values[name] = None
                continue
            try:
                if name == "nap_hours":
                    values[name] = _sum_asleep_by_source(payload, d, 14, nap_end_hour)
                else:
                    values[name] = func(payload, ctx)
            except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                self._logger.warning(
                    "autosync field %s の集計に失敗しました (date=%s): %s", name, d, exc,
                )
                errors.append(f"autosync {name}: {type(exc).__name__}: {exc}")
                values[name] = None
                failed_fields.add(name)
        withheld = tuple(
            name for name in wanted if FIELD_SOURCES[name][0] in withheld_dirs
        )
        unknown = frozenset(
            name for name in wanted
            if name in failed_fields
            or FIELD_SOURCES[name][0] in failed_dirs
            or FIELD_SOURCES[name][0] in withheld_dirs
        )
        return _AutoSyncFields(
            values=values,
            errors=tuple(errors),
            used_dirs=frozenset(payloads),
            withheld=withheld,
            unknown=unknown,
            notes=tuple(notes),
        )


def _data_list(payload: "Mapping[str, object] | None") -> "list":
    if not isinstance(payload, Mapping):
        return []
    data = payload.get("data")
    return list(data) if isinstance(data, list) else []


def _filter_autosync_records_by_date(
    payload: "Mapping[str, object]", d: date
) -> "tuple[dict, int]":
    """AutoSync payload の ``data`` から ``start`` (Mac absolute time) の JST 日付が
    ``d`` でない record を除いた新しい payload と、除いた件数を返す。

    AutoSync が同期の遅れを取り戻す際に、1 ファイルへ複数日分の record を
    まとめて書くことがある。旧集計関数 (睡眠以外) は日付で絞らないので、そのまま
    渡すと複数日分が 1 日に計上される。``start`` が無い / 数値でない record は
    日付を判定できないので残す (旧挙動を維持)。
    """
    kept: "list" = []
    foreign = 0
    for record in _data_list(payload):
        start = record.get("start") if isinstance(record, Mapping) else None
        if isinstance(start, (int, float)) and not isinstance(start, bool):
            try:
                record_date = _jst_datetime(start).date()
            except (OverflowError, ValueError):
                record_date = None
            if record_date is not None and record_date != d:
                foreign += 1
                continue
        kept.append(record)
    filtered = dict(payload)
    filtered["data"] = kept
    return filtered, foreign
