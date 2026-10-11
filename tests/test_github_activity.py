"""github_activity.py のテスト。

ネットワークには一切出ない: ``GitHubClient`` に ``opener`` を注入し、本モジュールの
GraphQL クエリ 4 種 (IhealthViewer / IhealthViewerLogin / IhealthOwnerRepos /
IhealthRepoActivity) だけを解釈する最小エミュレータ :class:`FakeGitHub` が応答する。
``gh`` も ``runner`` 注入で実行しない。データはすべて合成 (実在の repo 名・token・活動は
使わない)。
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import re
import ssl
import subprocess
import unittest
import urllib.error
from datetime import date, datetime, timezone
from email.message import Message
from typing import Callable, Iterator, Sequence

from ihealth.github_activity import (
    JST,
    NO_ACTIVITY_TEXT,
    GitHubActivity,
    GitHubClient,
    GitHubError,
    GitHubItem,
    collect_github_activity,
    format_github_items,
    resolve_github_token,
)


#: テストの warning を stderr (logging.lastResort) に垂れ流さない。assertLogs は
#: 自前の handler を差し込むので検証には影響しない。
logging.getLogger("test.github_activity").addHandler(logging.NullHandler())

#: 合成 token (実 token ではない)。ログ / 例外 / errors に出ないことを検証する。
TOKEN = "ghp_SYNTHETICtoken0123456789abcdefXYZ"
ENDPOINT = "https://api.github.com/graphql"
UTC = timezone.utc


# ======================================================================
# ヘルパー
# ======================================================================


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def at_jst(y: int, mo: int, d: int, h: int = 12, mi: int = 0, s: int = 0) -> str:
    """JST の壁時計時刻を GitHub 形式 (UTC, Z) の文字列にする。"""
    return iso(datetime(y, mo, d, h, mi, s, tzinfo=JST))


def _parse(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _page(items: list, first: int, after: "str | None") -> dict:
    start = int(after) if after else 0
    chunk = items[start:start + first]
    end = start + len(chunk)
    return {
        "pageInfo": {"hasNextPage": end < len(items), "endCursor": str(end) if chunk else None},
        "nodes": chunk,
    }


def pr_ref(
    number: int, merged_at: "str | None", merge_oid: "str | None", merge_parents: int = 1,
) -> dict:
    """commit の associatedPullRequests.nodes の 1 件。"""
    return {
        "number": number,
        "mergedAt": merged_at,
        "mergeCommit": (
            {"oid": merge_oid, "parents": {"totalCount": merge_parents}}
            if merge_oid is not None else None
        ),
    }


class FakeGitHub:
    """GitHub GraphQL の最小エミュレータ (本モジュールのクエリだけを解釈する)。

    - repositoryOwner.repositories: pushedAt 降順 + first/after
    - issues / pullRequests: updatedAt 降順 + first/after
    - history: ``since <= committedDate <= until`` (git 同様 until は inclusive) を
      committedDate 降順で first/after
    - ``@include(if: $withXxx)`` で落とされた connection は応答に含めない
    """

    def __init__(self, viewer: str = "octo-user", orgs: "Sequence[str]" = ("octo-org",)) -> None:
        self.viewer = viewer
        self.orgs = list(orgs)
        self.repos: "dict[str, list[dict]]" = {}
        self.issues: "dict[str, list[dict]]" = {}
        self.prs: "dict[str, list[dict]]" = {}
        self.commits: "dict[str, list[dict]]" = {}
        self.repo_errors: "dict[str, list[dict]]" = {}
        self.owner_errors: "dict[str, list[dict]]" = {}
        self.viewer_exception: "Exception | None" = None
        self.viewer_login_exception: "Exception | None" = None
        self.calls: "list[tuple[str, dict]]" = []
        self.queries: "list[str]" = []

    # ---------- builder ----------

    def add_repo(
        self, owner: str, name: str, *, pushed_at: "str | None",
        updated_at: "str | None" = None, archived: bool = False, fork: bool = False,
        branch: "str | None" = "main",
    ) -> str:
        nwo = f"{owner}/{name}"
        self.repos.setdefault(owner.lower(), []).append({
            "name": name,
            "nameWithOwner": nwo,
            "url": f"https://github.com/{nwo}",
            "isArchived": archived,
            "isFork": fork,
            "pushedAt": pushed_at,
            "updatedAt": updated_at if updated_at is not None else pushed_at,
            "owner": {"login": owner},
            "defaultBranchRef": {"name": branch} if branch else None,
        })
        return nwo

    def add_issue(
        self, nwo: str, number: int, closed_at: "str | None", *,
        title: "str | None" = None, updated_at: "str | None" = None,
    ) -> None:
        self.issues.setdefault(nwo.lower(), []).append({
            "number": number,
            "title": title if title is not None else f"synthetic issue {number}",
            "url": f"https://github.com/{nwo}/issues/{number}",
            "closedAt": closed_at,
            "updatedAt": updated_at or closed_at,
        })

    def add_pr(
        self, nwo: str, number: int, merged_at: "str | None", *,
        title: "str | None" = None, updated_at: "str | None" = None,
    ) -> None:
        self.prs.setdefault(nwo.lower(), []).append({
            "number": number,
            "title": title if title is not None else f"synthetic pr {number}",
            "url": f"https://github.com/{nwo}/pull/{number}",
            "mergedAt": merged_at,
            "updatedAt": updated_at or merged_at,
        })

    def add_commit(
        self, nwo: str, oid: str, committed_at: str, *, additions: int = 1,
        deletions: int = 0, parents: int = 1, login: "str | None" = "octo-user",
        name: str = "Octo User", email: str = "octo@example.com",
        prs: "Sequence[dict]" = (), pr_total: "int | None" = None,
    ) -> None:
        self.commits.setdefault(nwo.lower(), []).append({
            "oid": oid,
            "committedDate": committed_at,
            "additions": additions,
            "deletions": deletions,
            "parents": {"totalCount": parents},
            "author": {
                "name": name,
                "email": email,
                "user": {"login": login} if login else None,
            },
            "associatedPullRequests": {
                "totalCount": pr_total if pr_total is not None else len(prs),
                "nodes": list(prs),
            },
        })

    # ---------- opener ----------

    def opener(self, req: object, timeout: float) -> bytes:
        assert req.full_url == ENDPOINT  # type: ignore[attr-defined]
        assert req.get_method() == "POST"  # type: ignore[attr-defined]
        payload = json.loads(req.data.decode("utf-8"))  # type: ignore[attr-defined]
        query = payload["query"]
        variables = payload.get("variables") or {}
        found = re.search(r"query\s+(\w+)", query)
        assert found is not None, "operation 名の無いクエリ"
        op = found.group(1)
        self.calls.append((op, dict(variables)))
        self.queries.append(query)
        if op == "IhealthViewer":
            body = self._viewer(variables)
        elif op == "IhealthViewerLogin":
            if self.viewer_login_exception is not None:
                raise self.viewer_login_exception
            body = {"data": {"rateLimit": self._rate(), "viewer": {"login": self.viewer}}}
        elif op == "IhealthOwnerRepos":
            body = self._owner_repos(variables)
        elif op == "IhealthRepoActivity":
            body = self._repo_activity(variables)
        else:  # pragma: no cover - 未知のクエリはテスト失敗
            raise AssertionError(f"unknown operation {op}")
        return json.dumps(body).encode("utf-8")

    def ops(self, op: str) -> "list[dict]":
        return [v for o, v in self.calls if o == op]

    def scanned(self) -> "list[str]":
        """RepoActivity で 1 page 目を取りに行った repo (owner/name) の順序付き一覧。"""
        names: "list[str]" = []
        for variables in self.ops("IhealthRepoActivity"):
            nwo = f"{variables['owner']}/{variables['name']}"
            if nwo not in names:
                names.append(nwo)
        return names

    @staticmethod
    def _rate() -> dict:
        return {"cost": 1, "remaining": 4999, "resetAt": "2030-01-01T00:00:00Z"}

    def _viewer(self, variables: dict) -> dict:
        if self.viewer_exception is not None:
            raise self.viewer_exception
        orgs = [{"login": org} for org in self.orgs]
        return {"data": {
            "rateLimit": self._rate(),
            "viewer": {
                "login": self.viewer,
                "organizations": _page(orgs, 100, variables.get("orgsAfter")),
            },
        }}

    def _owner_repos(self, variables: dict) -> dict:
        login = variables["login"]
        key = login.lower()
        if key in self.owner_errors:
            return {
                "data": {"rateLimit": self._rate(), "repositoryOwner": None},
                "errors": self.owner_errors[key],
            }
        known = {self.viewer.lower(), *(o.lower() for o in self.orgs), *self.repos.keys()}
        if key not in known:
            return {"data": {"rateLimit": self._rate(), "repositoryOwner": None}}
        repos = sorted(
            self.repos.get(key, []), key=lambda r: r["pushedAt"] or "", reverse=True,
        )
        return {"data": {
            "rateLimit": self._rate(),
            "repositoryOwner": {
                "login": login,
                "repositories": _page(repos, variables["first"], variables.get("after")),
            },
        }}

    def _repo_activity(self, variables: dict) -> dict:
        nwo = f"{variables['owner']}/{variables['name']}".lower()
        if nwo in self.repo_errors:
            return {
                "data": {"rateLimit": self._rate(), "repository": None},
                "errors": self.repo_errors[nwo],
            }
        repo = next(
            (r for r in self.repos.get(variables["owner"].lower(), [])
             if r["name"].lower() == variables["name"].lower()),
            None,
        )
        if repo is None:
            return {
                "data": {"rateLimit": self._rate(), "repository": None},
                "errors": [{"type": "NOT_FOUND", "message": "Could not resolve to a Repository"}],
            }
        node: "dict[str, object]" = {}
        if variables["withIssues"]:
            items = sorted(self.issues.get(nwo, []), key=lambda n: n["updatedAt"], reverse=True)
            node["issues"] = _page(items, variables["issuesFirst"], variables.get("issuesAfter"))
        if variables["withPrs"]:
            items = sorted(self.prs.get(nwo, []), key=lambda n: n["updatedAt"], reverse=True)
            node["pullRequests"] = _page(items, variables["prsFirst"], variables.get("prsAfter"))
        if variables["withHistory"]:
            if repo["defaultBranchRef"] is None:
                node["defaultBranchRef"] = None
            else:
                since, until = _parse(variables["since"]), _parse(variables["until"])
                items = [
                    c for c in self.commits.get(nwo, [])
                    if since <= _parse(c["committedDate"]) <= until
                ]
                items.sort(key=lambda c: c["committedDate"], reverse=True)
                node["defaultBranchRef"] = {
                    "name": repo["defaultBranchRef"]["name"],
                    "target": {"history": _page(
                        items, variables["historyFirst"], variables.get("historyAfter"),
                    )},
                }
        return {"data": {"rateLimit": self._rate(), "repository": node}}


def make_client(
    opener: object, *, logger: "logging.Logger | None" = None,
    sleeps: "list[float] | None" = None, clock: "float | None" = None, **kwargs: object,
) -> GitHubClient:
    sleep_log = sleeps if sleeps is not None else []
    return GitHubClient(
        TOKEN,
        logger=logger or logging.getLogger("test.github_activity"),
        opener=opener,  # type: ignore[arg-type]
        sleep=sleep_log.append,
        clock=(lambda: clock) if clock is not None else (lambda: 1_800_000_000.0),
        **kwargs,  # type: ignore[arg-type]
    )


def http_error(
    code: int, body: "dict | str | None" = None, headers: "dict[str, str] | None" = None,
) -> urllib.error.HTTPError:
    msg = Message()
    for key, value in (headers or {}).items():
        msg[key] = value
    if isinstance(body, dict):
        raw = json.dumps(body).encode("utf-8")
    elif isinstance(body, str):
        raw = body.encode("utf-8")
    else:
        raw = b""
    return urllib.error.HTTPError(ENDPOINT, code, "error", msg, io.BytesIO(raw))


class ScriptedOpener:
    """呼び出しごとに用意した応答 (dict = JSON 応答 / Exception = raise) を順に返す。"""

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.requests: "list[object]" = []

    def __call__(self, req: object, timeout: float) -> bytes:
        # 再試行時も毎回「同じ GraphQL JSON (bytes)」を送っていること (回帰: 例外処理で
        # 送信 body 変数を上書きし、再試行でエラー本文を POST していたバグ)
        data = req.data  # type: ignore[attr-defined]
        assert isinstance(data, bytes), f"request body が bytes ではない: {type(data)}"
        assert "query" in json.loads(data.decode("utf-8"))
        if self.requests:
            assert data == self.requests[0].data, "再試行で request body が変わった"  # type: ignore[attr-defined]
        self.requests.append(req)
        if not self.responses:
            raise AssertionError("想定外の追加リクエスト")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, bytes):
            return item
        return json.dumps(item).encode("utf-8")


OK_RESPONSE = {"data": {"rateLimit": {"cost": 3, "remaining": 4000}, "viewer": {"login": "x"}}}

#: GitHub がクエリ timeout 時に返す GraphQL エラー (実際の文面に準拠した合成値)
TIMEOUT_GRAPHQL_ERROR = {
    "data": None,
    "errors": [{
        "message": "Something went wrong while executing your query. This may be the "
                   "result of a timeout, or it could be a GitHub bug.",
    }],
}


class FaultInjector:
    """:class:`FakeGitHub` の opener を包み、RepoActivity request に障害を差し込む。

    ``fault(variables)`` が Exception を返せば raise、dict を返せばその JSON を応答、
    ``None`` なら FakeGitHub が通常応答する。失敗した試行も含めて RepoActivity の
    variables を ``repo_calls`` に記録する。
    """

    def __init__(self, fake: FakeGitHub, fault: "Callable[[dict], object]") -> None:
        self.fake = fake
        self.fault = fault
        self.repo_calls: "list[dict]" = []

    def __call__(self, req: object, timeout: float) -> bytes:
        payload = json.loads(req.data.decode("utf-8"))  # type: ignore[attr-defined]
        if "query IhealthRepoActivity" in payload["query"]:
            variables = dict(payload.get("variables") or {})
            self.repo_calls.append(variables)
            injected = self.fault(variables)
            if isinstance(injected, Exception):
                raise injected
            if isinstance(injected, dict):
                return json.dumps(injected).encode("utf-8")
        return self.fake.opener(req, timeout)

    def history_firsts(self) -> "list[int]":
        return [v["historyFirst"] for v in self.repo_calls]


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: "list[str]" = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextlib.contextmanager
def capture_all_logs(name: str) -> "Iterator[tuple[logging.Logger, _ListHandler]]":
    """tests/__init__ の ``logging.disable(INFO)`` を一時解除して DEBUG まで全部拾う。"""
    logger = logging.getLogger(name)
    handler = _ListHandler()
    previous_disable = logging.root.manager.disable
    previous_level = logger.level
    logging.disable(logging.NOTSET)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield logger, handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logging.disable(previous_disable)


def collect(
    fake: FakeGitHub, start: date, end: date, **kwargs: object,
) -> GitHubActivity:
    client = make_client(fake.opener)
    return collect_github_activity(
        client, start, end, logger=logging.getLogger("test.github_activity"),
        **kwargs,  # type: ignore[arg-type]
    )


D18 = date(2026, 7, 18)
D19 = date(2026, 7, 19)
D20 = date(2026, 7, 20)


# ======================================================================
# resolve_github_token
# ======================================================================


class TestResolveGithubToken(unittest.TestCase):
    def _runner(self, *results: object) -> "tuple[list[tuple[list, dict]], object]":
        calls: "list[tuple[list, dict]]" = []
        queue = list(results)

        def runner(args: list, **kwargs: object) -> object:
            calls.append((args, kwargs))
            item = queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        return calls, runner

    def test_env_token_wins_and_is_stripped(self) -> None:
        calls, runner = self._runner()
        token = resolve_github_token(
            f"  {TOKEN}\n", logging.getLogger("test.github_activity.t"), runner=runner,  # type: ignore[arg-type]
        )
        self.assertEqual(token, TOKEN)
        self.assertEqual(calls, [])

    def test_blank_env_falls_back_to_gh_auth_token(self) -> None:
        ok = subprocess.CompletedProcess(["gh"], 0, stdout=f"{TOKEN}\n", stderr="")
        calls, runner = self._runner(ok)
        token = resolve_github_token(
            "   ", logging.getLogger("test.github_activity.t"), gh_candidates=("/opt/gh",), runner=runner,  # type: ignore[arg-type]
        )
        self.assertEqual(token, TOKEN)
        args, kwargs = calls[0]
        self.assertEqual(args, ["/opt/gh", "auth", "token", "--hostname", "github.com"])
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["timeout"], 10.0)
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])

    def test_missing_gh_tries_next_candidate(self) -> None:
        ok = subprocess.CompletedProcess(["gh"], 0, stdout=TOKEN, stderr="")
        calls, runner = self._runner(FileNotFoundError("gh"), ok)
        token = resolve_github_token(
            None, logging.getLogger("test.github_activity.t"), gh_candidates=("/a/gh", "/b/gh"), runner=runner,  # type: ignore[arg-type]
        )
        self.assertEqual(token, TOKEN)
        self.assertEqual([c[0][0] for c in calls], ["/a/gh", "/b/gh"])

    def test_all_failures_return_none_with_warning(self) -> None:
        failed = subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="not logged in")
        timeout = subprocess.TimeoutExpired(["gh"], 10)
        empty = subprocess.CompletedProcess(["gh"], 0, stdout="\n", stderr="")
        spaced = subprocess.CompletedProcess(["gh"], 0, stdout="two words\n", stderr="")
        _calls, runner = self._runner(failed, timeout, empty, spaced)
        with self.assertLogs("test.github_activity.token", level="WARNING") as cm:
            token = resolve_github_token(
                None, logging.getLogger("test.github_activity.token"),
                gh_candidates=("/a", "/b", "/c", "/d"), runner=runner,  # type: ignore[arg-type]
            )
        self.assertIsNone(token)
        self.assertTrue(any("取得できません" in line for line in cm.output))

    def test_token_value_never_logged(self) -> None:
        ok = subprocess.CompletedProcess(["gh"], 0, stdout=TOKEN, stderr="")
        _calls, runner = self._runner(ok)
        with capture_all_logs("test.github_activity.token_debug") as (logger, handler):
            token = resolve_github_token(
                None, logger, gh_candidates=("/a",), runner=runner,  # type: ignore[arg-type]
            )
            resolve_github_token(TOKEN, logger)
        self.assertEqual(token, TOKEN)
        self.assertTrue(handler.messages)  # DEBUG ログ自体は出ている
        self.assertFalse(any(TOKEN in m for m in handler.messages))


# ======================================================================
# GitHubClient
# ======================================================================


class TestGitHubClient(unittest.TestCase):
    def test_request_shape_and_accounting(self) -> None:
        opener = ScriptedOpener(OK_RESPONSE)
        client = make_client(opener)
        data = client.graphql("query Q { viewer { login } }", {"a": 1})
        self.assertEqual(data["viewer"], {"login": "x"})
        req = opener.requests[0]
        self.assertEqual(req.full_url, ENDPOINT)  # type: ignore[attr-defined]
        self.assertEqual(req.get_method(), "POST")  # type: ignore[attr-defined]
        self.assertEqual(req.get_header("Authorization"), f"bearer {TOKEN}")  # type: ignore[attr-defined]
        self.assertEqual(req.get_header("Content-type"), "application/json")  # type: ignore[attr-defined]
        self.assertTrue(req.get_header("User-agent").startswith("ihealth-relay/"))  # type: ignore[attr-defined]
        body = json.loads(req.data.decode("utf-8"))  # type: ignore[attr-defined]
        self.assertEqual(body, {"query": "query Q { viewer { login } }", "variables": {"a": 1}})
        self.assertEqual(client.request_count, 1)
        self.assertEqual(client.cost_total, 3)
        self.assertEqual(client.last_remaining, 4000)

    def test_graphql_errors_raise_with_partial_data(self) -> None:
        opener = ScriptedOpener({
            "data": {"repository": None, "viewer": {"login": "x"}},
            "errors": [{"type": "NOT_FOUND", "message": "Could not resolve to a Repository"}],
        })
        sleeps: "list[float]" = []
        client = make_client(opener, sleeps=sleeps)
        with self.assertRaises(GitHubError) as cm:
            client.graphql("query Q { x }")
        self.assertEqual(cm.exception.error_types, ("NOT_FOUND",))
        self.assertEqual(cm.exception.data, {"repository": None, "viewer": {"login": "x"}})
        self.assertIn("Could not resolve", str(cm.exception))
        self.assertEqual(sleeps, [])
        self.assertEqual(client.request_count, 1)

    def test_http_401_fails_without_retry_and_hides_token(self) -> None:
        opener = ScriptedOpener(http_error(401, {"message": "Bad credentials"}))
        sleeps: "list[float]" = []
        client = make_client(opener, sleeps=sleeps)
        with self.assertRaises(GitHubError) as cm:
            client.graphql("query Q { x }")
        self.assertEqual(cm.exception.status, 401)
        self.assertIn("Bad credentials", str(cm.exception))
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertNotIn(TOKEN, repr(cm.exception))
        self.assertNotIn(TOKEN, repr(client))
        self.assertEqual(sleeps, [])

    def test_403_with_retry_after_waits_then_succeeds(self) -> None:
        opener = ScriptedOpener(
            http_error(403, {"message": "secondary rate limit"}, {"Retry-After": "7"}),
            OK_RESPONSE,
        )
        sleeps: "list[float]" = []
        client = make_client(opener, sleeps=sleeps)
        self.assertEqual(client.graphql("query Q { x }", {"v": 1})["viewer"]["login"], "x")
        self.assertEqual(sleeps, [7.0])
        self.assertEqual(client.request_count, 2)
        retried = json.loads(opener.requests[1].data.decode("utf-8"))  # type: ignore[attr-defined]
        self.assertEqual(retried, {"query": "query Q { x }", "variables": {"v": 1}})

    def test_429_without_headers_backs_off(self) -> None:
        opener = ScriptedOpener(http_error(429), OK_RESPONSE)
        sleeps: "list[float]" = []
        client = make_client(opener, sleeps=sleeps)
        client.graphql("query Q { x }")
        self.assertEqual(len(sleeps), 1)
        self.assertGreater(sleeps[0], 0)
        self.assertLessEqual(sleeps[0], 60.0)

    def test_403_without_rate_limit_signal_is_not_retried(self) -> None:
        opener = ScriptedOpener(http_error(403, {"message": "Resource not accessible"}))
        sleeps: "list[float]" = []
        client = make_client(opener, sleeps=sleeps)
        with self.assertRaises(GitHubError) as cm:
            client.graphql("query Q { x }")
        self.assertEqual(cm.exception.status, 403)
        self.assertFalse(cm.exception.rate_limited)
        self.assertEqual(sleeps, [])

    def test_primary_rate_limit_with_far_reset_fails_fast(self) -> None:
        now = 1_800_000_000.0
        opener = ScriptedOpener(http_error(
            403, {"message": "API rate limit exceeded"},
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(now + 3600))},
        ))
        sleeps: "list[float]" = []
        client = make_client(opener, sleeps=sleeps, clock=now)
        with self.assertRaises(GitHubError) as cm:
            client.graphql("query Q { x }")
        self.assertTrue(cm.exception.rate_limited)
        self.assertEqual(sleeps, [])  # 1 時間も待たない

    def test_primary_rate_limit_with_near_reset_waits(self) -> None:
        now = 1_800_000_000.0
        opener = ScriptedOpener(
            http_error(403, {"message": "API rate limit exceeded"},
                       {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(now + 20))}),
            OK_RESPONSE,
        )
        sleeps: "list[float]" = []
        client = make_client(opener, sleeps=sleeps, clock=now)
        client.graphql("query Q { x }")
        self.assertEqual(sleeps, [20.0])

    def test_retry_after_over_cap_fails_fast(self) -> None:
        opener = ScriptedOpener(http_error(429, None, {"Retry-After": "120"}))
        sleeps: "list[float]" = []
        client = make_client(opener, sleeps=sleeps)
        with self.assertRaises(GitHubError) as cm:
            client.graphql("query Q { x }")
        self.assertTrue(cm.exception.rate_limited)
        self.assertEqual(sleeps, [])

    def test_gives_up_after_three_retries(self) -> None:
        opener = ScriptedOpener(*[http_error(502) for _ in range(4)])
        sleeps: "list[float]" = []
        client = make_client(opener, sleeps=sleeps)
        with self.assertRaises(GitHubError) as cm:
            client.graphql("query Q { x }")
        self.assertEqual(cm.exception.status, 502)
        self.assertTrue(cm.exception.server_timeout)
        self.assertEqual(len(sleeps), 3)
        self.assertEqual(client.request_count, 4)

    def test_server_timeout_flag_by_failure_kind(self) -> None:
        cases: "list[tuple[str, Callable[[], object], bool]]" = [
            ("http 504", lambda: http_error(504), True),
            ("http 502", lambda: http_error(502), True),
            ("http 503", lambda: http_error(503), False),
            ("http 500", lambda: http_error(500), False),
            ("graphql timeout", lambda: TIMEOUT_GRAPHQL_ERROR, True),
            ("connection", lambda: urllib.error.URLError("down"), False),
            ("not found", lambda: {"errors": [{"type": "NOT_FOUND", "message": "nope"}]}, False),
            ("bad credentials", lambda: http_error(401, {"message": "Bad credentials"}), False),
        ]
        for label, make, expected in cases:
            with self.subTest(label):
                client = make_client(ScriptedOpener(*[make() for _ in range(4)]))
                with self.assertRaises(GitHubError) as cm:
                    client.graphql("query Q { x }")
                self.assertIs(cm.exception.server_timeout, expected)

    def test_timeout_retries_limits_only_github_side_timeouts(self) -> None:
        # 502 は timeout_retries=0 なら再送せず server_timeout で即失敗 (呼び出し側が縮小する)
        sleeps: "list[float]" = []
        client = make_client(ScriptedOpener(http_error(502), OK_RESPONSE), sleeps=sleeps)
        with self.assertRaises(GitHubError) as cm:
            client.graphql("query Q { x }", timeout_retries=0)
        self.assertTrue(cm.exception.server_timeout)
        self.assertEqual((client.request_count, sleeps), (1, []))

        # GraphQL の timeout 系エラーも同じ扱い
        client = make_client(ScriptedOpener(TIMEOUT_GRAPHQL_ERROR, OK_RESPONSE))
        with self.assertRaises(GitHubError) as cm:
            client.graphql("query Q { x }", timeout_retries=0)
        self.assertTrue(cm.exception.server_timeout)
        self.assertEqual(client.request_count, 1)

        # timeout_retries=1 → 1 回だけ再送
        client = make_client(ScriptedOpener(http_error(504), http_error(504), OK_RESPONSE))
        with self.assertRaises(GitHubError):
            client.graphql("query Q { x }", timeout_retries=1)
        self.assertEqual(client.request_count, 2)

        # 回帰: 接続エラー・503・レート制限の再試行は timeout_retries で減らさない
        # (以前の server_retries は全種の失敗を数えていたので、縮小可能な request
        # = 各 repo の初回 request で通常の接続エラー再試行が 1 回に減っていた)
        sleeps = []
        client = make_client(
            ScriptedOpener(
                urllib.error.URLError("reset"), http_error(503),
                http_error(429, None, {"Retry-After": "5"}), OK_RESPONSE,
            ),
            sleeps=sleeps,
        )
        self.assertEqual(
            client.graphql("query Q { x }", timeout_retries=0)["viewer"]["login"], "x",
        )
        self.assertEqual(client.request_count, 4)
        self.assertEqual(len(sleeps), 3)
        self.assertEqual(sleeps[2], 5.0)

    def test_url_error_is_retried(self) -> None:
        opener = ScriptedOpener(urllib.error.URLError("connection refused"), OK_RESPONSE)
        sleeps: "list[float]" = []
        client = make_client(opener, sleeps=sleeps)
        client.graphql("query Q { x }")
        self.assertEqual(len(sleeps), 1)

    def test_ssl_error_during_read_is_retried(self) -> None:
        opener = ScriptedOpener(ssl.SSLError("tls reset"), OK_RESPONSE)
        sleeps: "list[float]" = []
        client = make_client(opener, sleeps=sleeps)
        client.graphql("query Q { x }")
        self.assertEqual(len(sleeps), 1)

    def test_timeout_exhausts_retries(self) -> None:
        opener = ScriptedOpener(*[TimeoutError("timed out") for _ in range(4)])
        client = make_client(opener)
        with self.assertRaises(GitHubError) as cm:
            client.graphql("query Q { x }")
        self.assertIn("接続エラー", str(cm.exception))

    def test_invalid_json_raises(self) -> None:
        client = make_client(ScriptedOpener(b"<html>oops</html>"))
        with self.assertRaises(GitHubError):
            client.graphql("query Q { x }")

    def test_missing_data_raises(self) -> None:
        client = make_client(ScriptedOpener({"data": None}))
        with self.assertRaises(GitHubError):
            client.graphql("query Q { x }")

    def test_rate_limited_graphql_error_is_not_retried(self) -> None:
        opener = ScriptedOpener({"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]})
        sleeps: "list[float]" = []
        client = make_client(opener, sleeps=sleeps)
        with self.assertRaises(GitHubError) as cm:
            client.graphql("query Q { x }")
        self.assertTrue(cm.exception.rate_limited)
        self.assertEqual(sleeps, [])

    def test_transient_graphql_error_is_retried(self) -> None:
        opener = ScriptedOpener(
            {"errors": [{"message": "Something went wrong while executing your query."}]},
            OK_RESPONSE,
        )
        sleeps: "list[float]" = []
        client = make_client(opener, sleeps=sleeps)
        client.graphql("query Q { x }")
        self.assertEqual(len(sleeps), 1)

    def test_empty_token_rejected(self) -> None:
        with self.assertRaises(ValueError):
            GitHubClient("  ", logger=logging.getLogger("test.github_activity.t"))


# ======================================================================
# 書式
# ======================================================================


class TestFormatGithubItems(unittest.TestCase):
    def test_empty_is_no_activity(self) -> None:
        self.assertEqual(format_github_items([]), "該当なし")
        self.assertEqual(NO_ACTIVITY_TEXT, "該当なし")

    def test_predecessor_compatible_lines(self) -> None:
        items = [
            GitHubItem(kind="issue", repo="alpha", url="https://github.com/octo-user/alpha/issues/3",
                       number=3, title="[SETUP] synthetic: 設定"),
            GitHubItem(kind="pr", repo="alpha", url="https://github.com/octo-user/alpha/pull/12",
                       number=12, title="feat(x): synthetic change (1.0.0)"),
            GitHubItem(kind="commit", repo="beta", url="https://github.com/octo-org/beta/commits/main",
                       additions=120, deletions=34, commit_count=2),
        ]
        self.assertEqual(
            format_github_items(items),
            "[🎫 alpha:Issue #3: [SETUP] synthetic: 設定](https://github.com/octo-user/alpha/issues/3)\n"
            "[🔀 alpha:PR #12: feat(x): synthetic change (1.0.0)](https://github.com/octo-user/alpha/pull/12)\n"
            "[📝 beta:変更行数:+120-34 (2 commits)](https://github.com/octo-org/beta/commits/main)",
        )

    def test_one_commit_keeps_plural_commits_like_predecessor(self) -> None:
        item = GitHubItem(kind="commit", repo="r", url="u", additions=0, deletions=0, commit_count=1)
        self.assertEqual(format_github_items([item]), "[📝 r:変更行数:+0-0 (1 commits)](u)")

    def test_title_newlines_become_spaces(self) -> None:
        item = GitHubItem(kind="pr", repo="r", url="u", number=1, title="a\r\nb\nc\rd e")
        self.assertEqual(format_github_items([item]), "[🔀 r:PR #1: a b c d e](u)")
        self.assertNotIn("\n", format_github_items([item]))

    def test_unknown_kind_raises(self) -> None:
        with self.assertRaises(ValueError):
            format_github_items([GitHubItem(kind="release", repo="r", url="u")])


# ======================================================================
# collect_github_activity
# ======================================================================


class TestRepoEnumeration(unittest.TestCase):
    def test_org_repos_listed_via_repository_owner(self) -> None:
        # viewer.repositories(ORGANIZATION_MEMBER) は org の private repo を返さない
        # ことがあるため、owner ごとに repositoryOwner で列挙していることの回帰テスト
        fake = FakeGitHub(viewer="octo-user", orgs=["octo-org"])
        user_repo = fake.add_repo("octo-user", "alpha", pushed_at=at_jst(2026, 7, 18))
        org_repo = fake.add_repo("octo-org", "beta", pushed_at=at_jst(2026, 7, 18, 13))
        fake.add_pr(user_repo, 1, at_jst(2026, 7, 18, 9))
        fake.add_pr(org_repo, 2, at_jst(2026, 7, 18, 10))
        activity = collect(fake, D18, D18)
        self.assertTrue(activity.complete)
        self.assertEqual(
            [v["login"] for v in fake.ops("IhealthOwnerRepos")], ["octo-user", "octo-org"],
        )
        self.assertFalse(any("ORGANIZATION_MEMBER" in q for q in fake.queries))
        self.assertTrue(any("repositoryOwner" in q and "ownerAffiliations: [OWNER]" in q
                            for q in fake.queries))
        self.assertEqual(
            [item.repo for item in activity.days[D18]], ["beta", "alpha"],  # 最終イベント降順
        )
        self.assertEqual(activity.repos_listed, 2)
        self.assertEqual(activity.repos_scanned, 2)

    def test_candidate_filter(self) -> None:
        fake = FakeGitHub(viewer="octo-user", orgs=[])
        old = at_jst(2026, 7, 1)
        fake.add_repo("octo-user", "pushed", pushed_at=at_jst(2026, 7, 18, 1))
        fake.add_repo("octo-user", "updated-only", pushed_at=old, updated_at=at_jst(2026, 7, 18, 2))
        fake.add_repo("octo-user", "stale", pushed_at=old)
        fake.add_repo("octo-user", "archived", pushed_at=at_jst(2026, 7, 18, 3), archived=True)
        fake.add_repo("octo-user", "forked", pushed_at=at_jst(2026, 7, 18, 4), fork=True)
        fake.add_repo("octo-user", "Excluded", pushed_at=at_jst(2026, 7, 18, 5))
        fake.add_repo("octo-user", "empty", pushed_at=None, updated_at=None)
        # 期間開始ちょうど (JST 0:00) の push は対象、1 秒前は対象外
        fake.add_repo("octo-user", "edge-in", pushed_at=at_jst(2026, 7, 18, 0, 0, 0))
        fake.add_repo("octo-user", "edge-out", pushed_at=at_jst(2026, 7, 17, 23, 59, 59))

        collect(fake, D18, D18, exclude_repos=[" OCTO-USER/excluded "])
        self.assertEqual(
            sorted(fake.scanned()),
            sorted([
                "octo-user/pushed", "octo-user/updated-only", "octo-user/archived",
                "octo-user/forked", "octo-user/edge-in",
            ]),
        )

        fake.calls.clear()
        collect(fake, D18, D18, include_archived=False)
        self.assertNotIn("octo-user/archived", fake.scanned())
        self.assertIn("octo-user/Excluded", fake.scanned())

    def test_owners_restricts_scope_and_skips_viewer_lookup(self) -> None:
        fake = FakeGitHub(viewer="octo-user", orgs=["octo-org"])
        fake.add_repo("octo-user", "alpha", pushed_at=at_jst(2026, 7, 18))
        fake.add_repo("octo-org", "beta", pushed_at=at_jst(2026, 7, 18))
        activity = collect(fake, D18, D18, owners=["octo-org", "OCTO-ORG", " "])
        self.assertTrue(activity.complete)
        self.assertEqual(fake.ops("IhealthViewer"), [])
        self.assertEqual([v["login"] for v in fake.ops("IhealthOwnerRepos")], ["octo-org"])
        self.assertEqual(fake.scanned(), ["octo-org/beta"])

    def test_unknown_owner_marks_incomplete(self) -> None:
        fake = FakeGitHub(viewer="octo-user", orgs=[])
        fake.add_repo("octo-user", "alpha", pushed_at=at_jst(2026, 7, 18))
        activity = collect(fake, D18, D18, owners=["octo-user", "no-such-owner"])
        self.assertFalse(activity.complete)
        self.assertEqual(activity.status, "partial")
        self.assertTrue(any("no-such-owner" in e for e in activity.errors))
        self.assertEqual(fake.scanned(), ["octo-user/alpha"])


class TestOwnerAndExcludeArguments(unittest.TestCase):
    """``owners`` / ``exclude_repos`` は list のほかカンマ区切りの str (.env の値そのまま)
    も受け付ける。回帰: str を Sequence として 1 文字ずつ owner / 除外名にしていた。"""

    def _fake(self) -> FakeGitHub:
        fake = FakeGitHub(viewer="octo-user", orgs=["octo-org"])
        alpha = fake.add_repo("octo-user", "alpha", pushed_at=at_jst(2026, 7, 18, 9))
        beta = fake.add_repo("octo-org", "beta", pushed_at=at_jst(2026, 7, 18, 10))
        fake.add_pr(alpha, 1, at_jst(2026, 7, 18, 9))
        fake.add_pr(beta, 2, at_jst(2026, 7, 18, 10))
        return fake

    def test_single_owner_as_str(self) -> None:
        fake = self._fake()
        activity = collect(fake, D18, D18, owners="octo-org")
        self.assertTrue(activity.complete, activity.errors)
        self.assertEqual(fake.ops("IhealthViewer"), [])
        self.assertEqual([v["login"] for v in fake.ops("IhealthOwnerRepos")], ["octo-org"])
        self.assertEqual(fake.scanned(), ["octo-org/beta"])
        self.assertEqual([(i.repo, i.number) for i in activity.days[D18]], [("beta", 2)])

    def test_comma_separated_owners_with_spaces_and_blanks(self) -> None:
        fake = self._fake()
        activity = collect(fake, D18, D18, owners=" octo-user , OCTO-ORG ,, octo-org,")
        self.assertTrue(activity.complete, activity.errors)
        self.assertEqual(
            [v["login"] for v in fake.ops("IhealthOwnerRepos")], ["octo-user", "OCTO-ORG"],
        )
        self.assertEqual(sorted(fake.scanned()), ["octo-org/beta", "octo-user/alpha"])

    def test_list_elements_are_split_on_commas_too(self) -> None:
        fake = self._fake()
        collect(fake, D18, D18, owners=["octo-user,octo-org"])
        self.assertEqual(
            [v["login"] for v in fake.ops("IhealthOwnerRepos")], ["octo-user", "octo-org"],
        )

    def test_blank_owner_str_means_viewer_and_orgs(self) -> None:
        fake = self._fake()
        activity = collect(fake, D18, D18, owners=" , ")
        self.assertTrue(activity.complete)
        self.assertEqual(len(fake.ops("IhealthViewer")), 1)
        self.assertEqual(sorted(fake.scanned()), ["octo-org/beta", "octo-user/alpha"])

    def test_exclude_repos_as_str(self) -> None:
        fake = self._fake()
        activity = collect(fake, D18, D18, exclude_repos="octo-org/beta")
        self.assertTrue(activity.complete)
        self.assertEqual(fake.scanned(), ["octo-user/alpha"])
        self.assertEqual([(i.repo, i.number) for i in activity.days[D18]], [("alpha", 1)])

    def test_comma_separated_exclude_repos(self) -> None:
        fake = self._fake()
        activity = collect(fake, D18, D18, exclude_repos=" Octo-Org/BETA , octo-user/alpha ")
        self.assertTrue(activity.complete)
        self.assertEqual(fake.scanned(), [])
        self.assertEqual(activity.text_for(D18), "該当なし")

    def test_invalid_owner_raises_before_any_request(self) -> None:
        for bad in ("octo-org/beta", "https://github.com/octo-org", "octo org", "-octo",
                    "@octo-user", "x" * 40):
            with self.subTest(owner=bad):
                fake = self._fake()
                with self.assertRaises(ValueError):
                    collect(fake, D18, D18, owners=f"octo-user,{bad}")
                self.assertEqual(fake.calls, [])

    def test_invalid_exclude_raises_before_any_request(self) -> None:
        for bad in ("beta", "octo-org/", "/beta", "octo-org/beta/extra", "octo org/beta",
                    "octo-org/be ta"):
            with self.subTest(exclude=bad):
                fake = self._fake()
                with self.assertRaises(ValueError):
                    collect(fake, D18, D18, exclude_repos=["octo-user/alpha", bad])
                self.assertEqual(fake.calls, [])

    def test_non_str_entry_raises_type_error(self) -> None:
        fake = self._fake()
        with self.assertRaises(TypeError):
            collect(fake, D18, D18, owners=["octo-org", 42])
        with self.assertRaises(TypeError):
            collect(fake, D18, D18, exclude_repos=[None])
        self.assertEqual(fake.calls, [])

    def test_valid_login_and_repo_name_shapes_are_accepted(self) -> None:
        fake = FakeGitHub(viewer="octo-user", orgs=[])
        activity = collect(
            fake, D18, D18,
            owners="a,Octo-User,octo_shortcode,x" + "y" * 38,
            exclude_repos="octo-user/.github,octo-user/my_repo.v2,octo-user/A-b",
        )
        self.assertEqual(
            [v["login"] for v in fake.ops("IhealthOwnerRepos")],
            ["a", "Octo-User", "octo_shortcode", "x" + "y" * 38],
        )
        self.assertFalse(activity.complete)  # Fake に存在しない owner は不完全扱い


class TestWindowBoundaries(unittest.TestCase):
    def test_jst_day_boundaries_and_utc_variables(self) -> None:
        fake = FakeGitHub(viewer="octo-user", orgs=[])
        nwo = fake.add_repo("octo-user", "alpha", pushed_at=at_jst(2026, 7, 20, 1))
        fake.add_issue(nwo, 1, at_jst(2026, 7, 17, 23, 59, 59))  # 前日 → 対象外
        fake.add_issue(nwo, 2, at_jst(2026, 7, 18, 0, 0, 0))     # 開始ちょうど → 18 日
        fake.add_issue(nwo, 3, at_jst(2026, 7, 19, 23, 59, 59))  # 終了日の最後 → 19 日
        fake.add_issue(nwo, 4, at_jst(2026, 7, 20, 0, 0, 0))     # 終了の翌日 0:00 → 対象外
        fake.add_pr(nwo, 5, at_jst(2026, 7, 18, 8, 59, 59))      # UTC では 17 日 → JST 18 日
        fake.add_commit(nwo, "c-start", at_jst(2026, 7, 18, 0, 0, 0), additions=1)
        fake.add_commit(nwo, "c-end", at_jst(2026, 7, 20, 0, 0, 0), additions=100)  # until inclusive でも除外
        fake.add_commit(nwo, "c-19", at_jst(2026, 7, 19, 9), additions=10, deletions=2)

        activity = collect(fake, D18, D19)
        self.assertTrue(activity.complete)
        first = fake.ops("IhealthRepoActivity")[0]
        self.assertEqual(first["since"], "2026-07-17T15:00:00Z")
        self.assertEqual(first["until"], "2026-07-19T15:00:00Z")
        self.assertEqual(
            activity.text_for(D18),
            "[🎫 alpha:Issue #2: synthetic issue 2](https://github.com/octo-user/alpha/issues/2)\n"
            "[🔀 alpha:PR #5: synthetic pr 5](https://github.com/octo-user/alpha/pull/5)\n"
            "[📝 alpha:変更行数:+1-0 (1 commits)](https://github.com/octo-user/alpha/commits/main)",
        )
        self.assertEqual(
            activity.text_for(D19),
            "[🎫 alpha:Issue #3: synthetic issue 3](https://github.com/octo-user/alpha/issues/3)\n"
            "[📝 alpha:変更行数:+10-2 (1 commits)](https://github.com/octo-user/alpha/commits/main)",
        )

    def test_custom_tz(self) -> None:
        fake = FakeGitHub(viewer="octo-user", orgs=[])
        nwo = fake.add_repo("octo-user", "alpha", pushed_at=iso(datetime(2026, 7, 18, 20, tzinfo=UTC)))
        # UTC 2026-07-18 23:30 = JST 19 日だが tz=UTC なら 18 日
        fake.add_pr(nwo, 7, iso(datetime(2026, 7, 18, 23, 30, tzinfo=UTC)))
        activity = collect(fake, D18, D18, tz=UTC)
        self.assertEqual([i.number for i in activity.days[D18]], [7])
        self.assertEqual(fake.ops("IhealthRepoActivity")[0]["since"], "2026-07-18T00:00:00Z")

    def test_all_days_present_and_no_activity_text(self) -> None:
        fake = FakeGitHub(viewer="octo-user", orgs=[])
        nwo = fake.add_repo("octo-user", "alpha", pushed_at=at_jst(2026, 7, 19))
        fake.add_pr(nwo, 1, at_jst(2026, 7, 19))
        activity = collect(fake, D18, D20)
        self.assertEqual(sorted(activity.days), [D18, D19, D20])
        self.assertEqual(activity.text_for(D18), "該当なし")
        self.assertEqual(activity.text_for(D20), "該当なし")
        self.assertIsNone(activity.text_for(date(2026, 7, 21)))  # 期間外は書かない
        self.assertEqual(activity.items_for(D19)[0].kind, "pr")

    def test_invalid_range_raises(self) -> None:
        with self.assertRaises(ValueError):
            collect(FakeGitHub(), D19, D18)


class TestCommitRules(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeGitHub(viewer="octo-user", orgs=[])
        self.nwo = self.fake.add_repo("octo-user", "alpha", pushed_at=at_jst(2026, 7, 18, 20))

    def test_merge_and_pr_branch_commits_excluded_squash_and_rebase_counted(self) -> None:
        f, nwo, t = self.fake, self.nwo, at_jst(2026, 7, 18, 10)
        merged = at_jst(2026, 7, 18, 11)
        # 通常 merge: merge commit (parent 2) と PR ブランチ commit は除外
        f.add_commit(nwo, "m1", t, additions=1000, parents=2, prs=[pr_ref(5, merged, "m1", 2)])
        f.add_commit(nwo, "b1", t, additions=2000, prs=[pr_ref(5, merged, "m1", 2)])
        # squash merge: mergeCommit == 自分 → 直コミット扱い
        f.add_commit(nwo, "s1", t, additions=10, deletions=1, prs=[pr_ref(6, merged, "s1", 1)])
        # rebase merge: mergeCommit (parent 1) は別 commit だが default branch 上 → 直コミット
        f.add_commit(nwo, "r1", t, additions=20, deletions=2, prs=[pr_ref(7, merged, "r2", 1)])
        f.add_commit(nwo, "r2", t, additions=30, deletions=3, prs=[pr_ref(7, merged, "r2", 1)])
        # merged なのに mergeCommit 不明 → 保守的に除外
        f.add_commit(nwo, "u1", t, additions=4000, prs=[pr_ref(8, merged, None)])
        # 未 merge (open) の PR に紐づくだけ → 直コミット
        f.add_commit(nwo, "o1", t, additions=40, deletions=4, prs=[pr_ref(9, None, None)])
        # PR 無しの直 push
        f.add_commit(nwo, "d1", t, additions=50, deletions=5)

        activity = collect(f, D18, D18)
        self.assertEqual(
            activity.text_for(D18),
            "[📝 alpha:変更行数:+150-15 (5 commits)](https://github.com/octo-user/alpha/commits/main)",
        )

    def test_squash_merged_prs_count_as_commits_like_predecessor(self) -> None:
        f, nwo = self.fake, self.nwo
        for n, (oid, adds) in enumerate([("s1", 100), ("s2", 200), ("s3", 300)], start=11):
            moment = at_jst(2026, 7, 18, 7 + n - 11)
            f.add_pr(nwo, n, moment)
            f.add_commit(nwo, oid, moment, additions=adds, deletions=n - 10,
                         prs=[pr_ref(n, moment, oid, 1)])
        activity = collect(f, D18, D18)
        lines = activity.text_for(D18).split("\n")  # type: ignore[union-attr]
        self.assertEqual([line.split(":")[1] for line in lines[:3]], ["PR #13", "PR #12", "PR #11"])
        self.assertEqual(
            lines[3],
            "[📝 alpha:変更行数:+600-6 (3 commits)](https://github.com/octo-user/alpha/commits/main)",
        )

    def test_bot_commits_excluded_unless_include_bots(self) -> None:
        f, nwo, t = self.fake, self.nwo, at_jst(2026, 7, 18, 10)
        f.add_commit(nwo, "h1", t, additions=1)
        f.add_commit(nwo, "b-login", t, additions=10, login="dependabot[bot]", name="dependabot")
        f.add_commit(nwo, "b-name", t, additions=100, login=None, name="github-actions[bot]")
        f.add_commit(nwo, "b-mail", t, additions=1000, login=None, name="automation",
                     email="12345+renovate[bot]@users.noreply.github.com")
        self.assertEqual(
            collect(f, D18, D18).days[D18][0].commit_count, 1,
        )
        with_bots = collect(f, D18, D18, include_bots=True).days[D18][0]
        self.assertEqual((with_bots.commit_count, with_bots.additions), (4, 1111))

    def test_associated_pr_overflow_warns(self) -> None:
        f, nwo, t = self.fake, self.nwo, at_jst(2026, 7, 18, 10)
        f.add_commit(nwo, "x1", t, prs=[pr_ref(1, None, None)], pr_total=12)
        with self.assertLogs("test.github_activity", level="WARNING") as cm:
            activity = collect(f, D18, D18)
        self.assertTrue(any("関連 PR が 12 件" in line for line in cm.output))
        self.assertEqual(activity.days[D18][0].commit_count, 1)

    def test_commit_link_uses_default_branch(self) -> None:
        nwo = self.fake.add_repo("octo-org", "trunky", pushed_at=at_jst(2026, 7, 18), branch="trunk")
        self.fake.orgs.append("octo-org")
        self.fake.add_commit(nwo, "t1", at_jst(2026, 7, 18, 9), additions=3)
        items = collect(self.fake, D18, D18).days[D18]
        self.assertEqual(items[0].url, "https://github.com/octo-org/trunky/commits/trunk")

    def test_repo_without_default_branch(self) -> None:
        nwo = self.fake.add_repo("octo-user", "blank", pushed_at=at_jst(2026, 7, 18), branch=None)
        self.fake.add_issue(nwo, 1, at_jst(2026, 7, 18, 9))
        activity = collect(self.fake, D18, D18)
        self.assertTrue(activity.complete)
        self.assertEqual([i.kind for i in activity.days[D18]], ["issue"])


class TestOrdering(unittest.TestCase):
    """日内の並び: 種別 → repo (その日の最終イベント時刻の降順, 同時刻は owner/name 昇順)
    → number 降順。現在の pushedAt には依存しない (lookback 再実行で過去日が揺れない)。"""

    def test_kind_then_repo_by_last_event_of_the_day_then_number_desc(self) -> None:
        fake = FakeGitHub(viewer="octo-user", orgs=["octo-org"])
        # pushedAt は xray が最新だが、18 日のイベントは yankee のほうが遅い
        x = fake.add_repo("octo-user", "xray", pushed_at=at_jst(2026, 7, 18, 23))
        y = fake.add_repo("octo-org", "yankee", pushed_at=at_jst(2026, 7, 18, 21), branch="trunk")
        fake.add_issue(x, 2, at_jst(2026, 7, 18, 9))
        fake.add_issue(x, 9, at_jst(2026, 7, 18, 8))
        fake.add_issue(y, 4, at_jst(2026, 7, 18, 7))
        fake.add_pr(x, 3, at_jst(2026, 7, 18, 10))
        fake.add_pr(y, 8, at_jst(2026, 7, 18, 11))
        fake.add_pr(y, 10, at_jst(2026, 7, 18, 6))
        # yankee の最終イベント = commit 20:00 (種別をまたいで repo 順を決める)
        fake.add_commit(y, "y1", at_jst(2026, 7, 18, 20), additions=5, deletions=1)
        fake.add_commit(x, "x1", at_jst(2026, 7, 18, 12), additions=7)
        activity = collect(fake, D18, D18)
        self.assertEqual(
            [(i.kind, i.repo, i.number) for i in activity.days[D18]],
            [
                ("issue", "yankee", 4), ("issue", "xray", 9), ("issue", "xray", 2),
                ("pr", "yankee", 10), ("pr", "yankee", 8), ("pr", "xray", 3),
                ("commit", "yankee", None), ("commit", "xray", None),
            ],
        )
        self.assertEqual(
            activity.days[D18][6].url, "https://github.com/octo-org/yankee/commits/trunk",
        )

    def test_each_day_has_its_own_repo_order(self) -> None:
        fake = FakeGitHub(viewer="octo-user", orgs=[])
        a = fake.add_repo("octo-user", "alpha", pushed_at=at_jst(2026, 7, 19, 23))
        b = fake.add_repo("octo-user", "bravo", pushed_at=at_jst(2026, 7, 19, 22))
        fake.add_pr(a, 1, at_jst(2026, 7, 18, 9))
        fake.add_pr(b, 2, at_jst(2026, 7, 18, 10))   # 18 日は bravo が後
        fake.add_pr(a, 3, at_jst(2026, 7, 19, 15))   # 19 日は alpha が後
        fake.add_pr(b, 4, at_jst(2026, 7, 19, 14))
        activity = collect(fake, D18, D19)
        self.assertEqual([i.number for i in activity.days[D18]], [2, 1])
        self.assertEqual([i.number for i in activity.days[D19]], [3, 4])

    def test_past_day_text_does_not_change_after_unrelated_later_push(self) -> None:
        # 回帰: 以前は repo 順 = 実行時点の pushedAt 降順だったため、20 日の無関係な
        # push だけで 18 日の行順が入れ替わり、内容が同じノートを毎回書き換えていた
        def run(alpha_pushed_last: bool) -> "GitHubActivity":
            fake = FakeGitHub(viewer="octo-user", orgs=["octo-org"])
            a = fake.add_repo("octo-user", "alpha",
                              pushed_at=at_jst(2026, 7, 20 if alpha_pushed_last else 18, 22))
            b = fake.add_repo("octo-org", "bravo",
                              pushed_at=at_jst(2026, 7, 18 if alpha_pushed_last else 20, 22))
            fake.add_pr(a, 5, at_jst(2026, 7, 18, 9))
            fake.add_pr(b, 7, at_jst(2026, 7, 18, 10))
            fake.add_commit(a, "a1", at_jst(2026, 7, 18, 11), additions=3)
            fake.add_commit(b, "b1", at_jst(2026, 7, 18, 8), additions=4)
            later = a if alpha_pushed_last else b
            fake.add_commit(later, "late", at_jst(2026, 7, 20, 21), additions=99)
            return collect(fake, D18, D20)

        first, second = run(True), run(False)
        self.assertEqual(first.text_for(D18), second.text_for(D18))
        self.assertEqual(
            first.text_for(D18),
            # 18 日の最終イベント: alpha = commit 11:00, bravo = PR 10:00 → alpha が先
            "[🔀 alpha:PR #5: synthetic pr 5](https://github.com/octo-user/alpha/pull/5)\n"
            "[🔀 bravo:PR #7: synthetic pr 7](https://github.com/octo-org/bravo/pull/7)\n"
            "[📝 alpha:変更行数:+3-0 (1 commits)](https://github.com/octo-user/alpha/commits/main)\n"
            "[📝 bravo:変更行数:+4-0 (1 commits)](https://github.com/octo-org/bravo/commits/main)",
        )

    def test_same_last_event_time_ties_break_by_owner_name_ascending(self) -> None:
        fake = FakeGitHub(viewer="octo-user", orgs=["octo-org"])
        t = at_jst(2026, 7, 18, 10)
        # 列挙順・pushedAt 順とは逆の owner/name 順になることを確認する
        z = fake.add_repo("octo-user", "Zulu", pushed_at=at_jst(2026, 7, 18, 23))
        m = fake.add_repo("octo-user", "mike", pushed_at=at_jst(2026, 7, 18, 22))
        o = fake.add_repo("octo-org", "oscar", pushed_at=at_jst(2026, 7, 18, 21))
        for nwo, number in ((z, 1), (m, 2), (o, 3)):
            fake.add_pr(nwo, number, t)
        activity = collect(fake, D18, D18)
        self.assertEqual(
            [i.repo for i in activity.days[D18]],
            ["oscar", "mike", "Zulu"],  # octo-org/oscar < octo-user/mike < octo-user/zulu
        )


class TestPagination(unittest.TestCase):
    def test_101_repos_51_issues_51_prs_101_commits(self) -> None:
        fake = FakeGitHub(viewer="octo-user", orgs=[])
        old = at_jst(2026, 6, 1)
        for n in range(100):  # page 1 を埋める非活動 repo (pushedAt は期間前で新しい順)
            fake.add_repo("octo-user", f"idle-{n:03d}", pushed_at=at_jst(2026, 6, 30, 12, n % 60))
        # pushedAt が最古 = 2 page 目に来る repo。updatedAt だけ期間内 (issue close のみ)
        late = fake.add_repo("octo-user", "late-page", pushed_at=old, updated_at=at_jst(2026, 7, 18, 23))
        for n in range(1, 52):
            fake.add_issue(late, n, at_jst(2026, 7, 18, 10, n % 60))
        # 1 page 目に来る活発な repo: PR 51 件 + commit 101 件
        busy = fake.add_repo("octo-user", "busy", pushed_at=at_jst(2026, 7, 18, 23, 30))
        for n in range(1, 52):
            fake.add_pr(busy, n, at_jst(2026, 7, 18, 11, n % 60))
        for n in range(101):
            fake.add_commit(busy, f"c{n:03d}", at_jst(2026, 7, 18, 1 + n // 60, n % 60),
                            additions=2, deletions=1)

        activity = collect(fake, D18, D18)
        self.assertTrue(activity.complete, activity.errors)
        self.assertEqual(activity.repos_listed, 102)
        repo_pages = fake.ops("IhealthOwnerRepos")
        self.assertEqual([v["after"] for v in repo_pages], [None, "100"])
        items = activity.days[D18]
        issues = [i for i in items if i.kind == "issue"]
        prs = [i for i in items if i.kind == "pr"]
        commits = [i for i in items if i.kind == "commit"]
        self.assertEqual(sorted(i.number for i in issues), list(range(1, 52)))  # type: ignore[type-var]
        self.assertEqual(sorted(i.number for i in prs), list(range(1, 52)))  # type: ignore[type-var]
        self.assertEqual(len(commits), 1)
        self.assertEqual(
            (commits[0].commit_count, commits[0].additions, commits[0].deletions), (101, 202, 101),
        )
        busy_calls = [v for v in fake.ops("IhealthRepoActivity") if v["name"] == "busy"]
        self.assertEqual(len(busy_calls), 2)
        second = busy_calls[1]
        self.assertFalse(second["withIssues"])        # issue は 1 page で完了
        self.assertTrue(second["withPrs"])
        self.assertEqual(second["prsAfter"], "50")
        self.assertTrue(second["withHistory"])
        self.assertEqual(second["historyAfter"], "100")
        late_calls = [v for v in fake.ops("IhealthRepoActivity") if v["name"] == "late-page"]
        self.assertEqual([v["issuesAfter"] for v in late_calls], [None, "50"])
        # 2 page 目以降の PR / issue も number 降順で並ぶ
        self.assertEqual([i.number for i in prs][:3], [51, 50, 49])

    def test_issue_paging_stops_once_older_than_window(self) -> None:
        fake = FakeGitHub(viewer="octo-user", orgs=[])
        nwo = fake.add_repo("octo-user", "alpha", pushed_at=at_jst(2026, 7, 18, 20))
        fake.add_issue(nwo, 1000, at_jst(2026, 7, 18, 9))
        for n in range(1, 120):  # 期間より前に更新された古い closed issue が大量にある
            fake.add_issue(nwo, n, at_jst(2026, 5, 1), updated_at=at_jst(2026, 6, 1, 0, n % 60))
        activity = collect(fake, D18, D18)
        self.assertTrue(activity.complete)
        self.assertEqual(len(fake.ops("IhealthRepoActivity")), 1)
        self.assertEqual([i.number for i in activity.days[D18]], [1000])

    def test_page_cap_marks_incomplete(self) -> None:
        fake = FakeGitHub(viewer="octo-user", orgs=[])
        nwo = fake.add_repo("octo-user", "alpha", pushed_at=at_jst(2026, 7, 18, 20))
        for n in range(1, 52):
            fake.add_issue(nwo, n, at_jst(2026, 7, 18, 9, n % 60))
        activity = collect(fake, D18, D18, max_pages=1)
        self.assertFalse(activity.complete)
        self.assertTrue(any("pagination" in e for e in activity.errors))
        self.assertIsNone(activity.text_for(D18))


class TestFailures(unittest.TestCase):
    def test_one_repo_failure_continues_and_marks_incomplete(self) -> None:
        fake = FakeGitHub(viewer="octo-user", orgs=["octo-org"])
        good = fake.add_repo("octo-user", "good", pushed_at=at_jst(2026, 7, 18, 20))
        bad = fake.add_repo("octo-org", "bad", pushed_at=at_jst(2026, 7, 18, 21))
        fake.add_pr(good, 1, at_jst(2026, 7, 18, 9))
        fake.repo_errors[bad.lower()] = [{"type": "FORBIDDEN", "message": "Resource not accessible"}]
        with self.assertLogs("test.github_activity", level="WARNING"):
            activity = collect(fake, D18, D18)
        self.assertFalse(activity.complete)
        self.assertEqual(activity.status, "partial")
        self.assertEqual(activity.repos_scanned, 1)
        self.assertEqual([(i.repo, i.number) for i in activity.days[D18]], [("good", 1)])
        self.assertTrue(any(e.startswith("octo-org/bad:") for e in activity.errors))
        self.assertIsNone(activity.text_for(D18))  # 不完全な日は書かない

    def test_owner_listing_failure_marks_incomplete(self) -> None:
        fake = FakeGitHub(viewer="octo-user", orgs=["octo-org"])
        good = fake.add_repo("octo-user", "good", pushed_at=at_jst(2026, 7, 18, 20))
        fake.add_pr(good, 1, at_jst(2026, 7, 18, 9))
        fake.owner_errors["octo-org"] = [{"type": "FORBIDDEN", "message": "SAML enforcement"}]
        activity = collect(fake, D18, D18)
        self.assertFalse(activity.complete)
        self.assertEqual(fake.scanned(), ["octo-user/good"])
        self.assertTrue(any(e.startswith("octo-org:") for e in activity.errors))

    def test_viewer_failure_is_failed_and_hides_token(self) -> None:
        fake = FakeGitHub()
        fake.viewer_exception = http_error(401, {"message": "Bad credentials"})
        with capture_all_logs("test.github_activity.secret") as (logger, handler):
            client = make_client(fake.opener, logger=logger)
            activity = collect_github_activity(client, D18, D19, logger=logger)
        self.assertFalse(activity.complete)
        self.assertEqual(activity.status, "failed")
        self.assertEqual(sorted(activity.days), [D18, D19])
        self.assertEqual(activity.days[D18], [])
        self.assertIsNone(activity.text_for(D18))
        self.assertTrue(any("Bad credentials" in e for e in activity.errors))
        joined = "\n".join(handler.messages + activity.errors)
        self.assertNotIn(TOKEN, joined)

    def test_token_not_in_debug_logs_of_successful_run(self) -> None:
        fake = FakeGitHub()
        nwo = fake.add_repo("octo-user", "alpha", pushed_at=at_jst(2026, 7, 18))
        fake.add_pr(nwo, 1, at_jst(2026, 7, 18, 9))
        with capture_all_logs("test.github_activity.debug") as (logger, handler):
            client = make_client(fake.opener, logger=logger)
            activity = collect_github_activity(client, D18, D18, logger=logger)
        self.assertTrue(activity.complete)
        self.assertTrue(handler.messages)
        self.assertFalse(any(TOKEN in m for m in handler.messages))

    def test_request_and_cost_accounting(self) -> None:
        fake = FakeGitHub()
        nwo = fake.add_repo("octo-user", "alpha", pushed_at=at_jst(2026, 7, 18))
        fake.add_pr(nwo, 1, at_jst(2026, 7, 18, 9))
        activity = collect(fake, D18, D18)
        self.assertEqual(activity.requests, len(fake.calls))
        self.assertEqual(activity.cost, len(fake.calls))  # Fake は 1 request = cost 1

    def test_malformed_node_is_repo_failure(self) -> None:
        fake = FakeGitHub()
        nwo = fake.add_repo("octo-user", "alpha", pushed_at=at_jst(2026, 7, 18))
        fake.add_commit(nwo, "c1", at_jst(2026, 7, 18, 9))
        fake.commits[nwo.lower()][0]["additions"] = "many"  # 型違反
        activity = collect(fake, D18, D18)
        self.assertFalse(activity.complete)
        self.assertTrue(any("additions" in e for e in activity.errors))


class TestHistoryPageShrink(unittest.TestCase):
    """GitHub 側 timeout (502/504 / GraphQL timeout) で history の page 件数を
    100 → 50 → 25 → 10 と縮小し、同じ cursor から取り直す。"""

    def _fake_with_commits(self, count: int) -> "tuple[FakeGitHub, str]":
        fake = FakeGitHub(viewer="octo-user", orgs=[])
        nwo = fake.add_repo("octo-user", "alpha", pushed_at=at_jst(2026, 7, 18, 23))
        for n in range(count):
            fake.add_commit(nwo, f"c{n:03d}", at_jst(2026, 7, 18, 1 + n // 60, n % 60),
                            additions=2, deletions=1)
        return fake, nwo

    def _collect(
        self, injector: FaultInjector, sleeps: "list[float] | None" = None, **kwargs: object,
    ) -> GitHubActivity:
        client = make_client(injector, sleeps=sleeps)
        return collect_github_activity(
            client, D18, D18, logger=logging.getLogger("test.github_activity"),
            **kwargs,  # type: ignore[arg-type]
        )

    def test_shrinks_from_same_cursor_and_stays_shrunk_for_the_repo(self) -> None:
        fake, _nwo = self._fake_with_commits(60)
        injector = FaultInjector(
            fake,
            lambda v: http_error(502) if v["withHistory"] and v["historyFirst"] > 25 else None,
        )
        with self.assertLogs("test.github_activity", level="WARNING") as cm:
            activity = self._collect(injector)
        self.assertTrue(activity.complete, activity.errors)
        # 100 で 1 回再送 → 50 で 1 回再送 → 25 で成功、以降の page も 25 のまま
        self.assertEqual(injector.history_firsts(), [100, 100, 50, 50, 25, 25, 25])
        self.assertEqual(
            [v["historyAfter"] for v in injector.repo_calls],
            [None, None, None, None, None, "25", "50"],
        )
        commit = activity.days[D18][0]
        self.assertEqual(
            (commit.commit_count, commit.additions, commit.deletions), (60, 120, 60),
        )
        self.assertEqual(sum("縮小" in line for line in cm.output), 2)

    def test_timeout_on_a_later_page_resumes_from_that_cursor(self) -> None:
        fake, _nwo = self._fake_with_commits(130)
        injector = FaultInjector(
            fake,
            lambda v: (http_error(504) if v["historyAfter"] == "100" and v["historyFirst"] == 100
                       else None),
        )
        activity = self._collect(injector)
        self.assertTrue(activity.complete, activity.errors)
        self.assertEqual(injector.history_firsts(), [100, 100, 100, 50])
        self.assertEqual(
            [v["historyAfter"] for v in injector.repo_calls], [None, "100", "100", "100"],
        )
        self.assertEqual(activity.days[D18][0].commit_count, 130)

    def test_graphql_timeout_error_also_shrinks(self) -> None:
        fake, _nwo = self._fake_with_commits(3)
        injector = FaultInjector(
            fake, lambda v: TIMEOUT_GRAPHQL_ERROR if v["historyFirst"] == 100 else None,
        )
        activity = self._collect(injector)
        self.assertTrue(activity.complete, activity.errors)
        self.assertEqual(injector.history_firsts(), [100, 100, 50])
        self.assertEqual(activity.days[D18][0].commit_count, 3)

    def test_smallest_page_gets_full_retries_then_the_repo_fails(self) -> None:
        fake, _nwo = self._fake_with_commits(3)
        injector = FaultInjector(fake, lambda v: http_error(502))
        sleeps: "list[float]" = []
        activity = self._collect(injector, sleeps=sleeps)
        self.assertEqual(
            injector.history_firsts(), [100, 100, 50, 50, 25, 25, 10, 10, 10, 10],
        )
        self.assertEqual(len(sleeps), 1 + 1 + 1 + 3)
        self.assertFalse(activity.complete)
        self.assertEqual(activity.status, "failed")
        self.assertTrue(any("502" in e for e in activity.errors))
        self.assertIsNone(activity.text_for(D18))

    def test_non_timeout_errors_do_not_shrink(self) -> None:
        cases: "list[tuple[str, Callable[[dict], object], int]]" = [
            ("http 503", lambda v: http_error(503), 4),
            ("connection", lambda v: urllib.error.URLError("down"), 4),
            ("forbidden", lambda v: {"data": {"repository": None},
                                     "errors": [{"type": "FORBIDDEN", "message": "no"}]}, 1),
        ]
        for label, fault, attempts in cases:
            with self.subTest(label):
                fake, _nwo = self._fake_with_commits(3)
                injector = FaultInjector(fake, fault)
                activity = self._collect(injector)
                self.assertEqual(injector.history_firsts(), [100] * attempts)
                self.assertFalse(activity.complete)

    def test_requests_without_history_are_not_shrunk(self) -> None:
        fake, nwo = self._fake_with_commits(1)
        for n in range(1, 52):  # PR 2 page 目は history なしの request になる
            fake.add_pr(nwo, n, at_jst(2026, 7, 18, 11, n % 60))
        injector = FaultInjector(
            fake, lambda v: http_error(502) if not v["withHistory"] else None,
        )
        activity = self._collect(injector)
        self.assertFalse(activity.complete)
        later = injector.repo_calls[1:]
        self.assertEqual(len(later), 4)  # 通常の再試行 (初回 + 3 回)、縮小しない
        self.assertTrue(all(not v["withHistory"] and v["prsAfter"] == "50" for v in later))

    def test_page_cap_counts_history_in_default_page_units(self) -> None:
        # 50 件に縮小した後は max_pages=1 で 2 page (= 100 commit 相当) まで取れる
        fake, _nwo = self._fake_with_commits(100)
        injector = FaultInjector(
            fake, lambda v: http_error(504) if v["historyFirst"] == 100 else None,
        )
        activity = self._collect(injector, max_pages=1)
        self.assertTrue(activity.complete, activity.errors)
        self.assertEqual(injector.history_firsts(), [100, 100, 50, 50])
        self.assertEqual(activity.days[D18][0].commit_count, 100)

        fake, _nwo = self._fake_with_commits(150)
        injector = FaultInjector(
            fake, lambda v: http_error(504) if v["historyFirst"] == 100 else None,
        )
        activity = self._collect(injector, max_pages=1)
        self.assertFalse(activity.complete)
        self.assertTrue(any("pagination" in e for e in activity.errors))
        self.assertEqual(injector.history_firsts(), [100, 100, 50, 50])


class TestForkCommits(unittest.TestCase):
    """fork repo の default branch commit は viewer が author のものだけ数える (既定)。
    upstream の同期で他人の commit が元の committer date のまま過去日に載るのを防ぐ。"""

    def _fake(
        self, viewer: str = "octo-user", my_login: str = "octo-user",
    ) -> "tuple[FakeGitHub, str]":
        fake = FakeGitHub(viewer=viewer, orgs=[])
        nwo = fake.add_repo("octo-user", "forked-oss", pushed_at=at_jst(2026, 7, 18, 20),
                            fork=True)
        t = at_jst(2026, 7, 18, 10)
        fake.add_commit(nwo, "up1", t, additions=12000, deletions=800, login="upstream-dev",
                        name="Upstream Dev", email="dev@upstream.example")
        fake.add_commit(nwo, "me1", t, additions=3, deletions=1, login=my_login)
        # GitHub アカウントに紐付いていないメールの commit (author.user = null)
        fake.add_commit(nwo, "nolink", t, additions=40, deletions=4, login=None,
                        name="Octo User", email="octo@laptop.local")
        fake.add_pr(nwo, 3, at_jst(2026, 7, 18, 11))
        fake.add_issue(nwo, 2, at_jst(2026, 7, 18, 12))
        return fake, nwo

    def test_default_counts_only_viewer_authored_commits(self) -> None:
        fake, _nwo = self._fake()
        activity = collect(fake, D18, D18)
        self.assertTrue(activity.complete, activity.errors)
        self.assertEqual(
            activity.text_for(D18),
            "[🎫 forked-oss:Issue #2: synthetic issue 2]"
            "(https://github.com/octo-user/forked-oss/issues/2)\n"
            "[🔀 forked-oss:PR #3: synthetic pr 3](https://github.com/octo-user/forked-oss/pull/3)\n"
            "[📝 forked-oss:変更行数:+3-1 (1 commits)]"
            "(https://github.com/octo-user/forked-oss/commits/main)",
        )
        self.assertEqual(fake.ops("IhealthViewerLogin"), [])  # viewer は列挙時に取得済み

    def test_fork_all_authors_counts_every_author(self) -> None:
        fake, _nwo = self._fake()
        commit = collect(fake, D18, D18, fork_all_authors=True).days[D18][-1]
        self.assertEqual(
            (commit.commit_count, commit.additions, commit.deletions), (3, 12043, 805),
        )

    def test_viewer_login_match_is_case_insensitive(self) -> None:
        # viewer / commit author の login の大文字小文字が両方とも異なっても本人扱い
        for owners in (None, "octo-user"):  # 列挙時に取得 / IhealthViewerLogin で取得
            with self.subTest(owners=owners):
                fake, _nwo = self._fake(viewer="Octo-User", my_login="OCTO-USER")
                commit = collect(fake, D18, D18, owners=owners).days[D18][-1]
                self.assertEqual(
                    (commit.kind, commit.commit_count, commit.additions), ("commit", 1, 3),
                )

    def test_fork_with_only_upstream_commits_has_no_commit_line(self) -> None:
        fake = FakeGitHub(viewer="octo-user", orgs=[])
        nwo = fake.add_repo("octo-user", "synced", pushed_at=at_jst(2026, 7, 19), fork=True)
        for n in range(5):  # Sync fork で入った upstream の commit (committer date は過去日)
            fake.add_commit(nwo, f"u{n}", at_jst(2026, 7, 18, 9 + n), additions=10000,
                            login="upstream-dev")
        activity = collect(fake, D18, D19)
        self.assertTrue(activity.complete)
        self.assertEqual(activity.text_for(D18), "該当なし")

    def test_non_fork_repo_still_counts_all_authors(self) -> None:
        fake = FakeGitHub(viewer="octo-user", orgs=[])
        nwo = fake.add_repo("octo-user", "team", pushed_at=at_jst(2026, 7, 18, 20))
        fake.add_commit(nwo, "c1", at_jst(2026, 7, 18, 9), additions=5, login="colleague")
        fake.add_commit(nwo, "c2", at_jst(2026, 7, 18, 10), additions=7, login=None)
        commit = collect(fake, D18, D18).days[D18][0]
        self.assertEqual((commit.commit_count, commit.additions), (2, 12))

    def test_owners_given_resolves_viewer_login_only_when_a_fork_is_scanned(self) -> None:
        fake, _nwo = self._fake()
        fake.add_repo("octo-user", "fork-two", pushed_at=at_jst(2026, 7, 18, 21), fork=True)
        fake.add_repo("octo-user", "plain", pushed_at=at_jst(2026, 7, 18, 22))
        activity = collect(fake, D18, D18, owners="octo-user")
        self.assertTrue(activity.complete, activity.errors)
        self.assertEqual(len(fake.ops("IhealthViewerLogin")), 1)
        self.assertEqual(fake.ops("IhealthViewer"), [])
        self.assertEqual(activity.days[D18][-1].commit_count, 1)

        fake.calls.clear()
        collect(fake, D18, D18, owners="octo-user", fork_all_authors=True)
        self.assertEqual(fake.ops("IhealthViewerLogin"), [])

        no_fork = FakeGitHub(viewer="octo-user", orgs=[])
        no_fork.add_repo("octo-user", "plain", pushed_at=at_jst(2026, 7, 18, 22))
        collect(no_fork, D18, D18, owners="octo-user")
        self.assertEqual(no_fork.ops("IhealthViewerLogin"), [])

    def test_viewer_login_failure_skips_only_fork_repos(self) -> None:
        fake, _fork = self._fake()
        plain = fake.add_repo("octo-user", "plain", pushed_at=at_jst(2026, 7, 18, 22))
        fake.add_pr(plain, 9, at_jst(2026, 7, 18, 9))
        fake.viewer_login_exception = http_error(401, {"message": "Bad credentials"})
        activity = collect(fake, D18, D18, owners="octo-user")
        self.assertFalse(activity.complete)
        self.assertEqual(activity.status, "partial")
        self.assertEqual(fake.scanned(), ["octo-user/plain"])
        self.assertEqual((activity.repos_scanned, activity.repos_failed), (1, 1))
        self.assertTrue(any("fork" in e and "octo-user/forked-oss" in e for e in activity.errors))
        self.assertIsNone(activity.text_for(D18))

        only_fork, _nwo = self._fake()
        only_fork.viewer_login_exception = http_error(401, {"message": "Bad credentials"})
        self.assertEqual(collect(only_fork, D18, D18, owners="octo-user").status, "failed")


class TestStatus(unittest.TestCase):
    """``status``: complete / partial (一部でも取れた) / failed (使えるデータ無し)。"""

    def test_all_candidate_scans_failing_is_failed(self) -> None:
        fake = FakeGitHub(viewer="octo-user", orgs=[])
        for n in range(3):
            nwo = fake.add_repo("octo-user", f"r{n}", pushed_at=at_jst(2026, 7, 18, 10 + n))
            fake.repo_errors[nwo.lower()] = [{"type": "FORBIDDEN", "message": "no access"}]
        activity = collect(fake, D18, D18)
        self.assertFalse(activity.complete)
        self.assertEqual(
            (activity.repos_scanned, activity.repos_failed, activity.repos_listed), (0, 3, 3),
        )
        self.assertEqual(activity.status, "failed")

    def test_network_down_after_listing_is_failed(self) -> None:
        fake = FakeGitHub(viewer="octo-user", orgs=[])
        for n in range(2):
            fake.add_repo("octo-user", f"r{n}", pushed_at=at_jst(2026, 7, 18, 10 + n))
        injector = FaultInjector(fake, lambda v: urllib.error.URLError("network down"))
        client = make_client(injector)
        activity = collect_github_activity(
            client, D18, D18, logger=logging.getLogger("test.github_activity"),
        )
        self.assertEqual(activity.status, "failed")

    def test_some_owner_listed_without_candidates_and_another_failed_is_partial(self) -> None:
        fake = FakeGitHub(viewer="octo-user", orgs=["octo-org"])
        fake.add_repo("octo-user", "stale", pushed_at=at_jst(2026, 6, 1))
        fake.owner_errors["octo-org"] = [{"type": "FORBIDDEN", "message": "SAML enforcement"}]
        activity = collect(fake, D18, D18)
        self.assertFalse(activity.complete)
        self.assertEqual((activity.owners_listed, activity.repos_scanned), (1, 0))
        self.assertEqual(activity.status, "partial")

    def test_every_owner_listing_failing_is_failed(self) -> None:
        fake = FakeGitHub(viewer="octo-user", orgs=["octo-org"])
        for owner in ("octo-user", "octo-org"):
            fake.owner_errors[owner] = [{"type": "FORBIDDEN", "message": "no"}]
        activity = collect(fake, D18, D18)
        self.assertEqual(activity.owners_listed, 0)
        self.assertEqual(activity.status, "failed")

    def test_status_of_bare_constructed_activity(self) -> None:
        self.assertEqual(GitHubActivity(days={}, complete=True).status, "complete")
        self.assertEqual(GitHubActivity(days={}, complete=False).status, "failed")


if __name__ == "__main__":
    unittest.main()
