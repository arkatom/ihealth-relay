"""気象庁「過去の気象データ検索」の 1 時間ごとの値 (``hourly_s1``) から日次の天気サマリを作る.

前身 GoogleFitNotionIntegration の ``src/weather/weather_notion.py`` (requests +
BeautifulSoup) を **標準ライブラリだけ** (``urllib.request`` + ``html.parser``)
で置き換えたもの. 出力する 6 文字列 (Obsidian 日記 frontmatter の ``気温`` /
``湿度`` / ``気圧`` / ``天気`` / ``日照時間`` / ``降水量``) は、前身が Notion に
書いていた (= Obsidian に移行済みの過去ノートに残っている) 文字列と **同一書式・
同一集計** で作る (値は合成例)::

    気温: "朝:平均21.0℃, 昼:平均24.5℃, 夜:平均18.2℃（最高:26.1℃, 最低:15.3℃）"
    湿度: "朝:平均70.4%, 昼:平均55.0%, 夜:平均81.3%（最高:90%, 最低:48%）"
    気圧: "1-6時:平均1012.3hPa, 7-12時:平均1011.0hPa⤵️💣️, 13-18時:平均1005.2hPa, 19-24時:平均1004.8hPa"
    天気: "3時: 晴れ☀️, 6時: 快晴☀️☀️, 9時: 薄曇⛅️, 12時: 曇☁️, 15時: 雨☔, 18時: 雷電, 21時: 雪❄️"
    日照時間: "6.4時間"
    降水量: "朝:0.0mm, 昼:3.5mm, 夜:0.5mm"

## 集計規則 (前身 ``get_weather_data`` と同一)

- 気象庁の「N 時」の行は (N-1):00〜N:00 の 1 時間 (1〜24 時).
- 区分: 朝 = 5〜11 時, 昼 = 12〜18 時, 夜 = 19〜24 時 + 1〜4 時.
- 気温 / 湿度: 区分ごとの平均 (``statistics.mean``) を小数 1 桁. 最高 / 最低は全時間
  (湿度は整数のまま ``{}`` で出す).
- 降水量: 区分ごとの合計 ``{:.1f}mm``. 日照時間: 1 日の合計 ``{:.1f}時間``.
- 気圧: 海面気圧を 1-6 / 7-12 / 13-18 / 19-24 時の 4 span で平均. span の最大値 −
  次 span のいずれかの値 ≥ 5 なら ``⤵️💣️``、そうでなく 次 span のいずれかの値 −
  span の最小値 ≥ 5 なら ``⤴️⚠️`` (最後の span は無印). 境界判定は前身と同じ
  float 演算 (``>= 5``) で行う.
- 天気: 天気欄の画像 alt (3 時間ごとの目視観測) を ``"{時}時: {alt}{絵文字}"`` で
  ``", "`` 連結. 絵文字は 快晴 → 晴 → 薄曇 → 曇 → 雨 → 雪 の順に部分一致判定.

## 前身からの意図的な変更 (誤った値を書かないための安全側の修正)

1. **欠測を 0 で埋めない** (SPEC v2 §4.3): 区分 (朝/昼/夜, 気圧の 6 時間 span)
   の値が全欠測ならそのキーは ``None`` (= 書かない, 既存値保持). 前身は 0℃ / 0% を
   書いていた. 合計系 (降水量 / 日照時間) は 1 時間でも欠測があると合計が過小に
   なるので、欠測が 1 つでもあれば ``None`` にする.
2. **日の完全性**: 24 時の行に気温と海面気圧があり、天気 (img alt) が 1 件以上
   あるときだけ ``is_complete=True``. 未完成の日は全キー ``None`` で返す.
   当日・未来日のページには表そのものが無く、前日分は数値の行が先に出て天気欄が
   後から埋まる (前身が朝の実行では天気を取れず、実行を午後に移した理由).
   そのため :func:`fetch_weather` は、観測所の日付 (JST) で今日から
   ``_PUBLICATION_GRACE_DAYS`` 日以内の未完成を「公開待ち」(``pending``,
   ``status == "nodata"`` = 想定内の遅延) とし、それより古い日の未完成だけを
   ``"partial"`` (確定済みのはずの日が埋まらない = 気象庁の欠測か、天気欄の形式など
   ページ仕様の変更) として警告ログに出す.
3. **品質記号** (気象庁「値欄の記号の説明」): ``)`` 準正常値は記号を除いた値を正常値
   として採用 (``--)`` は「該当現象なし」の準正常値).
   ``]`` 資料不足値 (「値そのものを信用することはできず、通常は上位の統計に用いません」),
   ``#`` 疑問値 (値は表示されない), ``×`` / ``///`` 欠測, 空欄 (未観測・通信障害)
   は欠測扱い. ``--`` (該当現象なし) は降水量 / 日照時間では 0.
   日照時間の空欄は夜間 (観測対象外) なら 0 として正しいので、数値のある時間帯の
   内側にある空欄と、全国どこでも日中である 8〜16 時の欠落だけを欠測とみなす.
4. **1 時の行を使う** (``_FIRST_HOUR = 1``): 前身は見出し行のスキップ判定 ``if i > 2``
   が 1 行ずれていて (見出しは 2 行), 1 時 (0:00〜1:00) の行を読み捨てていた.
   本実装は 1〜24 時を全て使う. このため 1-6 時の気圧平均・夜の気温/湿度平均 (1 時が
   極値の日は最高/最低, 1 時に降水がある日は夜の降水量, 稀に気圧マークも) が前身の値
   (= 移行済みの過去ノート) と異なる. 実ページ 72 日分を前身 (bs4 版) そのもので処理した
   結果と比べ、差は全てこの 1 時の行で説明できる. ``_FIRST_HOUR = 2`` にすると前身と
   完全に同じ集計になる (tests の ``LegacyParityTest`` が両方の設定を前身の逐語転写と
   突き合わせている).
5. **列位置は見出しから特定**: rowspan / colspan を展開した見出しラベルから
   海面気圧 / 降水量 / 気温 / 湿度 / 日照 / 天気 の列を一意に探す. 見つからない /
   複数ある場合は :class:`WeatherError` (レイアウト変更を黙って誤パースしない).
   時刻列も同様で、数字を含むのに整数として読めない時刻 (``1時`` / ``00:00`` 等) や、
   データ行があるのに時刻を 1 つも読めない表は :class:`WeatherError`.
6. ページ見出しの日付 (``2026年10月10日``) が要求日と違えば :class:`WeatherError`.

ネットワークに出るのは :func:`fetch_weather` だけ. 呼び出し側 (diary 統合) は
気象庁への負荷を避けるため **1 リクエスト / 秒以下** に間隔を空けること.
"""

from __future__ import annotations

import codecs
import http.client
import logging
import math
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ihealth import __version__


#: 既定の観測所: 東京 (府県番号 44 / 地点番号 47662). 前身と同じ.
DEFAULT_PREC_NO = 44
DEFAULT_BLOCK_NO = 47662

#: 気象庁 過去の気象データ検索「1 時間ごとの値」(気象官署 = s1) のエンドポイント.
JMA_HOURLY_ENDPOINT = "https://www.data.jma.go.jp/stats/etrn/view/hourly_s1.php"

#: 気象庁へのリクエストに付ける User-Agent (連絡先として repo URL を含める).
USER_AGENT = f"ihealth-relay/{__version__} (+https://github.com/arkatom/ihealth-relay)"

#: :class:`WeatherSummary` の文字列フィールド名 (SPEC の順).
WEATHER_FIELDS: "tuple[str, ...]" = (
    "weather",
    "temperature",
    "humidity",
    "precipitation",
    "pressure",
    "sunshine",
)

#: 未公開日 (当日・未来日) のページに出る案内文の一部.
#: 例: 「2026年10月11日のページを表示しましたが、閲覧可能な日は、昨日2026年10月10日までです。」
_UNAVAILABLE_MARKER = "閲覧可能な日"

#: 気象庁の日付 (観測所は全て日本標準時).
_JST = timezone(timedelta(hours=9), "JST")

#: 未完成のページを「公開待ち (想定内)」とみなす日数. 今日 (JST) − 対象日 がこの日数以下なら
#: 未完成でも ``status == "nodata"``. 前日分は数値の行が先に公開され天気欄が後から埋まる
#: (前身は JST 8 時の実行では天気を取れず 15 時に移し、さらに前々日も毎回取り直していた).
#: それより古い日の未完成は確定済みのはずなので ``"partial"`` (警告) にする.
_PUBLICATION_GRACE_DAYS = 2

#: 集計に使う最初の時刻行. 1 = 1〜24 時を全て使う (前身の 1 時欠落 off-by-one を直した値).
#: 2 にすると前身 (``if i > 2`` で 1 時の行を読み捨てる) と完全に同じ集計になる.
#: 1-6 時の気圧 span の見出し (``1-6時``) はどちらでも同じ.
_FIRST_HOUR = 1

#: 応答本文の上限 (実ページは 25KB 程度). 想定外の巨大応答でメモリを食わない.
_MAX_BODY_BYTES = 5 * 1024 * 1024

#: リトライ間隔の基準 (秒). attempt 回目は ``_BACKOFF_BASE_SEC * 2**attempt`` 待つ.
_BACKOFF_BASE_SEC = 1.0
_BACKOFF_MAX_SEC = 30.0

#: 再試行する HTTP status (5xx に加えて 429 Too Many Requests).
_RETRYABLE_STATUS_MIN = 500

#: colspan / rowspan の上限 (不正値で grid が爆発しないように).
_MAX_SPAN = 100

#: 区分: 朝 = 5〜11 時, 昼 = 12〜18 時, 夜 = 19〜24 時 + 1〜4 時 (前身と同一).
_MORNING = "morning"
_DAYTIME = "daytime"
_EVENING = "evening"
_SEGMENTS: "tuple[str, ...]" = (_MORNING, _DAYTIME, _EVENING)

#: 気圧の 6 時間 span の開始時刻 (前身 ``range(1, 25, 6)``): 1-6 / 7-12 / 13-18 / 19-24 時.
_PRESSURE_SPAN_STARTS: "tuple[int, ...]" = (1, 7, 13, 19)
_PRESSURE_SPAN_HOURS = 6

#: 気圧変化マークの閾値 (hPa). 前身と同じく ``>= 5`` (float 演算) で判定する.
_PRESSURE_CHANGE_THRESHOLD = 5
#: 絵文字表示を指定する異体字セレクタ (U+FE0F). 前身の文字列 (= 過去ノート) に含まれるので落とさない.
_VS16 = "\N{VARIATION SELECTOR-16}"
_PRESSURE_DROP_MARK = (
    "\N{ARROW POINTING RIGHTWARDS THEN CURVING DOWNWARDS}" + _VS16 + "\N{BOMB}" + _VS16
)  # ⤵️💣️
_PRESSURE_RISE_MARK = (
    "\N{ARROW POINTING RIGHTWARDS THEN CURVING UPWARDS}" + _VS16 + "\N{WARNING SIGN}" + _VS16
)  # ⤴️⚠️

#: 日照時間の「全国どこでも日中を含む」時刻の行 (8〜16 時 = 7:00〜16:00).
#: 日本で最も遅い日の出 (1 月の八重山, 7 時半頃) でも 8 時の行 (7:00〜8:00) に、最も早い
#: 日の入り (12 月の根室, 15 時 40 分頃) でも 16 時の行 (15:00〜16:00) に数十分の日中が含まれる.
#: 日中を 15 分程度以上含む行は 0.0 でも数値が出る (東京・札幌の実ページの 6 時の行などで確認) が、
#: 日の出 / 日の入り前後の数分しか含まない行は空欄になりうる (札幌 2026-10-10 の 18 時の行).
#: そのため端の時間帯は必須にせず、この 8〜16 時が空欄のときだけ欠測とみなす.
_SUNSHINE_CORE_HOURS: "tuple[int, ...]" = tuple(range(8, 17))

#: 天気 alt → 絵文字. 前身 ``get_weather_emoji`` と同じ判定順 (部分一致, 先勝ち).
_WEATHER_EMOJI_RULES: "tuple[tuple[str, str], ...]" = (
    ("快晴", "\N{BLACK SUN WITH RAYS}" + _VS16 + "\N{BLACK SUN WITH RAYS}" + _VS16),  # ☀️☀️
    ("晴", "\N{BLACK SUN WITH RAYS}" + _VS16),  # ☀️
    ("薄曇", "\N{SUN BEHIND CLOUD}" + _VS16),  # ⛅️
    ("曇", "\N{CLOUD}" + _VS16),  # ☁️
    ("雨", "\N{UMBRELLA WITH RAIN DROPS}"),  # ☔ (前身も異体字セレクタなし)
    ("雪", "\N{SNOWFLAKE}" + _VS16),  # ❄️
)

#: 見出しラベル (空白除去後) で列を特定するためのキーワード. 各キーワードは
#: ちょうど 1 列に一致しなければならない (蒸気圧 / 露点温度 などと衝突しない語を選ぶ).
_COLUMN_KEYWORDS: "tuple[tuple[str, str], ...]" = (
    ("sea_level_pressure", "海面"),
    ("precipitation", "降水量"),
    ("temperature", "気温"),
    ("humidity", "湿度"),
    ("sunshine", "日照"),
    ("weather", "天気"),
)
#: 時刻列の見出し (完全一致).
_HOUR_COLUMN_LABEL = "時"

#: 準正常値記号 (値の後ろに付く. 前に空白を挟むことがある: ``12.3 )`` / ``--)``).
_QUASI_NORMAL_MARK = ")"
#: 数値 (符号・小数可). 準正常値記号は除去済みの文字列に当てる. ``]`` / ``#`` 付きや
#: ``×`` ``///`` は一致しない (= 欠測).
_NUMBER_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
#: 時刻列のセルが「時刻らしい」(数字を含む) か. 整数として読めない時刻はレイアウト変更とみなす.
_DIGIT_RE = re.compile(r"\d")
#: 見出しの日付 (例: ``東京（東京都)　2026年10月10日　（１時間ごとの値）``).
_PAGE_DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
#: ``<meta charset=...>`` / ``<meta http-equiv=... content="...; charset=...">``.
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]*?charset\s*=\s*["']?\s*([A-Za-z0-9._:-]+)""",
    re.IGNORECASE,
)

#: セル値の分類.
_KIND_VALUE = "value"      # 数値 (準正常値 ``)`` を含む)
_KIND_NONE = "none"        # ``--`` 該当現象なし
_KIND_BLANK = "blank"      # 空欄 (未観測 / 夜間の日照 / 通信障害)
_KIND_MISSING = "missing"  # ``×`` / ``///`` / ``]`` / ``#`` / 解釈不能

#: 注入可能な HTTP opener: ``(request, timeout) -> 応答本文 bytes``.
Opener = Callable[[urllib.request.Request, float], bytes]


class WeatherError(RuntimeError):
    """気象庁ページの取得失敗 / レイアウト変更 / 想定外ページ."""


@dataclass(frozen=True)
class HourlyObservation:
    """気象庁 hourly_s1 の 1 行 (1 時間分) の観測値.

    数値フィールドの ``None`` は「使える値が無い」(空欄 / 欠測記号 / 資料不足値 /
    疑問値) を表す. 降水量と日照時間の ``--`` (該当現象なし) は 0.0.
    日照時間だけは空欄 (夜間 = 観測対象外, 0 として正しい) と欠測記号を区別する
    必要があるので ``sunshine_missing`` を持つ.
    """

    hour: int                                   # 1〜24 ((hour-1):00〜hour:00)
    sea_level_pressure: "float | None" = None   # 海面気圧 (hPa)
    precipitation: "float | None" = None        # 降水量 (mm)
    temperature: "float | None" = None          # 気温 (℃)
    humidity: "float | None" = None             # 湿度 (%) — 整数値なら int で保持 (前身互換の表示)
    sunshine: "float | None" = None             # 日照時間 (h). 空欄 / 欠測は None
    sunshine_missing: bool = False              # 日照が欠測記号 (×, ///, ], # 等) だった
    weather: "str | None" = None                # 天気 (天気欄 img の alt)


@dataclass(frozen=True)
class WeatherSummary:
    """1 日分の天気サマリ (Obsidian 日記 frontmatter の 6 キー分).

    文字列フィールドの ``None`` は「欠測 / 未完成のため書かない」(既存値を保持)
    を表す. ``is_complete=False`` の日は全文字列フィールドが ``None``.
    """

    date: date
    weather: "str | None"        # 天気
    temperature: "str | None"    # 気温
    humidity: "str | None"       # 湿度
    precipitation: "str | None"  # 降水量
    pressure: "str | None"       # 気圧
    sunshine: "str | None"       # 日照時間
    is_complete: bool            # 24 時の気温・海面気圧 + 天気 1 件以上 (SPEC v2 §4.3)
    observed_hours: int = 0      # 表から読めた時間行の数 (0 = 未公開ページ)
    pending: bool = False        # 未完成なのは公開待ち (直近の日) のため = 想定内. 完成した日は常に False

    @property
    def missing_keys(self) -> "tuple[str, ...]":
        """値が ``None`` (= 書かない) のフィールド名 (:data:`WEATHER_FIELDS` の順)."""
        return tuple(name for name in WEATHER_FIELDS if getattr(self, name) is None)

    @property
    def status(self) -> str:
        """SPEC v2 §4.1 の完全性ステータス.

        - ``"complete"``: 日が完成していて 6 キー全て値がある
        - ``"nodata"``: 日が未完成で、それが想定内 = 公開待ちの期間内 (``pending``:
          当日・未来日のページには観測行が無く、前日分は天気欄が後から埋まる).
          何も書かず、通知もしない (次回以降の実行で埋まる)
        - ``"partial"``: 完成した日の一部キーが欠測 (そのキーだけ書かない), または
          確定済みのはずの日 (``pending=False``) が未完成 (何も書かない. 気象庁の欠測か
          ページ仕様の変更の可能性があるので、続くようなら利用者に知らせる).
          観測行が 1 つも無い場合も、公開待ちの期間を過ぎていれば ``"partial"``
          (廃止・休止した観測所 / ``JMA_PREC_NO`` / ``JMA_BLOCK_NO`` の誤り /
          過去日なのに「閲覧可能な日は…」の案内が返る障害を、黙って「データなし」に
          しない. 統合側の「2 日以上続けて未完成なら通知」に乗る)

        取得自体の失敗 (``"failed"``) は :class:`WeatherError` として raise される.
        """
        if self.is_complete:
            return "complete" if not self.missing_keys else "partial"
        if self.pending:
            return "nodata"
        return "partial"


# ---------------------------------------------------------------------------
# URL
# ---------------------------------------------------------------------------


def _positive_int(value: object, name: str) -> int:
    """観測所番号を正の int に正規化する (``"44"`` のような文字列も許容)."""
    if isinstance(value, bool):
        raise ValueError(f"{name} は正の整数である必要があります: {value!r}")
    try:
        number = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        raise ValueError(f"{name} は正の整数である必要があります: {value!r}") from None
    if number <= 0:
        raise ValueError(f"{name} は正の整数である必要があります: {value!r}")
    return number


def jma_hourly_url(
    d: date,
    prec_no: int = DEFAULT_PREC_NO,
    block_no: int = DEFAULT_BLOCK_NO,
) -> str:
    """指定日の「1 時間ごとの値」ページ URL (前身と同じく月日は 0 埋め).

    例: ``https://www.data.jma.go.jp/stats/etrn/view/hourly_s1.php?prec_no=44&block_no=47662&year=2026&month=10&day=10&view=p1``
    """
    query = urllib.parse.urlencode(
        (
            ("prec_no", _positive_int(prec_no, "prec_no")),
            ("block_no", _positive_int(block_no, "block_no")),
            ("year", f"{d.year:04d}"),
            ("month", f"{d.month:02d}"),
            ("day", f"{d.day:02d}"),
            ("view", "p1"),
        )
    )
    return f"{JMA_HOURLY_ENDPOINT}?{query}"


# ---------------------------------------------------------------------------
# HTML パース
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Cell:
    tag: str                 # "td" | "th"
    text: str                # セル内テキスト (前後空白除去, <br> は無視)
    colspan: int = 1
    rowspan: int = 1
    img_alt: "str | None" = None   # セル内の最初の img の alt (img / alt が無ければ None)


@dataclass
class _CellBuilder:
    tag: str
    colspan: int
    rowspan: int
    parts: "List[str]" = field(default_factory=list)
    img_seen: bool = False
    img_alt: "Optional[str]" = None

    def build(self) -> _Cell:
        return _Cell(
            tag=self.tag,
            text="".join(self.parts).strip(),
            colspan=self.colspan,
            rowspan=self.rowspan,
            img_alt=self.img_alt,
        )


@dataclass
class _TableBuilder:
    target: bool                                  # class に data2_s を持つ表か
    heading: "Optional[str]" = None               # 表の直前の h3 テキスト
    rows: "List[List[_Cell]]" = field(default_factory=list)
    row: "Optional[List[_Cell]]" = None
    cell: "Optional[_CellBuilder]" = None

    def close_cell(self) -> None:
        if self.cell is None:
            return
        if self.row is None:
            self.row = []
        self.row.append(self.cell.build())
        self.cell = None

    def close_row(self) -> None:
        self.close_cell()
        if self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None


def _span(attrs: "Dict[str, Optional[str]]", name: str) -> int:
    raw = attrs.get(name)
    if raw is None:
        return 1
    try:
        value = int(raw.strip())
    except ValueError:
        return 1
    return min(max(value, 1), _MAX_SPAN)


class _JmaPageParser(HTMLParser):
    """``table.data2_s`` (ネスト位置は問わない) の行とセル, 直前の h3 を集める.

    - 行 / セルは「最も内側の開いている表」に属する. data2_s 表のセル内に別の表が
      ネストしていても、その中身は外側のセルに混ぜない.
    - 閉じタグ省略 (``<td>`` 連続, ``</tr>`` 無し) は次の ``<td>`` / ``<tr>`` /
      ``</table>`` で暗黙に閉じる.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: "List[_TableBuilder]" = []
        self._stack: "List[_TableBuilder]" = []
        self._h3_parts: "Optional[List[str]]" = None
        self._last_h3: "Optional[str]" = None

    # -- helpers --
    def _current(self) -> "Optional[_TableBuilder]":
        if not self._stack:
            return None
        top = self._stack[-1]
        return top if top.target else None

    def handle_starttag(self, tag: str, attrs: "List[Tuple[str, Optional[str]]]") -> None:
        attr_map = {name: value for name, value in attrs}
        if tag == "table":
            classes = (attr_map.get("class") or "").split()
            table = _TableBuilder(target="data2_s" in classes, heading=self._last_h3)
            self._stack.append(table)
            if table.target:
                self.tables.append(table)
            return
        table = self._current()
        if tag == "h3" and table is None:
            self._h3_parts = []
            return
        if table is None:
            return
        if tag == "tr":
            table.close_row()
            table.row = []
        elif tag in ("td", "th"):
            table.close_cell()
            if table.row is None:
                table.row = []
            table.cell = _CellBuilder(
                tag=tag,
                colspan=_span(attr_map, "colspan"),
                rowspan=_span(attr_map, "rowspan"),
            )
        elif tag == "img" and table.cell is not None and not table.cell.img_seen:
            # 前身 (bs4 ``cell.find('img')``) と同じく「最初の img」の alt だけを見る.
            table.cell.img_seen = True
            table.cell.img_alt = attr_map.get("alt")

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            if self._stack:
                table = self._stack.pop()
                table.close_row()
            return
        if tag == "h3":
            if self._h3_parts is not None:
                self._last_h3 = "".join(self._h3_parts).strip()
                self._h3_parts = None
            return
        table = self._current()
        if table is None:
            return
        if tag in ("td", "th"):
            table.close_cell()
        elif tag == "tr":
            table.close_row()

    def handle_data(self, data: str) -> None:
        table = self._current()
        if table is not None:
            if table.cell is not None:
                table.cell.parts.append(data)
            return
        if self._h3_parts is not None:
            self._h3_parts.append(data)

    def close(self) -> None:
        super().close()
        while self._stack:
            self._stack.pop().close_row()


def _layout(rows: "Sequence[Sequence[_Cell]]") -> "List[Dict[int, _Cell]]":
    """rowspan / colspan を展開して ``行 → {列番号: セル}`` の grid にする."""
    grid: "List[Dict[int, _Cell]]" = [dict() for _ in rows]
    for r, row in enumerate(rows):
        col = 0
        for cell in row:
            while col in grid[r]:
                col += 1
            for dr in range(cell.rowspan):
                rr = r + dr
                if rr >= len(rows):
                    break
                for dc in range(cell.colspan):
                    grid[rr].setdefault(col + dc, cell)
            col += cell.colspan
    return grid


def _normalize_label(text: str) -> str:
    return "".join(text.split())


def _column_labels(header_grid: "Sequence[Dict[int, _Cell]]") -> "Dict[int, str]":
    """見出し行 grid から列ごとのラベル (上段→下段の連結, 空白除去) を作る."""
    labels: "Dict[int, str]" = {}
    columns = sorted({col for row in header_grid for col in row})
    for col in columns:
        parts: "List[str]" = []
        previous: "Optional[_Cell]" = None
        for row in header_grid:
            cell = row.get(col)
            if cell is None or cell is previous:
                continue
            parts.append(cell.text)
            previous = cell
        labels[col] = _normalize_label("".join(parts))
    return labels


def _locate_columns(labels: "Dict[int, str]") -> "Dict[str, int]":
    """必要な列の位置を見出しラベルから一意に特定する. 失敗は :class:`WeatherError`."""
    located: "Dict[str, int]" = {}
    problems: "List[str]" = []
    hour_cols = [col for col, label in labels.items() if label == _HOUR_COLUMN_LABEL]
    if len(hour_cols) == 1:
        located["hour"] = hour_cols[0]
    else:
        problems.append(f"{_HOUR_COLUMN_LABEL!r} 列が {len(hour_cols)} 個")
    for name, keyword in _COLUMN_KEYWORDS:
        cols = [col for col, label in labels.items() if keyword in label]
        if len(cols) == 1:
            located[name] = cols[0]
        else:
            problems.append(f"{keyword!r} 列が {len(cols)} 個")
    if problems:
        shown = ", ".join(f"{col}:{labels[col]}" for col in sorted(labels))
        raise WeatherError(
            "気象庁 hourly 表の見出しが想定と違います (レイアウト変更の可能性): "
            + "; ".join(problems)
            + f" [見出し: {shown}]"
        )
    return located


def _classify(text: str) -> "Tuple[str, Optional[float]]":
    """セル文字列を (分類, 数値) にする. 数値は ``_KIND_VALUE`` のときだけ.

    準正常値記号 ``)`` は 1 つだけ外してから判定する (``12.3 )`` → 12.3, ``--)`` →
    該当現象なし). 記号だけ (``)``) や ``]`` / ``#`` 付き, ``×`` / ``///`` は欠測.
    """
    stripped = text.strip()
    if not stripped:
        return _KIND_BLANK, None
    core = stripped
    if core.endswith(_QUASI_NORMAL_MARK):
        core = core[: -len(_QUASI_NORMAL_MARK)].rstrip()
    if core == "--":
        return _KIND_NONE, None
    if _NUMBER_RE.fullmatch(core) is None:
        return _KIND_MISSING, None
    value = float(core)
    if not math.isfinite(value):  # 桁数が極端に多い数字列は inf になる
        return _KIND_MISSING, None
    return _KIND_VALUE, value


def _measured(text: str) -> "Optional[float]":
    """気圧 / 気温 / 湿度用: 数値以外 (``--`` 含む) は全て None."""
    kind, value = _classify(text)
    return value if kind == _KIND_VALUE else None


def _amount(text: str) -> "Tuple[Optional[float], bool]":
    """降水量 / 日照時間用: ``--`` は 0.0. 戻り値は (値, 欠測記号だったか)."""
    kind, value = _classify(text)
    if kind == _KIND_VALUE:
        return value, False
    if kind == _KIND_NONE:
        return 0.0, False
    return None, kind == _KIND_MISSING


def _int_if_integral(value: "Optional[float]") -> "Optional[float]":
    """湿度は前身が ``int()`` で保持していたので、整数値なら int にする (表示 ``88%``)."""
    if value is not None and float(value).is_integer():
        return int(value)
    return value


@dataclass(frozen=True)
class _ParsedPage:
    table_found: bool                         # 見出し付き data2_s 表があったか
    observations: "List[HourlyObservation]"
    page_date: "Optional[date]"               # 表直前の h3 の日付 (読めなければ None)


def _page_date(heading: "Optional[str]") -> "Optional[date]":
    if not heading:
        return None
    match = _PAGE_DATE_RE.search(heading)
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _header_row_count(rows: "Sequence[Sequence[_Cell]]") -> int:
    count = 0
    for row in rows:
        if row and all(cell.tag == "th" for cell in row):
            count += 1
        else:
            break
    return count


def _observations_from_table(table: _TableBuilder) -> "List[HourlyObservation]":
    grid = _layout(table.rows)
    header_count = _header_row_count(table.rows)
    columns = _locate_columns(_column_labels(grid[:header_count]))

    def text_at(row: "Dict[int, _Cell]", name: str) -> str:
        cell = row.get(columns[name])
        return cell.text if cell is not None else ""

    observations: "List[HourlyObservation]" = []
    seen_hours: "set[int]" = set()
    data_rows = 0
    for raw_row, row in zip(table.rows[header_count:], grid[header_count:]):
        if not any(cell.tag == "td" for cell in raw_row):
            continue  # 途中に挟まった見出し行など
        data_rows += 1
        hour_text = text_at(row, "hour")
        try:
            hour = int(hour_text)
        except ValueError:
            if _DIGIT_RE.search(hour_text):
                # "1時" / "00:00" のような時刻の書式変更. 区間の意味 ((N-1)時〜N時) が
                # 変わった可能性もあるので読み替えずに止める.
                raise WeatherError(
                    f"気象庁 hourly 表の時刻列を読めません: {hour_text!r} (レイアウト変更の可能性)"
                ) from None
            continue  # 合計行など時刻でない行
        if not 1 <= hour <= 24:
            raise WeatherError(f"気象庁 hourly 表に 1〜24 以外の時刻行があります: {hour_text!r}")
        if hour in seen_hours:
            raise WeatherError(f"気象庁 hourly 表に同じ時刻の行が複数あります: {hour}時")
        seen_hours.add(hour)

        precipitation, _ = _amount(text_at(row, "precipitation"))
        sunshine, sunshine_missing = _amount(text_at(row, "sunshine"))
        weather_cell = row.get(columns["weather"])
        alt = weather_cell.img_alt if weather_cell is not None else None
        weather = alt.strip() if alt is not None and alt.strip() else None
        observations.append(
            HourlyObservation(
                hour=hour,
                sea_level_pressure=_measured(text_at(row, "sea_level_pressure")),
                precipitation=precipitation,
                temperature=_measured(text_at(row, "temperature")),
                humidity=_int_if_integral(_measured(text_at(row, "humidity"))),
                sunshine=sunshine,
                sunshine_missing=sunshine_missing,
                weather=weather,
            )
        )
    if data_rows and not observations:
        # データ行はあるのに時刻を 1 つも読めない = 時刻列の仕様変更. 「行が 0 件」(未公開)
        # と取り違えて黙って nodata にしないよう失敗にする.
        raise WeatherError(
            f"気象庁 hourly 表のデータ行 {data_rows} 行から時刻を 1 つも読めません (レイアウト変更の可能性)"
        )
    return observations


def _parse_page(html: str) -> _ParsedPage:
    parser = _JmaPageParser()
    parser.feed(html)
    parser.close()
    # 見出し (th だけの行) を持つ data2_s 表が観測表. 後続の「記事」表 (td だけ) は対象外.
    candidates = [t for t in parser.tables if _header_row_count(t.rows) > 0]
    if not candidates:
        return _ParsedPage(table_found=False, observations=[], page_date=None)
    errors: "List[str]" = []
    for table in candidates:
        try:
            observations = _observations_from_table(table)
        except WeatherError as exc:
            errors.append(str(exc))
            continue
        return _ParsedPage(
            table_found=True,
            observations=observations,
            page_date=_page_date(table.heading),
        )
    raise WeatherError(errors[0])


def parse_jma_hourly(html: str) -> "list[HourlyObservation]":
    """気象庁 hourly_s1 ページの HTML から時間ごとの観測値を読む.

    観測表 (``table.data2_s``) が無いページ (当日・未公開日) は空リスト.
    見出しから必要な列を一意に特定できない / 時刻行が異常な場合は :class:`WeatherError`.
    """
    return _parse_page(html).observations


# ---------------------------------------------------------------------------
# 集計 (前身 get_weather_data の 112〜205 行目と同一の式・書式)
# ---------------------------------------------------------------------------


def _segment_of(hour: int) -> str:
    if 5 <= hour <= 11:
        return _MORNING
    if 12 <= hour <= 18:
        return _DAYTIME
    return _EVENING  # hour >= 19 or hour <= 4


def _weather_emoji(condition: str) -> str:
    lowered = condition.lower()
    for keyword, emoji in _WEATHER_EMOJI_RULES:
        if keyword in lowered:
            return emoji
    return ""


def _by_segment(
    rows: "Sequence[HourlyObservation]",
    getter: "Callable[[HourlyObservation], Optional[float]]",
) -> "Optional[Dict[str, List[float]]]":
    """区分ごとの値リスト (時刻順). 1 区分でも全欠測なら None."""
    buckets: "Dict[str, List[float]]" = {name: [] for name in _SEGMENTS}
    for row in rows:
        value = getter(row)
        if value is not None:
            buckets[_segment_of(row.hour)].append(value)
    if any(not values for values in buckets.values()):
        return None
    return buckets


def _format_temperature(rows: "Sequence[HourlyObservation]") -> "Optional[str]":
    buckets = _by_segment(rows, lambda o: o.temperature)
    if buckets is None:
        return None
    all_temps = [o.temperature for o in rows if o.temperature is not None]
    return (
        f"朝:平均{statistics.mean(buckets[_MORNING]):.1f}℃, "
        f"昼:平均{statistics.mean(buckets[_DAYTIME]):.1f}℃, "
        f"夜:平均{statistics.mean(buckets[_EVENING]):.1f}℃"
        f"（最高:{max(all_temps):.1f}℃, 最低:{min(all_temps):.1f}℃）"
    )


def _format_humidity(rows: "Sequence[HourlyObservation]") -> "Optional[str]":
    buckets = _by_segment(rows, lambda o: o.humidity)
    if buckets is None:
        return None
    all_humidity = [o.humidity for o in rows if o.humidity is not None]
    return (
        f"朝:平均{statistics.mean(buckets[_MORNING]):.1f}%, "
        f"昼:平均{statistics.mean(buckets[_DAYTIME]):.1f}%, "
        f"夜:平均{statistics.mean(buckets[_EVENING]):.1f}%"
        f"（最高:{max(all_humidity)}%, 最低:{min(all_humidity)}%）"
    )


def _format_precipitation(by_hour: "Dict[int, HourlyObservation]") -> "Optional[str]":
    # 合計は 1 時間でも欠測があると過小になるので、集計対象の全時刻 (_FIRST_HOUR〜24 時) が
    # 値 (``--`` = 0) のときだけ出す.
    totals: "Dict[str, float]" = {name: 0.0 for name in _SEGMENTS}
    for hour in range(_FIRST_HOUR, 25):
        row = by_hour.get(hour)
        if row is None or row.precipitation is None:
            return None
        totals[_segment_of(hour)] += row.precipitation
    return (
        f"朝:{totals[_MORNING]:.1f}mm, "
        f"昼:{totals[_DAYTIME]:.1f}mm, "
        f"夜:{totals[_EVENING]:.1f}mm"
    )


def _format_sunshine(by_hour: "Dict[int, HourlyObservation]") -> "Optional[str]":
    rows = [by_hour[hour] for hour in sorted(by_hour)]
    if any(row.sunshine_missing for row in rows):
        return None
    numeric_hours = [row.hour for row in rows if row.sunshine is not None]
    if not numeric_hours:
        return None
    # 数値のある時間帯 (日の出〜日の入り) の内側の空欄は通信障害等の欠測.
    for hour in range(numeric_hours[0], numeric_hours[-1] + 1):
        row = by_hour.get(hour)
        if row is None or row.sunshine is None:
            return None
    # 全国どこでも日中の時間帯に数値が無ければ欠測 (端の時間帯の空欄は夜間と区別できないため).
    for hour in _SUNSHINE_CORE_HOURS:
        row = by_hour.get(hour)
        if row is None or row.sunshine is None:
            return None
    total = 0.0
    for row in rows:
        if row.sunshine is not None:
            total += row.sunshine
    return f"{total:.1f}時間"


def _format_pressure(rows: "Sequence[HourlyObservation]") -> "Optional[str]":
    spans: "List[Tuple[int, int, float, float, float, List[float]]]" = []
    for start in _PRESSURE_SPAN_STARTS:
        end = start + _PRESSURE_SPAN_HOURS
        values = [
            o.sea_level_pressure
            for o in rows
            if start <= o.hour < end and o.sea_level_pressure is not None
        ]
        if not values:
            return None  # span が全欠測 → キーごと欠測
        spans.append((start, end - 1, statistics.mean(values), min(values), max(values), values))

    parts: "List[str]" = []
    for index, (start, end, average, minimum, maximum, _values) in enumerate(spans):
        mark = ""
        if index + 1 < len(spans):
            next_values = spans[index + 1][5]
            if any(maximum - value >= _PRESSURE_CHANGE_THRESHOLD for value in next_values):
                mark = _PRESSURE_DROP_MARK
            elif any(value - minimum >= _PRESSURE_CHANGE_THRESHOLD for value in next_values):
                mark = _PRESSURE_RISE_MARK
        parts.append(f"{start}-{end}時:平均{average:.1f}hPa{mark}")
    return ", ".join(parts)


def _format_weather(rows: "Sequence[HourlyObservation]") -> "Optional[str]":
    items = [
        f"{row.hour}時: {row.weather}{_weather_emoji(row.weather)}"
        for row in rows
        if row.weather
    ]
    return ", ".join(items) if items else None


def _index_by_hour(obs: "Sequence[HourlyObservation]") -> "Dict[int, HourlyObservation]":
    """1〜24 時の行を 時刻 → 行 にする (範囲外は無視, 同じ時刻は最初の行)."""
    by_hour: "Dict[int, HourlyObservation]" = {}
    for row in obs:
        if 1 <= row.hour <= 24 and row.hour not in by_hour:
            by_hour[row.hour] = row
    return by_hour


def _incomplete_reasons(by_hour: "Dict[int, HourlyObservation]") -> "List[str]":
    """日が未完成である理由 (SPEC v2 §4.3). 空リストなら完成した日."""
    if not by_hour:
        return ["観測行なし"]
    reasons: "List[str]" = []
    last = by_hour.get(24)
    if last is None:
        reasons.append("24時の行なし")
    else:
        if last.temperature is None:
            reasons.append("24時の気温が欠測")
        if last.sea_level_pressure is None:
            reasons.append("24時の海面気圧が欠測")
    if not any(row.weather for row in by_hour.values()):
        reasons.append("天気 (画像の alt) が 0 件")
    return reasons


def summarize_weather(
    obs: "Sequence[HourlyObservation]",
    d: date,
    *,
    pending: bool = False,
) -> WeatherSummary:
    """時間ごとの観測値を前身と同一書式の 6 文字列にまとめる.

    - 日が未完成 (観測行が無い, 24 時の気温 / 海面気圧が無い, 天気が 1 件も無い) なら
      ``is_complete=False`` で全文字列 ``None``. ``pending=True`` (公開待ちの期間内,
      :func:`fetch_weather` が日付から判定する) なら ``status == "nodata"``、それ以外は
      ``"partial"`` (観測行が 0 件でも. :attr:`WeatherSummary.status` 参照).
    - 完成している日でも、キーの元データが区分単位で全欠測 (合計系は 1 時間でも欠測)
      ならそのキーだけ ``None``.
    - 1〜24 以外の時刻は無視し、同じ時刻が複数あれば最初の行を使う.
      集計には ``_FIRST_HOUR``〜24 時の行を使う.
    """
    by_hour = _index_by_hour(obs)
    if _incomplete_reasons(by_hour):
        return WeatherSummary(
            date=d,
            weather=None,
            temperature=None,
            humidity=None,
            precipitation=None,
            pressure=None,
            sunshine=None,
            is_complete=False,
            observed_hours=len(by_hour),
            pending=pending,
        )
    used = {hour: row for hour, row in by_hour.items() if hour >= _FIRST_HOUR}
    rows = [used[hour] for hour in sorted(used)]
    return WeatherSummary(
        date=d,
        weather=_format_weather(rows),
        temperature=_format_temperature(rows),
        humidity=_format_humidity(rows),
        precipitation=_format_precipitation(used),
        pressure=_format_pressure(rows),
        sunshine=_format_sunshine(used),
        is_complete=True,
        observed_hours=len(by_hour),
    )


# ---------------------------------------------------------------------------
# 取得
# ---------------------------------------------------------------------------


def _default_opener(request: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 — 固定 https URL
        body = response.read(_MAX_BODY_BYTES + 1)
    if len(body) > _MAX_BODY_BYTES:
        raise WeatherError(f"気象庁ページの応答が大きすぎます (> {_MAX_BODY_BYTES} bytes)")
    return body


def _is_text_encoding(name: str) -> bool:
    """``bytes.decode`` に使える文字コード名か.

    未知の名前に加え、``codecs.lookup`` は通るが ``bytes.decode`` できない bytes 用
    codec (base64 / zlib 等) も False. (空 bytes の decode は CPython が codec を引かずに
    ``""`` を返すので検査に使えない.)
    """
    try:
        info = codecs.lookup(name)
    except LookupError:
        return False
    return bool(getattr(info, "_is_text_encoding", True))


def _decode_html(body: bytes, log: logging.Logger) -> str:
    """meta charset (無ければ UTF-8) で decode. 不正 byte は置換して続行する."""
    encoding = "utf-8"
    match = _META_CHARSET_RE.search(body[:4096])
    if match is not None:
        candidate = match.group(1).decode("ascii", "ignore").lower()
        if _is_text_encoding(candidate):
            encoding = candidate
        else:
            log.warning("気象庁ページの charset %r は文字コードとして使えないため UTF-8 で読みます", candidate)
    try:
        return body.decode(encoding)
    except UnicodeDecodeError:
        log.warning("気象庁ページに %s として不正な byte があり、置換して読みます", encoding)
        return body.decode(encoding, errors="replace")


def _backoff_seconds(attempt: int) -> float:
    return min(_BACKOFF_BASE_SEC * (2 ** attempt), _BACKOFF_MAX_SEC)


def _jst_today(clock: "Callable[[], float]" = time.time) -> date:
    """気象庁の日付 (JST) での今日. 実行環境のタイムゾーン設定には依存しない."""
    return datetime.fromtimestamp(clock(), _JST).date()


def _download(
    url: str,
    *,
    timeout: float,
    retries: int,
    opener: Opener,
    log: logging.Logger,
    sleep: "Callable[[float], None]",
) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
        method="GET",
    )
    attempts = max(int(retries), 0) + 1
    for attempt in range(attempts):
        last_attempt = attempt == attempts - 1
        try:
            return opener(request, timeout)
        except urllib.error.HTTPError as exc:
            retryable = exc.code >= _RETRYABLE_STATUS_MIN or exc.code == 429
            if retryable and not last_attempt:
                wait = _backoff_seconds(attempt)
                log.warning(
                    "気象庁ページ取得 HTTP %s, %.1f 秒後に再試行 (%d/%d): %s",
                    exc.code, wait, attempt + 1, attempts - 1, url,
                )
                sleep(wait)
                continue
            raise WeatherError(f"気象庁ページの取得に失敗しました (HTTP {exc.code}): {url}") from exc
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            reason = getattr(exc, "reason", None) or exc
            if not last_attempt:
                wait = _backoff_seconds(attempt)
                log.warning(
                    "気象庁ページ取得失敗 (%s), %.1f 秒後に再試行 (%d/%d): %s",
                    reason, wait, attempt + 1, attempts - 1, url,
                )
                sleep(wait)
                continue
            raise WeatherError(f"気象庁ページの取得に失敗しました ({reason}): {url}") from exc
    raise WeatherError(f"気象庁ページの取得に失敗しました: {url}")  # pragma: no cover — 到達しない


def fetch_weather(
    d: date,
    *,
    prec_no: int = DEFAULT_PREC_NO,
    block_no: int = DEFAULT_BLOCK_NO,
    timeout: float = 20.0,
    retries: int = 2,
    opener: "Opener | None" = None,
    logger: "logging.Logger | None" = None,
    sleep: "Callable[[float], None]" = time.sleep,
    today: "date | None" = None,
) -> WeatherSummary:
    """気象庁から ``d`` の 1 時間ごとの値を取得して :class:`WeatherSummary` にする.

    - 5xx / 429 / 通信エラー / タイムアウトは ``retries`` 回まで指数 backoff
      (1, 2, 4 … 秒, 上限 30 秒) で再試行. 4xx はすぐ失敗.
    - 当日・未公開日 (表が無く「閲覧可能な日は…」の案内がある) は
      ``is_complete=False`` の空サマリを返す (例外にしない). 公開待ちの期間内なら
      ``status == "nodata"`` (info ログ)、それより古い日なのに案内が返ったときは
      ``status == "partial"`` (warning ログ. 気象庁側の障害や観測所番号の誤りを
      「データなし」と取り違えない).
    - 表はあるが日が未完成のとき: ``today − d`` が ``_PUBLICATION_GRACE_DAYS`` 日以内なら
      公開待ち (``pending=True``, ``status == "nodata"``, info ログ). それより古い日は
      ``status == "partial"`` で warning ログ (気象庁の欠測か、天気欄の形式などページ仕様の
      変更の可能性. 統合側は続くようなら利用者に知らせる).
    - 表も案内も無いページ (メンテナンス / 仕様変更), 見出しや時刻列の不一致, ページ日付の
      不一致, 取得失敗は :class:`WeatherError`.

    Args:
        d: 対象日 (観測所の日付 = JST).
        prec_no / block_no: 気象庁の府県番号 / 地点番号 (既定は東京).
        timeout: 1 回の HTTP 要求のタイムアウト秒.
        retries: 失敗時の再試行回数 (合計試行回数は ``retries + 1``).
        opener: ``(Request, timeout) -> bytes``. テストでモックを注入する.
        logger: 省略時は ``ihealth.weather``.
        sleep: backoff 待ち関数 (テスト用に差し替え可能).
        today: 公開待ち判定に使う今日の日付 (JST). 省略時は現在時刻から求める.
    """
    log = logger or logging.getLogger(__name__)
    reference = today if today is not None else _jst_today()
    pending = (reference - d).days <= _PUBLICATION_GRACE_DAYS
    url = jma_hourly_url(d, prec_no, block_no)
    log.debug("気象庁 hourly 取得: %s", url)
    body = _download(
        url,
        timeout=timeout,
        retries=retries,
        opener=opener or _default_opener,
        log=log,
        sleep=sleep,
    )
    html = _decode_html(body, log)
    page = _parse_page(html)

    if not page.table_found:
        if _UNAVAILABLE_MARKER in html:
            if pending:
                log.info("気象庁 %s のページはまだ公開されていません (閲覧可能なのは前日まで)", d.isoformat())
            else:
                log.warning(
                    "気象庁 %s: 公開済みのはずの日なのに「閲覧可能な日は…」の案内が返りました "
                    "(気象庁側の障害 / 観測所番号の誤りの可能性): %s",
                    d.isoformat(), url,
                )
            return summarize_weather([], d, pending=pending)
        raise WeatherError(
            f"気象庁ページに観測表も未公開の案内もありません (メンテナンス / 仕様変更の可能性): {url}"
        )
    if page.page_date is not None and page.page_date != d:
        raise WeatherError(
            f"気象庁ページの日付 {page.page_date.isoformat()} が要求日 {d.isoformat()} と一致しません: {url}"
        )

    summary = summarize_weather(page.observations, d, pending=pending)
    if summary.is_complete:
        log.info(
            "気象庁 %s: %d 時間分, 欠測キー=%s",
            d.isoformat(), summary.observed_hours, ",".join(summary.missing_keys) or "なし",
        )
        return summary
    reasons = "、".join(_incomplete_reasons(_index_by_hour(page.observations)))
    if summary.status == "nodata":
        log.info(
            "気象庁 %s: まだ確定していないため書き込み対象外 (%s, %d 時間分). 次回以降の実行で埋まる見込み",
            d.isoformat(), reasons, summary.observed_hours,
        )
    else:
        log.warning(
            "気象庁 %s: 確定済みのはずの日が未完成のため書き込み対象外 (%s, %d 時間分). "
            "気象庁の欠測か、ページ仕様の変更 (天気欄の形式など) の可能性: %s",
            d.isoformat(), reasons, summary.observed_hours, url,
        )
    return summary
