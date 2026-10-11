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
* JSON が無い日は AutoSync .hae だけで集計する (旧パイプラインと同じ集計関数)。
  アクティビティ詳細はワークアウトを読まないので書かない (``None``)
* どちらも無ければ ``nodata``、読めなければ ``failed``

を返す。各結果には完全性ステータス (``complete`` / ``partial`` / ``failed`` /
``nodata``) を付け、統合側 (diary) が「書く / 既存値を保持する / 通知する」を
判断できるようにする。

**途中までの出力は確定値として返さない** (「確定」ルール): HAE はファイルを
書き出した時点までのデータしか含めない。ファイルの最終更新 (mtime) が対象日の
終了 (ローカル時刻の翌日 0:00) より前なら、そのファイルは対象日の途中までしか
含まない。

* 対象日が **まだ終わっていない** (今日) → 値を返し ``provisional=True`` (後の実行で
  上書きされる暫定値。§4.6)
* 対象日が **もう終わっている** → そのファイル由来の値は返さず ``withheld_fields``
  に載せる (既存値を保持)。オートメーションは後で再出力されれば次回以降の実行で
  確定値が入る。AutoSync (停止済み) は二度と更新されないので恒久的に保留
* 最良候補を読めず古い候補へ fallback するときは、その候補が対象日の終了後に
  書かれたもの (確定) である場合に限る (読めなかった新しい候補より古い値で既存値を
  上書きしないため。今日の分は fallback しない)

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
from datetime import date, datetime, time, timedelta
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
    SLEEP_MODE_SEGMENTS,
    build_activity_detail,
    has_data_for,
    needs_prev_payload,
    parse_automation_day_detailed,
)
from ihealth.config import Config
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
#: 正常に探したが対象日の確定データが存在しない (HAE 未出力・出力が途中までなど。通知不要)
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
      非 None の値は status が partial でも「確実に取れた値」: 失敗した / 過少の恐れが
      ある / 対象日の途中までの出力に由来するフィールドは None に落としてある
    * ``provisional=True`` は対象日がまだ終わっていない (今日) ことを表す。値は
      その時点までの暫定値で、後の実行で確定値に上書きされる前提 (§4.6)
    * ``failed`` は通知対象 (``errors`` に理由)。``partial`` は取れた値を書いたうえで
      ``errors`` を報告する。``nodata`` (ファイル未出力 / 出力が途中まで) は通知不要

    Attributes:
        health: 集計結果。1 フィールドも取れなかった日は ``None``
        activity_detail: ``"瞑想3分"`` / ``"なし"`` 等。判断できない日は ``None`` (書かない)
        source: ``"automation"`` / ``"autosync"`` / ``"none"`` (主に使ったデータ源)
        detail: ログ用の説明 (採用 path / 補完 / 保留 / 失敗理由)
        status: ``complete`` / ``partial`` / ``failed`` / ``nodata``
        missing_fields: ``None`` のまま (= 書かない) のフィールド名 (定義順)
        errors: 失敗の説明 (partial / failed の根拠。通知本文用)
        supplemented_fields: オートメーション JSON の欠落を AutoSync .hae で補ったフィールド
        provisional: 対象日がまだ終わっていない (= 値は暫定、後で増えうる)
        withheld_fields: データ源が **確定していない** ため返さなかったフィールド
            (``missing_fields`` の部分集合)。対象日の終了前に書かれた出力
            (オートメーション JSON は値があったフィールド、AutoSync は解凍せずに
            保留したファイルが担うフィールド)、segment 形式で前日ファイルを結合できず
            過少の恐れがある夜主睡眠など。エラーではない (status に影響しない) が、
            run レポートで欠落理由を示すのに使う
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


def _day_end(d: date) -> datetime:
    """``d`` の終了時刻 (ローカルの翌日 0:00、naive)。"""
    return datetime.combine(d + timedelta(days=1), time.min)


def _mtime(path: Path) -> "float | None":
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _is_final(mtime: "float | None", d: date) -> bool:
    """最終更新が ``d`` の終了後 = ``d`` の全データを含みうる出力か。不明なら False。"""
    return mtime is not None and datetime.fromtimestamp(mtime) >= _day_end(d)


def _format_mtime(mtime: "float | None") -> str:
    if mtime is None:
        return "不明"
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")


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


class HealthLoader:
    """日付 → :class:`HealthLoadResult` のローダー (オートメーション探索結果はキャッシュ)。

    コンストラクタはファイルシステムに触れない (権限エラー等は :meth:`load` の中で
    ``failed`` として返す)。

    Args:
        config: ``health_export_dir`` / ``archive_root`` / ``max_heart_rate`` を使う
        logger: ロガー
        index: 事前に作った :class:`AutomationIndex` (テスト / 再利用)。None なら
            初回 :meth:`index` で探索する
        discover: 探索関数 (DI)。既定 :func:`discover_automation_files`
        json_loader: JSON 読込関数 (DI)。既定 :func:`load_automation_json`
        metric_fetcher: AutoSync 1 メトリクス解凍関数 (DI)。既定
            :func:`ihealth.source.fetch_metric_for_date`
        max_depth: オートメーション探索の深さ上限
        now: 現在時刻 (ローカル naive) を返す関数 (DI)。「対象日が終わったか」の判定に使う
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
        now: "Callable[[], datetime]" = datetime.now,
    ) -> None:
        self._config = config
        self._logger = logger
        self._index = index
        self._discover = discover
        self._json_loader = json_loader
        self._metric_fetcher = metric_fetcher
        self._max_depth = max_depth
        self._now = now
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
        day_ended = self._now() >= _day_end(d)
        # 探索で読めなかったディレクトリがある → 候補を取りこぼしている可能性がある。
        # 「無い」を nodata と言い切れないので全日付の結果に失敗として載せる
        errors: "list[str]" = [f"探索: {message}" for message in self.index().errors]
        notes: "list[str]" = []
        chosen = self._select_automation(d, errors, notes)
        if chosen is not None:
            return self._from_automation(d, ctx, chosen, errors, notes, day_ended)
        return self._from_autosync_only(d, ctx, errors, notes, day_ended)

    def _select_automation(
        self, d: date, errors: "list[str]", notes: "list[str]"
    ) -> "_Chosen | None":
        """``d`` の候補を最良 (mtime 降順) から順に読み、対象日のデータを含む最初の 1 つを返す。

        * 壊れた候補 (materialize 失敗 / JSON 不正) は errors に記録して次へ
        * 壊れた候補より後ろ (= 古い) の候補は、対象日の終了後に書かれた **確定** の
          ものだけを使う (読めなかった新しい候補より古い途中経過で既存値を上書きしない)
        * 対象日の record を含まない候補 (他の日のデータだけ等) は skip
        """
        load_failed = False
        for path in self.index().candidates(d):
            mtime = _mtime(path)
            if load_failed and not _is_final(mtime, d):
                note = (
                    f"{path} (最終更新 {_format_mtime(mtime)}) は対象日の終了前に書かれた"
                    "古い候補のため不採用 (より新しい候補を読めなかった)"
                )
                self._logger.warning("オートメーション JSON: %s", note)
                notes.append(note)
                continue
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
            return _Chosen(path=path, payload=payload, mtime=mtime)
        return None

    def _from_automation(
        self,
        d: date,
        ctx: ParseContext,
        chosen: _Chosen,
        errors: "list[str]",
        notes: "list[str]",
        day_ended: bool,
    ) -> HealthLoadResult:
        withheld: "list[str]" = []
        prev_note = "不要"
        json_withheld = day_ended and not _is_final(chosen.mtime, d)
        if json_withheld:
            # 対象日が終わったのに、ファイルは終わる前の書き出しのまま → 全フィールド保留
            parsed = parse_automation_day_detailed(
                chosen.payload, d, ctx, logger=self._logger,
            )
            values: "dict[str, object]" = {name: None for name in HEALTH_FIELDS}
            withheld.extend(
                name for name in HEALTH_FIELDS if getattr(parsed.health, name) is not None
            )
            activity_detail: "str | None" = None
            notes.append(
                f"{chosen.path.name} は対象日の終了前 (最終更新 "
                f"{_format_mtime(chosen.mtime)}) の途中までの出力のため保留"
            )
        else:
            prev: "_Chosen | None" = None
            prev_errors: "list[str]" = []
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
            values = {name: getattr(parsed.health, name) for name in HEALTH_FIELDS}
            activity_detail = parsed.activity_detail
            if (
                parsed.sleep_mode == SLEEP_MODE_SEGMENTS
                and parsed.main_sleep_needs_prev
                and values["sleep_hours"] is not None
            ):
                problem = self._prev_problem(prev, parsed.main_sleep_end, prev_errors)
                if problem is not None:
                    # 0 時前の segment を取りこぼした過少値かもしれない → 書かない (§4.1)
                    values["sleep_hours"] = None
                    withheld.append("sleep_hours")
                    notes.append(f"夜主睡眠を保留: {problem}")
        errors.extend(f"automation {message}" for message in parsed.errors)
        if not json_withheld and not parsed.workouts_included:
            # ワークアウトを出力しない設定 → アクティビティ詳細は判断できない (書かない)
            notes.append("ワークアウト未出力のためアクティビティ詳細は書かない")
            if not self._noted_no_workouts:
                self._noted_no_workouts = True
                self._logger.info(
                    "オートメーション JSON にワークアウト (data.workouts) が含まれていないため、"
                    "アクティビティ詳細は書きません (Health Auto Export のオートメーションで"
                    "ワークアウトも書き出す設定にすると書けます): %s", chosen.path.name,
                )

        # フィールド単位補完: JSON で None のものを同日の AutoSync .hae で埋める
        supplemented: "list[str]" = []
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
                d, ctx, missing_now, day_ended=day_ended, nap_end_hour=nap_end_hour,
            )
            errors.extend(filled.errors)
            notes.extend(filled.notes)
            withheld.extend(name for name in filled.withheld if name not in withheld)
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

        if any(name in supplemented for name in ("mindful_minutes", "mindful_sessions")):
            # JSON に瞑想が無かった (or 解釈できなかった) 日を AutoSync の瞑想で補った。
            # ワークアウトは JSON 由来なので、JSON を採用していて解釈できたときだけ組み直す
            activity_detail = (
                build_activity_detail(health.mindful_minutes, parsed.workouts)
                if not json_withheld and parsed.workouts is not None
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
            day_ended=day_ended,
        )

    @staticmethod
    def _prev_problem(
        prev: "_Chosen | None",
        main_sleep_end: "datetime | None",
        prev_errors: "Iterable[str]",
    ) -> "str | None":
        """segment 形式の夜主睡眠に前日ファイルを結合できたか。問題があれば理由を返す。"""
        if prev is None:
            return "前日ファイルを読めませんでした" if list(prev_errors) else "前日ファイルがありません"
        if not needs_prev_payload(prev.payload):
            return f"前日ファイル {prev.path.name} の睡眠が集計形式で segment を結合できません"
        if (
            main_sleep_end is None
            or prev.mtime is None
            or prev.mtime < main_sleep_end.timestamp()
        ):
            return (
                f"前日ファイル {prev.path.name} (最終更新 {_format_mtime(prev.mtime)}) は"
                f"夜主睡眠の終了 ({main_sleep_end}) より前に書かれています"
            )
        return None

    def _from_autosync_only(
        self,
        d: date,
        ctx: ParseContext,
        errors: "list[str]",
        notes: "list[str]",
        day_ended: bool,
    ) -> HealthLoadResult:
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
                day_ended=day_ended,
            )

        filled = self._load_autosync_fields(
            d, ctx, HEALTH_FIELDS, day_ended=day_ended, nap_end_hour=_LEGACY_NAP_END_HOUR,
        )
        errors.extend(filled.errors)
        notes.extend(filled.notes)
        health = _rebuild_with_validation_drop(d, filled.values)
        dropped = [
            name for name in HEALTH_FIELDS
            if filled.values.get(name) is not None and getattr(health, name) is None
        ]
        # アクティビティ詳細は書かない: AutoSync のワークアウト (``AutoSync/Workouts/``)
        # は読んでいないので、瞑想だけで「瞑想N分 / なし」と断定すると、その日に実際に
        # あったワークアウトを含む既存の値 (旧 Notion 時代の値など) を消してしまう
        activity_detail: "str | None" = None
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
            day_ended=day_ended,
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
        day_ended: bool,
    ) -> HealthLoadResult:
        """完全性ステータスを決めて結果を組む。

        * 1 フィールドも取れず活動詳細も無い → health=None、エラーがあれば failed / 無ければ nodata
          (確定していないデータを保留しただけなら nodata)
        * 取れたがエラーもある → partial
        * それ以外 → complete
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
            provisional=not day_ended,
            withheld_fields=withheld_tuple,
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
        self, dir_name: str, d: date, *, require_final: bool
    ) -> _HaeRead:
        """1 メトリクスの .hae を解凍して payload を返す。

        * .hae が存在しない → 空の結果 (データなし)
        * ``require_final`` で、最終更新が ``d`` の終了前 → 解凍せず ``withheld``
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
        if require_final and not _is_final(mtime, d):
            return _HaeRead(
                withheld=True,
                note=(
                    f"{label} は対象日の終了前 (最終更新 {_format_mtime(mtime)}) の"
                    "途中までの出力のため保留"
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
        nap_end_hour: int,
    ) -> _AutoSyncFields:
        """必要なフィールドの .hae だけを解凍し、:data:`ihealth.parser.FIELD_SOURCES` の
        集計関数 (旧パイプラインと同一) で値を求める。

        * 対象日が終わっているのに終了前に書かれた .hae は使わない (``withheld``)
        * sleep は Issue #14 と同じく当日 + 前日の ``sleep_analysis`` を結合して渡す。
          ただし当日の .hae を使えない (無い / 失敗 / 保留) ときは前日分だけで
          集計しない (日付をまたぐセッションしか入っていない過少値になる)。
          当日の .hae は「その日に重なる全 record」を含むので、前日の .hae は
          確定版だけを補助的に結合し、その読込失敗はエラーとして報告するが当日の値は残す
        * 昼寝は終了時刻 [14, ``nap_end_hour``) の asleep
        """
        wanted = [name for name in fields if name in FIELD_SOURCES]
        dirs: "list[str]" = []
        for name in wanted:
            dir_name = FIELD_SOURCES[name][0]
            if dir_name not in dirs:
                dirs.append(dir_name)

        errors: "list[str]" = []
        notes: "list[str]" = []
        payloads: "dict[str, dict]" = {}
        withheld_dirs: "set[str]" = set()
        for dir_name in dirs:
            read = self._read_autosync_metric(dir_name, d, require_final=day_ended)
            if read.error is not None:
                errors.append(read.error)
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
        withheld = tuple(
            name for name in wanted if FIELD_SOURCES[name][0] in withheld_dirs
        )
        return _AutoSyncFields(
            values=values,
            errors=tuple(errors),
            used_dirs=frozenset(payloads),
            withheld=withheld,
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
