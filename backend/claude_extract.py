"""
claude_extract.py — Claude API によるマイソクPDFの物件情報抽出（AI抽出）。

ルールベース（extract_info.py）はレイアウト依存で精度にむらが出るため、
APIキーがあるときは各ページを画像化して Claude に読ませ、
構造化出力（JSON Schema強制）で物件リストを得る。

- 複数物件PDF対応: 1物件=1要素。写真だけの続きページは直前物件に統合される。
- スキャン画像ページもOCR不要でそのまま読める（プラウドタワー等の装飾文字も可）。
- 失敗時は呼び出し側でルールベースにフォールバックする。

環境変数:
  ANTHROPIC_API_KEY … 必須（backend/.env に置く）
  ANTHROPIC_MODEL   … 省略時 claude-opus-4-8
"""
from __future__ import annotations

import base64
import json
import os

import fitz  # PyMuPDF
from anthropic import Anthropic

DEFAULT_MODEL = "claude-opus-4-8"

# 1ページあたりの最大画像サイズ（長辺px）。Opus 4.8 の高解像度上限(2576px)内。
MAX_EDGE_PX = 2300

SCHEMA = {
    "type": "object",
    "properties": {
        "properties": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name":   {"type": "string", "description": "物件名（マンション名）"},
                    "price":  {"type": "string", "description": "価格 例: 1億2,800万円 / 25,000万円"},
                    "addr":   {"type": "string", "description": "所在地住所"},
                    "access": {"type": "string", "description": "交通 例: JR山手線「田町」駅 徒歩13分"},
                    "area":   {"type": "string", "description": "専有面積と間取り 例: 104.6㎡ / 3LDK"},
                    "unit":   {"type": "string", "description": "所在階・向き 例: 35階 南向き"},
                    "year":   {"type": "string", "description": "築年月 例: 2006年12月"},
                    "floors": {"type": "string", "description": "階数 例: 35階／48階建"},
                    "mgmt":   {"type": "string", "description": "管理費 例: 月額25,420円"},
                    "page":   {"type": "integer", "description": "この物件の主たるページ番号（1始まり）"},
                },
                "required": ["name", "price", "addr", "access", "area",
                             "unit", "year", "floors", "mgmt", "page"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["properties"],
    "additionalProperties": False,
}

PROMPT = """これは不動産のマイソク（物件概要書）PDFの各ページ画像です。掲載されている物件の情報を抽出してください。

ルール:
- 1つの物件 = 配列の1要素。複数物件が掲載されていれば全物件を抽出する。
- 写真だけのページや、直前の物件の続き（2ページ目）は新しい物件にせず、その情報は該当物件の要素に統合する。
- 同じ建物でも部屋が違う（価格・階・面積が異なる）場合は別々の物件として扱う。
- page はその物件の情報が主に載っているページ番号（1始まり）。
- 判読できない項目は空文字にする。推測で埋めない。
- 価格・管理費の数字はカンマ区切り（例: 25,000万円 / 月額25,420円）。
- 住所は都道府県から書けるなら含める（例: 東京都港区芝浦4丁目15-1）。"""


def is_configured() -> bool:
    """APIキーが実際に設定されているか（プレースホルダのままなら False）"""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    return key.startswith("sk-ant")


def _pdf_to_image_blocks(pdf_bytes: bytes) -> list[dict]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    blocks: list[dict] = []
    try:
        for i, page in enumerate(doc):
            long_edge = max(page.rect.width, page.rect.height)
            zoom = min(2.4, MAX_EDGE_PX / long_edge) if long_edge > 0 else 1.0
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            b64 = base64.standard_b64encode(pix.tobytes("png")).decode("utf-8")
            blocks.append({"type": "text", "text": f"--- ページ {i + 1} ---"})
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": b64},
            })
    finally:
        doc.close()
    return blocks


def extract_with_claude(pdf_bytes: bytes) -> list[dict]:
    """PDFから物件リストを抽出して返す。キー未設定・失敗時は例外を投げる（呼び出し側でフォールバック）"""
    if not is_configured():
        raise RuntimeError("ANTHROPIC_API_KEY が未設定です（backend/.env を確認）")

    client = Anthropic()  # ANTHROPIC_API_KEY を環境変数から読む
    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)

    content = _pdf_to_image_blocks(pdf_bytes)
    content.append({"type": "text", "text": PROMPT})

    response = client.messages.create(
        model=model,
        max_tokens=8000,
        thinking={"type": "adaptive"},
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": content}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("AI抽出が拒否されました")

    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    out = []
    for p in data.get("properties", []):
        p = {k: (v if v is not None else "") for k, v in p.items()}
        p["page"] = int(p.get("page") or 0) or 1
        out.append(p)
    return out
