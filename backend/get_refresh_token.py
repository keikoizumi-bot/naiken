"""
get_refresh_token.py — support@08equitas.com のリフレッシュトークンを一度だけ取得する補助スクリプト。

前提:
  Google Cloud Console で「OAuth 2.0 クライアント ID（アプリの種類: デスクトップ）」を作成し、
  client_secret.json をこのフォルダに置く。

使い方（手元のPCで実行）:
  pip install google-auth-oauthlib
  python get_refresh_token.py
  → ブラウザが開くので support@08equitas.com でログイン＆Drive権限を許可
  → 画面（と refresh_token.txt）に GOOGLE_REFRESH_TOKEN が出力される

出力された値を Cloud Run の環境変数 GOOGLE_REFRESH_TOKEN に設定する。
client_secret.json の client_id / client_secret も
GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET に設定する。
"""
import json
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]
SECRET = Path(__file__).parent / "client_secret.json"


def main():
    if not SECRET.exists():
        raise SystemExit(
            "client_secret.json が見つかりません。"
            "Google Cloud Console でデスクトップ用 OAuth クライアントを作成し、"
            "ダウンロードした JSON をこのフォルダに client_secret.json として置いてください。"
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(SECRET), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")

    info = json.loads(SECRET.read_text())
    node = info.get("installed") or info.get("web") or {}
    print("\n==== 以下を Cloud Run の環境変数に設定してください ====")
    print("GOOGLE_CLIENT_ID     =", node.get("client_id"))
    print("GOOGLE_CLIENT_SECRET =", node.get("client_secret"))
    print("GOOGLE_REFRESH_TOKEN =", creds.refresh_token)

    (Path(__file__).parent / "refresh_token.txt").write_text(creds.refresh_token or "")
    print("\n(refresh_token.txt にも保存しました。取り扱い注意・コミット禁止)")


if __name__ == "__main__":
    main()
