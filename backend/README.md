# 内見候補サイト バックエンド（PDF → obi消し → Drive格納）

サイトからアップロードされた図面PDFを、業者帯除去（obi消し）したうえで Google Drive の
2か所に格納し、サイトに図面リンク・提案日を返すサービスです。

```
[サイト(Netlify)] --PDF＋案件フォルダID--> [このバックエンド(Cloud Run)]
    1. obi_keshi.py で業者帯除去（A4横統一）
    2. 案件フォルダ/01.お客様共有資料/01.候補物件 へ  yyyymmdd_候補物件.pdf（obi消し済み）
    3. 案件フォルダ/02.社内用/提案物件元データ/ファイル へ  元PDF（そのまま）
    4. 図面プレビューリンク・提案日(mm/dd) を返す
```

## ファイル

| ファイル | 役割 |
|---|---|
| `app.py` | FastAPI 本体（`/upload` エンドポイント） |
| `drive.py` | Drive 連携（OAuth・フォルダ作成・アップロード・共有設定） |
| `obi_keshi.py` | 業者帯除去（既存スクリプトのコピー） |
| `get_refresh_token.py` | OAuth リフレッシュトークン取得（初回のみ手元で実行） |
| `Dockerfile` / `requirements.txt` | Cloud Run 用 |

---

## セットアップ手順（初回のみ）

### 1. Google Cloud プロジェクト
1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクトを作成（既存でも可）。
2. 「APIとサービス」→「ライブラリ」で **Google Drive API** を有効化。
3. 「OAuth 同意画面」を設定（内部/External はドメイン運用に合わせて。テスト時は自分を테스트ユーザーに追加）。
4. 「認証情報」→「OAuth クライアント ID を作成」→ アプリの種類 **デスクトップ** → JSON をダウンロードし、
   このフォルダに `client_secret.json` として保存。

### 2. リフレッシュトークン取得（support@08equitas.com で認可）
```bash
pip install google-auth-oauthlib
python get_refresh_token.py
```
ブラウザで **support@08equitas.com** としてログイン＆Drive権限を許可。
出力された `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REFRESH_TOKEN` を控える。

> ⚠️ `client_secret.json` と `refresh_token.txt` は秘匿情報。リポジトリにコミットしない（`.gitignore` 済み）。

### 3. Cloud Run へデプロイ
```bash
gcloud run deploy equitas-viewing-backend \
  --source . \
  --region asia-northeast1 \
  --allow-unauthenticated \
  --set-env-vars "GOOGLE_CLIENT_ID=...,GOOGLE_CLIENT_SECRET=...,GOOGLE_REFRESH_TOKEN=...,ALLOWED_ORIGINS=https://<あなたのNetlifyドメイン>"
```
デプロイ後に表示される URL（例 `https://equitas-viewing-backend-xxxx.a.run.app`）を控える。

### 4. サイト側の設定
`index.html` 先頭の CONFIG に、上記 URL と案件フォルダIDを設定：
```js
const BACKEND_URL = "https://equitas-viewing-backend-xxxx.a.run.app";
const PROJECT_FOLDER_ID = "1cknlOYYMFBAN5yndzB1PkwKPPbH-RqLc"; // 案件ごとに変える
```

---

## 環境変数

| 変数 | 必須 | 既定 | 説明 |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | 推奨 | — | Claude APIキー（**AI抽出**用。設定すると物件情報の読み取り精度が大幅向上。未設定ならルールベース抽出で動作）。ローカルは `backend/.env` に記載、Cloud Run は環境変数で設定 |
| `ANTHROPIC_MODEL` | | `claude-opus-4-8` | AI抽出に使うモデル（コスト重視なら `claude-haiku-4-5`） |
| `GOOGLE_CLIENT_ID` | ✅ | — | OAuth クライアントID |
| `GOOGLE_CLIENT_SECRET` | ✅ | — | OAuth クライアントシークレット |
| `GOOGLE_REFRESH_TOKEN` | ✅ | — | 取得したリフレッシュトークン |
| `ALLOWED_ORIGINS` | | `*` | CORS 許可オリジン（本番は Netlify ドメインに限定推奨） |
| `MAKE_CANDIDATE_LINK_SHAREABLE` | | `1` | 候補PDFを「リンクを知る全員が閲覧可」にする（サイトのiframe表示に必要）。`0`で無効 |
| `SHARE_DIR_NAME` | | `01.お客様共有資料` | 格納先①の親フォルダ名 |
| `CANDIDATE_DIR_NAME` | | `01.候補物件` | 格納先①のフォルダ名 |
| `INTERNAL_DIR_NAME` | | `02.社内用` | 格納先②の親フォルダ名 |
| `RAWDATA_DIR_NAME` | | `提案物件元データ` | 格納先②の中間フォルダ名（無ければ自動作成） |
| `RAWFILE_DIR_NAME` | | `ファイル` | 格納先②の最終フォルダ名（無ければ自動作成） |

> **共有設定について**：`MAKE_CANDIDATE_LINK_SHAREABLE=1`（既定）は、お客様がサイト上の図面をそのまま閲覧できるようにするため、
> obi消し済みPDFに「リンクを知っている全員が閲覧可」を付与します。生PDF（社内用）には付与しません。
> お客様アカウント個別共有にしたい等の運用があれば `0` にして別途共有してください。

---

## ローカル起動（動作確認）
```bash
pip install -r requirements.txt
export GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... GOOGLE_REFRESH_TOKEN=...
uvicorn app:app --reload --port 8080
# 別ターミナルで
curl -F "file=@sample.pdf" -F "project_folder_id=1cknlOYYMFBAN5yndzB1PkwKPPbH-RqLc" \
     http://localhost:8080/upload
```

## `/upload` レスポンス例
```json
{
  "ok": true,
  "date": "20260714",
  "proposalDate": "07/14",
  "candidate": {
    "id": "…", "name": "20260714_候補物件.pdf",
    "webViewLink": "https://drive.google.com/file/d/…/view",
    "previewLink": "https://drive.google.com/file/d/…/preview"
  },
  "raw": { "id": "…", "name": "元のファイル名.pdf", "previewLink": "…" }
}
```
