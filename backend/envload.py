"""
envload.py — backend/.env を環境変数に読み込む小さなローダー。

秘密情報（APIキー等）はコードに書かず .env に置く。
既に環境変数が設定されている場合は上書きしない（Cloud Run では env 側が優先）。
"""
from __future__ import annotations

import os
from pathlib import Path


def load_dotenv_file(path: Path | None = None) -> None:
    p = path or (Path(__file__).parent / ".env")
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value
