"""
BingX Trading Bot - 메인 실행
"""

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import signal
import time
import sys

# Windows 터미널 UTF-8 출력 강제
if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import trade_journal
from config import MAIN_LOOP_INTERVAL_SEC, LOG_LEVEL, TOTAL_CAPITAL
from strategy_engine import StrategyEngine

STATE_FILE       = os.path.join(os.path.dirname(__file__), "state.json")
ENGINE_STATE_FILE= os.path.join(os.path.dirname(__file__), "engine_state.json")

# ────────────────────────────────────────────────
#  로거 설정
# ────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler("bot.log", encoding="utf-8", maxBytes=20*1024*1024, backupCount=3)
    ]
)
logger = logging.getLogger(__name__)


def _show_current_state():
    """현재 저장된 거래 기록 요약 출력"""
    if not os.path.exists(STATE_FILE):
        print("  저장된 기록 없음")
        return
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        wins   = d.get("win_count", 0)
        losses = d.get("loss_count", 0)
        pnl    = d.get("total_pnl", 0.0)
        total  = wins + losses
        pos    = d.get("position")
        withdrawn = d.get("total_withdrawn", 0.0)
        w_count   = d.get("withdrawal_count", 0)
        print(f"  누적 손익  : ${pnl:+.2f}")
        if w_count > 0:
            print(f"  총 출금    : ${withdrawn:.2f} ({w_count}회)")
        print(f"  거래 횟수  : {total}회  ({wins}W / {losses}L)")
        if pos:
            print(f"  보유 포지션: {pos['symbol']} ({pos['trend']})  "
                  f"투입 ${pos['total_invested']:.0f}  단계 {pos['avg_down_step']}")
        else:
            print("  보유 포지션: 없음")
    except Exception as e:
        print(f"  기록 읽기 실패: {e}")


def _reset_state():
    """거래 기록 초기화 (state + engine_state)"""
    empty_state = {
        "total_pnl": 0.0, "win_count": 0, "loss_count": 0,
        "closed_trades": [], "position": None,
        "total_withdrawn": 0.0, "withdrawal_count": 0, "withdrawal_history": []
    }
    for path in [STATE_FILE, STATE_FILE + ".bak"]:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(empty_state, f, ensure_ascii=False, indent=2)
    with open(ENGINE_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"blocked_symbols": {}, "consec_wins": {}, "consec_win_blocked": {}},
                  f, ensure_ascii=False, indent=2)
    print(f"  ✔ 초기화 완료 — ${TOTAL_CAPITAL:,.0f} 에서 새로 시작합니다.")


def _input_with_countdown(prompt: str, timeout: int = 5, default: str = "1") -> str:
    """카운트다운 후 기본값 자동 선택 (Windows/Linux 공용)"""
    import threading
    result = [None]
    event = threading.Event()

    def _get():
        try:
            result[0] = input("")
        except EOFError:
            result[0] = default
        event.set()

    t = threading.Thread(target=_get, daemon=True)
    t.start()

    for i in range(timeout, 0, -1):
        sys.stdout.write(f"\r  {prompt}[{i}초 후 '{default}' 자동 선택] ")
        sys.stdout.flush()
        if event.wait(1):
            break

    sys.stdout.write("\r" + " " * 60 + "\r")
    sys.stdout.flush()

    if result[0] is None:
        print(f"  → {timeout}초 경과, '{default}' 자동 선택됨")
        return default
    return result[0].strip()


def _startup_menu():
    """봇 시작 전 선택 메뉴 (TTY가 없으면 자동 이어하기)"""
    print()
    print("=" * 50)
    print("  BingX Trading Bot")
    print("=" * 50)
    print()
    print("  [현재 기록]")
    _show_current_state()
    print()

    # 백그라운드 / 파이프 / 서비스 모드 → 즉시 이어하기
    if not sys.stdin.isatty():
        print("  [자동] 백그라운드 모드 감지 → 이어서 시작합니다.")
        return True

    print("  시작 방법을 선택하세요:")
    print("    1) 이어서 시작  (기존 기록 유지)")
    print(f"    2) 초기화 후 시작  (${TOTAL_CAPITAL:,.0f} 리셋)")
    print("    3) 종료")
    print()

    # 5초 카운트다운 → 자동으로 이어하기 선택
    auto_selected = _input_with_countdown("  선택 (1/2/3) > ", timeout=5, default="1")

    while True:
        choice = auto_selected if auto_selected else input("  선택 (1/2/3) > ").strip()
        auto_selected = None  # 두 번째 루프부터는 직접 입력
        if choice == "1" or choice == "":
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


def _force_exit(sig, frame):
    os._exit(0)


def main():
    signal.signal(signal.SIGINT, _force_exit)

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--no-menu", action="store_true")
    pargs, _ = parser.parse_known_args()

    if pargs.no_menu:
        logger.info("자동 재시작 모드 - 기존 상태 이어서 시작")
        _show_current_state()
    elif not _startup_menu():
        os._exit(0)

    logger.info("=" * 60)
    logger.info("  BingX Trading Bot 시작")
    logger.info("=" * 60)

    # 저널이 아직 없으면 state.json 의 기존 기록을 1회 이관 (분석 이력 확보)
    trade_journal.backfill_from_state()

    engine = StrategyEngine()
    status_interval = 60   # 60초마다 상태 출력
    last_status_time = 0.0

    try:
        while True:
            now = time.time()

            # 메인 전략 틱
            try:
                engine.tick()
            except Exception as e:
                logger.error(f"틱 처리 오류: {e}", exc_info=True)

            # 주기적 상태 출력
            if now - last_status_time >= status_interval:
                engine.print_status()
                last_status_time = now

            # 0.5초씩 나눠 자서 Ctrl+C 즉시 반응
            end = time.time() + MAIN_LOOP_INTERVAL_SEC
            while time.time() < end:
                time.sleep(0.5)

    except KeyboardInterrupt:
        os._exit(0)
    except Exception as e:
        logger.critical(f"치명적 오류: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
