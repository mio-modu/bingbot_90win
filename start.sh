#!/bin/bash
# 서버(Linux)에서 직접 실행용 — 포그라운드 실행 (로그 확인 가능)
cd "$(dirname "$0")"
source venv/bin/activate 2>/dev/null || true
python watchdog.py --auto
