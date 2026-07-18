# Tipness 振替枠監視

iTIPNESS（KIDSスイミング）の振替カレンダーを10分おきにチェックし、**日曜日に振替可能枠が出たらiPhoneにプッシュ通知**します。

## 仕組み

- GitHub Actions が10分おきに `monitor.py` を実行
- Playwright でキッズ保護者用ログイン → 会員選択 → 振替カレンダー(通常練習日)を開く
- カレンダー上でリンクになっている日付 = 振替可能日
- そのうち**日曜日**があれば [ntfy.sh](https://ntfy.sh) 経由で通知
- 一度通知した枠は `state.json` に記録し、埋まって再度空くまで再通知しない

## セットアップ手順

### 1. iPhoneにntfyアプリを入れる

1. App Storeで「ntfy」をインストール
2. アプリで「＋」→ 購読するトピック名を入力（例: `tipness-abc123xyz` のような**推測されにくいランダムな文字列**にすること。トピック名を知っている人は誰でも通知を見られます）

### 2. GitHubリポジトリを作る

1. GitHubで**Privateリポジトリ**を新規作成（例: `tipness-monitor`）
2. このフォルダの中身（`monitor.py`, `.github/workflows/monitor.yml`, `README.md`）をアップロード
   - Web画面なら「Add file → Upload files」でOK。`.github/workflows/` のフォルダ構成を保つこと

### 3. Secretsを登録する

リポジトリの Settings → Secrets and variables → Actions → New repository secret で以下の3つを登録:

| Name | 値 |
|---|---|
| `TIPNESS_EMAIL` | キッズ保護者用ログインのメールアドレス |
| `TIPNESS_PASSWORD` | パスワード |
| `NTFY_TOPIC` | 手順1で決めたトピック名 |

### 4. 動作確認

1. リポジトリの Actions タブ → 「Tipness振替枠監視」→「Run workflow」で手動実行
2. ログに `振替可能日: [...]` と表示されれば成功
3. 失敗した場合は、実行結果ページ下部の Artifacts に `error-screenshot` が保存されるので、どの画面で止まったか確認できる

### 5. 通知テスト（任意）

ターミナルやiPhoneのショートカットから以下を実行すると、通知が届くか確認できます:

```
curl -d "テスト通知" https://ntfy.sh/あなたのトピック名
```

## 調整ポイント

`monitor.py` の冒頭の定数で変更できます:

- `TARGET_WEEKDAY = 6` … 監視する曜日（月=0〜日=6）
- `CHECK_NEXT_MONTH = True` … 翌月分もチェックするか
- チェック間隔を変えたい場合は `monitor.yml` の cron を編集（例: `*/30 * * * *` で30分おき）

## 注意事項

- 通知を受けたら**予約は手動**で行ってください（自動予約はしません）
- サイトのメンテナンス時間（23:45〜0:15）は実行が失敗することがありますが問題ありません
- ワークフローが失敗するとGitHubからメールが届きます（Settings → Notificationsで調整可）
- 60日間リポジトリに変更がないとGitHubがscheduleを自動停止することがあります。その場合はActionsタブから再有効化してください
- アクセスは10分に1回・数ページの閲覧のみなのでサイトへの負荷は軽微ですが、利用は自己責任でお願いします
