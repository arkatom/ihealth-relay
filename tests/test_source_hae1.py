"""source.decompress_hae の HAE1 コンテナ対応のテスト。

AutoSync は追記型の .hae を ``b"HAE1" + [uint32 BE 長さ + LZFSE stream]*`` で書くことがある
(各 stream = 1 時間ぶんの ``{"data": [...], "metric": ..., "date": ...}``)。
大半のテストは compression_tool をモックし、stdin をそのまま stdout に返す
(= 「解凍済み」とみなす)。macOS で ``/usr/bin/compression_tool`` がある環境では
実ツールで LZFSE を往復させるテストも走る (値はすべて合成)。
"""

from __future__ import annotations

import json
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ihealth.source import SourceError, decompress_hae

_FAKE_COMPRESSION_TOOL = Path("/usr/bin/env")
_REAL_COMPRESSION_TOOL = Path("/usr/bin/compression_tool")


class _Completed:
    def __init__(self, stdout: bytes, returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = b"" if returncode == 0 else b"decode failed"
        self.returncode = returncode


def _identity_decode(cmd, input=None, capture_output=None, timeout=None):  # noqa: A002
    assert "-i" not in cmd and "-o" not in cmd, "HAE1 は segment を stdin で渡す"
    return _Completed(input)


def _container(*segments: bytes) -> bytes:
    body = b"HAE1"
    for segment in segments:
        body += struct.pack(">I", len(segment)) + segment
    return body


def _hae1(*documents: dict) -> bytes:
    return _container(*(json.dumps(document).encode("utf-8") for document in documents))


class TestDecompressHae1(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._tool = patch("ihealth.source.COMPRESSION_TOOL", _FAKE_COMPRESSION_TOOL)
        self._tool.start()

    def tearDown(self) -> None:
        self._tool.stop()
        self._tmp.cleanup()

    def _leftovers(self) -> "list[str]":
        return sorted(p.name for p in self.dir.rglob("*") if p.suffix in (".json", ".tmp"))

    def test_segments_are_merged(self):
        src = self.dir / "20300115.hae"
        src.write_bytes(_hae1(
            {"data": [], "metric": "Step Count", "date": 1000},
            {"data": [{"qty": 10, "unit": "count", "start": 1, "end": 2}],
             "metric": "Step Count", "date": 4600},
            {"data": [{"qty": 5, "unit": "count", "start": 3, "end": 4}],
             "metric": "Step Count", "date": 8200},
        ))
        dst = self.dir / "out" / "step_count.json"
        with patch("ihealth.source.subprocess.run", side_effect=_identity_decode) as run:
            decompress_hae(src, dst)
        self.assertEqual(run.call_count, 3)
        payload = json.loads(dst.read_text(encoding="utf-8"))
        self.assertEqual(payload["metric"], "Step Count")
        self.assertEqual([r["qty"] for r in payload["data"]], [10, 5])
        self.assertFalse(dst.with_name(dst.name + ".tmp").exists())

    def test_segment_without_data_key_is_skipped(self):
        src = self.dir / "20300115.hae"
        src.write_bytes(_hae1(
            {"metric": "Step Count", "date": 1000},
            {"data": [{"qty": 7}], "metric": "Step Count", "date": 4600},
        ))
        dst = self.dir / "out.json"
        with patch("ihealth.source.subprocess.run", side_effect=_identity_decode):
            decompress_hae(src, dst)
        payload = json.loads(dst.read_text(encoding="utf-8"))
        self.assertEqual(payload["data"], [{"qty": 7}])

    def test_data_not_a_list_raises_and_writes_nothing(self):
        # 黙って捨てると過少集計になるので失敗にする
        src = self.dir / "20300115.hae"
        src.write_bytes(_hae1(
            {"data": [{"qty": 1}], "metric": "x"},
            {"data": {"qty": 2}, "metric": "x"},
        ))
        dst = self.dir / "out.json"
        with patch("ihealth.source.subprocess.run", side_effect=_identity_decode):
            with self.assertRaises(SourceError) as ctx:
                decompress_hae(src, dst)
        self.assertIn("配列", str(ctx.exception))
        self.assertEqual(self._leftovers(), [])

    def test_truncated_container_raises(self):
        src = self.dir / "20300115.hae"
        src.write_bytes(_hae1({"data": [], "metric": "x"})[:-3])
        with patch("ihealth.source.subprocess.run", side_effect=_identity_decode):
            with self.assertRaises(SourceError):
                decompress_hae(src, self.dir / "out.json")
        self.assertEqual(self._leftovers(), [])

    def test_truncated_length_header_raises(self):
        src = self.dir / "20300115.hae"
        src.write_bytes(_hae1({"data": [], "metric": "x"}) + b"\x00\x00")
        with patch("ihealth.source.subprocess.run", side_effect=_identity_decode):
            with self.assertRaises(SourceError) as ctx:
                decompress_hae(src, self.dir / "out.json")
        self.assertIn("segment 長", str(ctx.exception))

    def test_empty_container_raises(self):
        src = self.dir / "20300115.hae"
        src.write_bytes(b"HAE1")
        with patch("ihealth.source.subprocess.run", side_effect=_identity_decode) as run:
            with self.assertRaises(SourceError):
                decompress_hae(src, self.dir / "out.json")
        run.assert_not_called()

    def test_segment_decode_failure_raises(self):
        src = self.dir / "20300115.hae"
        src.write_bytes(_hae1({"data": [], "metric": "x"}))
        with patch("ihealth.source.subprocess.run",
                   side_effect=lambda *a, **k: _Completed(b"", returncode=1)):
            with self.assertRaises(SourceError):
                decompress_hae(src, self.dir / "out.json")
        self.assertEqual(self._leftovers(), [])

    def test_segment_not_json_raises(self):
        src = self.dir / "20300115.hae"
        src.write_bytes(_container(b"not json"))
        with patch("ihealth.source.subprocess.run", side_effect=_identity_decode):
            with self.assertRaises(SourceError):
                decompress_hae(src, self.dir / "out.json")

    def test_decoded_size_limit_raises(self):
        src = self.dir / "20300115.hae"
        src.write_bytes(_hae1({"data": [{"qty": 1}] * 50, "metric": "x"}))
        with patch("ihealth.source._MAX_DECODED_SIZE_BYTES", 64), \
             patch("ihealth.source.subprocess.run", side_effect=_identity_decode):
            with self.assertRaises(SourceError):
                decompress_hae(src, self.dir / "out.json")

    def test_write_failure_raises_source_error_and_cleans_tmp(self):
        src = self.dir / "20300115.hae"
        src.write_bytes(_hae1({"data": [{"qty": 1}], "metric": "x"}))
        dst = self.dir / "out.json"
        with patch("ihealth.source.subprocess.run", side_effect=_identity_decode), \
             patch("ihealth.source.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(SourceError) as ctx:
                decompress_hae(src, dst)
        self.assertIn("disk full", str(ctx.exception))
        self.assertEqual(self._leftovers(), [])

    def test_plain_lzfse_still_uses_file_arguments(self):
        # HAE1 でない (従来の単一 LZFSE stream) .hae は従来どおり -i / -o で解凍する
        src = self.dir / "20300115.hae"
        src.write_bytes(b"bvx2" + b"\x00" * 16)
        dst = self.dir / "out.json"

        def _file_decode(cmd, capture_output=None, timeout=None):
            self.assertIn("-i", cmd)
            self.assertIn("-o", cmd)
            Path(cmd[cmd.index("-o") + 1]).write_text('{"data": []}', encoding="utf-8")
            return _Completed(b"")

        with patch("ihealth.source.subprocess.run", side_effect=_file_decode) as run:
            decompress_hae(src, dst)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(json.loads(dst.read_text(encoding="utf-8")), {"data": []})


@unittest.skipUnless(_REAL_COMPRESSION_TOOL.exists(), "macOS の compression_tool が必要")
class TestDecompressHae1RealTool(unittest.TestCase):
    """実ツールで LZFSE segment を往復させる (stdin / stdout 経由の解凍が通ることの確認)。"""

    def _encode(self, document: dict) -> bytes:
        result = subprocess.run(
            [str(_REAL_COMPRESSION_TOOL), "-encode", "-a", "lzfse"],
            input=json.dumps(document).encode("utf-8"),
            capture_output=True,
            timeout=30,
            check=True,
        )
        return result.stdout

    def test_real_lzfse_segments_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "20300115.hae"
            src.write_bytes(_container(
                self._encode({"data": [{"qty": 3, "unit": "count"}], "metric": "Step Count"}),
                self._encode({"data": [{"qty": 4, "unit": "count"}], "metric": "Step Count"}),
            ))
            dst = Path(tmp) / "step_count.json"
            decompress_hae(src, dst)
            payload = json.loads(dst.read_text(encoding="utf-8"))
        self.assertEqual(payload["metric"], "Step Count")
        self.assertEqual([r["qty"] for r in payload["data"]], [3, 4])


if __name__ == "__main__":
    unittest.main()
