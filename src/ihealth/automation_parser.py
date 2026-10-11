"""Health Auto Export「オートメーション」JSON (ExportVersion.v2) を日次集計する。

入力は :func:`ihealth.automation.load_automation_json` の戻り値::

    {"data": {
        "metrics": [
            {"name": "step_count", "units": "count",
             "data": [{"date": "2026-10-10 05:02:00 +0900", "qty": 12, "source": "..."},
                      ...]},
            ...
        ],
        "workouts": [...]          # 任意 (オートメーションで含めた場合のみ)
    }}

主対象は ``aggregateData=true`` (record = 1 分集計) / ``aggregateSleep=true``
(睡眠は 1 日 1 レコードの集計値) の設定。非集計の睡眠 segment 形式
(``startDate`` / ``endDate`` + ``value`` = stage) にも対応する。

規約 (旧 AutoSync 用 :mod:`ihealth.parser` と同じ考え方):

* 全て tolerant: 欠損キー / 型違い (数値でない qty 等) の record は skip する
* record の日時 (``"2026-10-10 05:02:00 +0900"``) は **record 自身の offset の暦日**
  で ``target_date`` と比較する (JST 固定にしない。海外滞在中の記録も現地日付で
  1 日に入る)
* 該当 record が無いフィールドは ``None`` (= publisher は書かない / 既存値を保持)
* 単位は metric ごとに換算 (kJ → kcal, mi → km, lb → kg ...)。未知の単位は
  黙って誤換算せず、警告を出してそのフィールドを ``None`` にする
* 同名 metric が複数回出現したら優先単位 (kcal > kJ, kg > lb > g, km > m > mi)
  の 1 つだけ採用し、他は捨てる (加算すると二重計上になるため)
* 生理学的妥当性は :func:`ihealth.parser.drop_invalid_fields` でフィールド単位に
  検証し、違反フィールドだけ ``None`` に落とす
"""

from __future__ import annotations

import logging
import math
import statistics
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from datetime import date, datetime, timezone
from types import MappingProxyType
from typing import Callable, Iterable, Sequence, TypeVar

from ihealth.models import DailyHealthData
from ihealth.parser import (
    ParseContext,
    _rebuild_with_validation_drop,
    drop_invalid_fields,
)


_logger = logging.getLogger(__name__)

_T = TypeVar("_T")


# ---------- HAE の metric 名 (ExportVersion.v2 は snake_case) ----------

METRIC_STEP_COUNT = "step_count"
METRIC_DISTANCE = "walking_running_distance"
METRIC_ACTIVE_ENERGY = "active_energy"
METRIC_EXERCISE_TIME = "apple_exercise_time"
METRIC_HEART_RATE = "heart_rate"
METRIC_RESTING_HEART_RATE = "resting_heart_rate"
METRIC_OXYGEN_SATURATION = "blood_oxygen_saturation"
METRIC_SLEEP = "sleep_analysis"
METRIC_MINDFUL = "mindful_minutes"
#: ``body_mass_index`` (BMI) とは別物。名前の完全一致でしか拾わない。
METRIC_BODY_MASS = "weight_body_mass"
METRIC_BODY_FAT = "body_fat_percentage"

#: DailyHealthData の集計フィールド名 (date を除く 15 個、定義順)。
HEALTH_FIELDS: "tuple[str, ...]" = tuple(
    f.name for f in dataclass_fields(DailyHealthData) if f.name != "date"
)


# ---------- 単位換算 ----------

_DISTANCE_TO_KM: "Mapping[str, float]" = MappingProxyType({
    "km": 1.0, "m": 0.001, "mi": 1.609344,
})
#: ``Cal`` (大カロリー) は kcal と同じ。小文字の ``cal`` (小カロリー) と混同しないよう
#: 大文字小文字を区別した表から先に引く (:func:`_unit_factor`)。
_ENERGY_TO_KCAL: "Mapping[str, float]" = MappingProxyType({
    "kcal": 1.0, "Cal": 1.0, "kJ": 1.0 / 4.184,
})
_MASS_TO_KG: "Mapping[str, float]" = MappingProxyType({
    "kg": 1.0, "lb": 0.45359237, "lbs": 0.45359237, "g": 0.001,
})
#: 時間量 → 分。
_DURATION_TO_MIN: "Mapping[str, float]" = MappingProxyType({
    "min": 1.0, "hr": 60.0, "h": 60.0, "s": 1.0 / 60.0, "sec": 1.0 / 60.0,
})

#: 同名 metric 重複時の優先単位 (左ほど優先)。ここに無い metric は先勝ち。
_UNIT_PRIORITY: "Mapping[str, tuple[str, ...]]" = MappingProxyType({
    METRIC_ACTIVE_ENERGY: ("kcal", "Cal", "kJ"),
    METRIC_BODY_MASS: ("kg", "lb", "lbs", "g"),
    METRIC_DISTANCE: ("km", "m", "mi"),
})


# ---------- 睡眠 ----------

#: aggregateSleep=true の集計レコードだけが持つキー。
_AGGREGATED_SLEEP_KEYS = frozenset({
    "totalSleep", "sleepStart", "sleepEnd", "inBedStart", "inBedEnd",
})
#: 集計レコードの睡眠ステージ別時間 (totalSleep が無い/0 のときの合算対象)。
_SLEEP_STAGE_KEYS: "tuple[str, ...]" = ("asleep", "core", "deep", "rem")
#: 非集計 segment の asleep 系 stage (小文字部分一致)。
_ASLEEP_STAGE_WORDS: "tuple[str, ...]" = (
    "asleep", "core", "deep", "rem", "unspecified", "light",
)
#: 非集計 segment の除外 stage (小文字部分一致、asleep 判定より優先)。
_EXCLUDED_STAGE_WORDS: "tuple[str, ...]" = ("awake", "in bed", "inbed", "in_bed")
#: 同一ソースの segment を 1 つの睡眠セッションにまとめる最大間隔 (秒)。
#: 夜中の覚醒 (Awake segment は合算対象外なので asleep segment 間の隙間になる)
#: でセッションが割れ、0 時前に終わる前半部分が「前日の 20-24 時終了」として
#: どの日にも入らなくなるのを防ぐ。2 時間を超える覚醒は別セッション扱い。
_SLEEP_SESSION_GAP_SEC = 2 * 60 * 60
#: 夜主睡眠 / 昼寝の判定 (セッション終了時刻の現地 hour)。
_MAIN_SLEEP_END_HOURS = (0, 14)
_NAP_END_HOURS = (14, 20)

SLEEP_MODE_AGGREGATED = "aggregated"
SLEEP_MODE_SEGMENTS = "segments"


# ---------- ワークアウト名 (英 → 日) ----------

#: HAE のワークアウト名 (小文字・空白正規化済み) → 日本語名。未知の名前はそのまま使う。
#: 旧 Google Fit 時代の表記と意味が一致するもの (ハイキング / セーリング / その他 /
#: ジョギング) はその表記に合わせる。
WORKOUT_NAMES_JA: "Mapping[str, str]" = MappingProxyType({
    "walking": "ウォーキング",
    "outdoor walk": "ウォーキング",
    "indoor walk": "室内ウォーキング",
    "running": "ランニング",
    "outdoor run": "ランニング",
    "indoor run": "室内ランニング",
    "jogging": "ジョギング",
    "cycling": "サイクリング",
    "outdoor cycle": "サイクリング",
    "indoor cycle": "室内サイクリング",
    "hiking": "ハイキング",
    "sailing": "セーリング",
    "swimming": "水泳",
    "pool swim": "プールスイミング",
    "open water swim": "オープンウォータースイミング",
    "yoga": "ヨガ",
    "pilates": "ピラティス",
    "dance": "ダンス",
    "cardio dance": "ダンス",
    "social dance": "ダンス",
    "elliptical": "エリプティカル",
    "rowing": "ローイング",
    "stair stepper": "ステアステッパー",
    "stair climbing": "階段昇降",
    "stairs": "階段昇降",
    "traditional strength training": "筋力トレーニング",
    "functional strength training": "機能的筋力トレーニング",
    "core training": "コアトレーニング",
    "high intensity interval training": "HIIT",
    "hiit": "HIIT",
    "mixed cardio": "ミックスカーディオ",
    "cross training": "クロストレーニング",
    "flexibility": "柔軟運動",
    "cooldown": "クールダウン",
    "mind and body": "マインドとボディ",
    "tai chi": "太極拳",
    "martial arts": "格闘技",
    "boxing": "ボクシング",
    "kickboxing": "キックボクシング",
    "tennis": "テニス",
    "table tennis": "卓球",
    "badminton": "バドミントン",
    "golf": "ゴルフ",
    "soccer": "サッカー",
    "basketball": "バスケットボール",
    "baseball": "野球",
    "volleyball": "バレーボール",
    "climbing": "クライミング",
    "skating": "スケート",
    "snowboarding": "スノーボード",
    "downhill skiing": "スキー",
    "cross country skiing": "クロスカントリースキー",
    "surfing": "サーフィン",
    "surfing sports": "サーフィン",
    "paddle sports": "パドルスポーツ",
    "jump rope": "縄跳び",
    "fitness gaming": "フィットネスゲーム",
    "other": "その他",
})


@dataclass(frozen=True)
class AutomationParseResult:
    """:func:`parse_automation_day_detailed` の戻り値。

    Attributes:
        health: 集計結果 (妥当性違反フィールドは None に落とし済み)
        activity_detail: ``"瞑想3分、ウォーキング20分"`` / ``"なし"``。
            payload に metrics が 1 件も無い (or 対象日のデータが 1 つも無い)
            ときは ``None`` (= 書かない)
        has_metrics: payload に metric が 1 件以上あったか
        sleep_mode: ``"aggregated"`` / ``"segments"`` / ``None`` (睡眠データなし)
        errors: 集計中に例外になった metric の説明 (該当フィールドは None)
        dropped_fields: 妥当性検証で None に落としたフィールド名
    """

    health: DailyHealthData
    activity_detail: "str | None"
    has_metrics: bool
    sleep_mode: "str | None" = None
    errors: "tuple[str, ...]" = ()
    dropped_fields: "tuple[str, ...]" = ()


# ---------- 汎用ヘルパー ----------

_DATETIME_FORMATS: "tuple[str, ...]" = (
    "%Y-%m-%d %H:%M:%S %z",
    "%Y-%m-%d %H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S.%f %z",
    "%Y-%m-%d %H:%M %z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f%z",
)


def parse_hae_datetime(value: object) -> "datetime | None":
    """HAE の日時文字列 (``"2026-10-10 05:02:00 +0900"``) を datetime にする。

    offset 付きなら aware datetime (offset はそのまま保持、JST へ変換しない)。
    ISO 8601 (``2026-10-10T05:02:00+09:00``) も受け付ける。解釈できなければ None。
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _epoch(dt: datetime) -> float:
    """比較・間隔計算用の epoch 秒。naive は UTC とみなす (payload 内で一貫していれば十分)。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _num(value: object) -> "float | None":
    """有限の数値なら float、それ以外 (bool / 文字列 / NaN / inf / None) は None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _first_num(record: "Mapping[str, object]", keys: "Iterable[str]") -> "float | None":
    for key in keys:
        value = _num(record.get(key))
        if value is not None:
            return value
    return None


def _first_datetime(
    record: "Mapping[str, object]", keys: "Iterable[str]"
) -> "datetime | None":
    for key in keys:
        dt = parse_hae_datetime(record.get(key))
        if dt is not None:
            return dt
    return None


def _unit_factor(
    units: object, table: "Mapping[str, float]", default_unit: str
) -> "float | None":
    """``units`` を ``table`` の換算係数に引く。未指定は ``default_unit`` とみなす。

    大文字小文字を区別した完全一致を優先し、無ければ小文字比較で引く
    (ただし ``Cal`` は小文字比較の対象にしない: ``cal`` は小カロリーで別単位)。
    未知の単位は ``None``。
    """
    if units is None:
        return table[default_unit]
    if not isinstance(units, str):
        return None
    text = units.strip()
    if not text:
        return table[default_unit]
    if text in table:
        return table[text]
    lowered = {key.lower(): factor for key, factor in table.items() if key != "Cal"}
    return lowered.get(text.lower())


def _unit_rank(metric: "Mapping[str, object]", priority: "Sequence[str]") -> int:
    units = metric.get("units")
    if isinstance(units, str):
        text = units.strip()
        for index, candidate in enumerate(priority):
            if text == candidate or (candidate != "Cal" and text.lower() == candidate.lower()):
                return index
    return len(priority)


def _metrics_by_name(
    payload: "Mapping[str, object] | None", log: "logging.Logger | None"
) -> "dict[str, Mapping[str, object]]":
    """``data.metrics`` を name (小文字) → metric dict に索引化する。

    同名が複数回あれば :data:`_UNIT_PRIORITY` の優先単位の 1 つだけを残す
    (優先順位が無い metric は先勝ち)。**加算はしない** (二重計上防止)。
    ``log`` が None なら重複の警告を出さない (判定だけの補助呼び出し用)。
    """
    if not isinstance(payload, Mapping):
        return {}
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return {}
    metrics = data.get("metrics")
    if not isinstance(metrics, list):
        return {}

    selected: "dict[str, Mapping[str, object]]" = {}
    for metric in metrics:
        if not isinstance(metric, Mapping):
            continue
        raw_name = metric.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            continue
        name = raw_name.strip().lower()
        current = selected.get(name)
        if current is None:
            selected[name] = metric
            continue
        priority = _UNIT_PRIORITY.get(name)
        replace = (
            priority is not None
            and _unit_rank(metric, priority) < _unit_rank(current, priority)
        )
        if log is not None:
            log.warning(
                "metric %s が payload に複数回出現しました (units=%r / %r)。"
                "優先単位の 1 つだけ採用し、加算しません (採用: units=%r)",
                name, current.get("units"), metric.get("units"),
                (metric if replace else current).get("units"),
            )
        if replace:
            selected[name] = metric
    return selected


def _records(metric: "Mapping[str, object] | None") -> "list[Mapping[str, object]]":
    if metric is None:
        return []
    data = metric.get("data")
    if not isinstance(data, list):
        return []
    return [record for record in data if isinstance(record, Mapping)]


#: 一般 metric の record 日時キー (先に見つかったものを使う)。
_RECORD_DATE_KEYS: "tuple[str, ...]" = ("date", "startDate", "start")


def _records_on(
    metric: "Mapping[str, object] | None", target_date: date
) -> "list[tuple[datetime, Mapping[str, object]]]":
    """record 自身の offset の暦日が ``target_date`` の record を ``(datetime, record)`` で返す。

    日時が無い / 解釈できない record は日付を判定できないので除外する。
    """
    matched: "list[tuple[datetime, Mapping[str, object]]]" = []
    for record in _records(metric):
        dt = _first_datetime(record, _RECORD_DATE_KEYS)
        if dt is None or dt.date() != target_date:
            continue
        matched.append((dt, record))
    return matched


def _sum_qty(records: "Sequence[tuple[datetime, Mapping[str, object]]]") -> "float | None":
    values = [_num(record.get("qty")) for _dt, record in records]
    present = [value for value in values if value is not None]
    if not present:
        return None
    return float(sum(present))


def _guard(
    label: str,
    errors: "list[str]",
    log: logging.Logger,
    func: "Callable[[], _T]",
    default: "_T",
) -> "_T":
    """``func`` を実行し、データ異常系の例外は ``default`` に縮退して errors に記録する。

    :func:`ihealth.parser.parse_all` と同じく (KeyError / TypeError / ValueError /
    ArithmeticError) だけを握る。それ以外 (実装バグ) は呼び出し側へ伝播させる。
    """
    try:
        return func()
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        log.warning(
            "automation metric %s の集計に失敗しました: %s: %s",
            label, type(exc).__name__, exc,
        )
        errors.append(f"{label}: {type(exc).__name__}: {exc}")
        return default


def _warn_unit(log: logging.Logger, metric_name: str, units: object) -> None:
    log.warning(
        "metric %s の単位 %r は未対応のため集計しません (誤換算防止)",
        metric_name, units,
    )


# ---------- metric 別の集計 ----------


def _step_count(metric: "Mapping[str, object] | None", target_date: date) -> "int | None":
    total = _sum_qty(_records_on(metric, target_date))
    if total is None:
        return None
    return int(round(total))


def _converted_sum(
    metric: "Mapping[str, object] | None",
    target_date: date,
    table: "Mapping[str, float]",
    default_unit: str,
    metric_name: str,
    log: logging.Logger,
) -> "float | None":
    records = _records_on(metric, target_date)
    if not records or metric is None:
        return None
    factor = _unit_factor(metric.get("units"), table, default_unit)
    if factor is None:
        _warn_unit(log, metric_name, metric.get("units"))
        return None
    total = _sum_qty(records)
    if total is None:
        return None
    return total * factor


def _heart_rate(
    metric: "Mapping[str, object] | None", target_date: date, max_heart_rate: float
) -> "tuple[float | None, float | None, float | None]":
    """``(heart_rate_avg, heart_rate_max, exercise_intensity_score)`` を返す。

    record は 1 分集計の ``Avg`` / ``Min`` / ``Max`` (小文字・単一 ``qty`` も許容)。

    * ``heart_rate_avg`` = mean(Avg)
    * ``heart_rate_max`` = max(Max) (Max が無い record は qty / Avg で代用)
    * ``exercise_intensity_score`` = record を「分」(epoch 秒 // 60) で bucket し、
      bucket 内 Avg 平均が ``>= 0.70 * max_hr`` → 2pt、``>= 0.50 * max_hr`` → 1pt
      の総和 (Google Fit Heart Points 近似、:func:`ihealth.parser.parse_exercise_intensity_score`
      と同じ閾値)。心拍 record が 1 件以上あれば 0pt でも 0.0、無ければ None
    """
    avgs: "list[float]" = []
    peaks: "list[float]" = []
    buckets: "dict[int, tuple[float, int]]" = {}
    for dt, record in _records_on(metric, target_date):
        avg = _first_num(record, ("Avg", "avg", "qty"))
        peak = _first_num(record, ("Max", "max", "qty", "Avg", "avg"))
        if avg is not None:
            avgs.append(avg)
            bucket = int(_epoch(dt) // 60)
            prev_sum, prev_count = buckets.get(bucket, (0.0, 0))
            buckets[bucket] = (prev_sum + avg, prev_count + 1)
        if peak is not None:
            peaks.append(peak)

    heart_rate_avg = float(statistics.mean(avgs)) if avgs else None
    heart_rate_max = float(max(peaks)) if peaks else None
    if not buckets:
        return heart_rate_avg, heart_rate_max, None

    moderate = max_heart_rate * 0.50
    vigorous = max_heart_rate * 0.70
    points = 0
    for avg_sum, count in buckets.values():
        mean_avg = avg_sum / count
        if mean_avg >= vigorous:
            points += 2
        elif mean_avg >= moderate:
            points += 1
    return heart_rate_avg, heart_rate_max, float(points)


def _mean_qty(metric: "Mapping[str, object] | None", target_date: date) -> "float | None":
    values = [_num(record.get("qty")) for _dt, record in _records_on(metric, target_date)]
    present = [value for value in values if value is not None]
    if not present:
        return None
    return float(statistics.mean(present))


def _oxygen_saturation(
    metric: "Mapping[str, object] | None", target_date: date
) -> "float | None":
    values = [
        _first_num(record, ("qty", "Avg", "avg"))
        for _dt, record in _records_on(metric, target_date)
    ]
    present = [value for value in values if value is not None]
    if not present:
        return None
    if all(value <= 1.0 for value in present):
        # 0.97 のような fraction 表記で来た場合は % に直す
        present = [value * 100.0 for value in present]
    return float(statistics.mean(present))


def _latest_qty(
    metric: "Mapping[str, object] | None", target_date: date
) -> "float | None":
    latest: "tuple[float, float] | None" = None
    for dt, record in _records_on(metric, target_date):
        value = _num(record.get("qty"))
        if value is None:
            continue
        key = _epoch(dt)
        if latest is None or key >= latest[0]:
            latest = (key, value)
    return None if latest is None else latest[1]


def _body_mass_kg(
    metric: "Mapping[str, object] | None", target_date: date, log: logging.Logger
) -> "float | None":
    value = _latest_qty(metric, target_date)
    if value is None or metric is None:
        return None
    factor = _unit_factor(metric.get("units"), _MASS_TO_KG, "kg")
    if factor is None:
        _warn_unit(log, METRIC_BODY_MASS, metric.get("units"))
        return None
    return value * factor


def _body_fat_percentage(
    metric: "Mapping[str, object] | None", target_date: date
) -> "float | None":
    value = _latest_qty(metric, target_date)
    if value is None:
        return None
    return value * 100.0 if value <= 1.0 else value


def _source_count(record: "Mapping[str, object]") -> int:
    """record の ``source`` に含まれるソース数 (``"Muse | Apple Watch"`` → 2、無ければ 1)。

    aggregateData=true の分単位集計では、**同じ分に始まった** 複数ソースの
    セッションが 1 record にまとめられ、``source`` が ``|`` 区切りになる
    (実データ: Muse の瞑想と Apple Watch のマインドフルネスを同時に記録した日)。
    qty は合算されているので、回数だけソース数で数え直す。
    """
    source = record.get("source")
    if not isinstance(source, str):
        return 1
    names = {part.replace("\u00a0", " ").strip() for part in source.split("|")}
    names.discard("")
    return max(1, len(names))


def _mindful(
    metric: "Mapping[str, object] | None", target_date: date, log: logging.Logger
) -> "tuple[int | None, float | None]":
    """``(mindful_sessions, mindful_minutes)``。

    * sessions = record 数。ただし分単位集計で複数ソースがまとめられた record
      (``source`` が ``|`` 区切り) はソース数で数える (:func:`_source_count`)
    * minutes = SUM(qty)。qty が無い record は ``start``/``end``
      (``startDate``/``endDate``) の差で補う
    """
    records = _records_on(metric, target_date)
    if not records or metric is None:
        return None, None
    factor = _unit_factor(metric.get("units"), _DURATION_TO_MIN, "min")
    if factor is None:
        _warn_unit(log, METRIC_MINDFUL, metric.get("units"))
    sessions = sum(_source_count(record) for _dt, record in records)
    total = 0.0
    measured = False
    for _dt, record in records:
        qty = _num(record.get("qty"))
        if qty is not None and factor is not None:
            total += qty * factor
            measured = True
            continue
        start = _first_datetime(record, ("start", "startDate"))
        end = _first_datetime(record, ("end", "endDate"))
        if start is not None and end is not None:
            seconds = _epoch(end) - _epoch(start)
            if seconds >= 0:
                total += seconds / 60.0
                measured = True
    minutes = total if measured and total > 0 else None
    return sessions, minutes


def _is_aggregated_sleep(record: "Mapping[str, object]") -> bool:
    if any(key in record for key in _AGGREGATED_SLEEP_KEYS):
        return True
    return "value" not in record and any(key in record for key in _SLEEP_STAGE_KEYS)


def _is_sleep_segment(record: "Mapping[str, object]") -> bool:
    return isinstance(record.get("value"), str) and not _is_aggregated_sleep(record)


def _is_asleep_stage(stage: str) -> bool:
    lowered = stage.strip().lower()
    if any(word in lowered for word in _EXCLUDED_STAGE_WORDS):
        return False
    return any(word in lowered for word in _ASLEEP_STAGE_WORDS)


def _sleep_hours_factor(metric: "Mapping[str, object] | None") -> "float | None":
    """睡眠 metric の単位 → 時間への係数 (既定 hr)。"""
    units = metric.get("units") if metric is not None else None
    per_min = _unit_factor(units, _DURATION_TO_MIN, "hr")
    return None if per_min is None else per_min / 60.0


def _aggregated_sleep_hours(
    records: "Sequence[Mapping[str, object]]",
    target_date: date,
    hours_factor: float,
) -> "float | None":
    """aggregateSleep=true の集計レコードから ``target_date`` の総睡眠 (時間)。

    record の ``date`` の暦日 (無ければ ``sleepEnd`` の暦日) が target_date のもの。
    値は ``totalSleep`` (無い / 0 なら asleep+core+deep+rem)。複数ソース・重複が
    あっても最大の 1 つを採用する (加算すると二重計上)。0 以下は「測定なし」扱い。
    """
    best: "float | None" = None
    for record in records:
        if not _is_aggregated_sleep(record):
            continue
        anchor = parse_hae_datetime(record.get("date"))
        if anchor is None:
            anchor = parse_hae_datetime(record.get("sleepEnd"))
        if anchor is None or anchor.date() != target_date:
            continue
        total = _num(record.get("totalSleep"))
        if total is None or total <= 0:
            parts = [_num(record.get(key)) for key in _SLEEP_STAGE_KEYS]
            present = [part for part in parts if part is not None]
            total = float(sum(present)) if present else None
        if total is None or total <= 0:
            continue
        hours = total * hours_factor
        best = hours if best is None else max(best, hours)
    return best


def _segment_sleep_hours(
    records: "Sequence[Mapping[str, object]]",
    target_date: date,
    hours_factor: float,
) -> "tuple[float | None, float | None]":
    """非集計 segment から ``(sleep_hours, nap_hours)`` を求める。

    1. asleep 系 stage (Asleep / Core / Deep / REM / Unspecified 等) の segment だけを
       対象にし、(source, start, end, stage) が同じ重複 segment は 1 回だけ数える
       (当日 + 前日 payload の結合で同じ segment が 2 回来ても二重計上しない)
    2. source ごとに時刻順に並べ、間隔 :data:`_SLEEP_SESSION_GAP_SEC` 以内の segment を
       1 セッションにまとめる (セッション時間 = segment の duration 合計、
       duration は qty (時間) が無ければ end - start)
    3. セッション終了 (end) の **現地暦日** が target_date で、end.hour が
       [0,14) → 夜主睡眠、[14,20) → 昼寝。source ごとに合計し、最大の source を採用
       (Apple Watch と AutoSleep 等の重複投入を二重計上しない)
    """
    seen: "set[tuple[str, float | None, float, str]]" = set()
    by_source: "dict[str, list[tuple[float, datetime, float]]]" = {}
    for record in records:
        if not _is_sleep_segment(record):
            continue
        stage = str(record.get("value"))
        if not _is_asleep_stage(stage):
            continue
        start = _first_datetime(record, ("startDate", "start"))
        end = _first_datetime(record, ("endDate", "end"))
        if end is None:
            continue
        source_raw = record.get("source")
        source = source_raw if isinstance(source_raw, str) else ""
        start_epoch = _epoch(start) if start is not None else None
        end_epoch = _epoch(end)
        key = (source, start_epoch, end_epoch, stage.strip().lower())
        if key in seen:
            continue
        seen.add(key)
        qty = _num(record.get("qty"))
        if qty is not None and qty >= 0:
            hours = qty * hours_factor
        elif start_epoch is not None and end_epoch >= start_epoch:
            hours = (end_epoch - start_epoch) / 3600.0
        else:
            continue
        begin_epoch = start_epoch if start_epoch is not None else end_epoch
        by_source.setdefault(source, []).append((begin_epoch, end, hours))

    sleep_by_source: "dict[str, float]" = {}
    nap_by_source: "dict[str, float]" = {}
    for source, segments in by_source.items():
        segments.sort(key=lambda item: (item[0], _epoch(item[1])))
        # session = [begin_epoch, end_datetime, hours]
        sessions: "list[list]" = []
        for begin_epoch, end, hours in segments:
            if sessions and begin_epoch - _epoch(sessions[-1][1]) <= _SLEEP_SESSION_GAP_SEC:
                if _epoch(end) > _epoch(sessions[-1][1]):
                    sessions[-1][1] = end
                sessions[-1][2] += hours
            else:
                sessions.append([begin_epoch, end, hours])
        for _begin, end, hours in sessions:
            if end.date() != target_date:
                continue
            if _MAIN_SLEEP_END_HOURS[0] <= end.hour < _MAIN_SLEEP_END_HOURS[1]:
                sleep_by_source[source] = sleep_by_source.get(source, 0.0) + hours
            elif _NAP_END_HOURS[0] <= end.hour < _NAP_END_HOURS[1]:
                nap_by_source[source] = nap_by_source.get(source, 0.0) + hours

    sleep_hours = max(sleep_by_source.values()) if sleep_by_source else None
    nap_hours = max(nap_by_source.values()) if nap_by_source else None
    return sleep_hours, nap_hours


def _sleep(
    metric: "Mapping[str, object] | None",
    prev_metric: "Mapping[str, object] | None",
    target_date: date,
    log: logging.Logger,
) -> "tuple[float | None, float | None, str | None]":
    """``(sleep_hours, nap_hours, sleep_mode)``。

    * aggregated (aggregateSleep=true): sleep_hours = その日の totalSleep
      (= 昼寝も含む総睡眠)。昼寝は分離できないので nap_hours = None
      (HealthLoader が AutoSync .hae から補完する)
    * segments: 当日 + 前日 payload の segment を結合して夜主睡眠 / 昼寝に分ける
    """
    records = _records(metric) + _records(prev_metric)
    if not records:
        return None, None, None
    hours_factor = _sleep_hours_factor(metric if metric is not None else prev_metric)
    if hours_factor is None:
        source_metric = metric if metric is not None else prev_metric
        _warn_unit(log, METRIC_SLEEP, source_metric.get("units") if source_metric else None)
        return None, None, None

    has_aggregated = any(_is_aggregated_sleep(record) for record in records)
    has_segments = any(_is_sleep_segment(record) for record in records)
    if has_aggregated:
        total = _aggregated_sleep_hours(records, target_date, hours_factor)
        if total is not None or not has_segments:
            return total, None, SLEEP_MODE_AGGREGATED
    if has_segments:
        sleep_hours, nap_hours = _segment_sleep_hours(records, target_date, hours_factor)
        return sleep_hours, nap_hours, SLEEP_MODE_SEGMENTS
    return None, None, None


# ---------- ワークアウト / アクティビティ詳細 ----------


def workout_name_ja(name: object) -> str:
    """HAE のワークアウト名を日本語名にする (未知はそのまま、空は ``その他``)。"""
    if not isinstance(name, str) or not name.strip():
        return "その他"
    normalized = " ".join(name.strip().lower().split())
    return WORKOUT_NAMES_JA.get(normalized, name.strip())


def extract_workouts(
    payload: "Mapping[str, object] | None", target_date: date
) -> "list[tuple[str, float]]":
    """``data.workouts`` から target_date の ``(日本語名, 分)`` を開始時刻順に返す。

    同じ日本語名は合算する (旧 Google Fit 時代の「種目ごと 1 回」表記に合わせる)。
    duration は ``duration`` (秒) を優先し、無ければ end - start。開始日時の現地暦日
    (開始が無ければ終了) が target_date のものだけを対象にする。
    """
    if not isinstance(payload, Mapping):
        return []
    data = payload.get("data")
    workouts = data.get("workouts") if isinstance(data, Mapping) else None
    if not isinstance(workouts, list):
        return []

    items: "list[tuple[float, int, str, float]]" = []
    for index, workout in enumerate(workouts):
        if not isinstance(workout, Mapping):
            continue
        start = _first_datetime(workout, ("start", "startDate"))
        end = _first_datetime(workout, ("end", "endDate"))
        anchor = start if start is not None else end
        if anchor is not None and anchor.date() != target_date:
            continue
        duration = _num(workout.get("duration"))
        if duration is not None and duration >= 0:
            minutes = duration / 60.0
        elif start is not None and end is not None and _epoch(end) >= _epoch(start):
            minutes = (_epoch(end) - _epoch(start)) / 60.0
        else:
            continue
        order = _epoch(anchor) if anchor is not None else math.inf
        items.append((order, index, workout_name_ja(workout.get("name")), minutes))

    items.sort(key=lambda item: (item[0], item[1]))
    totals: "dict[str, float]" = {}
    for _order, _index, name, minutes in items:
        totals[name] = totals.get(name, 0.0) + minutes
    return list(totals.items())


def build_activity_detail(
    mindful_minutes: "float | None",
    workouts: "Sequence[tuple[str, float]]" = (),
) -> str:
    """``"瞑想3分、ウォーキング20分"`` 形式のアクティビティ詳細。何も無ければ ``"なし"``。

    瞑想は合計分 (round) が 1 以上のときだけ、ワークアウトも round が 1 以上のもの
    だけを ``"、"`` で連結する。
    """
    parts: "list[str]" = []
    if mindful_minutes is not None:
        minutes = int(round(mindful_minutes))
        if minutes >= 1:
            parts.append(f"瞑想{minutes}分")
    for name, workout_minutes in workouts:
        minutes = int(round(workout_minutes))
        if minutes >= 1:
            parts.append(f"{name}{minutes}分")
    return "、".join(parts) if parts else "なし"


# ---------- エントリポイント ----------


def needs_prev_payload(payload: "Mapping[str, object] | None") -> bool:
    """前日 payload の併読が必要か (= 睡眠が非集計 segment 形式か)。

    aggregateSleep=true の集計レコードは「その日のファイル」に完結しているので
    前日ファイルは不要 (iCloud dataless の前日ファイルを無駄に materialize しない)。
    segment 形式は前夜の segment が前日ファイルに入るため併読が必要。
    """
    metric = _metrics_by_name(payload, None).get(METRIC_SLEEP)
    return any(_is_sleep_segment(record) for record in _records(metric))


def has_metrics(payload: "Mapping[str, object] | None") -> bool:
    """payload に名前付き metric が 1 件以上あるか。"""
    return bool(_metrics_by_name(payload, None))


def parse_automation_day_detailed(
    payload: "Mapping[str, object]",
    target_date: date,
    ctx: ParseContext,
    *,
    prev_payload: "Mapping[str, object] | None" = None,
    logger: "logging.Logger | None" = None,
) -> AutomationParseResult:
    """オートメーション JSON 1 日分を集計して :class:`AutomationParseResult` を返す。

    ``prev_payload`` (前日ファイル) は非集計睡眠 segment の結合にだけ使う。
    """
    log = logger or _logger
    metrics = _metrics_by_name(payload, log)
    prev_metrics = _metrics_by_name(prev_payload, log) if prev_payload is not None else {}
    errors: "list[str]" = []
    values: "dict[str, object]" = {name: None for name in HEALTH_FIELDS}

    def metric(name: str) -> "Mapping[str, object] | None":
        return metrics.get(name)

    values["step_count"] = _guard(
        METRIC_STEP_COUNT, errors, log,
        lambda: _step_count(metric(METRIC_STEP_COUNT), target_date), None,
    )
    values["distance_km"] = _guard(
        METRIC_DISTANCE, errors, log,
        lambda: _converted_sum(
            metric(METRIC_DISTANCE), target_date, _DISTANCE_TO_KM, "km",
            METRIC_DISTANCE, log,
        ),
        None,
    )
    values["active_energy_kcal"] = _guard(
        METRIC_ACTIVE_ENERGY, errors, log,
        lambda: _converted_sum(
            metric(METRIC_ACTIVE_ENERGY), target_date, _ENERGY_TO_KCAL, "kcal",
            METRIC_ACTIVE_ENERGY, log,
        ),
        None,
    )
    values["exercise_minutes"] = _guard(
        METRIC_EXERCISE_TIME, errors, log,
        lambda: _converted_sum(
            metric(METRIC_EXERCISE_TIME), target_date, _DURATION_TO_MIN, "min",
            METRIC_EXERCISE_TIME, log,
        ),
        None,
    )
    hr_avg, hr_max, intensity = _guard(
        METRIC_HEART_RATE, errors, log,
        lambda: _heart_rate(metric(METRIC_HEART_RATE), target_date, ctx.max_heart_rate),
        (None, None, None),
    )
    values["exercise_intensity_score"] = intensity
    values["heart_rate_avg"] = hr_avg
    values["heart_rate_max"] = hr_max
    values["heart_rate_resting"] = _guard(
        METRIC_RESTING_HEART_RATE, errors, log,
        lambda: _mean_qty(metric(METRIC_RESTING_HEART_RATE), target_date), None,
    )
    values["oxygen_saturation"] = _guard(
        METRIC_OXYGEN_SATURATION, errors, log,
        lambda: _oxygen_saturation(metric(METRIC_OXYGEN_SATURATION), target_date), None,
    )
    sleep_hours, nap_hours, sleep_mode = _guard(
        METRIC_SLEEP, errors, log,
        lambda: _sleep(
            metric(METRIC_SLEEP), prev_metrics.get(METRIC_SLEEP), target_date, log,
        ),
        (None, None, None),
    )
    values["sleep_hours"] = sleep_hours
    values["nap_hours"] = nap_hours
    mindful_sessions, mindful_minutes = _guard(
        METRIC_MINDFUL, errors, log,
        lambda: _mindful(metric(METRIC_MINDFUL), target_date, log), (None, None),
    )
    values["mindful_sessions"] = mindful_sessions
    values["mindful_minutes"] = mindful_minutes
    values["body_mass_kg"] = _guard(
        METRIC_BODY_MASS, errors, log,
        lambda: _body_mass_kg(metric(METRIC_BODY_MASS), target_date, log), None,
    )
    values["body_fat_percentage"] = _guard(
        METRIC_BODY_FAT, errors, log,
        lambda: _body_fat_percentage(metric(METRIC_BODY_FAT), target_date), None,
    )
    workouts = _guard(
        "workouts", errors, log, lambda: extract_workouts(payload, target_date), [],
    )

    cleaned = drop_invalid_fields(values, logger=log)
    dropped = tuple(
        name for name in HEALTH_FIELDS
        if values.get(name) is not None and cleaned.get(name) is None
    )
    # cleaned は検証済みだが、models に将来追加された未知の制約にも縮退できるよう
    # parser と同じ構築経路を通す (違反時は警告 + date のみの DailyHealthData)。
    health = _rebuild_with_validation_drop(target_date, cleaned)

    present = bool(metrics)
    if not present:
        activity_detail: "str | None" = None
    elif all(cleaned.get(name) is None for name in HEALTH_FIELDS) and not workouts:
        # metric はあるが対象日のデータが 1 つも無い → 「活動なし」とは言えない
        activity_detail = None
    else:
        activity_detail = build_activity_detail(health.mindful_minutes, workouts)

    return AutomationParseResult(
        health=health,
        activity_detail=activity_detail,
        has_metrics=present,
        sleep_mode=sleep_mode,
        errors=tuple(errors),
        dropped_fields=dropped,
    )


def parse_automation_day(
    payload: "Mapping[str, object]",
    target_date: date,
    ctx: ParseContext,
    *,
    prev_payload: "Mapping[str, object] | None" = None,
    logger: "logging.Logger | None" = None,
) -> "tuple[DailyHealthData, str | None]":
    """オートメーション JSON 1 日分を ``(DailyHealthData, activity_detail)`` に集計する。

    ``activity_detail`` は payload に metric が 1 件も無ければ ``None``。
    詳細 (エラー / 睡眠モード / drop されたフィールド) が必要なら
    :func:`parse_automation_day_detailed` を使う。
    """
    result = parse_automation_day_detailed(
        payload, target_date, ctx, prev_payload=prev_payload, logger=logger,
    )
    return result.health, result.activity_detail
