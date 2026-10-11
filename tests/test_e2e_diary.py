"""Obsidian 日記パイプラインの一気通貫テスト (WAVE3 W3-6).

``tests/test_diary.py`` は source を fake に差し替えて統合側の判断を確かめる。こちらは
**外部 I/O だけ** を差し替え、それ以外は本物を通す:

- 差し替える外部 I/O
  - HTTP: ``urllib.request.urlopen`` (気象庁 hourly_s1 と GitHub GraphQL の両方。合成 HTML と
    用意した GraphQL 応答 ``tests/fixtures/e2e/{jma,github}/`` を返す。それ以外の URL は失敗)
  - gh: GitHub token の解決 (``gh auth token`` を呼ばない)
  - iCloud: dataless ファイルの materialize (``brctl`` を呼ばず、呼ばれた path を記録する)
  - 時計・経過時間・待機、ネットワーク疎通確認 (TCP)、Reminders 通知
- 本物のまま通すもの
  - オートメーション JSON の探索・読込・集計 (``HealthLoader`` + ``automation_parser``)
  - 気象庁ページの取得・解析・集計 (``fetch_weather``。opener は既定の ``urlopen`` 経由)
  - GitHub 活動の収集 (``GitHubClient`` + ``collect_github_activity``。opener も既定)
  - 日記ノートの更新 (``workflow.build_obsidian_publisher`` → ``ObsidianPublisher``:
    frontmatter upsert・書込前の独立検査・バックアップ・書込後の確認)
  - 実行レポート (``logs/diary-report-*.json``)・未確定の値の台帳 (``data/diary-state.json``)・
    通知の集約と終了コード

4 つの条件 (通常日 / JST 0 時跨ぎ / 日の途中の書き出し (暫定値 → 確定) / 欠測) で、
ノートに書かれた値と保持された値・実行レポート・台帳を照合する。日付境界の条件は
launchd と同じ ``python3 -m ihealth`` (引数なし) の入口 (``__main__.main``) から通す。

期待値は fixture の値から手計算したもの (計算は ``tests/fixtures/e2e/README.md``)。
プロセスのローカル tz (CI は UTC) に依存しない (時刻はすべて offset 付きで与える)。
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import shutil
import tempfile
import unittest
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence
from unittest import mock

from ihealth.__main__ import main
from ihealth.config import Config, load_config
from ihealth.diary import (
    DiaryDeps,
    DiaryOptions,
    DiaryRunResult,
    diary_today,
    resolve_dates,
    run_diary,
)
from ihealth.logger import configure as configure_logger


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "e2e"
JST = timezone(timedelta(hours=9))

#: 合成の GitHub token。どの出力 (ノート・レポート・台帳・通知) にも出てはいけない。
TOKEN = "e2e-synthetic-token-not-a-secret"

#: HAE のオートメーションの出力フォルダ (名前は利用者が付ける。2 つある = 移行期間)。
CURRENT_FOLDER = "E2E オートメーション"
PREVIOUS_FOLDER = "E2E 旧オートメーション"

#: 日記テンプレート順の担当キー (実行レポートは全数をこの順で持つ)。
ALL_KEYS: "tuple[str, ...]" = (
    "歩数 (歩)", "睡眠時間 (分)", "昼寝時間", "消費カロリー (kcal)", "移動距離 (km)",
    "安静時心拍数 (bpm)", "平均心拍数 (bpm)", "最大心拍数 (bpm)", "酸素飽和度 (%)",
    "運動強度スコア", "瞑想時間 (分)", "瞑想回数 (回)", "アクティビティ時間 (分)", "体重 (kg)",
    "体脂肪率 (%)", "気温", "湿度", "気圧", "天気", "日照時間", "降水量", "アクティビティ詳細",
    "Github",
)
WEATHER_KEYS: "tuple[str, ...]" = ("気温", "湿度", "気圧", "天気", "日照時間", "降水量")

# ---- 気象庁ページ (tests/fixtures/e2e/jma) から手計算した天気の文字列 ----
SUN = "\N{BLACK SUN WITH RAYS}\N{VARIATION SELECTOR-16}"
PARTLY = "\N{SUN BEHIND CLOUD}\N{VARIATION SELECTOR-16}"
CLOUD = "\N{CLOUD}\N{VARIATION SELECTOR-16}"
RAIN = "\N{UMBRELLA WITH RAIN DROPS}"
DROP = (
    "\N{ARROW POINTING RIGHTWARDS THEN CURVING DOWNWARDS}\N{VARIATION SELECTOR-16}"
    "\N{BOMB}\N{VARIATION SELECTOR-16}"
)

WEATHER_0514: "dict[str, str]" = {
    "気温": "朝:平均18.3℃, 昼:平均23.6℃, 夜:平均17.2℃（最高:25.1℃, 最低:15.0℃）",
    "湿度": "朝:平均71.3%, 昼:平均54.7%, 夜:平均78.2%（最高:86%, 最低:50%）",
    "気圧": (
        f"1-6時:平均1013.5hPa, 7-12時:平均1014.0hPa{DROP}, 13-18時:平均1010.0hPa, "
        "19-24時:平均1011.0hPa"
    ),
    "天気": (
        f"3時: 晴れ{SUN}, 6時: 快晴{SUN}{SUN}, 9時: 薄曇{PARTLY}, 12時: 曇{CLOUD}, "
        f"15時: 雨{RAIN}, 18時: 曇{CLOUD}, 21時: 晴れ{SUN}"
    ),
    "日照時間": "6.2時間",
    "降水量": "朝:0.0mm, 昼:2.0mm, 夜:0.5mm",
}
#: 2030-05-14 の気温 +2.0 / 湿度 -5 / 海面気圧 +1.0、天気欄は別の並び。
WEATHER_0515: "dict[str, str]" = {
    "気温": "朝:平均20.3℃, 昼:平均25.6℃, 夜:平均19.2℃（最高:27.1℃, 最低:17.0℃）",
    "湿度": "朝:平均66.3%, 昼:平均49.7%, 夜:平均73.2%（最高:81%, 最低:45%）",
    "気圧": (
        f"1-6時:平均1014.5hPa, 7-12時:平均1015.0hPa{DROP}, 13-18時:平均1011.0hPa, "
        "19-24時:平均1012.0hPa"
    ),
    "天気": (
        f"3時: 曇{CLOUD}, 6時: 曇{CLOUD}, 9時: 晴れ{SUN}, 12時: 快晴{SUN}{SUN}, "
        f"15時: 快晴{SUN}{SUN}, 18時: 晴れ{SUN}, 21時: 曇{CLOUD}"
    ),
    "日照時間": "6.2時間",
    "降水量": "朝:0.0mm, 昼:2.0mm, 夜:0.5mm",
}
#: 2030-05-14 の気温 -1.0 / 湿度 +3 / 海面気圧 -2.0、降水なし、天気欄は別の並び。
WEATHER_0520: "dict[str, str]" = {
    "気温": "朝:平均17.3℃, 昼:平均22.6℃, 夜:平均16.2℃（最高:24.1℃, 最低:14.0℃）",
    "湿度": "朝:平均74.3%, 昼:平均57.7%, 夜:平均81.2%（最高:89%, 最低:53%）",
    "気圧": (
        f"1-6時:平均1011.5hPa, 7-12時:平均1012.0hPa{DROP}, 13-18時:平均1008.0hPa, "
        "19-24時:平均1009.0hPa"
    ),
    "天気": (
        f"3時: 雨{RAIN}, 6時: 雨{RAIN}, 9時: 曇{CLOUD}, 12時: 曇{CLOUD}, "
        f"15時: 薄曇{PARTLY}, 18時: 晴れ{SUN}, 21時: 快晴{SUN}{SUN}"
    ),
    "日照時間": "6.2時間",
    "降水量": "朝:0.0mm, 昼:0.0mm, 夜:0.0mm",
}
#: 2030-05-14 と同じ観測値で、湿度 (朝が全欠測) / 日照 (×) / 降水量 (資料不足 ]) が欠ける日。
WEATHER_0525: "dict[str, str]" = {
    key: WEATHER_0514[key] for key in ("気温", "気圧", "天気")
}

# ---- GitHub (tests/fixtures/e2e/github) から手計算した Github の文字列 ----
TICKET = "\N{TICKET}"
SHUFFLE = "\N{TWISTED RIGHTWARDS ARROWS}"
MEMO = "\N{MEMO}"
NOTES = "https://github.com/e2e-sample-user/notes"
TOOL = "https://github.com/e2e-sample-org/tool"

GITHUB_0514 = "\n".join([
    f"[{TICKET} notes:Issue #7: 合成の課題を閉じる]({NOTES}/issues/7)",
    f"[{SHUFFLE} notes:PR #11: feat: PR ブランチの変更]({NOTES}/pull/11)",
    f"[{SHUFFLE} tool:PR #3: feat: 合成ツールの改善]({TOOL}/pull/3)",
    f"[{MEMO} notes:変更行数:+125-32 (2 commits)]({NOTES}/commits/main)",
    f"[{MEMO} tool:変更行数:+40-8 (1 commits)]({TOOL}/commits/main)",
])
GITHUB_0515 = "\n".join([
    f"[{TICKET} notes:Issue #8: 日付境界の課題]({NOTES}/issues/8)",
    f"[{SHUFFLE} notes:PR #12: fix: 日付境界の直前にマージ]({NOTES}/pull/12)",
    f"[{MEMO} notes:変更行数:+10-4 (1 commits)]({NOTES}/commits/main)",
])
GITHUB_0516 = f"[{MEMO} notes:変更行数:+3-1 (1 commits)]({NOTES}/commits/main)"
GITHUB_0520 = "\n".join([
    f"[{SHUFFLE} tool:PR #5: docs: 合成ドキュメント更新]({TOOL}/pull/5)",
    f"[{MEMO} notes:変更行数:+15-0 (1 commits)]({NOTES}/commits/main)",
    f"[{MEMO} tool:変更行数:+8-3 (1 commits)]({TOOL}/commits/main)",
])
GITHUB_0525 = f"[{MEMO} tool:変更行数:+20-5 (1 commits)]({TOOL}/commits/main)"

JMA_URL = "https://www.data.jma.go.jp/stats/etrn/view/hourly_s1.php"
GRAPHQL_URL = "https://api.github.com/graphql"

LOGGER = logging.getLogger("test-e2e-diary")
LOGGER.propagate = False  # 想定内の WARNING (欠測・取得失敗) をテスト出力に流さない
LOGGER.addHandler(logging.NullHandler())


def jst(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=JST)


def q(text: str) -> str:
    """frontmatter に書かれる文字列の値 (YAML double-quoted = JSON 互換の表記)。"""
    return json.dumps(text, ensure_ascii=False)


def quoted(values: "Mapping[str, str]") -> "dict[str, str]":
    return {key: q(text) for key, text in values.items()}


def fill_empty_entries(note: str, values: "Mapping[str, str]") -> str:
    """``note`` の frontmatter で値が空の entry (``key:`` / ``"key":``) に値を入れたテキスト。

    期待値を「元のノートの、その行だけが変わったもの」として組む (他の行は 1 byte も変えない)。
    """
    lines = note.splitlines(keepends=True)
    close = lines.index("---\n", 1)
    for key, raw in values.items():
        hits = [i for i in range(1, close) if lines[i] in (f"{key}:\n", f'"{key}":\n')]
        if len(hits) != 1:
            raise AssertionError(f"空の entry {key!r} が frontmatter に 1 つではありません: {hits}")
        lines[hits[0]] = f"{lines[hits[0]][:-1]} {raw}\n"
    return "".join(lines)


def insert_entries(note: str, entries: "Sequence[tuple[str, str]]") -> str:
    """``note`` の frontmatter の閉じ ``---`` の直前に ``key: value`` 行を足したテキスト。"""
    lines = note.splitlines(keepends=True)
    close = lines.index("---\n", 1)
    lines[close:close] = [f"{key}: {raw}\n" for key, raw in entries]
    return "".join(lines)


def keys_view(day: "Mapping[str, Any]") -> "dict[str, tuple[Any, Any, Any]]":
    """実行レポートの 1 日分のキー → (action, new, reason)。"""
    return {
        key: (item["action"], item.get("new"), item.get("reason"))
        for key, item in day["keys"].items()
    }


def ledger_items(reason: str, since: str, values: "Mapping[str, object] | None" = None,
                 keys: "Sequence[str]" = ()) -> "dict[str, dict[str, object]]":
    """台帳の 1 日分の期待値 (``values`` は値を記録するキー、``keys`` は値なしのキー)。"""
    items: "dict[str, dict[str, object]]" = {}
    for key, value in (values or {}).items():
        items[key] = {"reason": reason, "since": since, "value": value}
    for key in keys:
        items[key] = {"reason": reason, "since": since}
    return items


# ======================================================================
# 外部 I/O の差し替え
# ======================================================================


class FakeInternet:
    """``urllib.request.urlopen`` の代わり。気象庁と GitHub GraphQL にだけ答える。"""

    def __init__(self) -> None:
        #: 日付 → 返す気象庁ページ (``tests/fixtures/e2e/jma`` のファイル名)。無い日は未公開ページ
        self.jma_pages: "dict[date, str]" = {}
        #: ``owner/name`` → その repo の活動の代わりに返す応答 (``tests/fixtures/e2e/github``)
        self.github_errors: "dict[str, str]" = {}
        self.jma_requests: "list[dict[str, Any]]" = []
        self.graphql_requests: "list[dict[str, Any]]" = []

    def urlopen(self, request: object, data: object = None, timeout: object = None,
                *args: object, **kwargs: object) -> io.BytesIO:
        if not isinstance(request, urllib.request.Request):
            raise AssertionError(f"想定外の urlopen 呼び出し: {request!r}")
        url = request.full_url
        parts = urllib.parse.urlsplit(url)
        if f"{parts.scheme}://{parts.netloc}{parts.path}" == JMA_URL:
            return io.BytesIO(self._jma(request, parts.query))
        if url == GRAPHQL_URL:
            return io.BytesIO(self._github(request))
        raise AssertionError(f"想定外のネットワークアクセス: {url}")

    def _jma(self, request: urllib.request.Request, query: str) -> bytes:
        params = dict(urllib.parse.parse_qsl(query))
        day = date(int(params["year"]), int(params["month"]), int(params["day"]))
        self.jma_requests.append({
            "date": day,
            "url": request.full_url,
            "method": request.get_method(),
            "user_agent": request.get_header("User-agent"),
        })
        name = self.jma_pages.get(day, "hourly_s1_unpublished.html")
        return (FIXTURES / "jma" / name).read_bytes()

    def _github(self, request: urllib.request.Request) -> bytes:
        payload = json.loads(bytes(request.data or b"").decode("utf-8"))
        match = re.match(r"\s*query\s+(\w+)", payload["query"])
        if match is None:
            raise AssertionError("GraphQL のクエリ名がありません")
        operation, variables = match.group(1), payload["variables"]
        self.graphql_requests.append({
            "operation": operation,
            "variables": variables,
            "method": request.get_method(),
            "authorization": request.get_header("Authorization"),
        })
        if operation == "IhealthViewer":
            name = "viewer.json"
        elif operation == "IhealthOwnerRepos":
            name = f"repos-{variables['login']}.json"
        elif operation == "IhealthRepoActivity":
            repo = f"{variables['owner']}/{variables['name']}"
            name = self.github_errors.get(repo) or (
                f"activity-{variables['owner']}-{variables['name']}.json"
            )
        else:
            raise AssertionError(f"想定外の GraphQL クエリ: {operation}")
        path = FIXTURES / "github" / name
        if not path.is_file():
            raise AssertionError(f"用意していない GraphQL 応答を求められました: {name}")
        return path.read_bytes()

    def activity_queries(self) -> "list[tuple[str, str, str, str]]":
        """repo の活動クエリ (owner, name, since, until)。"""
        return [
            (r["variables"]["owner"], r["variables"]["name"],
             r["variables"]["since"], r["variables"]["until"])
            for r in self.graphql_requests if r["operation"] == "IhealthRepoActivity"
        ]


class MaterializeSpy:
    """``ihealth.automation._ensure_materialized`` の代わり (``brctl`` を呼ばず path を記録)。"""

    def __init__(self) -> None:
        self.paths: "list[Path]" = []

    def __call__(self, path: Path, log: logging.Logger) -> None:
        if not Path(path).is_file():
            raise AssertionError(f"存在しないファイルを materialize しようとしました: {path}")
        self.paths.append(Path(path))


class FakeTime:
    """時計 (aware)・経過時間・待機。待機は記録して経過時間だけ進める。"""

    def __init__(self) -> None:
        self.now: "datetime | None" = None
        self.elapsed = 0.0
        self.sleeps: "list[float]" = []

    def clock(self) -> datetime:
        if self.now is None:
            raise AssertionError("時計が設定されていません")
        return self.now

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.elapsed += seconds


# ======================================================================
# 土台
# ======================================================================


class DiaryE2ETestBase(unittest.TestCase):
    """合成の HAE フォルダ・vault・logs を temp に作り、外部 I/O だけを差し替える。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        # HAE の iCloud Documents (オートメーションのフォルダが 2 つ)
        self.documents = self.root / "HealthExport" / "Documents"
        self.current = self.documents / CURRENT_FOLDER
        self.previous = self.documents / PREVIOUS_FOLDER
        self.current.mkdir(parents=True)
        self.previous.mkdir()
        # vault (日記 + Templater テンプレート)
        self.vault = self.root / "vault"
        self.diary = self.vault / "日記"
        self.diary.mkdir(parents=True)
        (self.vault / "テンプレート").mkdir()
        shutil.copyfile(
            FIXTURES / "notes" / "template.md", self.vault / "テンプレート" / "毎日の習慣.md",
        )
        # <project_root>/data/health (archive_root) と logs
        self.data = self.root / "data"
        (self.data / "health").mkdir(parents=True)
        self.logs = self.root / "logs"

        self.internet = FakeInternet()
        self.materialized = MaterializeSpy()
        self.time = FakeTime()
        self.notifications: "list[tuple[str, str]]" = []
        self.probes: "list[str]" = []
        self.token_requests: "list[object]" = []
        def no_process(*args: object, **kwargs: object) -> None:
            # gh / brctl / osascript / compression_tool などの外部プロセスは起動させない
            raise AssertionError(f"外部プロセスを起動しようとしました: {args!r}")

        for target, replacement in (
            ("urllib.request.urlopen", self.internet.urlopen),
            ("ihealth.automation._ensure_materialized", self.materialized),
            ("subprocess.run", no_process),
            # 開発機の config.toml (プロパティ名の override) を読まない
            ("ihealth.config_file.find_config_path", lambda *_args, **_kwargs: None),
        ):
            patcher = mock.patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    # ---- 外部 I/O の fake ----

    def resolve_token(self, env_token: object, logger: object) -> str:
        self.token_requests.append(env_token)
        return TOKEN

    def notify(self, title: str, body: str) -> bool:
        self.notifications.append((title, body))
        return True

    def probe(self, host: str) -> bool:
        self.probes.append(host)
        return True

    # ---- fixture の配置 ----

    def export_automation(
        self, kind: str, day: date, exported_at: datetime, *, folder: "Path | None" = None,
    ) -> Path:
        """HAE のオートメーションが ``exported_at`` に書き出した (= mtime) JSON を置く。"""
        name = f"HealthAutoExport-{day.isoformat()}.json"
        target = (folder or self.current) / name
        shutil.copyfile(FIXTURES / "automation" / kind / name, target)
        stamp = exported_at.timestamp()
        os.utime(target, (stamp, stamp))
        return target

    def place_note(self, day: date, *, folder: str = "") -> "tuple[Path, str]":
        """日記ノートを置き、(path, 元の内容) を返す。"""
        source = FIXTURES / "notes" / f"{day.isoformat()}.md"
        directory = self.diary / folder if folder else self.diary
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / source.name
        shutil.copyfile(source, target)
        return target, source.read_text(encoding="utf-8")

    # ---- 実行 ----

    def config(self) -> Config:
        """``JstMidnightBoundaryTest.run_cli`` が書く ``.env`` と同じ設定 (``load_config`` の結果相当)。

        HEALTH_EXPORT_DIR / BIRTH_DATE / IHEALTH_PUBLISHER=obsidian / OBSIDIAN_VAULT_DIR /
        OBSIDIAN_TEMPLATE / IHEALTH_LOOKBACK_DAYS=1 / GITHUB_EXCLUDE_REPOS。他は既定値
        (今日を含む・3 source・+09:00・ネットワーク待ち 60 秒・GITHUB_TOKEN なし = gh)。
        """
        return Config(
            notion_secret=None,
            database_id=None,
            health_export_dir=self.documents,
            archive_root=self.data / "health",
            birth_date=date(1990, 1, 1),
            default_publisher="obsidian",
            obsidian_vault_dir=self.vault,
            obsidian_template=Path("テンプレート/毎日の習慣.md"),
            lookback_days=1,
            github_exclude_repos="e2e-sample-org/ignored",
            logs_dir=self.logs,
        )

    def run_at(self, now: datetime, *, date_arg: "date | None" = None) -> DiaryRunResult:
        """``now`` に ``python3 -m ihealth`` (``--date`` 指定 or 引数なし) を実行したのと同じ流れ。

        日付の決め方は ``__main__._run_obsidian`` と同じ (日記の tz の今日から
        ``resolve_dates``、引数なしなら未確定の日も処理し直す)。依存は時計・待機・通知・
        疎通確認・token 解決だけ差し替え、source と publisher は既定 (本物) のまま。
        """
        self.time.now = now
        config = self.config()
        today = diary_today(self.time.clock, config.diary_tz)
        dates = resolve_dates(
            today=today, date_arg=date_arg, lookback_days=config.lookback_days,
            include_today=config.include_today,
        )
        options = DiaryOptions(
            dates=dates, today=today, sources=config.sources,
            revisit_unresolved=date_arg is None,
        )
        deps = DiaryDeps(
            github_token_resolver=self.resolve_token,
            notifier=self.notify,
            network_probe=self.probe,
            clock=self.time.clock,
            monotonic=self.time.monotonic,
            sleep=self.time.sleep,
        )
        return run_diary(config, options, LOGGER, deps)

    # ---- 結果の読み出し ----

    def saved_report(
        self, path: "Path | None", result: "DiaryRunResult | None" = None,
    ) -> "dict[str, Any]":
        """保存された実行レポート (journal は確定後に消えている。``result`` があれば同じ内容)。"""
        self.assertIsNotNone(path)
        assert path is not None
        self.assertEqual(sorted(self.logs.glob("diary-report-*.jsonl")), [], "journal は確定後に消える")
        saved = json.loads(path.read_text(encoding="utf-8"))
        if result is not None:
            self.assertEqual(saved, json.loads(json.dumps(result.report, ensure_ascii=False)))
        return saved

    def ledger(self) -> "dict[str, Any] | None":
        path = self.data / "diary-state.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def backups(self) -> "list[Path]":
        return sorted((self.data / "obsidian-backup").rglob("*.md"))

    def assert_vault_notes(self, *relative: str) -> None:
        """vault の日記ノートが ``relative`` だけ (新しく作られていない・tmp が残っていない)。"""
        notes = sorted(str(p.relative_to(self.diary)) for p in self.diary.rglob("*.md"))
        self.assertEqual(notes, sorted(relative))
        self.assertEqual(list(self.vault.rglob("*.tmp")), [], "tmp ファイルが残っている")

    def assert_token_not_leaked(self) -> None:
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(TOKEN.encode("utf-8"), path.read_bytes(), str(path))
        for title, body in self.notifications:
            self.assertNotIn(TOKEN, title + body)
        for request in self.internet.graphql_requests:
            self.assertEqual(request["authorization"], f"bearer {TOKEN}")
            self.assertEqual(request["method"], "POST")


# ======================================================================
# 1. 通常日: 実ソースから 1 日分を書き、再実行では何も変えない
# ======================================================================


#: 2030-05-14 (火) の全キー (昼寝時間は aggregateSleep のため値なし)。
WRITTEN_0514: "dict[str, str]" = {
    "歩数 (歩)": "5244",
    "睡眠時間 (分)": "405",
    "消費カロリー (kcal)": "530.7",
    "移動距離 (km)": "3.9",
    "安静時心拍数 (bpm)": "59",
    "平均心拍数 (bpm)": "93",
    "最大心拍数 (bpm)": "152",
    "酸素飽和度 (%)": "97",
    "運動強度スコア": "3",
    "瞑想時間 (分)": "8",
    "瞑想回数 (回)": "2",
    "アクティビティ時間 (分)": "21",
    "体重 (kg)": "64.1",
    "体脂肪率 (%)": "18.6",
    **quoted(WEATHER_0514),
    "アクティビティ詳細": q("瞑想8分、エクササイズ21分"),
    "Github": q(GITHUB_0514),
}


class NormalDayTest(DiaryE2ETestBase):
    """通常日 (翌朝 07:00 JST に ``--date`` で 1 日): 全 source が揃った日を書く。"""

    DAY = date(2030, 5, 14)

    def test_complete_day_is_written_from_real_sources_and_rerun_changes_nothing(self):
        # 現役のオートメーションが日付の変わった後に書き出した確定版と、旧オートメーションが
        # 昼に書き出した古い途中経過 (同じ日付の候補が 2 つ。新しい方だけを読む)
        exported = self.export_automation("current", self.DAY, jst(2030, 5, 15, 6, 55))
        self.export_automation("previous", self.DAY, jst(2030, 5, 14, 12, 0), folder=self.previous)
        note, original = self.place_note(self.DAY, folder="2030-Q2")
        self.internet.jma_pages[self.DAY] = "hourly_s1_2030-05-14.html"

        result = self.run_at(jst(2030, 5, 15, 7, 0), date_arg=self.DAY)

        # ---- ノート: 担当キーの空欄だけが埋まる (本文・他の frontmatter は 1 byte も変わらない)
        expected = fill_empty_entries(original, WRITTEN_0514)
        self.assertEqual(note.read_bytes(), expected.encode("utf-8"))
        self.assert_vault_notes("2030-Q2/2030-05-14.md")
        backup = self.data / "obsidian-backup" / "2030-05-14" / "2030-05-14.20300514T220000Z.md"
        self.assertEqual(self.backups(), [backup])
        self.assertEqual(backup.read_bytes(), original.encode("utf-8"))

        # ---- 実行レポート
        self.assertEqual(result.exit_code, 0)
        report = self.saved_report(result.report_path, result)
        self.assertEqual(result.report_path, self.logs / "diary-report-20300514T220000Z.json")
        self.assertEqual(
            {k: report[k] for k in (
                "status", "today", "planned_dates", "revisit_dates", "processed",
                "remaining_dates", "exit_code", "failures", "notifications",
            )},
            {
                "status": "finished", "today": "2030-05-15", "planned_dates": ["2030-05-14"],
                "revisit_dates": [], "processed": 1, "remaining_dates": [], "exit_code": 0,
                "failures": [], "notifications": {},
            },
        )
        self.assertEqual(report["github"], {
            "status": "complete", "errors": [], "repos_scanned": 2, "repos_failed": 0,
            "repos_listed": 4, "owners_listed": 2, "requests": 5, "cost": 5, "attempts": 1,
        })
        (day,) = report["dates"]
        self.assertEqual(day["date"], "2030-05-14")
        self.assertEqual(day["note"], {
            "status": "updated", "path": str(note), "backup_path": str(backup),
        })
        self.assertEqual(list(day["keys"]), list(ALL_KEYS), "実行レポートは全 23 キーを載せる")
        expected_keys = {key: ("update", raw, None) for key, raw in WRITTEN_0514.items()}
        expected_keys["昼寝時間"] = ("missing", None, "no_value")
        self.assertEqual(keys_view(day), expected_keys)
        self.assertEqual(day["keys"]["歩数 (歩)"]["old"], "")  # テンプレートの空欄を埋めた
        self.assertEqual(day["written_keys"], [k for k in ALL_KEYS if k != "昼寝時間"])
        self.assertEqual(
            (day["provisional"], day["provisional_keys"], day["unresolved"], day["tracked"]),
            (False, [], {}, False),
        )
        health = day["sources"]["health"]
        self.assertEqual(
            (health["status"], health["source"], health["provisional"],
             health["missing_fields"], health["withheld_fields"]),
            ("complete", "automation", False, ["nap_hours"], []),
        )
        self.assertIn(str(exported), health["detail"], "新しい方の候補を採用した")
        self.assertEqual(day["sources"]["weather"], {
            "status": "complete", "is_complete": True, "pending": False,
            "observed_hours": 24, "missing_keys": [],
        })
        self.assertEqual(day["sources"]["github"], {"status": "complete", "items": 5})

        # ---- 確定した日なので台帳は作らない / 通知しない
        self.assertIsNone(self.ledger())
        self.assertEqual(self.notifications, [])

        # ---- 外部 I/O: 何を、どの順で、どの範囲で求めたか
        self.assertEqual(self.materialized.paths, [exported], "読む前に materialize (古い候補は読まない)")
        (jma,) = self.internet.jma_requests
        self.assertEqual(
            (jma["date"], jma["url"], jma["method"]),
            (self.DAY, f"{JMA_URL}?prec_no=44&block_no=47662&year=2030&month=05&day=14&view=p1",
             "GET"),
        )
        self.assertTrue(jma["user_agent"].startswith("ihealth-relay/"), jma["user_agent"])
        self.assertEqual(
            [r["operation"] for r in self.internet.graphql_requests],
            ["IhealthViewer", "IhealthOwnerRepos", "IhealthOwnerRepos",
             "IhealthRepoActivity", "IhealthRepoActivity"],
        )
        # 期間は JST の 0:00〜翌 0:00 を UTC で。archived で古い repo と除外 repo は問い合わせない
        self.assertEqual(self.internet.activity_queries(), [
            ("e2e-sample-user", "notes", "2030-05-13T15:00:00Z", "2030-05-14T15:00:00Z"),
            ("e2e-sample-org", "tool", "2030-05-13T15:00:00Z", "2030-05-14T15:00:00Z"),
        ])
        self.assertEqual(self.token_requests, [None])
        self.assertEqual(self.probes, ["api.github.com"])
        self.assert_token_not_leaked()

        # ---- 同じ日をもう一度 (昼の定時実行): 値が同じなら何も書かない (冪等)
        again = self.run_at(jst(2030, 5, 15, 12, 0), date_arg=self.DAY)
        self.assertEqual(again.exit_code, 0)
        self.assertEqual(note.read_bytes(), expected.encode("utf-8"))
        self.assertEqual(self.backups(), [backup], "書かないのでバックアップも増えない")
        (day,) = self.saved_report(again.report_path, again)["dates"]
        self.assertEqual(day["note"]["status"], "unchanged")
        self.assertEqual(day["written_keys"], [])
        expected_keys = {key: ("unchanged", raw, None) for key, raw in WRITTEN_0514.items()}
        expected_keys["昼寝時間"] = ("missing", None, "no_value")
        self.assertEqual(keys_view(day), expected_keys)
        self.assertIsNone(self.ledger())


# ======================================================================
# 2. JST 0 時跨ぎ: launchd の実行 (引数なし) が JST の日付が変わった直後に走る
# ======================================================================


#: 2030-05-15 (水) に書かれるキー (挿入順 = 日記テンプレート順)。
INSERTED_0515: "list[tuple[str, str]]" = [
    ("歩数 (歩)", "3048"),
    ("睡眠時間 (分)", "426"),
    ("消費カロリー (kcal)", "210"),
    ("移動距離 (km)", "2.4"),
    ("安静時心拍数 (bpm)", "57"),
    ("平均心拍数 (bpm)", "70"),
    ("最大心拍数 (bpm)", "96"),
    ("酸素飽和度 (%)", "97"),
    ("運動強度スコア", "1"),
    # 運動 0 分の日: 実際の HAE はその日の apple_exercise_time 自体を出力しない (qty 0 の
    # record は出てこない) → アクティビティ時間は書かない。アクティビティ詳細は、心拍がある
    # (Watch を着けていた) ので「なし」(W3-7 改訂。旧: 書かない)
    *quoted(WEATHER_0515).items(),
    ("アクティビティ詳細", q("なし")),
    ("Github", q(GITHUB_0515)),
]
#: 2030-05-16 (木, 実行時点の今日) の 0:00〜0:10 の暫定値 (心拍があり、運動・瞑想はまだ無い
#: → アクティビティ詳細も暫定の「なし」)。
INSERTED_0516: "list[tuple[str, str]]" = [
    ("歩数 (歩)", "41"),
    ("睡眠時間 (分)", "6"),
    ("平均心拍数 (bpm)", "66"),
    ("最大心拍数 (bpm)", "69"),
    ("運動強度スコア", "0"),
    ("アクティビティ詳細", q("なし")),
    ("Github", q(GITHUB_0516)),
]
#: 2030-05-16 の天気 (観測値は 2030-05-14 と同じで、天気欄だけ違う)。
WEATHER_0516: "dict[str, str]" = {
    **WEATHER_0514,
    "天気": (
        f"3時: 曇{CLOUD}, 6時: 晴れ{SUN}, 9時: 晴れ{SUN}, 12時: 快晴{SUN}{SUN}, "
        f"15時: 晴れ{SUN}, 18時: 薄曇{PARTLY}, 21時: 曇{CLOUD}"
    ),
}
#: 2030-05-16 の確定値で置き換わった後のノートの frontmatter (1 回目に挿入した行は同じ位置で
#: 値だけ変わり (暫定の「なし」→「エクササイズ18分」も同じ位置)、1 回目に値の無かったキーは
#: 閉じ ``---`` の直前に追記される)。
FINAL_ENTRIES_0516: "list[tuple[str, str]]" = [
    ("歩数 (歩)", "6000"),
    ("睡眠時間 (分)", "414"),
    ("平均心拍数 (bpm)", "97.3"),
    ("最大心拍数 (bpm)", "139"),
    ("運動強度スコア", "3"),
    ("アクティビティ詳細", q("エクササイズ18分")),
    ("Github", q(GITHUB_0516)),
    ("消費カロリー (kcal)", "430.2"),
    ("移動距離 (km)", "4.4"),
    ("安静時心拍数 (bpm)", "58"),
    ("アクティビティ時間 (分)", "18"),
    *quoted(WEATHER_0516).items(),
]


class JstMidnightBoundaryTest(DiaryE2ETestBase):
    """``python3 -m ihealth`` (引数なし) が 2030-05-16 00:30 JST (= 05-15 15:30 UTC) に走る。

    UTC ではまだ 05-15 だが、日記の日付 (JST) では 05-15 は終わった日 (確定値) で 05-16 が
    今日 (暫定値)。健康 record・GitHub の活動は JST の 0 時ちょうどで日が分かれる。
    その後 (05-17 は実行されず) 05-18 07:00 の実行で、既定 window の外に出た 05-16 を台帳から
    処理し直して確定値に置き換える。
    """

    def setUp(self) -> None:
        super().setUp()
        logger = logging.getLogger("ihealth")
        saved = (list(logger.handlers), logger.level, logger.propagate)

        def restore() -> None:
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
            for handler in saved[0]:
                logger.addHandler(handler)
            logger.setLevel(saved[1])
            logger.propagate = saved[2]

        self.addCleanup(restore)
        self.log_stream = io.StringIO()

    def write_env(self) -> None:
        """``<project_root>/.env`` (project_root はテストの temp。``_project_root`` を差し替える)。"""
        env = {
            "HEALTH_EXPORT_DIR": str(self.documents),
            "BIRTH_DATE": "1990-01-01",
            "IHEALTH_PUBLISHER": "obsidian",
            "OBSIDIAN_VAULT_DIR": str(self.vault),
            "OBSIDIAN_TEMPLATE": "テンプレート/毎日の習慣.md",
            "IHEALTH_LOOKBACK_DAYS": "1",
            "GITHUB_EXCLUDE_REPOS": "e2e-sample-org/ignored",
        }
        (self.root / ".env").write_text(
            "".join(f'{key}="{value}"\n' for key, value in env.items()), encoding="utf-8",
        )

    def test_env_gives_the_config_used_by_the_direct_runs(self):
        # 他の条件 (run_diary を直接呼ぶ) が CLI と同じ設定で動いていることの確認
        self.write_env()
        with mock.patch("ihealth.config._project_root", return_value=self.root):
            self.assertEqual(load_config(require_notion=False), self.config())

    def run_cli(self, now: datetime, *argv: str) -> int:
        """``.env`` を書いて ``now`` に ``__main__.main`` を実行する (launchd と同じ入口)。"""
        self.write_env()
        self.time.now = now

        def configure(level: str) -> logging.Logger:
            # logs/run.log を作らず、ログはこのテストの StringIO にだけ出す
            return configure_logger(level, enable_file_handler=False, stream=self.log_stream)

        with contextlib.ExitStack() as stack:
            for target, kwargs in (
                ("ihealth.config._project_root", {"return_value": self.root}),
                ("ihealth.__main__.configure_logger", {"side_effect": configure}),
                ("ihealth.__main__._utc_now", {"side_effect": self.time.clock}),
                ("ihealth.diary._utc_now", {"side_effect": self.time.clock}),
                ("ihealth.diary.time", {"new": SimpleNamespace(
                    monotonic=self.time.monotonic, sleep=self.time.sleep)}),
                ("ihealth.diary.tcp_probe", {"side_effect": self.probe}),
                ("ihealth.diary.resolve_github_token", {"side_effect": self.resolve_token}),
                ("ihealth.diary.notify_failure", {"side_effect": self.notify}),
                ("ihealth.__main__.notify_failure", {"side_effect": self.notify}),
            ):
                stack.enter_context(mock.patch(target, **kwargs))
            return main(list(argv))

    def test_run_after_jst_midnight_closes_yesterday_and_tracks_today_until_final(self):
        # 05-15 のファイルは日付が変わった直後 (00:10) に書き出された = 05-15 の確定版
        # (前後の日の record も含む)。05-16 のファイルは 0:00〜0:10 の途中経過
        day15 = self.export_automation("current", date(2030, 5, 15), jst(2030, 5, 16, 0, 10))
        day16 = self.export_automation("current", date(2030, 5, 16), jst(2030, 5, 16, 0, 10))
        note15, original15 = self.place_note(date(2030, 5, 15))   # screen-diary の最小ノート
        note16, original16 = self.place_note(date(2030, 5, 16))
        self.internet.jma_pages[date(2030, 5, 15)] = "hourly_s1_2030-05-15.html"

        rc = self.run_cli(jst(2030, 5, 16, 0, 30))

        self.assertEqual(rc, 0, self.log_stream.getvalue())
        written15 = insert_entries(original15, INSERTED_0515)
        self.assertEqual(note15.read_bytes(), written15.encode("utf-8"))
        self.assertEqual(
            note16.read_bytes(), insert_entries(original16, INSERTED_0516).encode("utf-8"),
        )
        self.assert_vault_notes("2030-05-15.md", "2030-05-16.md")

        report = self.saved_report(self.logs / "diary-report-20300515T153000Z.json")
        self.assertEqual(
            (report["status"], report["today"], report["planned_dates"], report["revisit_dates"],
             report["exit_code"], report["failures"], report["notifications"]),
            ("finished", "2030-05-16", ["2030-05-15", "2030-05-16"], [], 0, [], {}),
        )
        day15_report, day16_report = report["dates"]

        # ---- 05-15: 終わった日の確定値 (UTC ではまだ 05-15 でも暫定値にしない)
        inserted15 = dict(INSERTED_0515)
        expected15 = {
            key: ("insert", inserted15[key], None) if key in inserted15
            else ("missing", None, "no_value")
            for key in ALL_KEYS
        }
        self.assertEqual(keys_view(day15_report), expected15)
        self.assertEqual(
            (day15_report["provisional"], day15_report["provisional_keys"],
             day15_report["unresolved"], day15_report["tracked"]),
            (False, [], {}, False),
        )
        self.assertEqual(
            (day15_report["sources"]["health"]["status"],
             day15_report["sources"]["health"]["provisional"],
             day15_report["sources"]["weather"]["status"],
             day15_report["sources"]["github"]),
            ("complete", False, "complete", {"status": "complete", "items": 3}),
        )

        # ---- 05-16: 今日の暫定値 (天気は気象庁がまだ公開していないので問い合わせない)
        inserted16 = dict(INSERTED_0516)
        expected16 = {}
        for key in ALL_KEYS:
            if key in inserted16:
                expected16[key] = ("insert", inserted16[key], None)
            elif key in WEATHER_KEYS:
                expected16[key] = ("missing", None, "not_published")
            else:
                expected16[key] = ("missing", None, "no_value")
        self.assertEqual(keys_view(day16_report), expected16)
        self.assertTrue(day16_report["provisional"])
        self.assertEqual(day16_report["provisional_keys"], list(inserted16))
        self.assertEqual(day16_report["unresolved"], {
            **{key: "provisional" for key in ("歩数 (歩)", "睡眠時間 (分)", "平均心拍数 (bpm)",
                                              "最大心拍数 (bpm)", "運動強度スコア",
                                              "アクティビティ詳細")},
            **{key: "weather_pending" for key in WEATHER_KEYS},
            "Github": "provisional",
        })
        self.assertTrue(day16_report["tracked"])
        self.assertEqual(
            (day16_report["sources"]["health"]["provisional"],
             day16_report["sources"]["weather"], day16_report["sources"]["github"]),
            (True, {"status": "nodata", "reason": "not_published"},
             {"status": "complete", "items": 1}),
        )

        # ---- 台帳: 今日 (05-16) の暫定値と公開待ちの天気だけを追跡する
        since = "2030-05-15T15:30:00+00:00"
        self.assertEqual(self.ledger(), {
            "version": 2,
            "updated_at": since,
            "dates": {"2030-05-16": {"keys": {
                **ledger_items("provisional", since, {
                    "歩数 (歩)": 41, "睡眠時間 (分)": 6, "平均心拍数 (bpm)": 66,
                    "最大心拍数 (bpm)": 69, "運動強度スコア": 0, "アクティビティ詳細": "なし",
                    "Github": GITHUB_0516,
                }),
                **ledger_items("weather_pending", since, keys=WEATHER_KEYS),
            }}},
        })

        # ---- 外部 I/O: 気象庁は 05-15 だけ、GitHub は JST の 05-15 0:00〜05-17 0:00
        self.assertEqual([r["date"] for r in self.internet.jma_requests], [date(2030, 5, 15)])
        self.assertEqual(self.internet.activity_queries(), [
            ("e2e-sample-user", "notes", "2030-05-14T15:00:00Z", "2030-05-16T15:00:00Z"),
            ("e2e-sample-org", "tool", "2030-05-14T15:00:00Z", "2030-05-16T15:00:00Z"),
        ])
        self.assertEqual(self.materialized.paths, [day15, day16])
        self.assertEqual((self.probes, self.token_requests, self.notifications),
                         (["api.github.com"], [None], []))

        # ---- 05-18 07:00 (05-17 は Mac がスリープしていて実行されなかった)。HAE は 05-17 00:10 に
        #      05-16 を書き直した (確定)。05-16 は既定 window (05-17〜05-18) の外だが台帳にあるので
        #      処理し直し、暫定値を確定値に置き換え・空だったキーを埋めて、台帳から外す
        self.export_automation("final", date(2030, 5, 16), jst(2030, 5, 17, 0, 10))
        self.internet.jma_pages[date(2030, 5, 16)] = "hourly_s1_2030-05-16.html"

        rc = self.run_cli(jst(2030, 5, 18, 7, 0))

        self.assertEqual(rc, 0, self.log_stream.getvalue())
        self.assertEqual(note15.read_bytes(), written15.encode("utf-8"), "window の外の確定日は触らない")
        self.assertEqual(
            note16.read_bytes(), insert_entries(original16, FINAL_ENTRIES_0516).encode("utf-8"),
        )
        self.assert_vault_notes("2030-05-15.md", "2030-05-16.md")  # 05-17 / 05-18 は作らない
        report = self.saved_report(self.logs / "diary-report-20300517T220000Z.json")
        self.assertEqual(
            (report["today"], report["planned_dates"], report["revisit_dates"],
             report["exit_code"], report["notifications"]),
            ("2030-05-18", ["2030-05-16", "2030-05-17", "2030-05-18"], ["2030-05-16"], 0, {}),
        )
        day16_report = report["dates"][0]
        replaced = {"歩数 (歩)", "睡眠時間 (分)", "平均心拍数 (bpm)", "最大心拍数 (bpm)", "運動強度スコア",
                    "アクティビティ詳細"}
        expected16 = {}
        for key, raw in FINAL_ENTRIES_0516:
            if key in replaced:
                expected16[key] = ("update", raw, None)
            elif key == "Github":
                expected16[key] = ("unchanged", raw, None)
            else:
                expected16[key] = ("insert", raw, None)
        for key in ALL_KEYS:
            expected16.setdefault(key, ("missing", None, "no_value"))
        self.assertEqual(keys_view(day16_report), expected16)
        self.assertEqual(day16_report["keys"]["歩数 (歩)"]["old"], "41")
        self.assertEqual(
            {key: item["resolved"] for key, item in day16_report["keys"].items()
             if "resolved" in item},
            {**{key: "provisional" for key in (*replaced, "Github")},
             **{key: "weather_pending" for key in WEATHER_KEYS}},
        )
        self.assertEqual(
            (day16_report["provisional"], day16_report["unresolved"], day16_report["tracked"]),
            (False, {}, False),
        )
        self.assertEqual(
            [(day["date"], day["note"]) for day in report["dates"][1:]],
            [(d, {"status": "missing", "path": None, "reason": "too_recent"})
             for d in ("2030-05-17", "2030-05-18")],
        )
        self.assertEqual(self.ledger()["dates"], {}, "確定したので台帳から外れる")
        self.assertEqual(
            [r["date"] for r in self.internet.jma_requests],
            [date(2030, 5, 15), date(2030, 5, 16), date(2030, 5, 17)],
        )
        self.assertEqual(self.materialized.paths, [day15, day16, day16])
        self.assertEqual(self.notifications, [])
        self.assert_token_not_leaked()


# ======================================================================
# 3. 日の途中で書き出された HAE の出力: 暫定値として書き、再出力で確定値に置き換える
# ======================================================================


PARTIAL_DAY = date(2030, 5, 20)
#: 2030-05-20 20:39 の書き出し (途中経過) の値。
PROVISIONAL_0520: "dict[str, object]" = {
    "歩数 (歩)": 6000,
    "睡眠時間 (分)": 390,
    "消費カロリー (kcal)": 400.5,
    "移動距離 (km)": 4.4,
    "安静時心拍数 (bpm)": 61,
    "平均心拍数 (bpm)": 92.5,
    "最大心拍数 (bpm)": 125,
    "酸素飽和度 (%)": 96,
    "運動強度スコア": 1,
    "瞑想時間 (分)": 5,
    "瞑想回数 (回)": 1,
    "アクティビティ時間 (分)": 15,
    "体重 (kg)": 64.8,
    "体脂肪率 (%)": 19.2,
    "アクティビティ詳細": "瞑想5分、エクササイズ15分",
}
#: 翌日 20:40 に書き直された (確定) 出力の値。
FINAL_0520: "dict[str, object]" = {
    "歩数 (歩)": 7500,
    "睡眠時間 (分)": 390,
    "消費カロリー (kcal)": 490,
    "移動距離 (km)": 5.5,
    "安静時心拍数 (bpm)": 60,
    "平均心拍数 (bpm)": 105,
    "最大心拍数 (bpm)": 141,
    "酸素飽和度 (%)": 96,
    "運動強度スコア": 3,
    "瞑想時間 (分)": 9,
    "瞑想回数 (回)": 2,
    "アクティビティ時間 (分)": 27,
    "体重 (kg)": 64.6,
    "体脂肪率 (%)": 19.2,
    "アクティビティ詳細": "瞑想9分、エクササイズ27分",
}


def rendered(values: "Mapping[str, object]") -> "dict[str, str]":
    """値 → frontmatter の表記 (文字列は double-quoted、数値はそのまま)。"""
    return {
        key: q(value) if isinstance(value, str) else str(value) for key, value in values.items()
    }


class PartialDayExportTest(DiaryE2ETestBase):
    """2030-05-20 のファイルが 20:39 に書き出されたまま日が変わった (W3-2h / W3-2d)。

    1 回目 (05-21 07:00): 値は暫定値として書き、台帳で追跡する。天気は公開待ち。
    利用者が体重を書き換える。HAE が 05-21 20:40 に 05-20 を書き直す (確定)。
    2 回目 (05-22 07:00): 05-20 は既定 window の外だが台帳から処理し直し、暫定値のままの
    キーを確定値に置き換え (利用者が書き換えた体重は保持)、天気を埋め、台帳から外す。
    """

    def test_mid_day_export_is_provisional_until_the_re_export_finalizes_it(self):
        target = self.export_automation("partial", PARTIAL_DAY, jst(2030, 5, 20, 20, 39))
        note, original = self.place_note(PARTIAL_DAY)
        # 翌朝の時点では前日の気象庁ページは数値だけで、天気欄がまだ埋まっていない
        self.internet.jma_pages[PARTIAL_DAY] = "hourly_s1_2030-05-20.pending.html"

        first = self.run_at(jst(2030, 5, 21, 7, 0))

        self.assertEqual(first.exit_code, 0)
        provisional_text = fill_empty_entries(original, {
            **rendered(PROVISIONAL_0520), "Github": q(GITHUB_0520),
        })
        self.assertEqual(note.read_bytes(), provisional_text.encode("utf-8"))
        self.assert_vault_notes("2030-05-20.md")  # 今日・昨日のノートは作らない (Sync 衝突防止)
        report = self.saved_report(first.report_path, first)
        self.assertEqual(
            (report["planned_dates"], report["revisit_dates"], report["status"]),
            (["2030-05-20", "2030-05-21"], [], "finished"),
        )
        day, today = report["dates"]
        expected = {key: ("update", raw, None) for key, raw in rendered(PROVISIONAL_0520).items()}
        expected.update({key: ("missing", None, "nodata") for key in WEATHER_KEYS})
        expected["昼寝時間"] = ("missing", None, "no_value")
        expected["Github"] = ("update", q(GITHUB_0520), None)
        self.assertEqual(keys_view(day), expected)
        self.assertTrue(day["provisional"], "日が終わっていても途中経過の出力は暫定値")
        self.assertTrue(day["sources"]["health"]["provisional"])
        self.assertIn("暫定値", day["sources"]["health"]["detail"])
        self.assertEqual(day["provisional_keys"], list(PROVISIONAL_0520))
        self.assertEqual(day["unresolved"], {
            **{key: "provisional" for key in PROVISIONAL_0520 if key != "アクティビティ詳細"},
            **{key: "weather_pending" for key in WEATHER_KEYS},
            "アクティビティ詳細": "provisional",
        })
        self.assertEqual(
            (day["sources"]["weather"]["status"], day["sources"]["weather"]["pending"]),
            ("nodata", True),
        )
        self.assertEqual(
            (today["date"], today["note"], today["unresolved"], today["tracked"]),
            ("2030-05-21", {"status": "missing", "path": None, "reason": "too_recent"}, {}, False),
        )
        since = "2030-05-20T22:00:00+00:00"
        self.assertEqual(self.ledger()["dates"], {"2030-05-20": {"keys": {
            **ledger_items("provisional", since, PROVISIONAL_0520),
            **ledger_items("weather_pending", since, keys=WEATHER_KEYS),
        }}})

        # ---- 1 回目と 2 回目の間: 利用者が体重を書き直し、HAE が 05-20 を書き直した (確定)
        edited = provisional_text.replace("体重 (kg): 64.8\n", "体重 (kg): 64.5\n")
        self.assertNotEqual(edited, provisional_text)
        note.write_bytes(edited.encode("utf-8"))
        self.export_automation("final", PARTIAL_DAY, jst(2030, 5, 21, 20, 40))
        self.internet.jma_pages[PARTIAL_DAY] = "hourly_s1_2030-05-20.html"

        second = self.run_at(jst(2030, 5, 22, 7, 0))

        self.assertEqual(second.exit_code, 0)
        final_text = fill_empty_entries(original, {
            **rendered(FINAL_0520),
            "体重 (kg)": "64.5",  # 利用者が書き換えた値は確定値でも上書きしない
            **quoted(WEATHER_0520),
            "Github": q(GITHUB_0520),
        })
        self.assertEqual(note.read_bytes(), final_text.encode("utf-8"))
        self.assert_vault_notes("2030-05-20.md")
        report = self.saved_report(second.report_path, second)
        self.assertEqual(
            (report["planned_dates"], report["revisit_dates"], report["exit_code"],
             report["notifications"]),
            (["2030-05-20", "2030-05-21", "2030-05-22"], ["2030-05-20"], 0, {}),
        )
        day = report["dates"][0]
        changed = {"歩数 (歩)", "消費カロリー (kcal)", "移動距離 (km)", "安静時心拍数 (bpm)",
                   "平均心拍数 (bpm)", "最大心拍数 (bpm)", "運動強度スコア", "瞑想時間 (分)",
                   "瞑想回数 (回)", "アクティビティ時間 (分)", "アクティビティ詳細"}
        final_rendered = rendered(FINAL_0520)
        expected = {
            key: ("update" if key in changed else "unchanged", raw, None)
            for key, raw in final_rendered.items()
        }
        expected["体重 (kg)"] = ("kept", "64.6", None)
        expected.update({key: ("update", raw, None) for key, raw in quoted(WEATHER_0520).items()})
        expected["昼寝時間"] = ("missing", None, "no_value")
        expected["Github"] = ("unchanged", q(GITHUB_0520), None)
        self.assertEqual(keys_view(day), expected)
        self.assertEqual(day["keys"]["体重 (kg)"]["old"], "64.5")
        self.assertEqual(day["keys"]["歩数 (歩)"]["old"], "6000")
        # 確定した (台帳から外れた) キーの理由がレポートに残る
        self.assertEqual(
            {key: item.get("resolved") for key, item in day["keys"].items() if "resolved" in item},
            {**{key: "provisional" for key in PROVISIONAL_0520},
             **{key: "weather_pending" for key in WEATHER_KEYS}},
        )
        self.assertEqual(
            (day["provisional"], day["provisional_keys"], day["unresolved"], day["tracked"]),
            (False, [], {}, False),
        )
        self.assertEqual(self.ledger()["dates"], {}, "確定したので台帳から外れる")

        # 書込前バックアップは 2 回分 (元のノート / 利用者が書き換えた後のノート)
        self.assertEqual([p.read_bytes() for p in self.backups()],
                         [original.encode("utf-8"), edited.encode("utf-8")])
        self.assertEqual(self.materialized.paths, [target, target])
        # 気象庁: 1 回目は 05-20 だけ、2 回目は 05-20 と 05-21 (1 秒以上空ける)
        self.assertEqual(
            [r["date"] for r in self.internet.jma_requests],
            [PARTIAL_DAY, PARTIAL_DAY, date(2030, 5, 21)],
        )
        self.assertEqual(self.time.sleeps, [1.0])
        self.assertEqual(self.notifications, [])
        self.assert_token_not_leaked()


# ======================================================================
# 4. 欠測: 値の無いキー・書き換えられない entry・取得が不完全な source は既存の値を保持する
# ======================================================================


MISSING_DAY = date(2030, 5, 25)
WRITTEN_0525: "dict[str, str]" = {
    "歩数 (歩)": "6321",
    "消費カロリー (kcal)": "323.4",
    "移動距離 (km)": "4.5",
    "安静時心拍数 (bpm)": "62",
    "平均心拍数 (bpm)": "82.5",
    "最大心拍数 (bpm)": "101",
    "運動強度スコア": "1",
    **quoted(WEATHER_0525),
}
#: 2030-05-25 のアクティビティ詳細。家では Watch を着けていた (朝夕の心拍あり) ので、瞑想・
#: エクササイズの記録が無い = 運動 0 分の「なし」を書き、ノートの既存の値を上書きする
#: (W3-7 改訂。旧: 運動のデータが無いので書かない = 既存の "ヨガ30分" を保持)。
ACTIVITY_0525_BEFORE = 'アクティビティ詳細: "ヨガ30分"\n'
ACTIVITY_0525_AFTER = 'アクティビティ詳細: "なし"\n'


class MissingDataTest(DiaryE2ETestBase):
    """2030-05-25: Watch を着けずに外出した日 (睡眠・SpO2・瞑想・エクササイズの記録なし,
    体重は空), 気象庁の湿度・日照・降水量が欠測, GitHub の 1 repo が権限エラー。

    家にいる間の心拍はある (Watch を着けていた) ので、アクティビティ詳細は「なし」になる。
    """

    def test_missing_values_keep_existing_entries_until_sources_complete(self):
        exported = self.export_automation("current", MISSING_DAY, jst(2030, 5, 26, 6, 40))
        note, original = self.place_note(MISSING_DAY)
        self.internet.jma_pages[MISSING_DAY] = "hourly_s1_2030-05-25.html"
        self.internet.github_errors["e2e-sample-org/tool"] = "error-forbidden.json"

        first = self.run_at(jst(2030, 5, 26, 7, 0), date_arg=MISSING_DAY)

        # ---- 値のあるキーだけ書く。値の無いキー・コメント付きの entry・取得が不完全な
        #      GitHub は既存の値 (利用者が書いた値 / 空欄) を 1 byte も変えない。
        #      アクティビティ詳細は値 (「なし」) があるので既存の値を上書きする
        self.assertEqual(original.count(ACTIVITY_0525_BEFORE), 1)
        expected_text = fill_empty_entries(original, WRITTEN_0525).replace(
            ACTIVITY_0525_BEFORE, ACTIVITY_0525_AFTER,
        )
        self.assertEqual(note.read_bytes(), expected_text.encode("utf-8"))
        for kept in ("体重 (kg): 65.5\n", "体脂肪率 (%): 21.0 # 体組成計の不調で手入力\n",
                     '湿度: "朝:平均60.0%, 昼:平均50.0%, 夜:平均70.0%（最高:80%, 最低:40%）"\n',
                     'Github: "該当なし"\n', "睡眠時間 (分):\n", "日照時間:\n", "降水量:\n"):
            self.assertIn(kept, expected_text)  # 期待値自体が保持を表している
        self.assertIn(ACTIVITY_0525_AFTER, expected_text)

        # ---- 実行レポート: 取得が不完全な GitHub は失敗 (exit 12 + 1 通の通知)
        self.assertEqual(first.exit_code, 12)
        report = self.saved_report(first.report_path, first)
        error = (
            "e2e-sample-org/tool: GitHub GraphQL エラー: "
            "Resource not accessible by personal access token"
        )
        message = f"GitHub: 一部の repo を取得できませんでした ({error})"
        self.assertEqual(report["failures"], [
            {"date": None, "source": "github", "kind": "source", "message": message},
        ])
        self.assertEqual(
            (report["github"]["status"], report["github"]["repos_scanned"],
             report["github"]["repos_failed"], report["github"]["errors"]),
            ("partial", 1, 1, [error]),
        )
        (day,) = report["dates"]
        expected = {key: ("update", raw, None) for key, raw in WRITTEN_0525.items()}
        expected["体脂肪率 (%)"] = ("kept", "20.4", None)
        expected["アクティビティ詳細"] = ("update", q("なし"), None)
        expected["Github"] = ("failed", None, "source_incomplete")
        for key in ALL_KEYS:
            expected.setdefault(key, ("missing", None, "no_value"))
        self.assertEqual(keys_view(day), expected)
        self.assertEqual(day["keys"]["アクティビティ詳細"]["old"], q("ヨガ30分"))
        body_fat = day["keys"]["体脂肪率 (%)"]
        self.assertEqual(body_fat["old"], "21.0 # 体組成計の不調で手入力")
        self.assertTrue(body_fat["warning"].startswith("comment present"), body_fat["warning"])
        self.assertEqual(day["sources"]["health"]["missing_fields"], [
            "exercise_minutes", "oxygen_saturation", "sleep_hours", "nap_hours",
            "mindful_sessions", "mindful_minutes", "body_mass_kg",
        ])
        self.assertEqual(day["sources"]["health"]["status"], "complete", "欠測は失敗ではない")
        self.assertEqual(day["sources"]["weather"], {
            "status": "partial", "is_complete": True, "pending": False, "observed_hours": 24,
            "missing_keys": ["humidity", "precipitation", "sunshine"],
        })
        self.assertEqual(day["sources"]["github"]["status"], "partial")
        self.assertEqual(
            (day["provisional"], day["unresolved"], day["tracked"]),
            (False, {"Github": "github_incomplete"}, True),
        )
        since = "2030-05-25T22:00:00+00:00"
        self.assertEqual(self.ledger()["dates"], {
            "2030-05-25": {"keys": ledger_items("github_incomplete", since, keys=("Github",))},
        })
        self.assertEqual(len(self.notifications), 1)
        title, body = self.notifications[0]
        self.assertEqual(title, "ihealth-relay: 日記の一部取得失敗 (github)")
        self.assertIn("[github] 2030-05-25 (期間全体)\n", body)
        self.assertIn(message, body)
        self.assertIn(str(first.report_path), body)
        self.assertEqual(report["notifications"], {"failure": {
            "title": title, "suppressed": False, "delivered": True,
        }})
        self.assertEqual(self.materialized.paths, [exported])

        # ---- GitHub が直った後の実行: Github だけが埋まり、台帳から外れる
        self.internet.github_errors.clear()
        second = self.run_at(jst(2030, 5, 26, 12, 0), date_arg=MISSING_DAY)

        self.assertEqual(second.exit_code, 0)
        final_text = expected_text.replace('Github: "該当なし"\n', f"Github: {q(GITHUB_0525)}\n")
        self.assertNotEqual(final_text, expected_text)
        self.assertEqual(note.read_bytes(), final_text.encode("utf-8"))
        (day,) = self.saved_report(second.report_path, second)["dates"]
        self.assertEqual(keys_view(day)["Github"], ("update", q(GITHUB_0525), None))
        self.assertEqual(day["keys"]["Github"]["resolved"], "github_incomplete")
        self.assertEqual(keys_view(day)["体脂肪率 (%)"], ("kept", "20.4", None))
        self.assertEqual(
            {key for key, item in day["keys"].items() if item["action"] == "unchanged"},
            {*WRITTEN_0525, "アクティビティ詳細"},
        )
        self.assertEqual((day["unresolved"], day["tracked"]), ({}, False))
        self.assertEqual(self.ledger()["dates"], {})
        self.assertEqual(len(self.notifications), 1, "直った実行では通知しない")
        self.assert_vault_notes("2030-05-25.md")
        self.assert_token_not_leaked()


if __name__ == "__main__":
    unittest.main()
