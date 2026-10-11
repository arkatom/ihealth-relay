# tests/fixtures/automation/

Health Auto Export の **オートメーション** (iCloud Drive / JSON / ExportVersion.v2)
出力を模した **完全な合成データ** です。実在の健康値・デバイス識別子・日付は
含みません (日付はわざと 2030 年、ソース名は `Sample Watch` などの架空名)。
構造 (キー名・日時書式 `"YYYY-MM-DD HH:MM:SS +0900"`・単位表記) だけを実データに
合わせています。

| ディレクトリ | 想定する設定 / 検証したいこと |
| --- | --- |
| `aggregated/HealthAutoExport-2030-01-15.json` | `aggregateData=true` / `aggregateSleep=true` (現役設定)。前日 record の除外、kJ → kcal、`Avg`/`Min`/`Max` 心拍、1 日 1 レコードの睡眠集計、瞑想 2 回、体重の最新値、BMI (`body_mass_index`) を体重と取り違えないこと、無関係 metric の無視 |
| `segments/HealthAutoExport-2030-02-01.json` + `2030-02-02.json` | 非集計の睡眠 segment (`startDate`/`endDate` + `value`)。前日ファイルとの結合、日付をまたぐ同一 segment の重複除去、Awake / In Bed の除外、セッション結合 (0 時前に終わる前半も夜主睡眠に入る)、ソース別最大、昼寝 (14-20 時終了) |
| `workouts/HealthAutoExport-2030-03-10.json` | `data.workouts` からアクティビティ詳細 (`"瞑想3分、ウォーキング31分、…"`) を組む。英 → 日の名前変換、未知名はそのまま、`duration` (秒) / end-start、同名の合算、前日ワークアウトの除外 |

期待値は各テスト (`tests/test_automation_parser.py`, `tests/test_health_loader.py`)
に手計算で書いてあります。fixture を変更したらテストの期待値も合わせて更新してください。
