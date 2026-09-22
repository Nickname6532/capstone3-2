"""Vercel 서버리스 진입점. 실제 앱 구현은 src/app.py에 있다."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from app import app  # noqa: E402
