"""weather.py (気象庁 hourly_s1 の取得・HTML パース・前身互換の集計) のテスト.

- fixture は **合成 HTML** (``tests/fixtures/jma/``, および本ファイルの ``_page``)。
  実ページの構造 (見出し 2 段 + rowspan/colspan, 後続の「記事」表, ナビゲーション画像)
  を模した架空値で、実観測値は含まない。
- 前身 GoogleFitNotionIntegration ``src/weather/weather_notion.py`` の集計部を逐語転写した
  参照実装 :func:`_legacy_summary` と「同じ入力で同じ出力」になることを固定ケース +
  乱数ケースで検証する (見出しスキップの off-by-one だけは意図的に修正済みなので、
  参照実装も全データ行を読む形にしてある)。
- ネットワークには出ない (opener 注入 / ``urllib.request.urlopen`` をモック)。
"""

from __future__ import annotations

import dataclasses
import http.client
import logging
import random
import socket
import statistics
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
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
# 前身 weather_notion.get_weather_data の集計部 (67〜199 行目) の逐語転写
# ---------------------------------------------------------------------------


def _legacy_emoji(condition: str) -> str:
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


def _legacy_summary(rows: DaySpec) -> "Dict[str, str]":
    """前身の集計 (bs4 の ``cells[i].text.strip()`` を行仕様の文字列に置換).

    前身の ``if i > 2`` は見出し 2 行 + 1 時の行を読み捨てる off-by-one だったが、本実装は
    意図的に修正しているので、ここでは全データ行を読む (それ以外の式・書式は逐語).
    """
    weather_info = []
    total_sunshine = 0
    humidity_data = []
    sea_level_pressures = []
    temperature_data = []
    precipitation_data = []

    for hour_number, spec in rows:
        cells = [str(hour_number) if name == "hour" else spec[name] for name in _COLUMN_ORDER]
        hour = int(str(cells[0]).strip())
        sea_pressure = str(cells[2]).strip()
        if sea_pressure != "--" and sea_pressure != "":
            sea_level_pressures.append((hour, float(sea_pressure)))
        precipitation = str(cells[3]).strip()
        if precipitation != "--" and precipitation != "":
            try:
                precipitation_data.append((hour, float(precipitation)))
            except ValueError:
                precipitation_data.append((hour, 0.0))
        temperature = str(cells[4]).strip()
        if temperature != "--" and temperature != "":
            temperature_data.append((hour, float(temperature)))
        humidity = str(cells[7]).strip()
        if humidity != "--" and humidity != "":
            humidity_data.append((hour, int(humidity)))
        sunshine = str(cells[10]).strip()
        if sunshine != "--" and sunshine != "":
            try:
                total_sunshine += float(sunshine)
            except ValueError:
                pass
        if cells[14] is not None:
            weather_condition = cells[14]
            emoji = _legacy_emoji(weather_condition)
            weather_info.append(f"{hour}時: {weather_condition}{emoji}")

    pressure_spans = []
    for i in range(1, 25, 6):
        span_pressures = [p[1] for p in sea_level_pressures if i <= p[0] < i + 6]
        if span_pressures:
            avg_pressure = statistics.mean(span_pressures)
            min_pressure = min(span_pressures)
            max_pressure = max(span_pressures)
            pressure_spans.append((i, i + 5, avg_pressure, min_pressure, max_pressure, span_pressures))
    for i in range(len(pressure_spans) - 1):
        current_span = pressure_spans[i]
        next_span = pressure_spans[i + 1]
        change_mark = ""
        if any(current_span[4] - next_value >= 5 for next_value in next_span[5]):
            change_mark = "⤵️💣️"
        elif any(next_value - current_span[3] >= 5 for next_value in next_span[5]):
            change_mark = "⤴️⚠️"
        pressure_spans[i] = (*current_span[:5], change_mark)
    if pressure_spans:
        pressure_spans[-1] = (*pressure_spans[-1][:5], "")

    morning_temps = [t[1] for t in temperature_data if 5 <= t[0] <= 11]
    daytime_temps = [t[1] for t in temperature_data if 12 <= t[0] <= 18]
    evening_temps = [t[1] for t in temperature_data if t[0] >= 19 or t[0] <= 4]
    all_temps = [t[1] for t in temperature_data]
    max_temp = max(all_temps) if all_temps else 0
    min_temp = min(all_temps) if all_temps else 0
    morning_avg = statistics.mean(morning_temps) if morning_temps else 0
    daytime_avg = statistics.mean(daytime_temps) if daytime_temps else 0
    evening_avg = statistics.mean(evening_temps) if evening_temps else 0

    morning_humidity = [h[1] for h in humidity_data if 5 <= h[0] <= 11]
    daytime_humidity = [h[1] for h in humidity_data if 12 <= h[0] <= 18]
    evening_humidity = [h[1] for h in humidity_data if h[0] >= 19 or h[0] <= 4]
    all_humidity = [h[1] for h in humidity_data]
    max_humidity = max(all_humidity) if all_humidity else 0
    min_humidity = min(all_humidity) if all_humidity else 0
    morning_hum_avg = statistics.mean(morning_humidity) if morning_humidity else 0
    daytime_hum_avg = statistics.mean(daytime_humidity) if daytime_humidity else 0
    evening_hum_avg = statistics.mean(evening_humidity) if evening_humidity else 0

    morning_precip = sum([p[1] for p in precipitation_data if 5 <= p[0] <= 11])
    daytime_precip = sum([p[1] for p in precipitation_data if 12 <= p[0] <= 18])
    evening_precip = sum([p[1] for p in precipitation_data if p[0] >= 19 or p[0] <= 4])

    pressure_str = ", ".join([f"{s[0]}-{s[1]}時:平均{s[2]:.1f}hPa{s[5]}" for s in pressure_spans])
    temp_str = f"朝:平均{morning_avg:.1f}℃, 昼:平均{daytime_avg:.1f}℃, 夜:平均{evening_avg:.1f}℃（最高:{max_temp:.1f}℃, 最低:{min_temp:.1f}℃）"
    humidity_str = f"朝:平均{morning_hum_avg:.1f}%, 昼:平均{daytime_hum_avg:.1f}%, 夜:平均{evening_hum_avg:.1f}%（最高:{max_humidity}%, 最低:{min_humidity}%）"
    precip_str = f"朝:{morning_precip:.1f}mm, 昼:{daytime_precip:.1f}mm, 夜:{evening_precip:.1f}mm"
    weather_str = ", ".join(weather_info)
    return {
        "weather": weather_str,
        "temperature": temp_str,
        "humidity": humidity_str,
        "precipitation": precip_str,
        "pressure": pressure_str,
        "sunshine": f"{total_sunshine:.1f}時間",
    }


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
        summary = summarize_weather([], date(2031, 2, 15))
        self.assertFalse(summary.is_complete)
        self.assertEqual(summary.status, "nodata")


# ---------------------------------------------------------------------------
# 前身と同じ入力で同じ出力
# ---------------------------------------------------------------------------


class LegacyParityTest(unittest.TestCase):
    def assert_same_as_legacy(self, rows: DaySpec) -> WeatherSummary:
        summary = _summarize_spec(rows)
        self.assertTrue(summary.is_complete)
        self.assertEqual(_summary_dict(summary), _legacy_summary(rows))
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
                self.assertEqual(weather._weather_emoji(condition), _legacy_emoji(condition))


# ---------------------------------------------------------------------------
# 前身からの意図的な変更
# ---------------------------------------------------------------------------


class HourOneRowTest(unittest.TestCase):
    """前身は ``if i > 2`` で 1 時の行を読み捨てていた. 本実装は 1〜24 時を全て使う."""

    def test_hour_one_counts_for_night_first_span_and_extremes(self) -> None:
        rows = _day({1: {"temp": "30.0", "slp": "1040.0", "hum": "99", "precip": "2.0"}})
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

    def test_header_only_table_is_nodata(self) -> None:
        page = weather._parse_page(_page([]))
        self.assertTrue(page.table_found)
        self.assertEqual(page.observations, [])
        self.assertEqual(summarize_weather(page.observations, FIXTURE_DATE).status, "nodata")

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


class LayoutChangeTest(unittest.TestCase):
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
        summary = self.fetch(_FakeOpener([body]), d=date(2031, 2, 15))
        self.assertFalse(summary.is_complete)
        self.assertEqual(summary.status, "nodata")
        self.assertEqual(summary.missing_keys, WEATHER_FIELDS)

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
            summary = fetch_weather(FIXTURE_DATE, timeout=7.5, logger=self.logger, sleep=self.sleeps.append)
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
                fetch_weather(FIXTURE_DATE, logger=self.logger, sleep=self.sleeps.append)


if __name__ == "__main__":
    unittest.main()
