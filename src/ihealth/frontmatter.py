"""Obsidian ノートの YAML frontmatter を「担当キーだけ」書き換える純関数群.

I/O は一切しない (文字列 → 文字列). ファイルの読み書き・競合検知・バックアップは
:mod:`ihealth.publishers.obsidian` の責務.

## 何を保証するか (byte 保存)

``upsert_frontmatter`` が返すテキストは、元テキストに対して

1. ``updates`` で指定したキーの **entry (key 行 + 継続行)** を
   ``{元の key 部分}: {新しい値}{改行}`` の 1 行に置き換え、
2. frontmatter に存在しないキーを閉じ ``---`` の **直前** に追記した

だけのものになる. それ以外 — frontmatter 内の担当外 entry・コメント・空行、
閉じ ``---`` 以降の本文、BOM、改行コード、末尾改行の有無 — は 1 byte も変えない.
Unicode 正規化 (NFC/NFD) もしない (実ノートの本文には NFD 文字列が混在する).

## 対応する frontmatter の形

- 先頭行 (BOM の後) が ``---`` で、次に現れる ``---`` 行 (または ``...`` 行) まで.
- top-level は block mapping のみ. 行頭 (インデント無し) の ``key:`` 行が entry の開始.
  後続の「空白/タブで始まる行」「``-`` で始まる block sequence 行」は同じ entry の継続.
- 空行とコメント行 (行頭 ``#`` も、インデント付きの ``  # …`` も) は entry を延長しない.
  ただし後ろに同じ entry の継続行が続く場合 (コメントを挟んだ block 値) は entry の内側
  として扱う (= その entry を置換するとき一緒に置き換わる. 継続行だけが孤立して YAML が
  壊れる事故を防ぐため). 例外として、block scalar (``|`` / ``>``) の内容インデント以上に
  字下げされた ``#`` 行はコメントではなく値の一部.
- 値が quoted scalar (``"…"`` / ``'…'``) や flow collection (``[…]`` / ``{…}``) で
  key 行のうちに閉じない場合は、閉じる行までを同じ entry とする (インデント無しの
  継続行も含む. YAML パーサと同じく、継続行を別のキーと誤認しない). frontmatter 内で
  閉じない / 閉じた後ろに余計な文字がある場合は :class:`FrontmatterError`.
- key は ``"…"`` (YAML double-quoted, escape 解釈), ``'…'`` (``''`` → ``'``), plain
  (前後空白除去) の 3 形式. 突き合わせは Unicode NFC 正規化後の完全一致
  (表示上同一の NFC/NFD キーを別キー扱いして重複追記しないため).

解釈できない top-level 行 (flow mapping, complex key ``? …``, 閉じ ``---`` 欠落など) を
含む frontmatter は **変更せず** :class:`FrontmatterError` を raise する
(壊れた/想定外の frontmatter に追記して被害を広げない).

## 既存値との比較 (``unchanged`` 判定)

1 行の scalar と literal block scalar (``|`` / ``|-`` / ``|+``. Obsidian は改行入りの
文字列をこの形に書き直す) は値を復号し、新しい値と **YAML として同じ値** なら書き換えない.
それ以外の複数行の値 (block collection, folded ``>``, 複数行の quoted / plain など) は
比較不能として扱い、値を書き換える (比較できなくても「書き込み漏れ」にはならない安全側).
"""

from __future__ import annotations

import json
import math
import re
import string
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Mapping


_BOM = "\ufeff"

#: FieldChange.action の取りうる値.
ACTION_INSERT = "insert"
ACTION_UPDATE = "update"
ACTION_UNCHANGED = "unchanged"
ACTION_KEPT = "kept"


class FrontmatterError(ValueError):
    """frontmatter を安全に書き換えられない (閉じ ``---`` 欠落 / 解釈不能な行 など)."""


@dataclass(frozen=True)
class FieldChange:
    """1 キー分の upsert 結果.

    Attributes:
        key: ``updates`` に渡されたキー.
        action: ``"insert"`` (新規追記) / ``"update"`` (値を置換) /
            ``"unchanged"`` (既存値と意味的に同一なので触らない) /
            ``"kept"`` (``fill_only_empty`` で既存の非空値を保持).
        old: 既存の raw 値テキスト (key 行のコロン以降を前後空白除去したもの +
            継続行を ``\\n`` 連結). ``insert`` では ``None``.
        new: 新しい raw 値テキスト (:func:`render_scalar` の出力). ``kept`` でも
            「書くはずだった値」を入れる (既存値との不一致を台帳に残せるように).
        warning: 重複キーなど、処理はしたが注意が必要な状況の説明. 通常 ``None``.
    """

    key: str
    action: str
    old: "str | None"
    new: "str | None"
    warning: "str | None" = None


# ---------------------------------------------------------------------------
# scalar / key の YAML 表現
# ---------------------------------------------------------------------------

def _needs_extra_escape(ch: str) -> bool:
    """json.dumps が escape しないが YAML では印字不可 / 改行扱いになりうる文字か.

    DEL, C1 制御文字 (NEL 含む), LINE/PARAGRAPH SEPARATOR, 非文字 U+FFFE/U+FFFF.
    """
    o = ord(ch)
    return (0x7F <= o <= 0x9F) or o in (0x2028, 0x2029, 0xFFFE, 0xFFFF)


def _quote_string(value: str) -> str:
    """文字列を YAML double-quoted (= JSON 互換) scalar にする."""
    for ch in value:
        if 0xD800 <= ord(ch) <= 0xDFFF:
            raise ValueError(
                "孤立サロゲートを含む文字列は frontmatter に書けません"
            )
    dumped = json.dumps(value, ensure_ascii=False)
    if not any(_needs_extra_escape(ch) for ch in dumped):
        return dumped
    return "".join(
        "\\u%04x" % ord(ch) if _needs_extra_escape(ch) else ch for ch in dumped
    )


def render_scalar(value: object) -> str:
    """Python 値を frontmatter に書く YAML scalar テキストにする.

    - ``bool`` → ``true`` / ``false`` (``int`` より先に判定)
    - ``int`` → ``str(value)``
    - ``float`` → 有限値のみ. 整数値なら int 表記 (``66.0`` → ``66``)、それ以外は
      ``repr`` (shortest round-trip). 指数表記になる値は位置表記に直す
      (YAML 1.1 では ``1e-05`` が文字列扱いになるため).
    - ``str`` → ``json.dumps(ensure_ascii=False)`` の double-quoted. 加えて
      U+2028/U+2029・DEL・C1 制御文字・U+FFFE/U+FFFF を ``\\uXXXX`` で escape
      (YAML double-quoted と JSON の共通部分だけを使う). 改行は ``\\n``.
    - ``datetime.date`` (``datetime`` は除く) → ISO 形式 ``YYYY-MM-DD`` (unquoted).

    Raises:
        ValueError: NaN / ±Inf / 孤立サロゲート.
        TypeError: 上記以外の型 (``None`` / ``list`` / ``datetime`` など).
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(int(value))  # IntEnum 等の subclass でも数値表記にする
    if isinstance(value, float):
        value = float(value)
        if not math.isfinite(value):
            raise ValueError(f"NaN/Inf は frontmatter に書けません: {value!r}")
        if value.is_integer():
            return str(int(value))
        text = repr(value)
        if "e" in text or "E" in text:
            text = format(Decimal(text), "f")
        return text
    if isinstance(value, str):
        return _quote_string(value)
    if isinstance(value, datetime):
        raise TypeError("datetime は frontmatter scalar として未対応です (date を渡してください)")
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"frontmatter scalar として未対応の型です: {type(value).__name__}")


#: plain key の先頭に来ると YAML の indicator として解釈されうる文字.
_KEY_UNSAFE_LEADING = frozenset("-?:,[]{}#&*!|>'\"%@`")

#: plain のままだと文字列以外 (null / bool / 数値 / 日付) に解決されうる key.
#: YAML 1.2 core schema と YAML 1.1 の両方を保守的にカバーする.
_AMBIGUOUS_PLAIN_RE = re.compile(
    r"""^(?:
        ~|null|Null|NULL
      | y|Y|yes|Yes|YES|n|N|no|No|NO
      | true|True|TRUE|false|False|FALSE
      | on|On|ON|off|Off|OFF
      | [-+]?[0-9][0-9_]*(?:\.[0-9_]*)?(?:[eE][-+]?[0-9]+)?
      | [-+]?\.[0-9]+(?:[eE][-+]?[0-9]+)?
      | 0o[0-7]+ | 0x[0-9a-fA-F]+ | 0b[01_]+
      | [-+]?\.(?:inf|Inf|INF) | \.(?:nan|NaN|NAN)
      | [0-9]{4}-[0-9]{1,2}-[0-9]{1,2}.*
      | <<|=
    )$""",
    re.VERBOSE,
)


def _is_plain_safe_char(ch: str) -> bool:
    o = ord(ch)
    if o < 0x20 or _needs_extra_escape(ch) or 0xD800 <= o <= 0xDFFF:
        return False
    return ch != _BOM


def render_key(key: str) -> str:
    """frontmatter key を YAML 表現にする. 安全な plain key はそのまま、それ以外は quote.

    quote する条件: 空 / 前後空白 / 先頭が indicator 文字 / ``": "`` ``" #"`` を含む /
    ``:`` で終わる / タブ・制御文字を含む / 文字列以外に解決されうる (``true``, ``123``,
    ``2026-10-11`` など). 例: ``歩数 (歩)`` → ``歩数 (歩)``, ``a: b`` → ``"a: b"``.

    Raises:
        TypeError: ``key`` が str でない.
        ValueError: 孤立サロゲートを含む.
    """
    if not isinstance(key, str):
        raise TypeError(f"frontmatter key は str である必要があります: {type(key).__name__}")
    needs_quote = (
        key == ""
        or key != key.strip()
        or key[0] in _KEY_UNSAFE_LEADING
        or ": " in key
        or " #" in key
        or "\t" in key
        or key.endswith(":")
        or not all(_is_plain_safe_char(ch) for ch in key)
        or _AMBIGUOUS_PLAIN_RE.match(key) is not None
    )
    return _quote_string(key) if needs_quote else key


# ---------------------------------------------------------------------------
# 行分割 / quoted scalar の走査
# ---------------------------------------------------------------------------

def _split_lines(text: str) -> "list[str]":
    """``\\n`` だけで分割し、各要素に改行 (``\\n`` or ``\\r\\n``) を含めたまま返す.

    ``str.splitlines`` は U+2028 / U+0085 / ``\\x0c`` 等でも分割してしまい
    byte 保存が壊れるので使わない.
    """
    lines: "list[str]" = []
    start = 0
    length = len(text)
    while start < length:
        idx = text.find("\n", start)
        if idx < 0:
            lines.append(text[start:])
            break
        lines.append(text[start:idx + 1])
        start = idx + 1
    return lines


def _content(line: str) -> str:
    """行から改行 (``\\n`` / ``\\r\\n``) を除いた本体."""
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith("\n"):
        return line[:-1]
    return line


def _detect_newline(text: str) -> str:
    """最初に出現する改行コード. 改行が無ければ ``\\n``."""
    idx = text.find("\n")
    if idx > 0 and text[idx - 1] == "\r":
        return "\r\n"
    return "\n"


_SIMPLE_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n",
    "v": "\v", "f": "\f", "r": "\r", "e": "\x1b", " ": " ", '"': '"',
    "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0", "L": "\u2028", "P": "\u2029",
}
_HEX_WIDTH = {"x": 2, "u": 4, "U": 8}
_HEXDIGITS = frozenset(string.hexdigits)


def _read_hex(s: str, start: int, width: int) -> "int | None":
    part = s[start:start + width]
    if len(part) != width or not all(c in _HEXDIGITS for c in part):
        return None
    return int(part, 16)


#: 走査結果の status.
_SCAN_OK = "ok"              # 閉じた
_SCAN_UNCLOSED = "unclosed"  # 入力の末尾まで閉じない (次の行へ続いている可能性)
_SCAN_INVALID = "invalid"    # 不正な escape / 括弧の対応違い


def _scan_dq(s: str, i: int) -> "tuple[str, int, str]":
    """``s[i] == '"'`` から閉じ quote までを YAML double-quoted として走査する.

    Returns:
        ``(status, end, value)``. ``status == "ok"`` のとき ``end`` は閉じ quote の
        次の index、``value`` は unescape 済み文字列. JSON 形式のサロゲートペア
        (``\\ud83d\\ude00``) は 1 文字に結合する (Obsidian = JavaScript の解釈と同じ).
        ``s`` が複数行を ``\\n`` で連結したものなら、行末の ``\\`` (escape された改行)
        は読み飛ばす (複数行の値は折り返し規則を適用しないので ``end`` だけを使うこと).
    """
    out: "list[str]" = []
    j = i + 1
    n = len(s)
    while j < n:
        ch = s[j]
        if ch == '"':
            return _SCAN_OK, j + 1, "".join(out)
        if ch != "\\":
            out.append(ch)
            j += 1
            continue
        if j + 1 >= n:
            return _SCAN_UNCLOSED, n, ""
        esc = s[j + 1]
        if esc == "\n":
            j += 2
            continue
        if esc in _SIMPLE_ESCAPES:
            out.append(_SIMPLE_ESCAPES[esc])
            j += 2
            continue
        width = _HEX_WIDTH.get(esc)
        if width is None:
            return _SCAN_INVALID, j, ""
        cp = _read_hex(s, j + 2, width)
        if cp is None or cp > 0x10FFFF:
            return _SCAN_INVALID, j, ""
        advance = 2 + width
        if esc == "u" and 0xD800 <= cp <= 0xDBFF and s[j + 6:j + 8] == "\\u":
            low = _read_hex(s, j + 8, 4)
            if low is not None and 0xDC00 <= low <= 0xDFFF:
                cp = 0x10000 + ((cp - 0xD800) << 10) + (low - 0xDC00)
                advance = 12
        out.append(chr(cp))
        j += advance
    return _SCAN_UNCLOSED, n, ""


def _scan_double_quoted(s: str, i: int) -> "tuple[int, str] | None":
    """:func:`_scan_dq` の簡易版. 閉じれば ``(end, value)``、それ以外は ``None``."""
    status, end, value = _scan_dq(s, i)
    return (end, value) if status == _SCAN_OK else None


def _scan_sq(s: str, i: int) -> "tuple[str, int, str]":
    """``s[i] == "'"`` から閉じ quote までを走査する (``''`` は ``'`` 1 文字).

    Returns:
        ``(status, end, value)`` (:func:`_scan_dq` と同じ形. status は ok / unclosed).
    """
    out: "list[str]" = []
    j = i + 1
    n = len(s)
    while j < n:
        ch = s[j]
        if ch == "'":
            if j + 1 < n and s[j + 1] == "'":
                out.append("'")
                j += 2
                continue
            return _SCAN_OK, j + 1, "".join(out)
        out.append(ch)
        j += 1
    return _SCAN_UNCLOSED, n, ""


def _scan_single_quoted(s: str, i: int) -> "tuple[int, str] | None":
    """:func:`_scan_sq` の簡易版. 閉じれば ``(end, value)``、それ以外は ``None``."""
    status, end, value = _scan_sq(s, i)
    return (end, value) if status == _SCAN_OK else None


_FLOW_CLOSERS = {"[": "]", "{": "}"}


def _scan_flow(s: str, i: int) -> "tuple[str, int]":
    """``s[i]`` (``[`` / ``{``) から対応する閉じ括弧までを走査する.

    quoted scalar (scalar の先頭位置にある quote だけ) とコメント (空白・改行の直後の
    ``#`` から行末まで) の中の括弧は数えない.

    Returns:
        ``(status, end)``. ``status == "ok"`` のとき ``end`` は閉じ括弧の次の index.
    """
    stack: "list[str]" = []
    prev = ""  # 直前の有意文字 (空白・コメント以外)
    j = i
    n = len(s)
    while j < n:
        ch = s[j]
        if ch in "[{":
            stack.append(_FLOW_CLOSERS[ch])
        elif ch in "]}":
            if not stack or stack.pop() != ch:
                return _SCAN_INVALID, j
            if not stack:
                return _SCAN_OK, j + 1
        elif ch in "\"'" and prev in ("[", "{", ",", ":", "?"):
            status, end, _value = _scan_dq(s, j) if ch == '"' else _scan_sq(s, j)
            if status != _SCAN_OK:
                return status, end
            prev = ch
            j = end
            continue
        elif ch == "#" and s[j - 1] in " \t\n":
            newline = s.find("\n", j)
            if newline < 0:
                return _SCAN_UNCLOSED, n
            j = newline
            continue
        if ch not in " \t\n":
            prev = ch
        j += 1
    return _SCAN_UNCLOSED, n


def _scan_value(s: str) -> "tuple[str, int]":
    """``s[0]`` が ``"`` / ``'`` / ``[`` / ``{`` の値を走査して ``(status, end)`` を返す."""
    first = s[0]
    if first == '"':
        status, end, _value = _scan_dq(s, 0)
        return status, end
    if first == "'":
        status, end, _value = _scan_sq(s, 0)
        return status, end
    return _scan_flow(s, 0)


def _is_blank_or_comment(rest: str) -> bool:
    """quoted scalar の閉じ quote 以降が「空白 + 任意のコメント」だけか."""
    stripped = rest.lstrip(" \t")
    if stripped == "":
        return True
    return stripped.startswith("#") and stripped != rest  # '#' の前に空白が必要


# ---------------------------------------------------------------------------
# frontmatter の構造解析
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _KeyLine:
    key: str        # 解釈済み key
    key_raw: str    # key 行のコロン直前までの原文 (quote・空白込み)
    value_head: str  # key 行のコロン以降 (改行除く)


@dataclass(frozen=True)
class _Entry:
    key: str
    key_raw: str
    value_head: str
    start: int   # key 行の index (frontmatter 外も含めた lines の index)
    end: int     # 最後の継続行 + 1 (exclusive)


@dataclass(frozen=True)
class _Parsed:
    lines: "list[str]"
    close_idx: int
    entries: "list[_Entry]"


def _parse_key_line(content: str) -> "_KeyLine | None":
    """インデント無し行を ``key: value`` として解釈する. 解釈不能なら ``None``."""
    if not content:
        return None
    first = content[0]
    if first in ('"', "'"):
        scanned = (
            _scan_double_quoted(content, 0) if first == '"'
            else _scan_single_quoted(content, 0)
        )
        if scanned is None:
            return None
        end, key = scanned
        j = end
        while j < len(content) and content[j] in " \t":
            j += 1
        if j >= len(content) or content[j] != ":":
            return None
        if j + 1 < len(content) and content[j + 1] not in " \t":
            return None
        return _KeyLine(key=key, key_raw=content[:j], value_head=content[j + 1:])
    if first in ",[]{}#&*!|>%@`":
        return None
    if first in "-?:" and (len(content) == 1 or content[1] in " \t"):
        return None
    for j, ch in enumerate(content):
        if ch == "#" and j > 0 and content[j - 1] in " \t":
            return None  # key の途中でコメントが始まる = mapping ではない
        if ch == ":" and (j + 1 == len(content) or content[j + 1] in " \t"):
            key_raw = content[:j]
            key = key_raw.strip(" \t")
            if not key:
                return None
            return _KeyLine(key=key, key_raw=key_raw, value_head=content[j + 1:])
    return None


def _is_delimiter(content: str, marks: "tuple[str, ...]") -> bool:
    return content.rstrip(" \t") in marks


def _leading_spaces(content: str) -> int:
    """行頭のスペース (タブは含めない) の数."""
    return len(content) - len(content.lstrip(" "))


def _value_start(value_head: str) -> int:
    """key 行のコロン以降で値の本体が始まる index (先頭の空白と anchor / tag を飛ばす)."""
    j = 0
    n = len(value_head)
    while True:
        while j < n and value_head[j] in " \t":
            j += 1
        if j < n and value_head[j] in "!&":
            while j < n and value_head[j] not in " \t":
                j += 1
            continue
        return j


_BLOCK_HEADER_RE = re.compile(
    r"^(?P<style>[|>])(?P<ind>[1-9][-+]?|[-+][1-9]?)?(?:[ \t]+#.*)?$"
)


@dataclass(frozen=True)
class _BlockHeader:
    """block scalar の header (``|`` / ``|-`` / ``>+`` / ``|2-`` など)."""

    literal: bool         # ``|`` なら True、``>`` (folded) なら False
    chomp: str            # ``""`` (clip) / ``"-"`` (strip) / ``"+"`` (keep)
    indent: "int | None"  # 明示インデント指示子. 無ければ None (最初の内容行から自動検出)
    has_props: bool       # anchor / tag が付いている


def _block_header(value_head: str) -> "_BlockHeader | None":
    """key 行の値部が block scalar の header なら解釈して返す. それ以外は ``None``."""
    start = _value_start(value_head)
    match = _BLOCK_HEADER_RE.match(value_head[start:].rstrip(" \t"))
    if match is None:
        return None
    indicators = match.group("ind") or ""
    digits = "".join(ch for ch in indicators if ch.isdigit())
    return _BlockHeader(
        literal=match.group("style") == "|",
        chomp="".join(ch for ch in indicators if ch in "-+"),
        indent=int(digits) if digits else None,
        has_props=value_head[:start].strip(" \t") != "",
    )


def _block_content_indent(
    lines: "list[str]", key_idx: int, close_idx: int, header: _BlockHeader,
) -> "int | None":
    """block scalar の内容インデント (行頭スペース数). 内容行が無ければ ``None``."""
    if header.indent is not None:
        return header.indent
    for k in range(key_idx + 1, close_idx):
        content = _content(lines[k])
        if content.strip(" \t") == "":
            continue
        spaces = _leading_spaces(content)
        return spaces if spaces > 0 else None
    return None


def _multiline_end(
    lines: "list[str]", key_idx: int, close_idx: int, value_head: str,
) -> int:
    """key 行の値が quoted scalar / flow collection で key 行のうちに閉じない場合、
    値が閉じる行の index を返す. 1 行で完結する値 (と quoted / flow 以外の値) は ``key_idx``.

    1 行の中で不正 (未知の escape, 括弧の対応違い) な値は 1 行の値として扱う
    (値は比較不能 = 書き換え対象になるだけで、entry の範囲は明確なため).

    Raises:
        FrontmatterError: frontmatter の終わりまで閉じない / 閉じた行の後ろに
            余計な文字がある (= entry の範囲を確定できない).
    """
    start = _value_start(value_head)
    if start >= len(value_head) or value_head[start] not in "\"'[{":
        return key_idx
    head = value_head[start:]
    status, _end = _scan_value(head)
    if status != _SCAN_UNCLOSED:
        return key_idx
    joined = "\n".join([head] + [_content(lines[k]) for k in range(key_idx + 1, close_idx)])
    status, end = _scan_value(joined)
    if status != _SCAN_OK:
        raise FrontmatterError(
            f"frontmatter {key_idx + 1} 行目: 値の quote / 括弧が frontmatter 内で閉じていません"
        )
    end_idx = key_idx + joined.count("\n", 0, end)
    newline = joined.find("\n", end)
    rest = joined[end:] if newline < 0 else joined[end:newline]
    if not _is_blank_or_comment(rest):
        raise FrontmatterError(
            f"frontmatter {end_idx + 1} 行目: 複数行の値の後ろに解釈できない文字があります"
        )
    return end_idx


def _parse(lines: "list[str]") -> "_Parsed | None":
    """frontmatter を解析する. frontmatter が無ければ ``None``.

    Raises:
        FrontmatterError: 閉じ ``---`` 欠落 / 解釈不能な top-level 行 /
            frontmatter 内で閉じない複数行の値.
    """
    if not lines or not _is_delimiter(_content(lines[0]), ("---",)):
        return None
    close_idx = None
    for i in range(1, len(lines)):
        if _is_delimiter(_content(lines[i]), ("---", "...")):
            close_idx = i
            break
    if close_idx is None:
        raise FrontmatterError(
            "frontmatter の閉じ '---' が見つかりません (壊れた frontmatter には書き込みません)"
        )

    entries: "list[_Entry]" = []
    current: "_KeyLine | None" = None
    current_start = -1
    current_last = -1
    block_indent: "int | None" = None  # 現 entry が block scalar のときの内容インデント

    def _flush() -> None:
        if current is not None:
            entries.append(_Entry(
                key=current.key, key_raw=current.key_raw,
                value_head=current.value_head,
                start=current_start, end=current_last + 1,
            ))

    i = 1
    while i < close_idx:
        content = _content(lines[i])
        stripped = content.lstrip(" \t")
        if stripped == "":
            i += 1
            continue  # 空行: entry を延長しない (後続の継続行があれば内側)
        indented = content[0] in " \t"
        if stripped.startswith("#") and not (
            indented and block_indent is not None and _leading_spaces(content) >= block_indent
        ):
            i += 1
            continue  # コメント行 (行頭 / インデント付き): 空行と同じ扱い
        if indented or content == "-" or content.startswith(("- ", "-\t")):
            if current is None:
                raise FrontmatterError(
                    f"frontmatter {i + 1} 行目: key より前にインデント行/シーケンス行があります"
                )
            current_last = i
            i += 1
            continue
        parsed = _parse_key_line(content)
        if parsed is None:
            raise FrontmatterError(
                f"frontmatter {i + 1} 行目を 'key: value' として解釈できません"
            )
        _flush()
        current = parsed
        current_start = i
        current_last = _multiline_end(lines, i, close_idx, parsed.value_head)
        header = _block_header(parsed.value_head)
        block_indent = (
            None if header is None else _block_content_indent(lines, i, close_idx, header)
        )
        i = current_last + 1
    _flush()
    return _Parsed(lines=lines, close_idx=close_idx, entries=entries)


def split_frontmatter(text: str) -> "tuple[str, str] | None":
    """``(BOM + frontmatter ブロック (閉じ行と改行を含む), それ以降の本文)`` に分ける.

    frontmatter が無ければ ``None``. 2 つを連結すると元の ``text`` に戻る.

    Raises:
        FrontmatterError: 閉じ ``---`` 欠落 / 解釈不能な top-level 行.
    """
    bom = _BOM if text.startswith(_BOM) else ""
    lines = _split_lines(text[len(bom):])
    parsed = _parse(lines)
    if parsed is None:
        return None
    head = bom + "".join(lines[:parsed.close_idx + 1])
    return head, text[len(head):]


# ---------------------------------------------------------------------------
# 既存値の解釈 (空判定 / 意味的同一判定)
# ---------------------------------------------------------------------------

_NULL_RE = re.compile(r"^(?:null|Null|NULL|~)$")
_BOOL_TRUE_RE = re.compile(r"^(?:true|True|TRUE)$")
_BOOL_FALSE_RE = re.compile(r"^(?:false|False|FALSE)$")
_INT_RE = re.compile(r"^[-+]?[0-9]+$")
_FLOAT_RE = re.compile(r"^[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?$")

_KIND_NULL = "null"
_KIND_EMPTY_COLLECTION = "empty_collection"
_KIND_STR = "str"
_KIND_NUMBER = "number"
_KIND_BOOL = "bool"


def _parse_inline_scalar(value_head: str) -> "tuple[str, object] | None":
    """key 行のコロン以降を単一行 scalar として解釈する (YAML 1.2 core schema 相当).

    Returns:
        ``(kind, value)``. 解釈できない (flow collection, anchor/tag, block scalar
        indicator, plain 内の ``": "`` など) なら ``None``.
    """
    s = value_head.strip(" \t")
    if s == "" or s.startswith("#"):
        return _KIND_NULL, None
    first = s[0]
    if first in ('"', "'"):
        scanned = _scan_double_quoted(s, 0) if first == '"' else _scan_single_quoted(s, 0)
        if scanned is None:
            return None
        end, value = scanned
        if not _is_blank_or_comment(s[end:]):
            return None
        return _KIND_STR, value
    if first in "[{":
        if s.replace(" ", "").replace("\t", "") in ("[]", "{}"):
            return _KIND_EMPTY_COLLECTION, None
        return None
    if first in "&*!|>%@`,]}":
        return None
    # plain scalar: 末尾コメントを除去
    for j in range(1, len(s)):
        if s[j] == "#" and s[j - 1] in " \t":
            s = s[:j].rstrip(" \t")
            break
    if ": " in s or ":\t" in s or s.endswith(":"):
        return None
    if first in "-?:" and (len(s) == 1 or s[1] in " \t"):
        return None
    if _NULL_RE.match(s):
        return _KIND_NULL, None
    if _BOOL_TRUE_RE.match(s):
        return _KIND_BOOL, True
    if _BOOL_FALSE_RE.match(s):
        return _KIND_BOOL, False
    if _INT_RE.match(s):
        return _KIND_NUMBER, int(s)
    if _FLOAT_RE.match(s):
        return _KIND_NUMBER, float(s)
    return _KIND_STR, s


def _semantically_equal(parsed: "tuple[str, object] | None", new_value: object) -> bool:
    """既存値 (解釈済み) と新しい値が YAML として同じ値か."""
    if parsed is None:
        return False
    kind, existing = parsed
    if isinstance(new_value, bool):
        return kind == _KIND_BOOL and existing is new_value
    if isinstance(new_value, (int, float)):
        return kind == _KIND_NUMBER and existing == new_value
    if isinstance(new_value, str):
        return kind == _KIND_STR and existing == new_value
    if isinstance(new_value, date) and not isinstance(new_value, datetime):
        return kind == _KIND_STR and existing == new_value.isoformat()
    return False


def _decode_block_scalar(
    lines: "list[str]", entry: _Entry, close_idx: int, header: _BlockHeader,
) -> "tuple[str, object] | None":
    """literal block scalar (``|`` / ``|-`` / ``|+``) を文字列に復号する.

    Obsidian は改行を含む文字列 (例: 複数件の ``Github``) を ``|-`` + 2 スペース
    インデントで書き直すので、これを復号できないと毎回「値が違う」と判定して
    無駄な書き換え・バックアップ・同期を起こす.

    folded (``>``)、anchor / tag 付き、インデントにタブを含む、内容インデントより浅い
    行が混じる (= YAML として不正)、末尾の空白だけの行が値に含まれる、など確実に
    解釈できない形は ``None`` (比較不能 = 書き換え対象) を返す.
    """
    if not header.literal or header.has_props:
        return None
    body = [_content(lines[k]) for k in range(entry.start + 1, entry.end)]
    # entry 直後の空行 (entry の範囲外). keep (``+``) では末尾の改行として値に含まれる.
    trailing: "list[str]" = []
    for k in range(entry.end, close_idx):
        content = _content(lines[k])
        if content.strip(" \t") != "":
            break
        trailing.append(content)
    for content in body + trailing:
        if "\t" in content[:len(content) - len(content.lstrip(" \t"))]:
            return None  # インデントにタブは使えない
    indent = header.indent
    if indent is None:
        indent = next((_leading_spaces(c) for c in body if c.strip(" ") != ""), None)
    if not body:
        if header.chomp != "+":
            return _KIND_STR, ""
        if indent is not None and any(_leading_spaces(c) > indent for c in trailing):
            return None
        return _KIND_STR, "\n" * len(trailing)
    if indent is None or indent < 1:
        return None
    out: "list[str]" = []
    seen_text = False
    for content in body:
        spaces = _leading_spaces(content)
        if content.strip(" ") == "":
            if spaces <= indent:
                out.append("")
            elif not seen_text and header.indent is None:
                return None  # 先頭の空行が最初の内容行より深い (YAML として不正)
            else:
                out.append(content[indent:])  # 空白だけの内容行
            continue
        if spaces < indent:
            return None  # 内容インデントより浅い行 (YAML として不正)
        seen_text = True
        out.append(content[indent:])
    if any(_leading_spaces(c) > indent for c in trailing):
        return None  # 末尾に空白だけの内容行がある (稀) → 比較しない
    text = "\n".join(out)
    if header.chomp == "-":
        return _KIND_STR, text
    if header.chomp == "+":
        return _KIND_STR, text + "\n" * (1 + len(trailing))
    return _KIND_STR, text + "\n"


def _existing_scalar(
    lines: "list[str]", entry: _Entry, close_idx: int,
) -> "tuple[str, object] | None":
    """entry の既存値を ``(kind, value)`` に解釈する. 比較不能なら ``None``."""
    header = _block_header(entry.value_head)
    if header is not None:
        return _decode_block_scalar(lines, entry, close_idx, header)
    if entry.end - entry.start > 1:
        return None  # block collection / 複数行の scalar: 比較しない
    return _parse_inline_scalar(entry.value_head)


def _raw_value(lines: "list[str]", entry: _Entry) -> str:
    parts = [entry.value_head.strip(" \t")]
    parts.extend(_content(lines[i]) for i in range(entry.start + 1, entry.end))
    return "\n".join(parts)


def _is_empty_value(existing: "tuple[str, object] | None") -> bool:
    """既存値が空 (null / 空文字列 / 空コレクション) か. 比較不能 (``None``) は空でない扱い."""
    if existing is None:
        return False
    kind, value = existing
    if kind in (_KIND_NULL, _KIND_EMPTY_COLLECTION):
        return True
    return kind == _KIND_STR and value == ""


def _norm_key(key: str) -> str:
    return unicodedata.normalize("NFC", key)


# ---------------------------------------------------------------------------
# upsert 本体
# ---------------------------------------------------------------------------

def upsert_frontmatter(
    text: str,
    updates: "Mapping[str, object]",
    *,
    fill_only_empty: bool = False,
    create_if_missing: bool = True,
) -> "tuple[str, list[FieldChange]]":
    """frontmatter の担当キーだけを書き換えたテキストと、キーごとの結果を返す.

    Args:
        text: ノート全文 (改行変換していない生テキスト).
        updates: frontmatter キー → 値. 値が ``None`` のキーは無視 (= 既存値保持).
            値は :func:`render_scalar` が扱える型.
        fill_only_empty: ``True`` なら既存値が空 (``key:`` / ``null`` / ``~`` /
            ``""`` / ``''`` / ``[]`` / ``{}`` で継続行なし) のキーだけ書き換える.
            空でない既存値は ``action="kept"`` で保持 (過去分 backfill 用).
        create_if_missing: frontmatter が無いノートに新規作成するか.
            ``False`` で frontmatter が無ければ :class:`FrontmatterError`.

    Returns:
        ``(新しいテキスト, [FieldChange, ...])``. FieldChange は ``updates`` の順
        (``None`` 値は除く). 何も変わらなければ入力と同じ文字列を返す.
        書くキーが 1 つも無い (全て ``None``) ときは frontmatter を解析せずに
        ``(text, [])`` を返す (触らないノートの形は問わない).

    ``unchanged`` 判定は **意味的同一** で行う: 既存値が ``55`` で新しい値が
    ``55.0``、既存が plain ``8.9時間`` で新しい値が ``"8.9時間"``、既存が
    ``|-`` の literal block で新しい値が同じ改行入り文字列、のように YAML として
    同じ値なら書き換えない (Obsidian が再シリアライズした表記を尊重し、
    無駄な書き込み・同期・バックアップを避ける).

    同一キーの entry が複数ある場合は最初の entry だけを対象にし、
    ``FieldChange.warning`` に記録する (他の entry には触れない).

    Raises:
        FrontmatterError: 閉じ ``---`` 欠落 / 解釈不能な top-level 行 /
            frontmatter 無しで ``create_if_missing=False``.
        TypeError / ValueError: 値やキーが書けない型・値 (NaN など). この場合も
            テキストは一切変更されない (全キーの render を先に済ませる).
    """
    pending: "list[tuple[str, object, str]]" = []
    seen_norm: "set[str]" = set()
    for key, value in updates.items():
        if value is None:
            continue
        if not isinstance(key, str) or not key:
            raise ValueError(f"frontmatter key は空でない str である必要があります: {key!r}")
        norm = _norm_key(key)
        if norm in seen_norm:
            raise ValueError(f"updates に正規化後同一のキーが重複しています: {key!r}")
        seen_norm.add(norm)
        pending.append((key, value, render_scalar(value)))

    if not pending:
        return text, []

    bom = _BOM if text.startswith(_BOM) else ""
    body = text[len(bom):]
    nl = _detect_newline(body)
    lines = _split_lines(body)
    parsed = _parse(lines)

    if parsed is None:
        if not create_if_missing:
            raise FrontmatterError("frontmatter がありません (create_if_missing=False)")
        block = "".join(f"{render_key(k)}: {rendered}{nl}" for k, _, rendered in pending)
        changes = [FieldChange(k, ACTION_INSERT, None, rendered) for k, _, rendered in pending]
        return f"{bom}---{nl}{block}---{nl}{body}", changes

    by_key: "dict[str, list[_Entry]]" = {}
    for entry in parsed.entries:
        by_key.setdefault(_norm_key(entry.key), []).append(entry)

    replacements: "dict[int, tuple[int, str]]" = {}
    inserts: "list[str]" = []
    changes: "list[FieldChange]" = []
    for key, value, rendered in pending:
        matches = by_key.get(_norm_key(key), [])
        if not matches:
            inserts.append(f"{render_key(key)}: {rendered}{nl}")
            changes.append(FieldChange(key, ACTION_INSERT, None, rendered))
            continue
        entry = matches[0]
        warning = None
        if len(matches) > 1:
            warning = (
                f"キー {key!r} の entry が frontmatter に {len(matches)} 個あります。"
                "最初の entry だけを対象にし、他は変更していません"
            )
        existing = _existing_scalar(lines, entry, parsed.close_idx)
        old_raw = _raw_value(lines, entry)
        if _semantically_equal(existing, value):
            action = ACTION_UNCHANGED
        elif fill_only_empty and not _is_empty_value(existing):
            action = ACTION_KEPT
        else:
            new_line = f"{entry.key_raw}: {rendered}{nl}"
            if new_line == "".join(lines[entry.start:entry.end]):
                action = ACTION_UNCHANGED
            else:
                action = ACTION_UPDATE
                replacements[entry.start] = (entry.end, new_line)
        changes.append(FieldChange(key, action, old_raw, rendered, warning))

    if not replacements and not inserts:
        return text, changes

    out: "list[str]" = []
    i = 0
    while i < len(lines):
        replacement = replacements.get(i)
        if replacement is not None:
            end, new_line = replacement
            out.append(new_line)
            i = end
            continue
        if i == parsed.close_idx:
            out.extend(inserts)
        out.append(lines[i])
        i += 1
    return bom + "".join(out), changes
