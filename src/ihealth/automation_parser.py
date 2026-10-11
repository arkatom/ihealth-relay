"""Health Auto Export「オートメーション」JSON (ExportVersion.v2) を日次集計する。

入力は :func:`ihealth.automation.load_automation_json` の戻り値::

    {"data": {
        "metrics": [
            {"name": "step_count", "units": "count",
             "data": [{"date": "2030-01-15 05:02:00 +0900", "qty": 12, "source": "..."},
                      ...]},
            ...
        ],
        "workouts": [...]          # 任意 (オートメーションで含めた場合のみ)
    }}

``data.workouts`` が **無い** 出力 (HAE のオートメーションでワークアウトを含めない設定 =
``includeWorkouts=false``) では、その日にワークアウトがあったかを判断できない。
そのためアクティビティ詳細は ``None`` (= 書かない) にする (``"なし"`` / ``"瞑想N分"`` と
断定すると、実際にあったワークアウトを含む既存の値を上書きしてしまう)。

主対象は ``aggregateData=true`` (record = 1 分集計) / ``aggregateSleep=true``
(睡眠は 1 日 1 レコードの集計値) の設定。非集計の睡眠 segment 形式
(``startDate`` / ``endDate`` + ``value`` = stage) にも対応する。

規約 (旧 AutoSync 用 :mod:`ihealth.parser` と同じ考え方):

* 個々の record の欠損キー / 型違い (数値でない qty 等) は skip する (tolerant)
* ただし **metric 全体が解釈できない** (data が配列でない / どの record の日時も
  解釈できない / 対象日の record に使える数値が 1 つも無い / 未知の単位) 場合は
  黙って ``None`` にせず ``errors`` に記録する。HAE の更新やロケール変更で書式が
  変わったときに「データなし」と区別できず、通知されないまま値が途絶えるのを防ぐ
* record の日時 (``"2030-01-15 05:02:00 +0900"``) は **record 自身の offset の暦日**
  で ``target_date`` と比較する (JST 固定にしない。海外滞在中の記録も現地日付で
  1 日に入る)
* 該当 record が無いフィールドは ``None`` (= publisher は書かない / 既存値を保持)
* 単位は metric ごとに換算 (kJ → kcal, mi → km, lb → kg ...)。未知の単位は
  黙って誤換算せず、そのフィールドを ``None`` にしてエラーとして記録する
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
from datetime import date, datetime, time, timedelta, timezone
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
#: ``errors`` / ``failed_metrics`` でワークアウトの集計失敗を表すラベル。
WORKOUTS_LABEL = "workouts"

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
#: 同一ソースの segment を 1 つの睡眠セッションとみなす最大間隔 (秒)。Awake /
#: In Bed segment もセッションの一部として間隔を埋める。セッションは
#: 「0 時前に終わる segment (20-24 時台終了) を翌朝の夜主睡眠へ繰り越すか」と
#: 「当日の夜主睡眠が前日ファイルの segment に依存しうるか」の判定にだけ使い、
#: 夜主睡眠 / 昼寝の分類自体は **segment ごとの終了時刻** で行う。
SLEEP_SESSION_GAP_SEC = 2 * 60 * 60
#: 夜主睡眠 / 昼寝の判定 (segment 終了時刻の現地 hour)。
_MAIN_SLEEP_END_HOURS = (0, 14)
_NAP_END_HOURS = (14, 20)
#: HAE の aggregateSleep 集計における「1 日」の締めの時刻 (現地 hour)。実データの
#: 観測: sleepEnd が 18 時以降のセッションは **翌日付** のレコードになり、翌日の
#: totalSleep に含まれる (18 時前に終わる昼寝は当日の totalSleep に含まれる)。
HAE_AGGREGATED_SLEEP_DAY_END_HOUR = 18

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
            payload に metrics が 1 件も無い / 対象日のデータが 1 つも無い /
            ``data.workouts`` が含まれていない (ワークアウトを出力しない設定) /
            瞑想かワークアウトを解釈できなかったときは ``None`` (= 書かない)
        has_metrics: payload に metric が 1 件以上あったか
        sleep_mode: ``"aggregated"`` / ``"segments"`` / ``None`` (睡眠データなし)
        errors: 集計できなかった metric の説明 (該当フィールドは None)
        dropped_fields: 妥当性検証で None に落としたフィールド名
        failed_metrics: 集計に失敗した metric 名 (``errors`` と対応。ワークアウトは
            :data:`WORKOUTS_LABEL`)
        workouts: 対象日のワークアウト ``(日本語名, 分)``。``data.workouts`` が無い
            (出力しない設定) / 解釈できなかったら ``None``
        workouts_included: payload に ``data.workouts`` があったか (False なら
            ワークアウトの有無を判断できないので activity_detail を書かない)
        main_sleep_end: segment 形式で、対象日の夜主睡眠に数えた segment の最遅終了時刻
            (record 自身の offset)。夜主睡眠が無い / aggregated なら ``None``
        main_sleep_needs_prev: segment 形式で、対象日の夜主睡眠のセッションが
            「0 時 + :data:`SLEEP_SESSION_GAP_SEC`」より前に始まっている
            (= 前日ファイルにしか無い 0 時前の segment が繋がりうる)。呼び出し側は
            前日ファイルを読めていない / 夜が明ける前に書かれた古い前日ファイル
            しか無いとき、夜主睡眠を「過少の恐れあり」として書かないこと
    """

    health: DailyHealthData
    activity_detail: "str | None"
    has_metrics: bool
    sleep_mode: "str | None" = None
    errors: "tuple[str, ...]" = ()
    dropped_fields: "tuple[str, ...]" = ()
    failed_metrics: "tuple[str, ...]" = ()
    workouts: "tuple[tuple[str, float], ...] | None" = ()
    main_sleep_end: "datetime | None" = None
    main_sleep_needs_prev: bool = False
    workouts_included: bool = False


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
    """HAE の日時文字列 (``"2030-01-15 05:02:00 +0900"``) を datetime にする。

    offset 付きなら aware datetime (offset はそのまま保持、JST へ変換しない)。
    ISO 8601 (``2030-01-15T05:02:00+09:00``) も受け付ける。解釈できなければ None。
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


def _unit_error(units: object) -> ValueError:
    """未知の単位: 誤換算せずに metric ごと集計失敗として記録させる例外。"""
    return ValueError(f"単位 {units!r} は未対応のため集計しません (誤換算防止)")


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
    """metric の record (dict のみ)。判定用の tolerant 版 (data が配列でなければ空)。"""
    if metric is None:
        return []
    data = metric.get("data")
    if not isinstance(data, list):
        return []
    return [record for record in data if isinstance(record, Mapping)]


def _strict_records(metric: "Mapping[str, object] | None") -> "list[Mapping[str, object]]":
    """集計用の record 取得。``data`` があるのに配列でなければ解釈不能として ValueError。"""
    if metric is None:
        return []
    data = metric.get("data")
    if data is None:
        return []
    if not isinstance(data, list):
        raise ValueError(f"data が配列ではありません ({type(data).__name__})")
    return [record for record in data if isinstance(record, Mapping)]


#: 一般 metric の record 日時キー (先に見つかったものを使う)。
_RECORD_DATE_KEYS: "tuple[str, ...]" = ("date", "startDate", "start")
#: :func:`has_data_for` が「対象日の record か」を見るときの日時キー (どれか 1 つでも一致)。
_ANY_DATE_KEYS: "tuple[str, ...]" = (
    "date", "startDate", "start", "endDate", "end", "sleepStart", "sleepEnd",
)


def _records_on(
    metric: "Mapping[str, object] | None", target_date: date
) -> "list[tuple[datetime, Mapping[str, object]]]":
    """record 自身の offset の暦日が ``target_date`` の record を ``(datetime, record)`` で返す。

    日時が無い / 解釈できない個々の record は日付を判定できないので除外する。
    ただし record があるのに **1 件も** 日時を解釈できない場合は書式変更とみなして
    ValueError (黙って「対象日のデータなし」にしない)。
    """
    records = _strict_records(metric)
    matched: "list[tuple[datetime, Mapping[str, object]]]" = []
    dated = 0
    for record in records:
        dt = _first_datetime(record, _RECORD_DATE_KEYS)
        if dt is None:
            continue
        dated += 1
        if dt.date() == target_date:
            matched.append((dt, record))
    if records and dated == 0:
        raise ValueError(f"record {len(records)} 件の日時をどれも解釈できません")
    return matched


def _require_values(
    records: "Sequence[object]", values: "Sequence[object]", what: str
) -> None:
    """対象日の record があるのに使える値が 1 つも無ければ書式変更とみなして ValueError。"""
    if records and not values:
        raise ValueError(f"対象日の record {len(records)} 件に数値の {what} がありません")


def _sum_qty(records: "Sequence[tuple[datetime, Mapping[str, object]]]") -> "float | None":
    values = [_num(record.get("qty")) for _dt, record in records]
    present = [value for value in values if value is not None]
    _require_values(records, present, "qty")
    if not present:
        return None
    return float(sum(present))


def _guard(
    label: str,
    errors: "list[str]",
    failed: "list[str]",
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
        failed.append(label)
        return default


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
) -> "float | None":
    records = _records_on(metric, target_date)
    if not records or metric is None:
        return None
    factor = _unit_factor(metric.get("units"), table, default_unit)
    if factor is None:
        raise _unit_error(metric.get("units"))
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
    records = _records_on(metric, target_date)
    avgs: "list[float]" = []
    peaks: "list[float]" = []
    buckets: "dict[int, tuple[float, int]]" = {}
    for dt, record in records:
        avg = _first_num(record, ("Avg", "avg", "qty"))
        peak = _first_num(record, ("Max", "max", "qty", "Avg", "avg"))
        if avg is not None:
            avgs.append(avg)
            bucket = int(_epoch(dt) // 60)
            prev_sum, prev_count = buckets.get(bucket, (0.0, 0))
            buckets[bucket] = (prev_sum + avg, prev_count + 1)
        if peak is not None:
            peaks.append(peak)
    _require_values(records, avgs or peaks, "Avg / Max / qty")

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
    records = _records_on(metric, target_date)
    present = [
        value for value in (_num(record.get("qty")) for _dt, record in records)
        if value is not None
    ]
    _require_values(records, present, "qty")
    if not present:
        return None
    return float(statistics.mean(present))


def _oxygen_saturation(
    metric: "Mapping[str, object] | None", target_date: date
) -> "float | None":
    """酸素飽和度の平均 (%)。

    ``0.97`` のような fraction 表記の値は **値ごとに** % へ直す (SpO2 が 1% 以下は
    生理的にありえないので、1.0 以下は fraction と断定できる)。fraction と % が
    混在していても平均が壊れない。
    """
    records = _records_on(metric, target_date)
    present = [
        value for value in (
            _first_num(record, ("qty", "Avg", "avg")) for _dt, record in records
        )
        if value is not None
    ]
    _require_values(records, present, "qty / Avg")
    if not present:
        return None
    return float(statistics.mean(
        value * 100.0 if value <= 1.0 else value for value in present
    ))


def _latest_qty(
    metric: "Mapping[str, object] | None", target_date: date
) -> "float | None":
    records = _records_on(metric, target_date)
    latest: "tuple[float, float] | None" = None
    for dt, record in records:
        value = _num(record.get("qty"))
        if value is None:
            continue
        key = _epoch(dt)
        if latest is None or key >= latest[0]:
            latest = (key, value)
    _require_values(records, [] if latest is None else [latest[1]], "qty")
    return None if latest is None else latest[1]


def _body_mass_kg(
    metric: "Mapping[str, object] | None", target_date: date
) -> "float | None":
    value = _latest_qty(metric, target_date)
    if value is None or metric is None:
        return None
    factor = _unit_factor(metric.get("units"), _MASS_TO_KG, "kg")
    if factor is None:
        raise _unit_error(metric.get("units"))
    return value * factor


def _body_fat_percentage(
    metric: "Mapping[str, object] | None", target_date: date
) -> "float | None":
    value = _latest_qty(metric, target_date)
    if value is None:
        return None
    return value * 100.0 if value <= 1.0 else value


def _source_count(record: "Mapping[str, object]") -> int:
    """record の ``source`` に含まれるソース数 (``"App A | Watch B"`` → 2、無ければ 1)。

    aggregateData=true の分単位集計では、**同じ分に始まった** 複数ソースの
    セッションが 1 record にまとめられ、``source`` が ``|`` 区切りになる
    (例: 瞑想アプリと Apple Watch のマインドフルネスを同時に記録した日)。
    qty は合算されているので、回数だけソース数で数え直す。
    """
    source = record.get("source")
    if not isinstance(source, str):
        return 1
    names = {part.replace("\u00a0", " ").strip() for part in source.split("|")}
    names.discard("")
    return max(1, len(names))


def _mindful(
    metric: "Mapping[str, object] | None", target_date: date
) -> "tuple[int | None, float | None]":
    """``(mindful_sessions, mindful_minutes)``。

    * sessions = record 数。ただし分単位集計で複数ソースがまとめられた record
      (``source`` が ``|`` 区切り) はソース数で数える (:func:`_source_count`)
    * minutes = SUM(qty)。qty が無い record は ``start``/``end``
      (``startDate``/``endDate``) の差で補う。合計が 0 以下なら None (旧
      :func:`ihealth.parser.parse_mindful_minutes` と同じ「測定なし」扱い)
    * 未知の単位の qty / 時間を求められる record が 1 件も無い → ValueError
    """
    records = _records_on(metric, target_date)
    if not records or metric is None:
        return None, None
    factor = _unit_factor(metric.get("units"), _DURATION_TO_MIN, "min")
    sessions = sum(_source_count(record) for _dt, record in records)
    total = 0.0
    measured = False
    for _dt, record in records:
        qty = _num(record.get("qty"))
        if qty is not None:
            if factor is None:
                raise _unit_error(metric.get("units"))
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
    if not measured:
        raise ValueError(
            f"対象日の record {len(records)} 件のどれからも瞑想時間を求められません"
        )
    minutes = total if total > 0 else None
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


def _sleep_hours_factor(metric: "Mapping[str, object] | None") -> float:
    """睡眠 metric の単位 → 時間への係数 (既定 hr)。未知の単位は ValueError。"""
    units = metric.get("units") if metric is not None else None
    per_min = _unit_factor(units, _DURATION_TO_MIN, "hr")
    if per_min is None:
        raise _unit_error(units)
    return per_min / 60.0


def _aggregated_sleep_hours(
    records: "Sequence[Mapping[str, object]]",
    target_date: date,
    hours_factor: float,
) -> "float | None":
    """aggregateSleep=true の集計レコードから ``target_date`` の総睡眠 (時間)。

    record の ``date`` の暦日 (無ければ ``sleepEnd`` の暦日) が target_date のもの。
    値は ``totalSleep`` (無い / 0 なら asleep+core+deep+rem)。複数ソース・重複が
    あっても最大の 1 つを採用する (加算すると二重計上)。0 以下は「測定なし」扱い。
    どの record の日付も解釈できなければ ValueError。
    """
    best: "float | None" = None
    anchored = 0
    for record in records:
        anchor = parse_hae_datetime(record.get("date"))
        if anchor is None:
            anchor = parse_hae_datetime(record.get("sleepEnd"))
        if anchor is None:
            continue
        anchored += 1
        if anchor.date() != target_date:
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
    if records and anchored == 0:
        raise ValueError(
            f"睡眠集計 record {len(records)} 件の日付 (date / sleepEnd) をどれも解釈できません"
        )
    return best


@dataclass(frozen=True)
class _Segment:
    """非集計睡眠 segment 1 件 (重複除去済み)。"""

    begin: float        #: 開始 epoch 秒 (start が無ければ end)
    end: datetime       #: 終了 (record 自身の offset を保持)
    end_epoch: float
    hours: float        #: asleep 系 stage の時間 (それ以外は 0)
    asleep: bool


@dataclass(frozen=True)
class _SleepResult:
    sleep_hours: "float | None" = None
    nap_hours: "float | None" = None
    mode: "str | None" = None
    main_sleep_end: "datetime | None" = None
    main_sleep_needs_prev: bool = False


def _collect_segments(
    entries: "Sequence[tuple[Mapping[str, object], float]]",
) -> "dict[str, list[_Segment]]":
    """``(record, 時間係数)`` から source 別の segment 一覧を作る。

    (source, start, end, stage) が同じ重複 segment は 1 回だけ (当日 + 前日 payload の
    結合で同じ segment が 2 回来ても二重計上しない)。asleep 系の duration は qty
    (時間) が無ければ end - start。Awake / In Bed はセッションの間隔を埋めるためだけに
    duration 0 で残す。segment があるのに 1 件も終了時刻を解釈できなければ ValueError。
    """
    seen: "set[tuple[str, float | None, float, str]]" = set()
    by_source: "dict[str, list[_Segment]]" = {}
    with_end = 0
    for record, hours_factor in entries:
        stage = str(record.get("value"))
        start = _first_datetime(record, ("startDate", "start"))
        end = _first_datetime(record, ("endDate", "end"))
        if end is None:
            continue
        with_end += 1
        source_raw = record.get("source")
        source = source_raw if isinstance(source_raw, str) else ""
        start_epoch = _epoch(start) if start is not None else None
        end_epoch = _epoch(end)
        key = (source, start_epoch, end_epoch, stage.strip().lower())
        if key in seen:
            continue
        seen.add(key)
        asleep = _is_asleep_stage(stage)
        if asleep:
            qty = _num(record.get("qty"))
            if qty is not None and qty >= 0:
                hours = qty * hours_factor
            elif start_epoch is not None and end_epoch >= start_epoch:
                hours = (end_epoch - start_epoch) / 3600.0
            else:
                continue
        elif start_epoch is None or end_epoch < start_epoch:
            continue  # 区間の無い Awake / In Bed は間隔を埋められない
        else:
            hours = 0.0
        begin = start_epoch if start_epoch is not None else end_epoch
        by_source.setdefault(source, []).append(
            _Segment(begin=begin, end=end, end_epoch=end_epoch, hours=hours, asleep=asleep)
        )
    if entries and with_end == 0:
        raise ValueError(f"睡眠 segment {len(entries)} 件の終了時刻をどれも解釈できません")
    return by_source


def _classify_segment(
    segment: _Segment, later_asleep: "Sequence[_Segment]"
) -> "tuple[str | None, date | None]":
    """segment を ``("main" | "nap", 帰属日)`` に分類する (どちらでもなければ ``(None, None)``)。

    * 終了の現地 hour が [0,14) → その日の夜主睡眠、[14,20) → その日の昼寝
    * [20,24) (0 時前に終わる夜の前半) → 同じセッションの後続 asleep segment に
      「翌日の [0,14) に終わるもの」があれば翌日の夜主睡眠へ繰り越す。無ければ
      どの日にも数えない (旧 AutoSync の分類と同じく 20 時以降の短い睡眠は対象外)

    後続の segment が先行 segment の分類を変えることは無い (長い朝寝の後の昼寝が
    夜主睡眠ごと昼寝に化ける、などを起こさない)。
    """
    hour = segment.end.hour
    if _MAIN_SLEEP_END_HOURS[0] <= hour < _MAIN_SLEEP_END_HOURS[1]:
        return "main", segment.end.date()
    if _NAP_END_HOURS[0] <= hour < _NAP_END_HOURS[1]:
        return "nap", segment.end.date()
    next_day = segment.end.date() + timedelta(days=1)
    for other in later_asleep:
        if (
            other.end.date() == next_day
            and _MAIN_SLEEP_END_HOURS[0] <= other.end.hour < _MAIN_SLEEP_END_HOURS[1]
        ):
            return "main", next_day
    return None, None


def _segment_sleep(
    entries: "Sequence[tuple[Mapping[str, object], float]]",
    target_date: date,
) -> _SleepResult:
    """非集計 segment から ``target_date`` の夜主睡眠 / 昼寝を求める。

    1. :func:`_collect_segments` で重複除去 (source, start, end, stage)
    2. source ごとに時刻順に並べ、間隔 :data:`SLEEP_SESSION_GAP_SEC` 以内 (Awake /
       In Bed segment も含めて) を 1 セッションにまとめる
    3. asleep 系 segment を **segment ごとの終了時刻** で分類する
       (:func:`_classify_segment`。0 時前に終わる前半だけセッション経由で翌朝へ繰り越す)
    4. source ごとに合計し、最大の source を採用 (Apple Watch と別アプリ等の
       重複投入を二重計上しない)
    """
    by_source = _collect_segments(entries)
    main_by_source: "dict[str, float]" = {}
    nap_by_source: "dict[str, float]" = {}
    main_end: "datetime | None" = None
    needs_prev = False
    for source, segments in by_source.items():
        segments.sort(key=lambda item: (item.begin, item.end_epoch))
        sessions: "list[list[_Segment]]" = []
        session_end = 0.0
        for segment in segments:
            if sessions and segment.begin - session_end <= SLEEP_SESSION_GAP_SEC:
                sessions[-1].append(segment)
                session_end = max(session_end, segment.end_epoch)
            else:
                sessions.append([segment])
                session_end = segment.end_epoch
        for session in sessions:
            session_start = min(item.begin for item in session)
            asleep = [item for item in session if item.asleep]
            for index, segment in enumerate(asleep):
                kind, day = _classify_segment(segment, asleep[index + 1:])
                if day != target_date:
                    continue
                if kind == "nap":
                    nap_by_source[source] = nap_by_source.get(source, 0.0) + segment.hours
                    continue
                main_by_source[source] = main_by_source.get(source, 0.0) + segment.hours
                if main_end is None or segment.end_epoch > _epoch(main_end):
                    main_end = segment.end
                midnight = datetime.combine(target_date, time.min, tzinfo=segment.end.tzinfo)
                if session_start < _epoch(midnight) + SLEEP_SESSION_GAP_SEC:
                    needs_prev = True

    return _SleepResult(
        sleep_hours=max(main_by_source.values()) if main_by_source else None,
        nap_hours=max(nap_by_source.values()) if nap_by_source else None,
        mode=SLEEP_MODE_SEGMENTS,
        main_sleep_end=main_end,
        main_sleep_needs_prev=needs_prev,
    )


def _sleep(
    metric: "Mapping[str, object] | None",
    prev_metric: "Mapping[str, object] | None",
    target_date: date,
) -> _SleepResult:
    """睡眠を集計する。

    * aggregated (aggregateSleep=true): **当日 payload の** 集計レコードだけを使う。
      sleep_hours = その日の totalSleep (= 昼寝も含む総睡眠)。昼寝は分離できないので
      nap_hours = None (HealthLoader が AutoSync .hae から補完する)。前日 payload の
      集計レコードは使わない (前日ファイルにある「翌日付」のレコードは書き出し時点の
      途中経過で、当日ファイルの同じ日付のレコードが正)
    * segments: 当日 + 前日 payload の segment を結合して夜主睡眠 / 昼寝に分ける
      (当日が segment 形式、または当日に睡眠レコードが無い日。設定切替日に前日が
      aggregated でも当日の segment で集計できる)
    """
    own = _strict_records(metric)
    prev = _records(prev_metric)
    aggregated = [record for record in own if _is_aggregated_sleep(record)]
    own_segments = [record for record in own if _is_sleep_segment(record)]
    prev_segments = [record for record in prev if _is_sleep_segment(record)]
    if own and not aggregated and not own_segments:
        raise ValueError(f"睡眠 record {len(own)} 件の形式を解釈できません")

    if aggregated:
        total = _aggregated_sleep_hours(aggregated, target_date, _sleep_hours_factor(metric))
        if total is not None or not own_segments:
            return _SleepResult(sleep_hours=total, mode=SLEEP_MODE_AGGREGATED)
    if not own_segments and not prev_segments:
        return _SleepResult()

    entries: "list[tuple[Mapping[str, object], float]]" = []
    if own_segments:
        own_factor = _sleep_hours_factor(metric)
        entries.extend((record, own_factor) for record in own_segments)
    if prev_segments:
        prev_factor = _sleep_hours_factor(prev_metric)
        entries.extend((record, prev_factor) for record in prev_segments)
    return _segment_sleep(entries, target_date)


# ---------- ワークアウト / アクティビティ詳細 ----------


def workout_name_ja(name: object) -> str:
    """HAE のワークアウト名を日本語名にする (未知はそのまま、空は ``その他``)。"""
    if not isinstance(name, str) or not name.strip():
        return "その他"
    normalized = " ".join(name.strip().lower().split())
    return WORKOUT_NAMES_JA.get(normalized, name.strip())


def _workouts_included(payload: "Mapping[str, object] | None") -> bool:
    """payload に ``data.workouts`` (ワークアウトの出力欄) があるか。"""
    if not isinstance(payload, Mapping):
        return False
    data = payload.get("data")
    return isinstance(data, Mapping) and data.get("workouts") is not None


def _workouts(
    payload: "Mapping[str, object] | None", target_date: date, *, strict: bool
) -> "list[tuple[str, float]] | None":
    """target_date のワークアウト。``data.workouts`` が無ければ ``None`` (= 有無が不明)。"""
    data = payload.get("data") if isinstance(payload, Mapping) else None
    workouts = data.get("workouts") if isinstance(data, Mapping) else None
    if workouts is None:
        return None
    if not isinstance(workouts, list):
        if strict:
            raise ValueError(f"workouts が配列ではありません ({type(workouts).__name__})")
        return []

    items: "list[tuple[float, int, str, float]]" = []
    candidates = 0
    dated = 0
    for index, workout in enumerate(workouts):
        if not isinstance(workout, Mapping):
            continue
        candidates += 1
        start = _first_datetime(workout, ("start", "startDate"))
        end = _first_datetime(workout, ("end", "endDate"))
        anchor = start if start is not None else end
        if anchor is None:
            continue  # 日付を判定できないワークアウトはどの日にも数えない
        dated += 1
        if anchor.date() != target_date:
            continue
        duration = _num(workout.get("duration"))
        if duration is not None and duration >= 0:
            minutes = duration / 60.0
        elif start is not None and end is not None and _epoch(end) >= _epoch(start):
            minutes = (_epoch(end) - _epoch(start)) / 60.0
        else:
            continue
        items.append((_epoch(anchor), index, workout_name_ja(workout.get("name")), minutes))
    if strict and candidates and dated == 0:
        raise ValueError(f"ワークアウト {candidates} 件の日時をどれも解釈できません")

    items.sort(key=lambda item: (item[0], item[1]))
    totals: "dict[str, float]" = {}
    for _order, _index, name, minutes in items:
        totals[name] = totals.get(name, 0.0) + minutes
    return list(totals.items())


def extract_workouts(
    payload: "Mapping[str, object] | None", target_date: date
) -> "list[tuple[str, float]]":
    """``data.workouts`` から target_date の ``(日本語名, 分)`` を開始時刻順に返す。

    同じ日本語名は合算する (旧 Google Fit 時代の「種目ごと 1 回」表記に合わせる)。
    duration は ``duration`` (秒) を優先し、無ければ end - start。開始日時の現地暦日
    (開始が無ければ終了) が target_date のものだけを対象にする。日時の無い
    ワークアウトはどの日にも数えない。``data.workouts`` が無ければ空 list
    (有無が不明なことを区別したいときは :func:`parse_automation_day_detailed` の
    ``workouts_included`` を見る)。
    """
    return _workouts(payload, target_date, strict=False) or []


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
    """前日 payload の併読が必要か (= 当日の睡眠が aggregateSleep の集計レコードでないか)。

    aggregateSleep=true の集計レコードは「その日のファイル」に完結しているので
    前日ファイルは不要 (iCloud dataless の前日ファイルを無駄に materialize しない)。
    それ以外 (非集計 segment / 当日ファイルに睡眠レコードが無い) は、前夜の
    segment が前日ファイルにしか無い可能性があるので併読が必要。
    """
    metric = _metrics_by_name(payload, None).get(METRIC_SLEEP)
    return not any(_is_aggregated_sleep(record) for record in _records(metric))


def has_metrics(payload: "Mapping[str, object] | None") -> bool:
    """payload に名前付き metric が 1 件以上あるか。"""
    return bool(_metrics_by_name(payload, None))


def has_data_for(payload: "Mapping[str, object] | None", target_date: date) -> bool:
    """payload の metric に ``target_date`` の record が含まれうるか。

    ``False`` を返すのは「名前付き metric の record が無い」か「全 record の日時を
    解釈でき、どれも target_date ではない」ときだけ。解釈できない metric / record が
    あれば ``True`` (集計側で書式エラーとして報告させる)。同じ日付の候補ファイルが
    複数あるとき、他の日の record しか持たないファイル (前日付の手首皮膚温だけ等)
    で、対象日のデータを持つ別の候補を隠さないための判定。ワークアウトだけの
    ファイルは対象外 (metric を持つ候補を優先する)。
    """
    if not isinstance(payload, Mapping):
        return False
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return False
    metrics = data.get("metrics")
    if not isinstance(metrics, list):
        return False
    for metric in metrics:
        if not isinstance(metric, Mapping):
            continue
        name = metric.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        records = metric.get("data")
        if records is None:
            continue
        if not isinstance(records, list):
            return True
        for record in records:
            if not isinstance(record, Mapping):
                continue
            stamps = [parse_hae_datetime(record.get(key)) for key in _ANY_DATE_KEYS]
            parsed = [stamp for stamp in stamps if stamp is not None]
            if not parsed:
                return True
            if any(stamp.date() == target_date for stamp in parsed):
                return True
    return False


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
    failed: "list[str]" = []
    values: "dict[str, object]" = {name: None for name in HEALTH_FIELDS}

    def metric(name: str) -> "Mapping[str, object] | None":
        return metrics.get(name)

    def guard(label: str, func: "Callable[[], _T]", default: "_T") -> "_T":
        return _guard(label, errors, failed, log, func, default)

    values["step_count"] = guard(
        METRIC_STEP_COUNT, lambda: _step_count(metric(METRIC_STEP_COUNT), target_date), None,
    )
    values["distance_km"] = guard(
        METRIC_DISTANCE,
        lambda: _converted_sum(metric(METRIC_DISTANCE), target_date, _DISTANCE_TO_KM, "km"),
        None,
    )
    values["active_energy_kcal"] = guard(
        METRIC_ACTIVE_ENERGY,
        lambda: _converted_sum(
            metric(METRIC_ACTIVE_ENERGY), target_date, _ENERGY_TO_KCAL, "kcal",
        ),
        None,
    )
    values["exercise_minutes"] = guard(
        METRIC_EXERCISE_TIME,
        lambda: _converted_sum(
            metric(METRIC_EXERCISE_TIME), target_date, _DURATION_TO_MIN, "min",
        ),
        None,
    )
    hr_avg, hr_max, intensity = guard(
        METRIC_HEART_RATE,
        lambda: _heart_rate(metric(METRIC_HEART_RATE), target_date, ctx.max_heart_rate),
        (None, None, None),
    )
    values["exercise_intensity_score"] = intensity
    values["heart_rate_avg"] = hr_avg
    values["heart_rate_max"] = hr_max
    values["heart_rate_resting"] = guard(
        METRIC_RESTING_HEART_RATE,
        lambda: _mean_qty(metric(METRIC_RESTING_HEART_RATE), target_date), None,
    )
    values["oxygen_saturation"] = guard(
        METRIC_OXYGEN_SATURATION,
        lambda: _oxygen_saturation(metric(METRIC_OXYGEN_SATURATION), target_date), None,
    )
    sleep = guard(
        METRIC_SLEEP,
        lambda: _sleep(metric(METRIC_SLEEP), prev_metrics.get(METRIC_SLEEP), target_date),
        _SleepResult(),
    )
    values["sleep_hours"] = sleep.sleep_hours
    values["nap_hours"] = sleep.nap_hours
    mindful_sessions, mindful_minutes = guard(
        METRIC_MINDFUL, lambda: _mindful(metric(METRIC_MINDFUL), target_date), (None, None),
    )
    values["mindful_sessions"] = mindful_sessions
    values["mindful_minutes"] = mindful_minutes
    values["body_mass_kg"] = guard(
        METRIC_BODY_MASS, lambda: _body_mass_kg(metric(METRIC_BODY_MASS), target_date), None,
    )
    values["body_fat_percentage"] = guard(
        METRIC_BODY_FAT,
        lambda: _body_fat_percentage(metric(METRIC_BODY_FAT), target_date), None,
    )
    workouts: "list[tuple[str, float]] | None" = guard(
        WORKOUTS_LABEL, lambda: _workouts(payload, target_date, strict=True), None,
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
    mindful_unknown = METRIC_MINDFUL in failed or "mindful_minutes" in dropped
    if not present or workouts is None or mindful_unknown:
        # metric が無い / ワークアウトを出力しない設定 (data.workouts が無い) /
        # 瞑想やワークアウトを解釈できなかった → 「なし」「瞑想N分」とは言えない
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
        sleep_mode=sleep.mode,
        errors=tuple(errors),
        dropped_fields=dropped,
        failed_metrics=tuple(failed),
        workouts=None if workouts is None else tuple(workouts),
        main_sleep_end=sleep.main_sleep_end,
        main_sleep_needs_prev=sleep.main_sleep_needs_prev,
        workouts_included=_workouts_included(payload),
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
