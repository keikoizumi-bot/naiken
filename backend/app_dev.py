"""
app_dev.py — ローカル動作確認用バックエンド（Driveを使わない）。

本番は app.py（Drive格納）。こちらはサイト側の
「PDFアップロード → obi消し → サイトに図面反映」UXを、
GCP/Drive無しで今すぐ確認するための開発サーバー。

  /upload            … 本番と同じI/F。obi消しを実行し、結果をローカル保存して
                       図面URL・提案日を返す（Driveの代わりにローカル配信）。
  /files/...         … 保存したPDFを配信（サイトのiframeがここを開く）
  /source/...        … テスト用の元PDF置き場（ブラウザ確認で使用）
  /site/...          … 動作確認用サイト（index.local.html）

起動:
  ./.venv/bin/uvicorn app_dev:app --port 8080
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import obi_keshi
from envload import load_dotenv_file
load_dotenv_file()   # backend/.env から ANTHROPIC_API_KEY 等を読み込む

import claude_extract
from extract_info import extract_all, geocode_jp

JST = ZoneInfo("Asia/Tokyo")
BASE = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8080")

ROOT = Path(__file__).parent
STORE = ROOT / "_local_store"
SOURCE = ROOT / "_dev_source"
SITE = ROOT / "_dev_site"
for d in [STORE / "candidate", STORE / "raw", SOURCE, SITE]:
    d.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Equitas 内見候補 ローカル確認用")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


@app.get("/")
def health():
    return {"ok": True, "mode": "dev-local", "note": "Driveは使わずローカル保存で確認"}


def _unique(dirpath: Path, base: str, ext: str) -> str:
    name = f"{base}{ext}"
    i = 2
    while (dirpath / name).exists():
        name = f"{base}_{i}{ext}"
        i += 1
    return name


@app.post("/upload")
async def upload(file: UploadFile = File(...), project_folder_id: str = Form(...)):
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="空のファイルです。")

    # 本物の obi消し
    try:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "in.pdf"
            dst = Path(td) / "out.pdf"
            src.write_bytes(raw)
            obi_keshi.process_pdfs([src], dst)
            cleaned = dst.read_bytes()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"obi消し処理に失敗: {e}")

    # PDFから物件情報を自動抽出:
    #   ①AI抽出（Claude API・キー設定時）→ ②ルールベース（フォールバック）
    extracted_list = []
    if claude_extract.is_configured():
        try:
            extracted_list = claude_extract.extract_with_claude(raw)
        except Exception as e:
            print(f"[extract] AI抽出失敗、ルールベースへフォールバック: {e}")
    if not extracted_list:
        try:
            extracted_list = extract_all(raw)
        except Exception:
            extracted_list = []
    _geo_cache = {}
    for item in extracted_list:
        addr = item.get("addr", "")
        if not addr:
            continue
        if addr not in _geo_cache:
            _geo_cache[addr] = geocode_jp(addr)
        if _geo_cache[addr]:
            item["lat"], item["lng"] = _geo_cache[addr]

    now = datetime.now(JST)
    ymd = now.strftime("%Y%m%d")

    cand_name = _unique(STORE / "candidate", f"{ymd}_候補物件", ".pdf")
    (STORE / "candidate" / cand_name).write_bytes(cleaned)

    raw_name = file.filename or f"{ymd}_original.pdf"
    (STORE / "raw" / raw_name).write_bytes(raw)

    return JSONResponse(
        {
            "ok": True,
            "date": ymd,
            "proposalDate": now.strftime("%m/%d"),
            "extractedList": extracted_list,
            "extracted": extracted_list[0] if extracted_list else {},
            "candidate": {
                "id": cand_name,
                "name": cand_name,
                "previewLink": f"{BASE}/files/candidate/{cand_name}",
                "webViewLink": f"{BASE}/files/candidate/{cand_name}",
            },
            "raw": {
                "id": raw_name,
                "name": raw_name,
                "previewLink": f"{BASE}/files/raw/{raw_name}",
            },
        }
    )


# 静的配信（保存物・テスト元PDF・確認用サイト）
app.mount("/files", StaticFiles(directory=str(STORE)), name="files")
app.mount("/source", StaticFiles(directory=str(SOURCE)), name="source")
app.mount("/site", StaticFiles(directory=str(SITE), html=True), name="site")
