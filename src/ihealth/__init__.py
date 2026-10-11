"""ihealth-relay: Apple Health データを集計して転記する macOS バッチツール。

iPhone の Health Auto Export が書き出したデータを読み、Obsidian 日記 (健康 + 気象庁の
天気 + GitHub 活動を日記ノートの frontmatter に書く) や、pluggable sink (Notion /
Markdown / stdout / SQLite / Slack) に転記する。
"""

__version__ = "0.2.0"
