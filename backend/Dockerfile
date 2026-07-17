FROM python:3.11-slim

# スキャン画像マイソクのOCR用（extract_info._ocr_page）
RUN apt-get update && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-jpn \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依存を先に入れてレイヤキャッシュを効かせる
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリ本体（app.py / drive.py / obi_keshi.py）
COPY . .

# Cloud Run は $PORT を渡してくる（既定 8080）
ENV PORT=8080
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
