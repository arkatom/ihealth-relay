"""``ihealth.frontmatter`` (byte 保存 upsert) の回帰テスト.

fixture はすべて合成 (実ノートの「形」だけを模したもの. 実健康値・日記本文は含めない).
「出力 == 元テキストの担当 entry だけを置換したもの」を **ファイル全体** で比較する
(本文だけでなく frontmatter 内の担当外 entry・コメント・空行も含めて byte 一致).
"""

from __future__ import annotations

import json
import re
import unicodedata
import unittest
from datetime import date, datetime
from enum import IntEnum

from ihealth.frontmatter import (
    FieldChange,
    FrontmatterError,
    render_key,
    render_scalar,
    split_frontmatter,
    upsert_frontmatter,
)


# ---------------------------------------------------------------------------
# 合成 fixture (実ノートの 4 形状)
# ---------------------------------------------------------------------------

#: テンプレート展開直後の形 (quoted key, 値は空, Meta Bind 用の空リスト).
TEMPLATE_SHAPE = (
    "---\n"
    "名前: 毎日の習慣 2030-01-02\n"
    "日付: 2030-01-02\n"
    "日記: false\n"
    "瞑想: []\n"
    "\"エクササイズ（水泳・ランニング）\": []\n"
    "\"歩数 (歩)\":\n"
    "\"睡眠時間 (分)\":\n"
    "昼寝時間:\n"
    "\"消費カロリー (kcal)\":\n"
    "運動強度スコア:\n"
    "気温:\n"
    "天気:\n"
    "アクティビティ詳細:\n"
    "参照URLs: \"[サンプル](https://example.com/a)、[別のサンプル](https://example.com/b)\"\n"
    "アウトプット:\n"
    "Github:\n"
    "---\n"
    "# 毎日の習慣 2030-01-02（水）\n"
    "\n"
    "## TODO\n"
    "- [ ] \n"
    "\n"
    "**瞑想**\n"
    "`INPUT[inlineListSuggester(option('a'), option('b')):瞑想]`\n"
)

#: Obsidian が再シリアライズした後の形 (plain key, block list, Meta Bind で入った値).
OBSIDIAN_SHAPE = (
    "---\n"
    "名前: 毎日の習慣 2030-01-03\n"
    "日付: 2030-01-03\n"
    "日記: true\n"
    "朝散歩:\n"
    "  - カーテンを開ける\n"
    "飲み物:\n"
    "  - 水500ml\n"
    "  - お茶 350ml ①\n"
    "歩数 (歩):\n"
    "睡眠時間 (分):\n"
    "昼寝時間:\n"
    "体重 (kg):\n"
    "気温:\n"
    "Github:\n"
    "---\n"
    "# 毎日の習慣 2030-01-03（木）\n"
    "\n"
    "- メモ: frontmatter 風の行 key: value も本文なら触らない\n"
    "---\n"
    "歩数 (歩): 本文中の偽 frontmatter\n"
)

#: 別ツール (screen-diary) が作る最小ノート + 本文末尾の日報節.
MINIMAL_SHAPE = (
    "---\n"
    "日付: 2030-01-04\n"
    "---\n"
    "\n"
    "# 🤖 2030-01-04の日報\n"
    "\n"
    "| 種類 | 時間 |\n"
    "|---|---|\n"
    "| 作成 | 10分 |\n"
)

#: Notion 移行ノートの形 (quoted key に値あり, 閉じ --- 直前に空行).
MIGRATED_SHAPE = (
    "---\n"
    "名前: \"毎日の習慣 2030-01-05 \"\n"
    "日付: 2030-01-05\n"
    "\"歩数 (歩)\": 1234\n"
    "\"睡眠時間 (分)\": 400\n"
    "運動強度スコア: 0\n"
    "気温: \"朝:平均1.0℃, 昼:平均2.0℃, 夜:平均3.0℃（最高:4.0℃, 最低:0.5℃）\"\n"
    "Github: \"[🔀 sample-repo:PR #1: synthetic](https://example.com/pull/1)\\n[📝 sample-repo:変更行数:+1-0 (1 commits)](https://example.com/commits/main)\"\n"
    "legacy-id: 00000000-0000-0000-0000-000000000000\n"
    "\n"
    "---\n"
    "![[参照メモ]]\n"
)

#: frontmatter の無いノート.
NO_FRONTMATTER = "# タイトルだけのノート\n\n本文。\n"


def _replace_once(text: str, old: str, new: str) -> str:
    """テスト用: ``old`` がちょうど 1 回だけ現れることを確認して置換する."""
    if text.count(old) != 1:
        raise AssertionError(f"fixture 内で {old!r} が {text.count(old)} 回出現")
    return text.replace(old, new, 1)


# ---------------------------------------------------------------------------
# 検証用の簡易 YAML ローダ (実装とは独立に書いた最小 subset)
# ---------------------------------------------------------------------------

def _mini_scalar(raw: str) -> object:
    raw = raw.strip()
    if raw == "" or raw in ("null", "~"):
        return None
    if raw == "[]":
        return []
    if raw.startswith('"'):
        return json.loads(raw)
    if raw.startswith("'"):
        return raw[1:-1].replace("''", "'")
    if re.fullmatch(r"[-+]?\d+", raw):
        return int(raw)
    if re.fullmatch(r"[-+]?\d+\.\d+", raw):
        return float(raw)
    if raw in ("true", "false"):
        return raw == "true"
    return raw


def _mini_yaml_load(text: str) -> "dict[str, object]":
    """``split_frontmatter`` を使わずに frontmatter を dict にする (テスト用 subset)."""
    body = text[1:] if text.startswith("\ufeff") else text
    lines = re.split(r"\r?\n", body)
    assert lines[0] == "---", lines[0]
    result: "dict[str, object]" = {}
    current = None
    for line in lines[1:]:
        if line in ("---", "..."):
            return result
        if not line.strip() or line.startswith("#"):
            continue
        if line.startswith("  - ") or line.startswith("- "):
            assert current is not None
            item = line.split("- ", 1)[1]
            existing = result[current]
            if not isinstance(existing, list):
                existing = []
            existing.append(_mini_scalar(item))
            result[current] = existing
            continue
        if line.startswith('"'):
            end = line.index('":')
            key = json.loads(line[:end + 1])
            value = line[end + 2:]
        elif line.startswith("'"):
            end = line.index("':")
            key = line[1:end].replace("''", "'")
            value = line[end + 2:]
        else:
            key, value = line.split(":", 1)
            key = key.strip()
        result[key] = _mini_scalar(value)
        current = key
    raise AssertionError("閉じ --- がありません")


# ---------------------------------------------------------------------------
# render_scalar / render_key
# ---------------------------------------------------------------------------

class TestRenderScalar(unittest.TestCase):
    def test_int(self):
        self.assertEqual(render_scalar(4009), "4009")
        self.assertEqual(render_scalar(0), "0")
        self.assertEqual(render_scalar(-3), "-3")

    def test_int_subclass_renders_number(self):
        class Level(IntEnum):
            HIGH = 3

        self.assertEqual(render_scalar(Level.HIGH), "3")

    def test_float_integer_value_uses_int_notation(self):
        self.assertEqual(render_scalar(66.0), "66")
        self.assertEqual(render_scalar(36.0), "36")
        self.assertEqual(render_scalar(-0.0), "0")

    def test_float_shortest_repr(self):
        self.assertEqual(render_scalar(1899.4), "1899.4")
        self.assertEqual(render_scalar(0.1 + 0.2), "0.30000000000000004")

    def test_float_exponent_becomes_positional(self):
        self.assertEqual(render_scalar(1e-05), "0.00001")
        self.assertEqual(render_scalar(1.5e-07), "0.00000015")
        self.assertEqual(render_scalar(1e20), "100000000000000000000")

    def test_float_non_finite_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                render_scalar(bad)

    def test_bool(self):
        self.assertEqual(render_scalar(True), "true")
        self.assertEqual(render_scalar(False), "false")

    def test_str_is_json_double_quoted(self):
        self.assertEqual(render_scalar("8.9時間"), '"8.9時間"')
        self.assertEqual(render_scalar('a"b\\c'), '"a\\"b\\\\c"')
        self.assertEqual(render_scalar(""), '""')

    def test_str_newline_is_escaped_single_line(self):
        rendered = render_scalar("line1\nline2\ttab")
        self.assertEqual(rendered, '"line1\\nline2\\ttab"')
        self.assertNotIn("\n", rendered)

    def test_str_yaml_line_separators_escaped(self):
        rendered = render_scalar("a\u2028b\u2029c\x85d\x7fe")
        self.assertEqual(rendered, '"a\\u2028b\\u2029c\\u0085d\\u007fe"')
        # JSON としても同じ文字列に戻る (YAML double-quoted と JSON の共通部分)
        self.assertEqual(json.loads(rendered), "a\u2028b\u2029c\x85d\x7fe")

    def test_str_emoji_kept_raw(self):
        self.assertEqual(render_scalar("快晴☀️☀️"), '"快晴☀️☀️"')

    def test_lone_surrogate_rejected(self):
        with self.assertRaises(ValueError):
            render_scalar("bad\ud800")

    def test_date(self):
        self.assertEqual(render_scalar(date(2030, 1, 2)), "2030-01-02")

    def test_unsupported_types(self):
        for bad in (None, [1], {"a": 1}, datetime(2030, 1, 2, 3, 4)):
            with self.assertRaises(TypeError):
                render_scalar(bad)


class TestRenderKey(unittest.TestCase):
    def test_safe_plain_keys(self):
        for key in ("歩数 (歩)", "体脂肪率 (%)", "Github", "タンパク質量(80-100g)", "C#", "3時"):
            self.assertEqual(render_key(key), key)

    def test_keys_that_need_quotes(self):
        cases = {
            "": '""',
            " lead": '" lead"',
            "trail ": '"trail "',
            "a: b": '"a: b"',
            "a #b": '"a #b"',
            "ends:": '"ends:"',
            "#tag": '"#tag"',
            "- item": '"- item"',
            "[x]": '"[x]"',
            "%p": '"%p"',
            "true": '"true"',
            "No": '"No"',
            "123": '"123"',
            "1.5": '"1.5"',
            "2030-01-02": '"2030-01-02"',
            "~": '"~"',
            "a\tb": '"a\\tb"',
            "a\nb": '"a\\nb"',
        }
        for key, expected in cases.items():
            self.assertEqual(render_key(key), expected, key)

    def test_non_str_rejected(self):
        with self.assertRaises(TypeError):
            render_key(1)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 実ノート形状ごとの upsert (ファイル全体比較)
# ---------------------------------------------------------------------------

class TestUpsertRealShapes(unittest.TestCase):
    def test_template_shape_fills_quoted_keys_in_place(self):
        updates = {
            "歩数 (歩)": 4321,
            "睡眠時間 (分)": 420,
            "昼寝時間": 15,
            "消費カロリー (kcal)": 1500.5,
            "運動強度スコア": 12.0,
            "気温": "朝:平均1.0℃, 昼:平均2.0℃, 夜:平均3.0℃（最高:4.0℃, 最低:0.5℃）",
            "天気": "3時: 快晴☀️☀️, 6時: 曇☁️",
            "アクティビティ詳細": "なし",
            "Github": "該当なし",
        }
        out, changes = upsert_frontmatter(TEMPLATE_SHAPE, updates)
        expected = TEMPLATE_SHAPE
        expected = _replace_once(expected, '"歩数 (歩)":\n', '"歩数 (歩)": 4321\n')
        expected = _replace_once(expected, '"睡眠時間 (分)":\n', '"睡眠時間 (分)": 420\n')
        expected = _replace_once(expected, "昼寝時間:\n", "昼寝時間: 15\n")
        expected = _replace_once(expected, '"消費カロリー (kcal)":\n', '"消費カロリー (kcal)": 1500.5\n')
        expected = _replace_once(expected, "運動強度スコア:\n", "運動強度スコア: 12\n")
        expected = _replace_once(
            expected, "気温:\n",
            '気温: "朝:平均1.0℃, 昼:平均2.0℃, 夜:平均3.0℃（最高:4.0℃, 最低:0.5℃）"\n',
        )
        expected = _replace_once(expected, "天気:\n", '天気: "3時: 快晴☀️☀️, 6時: 曇☁️"\n')
        expected = _replace_once(expected, "アクティビティ詳細:\n", 'アクティビティ詳細: "なし"\n')
        expected = _replace_once(expected, "Github:\n", 'Github: "該当なし"\n')
        self.assertEqual(out, expected)
        self.assertEqual([c.action for c in changes], ["update"] * len(updates))
        self.assertEqual([c.key for c in changes], list(updates))
        self.assertEqual(changes[0], FieldChange("歩数 (歩)", "update", "", "4321"))

    def test_obsidian_shape_plain_keys_and_lists_untouched(self):
        updates = {"歩数 (歩)": 812, "体重 (kg)": 60.0, "Github": "該当なし"}
        out, _ = upsert_frontmatter(OBSIDIAN_SHAPE, updates)
        expected = OBSIDIAN_SHAPE
        expected = _replace_once(expected, "歩数 (歩):\n睡眠", "歩数 (歩): 812\n睡眠")
        expected = _replace_once(expected, "体重 (kg):\n", "体重 (kg): 60\n")
        expected = _replace_once(expected, "Github:\n", 'Github: "該当なし"\n')
        self.assertEqual(out, expected)
        # 本文中の「歩数 (歩): …」行 (2 つ目の --- の後) は触らない
        self.assertTrue(out.endswith("---\n歩数 (歩): 本文中の偽 frontmatter\n"))

    def test_minimal_note_inserts_before_closing_in_update_order(self):
        updates = {"歩数 (歩)": 100, "睡眠時間 (分)": 360, "天気": "12時: 雨☔", "Github": "該当なし"}
        out, changes = upsert_frontmatter(MINIMAL_SHAPE, updates)
        expected = _replace_once(
            MINIMAL_SHAPE,
            "日付: 2030-01-04\n---\n",
            "日付: 2030-01-04\n"
            "歩数 (歩): 100\n"
            "睡眠時間 (分): 360\n"
            "天気: \"12時: 雨☔\"\n"
            "Github: \"該当なし\"\n"
            "---\n",
        )
        self.assertEqual(out, expected)
        self.assertEqual([c.action for c in changes], ["insert"] * 4)
        self.assertTrue(all(c.old is None for c in changes))

    def test_migrated_shape_updates_existing_and_inserts_missing(self):
        updates = {
            "歩数 (歩)": 1234,          # 同値 → unchanged
            "睡眠時間 (分)": 410,        # 更新
            "昼寝時間": 20,              # 不在 → 閉じ --- の直前 (空行の後) に追記
            "Github": "該当なし",        # 改行入りの既存値を置換
        }
        out, changes = upsert_frontmatter(MIGRATED_SHAPE, updates)
        expected = MIGRATED_SHAPE
        expected = _replace_once(expected, '"睡眠時間 (分)": 400\n', '"睡眠時間 (分)": 410\n')
        github_line = [l for l in MIGRATED_SHAPE.split("\n") if l.startswith("Github:")][0]
        expected = _replace_once(expected, github_line + "\n", 'Github: "該当なし"\n')
        expected = _replace_once(expected, "\n\n---\n", "\n\n昼寝時間: 20\n---\n")
        self.assertEqual(out, expected)
        self.assertEqual(
            [c.action for c in changes], ["unchanged", "update", "insert", "update"],
        )

    def test_no_frontmatter_creates_block_at_top(self):
        out, changes = upsert_frontmatter(NO_FRONTMATTER, {"歩数 (歩)": 5, "Github": "x"})
        self.assertEqual(out, '---\n歩数 (歩): 5\nGithub: "x"\n---\n' + NO_FRONTMATTER)
        self.assertEqual([c.action for c in changes], ["insert", "insert"])

    def test_no_frontmatter_without_create_raises(self):
        with self.assertRaises(FrontmatterError):
            upsert_frontmatter(NO_FRONTMATTER, {"a": 1}, create_if_missing=False)

    def test_no_frontmatter_and_nothing_to_write_is_noop(self):
        out, changes = upsert_frontmatter(NO_FRONTMATTER, {"a": None})
        self.assertEqual(out, NO_FRONTMATTER)
        self.assertEqual(changes, [])

    def test_body_is_byte_identical_for_all_shapes(self):
        updates = {"歩数 (歩)": 1, "気温": "x", "Github": "y"}
        for original in (TEMPLATE_SHAPE, OBSIDIAN_SHAPE, MINIMAL_SHAPE, MIGRATED_SHAPE):
            out, _ = upsert_frontmatter(original, updates)
            before = split_frontmatter(original)
            after = split_frontmatter(out)
            assert before is not None and after is not None
            self.assertEqual(after[1], before[1])
            self.assertEqual(after[1].encode("utf-8"), before[1].encode("utf-8"))

    def test_reparse_yields_written_values(self):
        updates = {
            "歩数 (歩)": 4321,
            "消費カロリー (kcal)": 1500.5,
            "天気": '3時: 快晴☀️☀️, "quoted"',
            "Github": "[a](https://example.com/1)\n[b](https://example.com/2)",
        }
        for original in (TEMPLATE_SHAPE, OBSIDIAN_SHAPE, MINIMAL_SHAPE, MIGRATED_SHAPE, NO_FRONTMATTER):
            out, _ = upsert_frontmatter(original, updates)
            loaded = _mini_yaml_load(out)
            for key, value in updates.items():
                self.assertEqual(loaded[key], value, (key, original[:20]))
            if original is OBSIDIAN_SHAPE:
                self.assertEqual(loaded["飲み物"], ["水500ml", "お茶 350ml ①"])

    def test_idempotent_second_run_is_noop(self):
        updates = {
            "歩数 (歩)": 4321, "睡眠時間 (分)": 420, "体重 (kg)": 60.5,
            "天気": "a\nb", "Github": "該当なし", "新しいキー": 7,
        }
        for original in (TEMPLATE_SHAPE, OBSIDIAN_SHAPE, MINIMAL_SHAPE, MIGRATED_SHAPE, NO_FRONTMATTER):
            first, _ = upsert_frontmatter(original, updates)
            second, changes = upsert_frontmatter(first, updates)
            self.assertEqual(second, first)
            self.assertEqual({c.action for c in changes}, {"unchanged"})


# ---------------------------------------------------------------------------
# 個別ケース
# ---------------------------------------------------------------------------

class TestUpsertEdgeCases(unittest.TestCase):
    def test_none_values_are_ignored(self):
        out, changes = upsert_frontmatter(MINIMAL_SHAPE, {"歩数 (歩)": None, "Github": None})
        self.assertEqual(out, MINIMAL_SHAPE)
        self.assertEqual(changes, [])

    def test_single_quoted_key(self):
        text = "---\n'it''s': old\nb: 1\n---\n"
        out, changes = upsert_frontmatter(text, {"it's": "new"})
        self.assertEqual(out, "---\n'it''s': \"new\"\nb: 1\n---\n")
        self.assertEqual(changes[0].old, "old")

    def test_double_quoted_key_with_escapes(self):
        text = '---\n"tab\\tkey": 1\n"\\u6b69\\u6570": 2\n---\nbody\n'
        out, _ = upsert_frontmatter(text, {"tab\tkey": 10, "歩数": 20})
        self.assertEqual(out, '---\n"tab\\tkey": 10\n"\\u6b69\\u6570": 20\n---\nbody\n')

    def test_key_spacing_before_colon_preserved(self):
        text = "---\nkey : 1\n---\n"
        out, _ = upsert_frontmatter(text, {"key": 2})
        self.assertEqual(out, "---\nkey : 2\n---\n")

    def test_crlf_document(self):
        text = "---\r\n日付: 2030-01-02\r\n歩数 (歩):\r\n---\r\n本文\r\n"
        out, _ = upsert_frontmatter(text, {"歩数 (歩)": 3, "Github": "x"})
        self.assertEqual(
            out, '---\r\n日付: 2030-01-02\r\n歩数 (歩): 3\r\nGithub: "x"\r\n---\r\n本文\r\n',
        )

    def test_mixed_newlines_follow_first_newline(self):
        # 先頭が LF → 挿入/置換行も LF. 他の行の CRLF はそのまま.
        text = "---\na: 1\r\nb:\r\n---\r\nbody\r\n"
        out, _ = upsert_frontmatter(text, {"b": 2, "c": 3})
        self.assertEqual(out, "---\na: 1\r\nb: 2\nc: 3\n---\r\nbody\r\n")
        # 先頭が CRLF → CRLF.
        text2 = "---\r\na: 1\nb:\n---\nbody\n"
        out2, _ = upsert_frontmatter(text2, {"b": 2, "c": 3})
        self.assertEqual(out2, "---\r\na: 1\nb: 2\r\nc: 3\r\n---\nbody\n")

    def test_bom_preserved(self):
        text = "\ufeff---\na:\n---\nbody"
        out, _ = upsert_frontmatter(text, {"a": 1})
        self.assertEqual(out, "\ufeff---\na: 1\n---\nbody")
        out2, _ = upsert_frontmatter("\ufeffbody\n", {"a": 1})
        self.assertEqual(out2, "\ufeff---\na: 1\n---\nbody\n")

    def test_no_trailing_newline_preserved(self):
        text = "---\na: 1\n---\nlast line without newline"
        out, _ = upsert_frontmatter(text, {"a": 2, "b": 3})
        self.assertEqual(out, "---\na: 2\nb: 3\n---\nlast line without newline")

    def test_closing_delimiter_without_newline(self):
        text = "---\na: 1\n---"
        out, _ = upsert_frontmatter(text, {"b": 2})
        self.assertEqual(out, "---\na: 1\nb: 2\n---")

    def test_empty_frontmatter(self):
        out, _ = upsert_frontmatter("---\n---\nbody\n", {"a": 1})
        self.assertEqual(out, "---\na: 1\n---\nbody\n")

    def test_dots_closing_delimiter(self):
        out, _ = upsert_frontmatter("---\na:\n...\nbody\n", {"a": 1, "b": 2})
        self.assertEqual(out, "---\na: 1\nb: 2\n...\nbody\n")

    def test_empty_file(self):
        out, changes = upsert_frontmatter("", {"a": 1})
        self.assertEqual(out, "---\na: 1\n---\n")
        self.assertEqual(changes[0].action, "insert")
        out2, changes2 = upsert_frontmatter("", {})
        self.assertEqual(out2, "")
        self.assertEqual(changes2, [])

    def test_block_list_value_replaced_entirely(self):
        text = "---\ntags:\n  - a\n  - b\nnext: 1\n---\n"
        out, changes = upsert_frontmatter(text, {"tags": "x"})
        self.assertEqual(out, '---\ntags: "x"\nnext: 1\n---\n')
        self.assertEqual(changes[0].old, "\n  - a\n  - b")

    def test_compact_block_sequence_at_column_zero(self):
        text = "---\ntags:\n- a\n- b\nnext: 1\n---\n"
        out, _ = upsert_frontmatter(text, {"tags": "x"})
        self.assertEqual(out, '---\ntags: "x"\nnext: 1\n---\n')

    def test_block_value_with_interleaved_comment_and_blank(self):
        text = (
            "---\n"
            "tags:\n"
            "  - a\n"
            "# コメント\n"
            "\n"
            "  - b\n"
            "# 末尾コメントは entry の外\n"
            "\n"
            "next: 1\n"
            "---\n"
        )
        out, _ = upsert_frontmatter(text, {"tags": "x"})
        self.assertEqual(
            out,
            "---\n"
            'tags: "x"\n'
            "# 末尾コメントは entry の外\n"
            "\n"
            "next: 1\n"
            "---\n",
        )

    def test_block_scalar_and_multiline_continuation(self):
        text = (
            "---\n"
            "memo: |\n"
            "  line1\n"
            "\n"
            "  line3\n"
            "plain: first\n"
            "  continued\n"
            "other: keep\n"
            "---\n"
        )
        out, changes = upsert_frontmatter(text, {"memo": "m", "plain": "p"})
        self.assertEqual(out, '---\nmemo: "m"\nplain: "p"\nother: keep\n---\n')
        self.assertEqual(changes[0].old, "|\n  line1\n\n  line3")

    def test_comments_and_blank_lines_outside_entries_preserved(self):
        text = "---\n# 先頭コメント\na: 1  # 行末コメント\n\n# 間のコメント\nb: 2\n---\n"
        out, changes = upsert_frontmatter(text, {"b": 3, "c": 4})
        self.assertEqual(
            out, "---\n# 先頭コメント\na: 1  # 行末コメント\n\n# 間のコメント\nb: 3\nc: 4\n---\n",
        )
        # 行末コメント付きでも値が同じなら触らない
        out2, changes2 = upsert_frontmatter(text, {"a": 1})
        self.assertEqual(out2, text)
        self.assertEqual(changes2[0].action, "unchanged")

    def test_duplicate_keys_only_first_updated_with_warning(self):
        text = "---\na: 1\nb: 2\na: 3\n---\n"
        out, changes = upsert_frontmatter(text, {"a": 9})
        self.assertEqual(out, "---\na: 9\nb: 2\na: 3\n---\n")
        self.assertEqual(changes[0].action, "update")
        self.assertIsNotNone(changes[0].warning)
        self.assertIn("2 個", changes[0].warning)

    def test_unicode_values_and_keys(self):
        text = "---\n天気:\n---\n"
        value = "3時: 快晴☀️☀️, 6時: 薄曇⛅️, 9時: 雪❄️ — 𠮷"
        out, _ = upsert_frontmatter(text, {"天気": value, "絵文字🔥キー": "🔥"})
        self.assertEqual(out, f'---\n天気: "{value}"\n絵文字🔥キー: "🔥"\n---\n')
        self.assertEqual(_mini_yaml_load(out)["天気"], value)

    def test_string_with_newlines_stays_on_one_line(self):
        text = "---\nGithub:\n---\n"
        value = "[🎫 repo:Issue #1: a](https://example.com/1)\n[📝 repo:変更行数:+1-2 (3 commits)](https://example.com/c)"
        out, _ = upsert_frontmatter(text, {"Github": value})
        fm_lines = out.split("\n")
        self.assertEqual(fm_lines[0], "---")
        self.assertTrue(fm_lines[1].startswith('Github: "[🎫 repo:Issue #1: a]'))
        self.assertIn("\\n[📝", fm_lines[1])
        self.assertEqual(fm_lines[2], "---")
        self.assertEqual(_mini_yaml_load(out)["Github"], value)

    def test_nfc_nfd_keys_match(self):
        nfd_key = unicodedata.normalize("NFD", "アクティビティ時間 (分)")
        self.assertNotEqual(nfd_key, "アクティビティ時間 (分)")
        text = f"---\n{nfd_key}:\n---\n"
        out, changes = upsert_frontmatter(text, {"アクティビティ時間 (分)": 30})
        # 既存 (NFD) の key 表記を保ったまま値だけ入る (重複追記しない)
        self.assertEqual(out, f"---\n{nfd_key}: 30\n---\n")
        self.assertEqual(changes[0].action, "update")

    def test_semantic_equality_keeps_existing_representation(self):
        text = "---\n体重 (kg): 66.0\n日照時間: 8.9時間\n歩数 (歩): '100'\n---\n"
        out, changes = upsert_frontmatter(
            text, {"体重 (kg)": 66, "日照時間": "8.9時間", "歩数 (歩)": 100},
        )
        # 66.0 == 66 / plain 8.9時間 == "8.9時間" は同値. '100' (文字列) と 100 (数値) は別物.
        self.assertEqual(out, "---\n体重 (kg): 66.0\n日照時間: 8.9時間\n歩数 (歩): 100\n---\n")
        self.assertEqual([c.action for c in changes], ["unchanged", "unchanged", "update"])

    def test_plain_value_that_is_not_valid_yaml_gets_rewritten(self):
        text = "---\n天気: 3時: 晴れ\n---\n"
        out, changes = upsert_frontmatter(text, {"天気": "3時: 晴れ"})
        self.assertEqual(out, '---\n天気: "3時: 晴れ"\n---\n')
        self.assertEqual(changes[0].action, "update")


class TestFillOnlyEmpty(unittest.TestCase):
    def test_empty_variants_are_filled(self):
        for raw in ("", " ", " null", " ~", ' ""', " ''", " []", " {}", " [ ]", " # comment"):
            text = f"---\na:{raw}\n---\n"
            out, changes = upsert_frontmatter(text, {"a": 5}, fill_only_empty=True)
            self.assertEqual(out, "---\na: 5\n---\n", repr(raw))
            self.assertEqual(changes[0].action, "update")

    def test_non_empty_values_are_kept(self):
        for raw in (" 1", ' "x"', " -1", " false", " [a]"):
            text = f"---\na:{raw}\n---\n"
            out, changes = upsert_frontmatter(text, {"a": 5}, fill_only_empty=True)
            self.assertEqual(out, text, repr(raw))
            self.assertEqual(changes[0].action, "kept")
            self.assertEqual(changes[0].new, "5")
            self.assertEqual(changes[0].old, raw.strip())

    def test_list_with_items_is_kept(self):
        text = "---\na:\n  - x\n---\n"
        out, changes = upsert_frontmatter(text, {"a": 5}, fill_only_empty=True)
        self.assertEqual(out, text)
        self.assertEqual(changes[0].action, "kept")

    def test_equal_value_reports_unchanged_not_kept(self):
        text = "---\na: 5\n---\n"
        _, changes = upsert_frontmatter(text, {"a": 5}, fill_only_empty=True)
        self.assertEqual(changes[0].action, "unchanged")

    def test_missing_keys_are_still_inserted(self):
        text = "---\na: 1\n---\n"
        out, changes = upsert_frontmatter(text, {"a": 2, "b": 3}, fill_only_empty=True)
        self.assertEqual(out, "---\na: 1\nb: 3\n---\n")
        self.assertEqual([c.action for c in changes], ["kept", "insert"])

    def test_migrated_note_backfill_keeps_existing(self):
        updates = {"歩数 (歩)": 9999, "睡眠時間 (分)": 1, "昼寝時間": 20}
        out, changes = upsert_frontmatter(MIGRATED_SHAPE, updates, fill_only_empty=True)
        expected = _replace_once(MIGRATED_SHAPE, "\n\n---\n", "\n\n昼寝時間: 20\n---\n")
        self.assertEqual(out, expected)
        self.assertEqual([c.action for c in changes], ["kept", "kept", "insert"])


class TestBrokenFrontmatter(unittest.TestCase):
    def test_unclosed_frontmatter_raises_and_does_not_append(self):
        for text in ("---\na: 1\nbody without closing\n", "---\n", "---"):
            with self.assertRaises(FrontmatterError):
                upsert_frontmatter(text, {"a": 2})

    def test_nothing_to_write_never_raises_even_if_broken(self):
        broken = "---\na: 1\nno closing line\n"
        self.assertEqual(upsert_frontmatter(broken, {}), (broken, []))
        self.assertEqual(upsert_frontmatter(broken, {"a": None}), (broken, []))

    def test_unparseable_top_level_line_raises(self):
        for text in (
            "---\njust text\n---\n",            # body の水平線と誤認したケース
            "---\n{a: 1}\n---\n",               # flow mapping
            "---\n? complex\n: value\n---\n",   # complex key
            "---\n  indented: 1\n---\n",        # key より前のインデント行
            "---\n- item\n---\n",               # top-level sequence
            "---\nkey #comment: 1\n---\n",
        ):
            with self.assertRaises(FrontmatterError, msg=repr(text)):
                upsert_frontmatter(text, {"a": 1})

    def test_invalid_value_raises_before_any_change(self):
        with self.assertRaises(ValueError):
            upsert_frontmatter(TEMPLATE_SHAPE, {"歩数 (歩)": 1, "気温": float("nan")})
        with self.assertRaises(TypeError):
            upsert_frontmatter(TEMPLATE_SHAPE, {"歩数 (歩)": [1, 2]})

    def test_duplicate_normalized_update_keys_rejected(self):
        nfd = unicodedata.normalize("NFD", "ビ")
        with self.assertRaises(ValueError):
            upsert_frontmatter("---\n---\n", {"ビ": 1, nfd: 2})

    def test_empty_update_key_rejected(self):
        with self.assertRaises(ValueError):
            upsert_frontmatter("---\n---\n", {"": 1})


class TestSplitFrontmatter(unittest.TestCase):
    def test_split_roundtrip(self):
        for text in (TEMPLATE_SHAPE, OBSIDIAN_SHAPE, MINIMAL_SHAPE, MIGRATED_SHAPE, "\ufeff---\na: 1\n---\nx"):
            parts = split_frontmatter(text)
            assert parts is not None
            self.assertEqual(parts[0] + parts[1], text)
            self.assertTrue(parts[0].rstrip("\n").endswith("---"))

    def test_no_frontmatter(self):
        self.assertIsNone(split_frontmatter(NO_FRONTMATTER))
        self.assertIsNone(split_frontmatter(""))

    def test_unclosed(self):
        with self.assertRaises(FrontmatterError):
            split_frontmatter("---\na: 1\n")


if __name__ == "__main__":
    unittest.main()
