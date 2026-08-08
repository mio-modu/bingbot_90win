"""
거래 저널 (append-only)
─────────────────────
state.json 의 closed_trades 는 최근 200건에서 잘려나가므로 학습용 이력이 소실된다.
이 모듈은 청산될 때마다 JSONL 한 줄을 append 하여 **영구 보존**한다.

  trades.jsonl   : 청산된 거래 1건 = 1줄 (절대 삭제/수정 안 함)

analyze.py 가 이 파일을 읽어 손실 원인을 분석한다.
저널 기록 실패는 절대 매매를 방해하지 않는다 (모든 예외를 삼킴).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

JOURNAL_FILE = os.path.join(os.path.dirname(__file__), "trades.jsonl")

# 로그·리포트 표기용 시간대 (거래소는 UTC, 사용자는 KST)
KST = timezone(timedelta(hours=9))

SCHEMA_VERSION = 1


def _iso(ts: float) -> str:
    """epoch → KST ISO 문자열"""
    try:
        return datetime.fromtimestamp(ts, KST).isoformat(timespec="seconds")
    except Exception:
        return ""


def append(record: dict) -> None:
    """거래 1건을 저널에 append. 실패해도 매매에 영향 없음."""
    try:
        record = dict(record)
        record["v"] = SCHEMA_VERSION
        with open(JOURNAL_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:  # 저널은 부가 기능 — 절대 매매를 막지 않는다
        logger.warning(f"[저널] 기록 실패 (매매에는 영향 없음): {e}")


def load(path: str = JOURNAL_FILE) -> list[dict]:
    """저널 전체를 읽어 리스트로 반환. 깨진 줄은 건너뛴다."""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning(f"[저널] {lineno}번째 줄 파싱 실패 — 건너뜀")
    return out


def backfill_from_state(state_file: str | None = None) -> int:
    """
    저널 도입 이전에 state.json 에 쌓여 있던 closed_trades 를 1회 이관한다.
    시각·MAE/MFE 가 없는 옛 기록이므로 backfill=True 로 표시해 둔다.
    이미 저널이 존재하면 아무것도 하지 않는다 (중복 방지).
    반환값: 이관한 건수
    """
    if os.path.exists(JOURNAL_FILE):
        return 0
    state_file = state_file or os.path.join(os.path.dirname(__file__), "state.json")
    if not os.path.exists(state_file):
        return 0
    try:
        with open(state_file, encoding="utf-8") as f:
            old = json.load(f).get("closed_trades", [])
    except Exception as e:
        logger.warning(f"[저널] backfill 실패: {e}")
        return 0

    for t in old:
        rec = dict(t)
        rec["backfill"] = True   # 시각·MAE/MFE 없음 → 시간대 분석에서 제외
        append(rec)
    if old:
        logger.info(f"[저널] 기존 기록 {len(old)}건을 trades.jsonl 로 이관했습니다.")
    return len(old)
