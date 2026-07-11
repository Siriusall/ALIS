from __future__ import annotations

import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DB_PATH = Path(os.getenv("NEWS_DB_PATH", str(DATA_DIR / "news.db")))

REQUEST_DELAY = float(os.getenv("CRAWL_DELAY", "0.35"))
MAX_RETRIES = int(os.getenv("CRAWL_MAX_RETRIES", "3"))
PAGE_SIZE = 100
TESSERACT_CMD = os.getenv("TESSERACT_CMD")
OCR_LANG = os.getenv("OCR_LANG", "chi_sim+eng")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": "https://quote.eastmoney.com/",
}
