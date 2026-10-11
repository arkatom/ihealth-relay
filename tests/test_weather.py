"""weather.py (気象庁 hourly_s1 の取得・HTML パース・前身互換の集計) のテスト.

- fixture は **合成 HTML** (``tests/fixtures/jma/``, および本ファイルの ``_page``)。
  実ページの構造 (見出し 2 段 + rowspan/colspan, 後続の「記事」表, ナビゲーション画像)
  を模した架空値で、実観測値は含まない。
- 前身 GoogleFitNotionIntegration ``src/weather/weather_notion.py`` の ``get_weather_data``
  を **逐語転写** した参照実装 :func:`_legacy_get_weather_data` (BeautifulSoup だけ最小限の
  代役 :class:`_Bs4Soup` に置換, 見出しスキップ ``if i > 2`` の off-by-one もそのまま) と
  「同じ入力で同じ出力」になることを固定ケース + 乱数ケースで検証する:
  ``_FIRST_HOUR = 2`` (前身と同じく 1 時の行を集計しない) なら完全一致、既定
  (``_FIRST_HOUR = 1``) は前身の式で 1〜24 時を全て読ませた結果と一致 (違いは 1 時の行だけ)。
- ネットワークには出ない (opener 注入 / ``urllib.request.urlopen`` をモック)。
  公開待ち判定の「今日」も注入し、実行時の時計に依存しない。
"""

from __future__ import annotations

import dataclasses
import http.client
import logging
import random
import re
import socket
import statistics
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from unittest import mock

from ihealth import __version__
from ihealth import weather
from ihealth.weather import (
    DEFAULT_BLOCK_NO,
    DEFAULT_PREC_NO,
    WEATHER_FIELDS,
    HourlyObservation,
    WeatherError,
    WeatherSummary,
    fetch_weather,
    jma_hourly_url,
    parse_jma_hourly,
    summarize_weather,
)


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "jma"
FIXTURE_DATE = date(2031, 2, 14)

VS16 = "\N{VARIATION SELECTOR-16}"
SUN2 = "\N{BLACK SUN WITH RAYS}" + VS16 + "\N{BLACK SUN WITH RAYS}" + VS16  # ☀️☀️
SUN = "\N{BLACK SUN WITH RAYS}" + VS16  # ☀️
PARTLY = "\N{SUN BEHIND CLOUD}" + VS16  # ⛅️
CLOUD = "\N{CLOUD}" + VS16  # ☁️
UMBRELLA = "\N{UMBRELLA WITH RAIN DROPS}"  # ☔ (異体字セレクタなし)
SNOW = "\N{SNOWFLAKE}" + VS16  # ❄️
DROP = "\N{ARROW POINTING RIGHTWARDS THEN CURVING DOWNWARDS}" + VS16 + "\N{BOMB}" + VS16  # ⤵️💣️
RISE = "\N{ARROW POINTING RIGHTWARDS THEN CURVING UPWARDS}" + VS16 + "\N{WARNING SIGN}" + VS16  # ⤴️⚠️


# ---------------------------------------------------------------------------
# 合成ページ builder (実ページと同じ 17 列 / 見出し 2 段)
# ---------------------------------------------------------------------------

_HEADER_ROWS = (
    '<tr class="mtx"><th rowspan="2" scope="col">時</th><th colspan="2" scope="colgroup">気圧(hPa)</th>'
    '<th rowspan="2" scope="col">降水量<br />(mm)</th><th rowspan="2" scope="col">気温<br />(℃)</th>'
    '<th rowspan="2" scope="col">露点<br />温度<br />(℃)</th><th rowspan="2" scope="col">蒸気圧<br />(hPa)</th>'
    '<th rowspan="2" scope="col">湿度<br />(％)</th><th colspan="2" scope="colgroup">風向・風速(m/s)</th>'
    '<th rowspan="2" scope="col">日照<br />時間<br />(h)</th><th rowspan="2" scope="col">全天<br />日射量<br />(MJ/㎡)</th>'
    '<th colspan="2" scope="colgroup">雪(cm)</th><th rowspan="2" scope="col">天気</th>'
    '<th rowspan="2" scope="col">雲量</th><th rowspan="2" scope="col">視程<br />(km)</th></tr>\n'
    '<tr class="mtx"><th scope="col">現地</th><th scope="col">海面</th><th scope="col">風速</th>'
    '<th scope="col">風向</th><th scope="col">降雪</th><th scope="col">積雪</th></tr>\n'
)

_COLUMN_ORDER = (
    "hour", "local", "slp", "precip", "temp", "dew", "vapor", "hum", "wind", "dir",
    "sun", "rad", "snowfall", "snowdepth", "alt", "cloud", "vis",
)
_ALT_HOURS = (3, 6, 9, 12, 15, 18, 21)

RowSpec = Dict[str, Optional[str]]
DaySpec = List[Tuple[int, RowSpec]]


def _default_row(hour: int) -> RowSpec:
    return {
        "local": "1007.0",
        "slp": "1010.0",
        "precip": "--",
        "temp": "10.0",
        "dew": "5.0",
        "vapor": "8.0",
        "hum": "60",
        "wind": "2.0",
        "dir": "北",
        "sun": "0.5" if 7 <= hour <= 17 else "",
        "rad": "",
        "snowfall": "×",
        "snowdepth": "×",
        "alt": "晴れ" if hour in _ALT_HOURS else None,
        "cloud": "",
        "vis": "",
    }


def _day(
    overrides: "Optional[Dict[int, RowSpec]]" = None,
    *,
    hours: Iterable[int] = range(1, 25),
) -> DaySpec:
    rows: DaySpec = []
    for hour in hours:
        spec = _default_row(hour)
        spec.update((overrides or {}).get(hour, {}))
        rows.append((hour, spec))
    return rows


def _row_html(hour: int, spec: RowSpec) -> str:
    cells: "List[str]" = []
    for name in _COLUMN_ORDER:
        if name == "hour":
            cells.append(f'<td style="white-space:nowrap">{hour}</td>')
        elif name == "alt":
            alt = spec["alt"]
            img = f'<img src="../../data/image/tenki/large/F8A0.gif" alt="{alt}">' if alt is not None else ""
            cells.append(f'<td class="data_0_0">{img}</td>')
        else:
            cells.append(f'<td class="data_0_0">{spec[name]}</td>')
    return '<tr class="mtx" style="text-align:right;">' + "".join(cells) + "</tr>"


def _page(
    rows: DaySpec,
    *,
    heading: str = "東京（東京都)　2031年2月14日　（１時間ごとの値）",
    header: str = _HEADER_ROWS,
    head_meta: str = '<meta charset="UTF-8">',
) -> str:
    body = "\n".join(_row_html(hour, spec) for hour, spec in rows)
    return (
        "<!doctype html>\n<html lang=\"ja\">\n<head>\n" + head_meta + "\n"
        "<title>気象庁｜過去の気象データ検索</title>\n</head>\n<body>\n"
        '<div class="print"><table style="float:left"><tr><td>'
        '<img src="../../data/icon/sellected/table.gif" alt="一覧表"></td>'
        '<td><img src="../../data/icon/false/day2.gif" alt="翌日"></td></tr></table></div>'
        f"<h3 style='padding:0px;margin-bottom:0px'>{heading}</h3>\n"
        "<table id='tablefix1' class='data2_s'>\n" + header + body + "\n</table>\n"
        "<h3 style='padding:0px;margin-bottom:0px'>記事</h3>\n"
        '<table class="data2_s" id="kiji"><tr><td>－－<br /><br /></td></tr></table>\n'
        "</body>\n</html>\n"
    )


def _summarize_spec(rows: DaySpec, d: date = FIXTURE_DATE) -> WeatherSummary:
    return summarize_weather(parse_jma_hourly(_page(rows)), d)


# ---------------------------------------------------------------------------
# 前身 weather_notion.get_weather_data の逐語転写 (参照実装)
# ---------------------------------------------------------------------------

_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr",
})


class _Bs4Tag:
    """BeautifulSoup の ``Tag`` の代役 (前身が使う ``find`` / ``find_all`` / ``text`` /
    ``has_attr`` / ``get`` だけ). weather.py のパーサとは独立に実装し、bs4 と同じく
    ``find_all`` は子孫を再帰的に探し ``text`` は子孫のテキストを連結する. 空要素でも真."""

    def __init__(self, name: str, attrs: "Sequence[Tuple[str, Optional[str]]]") -> None:
        self.name = name
        self.attrs: "Dict[str, Optional[str]]" = dict(attrs)
        self.children: "List[object]" = []

    def _descendants(self) -> "Iterable[_Bs4Tag]":
        for child in self.children:
            if isinstance(child, _Bs4Tag):
                yield child
                yield from child._descendants()

    def find_all(self, name: str, class_: "Optional[str]" = None) -> "List[_Bs4Tag]":
        return [
            tag
            for tag in self._descendants()
            if tag.name == name and (class_ is None or class_ in (tag.attrs.get("class") or "").split())
        ]

    def find(self, name: str, class_: "Optional[str]" = None) -> "Optional[_Bs4Tag]":
        found = self.find_all(name, class_)
        return found[0] if found else None

    @property
    def text(self) -> str:
        return "".join(child if isinstance(child, str) else child.text for child in self.children)  # type: ignore[attr-defined]

    def has_attr(self, key: str) -> bool:
        return key in self.attrs

    def get(self, key: str) -> "Optional[str]":
        return self.attrs.get(key)


class _Bs4Soup(HTMLParser):
    """``BeautifulSoup(html, 'html.parser')`` の代役 (要素の木を組み立てるだけ)."""

    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Bs4Tag("[document]", [])
        self._open: "List[_Bs4Tag]" = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag: str, attrs: "List[Tuple[str, Optional[str]]]") -> None:
        node = _Bs4Tag(tag, attrs)
        self._open[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self._open.append(node)

    def handle_startendtag(self, tag: str, attrs: "List[Tuple[str, Optional[str]]]") -> None:
        self._open[-1].children.append(_Bs4Tag(tag, attrs))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._open) - 1, 0, -1):
            if self._open[index].name == tag:
                del self._open[index:]
                return

    def handle_data(self, data: str) -> None:
        self._open[-1].children.append(data)

    def find(self, name: str, class_: "Optional[str]" = None) -> "Optional[_Bs4Tag]":
        return self.root.find(name, class_)


def _legacy_get_weather_emoji(condition):  # type: ignore[no-untyped-def]
    """前身 ``get_weather_emoji`` (weather_notion.py 27〜42 行目) の逐語転写."""
    condition = condition.lower()
    if "快晴" in condition:
        return "☀️☀️"
    elif "晴" in condition:
        return "☀️"
    elif "薄曇" in condition:
        return "⛅️"
    elif "曇" in condition:
        return "☁️"
    elif "雨" in condition:
        return "☔"
    elif "雪" in condition:
        return "❄️"
    return ""


def _legacy_get_weather_data(html: str) -> "Dict[str, object]":
    """前身 ``get_weather_data`` (weather_notion.py 44〜205 行目) の逐語転写.

    置き換えたのは ``requests.get`` (→ 引数 ``html``) と ``BeautifulSoup`` (→ :class:`_Bs4Soup`),
    使わない ``"日付"`` キーだけ. 見出しスキップ ``if i > 2`` (見出し 2 行に加えて 1 時の
    行も読み捨てる off-by-one) を含め、それ以外は一字一句そのまま.
    """
    soup = _Bs4Soup(html)
    table = soup.find('table', class_='data2_s')
    if table is None:
        return {
            "天気": "", "気温": "", "湿度": "", "降水量": "", "気圧": "", "日照時間": "",
            "_is_complete": False,
        }
    rows = table.find_all('tr')

    # 結果格納用の変数を初期化
    weather_info = []
    total_sunshine = 0
    humidity_data = []
    sea_level_pressures = []
    temperature_data = []
    precipitation_data = []

    for i, row in enumerate(rows):
        if i > 2:  # ヘッダー行をスキップ
            cells = row.find_all('td')
            if len(cells) >= 15:
                hour = int(cells[0].text.strip())

                # 海面気圧（インデックス2）
                sea_pressure = cells[2].text.strip()
                if sea_pressure != "--" and sea_pressure != "":
                    sea_level_pressures.append((hour, float(sea_pressure)))

                # 降水量（インデックス3）
                precipitation = cells[3].text.strip()
                if precipitation != "--" and precipitation != "":
                    try:
                        precipitation_data.append((hour, float(precipitation)))
                    except ValueError:
                        precipitation_data.append((hour, 0.0))

                # 気温（インデックス4）
                temperature = cells[4].text.strip()
                if temperature != "--" and temperature != "":
                    temperature_data.append((hour, float(temperature)))

                # 湿度（インデックス7）
                humidity = cells[7].text.strip()
                if humidity != "--" and humidity != "":
                    humidity_data.append((hour, int(humidity)))

                # 日照時間（インデックス10）
                sunshine = cells[10].text.strip()
                if sunshine != "--" and sunshine != "":
                    try:
                        total_sunshine += float(sunshine)
                    except ValueError:
                        # 数値に変換できない場合はスキップ
                        pass

                # 天気（インデックス14の画像のalt属性）
                weather_img = cells[14].find('img')
                if weather_img and weather_img.has_attr('alt'):
                    weather_condition = weather_img.get('alt')
                    emoji = _legacy_get_weather_emoji(weather_condition)
                    weather_info.append(f"{hour}時: {weather_condition}{emoji}")

    # 気圧データを6時間スパンで処理（大気潮に合わせる）
    pressure_spans = []

    # 6時間ごとのスパンの気圧データを収集
    for i in range(1, 25, 6):
        span_pressures = [p[1] for p in sea_level_pressures if i <= p[0] < i+6]
        if span_pressures:
            avg_pressure = statistics.mean(span_pressures)
            min_pressure = min(span_pressures)
            max_pressure = max(span_pressures)
            pressure_spans.append((i, i+5, avg_pressure, min_pressure, max_pressure, span_pressures))

    # 気圧変化のマークを追加
    for i in range(len(pressure_spans)-1):
        current_span = pressure_spans[i]
        next_span = pressure_spans[i+1]

        change_mark = ""

        # 下降判定: 現在のスパンの最大値と次のスパンのすべての値を比較
        if any(current_span[4] - next_value >= 5 for next_value in next_span[5]):
            change_mark = "⤵️💣️"
        # 上昇判定: 現在のスパンの最小値と次のスパンのすべての値を比較
        elif any(next_value - current_span[3] >= 5 for next_value in next_span[5]):
            change_mark = "⤴️⚠️"

        # マークを追加（元のタプルの要素を保持し、マークを最後に追加）
        pressure_spans[i] = (*current_span[:5], change_mark)

    # 最後のスパンの変化マークは空
    if pressure_spans:
        pressure_spans[-1] = (*pressure_spans[-1][:5], "")

    # 気温データを処理
    morning_temps = [t[1] for t in temperature_data if 5 <= t[0] <= 11]
    daytime_temps = [t[1] for t in temperature_data if 12 <= t[0] <= 18]
    evening_temps = [t[1] for t in temperature_data if t[0] >= 19 or t[0] <= 4]

    all_temps = [t[1] for t in temperature_data]
    max_temp = max(all_temps) if all_temps else 0
    min_temp = min(all_temps) if all_temps else 0

    morning_avg = statistics.mean(morning_temps) if morning_temps else 0
    daytime_avg = statistics.mean(daytime_temps) if daytime_temps else 0
    evening_avg = statistics.mean(evening_temps) if evening_temps else 0

    # 湿度データを処理
    morning_humidity = [h[1] for h in humidity_data if 5 <= h[0] <= 11]
    daytime_humidity = [h[1] for h in humidity_data if 12 <= h[0] <= 18]
    evening_humidity = [h[1] for h in humidity_data if h[0] >= 19 or h[0] <= 4]

    all_humidity = [h[1] for h in humidity_data]
    max_humidity = max(all_humidity) if all_humidity else 0
    min_humidity = min(all_humidity) if all_humidity else 0

    morning_hum_avg = statistics.mean(morning_humidity) if morning_humidity else 0
    daytime_hum_avg = statistics.mean(daytime_humidity) if daytime_humidity else 0
    evening_hum_avg = statistics.mean(evening_humidity) if evening_humidity else 0

    # 降水量データを処理
    morning_precip = sum([p[1] for p in precipitation_data if 5 <= p[0] <= 11])
    daytime_precip = sum([p[1] for p in precipitation_data if 12 <= p[0] <= 18])
    evening_precip = sum([p[1] for p in precipitation_data if p[0] >= 19 or p[0] <= 4])

    # 気圧情報を文字列化（6時間スパン版）
    pressure_str = ", ".join([f"{s[0]}-{s[1]}時:平均{s[2]:.1f}hPa{s[5]}" for s in pressure_spans])

    # 気温情報を文字列化
    temp_str = f"朝:平均{morning_avg:.1f}℃, 昼:平均{daytime_avg:.1f}℃, 夜:平均{evening_avg:.1f}℃（最高:{max_temp:.1f}℃, 最低:{min_temp:.1f}℃）"

    # 湿度情報を文字列化
    humidity_str = f"朝:平均{morning_hum_avg:.1f}%, 昼:平均{daytime_hum_avg:.1f}%, 夜:平均{evening_hum_avg:.1f}%（最高:{max_humidity}%, 最低:{min_humidity}%）"

    # 降水量情報を文字列化
    precip_str = f"朝:{morning_precip:.1f}mm, 昼:{daytime_precip:.1f}mm, 夜:{evening_precip:.1f}mm"

    # 天気情報を文字列化
    weather_str = ", ".join(weather_info)

    result = {
        "天気": weather_str,
        "気温": temp_str,
        "湿度": humidity_str,
        "降水量": precip_str,
        "気圧": pressure_str,
        "日照時間": f"{total_sunshine:.1f}時間"
    }

    # データの完全性を示すフラグを追加
    # 天気情報（imgのalt属性）が空の場合はデータ未公開と判断
    result["_is_complete"] = len(weather_info) > 0

    return result


#: 前身の出力キー → :class:`WeatherSummary` のフィールド名.
_LEGACY_KEYS: "Dict[str, str]" = {
    "weather": "天気",
    "temperature": "気温",
    "humidity": "湿度",
    "precipitation": "降水量",
    "pressure": "気圧",
    "sunshine": "日照時間",
}

#: 前身の ``if i > 2`` は見出し 2 行に加えて 3 行目 (= 1 時) も読み捨てる. 見出しの直後に
#: 空の行を 1 つ挟んだページを読ませると、前身の式のまま 1〜24 時を全て集計させられる.
_SACRIFICIAL_ROW = '<tr class="mtx"><th scope="col"></th></tr>\n'


def _legacy_fields(html: str) -> "Dict[str, object]":
    """前身の出力を :class:`WeatherSummary` のフィールド名で (``_is_complete`` も含む)."""
    result = _legacy_get_weather_data(html)
    fields: "Dict[str, object]" = {name: result[key] for name, key in _LEGACY_KEYS.items()}
    fields["_is_complete"] = result["_is_complete"]
    return fields


def _legacy_verbatim(rows: DaySpec) -> "Dict[str, object]":
    """前身そのもの (1 時の行を読み捨てる)."""
    return _legacy_fields(_page(rows))


def _legacy_all_hours(rows: DaySpec) -> "Dict[str, object]":
    """前身の式で 1〜24 時を全て読ませた結果 (入力側で off-by-one を打ち消す)."""
    return _legacy_fields(_page(rows, header=_HEADER_ROWS + _SACRIFICIAL_ROW))


_CONDITIONS = ("快晴", "晴れ", "薄曇", "曇", "雨", "雪", "みぞれ", "霧雨", "雷電", "霧", "あられ", "しゅう雨または止み間のある雨")


def _random_day(rng: random.Random) -> DaySpec:
    """前身でも欠測にならない (全値あり) ランダムな 1 日. 気圧は 5hPa 境界を踏みやすい刻み."""
    slp = round(rng.uniform(985.0, 1035.0), 1)
    temp = round(rng.uniform(-8.0, 32.0), 1)
    sunrise = rng.randint(5, 7)
    sunset = rng.randint(17, 19)
    overrides: "Dict[int, RowSpec]" = {}
    for hour in range(1, 25):
        slp = round(slp + rng.choice((-5.0, -2.5, -1.3, -0.6, -0.1, 0.0, 0.2, 0.7, 1.4, 2.5, 5.0)), 1)
        temp = round(temp + rng.uniform(-2.0, 2.0), 1)
        alt: "Optional[str]" = None
        if hour in _ALT_HOURS and rng.random() < 0.9:
            alt = rng.choice(_CONDITIONS)
        overrides[hour] = {
            "slp": f"{slp:.1f}",
            "temp": f"{temp:.1f}",
            "hum": str(rng.randint(8, 100)),
            "precip": "--" if rng.random() < 0.6 else f"{rng.randint(0, 40) * 0.5:.1f}",
            "sun": f"{rng.randint(0, 10) / 10:.1f}" if sunrise <= hour <= sunset else "",
            "alt": alt,
        }
    overrides[12]["alt"] = rng.choice(_CONDITIONS)  # 天気 1 件以上 (完全な日)
    return _day(overrides)


def _summary_dict(summary: WeatherSummary) -> "Dict[str, Optional[str]]":
    return {name: getattr(summary, name) for name in WEATHER_FIELDS}


# ---------------------------------------------------------------------------
# URL
# ---------------------------------------------------------------------------


class JmaHourlyUrlTest(unittest.TestCase):
    def test_default_station_is_tokyo_with_zero_padded_date(self) -> None:
        self.assertEqual((DEFAULT_PREC_NO, DEFAULT_BLOCK_NO), (44, 47662))
        self.assertEqual(
            jma_hourly_url(date(2026, 7, 5)),
            "https://www.data.jma.go.jp/stats/etrn/view/hourly_s1.php"
            "?prec_no=44&block_no=47662&year=2026&month=07&day=05&view=p1",
        )

    def test_custom_station(self) -> None:
        url = jma_hourly_url(date(2026, 10, 10), 62, 47772)
        self.assertIn("prec_no=62&block_no=47772&year=2026&month=10&day=10&view=p1", url)
        # .env 由来の文字列でも受け付ける
        self.assertEqual(url, jma_hourly_url(date(2026, 10, 10), "62", "47772"))  # type: ignore[arg-type]

    def test_invalid_station_is_rejected(self) -> None:
        for bad in (0, -1, "abc", None, True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    jma_hourly_url(date(2026, 10, 10), bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# パース (合成 fixture = 実ページ構造)
# ---------------------------------------------------------------------------


class ParseSyntheticFixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (FIXTURES / "hourly_synthetic.html").read_text(encoding="utf-8")
        self.obs = parse_jma_hourly(self.html)
        self.by_hour = {o.hour: o for o in self.obs}

    def test_reads_all_24_hours_in_order(self) -> None:
        self.assertEqual([o.hour for o in self.obs], list(range(1, 25)))

    def test_cell_values(self) -> None:
        first = self.by_hour[1]
        self.assertEqual(first.sea_level_pressure, 1012.0)  # 現地気圧 (1009.0) ではなく海面気圧
        self.assertEqual(first.temperature, 3.5)
        self.assertEqual(first.humidity, 70)
        self.assertIsInstance(first.humidity, int)
        self.assertEqual(first.precipitation, 0.0)          # "--" = 現象なし
        self.assertIsNone(first.sunshine)                   # 夜間の空欄
        self.assertFalse(first.sunshine_missing)
        self.assertIsNone(first.weather)
        self.assertEqual(self.by_hour[11].precipitation, 1.0)
        self.assertEqual(self.by_hour[16].sunshine, 0.1)
        self.assertEqual(self.by_hour[3].weather, "快晴")
        self.assertEqual(self.by_hour[21].weather, "雷電")

    def test_ignores_images_and_tables_outside_the_observation_table(self) -> None:
        alts = [o.weather for o in self.obs if o.weather]
        self.assertEqual(alts, ["快晴", "晴れ", "薄曇", "曇", "雨", "雪", "雷電"])

    def test_page_date_comes_from_heading(self) -> None:
        self.assertEqual(weather._parse_page(self.html).page_date, FIXTURE_DATE)

    def test_summary_exact_strings(self) -> None:
        """手計算した期待値 (書式・絵文字の code point まで固定)."""
        summary = summarize_weather(self.obs, FIXTURE_DATE)
        self.assertTrue(summary.is_complete)
        self.assertEqual(summary.status, "complete")
        self.assertEqual(summary.observed_hours, 24)
        self.assertEqual(
            summary.temperature,
            "朝:平均4.4℃, 昼:平均6.5℃, 夜:平均2.8℃（最高:8.3℃, 最低:2.0℃）",
        )
        self.assertEqual(
            summary.humidity,
            "朝:平均69.3%, 昼:平均77.3%, 夜:平均85.3%（最高:95%, 最低:55%）",
        )
        self.assertEqual(
            summary.pressure,
            f"1-6時:平均1012.8hPa, 7-12時:平均1012.9hPa{DROP}, "
            f"13-18時:平均1008.3hPa{RISE}, 19-24時:平均1012.6hPa",
        )
        self.assertEqual(
            summary.weather,
            f"3時: 快晴{SUN2}, 6時: 晴れ{SUN}, 9時: 薄曇{PARTLY}, 12時: 曇{CLOUD}, "
            f"15時: 雨{UMBRELLA}, 18時: 雪{SNOW}, 21時: 雷電",
        )
        self.assertEqual(summary.sunshine, "3.1時間")
        self.assertEqual(summary.precipitation, "朝:1.5mm, 昼:7.5mm, 夜:0.0mm")

    def test_unavailable_page_has_no_table(self) -> None:
        html = (FIXTURES / "hourly_unavailable.html").read_text(encoding="utf-8")
        page = weather._parse_page(html)
        self.assertFalse(page.table_found)
        self.assertEqual(parse_jma_hourly(html), [])
        summary = summarize_weather([], date(2031, 2, 15), pending=True)
        self.assertFalse(summary.is_complete)
        self.assertEqual(summary.status, "nodata")
        # 公開待ちの期間を過ぎた日の 0 行は「データなし」ではなく未完成 (partial)
        self.assertEqual(summarize_weather([], date(2031, 2, 15)).status, "partial")


# ---------------------------------------------------------------------------
# 前身と同じ入力で同じ出力
# ---------------------------------------------------------------------------


class LegacyParityTest(unittest.TestCase):
    """前身の逐語転写 :func:`_legacy_get_weather_data` と同じ入力で同じ出力.

    - ``_FIRST_HOUR = 2`` (前身と同じく 1 時の行を集計しない) では前身と **完全一致**.
      = ``_FIRST_HOUR`` 1 つを変えるだけで前身互換に戻せることの証明.
    - 既定 (``_FIRST_HOUR = 1``) は、前身の式で 1〜24 時を全て読ませた結果と一致
      (= 前身との違いは 1 時の行だけ. 具体値は :class:`HourOneRowTest` で固定).
    """

    def assert_same_as_legacy(self, rows: DaySpec) -> WeatherSummary:
        verbatim = _legacy_verbatim(rows)
        self.assertIs(verbatim.pop("_is_complete"), True)
        with mock.patch.object(weather, "_FIRST_HOUR", 2):
            strict = _summarize_spec(rows)
        self.assertTrue(strict.is_complete)
        self.assertEqual(_summary_dict(strict), verbatim)

        all_hours = _legacy_all_hours(rows)
        self.assertIs(all_hours.pop("_is_complete"), True)
        summary = _summarize_spec(rows)
        self.assertTrue(summary.is_complete)
        self.assertEqual(_summary_dict(summary), all_hours)
        return summary

    def test_default_day(self) -> None:
        self.assert_same_as_legacy(_day())

    def test_random_days(self) -> None:
        rng = random.Random(20261011)
        marks = {"drop": 0, "rise": 0}
        for index in range(300):
            rows = _random_day(rng)
            with self.subTest(index=index):
                summary = self.assert_same_as_legacy(rows)
                marks["drop"] += (summary.pressure or "").count(DROP)
                marks["rise"] += (summary.pressure or "").count(RISE)
        # 気圧マークの両分岐が実際に検証されていること (vacuous green 防止)
        self.assertGreater(marks["drop"], 0)
        self.assertGreater(marks["rise"], 0)

    def _pressure_day(self, spans: "Sequence[Sequence[str]]") -> DaySpec:
        overrides: "Dict[int, RowSpec]" = {}
        for span_index, values in enumerate(spans):
            for offset, value in enumerate(values):
                overrides[1 + span_index * 6 + offset] = {"slp": value}
        return _day(overrides)

    def test_pressure_drop_exactly_5_is_marked(self) -> None:
        rows = self._pressure_day([
            ["1015.0"] * 6,
            ["1012.0", "1010.0", "1012.0", "1012.0", "1012.0", "1012.0"],
            ["1012.0"] * 6,
            ["1012.0"] * 6,
        ])
        summary = self.assert_same_as_legacy(rows)
        self.assertEqual(
            summary.pressure,
            f"1-6時:平均1015.0hPa{DROP}, 7-12時:平均1011.7hPa, 13-18時:平均1012.0hPa, 19-24時:平均1012.0hPa",
        )

    def test_pressure_change_4_9_is_not_marked(self) -> None:
        rows = self._pressure_day([
            ["1015.0"] * 6,
            ["1010.1"] * 6,
            ["1015.0"] * 6,
            ["1015.0"] * 6,
        ])
        summary = self.assert_same_as_legacy(rows)
        self.assertNotIn(DROP, summary.pressure or "")
        self.assertNotIn(RISE, summary.pressure or "")

    def test_pressure_rise_exactly_5_is_marked(self) -> None:
        rows = self._pressure_day([
            ["1000.0"] * 6,
            ["1003.0", "1005.0", "1003.0", "1003.0", "1003.0", "1003.0"],
            ["1003.0"] * 6,
            ["1003.0"] * 6,
        ])
        summary = self.assert_same_as_legacy(rows)
        self.assertEqual(
            summary.pressure,
            f"1-6時:平均1000.0hPa{RISE}, 7-12時:平均1003.3hPa, 13-18時:平均1003.0hPa, 19-24時:平均1003.0hPa",
        )

    def test_drop_has_priority_over_rise(self) -> None:
        # span1: max 1016 / min 1004, span2 に 1011 → 下降 5 (1016-1011) と 上昇 7 (1011-1004) が同時成立
        rows = self._pressure_day([
            ["1016.0", "1004.0", "1010.0", "1010.0", "1010.0", "1010.0"],
            ["1011.0"] * 6,
            ["1011.0"] * 6,
            ["1011.0"] * 6,
        ])
        summary = self.assert_same_as_legacy(rows)
        self.assertTrue((summary.pressure or "").startswith(f"1-6時:平均1010.0hPa{DROP}, "))

    def test_last_span_is_never_marked(self) -> None:
        rows = self._pressure_day([
            ["1010.0"] * 6,
            ["1010.0"] * 6,
            ["1010.0"] * 6,
            ["990.0"] * 6,
        ])
        summary = self.assert_same_as_legacy(rows)
        self.assertEqual(
            summary.pressure,
            f"1-6時:平均1010.0hPa, 7-12時:平均1010.0hPa, 13-18時:平均1010.0hPa{DROP}, 19-24時:平均990.0hPa",
        )

    def test_pressure_boundary_uses_same_float_arithmetic_as_legacy(self) -> None:
        # 1024 を跨ぐと仮数の刻みが変わり、10 進で 5.0 ちょうどでも float 差は 5.0 にならないことがある.
        # 前身と同じ float 演算 (``>= 5``) で判定していることを確認する.
        for high, low in (("1024.4", "1019.4"), ("1024.3", "1019.3"), ("1030.7", "1025.7"), ("1023.9", "1018.9")):
            with self.subTest(high=high, low=low):
                rows = self._pressure_day([[high] * 6, [low] * 6, [low] * 6, [low] * 6])
                summary = self.assert_same_as_legacy(rows)
                expected_mark = (float(high) - float(low)) >= 5
                self.assertEqual(DROP in (summary.pressure or ""), expected_mark)

    def test_mean_is_correctly_rounded_like_legacy(self) -> None:
        # 7 時間そろった朝 / 昼の平均は 10 進で x.x5 ちょうどにならないが、1 時間欠けて 6 個に
        # なると起こりうる. 前身と同じ statistics.mean (正しく丸めた平均) でないと 1 桁目がずれる.
        # 空欄は前身も読み飛ばすので、逐語転写とそのまま突き合わせられる
        rows = _day({5: {"temp": ""}, 11: {"temp": "10.3"}, 12: {"temp": ""}, 18: {"temp": "10.3"}})
        summary = self.assert_same_as_legacy(rows)
        self.assertTrue((summary.temperature or "").startswith("朝:平均10.1℃, 昼:平均10.1℃, "))
        naive = [10.0] * 5 + [10.3]
        self.assertEqual(f"{sum(naive) / len(naive):.1f}", "10.0")  # 素朴な sum / len だと 10.0

    def test_humidity_extremes_are_integers(self) -> None:
        rows = _day({4: {"hum": "100"}, 14: {"hum": "9"}})
        summary = self.assert_same_as_legacy(rows)
        self.assertTrue((summary.humidity or "").endswith("（最高:100%, 最低:9%）"))

    def test_emoji_code_points_are_pinned(self) -> None:
        """過去ノートと同じ code point 列 (異体字セレクタ U+FE0F の有無まで) を数値で固定する."""
        expected = {
            "快晴": [0x2600, 0xFE0F, 0x2600, 0xFE0F],
            "晴れ": [0x2600, 0xFE0F],
            "薄曇": [0x26C5, 0xFE0F],
            "曇": [0x2601, 0xFE0F],
            "雨": [0x2614],
            "雪": [0x2744, 0xFE0F],
        }
        for condition, code_points in expected.items():
            with self.subTest(condition=condition):
                self.assertEqual([ord(c) for c in weather._weather_emoji(condition)], code_points)
        rows = self._pressure_day([["1015.0"] * 6, ["1010.0"] * 6, ["1015.0"] * 6, ["1015.0"] * 6])
        pressure = _summarize_spec(rows).pressure or ""
        drop = pressure[pressure.index("hPa") + 3:pressure.index(",")]
        rise = pressure.split(", ")[1].split("hPa", 1)[1]
        self.assertEqual([ord(c) for c in drop], [0x2935, 0xFE0F, 0x1F4A3, 0xFE0F])
        self.assertEqual([ord(c) for c in rise], [0x2934, 0xFE0F, 0x26A0, 0xFE0F])

    def test_weather_emoji_rules_match_legacy(self) -> None:
        expected = {
            "快晴": SUN2, "晴れ": SUN, "晴": SUN, "薄曇": PARTLY, "曇": CLOUD, "雨": UMBRELLA,
            "霧雨": UMBRELLA, "雪": SNOW, "みぞれ": "", "雷電": "", "霧": "", "あられ": "",
        }
        for condition, emoji in expected.items():
            with self.subTest(condition=condition):
                self.assertEqual(weather._weather_emoji(condition), emoji)
                self.assertEqual(weather._weather_emoji(condition), _legacy_get_weather_emoji(condition))


# ---------------------------------------------------------------------------
# 前身からの意図的な変更
# ---------------------------------------------------------------------------


class HourOneRowTest(unittest.TestCase):
    """前身は ``if i > 2`` で 1 時の行を読み捨てていた. 本実装は 1〜24 時を全て使う.

    SPEC §2-B「前身と完全一致」からの意図的な逸脱 (理由は weather.py の docstring).
    前身互換に戻すなら ``_FIRST_HOUR = 2`` にしてこのクラスの期待値を前身側に揃える.
    """

    ROWS = _day({1: {"temp": "30.0", "slp": "1040.0", "hum": "99", "precip": "2.0"}})

    def test_default_uses_hour_one(self) -> None:
        self.assertEqual(weather._FIRST_HOUR, 1)

    def test_predecessor_drops_hour_one(self) -> None:
        """前身の逐語転写は 1 時の行 (30℃ / 1040hPa / 99% / 2.0mm) を集計に入れない."""
        legacy = _legacy_verbatim(self.ROWS)
        self.assertEqual(
            legacy["temperature"],
            "朝:平均10.0℃, 昼:平均10.0℃, 夜:平均10.0℃（最高:10.0℃, 最低:10.0℃）",
        )
        self.assertEqual(
            legacy["humidity"],
            "朝:平均60.0%, 昼:平均60.0%, 夜:平均60.0%（最高:60%, 最低:60%）",
        )
        self.assertEqual(
            legacy["pressure"],
            "1-6時:平均1010.0hPa, 7-12時:平均1010.0hPa, 13-18時:平均1010.0hPa, 19-24時:平均1010.0hPa",
        )
        self.assertEqual(legacy["precipitation"], "朝:0.0mm, 昼:0.0mm, 夜:0.0mm")

    def test_first_hour_two_reproduces_predecessor(self) -> None:
        with mock.patch.object(weather, "_FIRST_HOUR", 2):
            summary = _summarize_spec(self.ROWS)
        expected = _legacy_verbatim(self.ROWS)
        self.assertIs(expected.pop("_is_complete"), True)
        self.assertEqual(_summary_dict(summary), expected)
        self.assertEqual(summary.observed_hours, 24)  # 1 時の行は読むが集計に使わないだけ
        self.assertEqual(summary.status, "complete")

    def test_hour_one_counts_for_night_first_span_and_extremes(self) -> None:
        rows = self.ROWS
        summary = _summarize_spec(rows)
        # 夜 = 1〜4 時 + 19〜24 時 の 10 時間: (30.0 + 9 × 10.0) / 10 = 12.0
        self.assertEqual(
            summary.temperature,
            "朝:平均10.0℃, 昼:平均10.0℃, 夜:平均12.0℃（最高:30.0℃, 最低:10.0℃）",
        )
        self.assertEqual(
            summary.humidity,
            "朝:平均60.0%, 昼:平均60.0%, 夜:平均63.9%（最高:99%, 最低:60%）",
        )
        # 1-6 時: (1040 + 5 × 1010) / 6 = 1015.0, 最大 1040 − 次 span 1010 ≥ 5 → 下降マーク
        self.assertEqual(
            summary.pressure,
            f"1-6時:平均1015.0hPa{DROP}, 7-12時:平均1010.0hPa, 13-18時:平均1010.0hPa, 19-24時:平均1010.0hPa",
        )
        self.assertEqual(summary.precipitation, "朝:0.0mm, 昼:0.0mm, 夜:2.0mm")


class QualityMarkTest(unittest.TestCase):
    def _single(self, **cells: str) -> HourlyObservation:
        rows = _day({5: dict(cells)})
        return {o.hour: o for o in parse_jma_hourly(_page(rows))}[5]

    def test_quasi_normal_value_is_used(self) -> None:
        obs = self._single(temp="12.3 )", slp="1012.5)", hum="77)", precip="1.5)", sun="0.4)")
        self.assertEqual(
            (obs.temperature, obs.sea_level_pressure, obs.humidity, obs.precipitation, obs.sunshine),
            (12.3, 1012.5, 77, 1.5, 0.4),
        )
        self.assertFalse(obs.sunshine_missing)

    def test_insufficient_questionable_and_missing_marks_are_missing(self) -> None:
        for mark in ("12.3]", "12.3#", "#", "×", "///", "abc"):
            with self.subTest(mark=mark):
                obs = self._single(temp=mark, slp=mark, hum=mark, precip=mark, sun=mark)
                self.assertIsNone(obs.temperature)
                self.assertIsNone(obs.sea_level_pressure)
                self.assertIsNone(obs.humidity)
                self.assertIsNone(obs.precipitation)
                self.assertIsNone(obs.sunshine)
                self.assertTrue(obs.sunshine_missing)

    def test_no_phenomenon_is_zero_for_amounts_only(self) -> None:
        obs = self._single(temp="--", precip="--", sun="--")
        self.assertIsNone(obs.temperature)
        self.assertEqual(obs.precipitation, 0.0)
        self.assertEqual(obs.sunshine, 0.0)
        self.assertFalse(obs.sunshine_missing)

    def test_quasi_normal_no_phenomenon_is_zero(self) -> None:
        # ``--)`` = 「該当現象なし」の準正常値. 記号を外して ``--`` と同じ扱い (0. 欠測ではない)
        for text in ("--)", "-- )", " --) "):
            with self.subTest(text=text):
                self.assertEqual(weather._amount(text), (0.0, False))
                self.assertIsNone(weather._measured(text))
        # 資料不足値 / 疑問値の ``--``, 記号だけ, 記号の重複は欠測
        for text in ("--]", "--#", ")", "]", "--))", "12.3))"):
            with self.subTest(text=text):
                self.assertEqual(weather._amount(text), (None, True))

    def test_quasi_normal_no_phenomenon_keeps_amount_keys(self) -> None:
        summary = _summarize_spec(_day({13: {"precip": "--)"}, 12: {"sun": "-- )"}}))
        self.assertEqual(summary.precipitation, "朝:0.0mm, 昼:0.0mm, 夜:0.0mm")
        self.assertEqual(summary.sunshine, "5.0時間")  # 12 時 = 0, 残り 7〜17 時の 10 時間 × 0.5
        self.assertEqual(summary.status, "complete")

    def test_blank_cells(self) -> None:
        obs = self._single(temp="", precip="", sun="&nbsp;")
        self.assertIsNone(obs.temperature)
        self.assertIsNone(obs.precipitation)
        self.assertIsNone(obs.sunshine)
        self.assertFalse(obs.sunshine_missing)  # 空欄は欠測記号ではない (夜間かどうかは集計側で判断)

    def test_negative_and_decimal_humidity(self) -> None:
        obs = self._single(temp="-3.4", hum="85.5")
        self.assertEqual(obs.temperature, -3.4)
        self.assertEqual(obs.humidity, 85.5)


class KeyLevelCompletenessTest(unittest.TestCase):
    """SPEC v2 §4.3: 区分が全欠測のキーは書かない (前身の 0℃ / 0% 埋めはしない)."""

    def test_temperature_segment_all_missing_drops_only_that_key(self) -> None:
        rows = _day({h: {"temp": "×"} for h in range(5, 12)})
        summary = _summarize_spec(rows)
        self.assertTrue(summary.is_complete)
        self.assertIsNone(summary.temperature)
        self.assertEqual(summary.missing_keys, ("temperature",))
        self.assertEqual(summary.status, "partial")
        for name in ("weather", "humidity", "precipitation", "pressure", "sunshine"):
            self.assertIsNotNone(getattr(summary, name), name)

    def test_temperature_partial_segment_missing_averages_the_rest(self) -> None:
        rows = _day({5: {"temp": "×"}, 6: {"temp": "16.0"}})
        summary = _summarize_spec(rows)
        # 朝 = 6〜11 時の 6 値: (16 + 5 × 10) / 6 = 11.0
        self.assertEqual(
            summary.temperature,
            "朝:平均11.0℃, 昼:平均10.0℃, 夜:平均10.0℃（最高:16.0℃, 最低:10.0℃）",
        )

    def test_humidity_segment_all_missing(self) -> None:
        rows = _day({h: {"hum": "///"} for h in (1, 2, 3, 4, 19, 20, 21, 22, 23, 24)})
        summary = _summarize_spec(rows)
        self.assertIsNone(summary.humidity)
        self.assertIsNotNone(summary.temperature)

    def test_pressure_span_all_missing(self) -> None:
        rows = _day({h: {"slp": "×"} for h in range(7, 13)})
        self.assertIsNone(_summarize_spec(rows).pressure)

    def test_precipitation_any_missing_hour_drops_key(self) -> None:
        for value in ("×", "", "2.5]", "///"):
            with self.subTest(value=value):
                rows = _day({13: {"precip": value}})
                summary = _summarize_spec(rows)
                self.assertIsNone(summary.precipitation)
                self.assertTrue(summary.is_complete)

    def test_sunshine_missing_mark_drops_key(self) -> None:
        self.assertIsNone(_summarize_spec(_day({10: {"sun": "×"}})).sunshine)

    def test_sunshine_blank_inside_daylight_drops_key(self) -> None:
        self.assertIsNone(_summarize_spec(_day({12: {"sun": ""}})).sunshine)

    def test_sunshine_blank_in_core_daylight_hours_drops_key(self) -> None:
        # 7〜9 時が空欄 (通信障害) → 数値は 10 時からで「内側」ではないが 8・9 時は全国どこでも日中
        rows = _day({h: {"sun": ""} for h in (7, 8, 9)})
        self.assertIsNone(_summarize_spec(rows).sunshine)

    def test_sunshine_missing_mark_outside_core_hours_drops_key(self) -> None:
        # 17 時 (8〜16 時の外, 数値のある時間帯の端) の欠測記号だけでも合計は過小になる
        self.assertIsNone(_summarize_spec(_day({17: {"sun": "×"}})).sunshine)

    def test_sunshine_blank_inside_numeric_range_outside_core_drops_key(self) -> None:
        # 6 時に数値があり 7 時が空欄 → 日中の内側の欠落 (8〜16 時の判定には掛からない位置)
        self.assertIsNone(_summarize_spec(_day({6: {"sun": "0.3"}, 7: {"sun": ""}})).sunshine)

    def test_sunshine_core_hours_are_8_to_16(self) -> None:
        # 端の 2 時間が欠けると数値の範囲は狭まるが、8 時 / 16 時は全国どこでも日中なので欠測
        for hours in ((7, 8), (16, 17)):
            with self.subTest(blank=hours):
                self.assertIsNone(_summarize_spec(_day({h: {"sun": ""} for h in hours})).sunshine)
        # 7 時 / 17 時だけの空欄は夜明け前 / 日没後と区別できないので 0 として合計する
        for hour in (7, 17):
            with self.subTest(blank=hour):
                self.assertEqual(_summarize_spec(_day({hour: {"sun": ""}})).sunshine, "5.0時間")

    def test_sunshine_night_blanks_are_zero(self) -> None:
        # 既定: 7〜17 時が 0.5h, 他は空欄 → 11 × 0.5 = 5.5
        self.assertEqual(_summarize_spec(_day()).sunshine, "5.5時間")

    def test_sunshine_short_winter_day(self) -> None:
        overrides = {h: {"sun": ""} for h in (7, 17)}
        overrides[8] = {"sun": "0.0"}
        self.assertEqual(_summarize_spec(_day(overrides)).sunshine, "4.0時間")

    def test_sunshine_all_blank_drops_key(self) -> None:
        rows = _day({h: {"sun": ""} for h in range(1, 25)})
        self.assertIsNone(_summarize_spec(rows).sunshine)

    def test_weather_with_some_hours_missing_lists_the_rest(self) -> None:
        rows = _day({h: {"alt": None} for h in (3, 6, 9, 15, 18, 21)})
        self.assertEqual(_summarize_spec(rows).weather, f"12時: 晴れ{SUN}")


class DayCompletenessTest(unittest.TestCase):
    """SPEC v2 §4.3: 24 時の行に気温・海面気圧があり、天気が 1 件以上 → 完成した日."""

    def assert_incomplete(self, rows: DaySpec, observed: int) -> WeatherSummary:
        summary = _summarize_spec(rows)
        self.assertFalse(summary.is_complete)
        self.assertEqual(_summary_dict(summary), {name: None for name in WEATHER_FIELDS})
        self.assertEqual(summary.missing_keys, WEATHER_FIELDS)
        self.assertEqual(summary.observed_hours, observed)
        return summary

    def test_complete_day(self) -> None:
        summary = _summarize_spec(_day())
        self.assertTrue(summary.is_complete)
        self.assertEqual(summary.missing_keys, ())
        self.assertEqual(summary.status, "complete")

    def test_missing_24_row(self) -> None:
        summary = self.assert_incomplete(_day(hours=range(1, 24)), observed=23)
        self.assertEqual(summary.status, "partial")

    def test_24_temperature_missing(self) -> None:
        self.assert_incomplete(_day({24: {"temp": "×"}}), observed=24)

    def test_24_pressure_missing(self) -> None:
        self.assert_incomplete(_day({24: {"slp": ""}}), observed=24)

    def test_no_weather_observation(self) -> None:
        self.assert_incomplete(_day({h: {"alt": None} for h in _ALT_HOURS}), observed=24)

    def test_header_only_table_is_nodata_only_while_pending(self) -> None:
        page = weather._parse_page(_page([]))
        self.assertTrue(page.table_found)
        self.assertEqual(page.observations, [])
        waiting = summarize_weather(page.observations, FIXTURE_DATE, pending=True)
        self.assertEqual((waiting.status, waiting.observed_hours), ("nodata", 0))
        # 確定済みのはずの日に 0 行 (休止した観測所 / 番号の誤り) は黙って nodata にしない
        stale = summarize_weather(page.observations, FIXTURE_DATE)
        self.assertEqual((stale.status, stale.observed_hours, stale.is_complete), ("partial", 0, False))
        self.assertEqual(stale.missing_keys, WEATHER_FIELDS)

    def test_single_observed_row(self) -> None:
        # 行が 1 つでもあれば「未公開 (0 行)」ではない. 確定済みのはずの日なら partial
        summary = self.assert_incomplete(_day(hours=[1]), observed=1)
        self.assertEqual(summary.status, "partial")
        waiting = summarize_weather(parse_jma_hourly(_page(_day(hours=[1]))), FIXTURE_DATE, pending=True)
        self.assertEqual(waiting.status, "nodata")

    def test_summarize_ignores_out_of_range_and_duplicate_hours(self) -> None:
        base = parse_jma_hourly(_page(_day()))
        extra = [
            HourlyObservation(hour=0, temperature=99.0, sea_level_pressure=900.0),
            HourlyObservation(hour=25, temperature=99.0, sea_level_pressure=900.0),
            HourlyObservation(hour=12, temperature=99.0, sea_level_pressure=900.0),  # 重複は先勝ち
        ]
        self.assertEqual(
            summarize_weather(base + extra, FIXTURE_DATE),
            summarize_weather(base, FIXTURE_DATE),
        )

    def test_summary_is_frozen(self) -> None:
        summary = _summarize_spec(_day())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            summary.weather = "x"  # type: ignore[misc]


class PendingStatusTest(unittest.TestCase):
    """SPEC v2 §4.1: 公開待ち (想定内の遅延) は nodata, 確定済みのはずの日の未完成は partial.

    前日分は 24 時間分の数値が先に公開され、天気欄 (img) が後から埋まる. この状態を
    毎朝 partial (= 部分失敗) と報告しないよう、:func:`fetch_weather` が日付から
    ``pending`` を決める (fetch 側の境界は :class:`FetchWeatherTest`).
    """

    def _prepublication_obs(self) -> "List[HourlyObservation]":
        return parse_jma_hourly(_page(_day({h: {"alt": None} for h in _ALT_HOURS})))

    def test_incomplete_day_status_follows_pending(self) -> None:
        obs = self._prepublication_obs()
        stale = summarize_weather(obs, FIXTURE_DATE)
        self.assertEqual(
            (stale.is_complete, stale.pending, stale.status, stale.observed_hours),
            (False, False, "partial", 24),
        )
        waiting = summarize_weather(obs, FIXTURE_DATE, pending=True)
        self.assertEqual((waiting.is_complete, waiting.pending, waiting.status), (False, True, "nodata"))
        self.assertEqual(waiting.missing_keys, WEATHER_FIELDS)

    def test_complete_day_is_never_pending(self) -> None:
        summary = summarize_weather(parse_jma_hourly(_page(_day())), FIXTURE_DATE, pending=True)
        self.assertEqual((summary.status, summary.pending), ("complete", False))

    def test_incomplete_reasons(self) -> None:
        """ログに出す未完成の理由 (完成した日は空)."""

        def reasons(rows: DaySpec) -> "List[str]":
            return weather._incomplete_reasons(weather._index_by_hour(parse_jma_hourly(_page(rows))))

        self.assertEqual(reasons(_day()), [])
        self.assertEqual(weather._incomplete_reasons({}), ["観測行なし"])
        self.assertEqual(reasons(_day({h: {"alt": None} for h in _ALT_HOURS})), ["天気 (画像の alt) が 0 件"])
        self.assertEqual(reasons(_day(hours=range(1, 24))), ["24時の行なし"])
        self.assertEqual(
            reasons(_day({24: {"temp": "×", "slp": ""}})),
            ["24時の気温が欠測", "24時の海面気圧が欠測"],
        )

    def test_missing_key_on_complete_day_is_partial_even_when_pending(self) -> None:
        # 日は完成している (= 公開済み) ので、欠測キーは公開待ちではなく欠測
        rows = _day({13: {"precip": "×"}})
        summary = summarize_weather(parse_jma_hourly(_page(rows)), FIXTURE_DATE, pending=True)
        self.assertEqual((summary.status, summary.missing_keys), ("partial", ("precipitation",)))


# ---------------------------------------------------------------------------
# HTML の頑健性 / レイアウト変更検知
# ---------------------------------------------------------------------------


class HtmlRobustnessTest(unittest.TestCase):
    def test_unclosed_cells_and_rows(self) -> None:
        html = _page(_day()).replace("</td>", "").replace("</tr>", "")
        self.assertEqual(
            _summary_dict(summarize_weather(parse_jma_hourly(html), FIXTURE_DATE)),
            _summary_dict(_summarize_spec(_day())),
        )

    def test_nested_table_inside_cell_is_ignored(self) -> None:
        html = _page(_day()).replace(
            '<td class="data_0_0"><img src="../../data/image/tenki/large/F8A0.gif" alt="晴れ"></td>',
            '<td class="data_0_0"><table><tr><td>99.9</td><td><img alt="ダミー"></td></tr></table>'
            '<img src="x.gif" alt="曇"></td>',
            1,
        )
        obs = {o.hour: o for o in parse_jma_hourly(html)}
        self.assertEqual(obs[3].weather, "曇")
        self.assertEqual(len(obs), 24)

    def test_only_first_image_in_weather_cell_counts(self) -> None:
        html = _page(_day()).replace(
            'alt="晴れ">', 'alt="雨"><img src="y.gif" alt="快晴">', 1,
        )
        self.assertEqual({o.hour: o for o in parse_jma_hourly(html)}[3].weather, "雨")

    def test_image_without_alt_or_empty_alt_is_not_weather(self) -> None:
        rows = _day({3: {"alt": ""}})
        html = _page(rows).replace('alt="晴れ">', ">", 1)  # 6 時の img から alt を消す
        obs = {o.hour: o for o in parse_jma_hourly(html)}
        self.assertIsNone(obs[3].weather)
        self.assertIsNone(obs[6].weather)
        self.assertEqual(obs[9].weather, "晴れ")

    def test_whitespace_and_entities_in_cells(self) -> None:
        rows = _day({5: {"temp": " 12.5 ", "hum": "&nbsp;81&nbsp;"}})
        obs = {o.hour: o for o in parse_jma_hourly(_page(rows))}[5]
        self.assertEqual((obs.temperature, obs.humidity), (12.5, 81))

    def test_columns_are_located_from_header_even_if_reordered(self) -> None:
        html = _page(_day({5: {"temp": "21.5", "hum": "44"}}))
        header_swapped = _HEADER_ROWS.replace("気温<br />(℃)", "TMP").replace("湿度<br />(％)", "気温<br />(℃)").replace("TMP", "湿度<br />(％)")
        html = html.replace(_HEADER_ROWS, header_swapped)
        obs = {o.hour: o for o in parse_jma_hourly(html)}[5]
        # 列 4 (実データは気温) が「湿度」見出しになったので、湿度=21.5 / 気温=44 と読む
        self.assertEqual((obs.temperature, obs.humidity), (44.0, 21.5))

    def test_table_wrapped_in_layout_table(self) -> None:
        html = _page(_day())
        start = html.index("<h3")
        end = html.index("<h3", start + 1)
        wrapped = html[:start] + "<table class='layout'><tr><td>" + html[start:end] + "</td></tr></table>" + html[end:]
        page = weather._parse_page(wrapped)
        self.assertEqual(len(page.observations), 24)
        self.assertEqual(page.page_date, FIXTURE_DATE)

    def test_non_hour_rows_are_skipped(self) -> None:
        html = _page(_day()).replace(
            "\n</table>",
            '\n<tr><td>合計</td><td>1</td><td>2</td></tr>\n<tr><th>時</th><th>海面</th></tr>\n</table>',
            1,
        )
        self.assertEqual(len(parse_jma_hourly(html)), 24)

    _ONE_ROW_HEADER = (
        "<tr>"
        + "".join(
            f"<th>{label}</th>"
            for label in (
                "時", "現地気圧", "海面気圧", "降水量", "気温", "露点温度", "蒸気圧", "湿度", "風速",
                "風向", "日照時間", "全天日射量", "降雪", "積雪", "天気", "雲量", "視程",
            )
        )
        + "</tr>\n"
    )
    # 「海面」は 3 段目にしか無い (見出しを 2 段と決め打ちすると列を特定できない)
    _THREE_ROW_HEADER = (
        '<tr><th rowspan="3">時</th><th colspan="2">気圧(hPa)</th><th rowspan="3">降水量</th>'
        '<th rowspan="3">気温</th><th rowspan="3">露点温度</th><th rowspan="3">蒸気圧</th>'
        '<th rowspan="3">湿度</th><th colspan="2" rowspan="2">風向・風速</th><th rowspan="3">日照時間</th>'
        '<th rowspan="3">全天日射量</th><th colspan="2" rowspan="2">雪(cm)</th><th rowspan="3">天気</th>'
        '<th rowspan="3">雲量</th><th rowspan="3">視程</th></tr>\n'
        '<tr><th colspan="2">観測値</th></tr>\n'
        "<tr><th>現地</th><th>海面</th><th>風速</th><th>風向</th><th>降雪</th><th>積雪</th></tr>\n"
    )

    def test_header_row_count_is_detected(self) -> None:
        expected = _summary_dict(_summarize_spec(_day()))
        for label, header in (("1 段", self._ONE_ROW_HEADER), ("3 段", self._THREE_ROW_HEADER)):
            with self.subTest(header=label):
                obs = parse_jma_hourly(_page(_day(), header=header))
                self.assertEqual([o.hour for o in obs], list(range(1, 25)))
                self.assertEqual(_summary_dict(summarize_weather(obs, FIXTURE_DATE)), expected)


_HOUR_CELL_RE = re.compile(r'(<td style="white-space:nowrap">)(\d+)(</td>)')


def _with_hour_cells(html: str, render: "Callable[[int], str]") -> str:
    """時刻列のセル (``1``〜``24``) を ``render(時刻)`` に書き換える (時刻列の仕様変更の模擬)."""
    rewritten = _HOUR_CELL_RE.sub(lambda m: m.group(1) + render(int(m.group(2))) + m.group(3), html)
    assert rewritten != html
    return rewritten


class LayoutChangeTest(unittest.TestCase):
    def test_hour_cells_in_another_format_raise(self) -> None:
        # 書式が変わった時刻を 0 行 (= 未公開, nodata) と取り違えず失敗にする.
        # "00:00" 形式は区間の始まりを表す可能性もあるので読み替えない
        formats: "Dict[str, Callable[[int], str]]" = {
            "1時": lambda h: f"{h}時",
            "00:00": lambda h: f"{h - 1:02d}:00",
            "1.0": lambda h: f"{h}.0",
        }
        for label, render in formats.items():
            with self.subTest(format=label):
                with self.assertRaisesRegex(WeatherError, "時刻列"):
                    parse_jma_hourly(_with_hour_cells(_page(_day()), render))

    def test_single_unreadable_hour_cell_raises(self) -> None:
        html = _page(_day()).replace(
            '<td style="white-space:nowrap">13</td>', '<td style="white-space:nowrap">13時</td>', 1,
        )
        with self.assertRaisesRegex(WeatherError, "13時"):
            parse_jma_hourly(html)

    def test_data_rows_without_any_hour_raise(self) -> None:
        html = _with_hour_cells(_page(_day()), lambda h: "-")
        with self.assertRaisesRegex(WeatherError, "24 行"):
            parse_jma_hourly(html)

    def test_missing_sunshine_column(self) -> None:
        html = _page(_day()).replace("日照<br />時間<br />(h)", "日射<br />時間<br />(h)")
        with self.assertRaisesRegex(WeatherError, "日照"):
            parse_jma_hourly(html)

    def test_ambiguous_column(self) -> None:
        html = _page(_day()).replace("露点<br />温度<br />(℃)", "最低気温<br />(℃)")
        with self.assertRaisesRegex(WeatherError, "気温"):
            parse_jma_hourly(html)

    def test_missing_hour_column(self) -> None:
        html = _page(_day()).replace('<th rowspan="2" scope="col">時</th>', '<th rowspan="2" scope="col">時刻</th>')
        with self.assertRaises(WeatherError):
            parse_jma_hourly(html)

    def test_duplicate_hour_rows(self) -> None:
        rows = _day() + [(24, _default_row(24))]
        with self.assertRaisesRegex(WeatherError, "24時"):
            parse_jma_hourly(_page(rows))

    def test_hour_out_of_range(self) -> None:
        rows = _day(hours=range(0, 24))
        with self.assertRaisesRegex(WeatherError, "1〜24"):
            parse_jma_hourly(_page(rows))


# ---------------------------------------------------------------------------
# fetch_weather (ネットワークはモック)
# ---------------------------------------------------------------------------


class _FakeOpener:
    """``(Request, timeout) -> bytes``. 応答 (bytes) または例外を順に返す."""

    def __init__(self, responses: "Sequence[object]") -> None:
        self._responses = list(responses)
        self.calls: "List[Tuple[urllib.request.Request, float]]" = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> bytes:
        self.calls.append((request, timeout))
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        assert isinstance(response, bytes)
        return response


class _WarningCollector(logging.Handler):
    """logger に付けて WARNING 以上の記録を集める (「警告が出ないこと」の確認用)."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: "List[logging.LogRecord]" = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(jma_hourly_url(FIXTURE_DATE), code, "error", None, None)  # type: ignore[arg-type]


class FetchWeatherTest(unittest.TestCase):
    def setUp(self) -> None:
        # 誤って実ネットワークに出たら即失敗させる
        patcher = mock.patch.object(
            urllib.request, "urlopen", side_effect=AssertionError("network access in unit test")
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.page = _page(_day()).encode("utf-8")
        self.sleeps: "List[float]" = []
        self.logger = logging.getLogger("test.ihealth.weather")

    def fetch(self, opener: _FakeOpener, d: date = FIXTURE_DATE, **kwargs: object) -> WeatherSummary:
        # 公開待ち判定を実行時の時計に依存させない. 既定は「確定済みの過去日」として取得する
        kwargs.setdefault("today", d + timedelta(days=30))
        return fetch_weather(d, opener=opener, logger=self.logger, sleep=self.sleeps.append, **kwargs)  # type: ignore[arg-type]

    def test_success_sends_user_agent_url_and_timeout(self) -> None:
        opener = _FakeOpener([self.page])
        summary = self.fetch(opener)
        self.assertTrue(summary.is_complete)
        self.assertEqual(summary.date, FIXTURE_DATE)
        self.assertEqual(_summary_dict(summary), _summary_dict(_summarize_spec(_day())))
        (request, timeout), = opener.calls
        self.assertEqual(request.full_url, jma_hourly_url(FIXTURE_DATE))
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(
            request.get_header("User-agent"),
            f"ihealth-relay/{__version__} (+https://github.com/arkatom/ihealth-relay)",
        )
        self.assertEqual(timeout, 20.0)
        self.assertEqual(self.sleeps, [])

    def test_custom_station_and_timeout(self) -> None:
        opener = _FakeOpener([self.page])
        self.fetch(opener, prec_no=62, block_no=47772, timeout=5.5)
        (request, timeout), = opener.calls
        self.assertEqual(request.full_url, jma_hourly_url(FIXTURE_DATE, 62, 47772))
        self.assertEqual(timeout, 5.5)

    def test_unavailable_page_is_nodata_not_error(self) -> None:
        body = (FIXTURES / "hourly_unavailable.html").read_bytes()
        summary = self.fetch(_FakeOpener([body]), d=date(2031, 2, 15), today=date(2031, 2, 15))
        self.assertFalse(summary.is_complete)
        self.assertEqual(summary.status, "nodata")
        self.assertEqual(summary.missing_keys, WEATHER_FIELDS)

    def test_zero_row_pages_of_old_days_are_partial_with_warning(self) -> None:
        # 公開待ちの期間 (2 日) を過ぎた日の「0 行」は想定外: 観測所の休止 / JMA_PREC_NO・
        # JMA_BLOCK_NO の誤り / 過去日に未公開の案内が返る障害。nodata (= 黙って何も
        # 書かない) にすると利用者が気付けないので partial + warning にする
        # (統合側は 2 日以上続けば通知する)
        cases = {
            # 見出しの日付 (2031-02-14) と要求日を合わせる
            "見出しだけの表": (_page([]).encode("utf-8"), FIXTURE_DATE),
            "未公開の案内": ((FIXTURES / "hourly_unavailable.html").read_bytes(), date(2031, 2, 15)),
        }
        for label, (body, d) in cases.items():
            with self.subTest(case=label):
                with self.assertLogs(self.logger, level="WARNING"):
                    summary = self.fetch(_FakeOpener([body]), d=d, today=d + timedelta(days=5))
                self.assertEqual(
                    (summary.status, summary.pending, summary.is_complete, summary.observed_hours),
                    ("partial", False, False, 0),
                )
                self.assertEqual(summary.missing_keys, WEATHER_FIELDS)
                # 公開待ちの期間内なら同じページでも nodata (想定内)
                waiting = self.fetch(_FakeOpener([body]), d=d, today=d + timedelta(days=2))
                self.assertEqual((waiting.status, waiting.pending), ("nodata", True))

    def _prepublication_body(self) -> bytes:
        """前日分の公開途中: 24 時間分の数値は出ているが天気欄 (img) がまだ空."""
        return _page(_day({h: {"alt": None} for h in _ALT_HOURS})).encode("utf-8")

    def test_prepublication_page_is_nodata_within_grace_days(self) -> None:
        # 想定内の遅延なので warning も出さない (tests/__init__.py が INFO 以下を止めているので
        # WARNING 以上だけを集めて「無いこと」を確かめる. 3.9 には assertNoLogs が無い)
        collector = _WarningCollector()
        self.logger.addHandler(collector)
        self.addCleanup(self.logger.removeHandler, collector)
        for days in (0, 1, 2):
            with self.subTest(days_since=days):
                summary = self.fetch(
                    _FakeOpener([self._prepublication_body()]), today=FIXTURE_DATE + timedelta(days=days),
                )
                self.assertEqual(
                    (summary.status, summary.pending, summary.is_complete, summary.observed_hours),
                    ("nodata", True, False, 24),
                )
                self.assertEqual(_summary_dict(summary), {name: None for name in WEATHER_FIELDS})
        self.assertEqual(collector.records, [])

    def test_incomplete_page_older_than_grace_days_is_partial_with_warning(self) -> None:
        # 確定済みのはずの日が埋まらない = 気象庁の欠測 / 天気欄の仕様変更. 黙って nodata にしない
        cases = {
            "天気欄が空": (self._prepublication_body(), "天気 (画像の alt) が 0 件"),
            "天気が img でなく文字": (
                re.sub(r'<img src="[^"]*tenki[^"]*" alt="([^"]*)">', r"\1", _page(_day())).encode("utf-8"),
                "天気 (画像の alt) が 0 件",
            ),
            "24時の気温が欠測": (_page(_day({24: {"temp": "×"}})).encode("utf-8"), "24時の気温が欠測"),
        }
        for label, (body, reason) in cases.items():
            with self.subTest(case=label):
                with self.assertLogs(self.logger, level="WARNING") as captured:
                    summary = self.fetch(_FakeOpener([body]), today=FIXTURE_DATE + timedelta(days=3))
                self.assertEqual((summary.status, summary.pending, summary.is_complete), ("partial", False, False))
                self.assertEqual(summary.missing_keys, WEATHER_FIELDS)
                self.assertIn(reason, captured.output[0])

    def test_complete_page_is_complete_whatever_the_date(self) -> None:
        for days in (0, 1, 3, 400):
            with self.subTest(days_since=days):
                summary = self.fetch(_FakeOpener([self.page]), today=FIXTURE_DATE + timedelta(days=days))
                self.assertEqual((summary.status, summary.pending), ("complete", False))

    def test_today_defaults_to_jst_clock(self) -> None:
        def fetch_without_today() -> WeatherSummary:
            return fetch_weather(
                FIXTURE_DATE, opener=_FakeOpener([self._prepublication_body()]),
                logger=self.logger, sleep=self.sleeps.append,
            )

        with mock.patch.object(weather, "_jst_today", return_value=FIXTURE_DATE + timedelta(days=2)) as clock:
            self.assertEqual(fetch_without_today().status, "nodata")
        clock.assert_called_once_with()
        with mock.patch.object(weather, "_jst_today", return_value=FIXTURE_DATE + timedelta(days=3)) as clock:
            with self.assertLogs(self.logger, level="WARNING"):
                self.assertEqual(fetch_without_today().status, "partial")
        clock.assert_called_once_with()

    def test_jst_today_is_the_japan_date(self) -> None:
        # 2031-02-14 15:00 UTC = 2031-02-15 00:00 JST (実行環境の TZ には依存しない)
        midnight_jst = datetime(2031, 2, 14, 15, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(weather._jst_today(lambda: midnight_jst), date(2031, 2, 15))
        self.assertEqual(weather._jst_today(lambda: midnight_jst - 1), date(2031, 2, 14))

    def test_hour_format_change_is_an_error_not_nodata(self) -> None:
        body = _with_hour_cells(_page(_day()), lambda h: f"{h}時").encode("utf-8")
        with self.assertRaisesRegex(WeatherError, "時刻列"):
            self.fetch(_FakeOpener([body]), today=FIXTURE_DATE + timedelta(days=1))

    def test_unexpected_page_without_table_or_notice_raises(self) -> None:
        body = "<html><body><p>ただいまメンテナンス中です</p></body></html>".encode("utf-8")
        with self.assertRaisesRegex(WeatherError, "仕様変更"):
            self.fetch(_FakeOpener([body]))

    def test_page_date_mismatch_raises(self) -> None:
        with self.assertRaisesRegex(WeatherError, "2031-02-14"):
            self.fetch(_FakeOpener([self.page]), d=date(2031, 2, 13))

    def test_layout_change_raises(self) -> None:
        body = _page(_day()).replace("天気</th>", "天候</th>").encode("utf-8")
        with self.assertRaises(WeatherError):
            self.fetch(_FakeOpener([body]))

    def test_retries_5xx_then_succeeds(self) -> None:
        opener = _FakeOpener([_http_error(503), self.page])
        with self.assertLogs(self.logger, level="WARNING") as captured:
            summary = self.fetch(opener)
        self.assertTrue(summary.is_complete)
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(self.sleeps, [1.0])
        self.assertIn("HTTP 503", captured.output[0])

    def test_retries_429(self) -> None:
        opener = _FakeOpener([_http_error(429), _http_error(429), self.page])
        with self.assertLogs(self.logger, level="WARNING"):
            self.assertTrue(self.fetch(opener).is_complete)
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_4xx_is_not_retried(self) -> None:
        opener = _FakeOpener([_http_error(404), self.page])
        with self.assertRaisesRegex(WeatherError, "HTTP 404"):
            self.fetch(opener)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(self.sleeps, [])

    def test_network_errors_exhaust_retries(self) -> None:
        errors = [urllib.error.URLError("dns failure"), socket.timeout("timed out"), ConnectionResetError("reset")]
        opener = _FakeOpener(errors)
        with self.assertLogs(self.logger, level="WARNING"):
            with self.assertRaisesRegex(WeatherError, "reset"):
                self.fetch(opener)
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_http_protocol_errors_are_retried(self) -> None:
        opener = _FakeOpener([http.client.RemoteDisconnected("closed"), http.client.IncompleteRead(b""), self.page])
        with self.assertLogs(self.logger, level="WARNING"):
            self.assertTrue(self.fetch(opener).is_complete)

    def test_retries_zero_means_single_attempt(self) -> None:
        opener = _FakeOpener([_http_error(503), self.page])
        with self.assertRaises(WeatherError):
            self.fetch(opener, retries=0)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(self.sleeps, [])

    def test_backoff_is_capped(self) -> None:
        self.assertEqual(weather._backoff_seconds(0), 1.0)
        self.assertEqual(weather._backoff_seconds(1), 2.0)
        self.assertEqual(weather._backoff_seconds(10), 30.0)

    def test_meta_charset_is_honored(self) -> None:
        html = _page(_day(), head_meta='<meta http-equiv="Content-Type" content="text/html; charset=Shift_JIS">')
        # ㎡ / 全角ハイフン (－) は厳密な Shift_JIS に無いので置き換えてから encode する
        body = html.replace("㎡", "m2").replace("－", "-").encode("shift_jis")
        self.assertNotEqual(body, html.encode("utf-8"))
        summary = self.fetch(_FakeOpener([body]))
        self.assertTrue(summary.is_complete)
        self.assertEqual(summary.weather, _summarize_spec(_day()).weather)

    def test_invalid_bytes_are_replaced_with_warning(self) -> None:
        body = self.page.replace(b"</body>", b"\xff\xfe</body>")
        with self.assertLogs(self.logger, level="WARNING") as captured:
            summary = self.fetch(_FakeOpener([body]))
        self.assertTrue(summary.is_complete)
        self.assertIn("置換", captured.output[0])

    def test_unknown_charset_falls_back_to_utf8(self) -> None:
        # 未知の名前 / bytes 用 codec (base64 は codecs.lookup は通るが bytes.decode できない)
        for charset in ("x-no-such-charset", "base64"):
            with self.subTest(charset=charset):
                body = _page(_day(), head_meta=f'<meta charset="{charset}">').encode("utf-8")
                with self.assertLogs(self.logger, level="WARNING"):
                    self.assertTrue(self.fetch(_FakeOpener([body])).is_complete)

    def test_default_opener_uses_urlopen_with_timeout(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = self.page
        with mock.patch.object(urllib.request, "urlopen", return_value=response) as urlopen:
            summary = fetch_weather(
                FIXTURE_DATE, timeout=7.5, logger=self.logger, sleep=self.sleeps.append,
                today=FIXTURE_DATE + timedelta(days=30),
            )
        self.assertTrue(summary.is_complete)
        (request,), kwargs = urlopen.call_args
        self.assertEqual(request.full_url, jma_hourly_url(FIXTURE_DATE))
        self.assertEqual(kwargs, {"timeout": 7.5})

    def test_default_opener_rejects_oversized_body(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"x" * 11
        with mock.patch.object(urllib.request, "urlopen", return_value=response), \
                mock.patch.object(weather, "_MAX_BODY_BYTES", 10):
            with self.assertRaisesRegex(WeatherError, "大きすぎ"):
                fetch_weather(
                    FIXTURE_DATE, logger=self.logger, sleep=self.sleeps.append,
                    today=FIXTURE_DATE + timedelta(days=30),
                )


if __name__ == "__main__":
    unittest.main()
