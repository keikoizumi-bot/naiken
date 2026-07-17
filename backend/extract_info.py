"""
extract_info.py — マイソクPDFから物件情報をヒューリスティックに抽出する。

マイソクは表組みでPDF内のテキスト順序が崩れているため、
「ラベル語の右隣にある語」を座標（bbox）ベースで拾う方式を主とし、
住所・路線などパターンが明確なものは正規表現で補完する。

app.py / app_dev.py の /upload から呼ぶ。best-effort（取れない項目は空文字）。
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

import fitz  # PyMuPDF


# ---------------------------------------------------------------
# 座標ベース: ラベル語の右側にある語を行帯（y範囲）で収集
# ---------------------------------------------------------------

def _words(page) -> list:
    """(x0,y0,x1,y1,text) のリスト"""
    return [(w[0], w[1], w[2], w[3], w[4]) for w in page.get_text("words")]


def _label_match(text: str, label: str) -> bool:
    """ラベル語の一致判定。「名称」は「マンション・名称」のような複合表記にも一致させる"""
    t = text.rstrip("：:")
    return t == label or t.startswith(label) or t.endswith("・" + label) or t.endswith(label)


def _value_right_of(words, label: str, max_gap: float = 90.0, stop_words=()) -> str:
    """
    label に一致（前方一致・末尾の：:は無視）する語を探し、
    同じ行帯（y中心が label の高さ内）で右側にある語を x順に連結して返す。
    語間の水平ギャップが max_gap を超えたら打ち切り（隣の列に入らないため）。
    """
    cands = [w for w in words if _label_match(w[4], label)]
    for lw in cands:
        ly = (lw[1] + lw[3]) / 2.0
        lh = max(lw[3] - lw[1], 6.0)
        row = [w for w in words
               if w is not lw
               and w[0] >= lw[2] - 2
               and abs((w[1] + w[3]) / 2.0 - ly) <= lh * 0.75]
        row.sort(key=lambda w: w[0])
        out, last_x1 = [], lw[2]
        for w in row:
            if w[0] - last_x1 > max_gap:
                break
            t = w[4]
            if t.rstrip("：:") in stop_words:
                break
            out.append(t)
            last_x1 = w[2]
        val = "".join(out).strip()
        if val:
            return val
    return ""


def _value_below(words, label: str, max_gap: float = 90.0, stop_words=()) -> str:
    """
    label の直下（セル内下段）にある語を拾う。
    ヘッダーセルの下に値が置かれるマイソク（物件名/価格など）向け。
    探索窓: y は label 下端から28pt以内、x は label 左端-12pt〜右端+240pt。
    見つけた語を起点に、同じ行帯の語を x順に連結する。
    """
    cands = [w for w in words if _label_match(w[4], label)]
    for lw in cands:
        seeds = [w for w in words
                 if w[1] >= lw[3] - 2
                 and w[1] - lw[3] <= 28
                 and w[0] >= lw[0] - 12
                 and w[0] - lw[2] <= 240]
        if not seeds:
            continue
        # 同じセル内の値を優先: ラベルとの水平距離 → 上から の順で選ぶ
        seed = min(seeds, key=lambda w: (max(0.0, w[0] - lw[2], lw[0] - w[2]), w[1]))
        sy = (seed[1] + seed[3]) / 2.0
        sh = max(seed[3] - seed[1], 6.0)
        row = [w for w in words
               if w[0] >= seed[0] - 2
               and abs((w[1] + w[3]) / 2.0 - sy) <= sh * 0.8]
        row.sort(key=lambda w: w[0])
        out, last_x1 = [], None
        for w in row:
            if last_x1 is not None and w[0] - last_x1 > max_gap:
                break
            t = w[4]
            if t.rstrip("：:") in stop_words:
                break
            out.append(t)
            last_x1 = w[2]
        val = "".join(out).strip()
        if val:
            return val
    return ""


def _clean(s: str) -> str:
    s = re.sub(r"[（(]税込[)）]", "", s)
    return re.sub(r"[ \t　]+", "", s).strip(" :：\n")


# ---------------------------------------------------------------
# 抽出本体
# ---------------------------------------------------------------

_STOP_LABELS = (
    "物件種別", "物件名", "価格", "所在", "所在地", "交通", "専有面積", "バルコニー",
    "その他", "構造", "建築", "築年月", "総戸数", "分譲会社", "施工会社", "設計会社",
    "管理会社", "管理形態", "管理費", "修繕積立金", "駐車場", "自転車", "バイク",
    "設備", "現況", "引渡時期", "ペット", "権利", "敷地面積", "共有持分", "用途地域",
)


_NAME_KEYWORDS = ("タワー", "レジデンス", "マンション", "ハウス", "コート", "ヒルズ",
                  "パレス", "メゾン", "コーポ", "ガーデン", "シティ", "プレイス",
                  "ステージ", "パーク", "テラス")

# 業者名・店舗名らしき語（物件名として不採用）
_NAME_NG = re.compile(r"仲介|プラザ|ステップ|リハウス|センター|不動産|営業所|株式会社|住友|三井の")


def _name_by_biggest(words) -> str:
    """
    ラベルが無いマイソク向け: ページ上の大きい文字の行を物件名候補とする。
    文字がバラけている（1文字=1語）レイアウトに備え、まず行帯にクラスタして結合。
    建物名らしいキーワードを含む行を優先し、無ければ最上部の大きい行。
    """
    cands = [w for w in words
             if w[4].strip()
             and not re.search(r"[0-9０-９]", w[4])
             and not re.search(r"万円|徒歩|駅|円|㎡|平米|会社|〒|TEL|FAX|免許|取引|媒介|仲介|プラザ|ステップ|リハウス|センター|不動産|営業所", w[4])]
    if not cands:
        return ""
    # 行帯クラスタリング
    cands.sort(key=lambda w: (w[1], w[0]))
    lines = []
    for w in cands:
        h = max(w[3] - w[1], 6.0)
        placed = False
        for ln in lines:
            if abs(w[1] - ln["y0"]) <= h * 0.7:
                ln["ws"].append(w)
                placed = True
                break
        if not placed:
            lines.append({"y0": w[1], "ws": [w]})
    rows = []
    for ln in lines:
        ws = sorted(ln["ws"], key=lambda w: w[0])
        txt = "".join(w[4] for w in ws).strip()
        if len(txt) < 4:
            continue
        if _NAME_NG.search(txt):   # 業者フッター等の行は除外
            continue
        rows.append({"txt": txt, "h": max(w[3] - w[1] for w in ws), "y": ln["y0"]})
    if not rows:
        return ""
    hmax = max(r["h"] for r in rows)
    big = [r for r in rows if r["h"] >= hmax * 0.8]
    kw = [r for r in big if any(k in r["txt"] for k in _NAME_KEYWORDS)]
    pool = kw or big
    return min(pool, key=lambda r: r["y"])["txt"]


def _clean_name(s: str) -> str:
    """物件名の後処理: 隣接セルから紛れ込む「48階」等の階数・ラベル語を除去"""
    s = _clean(s)
    s = re.sub(r"[0-9０-９]+階.*$", "", s)
    s = re.sub(r"(所在|交通|価格|名称|物件名|物件種別)+$", "", s)
    return s.strip()


def _extract_from_page(words, text, is_ocr: bool = False) -> dict:
    """1ページ分の words/text から物件情報を抽出する"""
    # OCRページは日本語がスペース分割されるため、全スペース除去版で正規表現を掛ける
    flat = re.sub(r"\s+", "", text) if is_ocr else re.sub(r"\s+", " ", text)

    out = {}

    # --- 物件名（「物件名」→「名称」ラベルの右/直下 → 大きい文字フォールバック） ---
    name = ""
    for label in ("物件名", "名称"):
        name = (_value_right_of(words, label, stop_words=_STOP_LABELS)
                or _value_below(words, label, stop_words=_STOP_LABELS))
        if name:
            break
    if not _clean_name(name):
        name = _name_by_biggest(words)
    name = _clean_name(name)
    # OCRページは名前の信頼性が低い: 建物名らしいキーワードを含み、業者名らしくない場合のみ採用
    if is_ocr and name and (not any(k in name for k in _NAME_KEYWORDS) or _NAME_NG.search(name)):
        name = ""
    out["name"] = name

    # --- 価格（ラベルの右 → 数字が無ければ直下。「25,000 万円」「（税込）」の混在に対応） ---
    price_raw = _value_right_of(words, "価格", stop_words=_STOP_LABELS)
    if not re.search(r"[0-9]", price_raw):
        price_raw = _value_below(words, "価格", stop_words=_STOP_LABELS)
    m = re.search(r"([0-9][0-9,，\.]*)\s*(億)?\s*(万?円)?", _clean(price_raw))
    price = ""
    if m and m.group(1):
        num = m.group(1).replace("，", ",")
        unit = (m.group(2) or "") + (m.group(3) or "")
        price = num + (unit if unit else "万円")
    if not price:
        # ラベル検出不可（OCRページ等）: 全文から「4億1,800万円」形式を拾う
        m = re.search(r"([0-9][0-9,，]*億)?\s*([0-9][0-9,，]*)\s*万円", flat)
        if m:
            price = (m.group(1) or "").replace("，", ",") + m.group(2).replace("，", ",") + "万円"
    elif "億" not in price:
        # ラベル値が断片（例: OCRで「4」だけ）のことがある: 全文に「N億…」があればそちらを優先
        m = re.search(r"([0-9][0-9,，]*)億\s*([0-9][0-9,，]*)?\s*万?円?", flat)
        if m:
            price = m.group(1).replace("，", ",") + "億" + (
                m.group(2).replace("，", ",") + "万円" if m.group(2) else "円")
    out["price"] = price

    # --- 住所（正規表現: 「◯◯区/市…丁目…」パターン） ---
    addr = ""
    m = re.search(
        r"((?:東京都|北海道|京都府|大阪府|[一-龥]{2,3}県)?"
        r"[一-龥ぁ-んァ-ヶa-zA-Z0-9]{1,8}[市区町村]"
        r"[一-龥ぁ-んァ-ヶa-zA-Z0-9]{0,15}"
        r"(?:\d+丁目)?[\d\-－ー]*)",
        flat,
    )
    if m:
        cand = m.group(1)
        # 「渋谷区恵比寿西1-20-6」(業者住所) より物件住所を優先するため、
        # 所在ラベル近く or 数字を含む最長候補を選ぶ
        cands = re.findall(
            r"(?:東京都|北海道|京都府|大阪府|[一-龥]{2,3}県)?"
            r"[一-龥ぁ-んァ-ヶa-zA-Z0-9]{1,8}[市区町村]"
            r"[一-龥ぁ-んァ-ヶa-zA-Z0-9]{0,15}(?:\d+丁目)?[\d\-－ー]*",
            flat,
        )
        # 業者情報らしいもの（〒直後・営業所/会社の近く）を除外
        filtered = []
        for c in cands:
            idx = flat.find(c)
            ctx = flat[max(0, idx - 12):idx]
            if "〒" in ctx or "営業所" in flat[idx:idx + len(c) + 6]:
                continue
            if not re.search(r"\d", c):  # 番地なしは住所とみなさない
                continue
            if re.search(r"所有者|管理|月額|専有|徒歩|売主|分譲|施工|新築", c):  # 誤マッチ・業者情報の除外
                continue
            # OCRノイズ等が先頭に付いた場合、都道府県表記から切り出す（例:「以計東京都港区…」）
            tm = re.search(r"(?:東京都|北海道|京都府|大阪府|[一-龥]{2,3}県)[\s\S]*$", c)
            filtered.append(tm.group(0) if tm else c)
        # 都道府県から始まる候補を優先し、その中で最長を採用
        pref = [c for c in filtered
                if re.match(r"(?:東京都|北海道|京都府|大阪府|[一-龥]{2,3}県)", c)]
        pool = pref or filtered or cands
        addr = max(pool, key=len) if pool else cand
    out["addr"] = _clean(addr)

    # --- 交通（「◯◯線「駅名」駅 徒歩◯分」） ---
    access = ""
    m = re.search(r"([一-龥ァ-ヶa-zA-Z0-9]*(?:線|ライン)?「[^」]+」駅?\s*徒歩\s*\d+\s*分)", flat)
    if m:
        access = re.sub(r"\s+", " ", m.group(1)).replace("徒歩 ", "徒歩").strip()
    # OCRページは閉じカッコ欠落等で周辺文字を巻き込みやすい: 長すぎる値は破棄
    if is_ocr and len(access) > 30:
        access = ""
    out["access"] = access

    # --- 専有面積＋間取り ---
    area_raw = _value_right_of(words, "専有面積", stop_words=_STOP_LABELS)
    area_num = _first_num_unit(area_raw) or _first_num_unit(flat, near="専有面積")
    layout = ""
    m = re.search(r"\b([1-9][SLDK]{1,4}(?:\+[A-Z]{1,4})?)\b", flat)
    if m:
        layout = m.group(1)
    if area_num and layout:
        out["area"] = f"{area_num}㎡ / {layout}"
    else:
        out["area"] = (f"{area_num}㎡" if area_num else layout or "")

    # --- 向き・部屋 ---
    direction = ""
    m = re.search(r"([東西南北]{1,2})\s*向き", flat)
    if m:
        direction = m.group(1) + "向き"
    floor_part = ""
    m = re.search(r"([0-9]+)\s*階部分", flat)
    if m:
        floor_part = m.group(1) + "階"
    out["unit"] = " ".join(x for x in [floor_part, direction] if x)

    # --- 築年月 ---
    year = _value_right_of(words, "建築", stop_words=_STOP_LABELS)
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", year or flat)
    out["year"] = f"{m.group(1)}年{m.group(2)}月" if m else ""

    # --- 階数（「35階／48階建」形式に整形。「地下1階建」誤取得を避け最大値を採用） ---
    b_nums = [int(x) for x in re.findall(r"地上\s*([0-9]+)\s*階", flat)] or \
             [int(x) for x in re.findall(r"([0-9]+)\s*階建", flat)]
    p_ = re.search(r"([0-9]+)\s*階部分", flat)
    if b_nums and p_:
        out["floors"] = f"{p_.group(1)}階／{max(b_nums)}階建"
    elif b_nums:
        out["floors"] = f"{max(b_nums)}階建"
    else:
        out["floors"] = ""

    # --- 管理費 ---
    mgmt_raw = _value_right_of(words, "管理費", stop_words=("修繕積立金",) + _STOP_LABELS)
    m = re.search(r"([0-9][0-9,，]*)\s*円", mgmt_raw)
    if not m:
        m = re.search(r"管理費[^0-9円]{0,14}([0-9][0-9,，]*)\s*円", flat)
    out["mgmt"] = f"月額{m.group(1).replace('，', ',')}円" if m else ""

    return {k: (v or "") for k, v in out.items()}


def _filled_count(d: dict) -> int:
    return sum(1 for v in d.values() if v)


def _ocr_page(page) -> tuple[list, str] | None:
    """
    テキストレイヤーの無いスキャン画像ページ用OCR。
    tesseract（+日本語データ）と pytesseract/Pillow が入っていれば
    (words, text) を返す。無ければ None（呼び出し側でスキップ）。
    Cloud Run 用 Dockerfile では tesseract-ocr / tesseract-ocr-jpn を導入済み。
    """
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return None
    try:
        zoom = 220 / 72.0   # OCR精度優先の高解像度（低いと住所等の小さな文字を読めない）
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        data = pytesseract.image_to_data(
            img, lang="jpn", output_type=pytesseract.Output.DICT
        )
    except Exception:
        return None
    words, lines = [], []
    for i, t in enumerate(data.get("text", [])):
        t = (t or "").strip()
        if not t:
            continue
        x, y = data["left"][i], data["top"][i]
        w, h = data["width"][i], data["height"][i]
        words.append((x / zoom, y / zoom, (x + w) / zoom, (y + h) / zoom, t))
        lines.append(t)
    if not words:
        return None
    return words, "\n".join(lines)


def extract_all(pdf_bytes: bytes) -> list[dict]:
    """
    複数物件が混在するPDF（物件リスト等）向け:
    ページごとに抽出し、物件のリストを返す。
      - テキストの無いページ（写真のみ等）はスキップ
      - 物件名が取れないページは直前の物件の補完とみなし、空欄のみ埋める
      - 各要素に page（1始まりのページ番号）を付与
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        results: list[dict] = []
        for i, page in enumerate(doc):
            text = page.get_text() or ""
            words = None
            is_ocr = False
            if len(text.strip()) < 30:
                # テキストレイヤーが無い（スキャン画像）ページ → OCRを試す
                ocr = _ocr_page(page)
                if ocr is None:
                    continue                 # OCR不可なら従来どおりスキップ
                words, text = ocr
                is_ocr = True
            info = _extract_from_page(
                words if words is not None else _words(page), text, is_ocr=is_ocr)
            if _filled_count(info) < 2:     # 物件情報がほぼ無いページ
                continue
            if is_ocr:
                # スキャン1枚＝独立した物件とみなす（前の物件へはマージしない）
                info["page"] = i + 1
                results.append(info)
            elif info.get("name"):
                info["page"] = i + 1
                results.append(info)
            elif results:
                # 名無しページ → 直前の物件の空欄を補完（2ページ構成のマイソク）
                prev = results[-1]
                for k, v in info.items():
                    if v and not prev.get(k):
                        prev[k] = v
            else:
                info["page"] = i + 1
                results.append(info)
        return results
    finally:
        doc.close()


def extract_info(pdf_bytes: bytes) -> dict:
    """単一物件向けの互換API: 最も情報の揃った1件を返す"""
    all_ = extract_all(pdf_bytes)
    if not all_:
        return {}
    return max(all_, key=_filled_count)


def geocode_jp(addr: str) -> tuple[float, float] | None:
    """
    国土地理院の住所検索API（無料・キー不要）で住所→(lat,lng)。
    Google Geocoding API が使えない環境でも地図ピンを立てられるようにする。
    """
    if not addr:
        return None
    try:
        url = ("https://msearch.gsi.go.jp/address-search/AddressSearch?q="
               + urllib.parse.quote(addr))
        req = urllib.request.Request(url, headers={"User-Agent": "equitas-viewing/1.0"})
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode("utf-8"))
        if data and data[0].get("geometry", {}).get("coordinates"):
            lng, lat = data[0]["geometry"]["coordinates"]
            return float(lat), float(lng)
    except Exception:
        pass
    return None


def _first_num_unit(s: str, near: str | None = None) -> str:
    """文字列から「104.6平米/㎡/m2」の数値部分を取り出す"""
    if not s:
        return ""
    if near:
        idx = s.find(near)
        if idx >= 0:
            s = s[idx:idx + 60]
        else:
            return ""
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:㎡|平米|m2|m²|m(?![a-zA-Z0-9]))", s)
    return m.group(1) if m else ""
