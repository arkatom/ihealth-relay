"""``config.toml`` 読み込み (Phase 1 B1 / Issue #18).

OSS 公開向けのユーザー固有設定を ``.env`` (秘密情報) と分離し、構造化された
形式で外部化する。`.env` には ``NOTION_SECRET`` ``DATABASE_ID`` などの
**秘密値だけ** を残し、Notion DB のプロパティ名マッピングなどの **構造的設定**
は本ファイルから読み出す ``config.toml`` に書く。

設計方針 (Phase 1 B1 のスコープ):
    - **後方互換最優先**: ``config.toml`` 不在時は内部 DEFAULT_PROPERTY_MAP
      (``publishers/notion.py``) がそのまま使われる = 既存 launchd 運用は無修正で継続
    - **段階的移行**: B1 では Notion ``property`` 名 (= Notion DB 列名) の
      override のみ対応。converter (単位変換) ロジックの差し替え、複数 Sink の
      enabled/disabled 切り替えは Phase 2 以降
    - **TOML 最小 subset**: 自前 :mod:`ihealth._toml` で読み込む (外部依存ゼロ)

設定ファイル形式 (例)::

    # config.toml (リポジトリルートに配置、.gitignore 対象)

    [publishers.notion.fields.step_count]
    property = "歩数 (歩)"

    [publishers.notion.fields.distance_km]
    property = "移動距離 (km)"

    # ... (他 11 フィールド)

参照優先順位:
    1. ``config.toml`` の ``[publishers.notion.fields.<field>]`` の ``property``
    2. (未指定なら) :data:`ihealth.publishers.notion.PROPERTY_MAP` の既定値

Obsidian 日記 (``--publisher obsidian``) も同じ構造で frontmatter キー名を上書きできる::

    [publishers.obsidian]
    date_property = "日付"          # 最小ノート生成時の日付キー (既定 "日付")

    [publishers.obsidian.fields.step_count]
    property = "Steps"              # 既定は "歩数 (歩)"

    [publishers.obsidian.fields.github]
    property = "GitHub"             # 既定は "Github"

内部名は :data:`ihealth.publishers.obsidian.DEFAULT_PROPERTY_MAP` のキー。統合後に
同じキーへ 2 つの内部名が向くと :func:`check_property_collisions` が
:class:`ConfigFileError` を投げる (統合側で ``ConfigAppError`` = exit 3)。

旧 ``歩数 (歩)`` 等の日本語名は B1 段階では DEFAULT_PROPERTY_MAP に残す。
B2 (= Phase 1 後半 PR) で英語キーに切り替え予定。それまで既存運用は無修正。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

from ihealth import _toml


_logger = logging.getLogger(__name__)


def find_config_path(project_root: Path) -> "Path | None":
    """プロジェクトルートに ``config.toml`` が存在すれば返す。なければ ``None``.

    探索場所:
        1. ``$IHEALTH_CONFIG_PATH`` (環境変数で明示指定された場合) — 後の Phase
        2. ``<project_root>/config.toml``

    現時点では (2) のみ。環境変数経路は Phase 2 で導入。
    """
    candidate = project_root / "config.toml"
    return candidate if candidate.is_file() else None


class ConfigFileError(ValueError):
    """``config.toml`` の構造的エラー (unknown field / non-string property など)。

    TOML 構文エラー (:class:`TomlParseError`) より上位レイヤの「論理的に正しくない」
    エラーを表す。上位の ``__main__`` で ConfigAppError に変換される。
    """


def load_config_toml(path: "Path | None") -> "dict[str, Any]":
    """``path`` の TOML を読み込んで dict を返す。``path=None`` なら空 dict。

    Args:
        path: ``find_config_path()`` の戻り値、または明示パス。

    Raises:
        TomlParseError: 構文エラー (上位の ``__main__`` で ConfigAppError 化)。
        ConfigFileError: UTF-8 として読めない (Shift_JIS で保存した等。上位で同様に変換)。
        OSError: ファイルアクセス失敗 (上位で同様に変換)。

    Returns:
        パース済みの dict。``path is None`` のときは空 dict (= override 無し)。
    """
    if path is None:
        _logger.debug("config.toml が見つかりません (= 既定 PROPERTY_MAP を使用)")
        return {}
    # BOM 付き UTF-8 (Windows 系エディタが付与しがち) も読めるよう ``utf-8-sig``
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        # UnicodeDecodeError は ValueError 系 (OSError ではない)。設定ファイルの不備として
        # 呼び出し側の ConfigAppError (exit 3) 経路に乗せる (想定外エラー exit 1 にしない)
        raise ConfigFileError(
            f"config.toml を UTF-8 として読めません (Shift_JIS 等で保存されていないか確認してください): "
            f"{path} ({exc.reason}, byte {exc.start})"
        ) from exc
    data = _toml.parse(text)
    _logger.info("config.toml をロードしました: %s", path)
    return data


def extract_property_overrides(
    config_data: "dict[str, Any]",
    publisher: str,
    *,
    valid_field_names: "frozenset[str] | None" = None,
) -> "dict[str, str]":
    """``config.toml`` から ``[publishers.<publisher>.fields.<field>].property`` を抽出する。

    返り値は ``{field_name: property_name}`` の dict。例えば::

        config.toml:
            [publishers.obsidian.fields.step_count]
            property = "Steps"

        → {"step_count": "Steps"}

    Args:
        config_data: :func:`load_config_toml` の戻り値
        publisher: ``"notion"`` / ``"obsidian"`` (``[publishers.<publisher>]`` の名前)
        valid_field_names: 有効なフィールド (内部名) の集合.
            指定すると、ここに含まれない field 名 (typo, unknown) があれば
            :class:`ConfigFileError` を raise する (strict mode). 省略時は
            unknown を silent に無視する (= 後方互換のフォールバック).

    Raises:
        ConfigFileError: ``[publishers.<publisher>.fields]`` 配下の構造が不正な場合 —
            unknown field 名, ``property`` が文字列でない, ``property`` が空文字列,
            entry が table でない, など. ``valid_field_names`` を渡したときのみ
            strict 検証する.

    Returns:
        フィールドごとの override map. 設定されていないフィールドは含まない
        (= 各 publisher の既定 property 名が使われる).
    """
    strict = valid_field_names is not None
    publishers = config_data.get("publishers")
    if publishers is None:
        return {}
    if not isinstance(publishers, dict):
        if strict:
            raise ConfigFileError(
                f"config.toml の 'publishers' は table である必要があります "
                f"(got {type(publishers).__name__})"
            )
        return {}
    section = publishers.get(publisher)
    if section is None:
        return {}
    if not isinstance(section, dict):
        if strict:
            raise ConfigFileError(
                f"config.toml の 'publishers.{publisher}' は table である必要があります "
                f"(got {type(section).__name__})"
            )
        return {}
    fields = section.get("fields")
    if fields is None:
        return {}
    if not isinstance(fields, dict):
        raise ConfigFileError(
            f"config.toml の 'publishers.{publisher}.fields' は table である必要があります "
            f"(got {type(fields).__name__})"
        )

    overrides: "dict[str, str]" = {}
    for field_name, entry in fields.items():
        if valid_field_names is not None and field_name not in valid_field_names:
            raise ConfigFileError(
                f"config.toml: 未知の field 名 {field_name!r} "
                f"(有効: {sorted(valid_field_names)})"
            )
        if not isinstance(entry, dict):
            raise ConfigFileError(
                f"config.toml: [publishers.{publisher}.fields.{field_name}] は "
                f"table である必要があります (got {type(entry).__name__})"
            )
        prop = entry.get("property")
        if prop is None:
            # property キーが無いだけなら silent skip (= 既定値が使われる)
            continue
        if not isinstance(prop, str):
            raise ConfigFileError(
                f"config.toml: [publishers.{publisher}.fields.{field_name}].property "
                f"は文字列である必要があります (got {type(prop).__name__})"
            )
        if not prop:
            raise ConfigFileError(
                f"config.toml: [publishers.{publisher}.fields.{field_name}].property "
                "が空文字列です"
            )
        overrides[field_name] = prop
    return overrides


def extract_notion_property_overrides(
    config_data: "dict[str, Any]",
    *,
    valid_field_names: "frozenset[str] | None" = None,
) -> "dict[str, str]":
    """``[publishers.notion.fields.<field>].property`` を抽出する (:func:`extract_property_overrides`)。

    例::

        config.toml:
            [publishers.notion.fields.step_count]
            property = "歩数 (歩)"

        → {"step_count": "歩数 (歩)"}
    """
    return extract_property_overrides(
        config_data, "notion", valid_field_names=valid_field_names,
    )


def extract_obsidian_property_overrides(
    config_data: "dict[str, Any]",
    *,
    valid_field_names: "frozenset[str] | None" = None,
) -> "dict[str, str]":
    """``[publishers.obsidian.fields.<内部名>].property`` を抽出する (SPEC §2-E)。

    内部名は :data:`ihealth.publishers.obsidian.DEFAULT_PROPERTY_MAP` のキー
    (健康 15 + 天気 6 + ``activity_detail`` + ``github``)。値は日記 frontmatter のキー名。
    """
    return extract_property_overrides(
        config_data, "obsidian", valid_field_names=valid_field_names,
    )


def extract_date_property(
    config_data: "dict[str, Any]",
    publisher: str,
) -> "str | None":
    """``config.toml`` から ``[publishers.<publisher>] date_property`` を抽出する.

    Args:
        config_data: :func:`load_config_toml` の戻り値.
        publisher: ``"notion"`` / ``"obsidian"``.

    Raises:
        ConfigFileError: 構造不正 / 値が文字列でない / 空文字列.

    Returns:
        指定されていれば文字列、未指定なら ``None`` (= 各 publisher の既定).
    """
    publishers = config_data.get("publishers")
    if publishers is None:
        return None
    if not isinstance(publishers, dict):
        raise ConfigFileError(
            f"config.toml の 'publishers' は table である必要があります "
            f"(got {type(publishers).__name__})"
        )
    section = publishers.get(publisher)
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ConfigFileError(
            f"config.toml の 'publishers.{publisher}' は table である必要があります "
            f"(got {type(section).__name__})"
        )
    value = section.get("date_property")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigFileError(
            f"config.toml: [publishers.{publisher}].date_property は文字列である "
            f"必要があります (got {type(value).__name__})"
        )
    if not value.strip():
        raise ConfigFileError(
            f"config.toml: [publishers.{publisher}].date_property が空 / 空白のみです"
        )
    return value


def extract_notion_date_property(
    config_data: "dict[str, Any]",
) -> "str | None":
    """``config.toml`` から ``[publishers.notion] date_property`` を抽出する.

    Phase 1 B2 で導入. 別名の Notion DB の "日付" 列名を override するため.
    例::

        [publishers.notion]
        date_property = "日付"

    Returns:
        指定されていれば文字列、未指定なら ``None``
        (= ``NotionClient.DEFAULT_DATE_PROPERTY_NAME`` が使われる).
    """
    return extract_date_property(config_data, "notion")


def extract_obsidian_date_property(
    config_data: "dict[str, Any]",
) -> "str | None":
    """``[publishers.obsidian] date_property`` (最小ノート生成時の日付キー, 既定 ``日付``)。"""
    return extract_date_property(config_data, "obsidian")


def check_property_collisions(
    defaults: "Mapping[str, str]",
    overrides: "Mapping[str, str]",
    *,
    publisher: str,
    reserved: "Mapping[str, str] | None" = None,
) -> None:
    """既定 + override 統合後に同じ property 名へ 2 つの内部名が向いていないか検査する。

    1 つの frontmatter キー / Notion 列に 2 つの値を書こうとすると後勝ちで片方が
    黙って消えるので、設定の時点で止める (SPEC v2 §4.5)。

    Args:
        defaults: 内部名 → 既定 property 名 (並び順 = 報告順)
        overrides: ``config.toml`` 由来の上書き
        publisher: メッセージ用の publisher 名
        reserved: 書き込み対象と衝突してはいけない property 名 → 用途の説明
            (例: obsidian の ``date_property``)

    Raises:
        ConfigFileError: 衝突があった場合 (property 名と両方の内部名をメッセージに含む)。
    """
    owners: "dict[str, str]" = {}
    for field_name, default_name in defaults.items():
        prop = overrides.get(field_name, default_name)
        previous = owners.get(prop)
        if previous is not None and previous != field_name:
            raise ConfigFileError(
                f"[publishers.{publisher}] の property 名 {prop!r} が複数フィールドで衝突: "
                f"{previous} と {field_name}"
            )
        owners[prop] = field_name
    for prop, purpose in (reserved or {}).items():
        if prop in owners:
            raise ConfigFileError(
                f"[publishers.{publisher}] の property 名 {prop!r} が {purpose} と衝突: "
                f"{owners[prop]}"
            )
