#!/usr/bin/env python3
"""
obi_keshi.py — マイソクPDFの業者帯（下部の業者情報帯）を白塗りで除去し、
全ページをA4横サイズに統一して出力するスクリプト。

処理内容:
  1. 各ページの帯領域を自動検出（水平罫線・キーワードによる動的検出）
  2. 帯以外のコンテンツを抽出
  3. 全ページをA4横サイズ (841.92 x 595.32 pt) に統一して再配置
  4. コンテンツは上下中央に配置

使い方:
    python obi_keshi.py <input.pdf> --out <output.pdf>

依存:
    PyMuPDF (fitz)
        pip install --break-system-packages pymupdf
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.stderr.write(
        "[obi_keshi] PyMuPDF が見つかりません。次のコマンドでインストールしてください:\n"
        "    pip install --break-system-packages pymupdf\n"
    )
    sys.exit(2)

# numpy はピクセル解析フォールバック（画像ベースのページ用）で使う。
# 無くてもベクター検出は動くが、画像1枚埋込のページで帯検出ができなくなる。
try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False


# ============================================================
# 設定
# ============================================================

# 出力ページサイズ（A4横, pt）。全ページこのサイズに統一。
TARGET_W = 841.92
TARGET_H = 595.32

# 帯候補を探す下部の領域比率（この範囲内で帯の上端を検出する）
SEARCH_BOTTOM_RATIO = 0.30

# 帯検出に使うキーワード（業者情報固有・誤検出が起きにくい厳選版）
# 注: 「売主」「媒介」「代理」「FAX」「営業時間」等は本文・免責文にも出るため除外。
OBI_KEYWORDS = (
    "国土交通大臣",
    "国土交通省",
    "公正取引協議会",
    "不動産流通経営協会",
    "不動産協会会員",
    "物件No.",
    "物件No",
    "物件Ｎｏ",
    "登録No.",
    "登録No",
    "登録Ｎｏ",
    "免許番号",
)

# 本文側の免責文・注記とみなして検出対象から除外するブロックの先頭マーカー
DISCLAIMER_PREFIXES = ("※", "＊", "*")

# 帯が検出できなかった場合のフォールバック（下から何％を帯とみなすか）
FALLBACK_BAND_RATIO = 0.20

# 水平罫線とみなす条件
HLINE_MIN_WIDTH_RATIO = 0.40  # 線幅 ≧ ページ幅 × この比率
HLINE_MAX_THICKNESS = 5.0     # 線の太さ（pt）

# 帯の「外枠」とみなす大きな矩形の条件
# （業者帯は枠つきのことが多く、その上端が真の帯上端になる）
OBI_FRAME_MIN_WIDTH_RATIO = 0.30   # 矩形幅 ≧ ページ幅 × この比率
OBI_FRAME_MIN_HEIGHT = 20.0        # 矩形高さ ≧ これ（線と区別するため）

# 帯と本文のすき間で切るときの、本文側に残す最小余白（pt）
# （切断点はすき間の中点を狙うが、最低でもこの値だけ本文の最下端から下に取る）
MIN_GAP_FROM_CONTENT = 2.0

# ピクセル解析フォールバックの設定
# （ベクター描画もキーワードも検出できなかった画像ベースのページ用）
PIXEL_DPI = 150                  # ラスタライズ解像度
PIXEL_DARK_THRESHOLD = 160       # これ未満を「暗いピクセル」と判定
# ピクセル検出は本文内の細かな構造に反応しやすいので、検索範囲はベクター検出より
# 保守的に「下 20%」に絞る（業者帯は通常下から 15% 以内に収まる）。
PIXEL_SEARCH_BOTTOM_RATIO = 0.20
PIXEL_HLINE_MIN_RATIO = 0.60     # 連続darkがページ幅×この比率以上で水平罫線とみなす
                                  # （業者帯の上端罫線/色帯は通常ページ幅の大部分を占めるため厳しめに）
PIXEL_CONTENT_MIN_RATIO = 0.02   # 上方スキャンで「本文行」とみなす dark 比率の最小値
# 注: 「白余白→暗化」境界は本文内のわずかな余白でも反応してしまい、
# 文字や写真キャプションを切ってしまうため使用しない。罫線検出のみに依存する。

WHITE = (1.0, 1.0, 1.0)


# ============================================================
# 帯の上端検出
# ============================================================

def _is_disclaimer(text: str) -> bool:
    """※・*・＊で始まる免責文・注記ブロックかどうか。"""
    t = text.lstrip()
    return any(t.startswith(p) for p in DISCLAIMER_PREFIXES)


def _detect_obi_top_by_pixels(page: "fitz.Page") -> tuple[float, float] | None:
    """
    画像ベース（drawings/text blocks が取れない）ページ用のピクセル解析フォールバック。
    ページ下部 PIXEL_SEARCH_BOTTOM_RATIO 領域をラスタライズして、

      連続darkピクセルが幅 ≧ PIXEL_HLINE_MIN_RATIO となる最上行
      （= 業者帯の上端罫線または上端の色帯）

    を求める。さらに、その上端から上方向に向かって最初に出会う
    「コンテンツのある行」(dark 比率 ≧ PIXEL_CONTENT_MIN_RATIO) を
    本文最下端 (content_bottom) とみなす。

    返り値: (obi_top_raw, content_bottom)  どちらもページ座標(pt)。検出失敗時 None。

    注意:
      - 検索範囲を 20% に絞ることで、本文内の幅広な水平構造（写真境界・
        表の罫線）を拾わないようにしている。
      - HLINE_MIN_RATIO を 60% と厳しくしているのも同じ理由（業者帯の
        上端は通常ページ幅のほぼ全体にわたる）。
      - 「白余白→暗化」境界は本文内のわずかな余白でも反応してしまうため
        意図的に採用していない。
    """
    if not _HAS_NUMPY:
        return None

    zoom = PIXEL_DPI / 72.0
    try:
        pix = page.get_pixmap(
            matrix=fitz.Matrix(zoom, zoom),
            alpha=False,
            colorspace=fitz.csGRAY,
        )
    except Exception:
        return None

    W, H = pix.width, pix.height
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(H, W)

    search_top_px = int(H * (1.0 - PIXEL_SEARCH_BOTTOM_RATIO))
    region = arr[search_top_px:, :]
    Hr, Wr = region.shape
    if Hr <= 0 or Wr <= 0:
        return None

    dark = region < PIXEL_DARK_THRESHOLD  # bool[Hr, Wr]

    # ---- 水平罫線: 各行で「連続dark」の最長 run を求め、最上行を採用 ----
    min_run = max(1, int(Wr * PIXEL_HLINE_MIN_RATIO))
    line_row: int | None = None
    for i in range(Hr):
        row = dark[i]
        if not row.any():
            continue
        d = np.diff(np.concatenate(([0], row.view(np.int8), [0])))
        starts = np.where(d == 1)[0]
        ends = np.where(d == -1)[0]
        if len(starts) == 0:
            continue
        if (ends - starts).max() >= min_run:
            line_row = i
            break

    if line_row is None:
        return None
    obi_top_row = line_row

    # ---- 本文最下端: obi_top_row より上方向に走査し、
    # 最初に「コンテンツのある行」(dark比率 ≧ PIXEL_CONTENT_MIN_RATIO) に出会った位置
    content_thresh = Wr * PIXEL_CONTENT_MIN_RATIO
    dark_per_row = dark.sum(axis=1)
    content_bottom_row: int | None = None
    for i in range(obi_top_row - 1, -1, -1):
        if dark_per_row[i] >= content_thresh:
            content_bottom_row = i + 1
            break
    if content_bottom_row is None:
        content_bottom_row = 0

    obi_top_pt = (search_top_px + obi_top_row) / zoom
    content_bottom_pt = (search_top_px + content_bottom_row) / zoom
    return obi_top_pt, content_bottom_pt


def detect_obi_top(page: "fitz.Page") -> float:
    """
    ページから業者帯の上端 y 座標（切断点）を推定して返す。

    戦略:
      A. 帯の「生」の上端候補 (obi_top_raw) を求める:
         (1) 下部 SEARCH_BOTTOM_RATIO 内の水平罫線で最も上のもの
         (2) 厳選キーワードに一致するテキストブロックで最も上の y0
             （※ で始まる免責ブロックは除外）
      B. obi_top_raw より上にある「本文」コンテンツ（テキスト・図形）の
         最下端 content_bottom を求める。
      C. content_bottom と obi_top_raw のすき間の中点を切断点とする。
         （帯は図面とは必ず分離されているという前提）
         すき間が極端に狭い場合は content_bottom + MIN_GAP_FROM_CONTENT を取る。
      D. すべて検出失敗時は FALLBACK_BAND_RATIO で決め打ち。
    """
    rect = page.rect
    search_top = rect.y1 - rect.height * SEARCH_BOTTOM_RATIO

    # --- A1: 水平罫線候補 ---
    line_candidates: list[float] = []
    # --- A1b: 大きな矩形（帯の外枠）候補 ---
    frame_candidates: list[float] = []
    try:
        drawings = page.get_drawings()
    except Exception:
        drawings = []
    for d in drawings:
        r = d.get("rect")
        if r is None:
            continue
        # 細い水平罫線
        if (
            r.width >= rect.width * HLINE_MIN_WIDTH_RATIO
            and r.height <= HLINE_MAX_THICKNESS
            and r.y0 >= search_top
        ):
            line_candidates.append(r.y0)
        # 大きな矩形（帯の枠線そのもの）
        if (
            r.width >= rect.width * OBI_FRAME_MIN_WIDTH_RATIO
            and r.height >= OBI_FRAME_MIN_HEIGHT
            and r.y0 >= search_top
        ):
            frame_candidates.append(r.y0)

    # --- A2: キーワード候補（免責文除外） ---
    try:
        blocks = page.get_text("blocks")
    except Exception:
        blocks = []
    kw_candidates: list[float] = []
    for b in blocks:
        if len(b) < 5:
            continue
        x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4]
        if y0 < search_top:
            continue
        if _is_disclaimer(text):
            continue
        if any(kw in text for kw in OBI_KEYWORDS):
            kw_candidates.append(y0)

    # 候補を統合
    raw_candidates = line_candidates + frame_candidates + kw_candidates
    if not raw_candidates:
        # D: ベクター検出が空 → ピクセル解析にフォールバック
        # 画像1枚で構成されたページ（マイソク画像をそのまま貼っただけ等）はここに来る。
        pix_res = _detect_obi_top_by_pixels(page)
        if pix_res is not None:
            obi_top_raw_pix, content_bottom_pix = pix_res
            gap = obi_top_raw_pix - content_bottom_pix
            if gap <= MIN_GAP_FROM_CONTENT * 2:
                cut_y = content_bottom_pix + min(
                    MIN_GAP_FROM_CONTENT, gap / 2.0 if gap > 0 else 0
                )
            else:
                cut_y = (content_bottom_pix + obi_top_raw_pix) / 2.0
            return max(min(cut_y, rect.y1), rect.y0)
        # E: 最終フォールバック（下から固定比率）
        return rect.y1 - rect.height * FALLBACK_BAND_RATIO

    obi_top_raw = min(raw_candidates)

    # --- B: obi_top_raw より上の本文コンテンツの最下端 ---
    content_bottom = rect.y0
    # テキストブロック
    for b in blocks:
        if len(b) < 5:
            continue
        x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4]
        if not text.strip():
            continue
        if y1 <= obi_top_raw - 0.1:
            content_bottom = max(content_bottom, y1)
    # 図形（drawings）— 帯候補そのもの（罫線・枠）はカウントしない
    obi_marker_ys = set(line_candidates) | set(frame_candidates)
    for d in drawings:
        r = d.get("rect")
        if r is None:
            continue
        if r.y0 in obi_marker_ys:
            continue
        if r.y1 <= obi_top_raw - 0.1:
            content_bottom = max(content_bottom, r.y1)

    # --- C: すき間の中点で切る ---
    gap = obi_top_raw - content_bottom
    if gap <= MIN_GAP_FROM_CONTENT * 2:
        # すき間が狭い: できる限り content を残す
        cut_y = content_bottom + min(MIN_GAP_FROM_CONTENT, gap / 2.0 if gap > 0 else 0)
    else:
        # 中点で切る
        cut_y = (content_bottom + obi_top_raw) / 2.0

    # 安全クリップ
    cut_y = max(min(cut_y, rect.y1), rect.y0)
    return cut_y


# ============================================================
# 1ページ分の再配置
# ============================================================

def render_page_without_obi(
    src_doc: "fitz.Document",
    src_page: "fitz.Page",
    out_doc: "fitz.Document",
) -> None:
    """
    ページ全体を A4 横サイズの新規ページにアスペクト比維持で中央配置し、
    検出した業者帯領域 (cut_y 〜 page bottom) を白い矩形で上書きする。

    旧実装は「帯を切った残りの矩形」を A4 横に letterbox していたため、
    元から A4 横サイズの入力でも切断後の縦横比が変わり上下に余白が生じて
    本文が圧縮されて見えるという欠点があった。本実装は

      (1) ページ全体を A4 横に配置 → A4 横入力なら等倍でそのまま
      (2) 業者帯部分だけを白塗りで覆い隠す

    という二段構えで、本文の表示位置・スケールを保ったまま帯だけ消す。
    入力ページサイズが A4 横以外（例: 1957×1384pt）の場合は、ページ全体を
    A4 横に縮小し、業者帯領域も同じスケールで白塗りする。
    """
    rect = src_page.rect
    obi_top = detect_obi_top(src_page)
    # 念のため上限・下限でクリップ（ページ下60%は越えない）
    obi_top = max(min(obi_top, rect.y1), rect.y1 - rect.height * 0.6)

    # 出力ページ作成（白背景）
    new_page = out_doc.new_page(width=TARGET_W, height=TARGET_H)
    new_page.draw_rect(
        fitz.Rect(0, 0, TARGET_W, TARGET_H),
        color=WHITE,
        fill=WHITE,
        overlay=False,
    )

    src_w = rect.width
    src_h = rect.height
    if src_w <= 0 or src_h <= 0:
        return

    # (1) ページ全体を A4 横にアスペクト比維持で中央配置
    scale = min(TARGET_W / src_w, TARGET_H / src_h)
    dst_w = src_w * scale
    dst_h = src_h * scale
    dst_x0 = (TARGET_W - dst_w) / 2.0
    dst_y0 = (TARGET_H - dst_h) / 2.0
    dst_rect = fitz.Rect(dst_x0, dst_y0, dst_x0 + dst_w, dst_y0 + dst_h)
    new_page.show_pdf_page(
        dst_rect,
        src_doc,
        src_page.number,
        clip=rect,
    )

    # (2) 業者帯領域を白塗り
    # ソース座標 (obi_top, rect.y1) を出力ページ座標に変換
    obi_top_dst = dst_y0 + (obi_top - rect.y0) * scale
    obi_bot_dst = dst_y0 + dst_h  # = page 下端の出力座標
    if obi_bot_dst > obi_top_dst:
        new_page.draw_rect(
            fitz.Rect(dst_x0, obi_top_dst, dst_x0 + dst_w, obi_bot_dst),
            color=WHITE,
            fill=WHITE,
            overlay=True,
        )


# ============================================================
# 全体処理
# ============================================================

def process_pdfs(input_paths: list[Path], output_path: Path) -> None:
    """
    1つ以上の入力PDFを順に処理し、全ページを A4 横サイズで1つの出力PDFに書き出す。
    複数入力時は、入力の順に出力PDFへページを連結する。
    """
    if not input_paths:
        raise ValueError("入力PDFが1つも指定されていません")

    for p in input_paths:
        if not p.exists():
            raise FileNotFoundError(f"入力PDFが見つかりません: {p}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    out_doc = fitz.open()
    try:
        for in_path in input_paths:
            src_doc = fitz.open(in_path)
            try:
                for page in src_doc:
                    render_page_without_obi(src_doc, page, out_doc)
            finally:
                src_doc.close()
        out_doc.save(output_path, garbage=4, deflate=True)
    finally:
        out_doc.close()


# 後方互換: 単一入力用の関数
def process_pdf(input_path: Path, output_path: Path) -> None:
    process_pdfs([input_path], output_path)


# ============================================================
# CLI
# ============================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "マイソクPDFの業者帯を白塗りで除去し、全ページをA4横サイズに統一します。"
            " 複数の入力PDFを指定すると、それらを連結した1つの出力PDFを生成します。"
        ),
    )
    p.add_argument("input", type=Path, nargs="+", help="入力PDFファイル（複数指定可・指定順に連結）")
    p.add_argument(
        "--out", "-o",
        type=Path, required=True,
        help="出力PDFファイルパス",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        process_pdfs(args.input, args.out)
    except Exception as e:
        sys.stderr.write(f"[obi_keshi] エラー: {e}\n")
        return 1
    print(f"[obi_keshi] 完了: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
