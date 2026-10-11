"""Health Auto Export「オートメーション」(iCloud Drive / JSON) 出力の探索と読み込み。

iPhone 側 Health Auto Export の **オートメーション** 機能 (iCloud Drive / JSON /
ExportVersion.v2 / exportFileLength=day) は、ユーザーが付けたオートメーション名の
フォルダに 1 日 1 ファイルの JSON を書き出す::

    <HAE Documents>/<オートメーション名>/HealthAutoExport-YYYY-MM-DD.json

フォルダ名はユーザーが任意に付ける (例: ``新しいオートメーション``) ため固定パスに
できない。本モジュールは Documents root 以下を **名前パターンだけで再帰探索** し、
日付 → 候補ファイルの索引 (:class:`AutomationIndex`) を作る。

設計上の注意:

* 同じ日付のファイルが複数フォルダにある (オートメーションを作り直した移行期間など)
  場合は **mtime が最新** のものを優先する (後から再出力された方が完全)。
  tie は size が大きい方、さらに tie は path 文字列順 (決定的にするため)。
* 探索は ``os.scandir`` の名前判定 + ``DirEntry.is_dir`` (d_type) が中心で、
  ``stat`` は名前が一致したファイルにしか発行しない。AutoSync 配下の数万の
  ``.hae`` があっても高速。``stat`` は iCloud dataless ファイルの materialize を
  起こさない (``ihealth.source._is_dataless`` の実機確認と同じ)。
* ``.`` 始まりの隠しディレクトリは降りない。symlink はたどらない (循環対策)。
* 実ファイルはほぼ全て iCloud **dataless** (placeholder)。``open()`` は File Provider
  の download 待ちで長時間 block しうるので、読む前に必ず
  :func:`ihealth.source._ensure_materialized` (``brctl download`` + polling) を通す。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from ihealth.source import SourceError, _ensure_materialized


#: オートメーション出力ファイル名。日付部分を group(1) で取り出す。
#: iCloud の競合コピー (``HealthAutoExport-2026-10-10 2.json``) は対象外。
AUTOMATION_FILE_RE = re.compile(r"^HealthAutoExport-(\d{4}-\d{2}-\d{2})\.json$")

#: 探索の深さ上限。root 直下のディレクトリが深さ 1
#: (``Documents/<オートメーション名>/<file>`` は深さ 1 のディレクトリ内)。
#: HAE の日付サブフォルダ運用でも 2-3 で足りるので 4 で十分な余裕がある。
DEFAULT_MAX_DEPTH = 4

#: 読み込む JSON のサイズ上限 (bytes)。実データは 1 日 0.4-0.8 MB 程度なので、
#: 200 MB を超えるものは壊れている / 想定外の出力とみなして読まない (メモリ保護)。
_MAX_JSON_SIZE_BYTES = 200 * 1024 * 1024


class AutomationError(RuntimeError):
    """オートメーション JSON の materialize / 読み込み / 構造検証の失敗。"""


def _empty_mapping() -> "Mapping":
    return MappingProxyType({})


@dataclass(frozen=True)
class AutomationIndex:
    """日付 → オートメーション JSON 候補の索引。

    Attributes:
        files: 日付 → 候補 path のタプル。**先頭が最良** (mtime 降順 → size 降順
            → path 順) になるよう sort 済み。
        mtimes: path → 探索時点の ``st_mtime``。:meth:`newest_mtime` 用。
            テスト等で ``files`` だけ与えて構築した場合は空でよい。
    """

    files: "Mapping[date, tuple[Path, ...]]" = field(default_factory=_empty_mapping)
    mtimes: "Mapping[Path, float]" = field(default_factory=_empty_mapping)

    def best(self, d: date) -> "Path | None":
        """``d`` の最良候補 (無ければ ``None``)。"""
        candidates = self.files.get(d)
        return candidates[0] if candidates else None

    def candidates(self, d: date) -> "tuple[Path, ...]":
        """``d`` の全候補 (最良が先頭)。候補が壊れていたときの fallback 用。"""
        return tuple(self.files.get(d, ()))

    def dates(self) -> "tuple[date, ...]":
        """候補が 1 つ以上ある日付の昇順タプル。"""
        return tuple(sorted(d for d, paths in self.files.items() if paths))

    def latest_date(self) -> "date | None":
        """最新ファイルの日付 (オートメーション停止検知用)。空なら ``None``。"""
        dates = self.dates()
        return dates[-1] if dates else None

    def newest_mtime(self) -> "float | None":
        """全候補の中で最も新しい ``st_mtime`` (epoch 秒)。不明なら ``None``。"""
        if not self.mtimes:
            return None
        return max(self.mtimes.values())


def discover_automation_files(
    root: Path,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    logger: "logging.Logger | None" = None,
) -> AutomationIndex:
    """``root`` 以下 ``max_depth`` 階層までを探索して :class:`AutomationIndex` を返す。

    - 名前が :data:`AUTOMATION_FILE_RE` に一致する通常ファイルだけを候補にする
      (日付として不正な ``2026-13-45`` 等は除外)
    - ``.`` 始まりのディレクトリは降りない。symlink はディレクトリ・ファイルとも
      たどらない (``follow_symlinks=False``: 循環 symlink / vault 外への逸脱対策)
    - 読めないディレクトリ (権限など) は skip して続行する。``root`` 自体が
      存在しない / 読めない場合も例外にせず空の索引を返す (呼び出し側が
      ``root.is_dir()`` で設定不備を判定する)

    ``stat`` は名前が一致したファイルにだけ発行する (dataless でも materialize は
    起きない)。
    """
    log = logger or logging.getLogger(__name__)
    found: "dict[date, list[tuple[float, int, str, Path]]]" = {}
    mtimes: "dict[Path, float]" = {}

    stack: "list[tuple[Path, int]]" = [(Path(root), 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = list(iterator)
        except OSError as exc:
            level = logging.WARNING if depth == 0 else logging.DEBUG
            log.log(level, "オートメーション探索: ディレクトリを読めません: %s (%s)",
                    directory, exc)
            continue

        for entry in entries:
            name = entry.name
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if name.startswith("."):
                    continue
                if depth < max_depth:
                    stack.append((Path(entry.path), depth + 1))
                continue

            match = AUTOMATION_FILE_RE.match(name)
            if match is None:
                continue
            try:
                file_date = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                log.debug("オートメーション探索: stat 失敗: %s (%s)", entry.path, exc)
                continue
            path = Path(entry.path)
            found.setdefault(file_date, []).append(
                (st.st_mtime, st.st_size, str(path), path)
            )
            mtimes[path] = st.st_mtime

    files: "dict[date, tuple[Path, ...]]" = {}
    for file_date, items in found.items():
        # mtime 降順 → size 降順 → path 昇順 (決定的)
        items.sort(key=lambda item: (-item[0], -item[1], item[2]))
        files[file_date] = tuple(item[3] for item in items)

    log.debug(
        "オートメーション探索: root=%s dates=%d files=%d",
        root, len(files), len(mtimes),
    )
    return AutomationIndex(
        files=MappingProxyType(files),
        mtimes=MappingProxyType(mtimes),
    )


def validate_automation_payload(payload: object, source: str = "<payload>") -> dict:
    """オートメーション JSON の最低限の構造を検証して dict を返す。

    期待する形::

        {"data": {"metrics": [ {...}, ... ], "workouts": [ ... ]?}}

    ``metrics`` / ``workouts`` は省略可 (データの無い日)。存在するなら list 必須。

    Raises:
        AutomationError: 構造が想定と異なる場合。
    """
    if not isinstance(payload, dict):
        raise AutomationError(
            f"オートメーション JSON の top-level が object ではありません: {source}"
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise AutomationError(
            f"オートメーション JSON に data object がありません: {source}"
        )
    for key in ("metrics", "workouts"):
        value = data.get(key)
        if value is not None and not isinstance(value, list):
            raise AutomationError(
                f"オートメーション JSON の data.{key} が配列ではありません: {source}"
            )
    return payload


def load_automation_json(path: Path, logger: logging.Logger) -> dict:
    """``path`` のオートメーション JSON を materialize → 読込 → 構造検証して返す。

    Raises:
        AutomationError: materialize 失敗 (``SourceError``) / I/O 失敗 /
            サイズ超過 / UTF-8 デコード失敗 / JSON 構文エラー / 構造不正。
            呼び出し側は次の候補ファイルへ fallback できる。
    """
    try:
        _ensure_materialized(path, logger)
    except SourceError as exc:
        raise AutomationError(f"materialize に失敗しました: {path}: {exc}") from exc

    try:
        size = path.stat().st_size
    except OSError as exc:
        raise AutomationError(
            f"オートメーション JSON の stat に失敗: {path}: {type(exc).__name__}: {exc}"
        ) from exc
    if size > _MAX_JSON_SIZE_BYTES:
        raise AutomationError(
            f"オートメーション JSON がサイズ上限を超えています "
            f"({size / 1024 / 1024:.1f} MB > "
            f"{_MAX_JSON_SIZE_BYTES / 1024 / 1024:.0f} MB): {path}"
        )

    try:
        # utf-8-sig: 先頭 BOM があっても json.loads が失敗しないように剥がす
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise AutomationError(
            f"オートメーション JSON の読み込みに失敗: {path}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    try:
        payload = json.loads(text)
    except (ValueError, RecursionError) as exc:
        # json.JSONDecodeError は ValueError のサブクラス
        raise AutomationError(
            f"オートメーション JSON の構文エラー: {path}: {exc}"
        ) from exc

    return validate_automation_payload(payload, str(path))
