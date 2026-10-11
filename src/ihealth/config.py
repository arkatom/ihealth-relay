"""`.env` の読み込みと実行時設定の保持。

外部依存を避けるため `python-dotenv` は使わず、必要最低限のパーサを自前で実装する。

対応記法:
- `KEY=value`
- `KEY="value with spaces"` / `KEY='value'` (両端のクオートは剥がす)
- `#` で始まる行はコメント
- 空行はスキップ
- `~/` で始まるパスはホームディレクトリに展開 (中間の `~` は文字どおり)

Obsidian 日記 (``--publisher obsidian``) 用のキーは **すべて optional** で、未指定でも
既存の Notion 運用は無修正で動く (SPEC §2-E / v2 §4.6):

- ``IHEALTH_PUBLISHER``: ``--publisher`` 未指定時の既定 publisher (未指定なら notion)
- ``OBSIDIAN_VAULT_DIR`` (obsidian で必須) / ``OBSIDIAN_DIARY_DIR`` (既定 ``日記``) /
  ``OBSIDIAN_TEMPLATE`` (vault 相対 or 絶対。設定するとノート不在時にテンプレートから作成) /
  ``OBSIDIAN_CREATE_MISSING`` (``never`` / ``template`` / ``minimal``。既定はテンプレート
  設定があれば ``template``、無ければ ``never``) / ``OBSIDIAN_CREATE_MIN_AGE_DAYS`` (既定 2)
- ``IHEALTH_LOOKBACK_DAYS`` (既定 7) / ``IHEALTH_INCLUDE_TODAY`` (既定 true) /
  ``IHEALTH_SOURCES`` (既定 ``health,weather,github``) / ``IHEALTH_TZ_OFFSET`` (既定 ``+09:00``) /
  ``IHEALTH_NETWORK_GRACE_SEC`` (既定 60。起動直後のネットワーク未接続を待つ秒数, 0 で無効)
- ``JMA_PREC_NO`` / ``JMA_BLOCK_NO`` (既定 44 / 47662 = 東京)
- ``GITHUB_TOKEN`` (任意。無ければ ``gh auth token``) / ``GITHUB_OWNERS`` /
  ``GITHUB_EXCLUDE_REPOS`` (任意, カンマ区切り。書式検証は収集時に行う)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta, timezone, tzinfo
from pathlib import Path

from ihealth.logger import ALLOWED_LEVELS as _ALLOWED_LEVELS


class ConfigError(RuntimeError):
    """`.env` の読み込み・検証で発生する例外。"""


#: ``--publisher`` / ``IHEALTH_PUBLISHER`` で受け付ける publisher 種別 (唯一の定義元。
#: :data:`ihealth.workflow.PUBLISHER_KINDS` はこれの re-export)。
#:
#: - ``notion``: Notion API へ書き込み (既定 / 既存運用)
#: - ``markdown``: Obsidian 互換 Markdown を ``MARKDOWN_OUTPUT_DIR`` に出力
#: - ``stdout``: JSON 1 行を sys.stdout に書き出す (Unix pipe で composable)
#: - ``sqlite``: ローカル SQLite DB に 1 日 1 行 UPSERT
#: - ``slack``: Incoming Webhook で日次サマリを Slack に投稿
#: - ``dryrun``: Notion publisher の組み立て結果をログ出力するだけ (HTTP 不発射)
#: - ``obsidian``: Obsidian 日記ノートの frontmatter に健康 + 天気 + GitHub 活動を
#:   書き込む (複数日・複数 source の diary パイプライン, :mod:`ihealth.diary`)
PUBLISHER_KINDS: "tuple[str, ...]" = (
    "notion", "markdown", "stdout", "sqlite", "slack", "dryrun", "obsidian",
)

#: diary パイプラインの source 名 (処理・表示の正規順)。
SOURCE_NAMES: "tuple[str, ...]" = ("health", "weather", "github")

#: ノート不在時の作成モード (:mod:`ihealth.publishers.obsidian` の ``CREATE_*`` と同じ値)。
CREATE_MISSING_MODES: "tuple[str, ...]" = ("never", "template", "minimal")

#: 日本標準時 (``IHEALTH_TZ_OFFSET`` の既定)。
JST = timezone(timedelta(hours=9))

_DEFAULT_LOOKBACK_DAYS = 7
_MAX_LOOKBACK_DAYS = 366
_DEFAULT_NETWORK_GRACE_SEC = 60.0
_MAX_NETWORK_GRACE_SEC = 600.0
_DEFAULT_CREATE_MIN_AGE_DAYS = 2
_MAX_CREATE_MIN_AGE_DAYS = 366
_DEFAULT_JMA_PREC_NO = 44
_DEFAULT_JMA_BLOCK_NO = 47662
_DEFAULT_DIARY_DIR = "日記"

_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})
_TZ_OFFSET_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})$")


@dataclass(frozen=True)
class Config:
    """実行時設定。必須項目は `load_config()` で検証済み。

    Phase 1 A3-md で ``markdown_output_dir`` を optional で追加。``--publisher
    markdown`` を指定したときに使う書き出し先で、未指定なら ``None``。Notion
    publisher を使う既定運用には影響しない (= 既存 .env を編集せずに動く)。

    Obsidian 日記用の項目 (``default_publisher`` 以降) はすべて既定値を持つ
    (既存の ``Config(...)`` 呼び出しはそのまま動く)。秘密値 (``notion_secret`` /
    ``slack_webhook_url`` / ``github_token``) は ``repr`` に出さない。
    """

    notion_secret: "str | None" = field(repr=False)
    database_id: "str | None"
    health_export_dir: Path
    archive_root: Path
    birth_date: date
    log_level: str = "info"
    markdown_output_dir: "Path | None" = None
    sqlite_db_path: "Path | None" = None
    slack_webhook_url: "str | None" = field(default=None, repr=False)
    # ---- Obsidian 日記 (SPEC §2-E / v2 §4.6) ----
    #: ``IHEALTH_PUBLISHER`` (``--publisher`` 未指定時の既定)。None なら notion。
    default_publisher: "str | None" = None
    obsidian_vault_dir: "Path | None" = None
    obsidian_diary_dir: str = _DEFAULT_DIARY_DIR
    #: vault 相対 (相対パス) または絶対パス。
    obsidian_template: "Path | None" = None
    #: ``never`` / ``template`` / ``minimal``。None なら :meth:`effective_create_missing` の規則。
    obsidian_create_missing: "str | None" = None
    obsidian_create_min_age_days: int = _DEFAULT_CREATE_MIN_AGE_DAYS
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS
    include_today: bool = True
    sources: "tuple[str, ...]" = SOURCE_NAMES
    jma_prec_no: int = _DEFAULT_JMA_PREC_NO
    jma_block_no: int = _DEFAULT_JMA_BLOCK_NO
    github_token: "str | None" = field(default=None, repr=False)
    #: カンマ区切りの生文字列 (書式検証は ``collect_github_activity`` が行う)。
    github_owners: "str | None" = None
    github_exclude_repos: "str | None" = None
    #: 日記の基準 tz (GitHub の日付 bucket と「今日 / 昨日」の判定)。
    diary_tz: tzinfo = JST
    #: 起動直後のネットワーク未接続を待つ猶予 (秒)。0 で待たない。
    network_grace_sec: float = _DEFAULT_NETWORK_GRACE_SEC
    #: 実行レポート / 通知状態ファイルの置き場 (``<project_root>/logs``)。None なら書かない。
    logs_dir: "Path | None" = None

    def effective_create_missing(self) -> str:
        """ノート不在時の作成モード。明示設定が無ければテンプレート設定の有無で決める。"""
        if self.obsidian_create_missing is not None:
            return self.obsidian_create_missing
        return "template" if self.obsidian_template is not None else "never"

    def obsidian_backup_dir(self) -> Path:
        """書込前バックアップの保存先 (``<archive_root の親>/obsidian-backup``, SPEC v2 §4.5)。"""
        return self.archive_root.parent / "obsidian-backup"

    def diary_state_path(self) -> Path:
        """日記の暫定値の台帳 (``<archive_root の親>/diary-state.json`` = ``data/``, gitignore 済)。

        今日の暫定値を書いた日と、その値を記録する (:mod:`ihealth.diary`)。確定値に
        置き換わるまで、既定 window の外に出ても処理し直すために使う。
        """
        return self.archive_root.parent / "diary-state.json"

    def missing_notion_keys(self) -> "list[str]":
        """Notion publisher に必要で未設定の ``.env`` キー名 (無ければ空)。"""
        return [
            name for name, value in (
                ("NOTION_SECRET", self.notion_secret),
                ("DATABASE_ID", self.database_id),
            ) if not value
        ]

    def age_years(self, as_of: "date | None" = None) -> int:
        """``as_of`` 時点の満年齢 (整数)。``None`` なら今日基準。

        2/29 生まれの扱い: 平年は 3/1 に歳を取る (``3/1 >= 2/29`` と比較されるため)。
        2/28 を記念日にしたいユーザーは仕様変更が必要。
        """
        today = as_of or date.today()
        years = today.year - self.birth_date.year
        if (today.month, today.day) < (self.birth_date.month, self.birth_date.day):
            years -= 1
        return years

    def max_heart_rate(self, as_of: "date | None" = None) -> float:
        """最大心拍数 (Tanaka 式: ``205.8 − 0.685 × age``)。

        Google Fit の Heart Points 算出で使われている公式式に揃える
        (https://support.google.com/fit/answer/7619539)。
        旧来の ``220 − age`` より若年側で低め、壮年側でも精度が高い。
        """
        age = self.age_years(as_of=as_of)
        return 205.8 - (0.685 * age)


def load_config(
    env_path: Path | None = None, *, require_notion: bool = True,
) -> Config:
    """プロジェクトルートの `.env` を読み込んで `Config` を返す。

    Args:
        env_path: 明示的な `.env` パス。`None` の場合はプロジェクトルートを使用。
        require_notion: ``NOTION_SECRET`` / ``DATABASE_ID`` を必須として扱うか.
            既定 ``True`` で **既存の launchd Notion 運用を壊さない**.
            ``--publisher markdown`` (Notion 不使用) のときは ``False`` を渡し、
            両キーが欠落していても ``Config.notion_secret`` / ``database_id``
            が ``None`` の状態で組み立てる.

    Raises:
        ConfigError: `.env` が存在しない、または必須キーが欠落している場合。
            ``require_notion=False`` でも ``HEALTH_EXPORT_DIR`` / ``BIRTH_DATE``
            は常に必須.
    """
    resolved_path = env_path if env_path is not None else _project_root() / ".env"

    if not resolved_path.exists():
        raise ConfigError(f".env が見つかりません: {resolved_path}")

    values = _parse_env_file(resolved_path)

    required = ["HEALTH_EXPORT_DIR", "BIRTH_DATE"]
    if require_notion:
        required = ["NOTION_SECRET", "DATABASE_ID", *required]
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise ConfigError(
            f".env に必須キーが不足しています: {', '.join(missing)}"
        )

    try:
        birth_date = date.fromisoformat(values["BIRTH_DATE"])
    except ValueError as exc:
        # 不正値は個人情報 (実誕生日の可能性) なので、値そのものはログに出さない。
        raise ConfigError(
            "BIRTH_DATE が ISO8601 (YYYY-MM-DD) 形式ではありません"
        ) from exc

    health_export_dir = _parse_health_export_dir(values["HEALTH_EXPORT_DIR"])

    # Issue #17: LOG_LEVEL の許可値を明示検証 (typo で静かに INFO に落ちるのを防ぐ)
    log_level = values.get("LOG_LEVEL", "info").strip().lower()
    if log_level not in _ALLOWED_LEVELS:
        raise ConfigError(
            f"LOG_LEVEL が不正です (許可値: {sorted(_ALLOWED_LEVELS)}): "
            f"{log_level!r}"
        )

    # Phase 1 A3-md: optional の Markdown 出力先。未指定なら None で持たせる
    # (Notion publisher を使う既定運用は無修正で動く).
    markdown_output_dir_raw = values.get("MARKDOWN_OUTPUT_DIR", "").strip()
    markdown_output_dir: "Path | None"
    if markdown_output_dir_raw:
        markdown_output_dir = _parse_optional_dir_path(
            markdown_output_dir_raw, key_name="MARKDOWN_OUTPUT_DIR",
        )
    else:
        markdown_output_dir = None

    # Phase 1 A3-sqlite: optional の SQLite DB ファイルパス. 未指定なら None.
    # ディレクトリではなく **ファイル** パスを受ける (例: ~/health.db).
    sqlite_db_path_raw = values.get("SQLITE_DB_PATH", "").strip()
    sqlite_db_path: "Path | None"
    if sqlite_db_path_raw:
        sqlite_db_path = _parse_optional_file_path(
            sqlite_db_path_raw, key_name="SQLITE_DB_PATH",
        )
    else:
        sqlite_db_path = None

    # Phase 1 A3-slack: optional の Slack Incoming Webhook URL.
    # secret token を URL に含むので Path 型ではなく str のまま保持する.
    slack_webhook_raw = values.get("SLACK_WEBHOOK_URL", "").strip()
    slack_webhook_url = _validate_slack_webhook_url(
        slack_webhook_raw, key_name="SLACK_WEBHOOK_URL",
    ) if slack_webhook_raw else None

    # NOTION_SECRET / DATABASE_ID は require_notion=False のとき値が無くても
    # OK. その場合は ``None`` を保持し、Notion 経路を試みた時点で
    # build_default_deps が ConfigAppError を raise する設計.
    notion_secret = values.get("NOTION_SECRET") or None
    database_id = values.get("DATABASE_ID") or None

    project_root = _project_root()
    return Config(
        notion_secret=notion_secret,
        database_id=database_id,
        health_export_dir=health_export_dir,
        archive_root=project_root / "data" / "health",
        birth_date=birth_date,
        log_level=log_level,
        markdown_output_dir=markdown_output_dir,
        sqlite_db_path=sqlite_db_path,
        slack_webhook_url=slack_webhook_url,
        logs_dir=default_logs_dir(),
        **_parse_diary_settings(values),
    )


def default_logs_dir() -> Path:
    """実行レポート・通知状態ファイルの既定の置き場 (``<project_root>/logs``)。

    ``.env`` を読めなかったとき (= :class:`Config` が無いとき) の通知の重複抑止にも使う。
    """
    return _project_root() / "logs"


def _parse_diary_settings(values: "dict[str, str]") -> "dict[str, object]":
    """Obsidian 日記用の optional キーを検証して :class:`Config` の引数 dict にする。

    どのキーも未指定 / 空なら既定値 (= 既存運用に影響しない)。値の **書式** だけを
    検証する (vault の存在などは obsidian publisher を使うときに確かめる)。
    """
    def raw(key: str) -> str:
        return values.get(key, "").strip()

    settings: "dict[str, object]" = {}

    publisher = raw("IHEALTH_PUBLISHER").lower()
    if publisher:
        if publisher not in PUBLISHER_KINDS:
            raise ConfigError(
                f"IHEALTH_PUBLISHER が不正です (許可値: {', '.join(PUBLISHER_KINDS)}): "
                f"{publisher!r}"
            )
        settings["default_publisher"] = publisher

    vault = raw("OBSIDIAN_VAULT_DIR")
    if vault:
        settings["obsidian_vault_dir"] = _parse_optional_dir_path(
            vault, key_name="OBSIDIAN_VAULT_DIR",
        )
    diary_dir = raw("OBSIDIAN_DIARY_DIR")
    if diary_dir:
        settings["obsidian_diary_dir"] = _parse_vault_relative_dir(
            diary_dir, key_name="OBSIDIAN_DIARY_DIR",
        )
    template = raw("OBSIDIAN_TEMPLATE")
    if template:
        settings["obsidian_template"] = _parse_template_path(template)
    create_missing = raw("OBSIDIAN_CREATE_MISSING").lower()
    if create_missing:
        if create_missing not in CREATE_MISSING_MODES:
            raise ConfigError(
                f"OBSIDIAN_CREATE_MISSING が不正です (許可値: {', '.join(CREATE_MISSING_MODES)}): "
                f"{create_missing!r}"
            )
        if create_missing == "template" and not template:
            raise ConfigError(
                "OBSIDIAN_CREATE_MISSING=template には OBSIDIAN_TEMPLATE の設定が必要です"
            )
        settings["obsidian_create_missing"] = create_missing
    min_age = raw("OBSIDIAN_CREATE_MIN_AGE_DAYS")
    if min_age:
        settings["obsidian_create_min_age_days"] = _parse_int_in_range(
            min_age, key_name="OBSIDIAN_CREATE_MIN_AGE_DAYS",
            lo=0, hi=_MAX_CREATE_MIN_AGE_DAYS,
        )

    lookback = _DEFAULT_LOOKBACK_DAYS
    if raw("IHEALTH_LOOKBACK_DAYS"):
        lookback = _parse_int_in_range(
            raw("IHEALTH_LOOKBACK_DAYS"), key_name="IHEALTH_LOOKBACK_DAYS",
            lo=0, hi=_MAX_LOOKBACK_DAYS,
        )
        settings["lookback_days"] = lookback
    include_today = True
    if raw("IHEALTH_INCLUDE_TODAY"):
        include_today = _parse_bool(raw("IHEALTH_INCLUDE_TODAY"), key_name="IHEALTH_INCLUDE_TODAY")
        settings["include_today"] = include_today
    if lookback == 0 and not include_today:
        raise ConfigError(
            "IHEALTH_LOOKBACK_DAYS=0 かつ IHEALTH_INCLUDE_TODAY=false では対象日がありません"
        )
    if raw("IHEALTH_SOURCES"):
        try:
            settings["sources"] = parse_sources(raw("IHEALTH_SOURCES"))
        except ValueError as exc:
            raise ConfigError(f"IHEALTH_SOURCES: {exc}") from None
    if raw("IHEALTH_TZ_OFFSET"):
        settings["diary_tz"] = _parse_tz_offset(raw("IHEALTH_TZ_OFFSET"))
    if raw("IHEALTH_NETWORK_GRACE_SEC"):
        settings["network_grace_sec"] = float(_parse_int_in_range(
            raw("IHEALTH_NETWORK_GRACE_SEC"), key_name="IHEALTH_NETWORK_GRACE_SEC",
            lo=0, hi=int(_MAX_NETWORK_GRACE_SEC),
        ))

    if raw("JMA_PREC_NO"):
        settings["jma_prec_no"] = _parse_int_in_range(
            raw("JMA_PREC_NO"), key_name="JMA_PREC_NO", lo=1, hi=None,
        )
    if raw("JMA_BLOCK_NO"):
        settings["jma_block_no"] = _parse_int_in_range(
            raw("JMA_BLOCK_NO"), key_name="JMA_BLOCK_NO", lo=1, hi=None,
        )

    # token は値の検証も含めて resolve_github_token に任せる (ここで値を一切出さない)
    if raw("GITHUB_TOKEN"):
        settings["github_token"] = raw("GITHUB_TOKEN")
    if raw("GITHUB_OWNERS"):
        settings["github_owners"] = raw("GITHUB_OWNERS")
    if raw("GITHUB_EXCLUDE_REPOS"):
        settings["github_exclude_repos"] = raw("GITHUB_EXCLUDE_REPOS")
    return settings


def parse_sources(raw: str) -> "tuple[str, ...]":
    """``health,weather,github`` 形式の source 指定を正規順のタプルにする。

    大文字小文字・前後空白は無視し、重複は 1 つにまとめる。

    Raises:
        ValueError: 未知の source 名 / 空指定。
    """
    names = [part.strip().lower() for part in raw.split(",")]
    names = [name for name in names if name]
    if not names:
        raise ValueError(
            f"source が指定されていません (指定可能: {', '.join(SOURCE_NAMES)})"
        )
    unknown = sorted({name for name in names if name not in SOURCE_NAMES})
    if unknown:
        raise ValueError(
            f"未知の source: {', '.join(unknown)} (指定可能: {', '.join(SOURCE_NAMES)})"
        )
    return tuple(name for name in SOURCE_NAMES if name in names)


def _parse_int_in_range(
    raw: str, *, key_name: str, lo: "int | None", hi: "int | None",
) -> int:
    try:
        value = int(raw, 10)
    except ValueError:
        raise ConfigError(f"{key_name} は整数で指定してください: {raw!r}") from None
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        if hi is None:
            expected = f"{lo} 以上"
        elif lo is None:
            expected = f"{hi} 以下"
        else:
            expected = f"{lo}〜{hi} の範囲"
        raise ConfigError(f"{key_name} は {expected}の整数で指定してください: {value}")
    return value


def _parse_bool(raw: str, *, key_name: str) -> bool:
    word = raw.strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise ConfigError(f"{key_name} は true / false で指定してください: {raw!r}")


def _parse_tz_offset(raw: str) -> tzinfo:
    """``+09:00`` / ``-0530`` / ``Z`` 形式の UTC オフセットを ``timezone`` にする。"""
    text = raw.strip()
    if text.upper() in ("Z", "UTC"):
        return timezone.utc
    match = _TZ_OFFSET_RE.match(text)
    if match is None:
        raise ConfigError(
            f"IHEALTH_TZ_OFFSET は +HH:MM 形式で指定してください (例: +09:00): {raw!r}"
        )
    sign, hours, minutes = match.groups()
    if int(hours) > 23 or int(minutes) > 59:
        raise ConfigError(f"IHEALTH_TZ_OFFSET の値が範囲外です: {raw!r}")
    offset = timedelta(hours=int(hours), minutes=int(minutes))
    return timezone(-offset if sign == "-" else offset)


def _parse_vault_relative_dir(raw: str, *, key_name: str) -> str:
    """vault 内の相対ディレクトリ名 (``日記`` / ``Journal/Daily`` 等) を検証する。

    絶対パスや ``..`` を許すと vault の外を書き換え対象にできてしまうので拒否する。
    """
    text = raw.strip().strip("/")
    if not text or raw.strip().startswith(("/", "~")):
        raise ConfigError(f"{key_name} は vault 内の相対パスで指定してください: {raw!r}")
    parts = Path(text).parts
    if any(part in ("..", ".") for part in parts):
        raise ConfigError(f"{key_name} に '.' / '..' は使えません: {raw!r}")
    return text


def _parse_template_path(raw: str) -> Path:
    """``OBSIDIAN_TEMPLATE``: ``~/`` 始まり・絶対パス・vault 相対パスのいずれか。"""
    if raw.startswith("~/"):
        return Path.home() / raw[2:]
    if raw.startswith("~"):
        raise ConfigError(
            "OBSIDIAN_TEMPLATE は ~/...・絶対パス・vault 相対パスのいずれかで指定してください "
            "(~user/... は未対応)"
        )
    return Path(raw)


def ensure_notion_settings(config: Config) -> None:
    """Notion publisher に必要な ``NOTION_SECRET`` / ``DATABASE_ID`` を検証する。

    ``load_config(require_notion=True)`` と同じ文言の :class:`ConfigError` を投げる
    (``IHEALTH_PUBLISHER`` を読むために ``require_notion=False`` で読み込んだ後、
    実際に notion を使うと決まった時点で呼ぶ)。
    """
    missing = config.missing_notion_keys()
    if missing:
        raise ConfigError(
            f".env に必須キーが不足しています: {', '.join(missing)}"
        )


def _validate_slack_webhook_url(raw: str, *, key_name: str) -> str:
    """Slack Incoming Webhook URL の構造を検証する.

    実体は :func:`ihealth.publishers.slack.validate_webhook_url` に委譲.
    検証内容:
    - ``https://`` 必須 (HTTP / file:// は reject)
    - host は ``hooks.slack.com`` / ``hooks.slack-gov.com`` の allow-list
    - userinfo (``https://user:pass@host/...``) 禁止
    - path は ``/services/`` で始まる必要
    - query / fragment 禁止

    "実際に届くか" は検証しない (= 起動時の通信を発生させない).
    Slack Webhook の secret token は URL に含まれるので、エラーメッセージに
    URL 全体は出さない (validator が source_label しか露出しない).
    """
    from ihealth.publishers.slack import validate_webhook_url
    try:
        return validate_webhook_url(raw, source_label=key_name)
    except ValueError as exc:
        raise ConfigError(str(exc)) from None


def _parse_optional_dir_path(raw: str, *, key_name: str) -> Path:
    """``HEALTH_EXPORT_DIR`` と同じ規則で ``~/`` のみ展開し絶対パス検証する.

    既存パスが **ファイル** (= ディレクトリではない) だった場合は
    :class:`ConfigError` で fail-fast. ``_parse_optional_file_path`` の対称契約として、CLI / .env
    両側で「ディレクトリ限定」を揃える. 存在しないパス (= 初回起動で publisher
    が mkdir で作るケース) は許可.
    """
    path = _parse_optional_path(raw, key_name=key_name)
    if path.exists() and not path.is_dir():
        raise ConfigError(
            f"{key_name} はディレクトリの絶対パスを指定してください "
            f"(現在ファイル): {path}"
        )
    return path


def _parse_optional_file_path(raw: str, *, key_name: str) -> Path:
    """ファイルパス受け取り用 (= 既存ディレクトリは拒否).

    ``SQLITE_DB_PATH`` のように「ファイル単体を絶対パスで指定」させたいキーで使う.
    既存パスがディレクトリだった場合 (例: ``SQLITE_DB_PATH=/tmp/`` のような
    typo) は :class:`ConfigError` で fail-fast. 既存ディレクトリを通すと、後段の SQLite publisher が
    ``connect()`` で OSError になり ConfigAppError(3) ではなく
    SQLiteAppError(9) に遅延してしまう.

    存在しないパス (= 初回起動で DB を新規作成するケース) は許可する.
    """
    path = _parse_optional_path(raw, key_name=key_name)
    if path.exists() and path.is_dir():
        raise ConfigError(
            f"{key_name} はファイルの絶対パスを指定してください "
            f"(現在ディレクトリ): {path}"
        )
    return path


def _parse_optional_path(raw: str, *, key_name: str) -> Path:
    if raw.startswith("~/"):
        path = Path.home() / raw[2:]
    elif raw.startswith("~"):
        raise ConfigError(
            f"{key_name} は ~/... または絶対パスで指定してください "
            "(~user/... のような user 指定は未対応)"
        )
    else:
        path = Path(raw)

    if not path.is_absolute():
        raise ConfigError(f"{key_name} は絶対パスで指定してください")
    return path


def _parse_health_export_dir(raw: str) -> Path:
    """``HEALTH_EXPORT_DIR`` を安全に Path に変換する。

    契約:
    - 先頭 ``~/`` のみ ホームディレクトリ展開 (``Path.expanduser`` の広範な挙動を避け、
      ``~root/...`` や ``~unknownuser/...`` を誤って解釈しない)
    - それ以外は絶対パス必須。相対パスはエラー

    中間の ``~`` (iCloud Drive の ``iCloud~com~ifunography~HealthExport`` など) は
    フォルダ名の一部として文字通り扱う。
    """
    if raw.startswith("~/"):
        path = Path.home() / raw[2:]
    elif raw.startswith("~"):
        raise ConfigError(
            "HEALTH_EXPORT_DIR は ~/... または絶対パスで指定してください "
            "(~user/... のような user 指定は未対応)"
        )
    else:
        path = Path(raw)

    if not path.is_absolute():
        raise ConfigError("HEALTH_EXPORT_DIR は絶対パスで指定してください")
    return path


def _parse_env_file(path: Path) -> "dict[str, str]":
    """`.env` ファイル全体をパースして dict を返す。

    ``utf-8-sig`` で読み込むことで BOM 付き UTF-8 (Windows 系 GUI エディタや
    いくつかのツールが付与する) でもキー名が ``\\ufeffNOTION_SECRET`` のように
    壊れないようにする。
    """
    values: "dict[str, str]" = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        parsed = _parse_env_line(raw_line)
        if parsed is None:
            continue
        key, value = parsed
        values[key] = value
    return values


def _parse_env_line(line: str) -> tuple[str, str] | None:
    """1行を `(key, value)` に変換。コメント・空行・不正形式は None。"""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if "=" not in stripped:
        return None

    key, _, raw_value = stripped.partition("=")
    key = key.strip()
    value = raw_value.strip()

    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]

    return key, value


def _project_root() -> Path:
    """プロジェクトルートを算出する (`src/ihealth/config.py` から 2 階層上)。"""
    return Path(__file__).resolve().parent.parent.parent
