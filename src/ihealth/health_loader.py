"""1 日分の健康データを「オートメーション JSON 優先 + AutoSync .hae 補完」で読み込む。

データ源は 2 系統ある:

1. **オートメーション JSON** (現役): ``<HAE Documents>/<名前>/HealthAutoExport-YYYY-MM-DD.json``
   (:mod:`ihealth.automation` / :mod:`ihealth.automation_parser`)
2. **AutoSync .hae** (2025-11 〜 2026-09 で停止した旧経路):
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

``HEALTH_EXPORT_DIR`` は HAE の Documents root でも AutoSync ディレクトリでも動く:

* ``<dir>/HealthMetrics`` がある → ``<dir>`` は AutoSync。オートメーション探索
  root はその親 (= Documents root。AutoSync 自体も配下に含まれる)
* ``<dir>/AutoSync/HealthMetrics`` がある → AutoSync は ``<dir>/AutoSync``、探索 root は ``<dir>``
* どちらも無い → AutoSync なし、探索 root は ``<dir>``
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
    HEALTH_FIELDS,
    build_activity_detail,
    extract_workouts,
    has_metrics,
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
    drop_invalid_fields,
)
from ihealth.source import METRIC_DIRS, fetch_metric_for_date


SOURCE_AUTOMATION = "automation"
SOURCE_AUTOSYNC = "autosync"
SOURCE_NONE = "none"

#: 全部取れた (欠けているフィールドは「その日そのデータが無い」だけ)
STATUS_COMPLETE = "complete"
#: 一部の読込・集計が失敗した (取れたフィールドは確実)
STATUS_PARTIAL = "partial"
#: 取得不能 (ファイルはあるが読めない / 想定外エラー / 設定不備)
STATUS_FAILED = "failed"
#: 正常に探したが対象日のデータが存在しない (HAE 未出力など。通知不要)
STATUS_NODATA = "nodata"

_SLEEP_DIR = "sleep_analysis"
_MINDFUL_DIR = "mindful_minutes"

#: AutoSync の 1 メトリクス解凍関数 (``ihealth.source.fetch_metric_for_date`` 互換)。
MetricFetcher = Callable[..., "Path | None"]


@dataclass(frozen=True)
class HealthLoadResult:
    """:meth:`HealthLoader.load` の結果。

    ``health`` の **非 None フィールドは status が partial でも「確実に取れた値」**
    (失敗した / 過少の恐れがあるフィールドは None に落としてある) なので、統合側は
    非 None のフィールドだけを書けばよい (None = 書かない = 既存値を保持)。

    Attributes:
        health: 集計結果。1 フィールドも取れなかった日は ``None``
        activity_detail: ``"瞑想3分"`` / ``"なし"`` 等。判断できない日は ``None`` (書かない)
        source: ``"automation"`` / ``"autosync"`` / ``"none"`` (主に使ったデータ源)
        detail: ログ用の説明 (採用 path / 補完 / 失敗理由)
        status: ``complete`` / ``partial`` / ``failed`` / ``nodata``
        missing_fields: ``None`` のまま (= 書かない) のフィールド名 (定義順)
        errors: 失敗の説明 (partial / failed の根拠。通知本文用)
        supplemented_fields: オートメーション JSON の欠落を AutoSync .hae で補ったフィールド
        provisional: 採用したオートメーション JSON の最終更新が対象日の終了 (ローカル
            時刻 24:00) より前 = その日の途中までしか含まない暫定値の可能性がある
            (当日分や、翌日以降に HAE が再出力していない日)。累積系 (歩数・消費
            カロリー等) は後の再出力で増えうる
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


def resolve_autosync_dir(health_export_dir: Path) -> "Path | None":
    """``HEALTH_EXPORT_DIR`` から AutoSync ディレクトリ (``HealthMetrics`` の親) を求める。"""
    if (health_export_dir / "HealthMetrics").is_dir():
        return health_export_dir
    candidate = health_export_dir / "AutoSync"
    if (candidate / "HealthMetrics").is_dir():
        return candidate
    return None


def resolve_automation_root(health_export_dir: Path) -> Path:
    """オートメーション JSON の探索 root。AutoSync を指していればその親 (Documents root)。"""
    if (health_export_dir / "HealthMetrics").is_dir():
        return health_export_dir.parent
    return health_export_dir


def _missing(health: "DailyHealthData | None") -> "tuple[str, ...]":
    if health is None:
        return HEALTH_FIELDS
    return tuple(name for name in HEALTH_FIELDS if getattr(health, name) is None)


def _populated(values: "Mapping[str, object]") -> int:
    return sum(1 for name in HEALTH_FIELDS if values.get(name) is not None)


class HealthLoader:
    """日付 → :class:`HealthLoadResult` のローダー (オートメーション探索結果はキャッシュ)。

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
    ) -> None:
        self._config = config
        self._logger = logger
        self._index = index
        self._discover = discover
        self._json_loader = json_loader
        self._metric_fetcher = metric_fetcher
        self._max_depth = max_depth
        export_dir = config.health_export_dir
        self._autosync_dir = resolve_autosync_dir(export_dir)
        self._automation_root = resolve_automation_root(export_dir)

    @property
    def automation_root(self) -> Path:
        """オートメーション JSON の探索 root。"""
        return self._automation_root

    @property
    def autosync_dir(self) -> "Path | None":
        """AutoSync ディレクトリ (``HealthMetrics`` の親)。無ければ None。"""
        return self._autosync_dir

    def index(self) -> AutomationIndex:
        """オートメーション JSON の索引 (初回だけ探索し、以降はキャッシュ)。"""
        if self._index is None:
            self._index = self._discover(
                self._automation_root, max_depth=self._max_depth, logger=self._logger,
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
            "健康データ %s: source=%s status=%s 取得=%d/%d%s%s (%s)",
            d, result.source, result.status, populated, len(HEALTH_FIELDS),
            f" 補完={','.join(result.supplemented_fields)}" if result.supplemented_fields else "",
            " 暫定" if result.provisional else "",
            result.detail,
        )
        return result

    # ---------- 内部実装 ----------

    def _load(self, d: date) -> HealthLoadResult:
        export_dir = self._config.health_export_dir
        if not export_dir.is_dir():
            message = f"HEALTH_EXPORT_DIR が存在しないかディレクトリではありません: {export_dir}"
            return HealthLoadResult(
                health=None, activity_detail=None, source=SOURCE_NONE,
                detail=message, status=STATUS_FAILED, errors=(message,),
            )

        ctx = ParseContext(
            target_date=d, max_heart_rate=self._config.max_heart_rate(as_of=d),
        )
        errors: "list[str]" = []
        primary = self._load_automation(d, errors)
        if primary is not None and has_metrics(primary[1]):
            return self._from_automation(d, ctx, primary[0], primary[1], errors)
        return self._from_autosync_only(d, ctx, errors, empty_automation=primary)

    def _load_automation(
        self, d: date, errors: "list[str]"
    ) -> "tuple[Path, dict] | None":
        """``d`` の候補を最良から順に読み、metric を含む最初の 1 つを返す。

        壊れた候補 (materialize 失敗 / JSON 不正) は errors に記録して次へ。
        metric を 1 つも含まない候補しか読めなければその最初のものを返す
        (呼び出し側で nodata 扱い)。候補なし / 全滅なら None。
        """
        empty: "tuple[Path, dict] | None" = None
        for path in self.index().candidates(d):
            try:
                payload = self._json_loader(path, self._logger)
            except AutomationError as exc:
                self._logger.warning(
                    "オートメーション JSON を読めません。次の候補へ: %s", exc,
                )
                errors.append(f"automation {path.name}: {exc}")
                continue
            if has_metrics(payload):
                return path, payload
            self._logger.info("metric を含まないオートメーション JSON を skip: %s", path)
            if empty is None:
                empty = (path, payload)
        return empty

    def _from_automation(
        self,
        d: date,
        ctx: ParseContext,
        path: Path,
        payload: dict,
        errors: "list[str]",
    ) -> HealthLoadResult:
        prev_payload: "dict | None" = None
        prev_note = "不要"
        prev_failed = False
        if needs_prev_payload(payload):
            prev_errors: "list[str]" = []
            prev = self._load_automation(d - timedelta(days=1), prev_errors)
            if prev is not None:
                prev_payload = prev[1]
                prev_note = prev[0].name
            else:
                prev_note = "なし"
            # 前日ファイルがあるのに読めない = 前夜の segment を取りこぼした可能性 → partial
            prev_failed = prev_payload is None and bool(prev_errors)
            errors.extend(f"前日 {message}" for message in prev_errors)

        parsed = parse_automation_day_detailed(
            payload, d, ctx, prev_payload=prev_payload, logger=self._logger,
        )
        errors.extend(f"automation {message}" for message in parsed.errors)
        values: "dict[str, object]" = {
            name: getattr(parsed.health, name) for name in HEALTH_FIELDS
        }
        if prev_failed:
            # 過少かもしれない睡眠値は書かない (= 既存値を保持)。§4.1「迷うキーは渡さない」
            values["sleep_hours"] = None
            values["nap_hours"] = None

        # フィールド単位補完: JSON で None のものを同日の AutoSync .hae で埋める
        supplemented: "list[str]" = []
        missing_now = [name for name in HEALTH_FIELDS if values[name] is None]
        if missing_now and self._autosync_has_date(d):
            filled, supplement_errors, _loaded = self._load_autosync_fields(
                d, ctx, missing_now,
            )
            errors.extend(supplement_errors)
            for name in missing_now:
                value = filled.get(name)
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

        activity_detail = parsed.activity_detail
        if "mindful_minutes" in supplemented:
            activity_detail = build_activity_detail(
                health.mindful_minutes, extract_workouts(payload, d),
            )

        detail = f"automation={path} 前日={prev_note}"
        if supplemented:
            detail += f" 補完(autosync)={','.join(supplemented)}"
        if dropped:
            detail += f" 妥当性違反で除外={','.join(dropped)}"
        return self._finish(
            d,
            health=health,
            activity_detail=activity_detail,
            source=SOURCE_AUTOMATION,
            detail=detail,
            errors=errors,
            supplemented=supplemented,
            provisional=self._is_provisional(path, d),
        )

    def _from_autosync_only(
        self,
        d: date,
        ctx: ParseContext,
        errors: "list[str]",
        *,
        empty_automation: "tuple[Path, dict] | None",
    ) -> HealthLoadResult:
        if not self._autosync_has_date(d):
            if empty_automation is not None:
                detail = f"オートメーション JSON に metric がありません: {empty_automation[0]}"
            elif errors:
                detail = "オートメーション JSON を読めず、AutoSync .hae もありません"
            else:
                detail = "対象日のオートメーション JSON / AutoSync .hae がありません"
            # 失敗 (候補が全部読めない) なら source=automation、nodata なら _finish が none にする
            return self._finish(
                d, health=None, activity_detail=None, source=SOURCE_AUTOMATION,
                detail=detail, errors=errors, supplemented=(), provisional=False,
            )

        values, autosync_errors, loaded_dirs = self._load_autosync_fields(
            d, ctx, HEALTH_FIELDS,
        )
        errors.extend(autosync_errors)
        health = _rebuild_with_validation_drop(d, values)
        dropped = [
            name for name in HEALTH_FIELDS
            if values.get(name) is not None and getattr(health, name) is None
        ]
        # mindful の .hae を読めた日だけ「瞑想N分 / なし」を判断できる
        activity_detail = (
            build_activity_detail(health.mindful_minutes)
            if _MINDFUL_DIR in loaded_dirs else None
        )
        detail = f"autosync={self._autosync_dir} dirs={len(loaded_dirs)}/{len(METRIC_DIRS)}"
        if dropped:
            detail += f" 妥当性違反で除外={','.join(dropped)}"
        return self._finish(
            d, health=health, activity_detail=activity_detail, source=SOURCE_AUTOSYNC,
            detail=detail, errors=errors, supplemented=(), provisional=False,
        )

    def _finish(
        self,
        d: date,
        *,
        health: "DailyHealthData | None",
        activity_detail: "str | None",
        source: str,
        detail: str,
        errors: "Iterable[str]",
        supplemented: "Iterable[str]",
        provisional: bool,
    ) -> HealthLoadResult:
        """完全性ステータスを決めて結果を組む。

        * 1 フィールドも取れず活動詳細も無い → health=None、エラーがあれば failed / 無ければ nodata
        * 取れたがエラーもある → partial
        * それ以外 → complete
        """
        error_tuple = tuple(errors)
        if health is not None and activity_detail is None and _populated(
            {name: getattr(health, name) for name in HEALTH_FIELDS}
        ) == 0:
            health = None
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
            provisional=provisional,
        )

    @staticmethod
    def _is_provisional(path: Path, d: date) -> bool:
        """ファイルの最終更新が ``d`` の翌日 0:00 (ローカル) より前なら暫定値。"""
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            return False
        day_end = datetime.combine(d + timedelta(days=1), time.min)
        return datetime.fromtimestamp(mtime) < day_end

    # ---------- AutoSync (.hae) ----------

    def _hae_path(self, dir_name: str, d: date) -> "Path | None":
        if self._autosync_dir is None:
            return None
        return self._autosync_dir / "HealthMetrics" / dir_name / (d.strftime("%Y%m%d") + ".hae")

    def _autosync_has_date(self, d: date) -> bool:
        """``d`` の .hae が 1 メトリクスでも存在するか (stat のみ。dataless でも安全)。"""
        if self._autosync_dir is None:
            return False
        for dir_name in METRIC_DIRS.values():
            path = self._hae_path(dir_name, d)
            if path is not None and path.exists():
                return True
        return False

    def _read_autosync_metric(
        self, dir_name: str, d: date
    ) -> "tuple[dict | None, str | None]":
        """1 メトリクスの .hae を解凍して payload を返す。``(payload, error)``。

        .hae が存在しない → ``(None, None)`` (データなし)。存在するのに解凍 / 読込に
        失敗 → ``(None, エラー説明)``。
        """
        src = self._hae_path(dir_name, d)
        if src is None or not src.exists():
            return None, None
        archived = self._metric_fetcher(
            health_export_dir=self._autosync_dir,
            metric_dir_name=dir_name,
            target_date=d,
            archive_root=self._config.archive_root,
            logger=self._logger,
        )
        if archived is None:
            return None, f"autosync {dir_name}/{src.name}: materialize / 解凍に失敗"
        try:
            payload = json.loads(Path(archived).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return None, f"autosync {dir_name}/{src.name}: 読込失敗: {type(exc).__name__}: {exc}"
        if not isinstance(payload, dict):
            return None, f"autosync {dir_name}/{src.name}: JSON の構造が不正です"
        return payload, None

    def _load_autosync_fields(
        self,
        d: date,
        ctx: ParseContext,
        fields: "Iterable[str]",
    ) -> "tuple[dict[str, object], list[str], set[str]]":
        """必要なフィールドの .hae だけを解凍し、:data:`ihealth.parser.FIELD_SOURCES` の
        集計関数 (旧パイプラインと同一) で値を求める。

        sleep は Issue #14 と同じく当日 + 前日の ``sleep_analysis`` を結合して渡す
        (前夜に始まった主睡眠が前日ファイルに入るため)。

        Returns:
            ``(field → 値 or None, エラー説明, 読めた metric dir の集合)``
        """
        wanted = [name for name in fields if name in FIELD_SOURCES]
        dirs: "list[str]" = []
        for name in wanted:
            dir_name = FIELD_SOURCES[name][0]
            if dir_name not in dirs:
                dirs.append(dir_name)

        errors: "list[str]" = []
        payloads: "dict[str, dict]" = {}
        for dir_name in dirs:
            payload, error = self._read_autosync_metric(dir_name, d)
            if error is not None:
                errors.append(error)
            if payload is None:
                continue
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

        if _SLEEP_DIR in dirs:
            prev_payload, prev_error = self._read_autosync_metric(
                _SLEEP_DIR, d - timedelta(days=1),
            )
            if prev_error is not None:
                errors.append(f"前日 {prev_error}")
            today_data = _data_list(payloads.get(_SLEEP_DIR))
            prev_data = _data_list(prev_payload)
            if today_data or prev_data:
                payloads[_SLEEP_DIR] = {
                    "metric": "Sleep Analysis (merged)",
                    "data": [*today_data, *prev_data],
                }

        values: "dict[str, object]" = {}
        for name in wanted:
            dir_name, func = FIELD_SOURCES[name]
            payload = payloads.get(dir_name)
            if payload is None:
                values[name] = None
                continue
            try:
                values[name] = func(payload, ctx)
            except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                self._logger.warning(
                    "autosync field %s の集計に失敗しました (date=%s): %s", name, d, exc,
                )
                errors.append(f"autosync {name}: {type(exc).__name__}: {exc}")
                values[name] = None
        return values, errors, set(payloads)


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

    実データ (2026-07-26) で、AutoSync が同期の遅れを取り戻す際に 1 ファイルへ
    8 日分の record をまとめて書いた例がある。旧集計関数 (睡眠以外) は日付で
    絞らないので、そのまま渡すと 8 日分が 1 日に計上される。``start`` が無い /
    数値でない record は日付を判定できないので残す (旧挙動を維持)。
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
