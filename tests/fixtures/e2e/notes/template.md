<%*
// Templater: 合成テンプレート (tests/test_e2e_diary.py 用)
const m = tp.file.title.match(/^(\d{4}-\d{2}-\d{2})$/);
const dateStr = m ? m[1] : tp.date.now("YYYY-MM-DD");
const weekday = tp.date.now("ddd", 0, dateStr, "YYYY-MM-DD");
-%>
---
名前: 毎日の習慣 <% dateStr %>
日付: <% dateStr %>
日記: false
瞑想: []
"歩数 (歩)":
"睡眠時間 (分)":
昼寝時間:
"消費カロリー (kcal)":
"移動距離 (km)":
"安静時心拍数 (bpm)":
"平均心拍数 (bpm)":
"最大心拍数 (bpm)":
"酸素飽和度 (%)":
運動強度スコア:
"瞑想時間 (分)":
"瞑想回数 (回)":
"アクティビティ時間 (分)":
"体重 (kg)":
"体脂肪率 (%)":
気温:
湿度:
気圧:
天気:
日照時間:
降水量:
アクティビティ詳細:
参照URLs: "[サンプル](https://example.com/a)"
アウトプット:
Github:
---
# 毎日の習慣 <% dateStr %>（<% weekday %>）

## TODO
- [ ] 合成のタスク

## なんでもメモ
