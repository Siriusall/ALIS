from __future__ import annotations

import os

REQUEST_DELAY = float(os.getenv("WEB_FALLBACK_DELAY", "0.35"))
MAX_RETRIES = int(os.getenv("WEB_FALLBACK_MAX_RETRIES", "3"))
TESSERACT_CMD = os.getenv("TESSERACT_CMD")
OCR_LANG = os.getenv("OCR_LANG", "chi_sim+eng")
