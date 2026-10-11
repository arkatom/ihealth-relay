# tests/fixtures/

テストが読み込む fixture の置き場。4 種類あり、どれもテスト以外 (実 iCloud /
実 vault / ネットワーク) には触れずに使える。

| ディレクトリ | 中身 | 作り方 | 主に使うテスト |
| --- | --- | --- | --- |
| `sample_health_export/` | AutoSync `.hae` を解凍した後のメトリクス別 JSON (`YYYY-MM-DD/{metric}.json`) | 実データを間引いて anonymize (下記) | `test_parser.py` ほか単日パイプライン |
| `automation/` | Health Auto Export **オートメーション** の日次 JSON (`HealthAutoExport-YYYY-MM-DD.json`) | **完全な合成** (手書き) | `test_automation_parser.py`, `test_health_loader.py` |
| `jma/` | 気象庁「1時間ごとの値」(`hourly_s1.php`) ページの HTML | **完全な合成** (手書き) | `test_weather.py` |
| `e2e/` | 日記パイプライン一気通貫テストの入力一式: 合成の HAE オートメーション JSON、気象庁 `hourly_s1` の HTML、GitHub GraphQL の応答、日記ノートと Templater テンプレート。条件ごとの狙いと期待値の手計算は [`e2e/README.md`](e2e/README.md) | **完全な合成** (手書き) | `test_e2e_diary.py` |

`automation/`・`jma/`・`e2e/` は Obsidian 日記対応 (v0.2.0) で追加した。実在の健康値・
日記本文・観測値・GitHub アカウントは含まない。fixture を変えたら、期待値を手計算で
書いているテスト側も合わせて更新すること。

## automation/ (オートメーション JSON, 合成)

Health Auto Export のオートメーション (iCloud Drive / JSON / ExportVersion.v2) の
出力を模した完全な合成データ。日付はわざと 2030 年、ソース名は `Sample Watch`
などの架空名で、キー名・日時書式 (`"YYYY-MM-DD HH:MM:SS +0900"`)・単位表記だけを
実データの構造に合わせている。各ファイルの狙いは
[`automation/README.md`](automation/README.md) に表でまとめてある。概要:

| ファイル | 想定する設定 / 検証したいこと |
| --- | --- |
| `aggregated/HealthAutoExport-2030-01-15.json` | 現役設定 (`aggregateData=true` / `aggregateSleep=true`)。11 メトリクスに加え、BMI (`body_mass_index`)・手首皮膚温・呼吸数などの無関係な metric を混ぜ、取り違えないことを確かめる |
| `segments/HealthAutoExport-2030-02-01.json` + `2030-02-02.json` | 非集計の睡眠 segment 形式。前日ファイルとの結合、日付をまたぐ segment の重複除去、夜主睡眠 / 昼寝の振り分け |
| `workouts/HealthAutoExport-2030-03-10.json` | `data.workouts` (5 件) から `アクティビティ詳細` を組む (英 → 日の名前変換、未知名、duration / end − start) |

## jma/ (気象庁ページ, 合成)

気象庁 hourly_s1 ページの構造 (見出し 2 段 + rowspan / colspan、観測表
`table#tablefix1.data2_s`、その外の「前日 / 翌日」アイコンや記事表) を模した
架空の値の HTML。

| ファイル | 内容 / 検証したいこと |
| --- | --- |
| `hourly_synthetic.html` | 見出しは「東京」の 2031-02-14 で、値はすべて架空。1〜24 時の全行がある。海面気圧と現地気圧の取り違え、`--` (現象なし → 0)、夜間の日照の空欄、集計に使わない列 (降雪 / 積雪) の `×`、天気欄の alt 7 種 (快晴 / 晴れ / 薄曇 / 曇 / 雨 / 雪 / 雷電 → 絵文字の付け分け)、観測表の外の画像・表を拾わないこと、見出しの日付。6 つのサマリ文字列を完全一致で確かめる |
| `hourly_unavailable.html` | 当日・未公開日のページ (観測表が無く「閲覧可能な日は、昨日…までです」の案内だけ)。`status == "nodata"` (公開待ち) になること |

品質記号 (`)` 準正常値 / `]` 資料不足値 / `#` 疑問値) や気圧マークの境界 (5.0 hPa
ちょうど)、24 時の欠測などの細かいケースは、`tests/test_weather.py` の中で HTML を
組み立てて検証している (fixture ファイルは増やしていない)。

## sample_health_export/ (AutoSync, 間引き + anonymize)

テスト用の間引き **合成サンプルデータ** (Health Auto Export の出力構造に
合わせた anonymized fixtures)。`scripts/make_fixtures.py` で
`data/health/YYYY-MM-DD/*.json` (.gitignore 対象) を読み取り、
`data` 配列を代表的な件数に切り詰めたコピーを保存しているが、コミット
時には UUID / source identifier / source name を全て anonymize 済み:

- HealthKit デバイス UUID → `00000000-0000-0000-0000-000000000000`
- アプリ identifier → `com.example.{scale,sleeptracker,mindfulness}` 等
- デバイス name → `Sample Watch` / `Sample Scale` 等

数値 (歩数 / 心拍 / 体重 / 体脂肪率 / 睡眠) と秒精度の timestamp は
parser 集計ルールの回帰検証に必要なため意味のある値を保持しているので、
完全に de-identified された合成データではなく **「device fingerprint と
アプリ identifier だけ匿名化したサンプル」** という位置付け.

**Privacy 注意 (履歴):** 現在の作業ツリーは anonymize 済みだが、リポジトリ
の `git log` を遡ると anonymize 以前の commit (initial fixture 投入時) に
**実 device UUID やアプリ identifier が残存**している。これは将来的に
`git filter-repo` で削除する選択肢を残しているが、v0.1.0 リリース時点では
著者判断で keep されている。この点を厳密に避けたい fork は、作業ツリー
だけでなく history も rewrite して再公開してください.

**再生成時の注意:** `scripts/make_fixtures.py` は既定で `--anonymize` ON
で動き、commit 対象の fixture は常に anonymize 済みになる。`--no-anonymize`
は debug 専用で、生成された fixture を git commit してはならない.

### ディレクトリ構成

```
sample_health_export/
├── 2026-04-22/   # フル populate (健康 15 フィールドのうち 13 フィールドに値、瞑想の 2 フィールドのみなし)
└── 2026-04-23/   # 部分欠落 (昼寝・体重・体脂肪なし、瞑想あり)
```

### 採用日付の理由

| 日付 | なぜ fixture に選んだか |
| --- | --- |
| 2026-04-22 | 健康 15 フィールドのうち **13** フィールドが populated (瞑想の `mindful_sessions` / `mindful_minutes` のみなし)。`sleep_hours` (夜主睡眠) と `nap_hours` (昼寝) の両方が同時に取れる構成で、sleep_analysis の JST end.hour 分岐を検証できる。`body_mass_kg` / `body_fat_percentage` 値も含まれる。 |
| 2026-04-23 | 部分欠落ケースのカバレッジ。昼寝なし (`nap_hours=None`)・体重計測なし (`body_mass_kg=None`) ・瞑想あり (`mindful_minutes`/`mindful_sessions` not None)。「データが無い日」の parser 挙動 (None フィールド) を検証する。 |

### 再生成方法

```bash
# fixture を最新のアーカイブから再生成 (既定 mode=evenly, limit=100)
# 事前に data/health/YYYY-MM-DD/ が埋まっている必要あり (python3 -m ihealth で生成)
/usr/bin/python3 scripts/make_fixtures.py 2026-04-22 2026-04-23

# 小さいサイズで再生成したい場合
/usr/bin/python3 scripts/make_fixtures.py 2026-04-22 2026-04-23 --limit 50

# head モード (先頭 N 件) を明示する場合
/usr/bin/python3 scripts/make_fixtures.py 2026-04-22 --mode head

# 実 device fingerprint を含む生 fixture (debug 専用、コミット禁止)
/usr/bin/python3 scripts/make_fixtures.py 2026-04-22 --no-anonymize
```

`--no-anonymize` 指定なしでは常に anonymize が走り、commit 対象の fixture
は安全になる. `--no-anonymize` で生成した fixture を git に push してはいけない.

再生成は **dst_dir の *.json を一旦全削除してから** 書き直すので、
入力側から消えたファイルの stale 残留は起こらない (README 等の周辺ファイルは保持)。

src_dir が存在しない / 中身が空 の日付は exit 1 で失敗する (成功扱いで古い fixture を温存しない)。

**間引きモード**:
- `evenly` (既定): 等間隔 N 件 + 各区間の中央点。head / tail bias なし。SUM 系集計の時系列偏り抑制に有効
- `head`: 先頭 N 件 (実装最小、時系列の先頭偏り)
- `tail`: 末尾 N 件 (末尾重視)

本リポジトリは `evenly --limit 100` で生成している。

### 間引きによる値の変化

間引きサンプル (`evenly --limit 100`) で `parse_all` を実行した結果は、
時系列 SUM 系 (step / distance / active_energy) で値が 50-60 倍ほど小さく
なるが、**型と None/not-None の挙動は維持される**ので parser の回帰
テストに使える。

時系列以外 (sleep / 体重 / 瞑想 / 体脂肪率 / 安静時心拍) は元データが
100 件未満なので間引き後も全件残り、値は変わらない。
型と非 None 判定は全フィールドで保持される。

### テストから読み込む方法

```python
from pathlib import Path
from datetime import date
from ihealth.parser import ParseContext, parse_all

FIX_ROOT = Path(__file__).resolve().parent / "fixtures" / "sample_health_export"

def test_parse_all_2026_04_22():
    ctx = ParseContext(target_date=date(2026, 4, 22), max_heart_rate=178.0)
    result = parse_all(FIX_ROOT / "2026-04-22", date(2026, 4, 22), ctx)
    assert result.step_count is not None
    assert result.nap_hours is not None  # 昼寝あり
    assert result.mindful_minutes is None  # 瞑想なし

def test_parse_all_2026_04_23():
    ctx = ParseContext(target_date=date(2026, 4, 23), max_heart_rate=178.0)
    result = parse_all(FIX_ROOT / "2026-04-23", date(2026, 4, 23), ctx)
    assert result.nap_hours is None  # 昼寝なし
    assert result.body_mass_kg is None  # 体重未計測
    assert result.mindful_minutes is not None  # 瞑想あり
```
