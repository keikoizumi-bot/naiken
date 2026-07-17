"""
app.py — 内見候補サイトのバックエンド（FastAPI）。

フロー:
  サイトから PDF＋案件フォルダID を受け取り
   1. obi_keshi.py で業者帯を除去（A4横統一）
   2. 格納先①（お客様共有資料/候補物件）へ  yyyymmdd_候補物件.pdf（obi消し済み）
   3. 格納先②（社内用/提案物件元データ/ファイル）へ  元PDF（そのまま）
   4. Drive のプレビューリンク・ファイルID・提案日を JSON で返す

環境変数:
  GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN  … OAuth（drive.py 参照）
  ALLOWED_ORIGINS      … CORS 許可オリジン（カンマ区切り。既定 "*"）
  SHARE_DIR_NAME       … 既定 "01.お客様共有資料"
  CANDIDATE_DIR_NAME   … 既定 "01.候補物件"
  INTERNAL_DIR_NAME    … 既定 "02.社内用"
  RAWDATA_DIR_NAME     … 既定 "提案物件元データ"
  RAWFILE_DIR_NAME     … 既定 "ファイル"
  MAKE_CANDIDATE_LINK_SHAREABLE … "1"(既定) で候補PDFを「リンクを知る全員が閲覧可」にする
                                   （サイトのiframeでお客様が図面を見られるようにするため）
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

import obi_keshi
import drive
from envload import load_dotenv_file
load_dotenv_file()   # ローカル実行時は backend/.env から読み込む（Cloud Runは環境変数優先）

import claude_extract
from extract_info import extract_all, geocode_jp

JST = ZoneInfo("Asia/Tokyo")

SHARE_DIR_NAME = os.environ.get("SHARE_DIR_NAME", "01.お客様共有資料")
CANDIDATE_DIR_NAME = os.environ.get("CANDIDATE_DIR_NAME", "01.候補物件")
INTERNAL_DIR_NAME = os.environ.get("INTERNAL_DIR_NAME", "02.社内用")
RAWDATA_DIR_NAME = os.environ.get("RAWDATA_DIR_NAME", "提案物件元データ")
RAWFILE_DIR_NAME = os.environ.get("RAWFILE_DIR_NAME", "ファイル")
MAKE_SHAREABLE = os.environ.get("MAKE_CANDIDATE_LINK_SHAREABLE", "1") == "1"

app = FastAPI(title="Equitas 内見候補バックエンド")

_origins = os.environ.get("ALLOWED_ORIGINS", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _origins.strip() == "*" else [o.strip() for o in _origins.split(",")],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/")
def health():
    return {"ok": True, "service": "equitas-viewing-backend"}


def _unique_name(service, folder_id: str, base: str, ext: str) -> str:
    """folder_id 内で base+ext が衝突する場合 _2, _3 … を付けて回避したファイル名を返す。"""
    existing = set()
    q = f"'{folder_id}' in parents and trashed = false"
    res = service.files().list(
        q=q, fields="files(name)", pageSize=1000,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    for f in res.get("files", []):
        existing.add(f.get("name"))
    name = f"{base}{ext}"
    if name not in existing:
        return name
    i = 2
    while f"{base}_{i}{ext}" in existing:
        i += 1
    return f"{base}_{i}{ext}"


def _run_obi_keshi(raw: bytes) -> bytes:
    """アップロードされたPDFバイト列に obi_keshi を適用し、処理済みPDFバイト列を返す。"""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.pdf"
        dst = Path(td) / "out.pdf"
        src.write_bytes(raw)
        obi_keshi.process_pdfs([src], dst)
        return dst.read_bytes()


@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    project_folder_id: str = Form(...),
):
    if file.content_type not in ("application/pdf", "application/octet-stream") and not (
        file.filename or ""
    ).lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="PDFファイルを添付してください。")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="空のファイルです。")

    # 1. obi消し
    try:
        cleaned = _run_obi_keshi(raw)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"obi消し処理に失敗しました: {e}")

    # 1.5 PDFから物件情報を自動抽出:
    #   ①AI抽出（Claude API・キー設定時）→ ②ルールベース（フォールバック）
    extracted_list = []
    if claude_extract.is_configured():
        try:
            extracted_list = claude_extract.extract_with_claude(raw)
        except Exception:
            extracted_list = []
    if not extracted_list:
        try:
            extracted_list = extract_all(raw)
        except Exception:
            extracted_list = []
    # 住所→座標（国土地理院・キー不要）。同一住所はキャッシュ
    _geo_cache: dict = {}
    for item in extracted_list:
        addr = item.get("addr", "")
        if not addr:
            continue
        if addr not in _geo_cache:
            _geo_cache[addr] = geocode_jp(addr)
        if _geo_cache[addr]:
            item["lat"], item["lng"] = _geo_cache[addr]

    # 提案日（アップロード日・JST）
    now = datetime.now(JST)
    yyyymmdd = now.strftime("%Y%m%d")
    proposal_mmdd = now.strftime("%m/%d")

    # Drive
    try:
        service = drive.get_service()
        candidate_folder = drive.ensure_path(
            service, project_folder_id, SHARE_DIR_NAME, CANDIDATE_DIR_NAME
        )
        internal_folder = drive.ensure_path(
            service, project_folder_id, INTERNAL_DIR_NAME, RAWDATA_DIR_NAME, RAWFILE_DIR_NAME
        )

        # 格納先①: obi消し済み  yyyymmdd_候補物件.pdf
        cand_name = _unique_name(service, candidate_folder, f"{yyyymmdd}_候補物件", ".pdf")
        cand = drive.upload_pdf(
            service, candidate_folder, cand_name, cleaned, link_shareable=MAKE_SHAREABLE
        )

        # 格納先②: 生PDF（元のファイル名のまま。内部用なので共有はしない）
        raw_name = file.filename or f"{yyyymmdd}_original.pdf"
        raw_res = drive.upload_pdf(
            service, internal_folder, raw_name, raw, link_shareable=False
        )
    except KeyError as e:
        raise HTTPException(status_code=500, detail=f"認証情報の環境変数が未設定です: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Drive への保存に失敗しました: {e}")

    return JSONResponse(
        {
            "ok": True,
            "date": yyyymmdd,
            "proposalDate": proposal_mmdd,
            "extractedList": extracted_list,  # PDFから自動抽出した物件情報（複数物件対応）
            "extracted": extracted_list[0] if extracted_list else {},  # 互換用
            "candidate": cand,      # {id, name, webViewLink, previewLink}
            "raw": raw_res,
        }
    )
