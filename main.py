"""
BingX Trading Bot - 실물거래 메인 실행
"""

import json
import logging
import os
import sys
import time

from config import MAIN_LOOP_INTERVAL_SEC, LOG_LEVEL, TOTAL_CAPITAL, LEVERAGE
from strategy_engine import StrategyEngine

STATE_FILE        = os.path.join(os.path.dirname(__file__), "state.json")
ENGINE_STATE_FILE = os.path.join(os.path.dirname(__file__), "engine_state.json")

# ────────────────────────────────────────────────
#  로거 설정
# ────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot_real.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def _check_api_keys() -> bool:
    """API 키 설정 여부 확인"""
    api_key    = os.getenv("BINGX_API_KEY", "")
    secret_key = os.getenv("BINGX_SECRET_KEY", "")
    if not api_key or not secret_key:
        print()
        print("  [오류] .env 파일에 API 키가 설정되지 않았습니다.")
        print("  BINGX_API_KEY 와 BINGX_SECRET_KEY 를 입력해 주세요.")
        return False
    if api_key == "여기에_API_KEY_입력" or secret_key == "여기에_SECRET_KEY_입력":
        print()
        print("  [오류] .env 파일에 실제 API 키를 입력해 주세요.")
        return False
    return True


def _show_current_state():
    """현재 저장된 거래 기록 요약 출력"""
    if not os.path.exists(STATE_FILE):
        print(f"  저장된 기록 없음  (초기 자본 ${TOTAL_CAPITAL:.0f})")
        return
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        wins   = d.get("win_count", 0)
        losses = d.get("loss_count", 0)
        pnl    = d.get("total_pnl", 0.0)
        total  = wins + losses
        pos    = d.get("position")
        print(f"  누적 손익  : ${pnl:+.2f}")
        print(f"  거래 횟수  : {total}회  ({wins}W / {losses}L)")
        if pos:
            print(f"  보유 포지션: {pos['symbol']} ({pos['trend']})  "
                  f"투입 ${pos['total_invested']:.2f}  단계 {pos['avg_down_step']}")
        else:
            print("  보유 포지션: 없음")
    except Exception as e:
        print(f"  기록 읽기 실패: {e}")


def _reset_state():
    """거래 기록 초기화"""
    empty_state = {"total_pnl": 0.0, "win_count": 0, "loss_count": 0,
                   "closed_trades": [], "position": None}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(empty_state, f, ensure_ascii=False, indent=2)
    with open(ENGINE_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"blocked_symbols": {}}, f, ensure_ascii=False, indent=2)
    print(f"  ✔ 초기화 완료 — ${TOTAL_CAPITAL:.0f} 에서 새로 시작합니다.")


def _startup_menu() -> bool:
    """봇 시작 전 선택 메뉴"""
    print()
    print("=" * 55)
    print("  BingX Trading Bot  ★ 실물거래 ★")
    print(f"  초기 자본: ${TOTAL_CAPITAL:.0f}  레버리지: {LEVERAGE}배")
    print("=" * 55)
    print()
    print("  ⚠  이 봇은 실제 자금을 사용합니다. 신중히 운영하세요.")
    print()
    print("  [현재 기록]")
    _show_current_state()
    print()
    print("  시작 방법을 선택하세요:")
    print(f"    1) 이어서 시작  (기존 기록 유지)")
    print(f"    2) 초기화 후 시작  (${TOTAL_CAPITAL:.0f} 리셋)")
    print("    3) 종료")
    print()

    while True:
        choice = input("  선택 (1/2/3) > ").strip()
        if choice == "1":
            print("  → 기존 기록 유지하고 시작합니다.")
            return True
        elif choice == "2":
            confirm = input("  기록을 모두 삭제합니다. 정말요? (y/N) > ").strip().lower()
            if confirm == "y":
                _reset_state()
                return True
            else:
                print("  취소됐습니다. 다시 선택하세요.")
        elif choice == "3":
            print("  종료합니다.")
            return False
        else:
            print("  1, 2, 3 중 하나를 입력하세요.")


def main():
    from dotenv import load_dotenv
    load_dotenv()

    if not _check_api_keys():
        sys.exit(1)

    if not _startup_menu():
        sys.exit(0)

    logger.info("=" * 60)
    logger.info("  BingX Trading Bot (실물거래) 시작")
    logger.info(f"  초기 자본: ${TOTAL_CAPITAL:.0f}  레버리지: {LEVERAGE}배")
    logger.info("=" * 60)

    engine = StrategyEngine()
    status_interval  = 60
    last_status_time = 0.0

    try:
        while True:
            now = time.time()

            try:
                engine.tick()
            except Exception as e:
                logger.error(f"틱 처리 오류: {e}", exc_info=True)

            if now - last_status_time >= status_interval:
                engine.print_status()
                last_status_time = now

            time.sleep(MAIN_LOOP_INTERVAL_SEC)

    except KeyboardInterrupt:
        logger.info("봇 종료 (Ctrl+C)")
        engine.print_status()
    except Exception as e:
        logger.critical(f"치명적 오류: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
