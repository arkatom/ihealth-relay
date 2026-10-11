"""Publisher (Sink) 実装の集約パッケージ (Phase 1 A2 で集約完了).

各 Publisher は :class:`ihealth.publishers.base.Publisher` プロトコルを実装し、
集計済みの :class:`ihealth.models.DailyHealthData` を任意の保存先 (Notion DB,
Markdown ファイル, SQLite, Slack 通知 ...) に流す責務を持つ.

実装済みの publisher (v0.1.0 + Obsidian 日記):

- ``notion``  : Notion DB の日記ページを PATCH で更新 (:mod:`.notion`)
- ``dryrun``  : ``--dry-run`` 用に組み立て結果をログ出力 (:mod:`.dryrun`)
- ``markdown``: Obsidian vault 等への frontmatter YAML + body 書き出し (:mod:`.markdown`)
- ``stdout``  : JSON 1 行を標準出力に流す (pipe 連携用) (:mod:`.stdout`)
- ``sqlite``  : ローカル SQLite DB に 1 日 1 行 UPSERT (:mod:`.sqlite`)
- ``slack``   : Incoming Webhook で日次サマリ通知 (:mod:`.slack`)
- ``obsidian``: 既存の Obsidian 日記ノート (``日記/**/YYYY-MM-DD.md``) の frontmatter の
  担当キーだけを部分更新 (:mod:`.obsidian`)。健康データに加えて気象庁の天気・GitHub
  活動も書く。CLI の ``--publisher obsidian`` は複数日・複数 source の
  :mod:`ihealth.diary` パイプラインから :meth:`ObsidianPublisher.publish_record` を
  呼ぶ (``Publisher`` プロトコルの ``publish`` は健康データだけを書く互換経路)

``markdown`` との違い: ``markdown`` は iHealth 専有ディレクトリにファイルを丸ごと
書き出す。``obsidian`` はユーザーが日々書いているノートに値を差し込むので、担当キー
以外を 1 byte も変えない・書込前バックアップ・並行編集検知などの安全策を持つ。

モジュール構成:
    - :mod:`.base` : :class:`Publisher` Protocol の単一定義.
    - :mod:`.dryrun` : :class:`DryRunPublisher`.
    - :mod:`.notion` : :class:`NotionClient` + :class:`NotionPublisher` + ``build_properties``.
    - :mod:`.markdown` / :mod:`.stdout` / :mod:`.sqlite` / :mod:`.slack` : 各 publisher.
    - :mod:`.obsidian` : :class:`ObsidianPublisher` (frontmatter の byte 保存 upsert は
      :mod:`ihealth.frontmatter`, 1 日分の値の束は :class:`ihealth.records.DiaryRecord`).
    - :mod:`._payload` : publisher 共通の DailyHealthData → flat dict 変換 + 型検証.

後方互換:
    旧 :mod:`ihealth.workflow` 経由の import (例: ``from ihealth.workflow
    import Publisher, NotionPublisher, DryRunPublisher``) は本パッケージの
    各クラスを workflow.py が re-export することで引き続き利用可能.
    :func:`tests.test_workflow.TestPublisherReexports` で互換を回帰テスト.
"""

from __future__ import annotations
