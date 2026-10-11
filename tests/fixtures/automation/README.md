# tests/fixtures/automation/

Health Auto Export の **オートメーション** (iCloud Drive / JSON / ExportVersion.v2)
出力を模した **完全な合成データ** です。実在の健康値・デバイス識別子・日付は
含みません (日付はわざと 2030 年、ソース名は `Sample Watch` などの架空名)。
構造 (キー名・日時書式 `"YYYY-MM-DD HH:MM:SS +0900"`・単位表記) だけを実データに
合わせています。

| ディレクトリ | 想定する設定 / 検証したいこと |
| --- | --- |
| `aggregated/HealthAutoExport-2030-01-15.json` | `aggregateData=true` / `aggregateSleep=true` (現役設定)。前日 record の除外、kJ → kcal、`Avg`/`Min`/`Max` 心拍、1 日 1 レコードの睡眠集計、瞑想 2 回、エクササイズ時間 3 分、体重の最新値、BMI (`body_mass_index`) を体重と取り違えないこと、無関係 metric の無視。ワークアウトは書き出さない設定 (`data.workouts` なし) なので、アクティビティ詳細は瞑想 + エクササイズ時間から組む (`"瞑想6分、エクササイズ3分"`。テストで `data.workouts: []` を足しても当日のワークアウトが 0 件なのでエクササイズ時間で組み、同じ値になる。当日のワークアウトを足すとワークアウトの一覧 `"瞑想6分、ヨガ20分"`)。record は `+0900` なので日の終わりは翌日 0:00 JST (mtime がそれより前なら暫定値) |
| `segments/HealthAutoExport-2030-02-01.json` + `2030-02-02.json` | 非集計の睡眠 segment (`startDate`/`endDate` + `value`)。前日ファイルとの結合、日付をまたぐ同一 segment の重複除去、Awake / In Bed の除外 (セッションの間隔を埋めるだけ)、夜主睡眠 / 昼寝は segment ごとの終了時刻で分類 (0 時前に終わる前半だけセッション経由で翌朝の夜主睡眠へ繰り越す)、ソース別最大、昼寝 (14-20 時終了)。前日ファイルが無い / 読めない / 夜が明ける前の古い前日ファイルしか無いときは、夜主睡眠を過少の恐れがある暫定値 (`provisional=True`、暫定なのは夜主睡眠だけなので `provisional_fields=("sleep_hours",)`) として返すことも、この組で検証する |
| `workouts/HealthAutoExport-2030-03-10.json` | `data.workouts` からアクティビティ詳細 (`"瞑想3分、ウォーキング31分、…"`) を組む。英 → 日の名前変換、未知名はそのまま、`duration` (秒) / end-start、同名の合算、前日ワークアウトの除外 |

アクティビティ詳細の規則 (`ihealth.automation_parser.compose_activity_detail`、W3-7 改訂):
瞑想 (合計 1 分以上で `"瞑想N分"`) → 当日のワークアウトがあればその一覧 (`"ヨガ20分"` 等) /
無ければ (出力しない設定、または出力していて当日 0 件) エクササイズ時間 (合計 1 分以上で
`"エクササイズN分"`)。何も無い日は、心拍のデータがある (Watch を着けていた) なら `"なし"`、
心拍も無ければ書かない (`None`)。実際の HAE のオートメーションは運動 0 分の日に
`apple_exercise_time` 自体を出力しない (qty 0 の record は出てこない) ので、「運動 0 分」と
「記録なし」はエクササイズ時間ではなく心拍の有無で分ける。瞑想 / ワークアウト / (使うときの)
エクササイズ時間を解釈できない日、JSON に無いそれらを AutoSync の .hae で補う日にその .hae が
途中までの出力 (保留) / 読めない日は、取りこぼした一覧や誤った `"なし"` を確定値として書かない
よう `None` (AutoSync だけの経路と同じ)。

期待値は各テスト (`tests/test_automation_parser.py`, `tests/test_health_loader.py`)
に手計算で書いてあります。fixture を変更したらテストの期待値も合わせて更新してください。
