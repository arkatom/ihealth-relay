"""GitHub 活動 (Issue close / PR merge / default branch 直コミット) の日別収集。

GitHub GraphQL API (``https://api.github.com/graphql``) を ``urllib.request`` だけで
叩く。外部 PyPI 依存ゼロ。日記 frontmatter の ``Github`` キーに書く文字列を、前身
(GoogleFitNotionIntegration の ``github_notion.py``) と完全互換の書式で組み立てる。

## 「本人の活動」の定義 (前身互換)

自分が owner の repo + 所属 org の repo における、全作者の

- Issue close (``closedAt`` が対象日)
- PR merge (``mergedAt`` が対象日)
- default branch への直コミット (``committedDate`` が対象日。merge commit / PR
  ブランチ上の commit / bot の commit は除外)

``owners`` を渡すとその owner (user / org) の repo だけに絞る (``GITHUB_OWNERS``)。
``owners`` / ``exclude_repos`` は list のほか **カンマ区切りの str** も受け付ける
(``.env`` の値をそのまま渡しても 1 文字ずつ owner 扱いされない。list の各要素も
カンマで分割する)。前後空白・空要素は捨て、大文字小文字無視で重複除去する。
GitHub の login として不正な owner (``/`` ・空白・URL など) や ``owner/name`` 形式で
ない exclude は **API を呼ぶ前に** ``ValueError`` (黙って範囲が変わった値を書かない)。

例外: **fork repo の commit は viewer 本人が author のものだけ数える**
(``fork_all_authors=False`` 既定)。fork の default branch は upstream の同期 (Sync fork /
fast-forward) や fork 直後の状態で他人の commit が **元の committer date のまま** 入って
くるため、lookback 再実行や backfill で過去日に upstream の変更行数が大量に載ってしまう。
判定は ``author.user.login`` と viewer の login の一致 (大文字小文字無視) なので、GitHub
アカウントに紐付いていないメールで作った本人 commit は fork では数えない。fork の
Issue / PR (fork 自身のもの) は従来どおり全作者。``fork_all_authors=True`` で SPEC
どおり全作者に戻せる。``owners`` 指定時は viewer の login を知らないので、fork が対象に
入ったときだけ 1 request で取得する (取れなければ fork repo だけ失敗扱い)。

## repo 列挙 (SPEC §2-C からの意図的な逸脱あり)

``viewer.repositories(ownerAffiliations: [OWNER, ORGANIZATION_MEMBER])`` は **org の
private repo を返さない** ことがある (実測: org owner (admin) の OAuth token でも、team /
base permission 経由でない private repo は返らず公開 repo だけが返った)。そのため owner ごとに
``repositoryOwner(login).repositories(ownerAffiliations: [OWNER])`` を全 page 取得する
(owner = viewer 自身 + ``viewer.organizations``)。前身の REST 実装
(``/user/repos?affiliation=owner`` + ``/orgs/{org}/repos``) と同じ範囲になる。

絞り込みは ``max(pushedAt, updatedAt) >= 期間開始`` のみ。**archived repo も含める**
(``include_archived=True`` 既定): archive 前の活動を backfill で取りこぼさないため
(実測: 前身が書いた過去日の値に、後日 archive された repo の commit 行が含まれていた)。
archive 後は push できないので、日次運用では時刻条件で自然に対象外になる。

## commit の除外規則

``history(since:, until:)`` は git log 相当で **全 parent を辿る** (PR を merge commit で
取り込むと PR ブランチの commit も default branch の history に現れる) うえ、since /
until は **committer date** 基準 (実測確認済み)。日付への bucket も ``committedDate``。

- ``parents.totalCount > 1`` (merge commit) → 除外
- bot (``author.user.login`` が ``[bot]`` で終わる / ``author.name`` が ``[bot]`` を含む /
  ``author.email`` が ``[bot]@`` を含む) → 除外 (``include_bots=True`` なら含める)
- PR ブランチ上の commit → 除外。判定: ``associatedPullRequests`` に merged かつ
  ``mergeCommit.oid != この commit`` かつ ``mergeCommit`` が **merge commit (parent 2 以上)**
  の PR がある。``mergeCommit`` 不明 (null) の merged PR も保守的に除外側。
- squash merge の commit (``mergeCommit.oid == この commit``) と、rebase merge で
  default branch に載った commit (PR の ``mergeCommit`` の parent が 1) は **直コミットとして
  数える**。前身 (PR の元 commit SHA 集合で除外) と同じ結果になる
  (例: 3 PR を squash merge した日 → ``変更行数 (3 commits)``)。

## 並び順 (SPEC §2-C からの意図的な逸脱)

その日の items を **種別 (issue → PR → commit) → repo → number 降順** に並べる。repo の
順序は「**その日のその repo の最終イベント時刻** (表示する Issue の ``closedAt`` / PR の
``mergedAt`` / 数えた commit の ``committedDate`` の最大。種別をまたいで共通) の降順、
同時刻は ``owner/name`` 昇順 (大文字小文字無視)」。SPEC の「repo 順 = (現在の) pushedAt
降順」は、lookback 再実行 (1 日 3 回 × 過去 7 日) のたびに **後日の無関係な push で過去日の
行順が入れ替わり**、内容が同じなのにノートが書き換わる (= バックアップ・Sync・レポート上の
update が毎回出る) ため採らない。この順序はその日のデータだけで決まるので、内容が変わら
ない限り文字列も変わらない (前身の「翌朝の実行時点の updated_at 順」にも近い)。

## 完全性 (SPEC §4.4)

:class:`GitHubActivity` の ``complete`` は「列挙・全 repo の全 connection を最後まで
取得できた」ときだけ ``True``。repo 列挙失敗 / いずれかの repo の取得失敗 /
pagination 上限での打ち切りは ``False`` (``errors`` に理由)。呼び出し側は
``complete=False`` のとき ``Github`` キーを書かない (既存値保持)。
``complete=True`` かつ活動なしの日だけ ``"該当なし"`` を書く (:meth:`GitHubActivity.text_for`)。
``status`` は通知・レポート用に ``complete`` / ``partial`` (一部でも取れた) / ``failed``
(viewer 取得失敗・全 owner の列挙失敗・走査が必要な repo が全滅のいずれか) を返す。

## GitHub 側 timeout への対処

GitHub GraphQL はサーバ側でおよそ 10 秒を超えたクエリを打ち切る (HTTP 502/504 や
``"Something went wrong ... timeout"``)。default branch history の 1 page (100 commit 分の
``additions``/``deletions`` = diff 統計) は巨大 commit を含むと超えることがある (実測:
長期 backfill の 1 page 目で 10.8 秒 → 502)。GitHub は打ち切られた計算も cache するので、
history を含む request は、まず同じクエリを 1 回だけ再送し、それでも timeout なら **同じ
cursor から page 件数を 100 → 50 → 25 → 10 と縮小** して取り直す (最小件数では通常どおり
最大 3 回再試行)。縮小した件数はその repo の残りの page にも使う (同じ repo で timeout を
繰り返さない)。pagination 上限は history だけ「既定 page 件数 (100) 換算」で数える。
1 repo の 1 page が全段階で失敗したときだけ、その repo を失敗 (= ``complete=False``) とする。
500 / 503 / 接続エラー / レート制限は timeout とみなさず縮小しない (通常の再試行回数のまま)。

## 秘密情報

token は ``Authorization`` ヘッダにだけ載せる。ログ・例外メッセージ・``errors`` には
一切出さない (HTTP エラーは status と GitHub の ``message`` 本文のみ)。空白・制御文字
(改行など)・非 ASCII を含む token は受け付けない (``http.client`` がヘッダ検証で token
入りの ``ValueError`` を投げるため)。送信時の ``ValueError`` も型名だけの
:class:`GitHubError` に置き換える。
"""

from __future__ import annotations

import email.utils
import http.client
import json
import logging
import math
import random
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from datetime import time as dt_time
from typing import Callable, Iterable, Sequence

from ihealth import __version__


#: 日本標準時。日記の日付境界 (0:00) の既定 tz。
JST = timezone(timedelta(hours=9))

#: GitHub GraphQL endpoint (github.com)。
GITHUB_GRAPHQL_ENDPOINT = "https://api.github.com/graphql"

#: ``gh auth token`` を探す候補。launchd 起動時は PATH が最小
#: (``/usr/bin:/bin:/usr/sbin:/sbin``) なので Homebrew の絶対パスを先に試す。
DEFAULT_GH_CANDIDATES: "tuple[str, ...]" = (
    "/opt/homebrew/bin/gh",
    "/usr/local/bin/gh",
    "gh",
)

#: 活動なし (complete=True かつ items 空) の日に書く文字列 (前身互換)。
NO_ACTIVITY_TEXT = "該当なし"

#: User-Agent (GitHub API は UA 必須)。
USER_AGENT = f"ihealth-relay/{__version__} (+https://github.com/arkatom/ihealth-relay)"

# ---------- 取得量の上限 ----------

#: owner ごとの repo 一覧 1 page の件数 (GraphQL の上限 100)。
_REPO_PAGE_SIZE = 100
#: closed issue / merged PR の 1 page 件数。
_ISSUE_PAGE_SIZE = 50
_PR_PAGE_SIZE = 50
#: default branch history の 1 page 件数の段階。先頭が既定。GitHub 側 timeout
#: (HTTP 502/504 / GraphQL の timeout 系エラー) のたびに次の段階へ縮小して同じ cursor から
#: 取り直す (モジュール docstring「GitHub 側 timeout への対処」)。
_HISTORY_PAGE_SIZES: "tuple[int, ...]" = (100, 50, 25, 10)
_HISTORY_PAGE_SIZE = _HISTORY_PAGE_SIZES[0]
#: 縮小できる段階で、縮小前に同じ page 件数で再送する回数 (GitHub は timeout した計算も
#: cache するので同一クエリの再送で通ることが多い。実測: 502 (10.8s) → 200 (5.0s))。
#: GitHub 側 timeout (502/504 / GraphQL timeout 系) にだけ効く (他の再試行は減らさない)。
_SHRINK_TIMEOUT_RETRIES = 1
#: commit ごとの associatedPullRequests 取得件数 (SPEC §4.4)。
_ASSOC_PR_FIRST = 10
#: 1 connection あたりの最大 page 数。超えたら打ち切り = complete=False (cursor 異常時の
#: 無限ループ防止)。issue/PR 50 × 100 = 5000 件, commit 100 × 100 = 10000 件。
#: history は page を縮小しうるので「既定 page 件数 (100) 換算」で数える (= 取得要求した
#: commit 数が ``max_pages × 100`` に達したら打ち切り)。
#: 日次運用では 1-2 page。数か月分の backfill を 1 回の呼び出しで取っても足りる量にしてある
#: (issue/PR は「期間開始以降に更新された全件」を辿るので、期間を細切れにして何度も
#: 呼ぶより、backfill 全期間を 1 回で取るほうが request 数は少ない)。
_MAX_PAGES_PER_CONNECTION = 100
#: owner あたりの repo 一覧の最大 page 数 (100 × 50 = 5000 repo)。
_MAX_REPO_PAGES = 50
#: viewer.organizations の最大 page 数 (100 × 10 = 1000 org)。
_MAX_ORG_PAGES = 10

# ---------- 引数検証 ----------

#: GitHub の user / org の login (英数字始まり、英数字・``-``、最大 39 文字。Enterprise
#: Managed User の ``_`` も許す)。``owner/name`` や URL・空白入りの設定ミスを弾く用途。
_LOGIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}")
#: GitHub の repo 名 (英数字・``.`` ・ ``-`` ・ ``_``、最大 100 文字)。
_REPO_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")

# ---------- HTTP / リトライ ----------

_DEFAULT_TIMEOUT_SEC = 30.0
#: 再試行回数 (初回を含まない)。SPEC「最大 3 回待って再試行」。
_DEFAULT_MAX_RETRIES = 3
#: 1 回の待機の上限秒。Retry-After / rate limit reset がこれを超える場合は待たずに失敗
#: (1 時間後リセットの primary rate limit を launchd job で待ち続けない)。
_DEFAULT_MAX_WAIT_SEC = 60.0
#: 5xx / 接続エラー / GraphQL 一時エラーの指数バックオフ初期値 (秒)。
_BASE_BACKOFF_SEC = 2.0
#: secondary rate limit (Retry-After 無し) の待機初期値 (秒)。GitHub は「最低 1 分」推奨だが
#: 15 → 30 → 60 と段階的に延ばす。
_SECONDARY_RATE_LIMIT_BASE_SEC = 15.0
#: エラーメッセージに含める GitHub 応答 message の最大長。
_MAX_ERROR_MESSAGE_LEN = 300
#: 指数バックオフで再試行する HTTP ステータス (サーバ側の一時エラー)。
_RETRYABLE_STATUS = frozenset({500, 502, 503, 504})
#: GitHub 側のクエリ timeout とみなす HTTP ステータス (GraphQL は ~10 秒で打ち切り 502/504)。
_SERVER_TIMEOUT_STATUS = frozenset({502, 504})

#: GraphQL ``errors[].message`` のうち一時的 (再試行で回復しうる) とみなすもの。
_TRANSIENT_GRAPHQL_MARKERS = (
    "something went wrong",
    "timeout",
    "timed out",
)

_logger = logging.getLogger(__name__)


# ======================================================================
# 例外 / token 解決
# ======================================================================


class GitHubError(RuntimeError):
    """GitHub API 呼び出しの失敗 (HTTP / 接続 / GraphQL errors / 応答形式不正)。

    メッセージには token を含めない (HTTP status と GitHub の ``message`` のみ)。

    Attributes:
        status: HTTP ステータス (HTTP 以外の失敗は ``None``)
        error_types: GraphQL ``errors[].type`` の一覧 (例 ``("NOT_FOUND",)``)
        data: GraphQL の partial ``data`` (errors と同時に返った場合のみ)
        rate_limited: レート制限起因なら ``True``
        server_timeout: GitHub 側のクエリ timeout と見られる失敗 (HTTP 502/504 /
            GraphQL の ``"Something went wrong"`` ・ timeout 系エラー) なら ``True``。
            クエリを軽くすれば (page 縮小) 通る見込みがある
    """

    def __init__(
        self,
        message: str,
        *,
        status: "int | None" = None,
        error_types: "Sequence[str]" = (),
        data: "dict | None" = None,
        rate_limited: bool = False,
        server_timeout: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error_types = tuple(error_types)
        self.data = data
        self.rate_limited = rate_limited
        self.server_timeout = server_timeout


def _is_valid_token(token: str) -> bool:
    """HTTP ヘッダにそのまま載せられる token か (印字可能 ASCII のみ・空白なし)。

    GitHub の token は ``[A-Za-z0-9_]`` だが将来の形式変更に備えて緩く判定する。改行・
    制御文字が混ざると ``http.client`` が token 入りの ``ValueError`` を、非 ASCII は
    ``UnicodeEncodeError`` を送信時に投げるので、受け付ける前に弾く。
    """
    return bool(token) and all(0x21 <= ord(ch) <= 0x7E for ch in token)


def resolve_github_token(
    env_token: "str | None",
    logger: "logging.Logger | None" = None,
    *,
    gh_candidates: "Sequence[str]" = DEFAULT_GH_CANDIDATES,
    runner: "Callable[..., subprocess.CompletedProcess]" = subprocess.run,
    hostname: str = "github.com",
    timeout: float = 10.0,
) -> "str | None":
    """GitHub token を解決する。``env_token`` → ``gh auth token`` の順。

    - ``env_token`` (``.env`` の ``GITHUB_TOKEN``) が空白除去後に非空ならそれを返す
    - ただし内部に空白・制御文字 (改行など)・非 ASCII を含む ``env_token`` は使わず
      ``None`` (warning)。明示設定された token が壊れているのに ``gh`` の (権限範囲の
      違いうる) token へ黙って切り替えないため、``gh`` にも fallback しない
    - 無ければ ``gh_candidates`` を順に ``gh auth token --hostname <hostname>`` で試す
      (timeout 10s, stdin=DEVNULL = 対話プロンプトで固まらない)。``hostname`` を明示するのは
      ``GH_HOST`` が別ホストを指す環境でも github.com の token を取るため。
    - 全滅なら ``None`` (warning)。**token の値はログに一切出さない** (終了コードのみ)。
    """
    log = logger or _logger
    if env_token is not None:
        stripped = env_token.strip()
        if stripped:
            if not _is_valid_token(stripped):
                log.warning(
                    "GITHUB_TOKEN に空白・制御文字・非 ASCII 文字が含まれるため使用しません "
                    "(値は表示しません。.env の GITHUB_TOKEN を確認)"
                )
                return None
            log.debug("GitHub token: 環境変数 (GITHUB_TOKEN) を使用")
            return stripped
    for gh in gh_candidates:
        try:
            proc = runner(
                [gh, "auth", "token", "--hostname", hostname],
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except FileNotFoundError:
            log.debug("gh が見つかりません: %s", gh)
            continue
        except subprocess.TimeoutExpired:
            log.warning("gh auth token がタイムアウト (%.0fs): %s", timeout, gh)
            continue
        except OSError as exc:
            log.warning("gh auth token を実行できません (%s): %s", type(exc).__name__, gh)
            continue
        if proc.returncode != 0:
            log.warning(
                "gh auth token が失敗 (exit=%s): %s (gh auth login 済みか確認)",
                proc.returncode, gh,
            )
            continue
        token = (proc.stdout or "").strip()
        if not _is_valid_token(token):
            log.warning("gh auth token の出力が token 形式ではありません: %s", gh)
            continue
        log.debug("GitHub token: gh auth token (%s) を使用", gh)
        return token
    log.warning(
        "GitHub token を取得できません (GITHUB_TOKEN 未設定 / gh auth token 失敗)"
    )
    return None


# ======================================================================
# GraphQL クライアント
# ======================================================================


def _default_opener(req: "urllib.request.Request", timeout: float) -> bytes:
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https 固定)
        return resp.read()


class GitHubClient:
    """GitHub GraphQL API の薄いラッパー (retry / rate limit / 使用量カウンタ付き)。

    ``opener(request, timeout) -> bytes`` を注入できる (テストはネットワークに出ない)。
    非 2xx は ``urllib.error.HTTPError`` を raise する契約 (``urlopen`` と同じ)。

    使用量: :attr:`request_count` (HTTP 試行回数) と :attr:`cost_total`
    (応答の ``rateLimit.cost`` 合計 = 消費 point) を数える。
    """

    def __init__(
        self,
        token: str,
        *,
        logger: "logging.Logger | None" = None,
        timeout: float = _DEFAULT_TIMEOUT_SEC,
        opener: "Callable[[urllib.request.Request, float], bytes] | None" = None,
        endpoint: str = GITHUB_GRAPHQL_ENDPOINT,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        max_wait_sec: float = _DEFAULT_MAX_WAIT_SEC,
        sleep: "Callable[[float], None]" = time.sleep,
        clock: "Callable[[], float]" = time.time,
    ) -> None:
        if not isinstance(token, str) or not token.strip():
            raise ValueError("GitHub token が空です")
        if not _is_valid_token(token.strip()):
            # 値はメッセージに入れない (改行入り token はヘッダ送信時に token 入りの
            # ValueError になるので、ここで型名だけの例外にして弾く)
            raise ValueError(
                "GitHub token に空白・制御文字・非 ASCII 文字が含まれます (値は表示しません)"
            )
        self._token = token.strip()
        self._logger = logger or _logger
        self._timeout = float(timeout)
        self._opener = opener or _default_opener
        self._endpoint = endpoint
        self._max_retries = max(0, int(max_retries))
        self._max_wait_sec = float(max_wait_sec)
        self._sleep = sleep
        self._clock = clock
        self.request_count = 0
        self.cost_total = 0
        self.last_remaining: "int | None" = None

    def __repr__(self) -> str:  # token を repr に出さない
        return f"GitHubClient(endpoint={self._endpoint!r})"

    def graphql(
        self,
        query: str,
        variables: "dict | None" = None,
        *,
        timeout_retries: "int | None" = None,
    ) -> dict:
        """GraphQL を 1 回実行して ``data`` dict を返す。

        - HTTP 失敗 / ``errors`` 付き応答 / JSON 不正 → :class:`GitHubError`
          (``errors`` と同時に返った partial data は ``GitHubError.data`` に載せる)
        - 403 / 429 のレート制限 (``Retry-After`` / ``x-ratelimit-reset``) は最大
          ``max_retries`` 回まで待って再試行。1 回の待機が ``max_wait_sec`` を超えるなら待たずに失敗
        - 500/502/503/504・接続エラー・GraphQL の一時エラー ("Something went wrong" 等) は
          指数バックオフで最大 ``max_retries`` 回再試行
        - ``timeout_retries`` を渡すと、そのうち **GitHub 側 timeout と見られる失敗**
          (HTTP 502/504 / GraphQL の timeout 系) の再試行回数だけをそれ以下に絞る
          (500/503・接続エラー・レート制限の再試行は絞らない)。呼び出し側が page 縮小で
          自前に取り直すとき用
        - GitHub 側 timeout と見られる失敗で諦めた場合は ``GitHubError.server_timeout=True``
        - 送信前の ``ValueError`` (ヘッダ値・URL の不正) は再試行せず、元の例外文 (ヘッダ値 =
          token を含みうる) を一切残さない :class:`GitHubError` にする
        """
        request_body = json.dumps(
            {"query": query, "variables": variables or {}}, ensure_ascii=False,
        ).encode("utf-8")
        attempts = self._max_retries + 1
        timeout_budget = self._max_retries
        if timeout_retries is not None:
            timeout_budget = max(0, min(int(timeout_retries), self._max_retries))
        timeout_failures = 0
        for attempt in range(1, attempts + 1):
            req = urllib.request.Request(
                self._endpoint,
                data=request_body,
                method="POST",
                headers={
                    "Authorization": f"bearer {self._token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )
            self.request_count += 1
            unsendable: "str | None" = None
            raw = b""
            try:
                raw = self._opener(req, self._timeout)
            except urllib.error.HTTPError as exc:
                error_body = _read_error_body(exc)
                wait, too_long = self._retry_wait(exc, error_body, attempt)
                if exc.code in _SERVER_TIMEOUT_STATUS:
                    timeout_failures += 1
                    if timeout_failures > timeout_budget:
                        wait = None
                if wait is not None and attempt < attempts:
                    self._logger.warning(
                        "GitHub API HTTP %d, %.1fs 後に再試行 (%d/%d)",
                        exc.code, wait, attempt, self._max_retries,
                    )
                    self._sleep(wait)
                    continue
                raise self._http_error(exc.code, error_body, too_long) from None
            except (OSError, http.client.HTTPException) as exc:
                # OSError は URLError / socket.timeout / TimeoutError / ConnectionError /
                # ssl.SSLError (read 中の TLS 断) をまとめて含む (HTTPError は上で処理済み)。
                # クライアント側 timeout (30s) は GitHub 側 timeout (~10s で 502) ではないので
                # timeout_retries の対象外 (page 縮小では直らない)
                if attempt < attempts:
                    wait = _backoff_sec(attempt)
                    self._logger.warning(
                        "GitHub API 接続エラー (%s), %.1fs 後に再試行 (%d/%d)",
                        type(exc).__name__, wait, attempt, self._max_retries,
                    )
                    self._sleep(wait)
                    continue
                raise GitHubError(
                    f"GitHub API 接続エラー ({type(exc).__name__}): {_reason(exc)}"
                ) from None
            except ValueError as exc:
                # http.client のヘッダ検証 (改行入り token 等) / URL 不正 / 非 latin-1 の
                # ヘッダ値 (UnicodeEncodeError)。例外文に Authorization ヘッダ値 = token が
                # 入るので型名だけ残し、except の外で投げ直す (__context__ にも残さない)。
                # 同じ要求は何度送っても同じなので再試行しない。
                unsendable = type(exc).__name__
            if unsendable is not None:
                raise GitHubError(
                    f"GitHub API リクエストを送信できません ({unsendable}; "
                    "token / endpoint の形式を確認)"
                )

            payload = self._decode(raw)
            self._account(payload)
            errors = payload.get("errors")
            if errors:
                gql_error = _graphql_error(errors, payload.get("data"))
                if gql_error.server_timeout:
                    timeout_failures += 1
                    if attempt < attempts and timeout_failures <= timeout_budget:
                        wait = _backoff_sec(attempt)
                        self._logger.warning(
                            "GitHub GraphQL 一時エラー, %.1fs 後に再試行 (%d/%d): %s",
                            wait, attempt, self._max_retries, gql_error,
                        )
                        self._sleep(wait)
                        continue
                raise gql_error
            data = payload.get("data")
            if not isinstance(data, dict):
                raise GitHubError(
                    f"GitHub GraphQL 応答の data が dict ではありません: {type(data).__name__}"
                )
            return data
        # ループは必ず return / raise する (型チェッカー向け)
        raise GitHubError("GitHub API 再試行上限に到達")  # pragma: no cover

    # ---------- 内部ヘルパー ----------

    def _decode(self, raw: bytes) -> dict:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubError(f"GitHub GraphQL 応答の JSON パースに失敗: {exc}") from None
        if not isinstance(payload, dict):
            raise GitHubError(
                f"GitHub GraphQL 応答が dict ではありません: {type(payload).__name__}"
            )
        return payload

    def _account(self, payload: dict) -> None:
        """応答の ``data.rateLimit`` があれば消費 point / 残量を記録する。"""
        data = payload.get("data")
        if not isinstance(data, dict):
            return
        rate = data.get("rateLimit")
        if not isinstance(rate, dict):
            return
        cost = rate.get("cost")
        if isinstance(cost, int) and not isinstance(cost, bool):
            self.cost_total += cost
        remaining = rate.get("remaining")
        if isinstance(remaining, int) and not isinstance(remaining, bool):
            self.last_remaining = remaining
        self._logger.debug(
            "GitHub GraphQL: cost=%s remaining=%s", rate.get("cost"), rate.get("remaining"),
        )

    def _retry_wait(
        self, exc: "urllib.error.HTTPError", body: str, attempt: int,
    ) -> "tuple[float | None, float | None]":
        """HTTPError を見て (再試行までの待機秒, 上限超過で諦めた待機秒) を返す。

        - 500/502/503/504 → 指数バックオフ
        - 403/429 はレート制限の兆候 (Retry-After / x-ratelimit-remaining=0 / 429 /
          本文の "rate limit") があるときだけ待つ。兆候の無い 403 は権限エラーとして即失敗
        - 必要な待機が ``max_wait_sec`` を超える (primary rate limit の reset が遠い等) →
          待たずに失敗 (2 つ目の値に必要秒を入れて返す)
        """
        code = exc.code
        if code in _RETRYABLE_STATUS:
            return _backoff_sec(attempt), None
        if code not in (403, 429):
            return None, None
        wait: "float | None" = None
        retry_after = _parse_retry_after(_header(exc, "Retry-After"), self._clock)
        if retry_after is not None:
            wait = retry_after
        elif _header(exc, "x-ratelimit-remaining") == "0":
            reset = _parse_float(_header(exc, "x-ratelimit-reset"))
            wait = max(1.0, reset - self._clock()) if reset is not None else self._max_wait_sec
        elif code == 429 or "rate limit" in body.lower():
            wait = min(
                self._max_wait_sec,
                _SECONDARY_RATE_LIMIT_BASE_SEC * (2 ** (attempt - 1)),
            )
        if wait is None:
            return None, None
        if wait > self._max_wait_sec:
            return None, wait
        return wait, None

    def _http_error(
        self, status: int, body: str, too_long: "float | None",
    ) -> GitHubError:
        message = _github_message(body)
        rate_limited = (
            too_long is not None or status == 429 or "rate limit" in body.lower()
        )
        text = f"GitHub API エラー HTTP {status}"
        if message:
            text += f": {message}"
        if too_long is not None:
            text += (
                f" (レート制限: 待機 {too_long:.0f}s が上限 "
                f"{self._max_wait_sec:.0f}s を超えるため中断)"
            )
        return GitHubError(
            text,
            status=status,
            rate_limited=rate_limited,
            server_timeout=status in _SERVER_TIMEOUT_STATUS,
        )


def _backoff_sec(attempt: int) -> float:
    """指数バックオフ (2, 4, 8, ... 秒) ±25% ジッタ、上限 60 秒。"""
    base = _BASE_BACKOFF_SEC * (2 ** (attempt - 1))
    jitter = random.uniform(-0.25, 0.25) * base
    return min(_DEFAULT_MAX_WAIT_SEC, max(0.0, base + jitter))


def _header(exc: "urllib.error.HTTPError", name: str) -> "str | None":
    """HTTPError のヘッダを大文字小文字無視で引く (Message / dict 両対応)。"""
    headers = getattr(exc, "headers", None)
    if headers is None:
        return None
    try:
        items = list(headers.items())
    except AttributeError:
        return None
    lname = name.lower()
    for key, value in items:
        if isinstance(key, str) and key.lower() == lname:
            return str(value).strip()
    return None


def _parse_float(value: "str | None") -> "float | None":
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_retry_after(
    value: "str | None", clock: "Callable[[], float]",
) -> "float | None":
    """``Retry-After`` (秒数 / HTTP-date) を秒に。不正・0 以下は ``None``。"""
    if value is None or not value.strip():
        return None
    seconds = _parse_float(value.strip())
    if seconds is not None:
        return seconds if seconds > 0 else None
    try:
        retry_at = email.utils.parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    if retry_at is None:
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    delta = retry_at.timestamp() - clock()
    return delta if delta > 0 and math.isfinite(delta) else None


def _read_error_body(exc: "urllib.error.HTTPError") -> str:
    try:
        raw = exc.read()
    except Exception:  # noqa: BLE001 - 本文が読めなくてもエラー処理は続行
        return ""
    if not raw:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _github_message(body: str) -> str:
    """GitHub のエラー本文 (JSON) から ``message`` だけを短く取り出す。"""
    if not body:
        return ""
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return ""
    if isinstance(parsed, dict) and isinstance(parsed.get("message"), str):
        return _truncate(parsed["message"])
    return ""


def _truncate(text: str) -> str:
    text = " ".join(text.split())
    if len(text) > _MAX_ERROR_MESSAGE_LEN:
        return text[: _MAX_ERROR_MESSAGE_LEN - 1] + "…"
    return text


def _reason(exc: BaseException) -> str:
    reason = getattr(exc, "reason", None)
    return _truncate(str(reason if reason is not None else exc))


def _graphql_error(errors: object, data: object) -> GitHubError:
    types: "list[str]" = []
    messages: "list[str]" = []
    if isinstance(errors, list):
        for err in errors:
            if not isinstance(err, dict):
                continue
            if isinstance(err.get("type"), str):
                types.append(err["type"])
            if isinstance(err.get("message"), str):
                messages.append(_truncate(err["message"]))
    text = "; ".join(messages[:3]) or "unknown error"
    if len(messages) > 3:
        text += f" (他 {len(messages) - 3} 件)"
    return GitHubError(
        f"GitHub GraphQL エラー: {text}",
        error_types=types,
        data=data if isinstance(data, dict) else None,
        rate_limited="RATE_LIMITED" in types,
        # "Something went wrong while executing your query. This may be the result of a
        # timeout ..." 等 = GitHub 側のクエリ timeout (クエリを軽くすれば通る見込み)
        server_timeout=_is_transient_graphql(errors),
    )


def _is_transient_graphql(errors: object) -> bool:
    if not isinstance(errors, list) or not errors:
        return False
    for err in errors:
        if not isinstance(err, dict):
            return False
        if err.get("type") == "RATE_LIMITED":
            return False
        message = str(err.get("message", "")).lower()
        if not any(marker in message for marker in _TRANSIENT_GRAPHQL_MARKERS):
            return False
    return True


# ======================================================================
# データ型
# ======================================================================


@dataclass(frozen=True)
class GitHubItem:
    """日記の ``Github`` に 1 行として出す活動。

    ``kind``: ``"issue"`` | ``"pr"`` | ``"commit"``。``repo`` は owner なしの repo 名
    (表示用, 前身互換)。commit は日 × repo の集計 1 行 (``additions`` / ``deletions`` /
    ``commit_count``)。
    """

    kind: str
    repo: str
    url: str
    number: "int | None" = None
    title: "str | None" = None
    additions: int = 0
    deletions: int = 0
    commit_count: int = 0


@dataclass(frozen=True)
class GitHubActivity:
    """:func:`collect_github_activity` の結果。

    Attributes:
        days: 対象期間の **全日付** → その日の items (活動なしは空 list)
        complete: 列挙 + 全 repo の全 connection を取り切れたときだけ ``True``
        errors: 失敗理由 (repo 名 + 理由。token は含まない)
        repos_scanned: 走査に成功した repo 数
        repos_listed: 列挙できた repo 総数 (絞り込み前)
        requests: この収集で発行した HTTP 試行回数
        cost: この収集で消費した GraphQL rate limit point
        repos_failed: 走査が必要だったのに失敗した repo 数
        owners_listed: repo 一覧の取得に成功した owner 数
    """

    days: "dict[date, list[GitHubItem]]"
    complete: bool
    errors: "list[str]" = field(default_factory=list)
    repos_scanned: int = 0
    repos_listed: int = 0
    requests: int = 0
    cost: int = 0
    repos_failed: int = 0
    owners_listed: int = 0

    @property
    def status(self) -> str:
        """``"complete"`` / ``"partial"`` (一部失敗) / ``"failed"`` (何も取れていない)。

        - ``complete``: 全部取れた
        - ``failed``: viewer の取得失敗 / 全 owner の repo 一覧取得失敗 / 走査が必要な
          repo が 1 つ以上あって全部失敗 のいずれか (= 使えるデータが無い)
        - ``partial``: 上記以外の失敗 (走査に成功した repo がある、または一部 owner の
          一覧は取れてその範囲に走査対象が無かった)

        ``complete`` 以外は日記に書かない前提 (:meth:`text_for`)。``partial`` /
        ``failed`` の区別は通知文面・実行レポート用。
        """
        if self.complete:
            return "complete"
        if self.repos_scanned > 0:
            return "partial"
        if self.repos_failed > 0 or self.owners_listed == 0:
            return "failed"
        return "partial"

    def items_for(self, d: date) -> "list[GitHubItem]":
        return list(self.days.get(d, []))

    def text_for(self, d: date) -> "str | None":
        """日記の ``Github`` に書く文字列。書くべきでない日は ``None``。

        - ``complete=False`` → ``None`` (取りこぼしの可能性があるので既存値を保持させる)
        - 期間外の日付 → ``None``
        - 活動なし → ``"該当なし"``
        """
        if not self.complete or d not in self.days:
            return None
        return format_github_items(self.days[d])


# ======================================================================
# 書式
# ======================================================================


def _one_line(text: "str | None") -> str:
    """タイトル内の改行類を空白に置換する (frontmatter の 1 行 = 1 item を保つ)。"""
    if text is None:
        return ""
    return (
        text.replace("\r\n", " ")
        .replace("\r", " ")
        .replace("\n", " ")
        .replace(" ", " ")
        .replace(" ", " ")
        .replace("\x85", " ")
    )


def format_github_item(item: GitHubItem) -> str:
    """1 item を前身互換の Markdown リンク 1 行にする。"""
    if item.kind == "issue":
        return f"[🎫 {item.repo}:Issue #{item.number}: {_one_line(item.title)}]({item.url})"
    if item.kind == "pr":
        return f"[🔀 {item.repo}:PR #{item.number}: {_one_line(item.title)}]({item.url})"
    if item.kind == "commit":
        return (
            f"[📝 {item.repo}:変更行数:+{item.additions}-{item.deletions} "
            f"({item.commit_count} commits)]({item.url})"
        )
    raise ValueError(f"未知の GitHubItem.kind: {item.kind!r}")


def format_github_items(items: "Sequence[GitHubItem]") -> str:
    """items を ``\\n`` 連結した文字列にする。空なら ``"該当なし"`` (前身互換)。"""
    if not items:
        return NO_ACTIVITY_TEXT
    return "\n".join(format_github_item(item) for item in items)


# ======================================================================
# GraphQL クエリ
# ======================================================================

_VIEWER_QUERY = """
query IhealthViewer($orgsAfter: String) {
  rateLimit { cost remaining resetAt }
  viewer {
    login
    organizations(first: 100, after: $orgsAfter) {
      pageInfo { hasNextPage endCursor }
      nodes { login }
    }
  }
}
"""

#: ``owners`` 指定時に fork の commit 作者判定のためだけに viewer の login を引く。
_VIEWER_LOGIN_QUERY = """
query IhealthViewerLogin {
  rateLimit { cost remaining resetAt }
  viewer { login }
}
"""

_OWNER_REPOS_QUERY = """
query IhealthOwnerRepos($login: String!, $first: Int!, $after: String) {
  rateLimit { cost remaining resetAt }
  repositoryOwner(login: $login) {
    login
    repositories(ownerAffiliations: [OWNER], first: $first, after: $after,
                 orderBy: {field: PUSHED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        nameWithOwner
        url
        isArchived
        isFork
        pushedAt
        updatedAt
        owner { login }
        defaultBranchRef { name }
      }
    }
  }
}
"""

_REPO_ACTIVITY_QUERY = """
query IhealthRepoActivity(
  $owner: String!, $name: String!, $since: GitTimestamp!, $until: GitTimestamp!,
  $withIssues: Boolean!, $issuesFirst: Int!, $issuesAfter: String,
  $withPrs: Boolean!, $prsFirst: Int!, $prsAfter: String,
  $withHistory: Boolean!, $historyFirst: Int!, $historyAfter: String,
  $assocFirst: Int!
) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    issues(states: CLOSED, orderBy: {field: UPDATED_AT, direction: DESC},
           first: $issuesFirst, after: $issuesAfter) @include(if: $withIssues) {
      pageInfo { hasNextPage endCursor }
      nodes { number title url closedAt updatedAt }
    }
    pullRequests(states: MERGED, orderBy: {field: UPDATED_AT, direction: DESC},
                 first: $prsFirst, after: $prsAfter) @include(if: $withPrs) {
      pageInfo { hasNextPage endCursor }
      nodes { number title url mergedAt updatedAt }
    }
    defaultBranchRef @include(if: $withHistory) {
      name
      target {
        ... on Commit {
          history(since: $since, until: $until, first: $historyFirst, after: $historyAfter) {
            pageInfo { hasNextPage endCursor }
            nodes {
              oid
              committedDate
              additions
              deletions
              parents { totalCount }
              author { name email user { login } }
              associatedPullRequests(first: $assocFirst) {
                totalCount
                nodes { number mergedAt mergeCommit { oid parents { totalCount } } }
              }
            }
          }
        }
      }
    }
  }
}
"""


# ======================================================================
# 収集
# ======================================================================


@dataclass(frozen=True)
class _Window:
    """``[start, end)`` の時刻窓 (aware datetime)。"""

    start: datetime
    end: datetime

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    @property
    def since_iso(self) -> str:
        return _utc_iso(self.start)

    @property
    def until_iso(self) -> str:
        return _utc_iso(self.end)


@dataclass(frozen=True)
class _RepoRef:
    owner: str
    name: str
    name_with_owner: str
    url: str
    default_branch: "str | None"
    pushed_at: "datetime | None"
    updated_at: "datetime | None"
    is_archived: bool
    is_fork: bool


@dataclass
class _RepoScan:
    """1 repo 分の走査結果 (成功時のみ全体に合流させる)。"""

    issues: "list[tuple[date, int, str, str]]" = field(default_factory=list)
    prs: "list[tuple[date, int, str, str]]" = field(default_factory=list)
    #: 日付 → [additions, deletions, commit 数]
    commits: "dict[date, list[int]]" = field(default_factory=dict)
    default_branch: "str | None" = None
    #: 日付 → その日に表示する item (issue close / PR merge / 数えた commit) の最終時刻。
    #: 日内の repo 順 (モジュール docstring「並び順」) に使う
    last_event: "dict[date, datetime]" = field(default_factory=dict)

    def note_event(self, day: date, moment: datetime) -> None:
        current = self.last_event.get(day)
        if current is None or moment > current:
            self.last_event[day] = moment


class _ShapeError(GitHubError):
    """応答の形が想定外 (API 変更 / バグ)。repo 単位の失敗として扱う。"""


def _utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_dt(value: object) -> "datetime | None":
    """GitHub の ISO8601 (``2026-07-18T12:25:48Z``) を aware datetime に。"""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _req_str(node: object, key: str, where: str) -> str:
    """``node[key]`` が非空 str であることを要求する (違反は :class:`_ShapeError`)。"""
    value = node.get(key) if isinstance(node, dict) else None
    if not isinstance(value, str) or not value:
        raise _ShapeError(f"{where}: {key} が文字列ではありません")
    return value


def _req_int(node: object, key: str, where: str) -> int:
    """``node[key]`` が int (bool 以外) であることを要求する。"""
    value = node.get(key) if isinstance(node, dict) else None
    if not isinstance(value, int) or isinstance(value, bool):
        raise _ShapeError(f"{where}: {key} が整数ではありません")
    return value


def _connection(node: object, key: str, where: str) -> dict:
    conn = node.get(key) if isinstance(node, dict) else None
    if not isinstance(conn, dict):
        raise _ShapeError(f"{where}: {key} が取得できません")
    nodes = conn.get("nodes")
    if not isinstance(nodes, list):
        raise _ShapeError(f"{where}: {key}.nodes が list ではありません")
    return conn


def _page_info(conn: dict, where: str) -> "tuple[bool, str | None]":
    info = conn.get("pageInfo")
    if not isinstance(info, dict):
        raise _ShapeError(f"{where}: pageInfo がありません")
    has_next = info.get("hasNextPage") is True
    cursor = info.get("endCursor")
    if has_next and not isinstance(cursor, str):
        raise _ShapeError(f"{where}: hasNextPage=true なのに endCursor がありません")
    return has_next, cursor if isinstance(cursor, str) else None


def _is_bot_author(author: object) -> bool:
    if not isinstance(author, dict):
        return False
    user = author.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    if isinstance(login, str) and login.lower().endswith("[bot]"):
        return True
    name = author.get("name")
    if isinstance(name, str) and "[bot]" in name.lower():
        return True
    mail = author.get("email")
    if isinstance(mail, str) and "[bot]@" in mail.lower():
        return True
    return False


def _is_pr_branch_commit(
    node: dict, oid: str, where: str, log: logging.Logger,
) -> bool:
    """PR ブランチ上の commit (= merge commit 経由で default branch に入った) か。

    merged PR の ``mergeCommit`` が自分自身 (squash / rebase の最終 commit) なら直コミット。
    ``mergeCommit`` の parent が 1 (squash / rebase merge) の PR に属する commit も
    直コミット (rebase merge で default branch に載った commit)。
    ``mergeCommit`` が merge commit (parent 2 以上) または不明なら PR ブランチ commit。
    """
    assoc = node.get("associatedPullRequests")
    if not isinstance(assoc, dict):
        return False
    prs = assoc.get("nodes")
    if not isinstance(prs, list):
        return False
    total = assoc.get("totalCount")
    if isinstance(total, int) and total > len(prs):
        log.warning(
            "%s: commit %s の関連 PR が %d 件 (取得 %d 件)。取得分だけで PR ブランチ判定します",
            where, oid[:7], total, len(prs),
        )
    for pr in prs:
        if not isinstance(pr, dict) or not pr.get("mergedAt"):
            continue
        merge_commit = pr.get("mergeCommit")
        if not isinstance(merge_commit, dict):
            return True  # merged なのに mergeCommit 不明 → 保守的に除外
        if merge_commit.get("oid") == oid:
            continue  # squash / rebase の最終 commit = 直コミット
        parents = merge_commit.get("parents")
        parent_count = parents.get("totalCount") if isinstance(parents, dict) else None
        if isinstance(parent_count, int) and parent_count <= 1:
            continue  # squash / rebase merge の PR → default branch 上の commit は直コミット
        return True
    return False


def _list_owners(
    client: GitHubClient, log: logging.Logger,
) -> "tuple[str, list[str]]":
    """viewer の login と所属 org の login 一覧を返す。"""
    viewer_login = ""
    orgs: "list[str]" = []
    after: "str | None" = None
    for _page in range(_MAX_ORG_PAGES):
        data = client.graphql(_VIEWER_QUERY, {"orgsAfter": after})
        viewer = data.get("viewer")
        viewer_login = _req_str(viewer, "login", "viewer")
        conn = _connection(viewer, "organizations", "viewer")
        for org in conn["nodes"]:
            org_login = org.get("login") if isinstance(org, dict) else None
            if isinstance(org_login, str) and org_login:
                orgs.append(org_login)
        has_next, after = _page_info(conn, "viewer.organizations")
        if not has_next:
            break
    else:
        raise GitHubError(
            f"viewer.organizations が {_MAX_ORG_PAGES} page を超えたため打ち切り"
        )
    log.debug("GitHub viewer=%s orgs=%d", viewer_login, len(orgs))
    return viewer_login, orgs


def _list_owner_repos(
    client: GitHubClient, owner: str, log: logging.Logger,
) -> "list[_RepoRef]":
    """owner (user / org) が所有する repo を pushedAt 降順で全 page 取得する。"""
    repos: "list[_RepoRef]" = []
    after: "str | None" = None
    for _page in range(_MAX_REPO_PAGES):
        data = client.graphql(
            _OWNER_REPOS_QUERY,
            {"login": owner, "first": _REPO_PAGE_SIZE, "after": after},
        )
        owner_node = data.get("repositoryOwner")
        if owner_node is None:
            raise GitHubError(f"owner {owner!r} が見つかりません (user / org 名を確認)")
        where = f"repositoryOwner({owner})"
        conn = _connection(owner_node, "repositories", where)
        for node in conn["nodes"]:
            repos.append(_repo_ref(node, where))
        has_next, after = _page_info(conn, where)
        if not has_next:
            break
    else:
        raise GitHubError(
            f"owner {owner!r} の repo 一覧が {_MAX_REPO_PAGES} page を超えたため打ち切り"
        )
    log.debug("GitHub owner=%s repos=%d", owner, len(repos))
    return repos


def _repo_ref(node: object, where: str) -> _RepoRef:
    name = _req_str(node, "name", where)
    name_with_owner = _req_str(node, "nameWithOwner", where)
    if not isinstance(node, dict):  # _req_str が保証するが型チェッカー向けに明示
        raise _ShapeError(f"{where}: repository node が dict ではありません")
    owner_node = node.get("owner")
    owner = owner_node.get("login") if isinstance(owner_node, dict) else None
    if not isinstance(owner, str) or not owner:
        owner = name_with_owner.split("/", 1)[0]
    url = node.get("url")
    if not isinstance(url, str) or not url:
        url = f"https://github.com/{name_with_owner}"
    branch_node = node.get("defaultBranchRef")
    branch = branch_node.get("name") if isinstance(branch_node, dict) else None
    return _RepoRef(
        owner=owner,
        name=name,
        name_with_owner=name_with_owner,
        url=url.rstrip("/"),
        default_branch=branch if isinstance(branch, str) and branch else None,
        pushed_at=_parse_dt(node.get("pushedAt")),
        updated_at=_parse_dt(node.get("updatedAt")),
        is_archived=node.get("isArchived") is True,
        is_fork=node.get("isFork") is True,
    )


def _is_candidate(repo: _RepoRef, window: _Window, include_archived: bool) -> bool:
    if repo.is_archived and not include_archived:
        return False
    stamps = [t for t in (repo.pushed_at, repo.updated_at) if t is not None]
    if not stamps:
        return False
    return max(stamps) >= window.start


def _author_login(author: object) -> "str | None":
    """commit の ``author.user.login`` (小文字化)。GitHub アカウント未紐付けなら ``None``。"""
    if not isinstance(author, dict):
        return None
    user = author.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    return login.lower() if isinstance(login, str) and login else None


def _consume_closed(
    conn: dict,
    time_key: str,
    window: _Window,
    tz: tzinfo,
    out: "list[tuple[date, int, str, str]]",
    seen: "set[int]",
    where: str,
    scan: _RepoScan,
) -> "tuple[bool, str | None]":
    """closed issue / merged PR の 1 page を処理し (続きが必要か, cursor) を返す。

    ``UPDATED_AT`` 降順なので、期間開始より前に更新された node が現れたら以降の page に
    期間内の close / merge は無い (close/merge 時刻 <= updatedAt のため)。
    """
    reached_older = False
    for node in conn["nodes"]:
        if not isinstance(node, dict):
            raise _ShapeError(f"{where}: node が dict ではありません")
        updated = _parse_dt(node.get("updatedAt"))
        if updated is None:
            raise _ShapeError(f"{where}: updatedAt がありません")
        if updated < window.start:
            reached_older = True
        moment = _parse_dt(node.get(time_key))
        if moment is None or not window.contains(moment):
            continue
        number = _req_int(node, "number", where)
        if number in seen:
            continue
        seen.add(number)
        title = node.get("title")
        if not isinstance(title, str):
            raise _ShapeError(f"{where}: title が文字列ではありません (#{number})")
        url = _req_str(node, "url", where)
        day = moment.astimezone(tz).date()
        out.append((day, number, title, url))
        scan.note_event(day, moment)
    has_next, cursor = _page_info(conn, where)
    return has_next and not reached_older, cursor


def _consume_history(
    conn: dict,
    window: _Window,
    tz: tzinfo,
    include_bots: bool,
    fork_author: "str | None",
    scan: _RepoScan,
    seen: "set[str]",
    where: str,
    log: logging.Logger,
) -> "tuple[bool, str | None]":
    """default branch history の 1 page を処理し (続きがあるか, cursor) を返す。

    ``fork_author`` (小文字の login) を渡すと、その author の commit だけを数える
    (fork repo の upstream 由来 commit を除外する用途)。
    """
    for node in conn["nodes"]:
        oid = _req_str(node, "oid", where)
        if oid in seen:
            continue
        seen.add(oid)
        committed = _parse_dt(node.get("committedDate"))
        if committed is None:
            raise _ShapeError(f"{where}: committedDate がありません ({oid[:7]})")
        if not window.contains(committed):
            continue
        parents = node.get("parents")
        parent_count = parents.get("totalCount") if isinstance(parents, dict) else None
        if not isinstance(parent_count, int):
            raise _ShapeError(f"{where}: parents.totalCount がありません ({oid[:7]})")
        if parent_count > 1:
            continue  # merge commit
        if not include_bots and _is_bot_author(node.get("author")):
            continue
        if fork_author is not None and _author_login(node.get("author")) != fork_author:
            continue  # fork: viewer 以外が author (upstream の同期で入った commit 等)
        if _is_pr_branch_commit(node, oid, where, log):
            continue
        additions = _req_int(node, "additions", where)
        deletions = _req_int(node, "deletions", where)
        day = committed.astimezone(tz).date()
        bucket = scan.commits.setdefault(day, [0, 0, 0])
        bucket[0] += additions
        bucket[1] += deletions
        bucket[2] += 1
        scan.note_event(day, committed)
    return _page_info(conn, where)


def _scan_repo(
    client: GitHubClient,
    repo: _RepoRef,
    window: _Window,
    tz: tzinfo,
    include_bots: bool,
    max_pages: int,
    log: logging.Logger,
    *,
    fork_author: "str | None" = None,
) -> _RepoScan:
    """1 repo の closed issue / merged PR / default branch history を最後まで取得する。

    1 request で 3 connection の 1 page 目をまとめて取り、続きが必要な connection だけ
    ``@include`` で絞って追加 page を取る。history を含む request が GitHub 側 timeout で
    失敗したら、history の page 件数を ``_HISTORY_PAGE_SIZES`` に沿って縮小し同じ cursor
    から取り直す (縮小後の件数はこの repo の残りの page にも使う)。

    ``fork_author`` (小文字の login) を渡すと history はその author の commit だけ数える。
    """
    where = repo.name_with_owner
    scan = _RepoScan(default_branch=repo.default_branch)
    variables: "dict[str, object]" = {
        "owner": repo.owner,
        "name": repo.name,
        "since": window.since_iso,
        "until": window.until_iso,
        "withIssues": True,
        "issuesFirst": _ISSUE_PAGE_SIZE,
        "issuesAfter": None,
        "withPrs": True,
        "prsFirst": _PR_PAGE_SIZE,
        "prsAfter": None,
        "withHistory": True,
        "historyFirst": _HISTORY_PAGE_SIZE,
        "historyAfter": None,
        "assocFirst": _ASSOC_PR_FIRST,
    }
    pages = {"issues": 0, "prs": 0}
    #: 取得要求した history の commit 数 (pagination 上限は既定 page 件数換算で判定)
    history_requested = 0
    size_index = 0
    seen_issues: "set[int]" = set()
    seen_prs: "set[int]" = set()
    seen_commits: "set[str]" = set()
    while variables["withIssues"] or variables["withPrs"] or variables["withHistory"]:
        history_first = _HISTORY_PAGE_SIZES[size_index]
        variables["historyFirst"] = history_first
        shrinkable = bool(variables["withHistory"]) and size_index + 1 < len(_HISTORY_PAGE_SIZES)
        try:
            data = client.graphql(
                _REPO_ACTIVITY_QUERY,
                dict(variables),
                timeout_retries=_SHRINK_TIMEOUT_RETRIES if shrinkable else None,
            )
        except GitHubError as exc:
            if not (shrinkable and exc.server_timeout):
                raise
            size_index += 1
            log.warning(
                "GitHub: %s の history 取得が GitHub 側で timeout (%s)。page 件数を "
                "%d → %d に縮小して同じ位置から取り直します",
                where, exc, history_first, _HISTORY_PAGE_SIZES[size_index],
            )
            continue
        repo_node = data.get("repository")
        if not isinstance(repo_node, dict):
            raise GitHubError(f"{where}: repository が取得できません (権限 / 削除?)")

        if variables["withIssues"]:
            pages["issues"] += 1
            conn = _connection(repo_node, "issues", f"{where} issues")
            more, cursor = _consume_closed(
                conn, "closedAt", window, tz, scan.issues, seen_issues, f"{where} issues",
                scan,
            )
            _check_page_limit(more, pages["issues"], max_pages, f"{where} issues")
            variables["withIssues"] = more
            variables["issuesAfter"] = cursor

        if variables["withPrs"]:
            pages["prs"] += 1
            conn = _connection(repo_node, "pullRequests", f"{where} pullRequests")
            more, cursor = _consume_closed(
                conn, "mergedAt", window, tz, scan.prs, seen_prs, f"{where} pullRequests",
                scan,
            )
            _check_page_limit(more, pages["prs"], max_pages, f"{where} pullRequests")
            variables["withPrs"] = more
            variables["prsAfter"] = cursor

        if variables["withHistory"]:
            branch_node = repo_node.get("defaultBranchRef")
            target = branch_node.get("target") if isinstance(branch_node, dict) else None
            if isinstance(branch_node, dict) and isinstance(branch_node.get("name"), str):
                scan.default_branch = branch_node["name"]
            if not isinstance(target, dict) or "history" not in target:
                # 空 repo (default branch 無し) / target が Commit 以外
                variables["withHistory"] = False
            else:
                conn = _connection(target, "history", f"{where} history")
                more, cursor = _consume_history(
                    conn, window, tz, include_bots, fork_author, scan, seen_commits,
                    f"{where} history", log,
                )
                history_requested += history_first
                if more and history_requested >= max_pages * _HISTORY_PAGE_SIZE:
                    raise GitHubError(
                        f"{where} history: pagination 上限 ({max_pages} page 相当 = "
                        f"{max_pages * _HISTORY_PAGE_SIZE} commit) に達したため打ち切り"
                    )
                variables["withHistory"] = more
                variables["historyAfter"] = cursor
    return scan


def _check_page_limit(more: bool, pages: int, max_pages: int, where: str) -> None:
    if more and pages >= max_pages:
        raise GitHubError(
            f"{where}: pagination 上限 ({max_pages} page) に達したため打ち切り"
        )


def _date_range(start: date, end: date) -> "list[date]":
    days: "list[date]" = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


def _dedupe_names(names: "Iterable[str]") -> "list[str]":
    """前後空白除去・空要素除去・大文字小文字無視の重複除去 (先勝ち・順序保持)。"""
    result: "list[str]" = []
    seen: "set[str]" = set()
    for raw in names:
        name = raw.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            result.append(name)
    return result


def _split_names(names: "Iterable[str] | str | None", what: str) -> "list[str]":
    """``owners`` / ``exclude_repos`` 引数を list に正規化する。

    list / tuple のほか **カンマ区切りの str** (``.env`` の値そのまま) を受け付ける。
    str を ``Sequence[str]`` として 1 文字ずつ回すと 1 文字の owner を大量に問い合わせる
    (実在する 1 文字 login の他人の活動を書きかねない) ため、str は必ずカンマで分割する。
    list の各要素もカンマで分割する。str 以外の要素は ``TypeError``。
    """
    if names is None:
        return []
    raw_items: "Iterable[object]" = [names] if isinstance(names, str) else names
    parts: "list[str]" = []
    for raw in raw_items:
        if not isinstance(raw, str):
            raise TypeError(f"{what} の要素は str で指定してください: {type(raw).__name__}")
        parts.extend(raw.split(","))
    return _dedupe_names(parts)


def _normalize_owners(owners: "Sequence[str] | str | None") -> "list[str]":
    """``owners`` を検証済みの login list にする (不正値は ``ValueError``)。"""
    names = _split_names(owners, "owners")
    bad = [name for name in names if not _LOGIN_RE.fullmatch(name)]
    if bad:
        raise ValueError(
            f"owners に GitHub の user / org 名として不正な値があります: {bad!r} "
            "(login 名をカンマ区切りで指定。例: 'octo-user,octo-org')"
        )
    return names


def _normalize_excludes(exclude_repos: "Sequence[str] | str | None") -> "set[str]":
    """``exclude_repos`` を小文字の ``owner/name`` 集合にする (不正値は ``ValueError``)。"""
    names = _split_names(exclude_repos, "exclude_repos")
    bad: "list[str]" = []
    for name in names:
        owner, sep, repo = name.partition("/")
        if not sep or not _LOGIN_RE.fullmatch(owner) or not _REPO_NAME_RE.fullmatch(repo):
            bad.append(name)
    if bad:
        raise ValueError(
            f"exclude_repos は owner/name 形式で指定してください: {bad!r} "
            "(例: 'octo-user/some-repo,octo-org/other')"
        )
    return {name.lower() for name in names}


def _viewer_login(client: GitHubClient) -> str:
    """viewer (token の持ち主) の login を 1 request で取得する。"""
    data = client.graphql(_VIEWER_LOGIN_QUERY)
    return _req_str(data.get("viewer"), "login", "viewer")


#: 日内の種別順 (issue → PR → commit)。
_KIND_ORDER = {"issue": 0, "pr": 1, "commit": 2}


def _row_key(
    scan: _RepoScan, repo: _RepoRef, day: date, item: GitHubItem,
) -> "tuple[date, int, float, str, str, int]":
    """日別の並びキー: 日付 → 種別 → その日のその repo の最終イベント時刻 (降順) →
    ``owner/name`` (大文字小文字無視 → 完全一致の昇順) → number 降順。

    その日のデータだけで決まる (後日の push や実行時刻に依存しない) ので、lookback で
    同じ日を何度処理しても内容が同じなら同じ文字列になる。
    """
    last = scan.last_event.get(day)
    stamp = last.timestamp() if last is not None else 0.0
    nwo = repo.name_with_owner
    return (day, _KIND_ORDER[item.kind], -stamp, nwo.lower(), nwo, -(item.number or 0))


def collect_github_activity(
    client: GitHubClient,
    start: date,
    end: date,
    *,
    tz: tzinfo = JST,
    owners: "Sequence[str] | str | None" = None,
    exclude_repos: "Sequence[str] | str | None" = (),
    include_bots: bool = False,
    include_archived: bool = True,
    fork_all_authors: bool = False,
    max_pages: int = _MAX_PAGES_PER_CONNECTION,
    logger: "logging.Logger | None" = None,
) -> GitHubActivity:
    """``start``..``end`` (両端含む, ``tz`` の暦日) の GitHub 活動を日別に集める。

    期間は ``[start 0:00, end+1 0:00)`` を ``tz`` で解釈し、API には UTC で渡す。
    1 repo の失敗は warning で skip して他 repo を続行する (結果は ``complete=False``)。

    Args:
        client: :class:`GitHubClient`
        start / end: 対象期間 (``start <= end``)
        tz: 日付境界の tz (既定 JST)
        owners: 対象 owner (user / org の login)。list またはカンマ区切りの str。
            ``None`` / 空なら viewer + 所属 org 全部
        exclude_repos: 除外する ``owner/name`` (大文字小文字無視の完全一致)。list または
            カンマ区切りの str
        include_bots: bot の commit も数えるなら ``True``
        include_archived: archived repo も時刻条件を満たせば走査する (既定 ``True``)
        fork_all_authors: fork repo の commit も全作者分数えるなら ``True`` (既定
            ``False`` = viewer が author の commit だけ)
        max_pages: 1 connection あたりの最大 page 数 (超えたら打ち切り = incomplete)。
            history は既定 page 件数 (100 commit) 換算
        logger: 注入ロガー

    Returns:
        :class:`GitHubActivity` (``days`` は期間の全日付をキーに持つ)

    Raises:
        ValueError: ``end < start`` / ``max_pages < 1`` / ``owners`` ・ ``exclude_repos``
            の書式不正 (いずれも API を呼ぶ前に検出)
        TypeError: ``owners`` / ``exclude_repos`` に str 以外の要素
    """
    log = logger or _logger
    if end < start:
        raise ValueError(f"期間の開始が終了より後です: {start} > {end}")
    if max_pages < 1:
        raise ValueError(f"max_pages は 1 以上: {max_pages}")
    owner_filter = _normalize_owners(owners)
    excluded = _normalize_excludes(exclude_repos)
    window = _Window(
        start=datetime.combine(start, dt_time(0, 0), tzinfo=tz),
        end=datetime.combine(end + timedelta(days=1), dt_time(0, 0), tzinfo=tz),
    )
    requests_before = client.request_count
    cost_before = client.cost_total
    days: "dict[date, list[GitHubItem]]" = {d: [] for d in _date_range(start, end)}
    errors: "list[str]" = []
    scanned = 0
    failed = 0
    owners_listed = 0

    def _result(complete: bool, listed: int) -> GitHubActivity:
        activity = GitHubActivity(
            days=days,
            complete=complete,
            errors=errors,
            repos_scanned=scanned,
            repos_listed=listed,
            requests=client.request_count - requests_before,
            cost=client.cost_total - cost_before,
            repos_failed=failed,
            owners_listed=owners_listed,
        )
        log.info(
            "GitHub 活動収集: %s..%s status=%s repos scanned=%d failed=%d listed=%d "
            "requests=%d cost=%d",
            start, end, activity.status, scanned, failed, listed,
            activity.requests, activity.cost,
        )
        return activity

    # ---- 1) owner 決定 ----
    viewer_login: "str | None" = None
    owner_list = owner_filter
    if not owner_list:
        try:
            viewer_login, orgs = _list_owners(client, log)
        except GitHubError as exc:
            errors.append(f"viewer / organizations の取得に失敗: {exc}")
            log.warning("GitHub: viewer / organizations の取得に失敗: %s", exc)
            return _result(False, 0)
        owner_list = _dedupe_names([viewer_login, *orgs])

    # ---- 2) repo 列挙 ----
    complete = True
    all_repos: "list[_RepoRef]" = []
    seen_names: "set[str]" = set()
    for owner in owner_list:
        try:
            owner_repos = _list_owner_repos(client, owner, log)
        except GitHubError as exc:
            complete = False
            errors.append(f"{owner}: repo 一覧の取得に失敗: {exc}")
            log.warning("GitHub: %s の repo 一覧の取得に失敗: %s", owner, exc)
            continue
        owners_listed += 1
        for repo in owner_repos:
            key = repo.name_with_owner.lower()
            if key in seen_names:
                continue
            seen_names.add(key)
            all_repos.append(repo)
    # 走査順だけ pushedAt 降順 (owner 跨ぎ。同時刻・None は列挙順 = sort は安定)。
    # 出力の並びは日ごとのイベント時刻で決める (step 4) のでここには依存しない。
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    all_repos.sort(key=lambda r: r.pushed_at or epoch, reverse=True)
    candidates = [
        repo for repo in all_repos
        if repo.name_with_owner.lower() not in excluded
        and _is_candidate(repo, window, include_archived)
    ]
    log.info(
        "GitHub: 期間 %s..%s (tz=%s) 対象 repo %d / 列挙 %d",
        start, end, tz, len(candidates), len(all_repos),
    )

    # ---- 2.5) fork の commit 作者判定に使う viewer login ----
    fork_login: "str | None" = viewer_login.lower() if viewer_login else None
    forks = [repo for repo in candidates if repo.is_fork]
    if forks and not fork_all_authors and fork_login is None:
        try:
            fork_login = _viewer_login(client).lower()
        except GitHubError as exc:
            complete = False
            names = ", ".join(repo.name_with_owner for repo in forks)
            errors.append(
                f"viewer の login を取得できないため fork repo の commit 作者を判定できず "
                f"{len(forks)} 件を skip ({names}): {exc}"
            )
            log.warning(
                "GitHub: viewer の login を取得できないため fork repo %d 件を skip: %s",
                len(forks), exc,
            )

    # ---- 3) repo ごとの走査 (item は並びキー :func:`_row_key` と一緒に集める) ----
    rows: "list[tuple[tuple[date, int, float, str, str, int], GitHubItem]]" = []
    for repo in candidates:
        fork_author: "str | None" = None
        if repo.is_fork and not fork_all_authors:
            if fork_login is None:
                failed += 1  # 理由は 2.5) で errors に 1 件だけ記録済み
                continue
            fork_author = fork_login
        try:
            scan = _scan_repo(
                client, repo, window, tz, include_bots, max_pages, log,
                fork_author=fork_author,
            )
        except GitHubError as exc:
            complete = False
            failed += 1
            errors.append(f"{repo.name_with_owner}: {exc}")
            log.warning("GitHub: %s の取得に失敗 (skip): %s", repo.name_with_owner, exc)
            continue
        scanned += 1
        for day, number, title, url in scan.issues:
            item = GitHubItem(kind="issue", repo=repo.name, url=url, number=number, title=title)
            rows.append((_row_key(scan, repo, day, item), item))
        for day, number, title, url in scan.prs:
            item = GitHubItem(kind="pr", repo=repo.name, url=url, number=number, title=title)
            rows.append((_row_key(scan, repo, day, item), item))
        branch = scan.default_branch or repo.default_branch or "main"
        for day, (additions, deletions, count) in scan.commits.items():
            item = GitHubItem(
                kind="commit",
                repo=repo.name,
                url=f"{repo.url}/commits/{branch}",
                additions=additions,
                deletions=deletions,
                commit_count=count,
            )
            rows.append((_row_key(scan, repo, day, item), item))
        log.debug(
            "GitHub: %s issues=%d prs=%d commit_days=%d",
            repo.name_with_owner, len(scan.issues), len(scan.prs), len(scan.commits),
        )

    # ---- 4) 日別に並べる: 種別 (issue → PR → commit) → repo (その日の最終イベント
    #         時刻の降順, 同時刻は owner/name 昇順) → number 降順 ----
    rows.sort(key=lambda row: row[0])
    for key, item in rows:
        day = key[0]
        if day in days:
            days[day].append(item)
    return _result(complete, len(all_repos))
