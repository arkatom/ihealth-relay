"""Obsidian 日記 1 日分の書き込み単位 (健康 + 天気 + GitHub 活動) を表す型.

:class:`DiaryRecord` は「その日の日記ノートに書きたい値の束」で、各 source
(HAE オートメーション JSON / 気象庁 / GitHub) が **独立に** 取得した結果を
統合層 (``ihealth.diary``) が 1 つにまとめて
:class:`ihealth.publishers.obsidian.ObsidianPublisher` に渡す.

None の意味 (models.py の方針を踏襲):
    ``None`` は「書かない = 日記側の既存値を保持」. 取得失敗・欠測・未確定の
    source はフィールドごと ``None`` にしておけば、publisher は該当キーに
    一切触れない. 「活動なし」のように **確定した空** を書きたい場合は
    呼び出し側が明示的な文字列 (例: GitHub の ``"該当なし"``) を入れる.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from ihealth.models import DailyHealthData


#: 天気系の内部名 (= :class:`WeatherLike` の属性名) を日記テンプレートの並び順で.
#: publisher はこの名前で ``getattr`` して frontmatter キーに対応付ける.
WEATHER_FIELDS: "tuple[str, ...]" = (
    "temperature",
    "humidity",
    "pressure",
    "weather",
    "sunshine",
    "precipitation",
)


class WeatherLike(Protocol):
    """天気サマリの最小インタフェース (``ihealth.weather.WeatherSummary`` 互換).

    各属性は日記にそのまま書く整形済み文字列. ``None`` は「欠測 / 未確定なので
    書かない」(SPEC v2 §4.3: キー単位の完全性). publisher 側は
    ``getattr`` で読むだけなので、同名属性を持つ任意のオブジェクトを渡せる
    (テストでは ``types.SimpleNamespace`` で十分).
    """

    weather: "str | None"
    temperature: "str | None"
    humidity: "str | None"
    precipitation: "str | None"
    pressure: "str | None"
    sunshine: "str | None"


@dataclass(frozen=True)
class DiaryRecord:
    """1 日分の日記書き込み値.

    Attributes:
        date: 対象日 (日記ノート ``YYYY-MM-DD.md`` の日付).
        health: 健康データ. ``None`` なら健康系キーには触れない.
        activity_detail: ``アクティビティ詳細`` に書く文字列 (例 ``"瞑想5分、ウォーキング20分"``).
        weather: 天気サマリ. ``None`` なら天気系キーには触れない.
        github: ``Github`` に書く整形済み文字列 (活動なしが確定した日は ``"該当なし"``).

    Raises:
        ValueError: ``health.date`` が ``date`` と一致しない (別の日のデータを
            取り違えて書き込む事故を構築時点で止める).
    """

    date: date
    health: "DailyHealthData | None" = None
    activity_detail: "str | None" = None
    weather: "WeatherLike | None" = None
    github: "str | None" = None

    def __post_init__(self) -> None:
        if self.health is not None and self.health.date != self.date:
            raise ValueError(
                f"DiaryRecord.date ({self.date}) と health.date "
                f"({self.health.date}) が一致しません"
            )
