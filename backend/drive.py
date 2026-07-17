"""
drive.py — Google Drive 連携ヘルパー。

認証方式: OAuth（support@08equitas.com で一度認可して得たリフレッシュトークンを
サーバーの環境変数に保管し、そのユーザー所有としてアップロードする）。

必要な環境変数:
  GOOGLE_CLIENT_ID       … OAuth クライアントID
  GOOGLE_CLIENT_SECRET   … OAuth クライアントシークレット
  GOOGLE_REFRESH_TOKEN   … get_refresh_token.py で取得したリフレッシュトークン

依存:
  google-auth, google-api-python-client
"""
from __future__ import annotations

import io
import os

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# フルの drive スコープ（既存フォルダへの書き込み・共有設定に必要）
SCOPES = ["https://www.googleapis.com/auth/drive"]

FOLDER_MIME = "application/vnd.google-apps.folder"


def _credentials() -> Credentials:
    client_id = os.environ["GOOGLE_CLIENT_ID"]
    client_secret = os.environ["GOOGLE_CLIENT_SECRET"]
    refresh_token = os.environ["GOOGLE_REFRESH_TOKEN"]
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )


def get_service():
    """Drive v3 サービスクライアントを返す。"""
    return build("drive", "v3", credentials=_credentials(), cache_discovery=False)


def ensure_subfolder(service, parent_id: str, name: str) -> str:
    """
    parent_id 直下に name フォルダがあれば その ID を、無ければ作成して ID を返す。
    （社内用 > 提案物件元データ > ファイル のような未作成の階層を自動生成するため）
    """
    safe = name.replace("'", "\\'")
    q = (
        f"name = '{safe}' and '{parent_id}' in parents "
        f"and mimeType = '{FOLDER_MIME}' and trashed = false"
    )
    res = (
        service.files()
        .list(q=q, fields="files(id, name)", pageSize=1, supportsAllDrives=True,
              includeItemsFromAllDrives=True)
        .execute()
    )
    files = res.get("files", [])
    if files:
        return files[0]["id"]

    meta = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
    created = (
        service.files()
        .create(body=meta, fields="id", supportsAllDrives=True)
        .execute()
    )
    return created["id"]


def ensure_path(service, root_id: str, *names: str) -> str:
    """root_id から names のフォルダ階層を順にたどり（無ければ作成し）最終フォルダIDを返す。"""
    current = root_id
    for n in names:
        current = ensure_subfolder(service, current, n)
    return current


def upload_pdf(
    service,
    parent_id: str,
    filename: str,
    data: bytes,
    link_shareable: bool = False,
) -> dict:
    """
    PDF を parent_id 直下にアップロードする。
    link_shareable=True の場合、「リンクを知っている全員が閲覧可」を付与
    （お客様がサイトのiframeで図面を閲覧できるようにするため）。

    返り値: {id, name, webViewLink, previewLink}
    """
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype="application/pdf", resumable=False)
    meta = {"name": filename, "parents": [parent_id]}
    f = (
        service.files()
        .create(body=meta, media_body=media,
                fields="id, name, webViewLink", supportsAllDrives=True)
        .execute()
    )
    file_id = f["id"]

    if link_shareable:
        service.permissions().create(
            fileId=file_id,
            body={"role": "reader", "type": "anyone"},
            supportsAllDrives=True,
        ).execute()

    return {
        "id": file_id,
        "name": f.get("name", filename),
        "webViewLink": f.get("webViewLink"),
        # iframe 埋め込み用のプレビューURL
        "previewLink": f"https://drive.google.com/file/d/{file_id}/preview",
    }
