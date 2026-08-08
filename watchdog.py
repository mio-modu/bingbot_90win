"""
불사조 봇 감시자 (Phoenix Watchdog)
- 봇 충돌 시 자동 재시작
- 네트워크 끊김 감지 → 복구 후 자동 재시작
- PC 부팅 후 자동 시작 (setup_autostart.bat 등록 후)
"""

import os
import sys
import time
import socket
import subprocess
import logging
import signal
import argparse

# Windows UTF-8
if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    # 절전 모드 방지: 이 프로세스가 떠있는 동안 유휴 타이머로 인한 절전 진입 차단
    # (전원코드가 빠져도 유휴 절전으로 봇이 죽는 사고 방지용 이중 안전장치)
    ES_CONTINUOUS       = 0x80000000
    ES_SYSTEM_REQUIRED  = 0x00000001
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

# ── 단일 인스턴스 보장 (중복 실행 방지) ──────────────────────
if sys.platform == "win32":
    import ctypes
    _MUTEX_NAME = "Global\\BingX_Phoenix_Live_v1"   # 실거래 봇 전용 (certain 봇과 분리)
    _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, True, _MUTEX_NAME)
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        print("[WATCHDOG] 이미 실행 중입니다 - 중복 실행 방지 → 종료")
        sys.exit(0)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WATCHDOG] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(BASE_DIR, "watchdog.log"), encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)

# ── 설정 ──────────────────────────────────────────
NET_HOSTS       = [("8.8.8.8", 53), ("1.1.1.1", 53)]  # 구글 DNS, 클라우드플레어
NET_TIMEOUT     = 3     # 연결 타임아웃 (초)
NET_CHECK_SEC   = 5     # 네트워크 점검 주기 (초)
NET_DOWN_KILL   = 30    # 네트워크 불통 이 초 이상 → 봇 강제 종료
RESTART_DELAY   = 15    # 재시작 전 대기 (초)
HEARTBEAT_SEC   = 1800  # 생존 로그 주기 (초) - 상태 전환 없으면 로그가 조용해서 사망처럼 보이는 문제 방지
MIN_RUN_SEC     = 10    # 이 초 미만 실행 후 종료 = 빠른 실패
MAX_QUICK_FAIL  = 5     # 빠른 실패 N회 연속 → 감시자 종료 (무한루프 방지)
# ──────────────────────────────────────────────────

_stop = False
_current_proc = None


def _on_signal(sig, frame):
    global _stop
    logger.info("종료 신호 수신 → 감시자 종료 중...")
    _stop = True
    if _current_proc and _current_proc.poll() is None:
        _current_proc.terminate()


signal.signal(signal.SIGINT, _on_signal)
if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, _on_signal)


def is_network_up() -> bool:
    for host, port in NET_HOSTS:
        try:
            s = socket.create_connection((host, port), timeout=NET_TIMEOUT)
            s.close()
            return True
        except OSError:
            pass
    return False


def wait_for_network() -> bool:
    """네트워크 복구 대기. 감시자 종료 요청 시 False 반환."""
    notified = False
    while not _stop:
        if is_network_up():
            if notified:
                logger.info("★ 네트워크 연결 복구! 봇을 재시작합니다.")
            return True
        if not notified:
            logger.warning("★ 네트워크 끊김 - 재연결 대기 중...")
            notified = True
        time.sleep(NET_CHECK_SEC)
    return False


def run_bot(no_menu: bool = False) -> int:
    """
    봇을 서브프로세스로 실행.
    반환값: 봇 종료 코드 | -999 (감시자 종료) | -2 (네트워크 오류)
    """
    global _current_proc

    args = [sys.executable, "-u", os.path.join(BASE_DIR, "main.py")]
    if no_menu:
        args.append("--no-menu")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    kwargs = {"env": env, "stdin": subprocess.DEVNULL}
    if no_menu and sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW: 백그라운드 모드에서 콘솔 창 억제

    _current_proc = subprocess.Popen(args, **kwargs)
    net_down_since = None
    last_heartbeat = time.time()

    while True:
        if _stop:
            _current_proc.terminate()
            try:
                _current_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _current_proc.kill()
            _current_proc = None
            return -999

        ret = _current_proc.poll()
        if ret is not None:
            _current_proc = None
            return ret

        # 네트워크 상태 확인
        if is_network_up():
            net_down_since = None
        else:
            if net_down_since is None:
                net_down_since = time.time()
                logger.warning("★ 네트워크 끊김 감지!")
            elif time.time() - net_down_since >= NET_DOWN_KILL:
                logger.warning(
                    f"★ 네트워크 {NET_DOWN_KILL}초 이상 불통 → 봇 강제 종료 후 재시작 대기"
                )
                _current_proc.terminate()
                try:
                    _current_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _current_proc.kill()
                _current_proc = None
                return -2

        now = time.time()
        if now - last_heartbeat >= HEARTBEAT_SEC:
            logger.info(f"정상 감시 중 (봇 PID {_current_proc.pid}, 네트워크 정상)")
            last_heartbeat = now

        time.sleep(NET_CHECK_SEC)


def _countdown(seconds: int, msg: str):
    for i in range(seconds, 0, -1):
        if _stop:
            break
        sys.stdout.write(f"\r  {msg} {i:2d}초...  ")
        sys.stdout.flush()
        time.sleep(1)
    sys.stdout.write("\r" + " " * 35 + "\r")
    sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--auto", action="store_true",
                        help="자동 시작 모드 (PC 부팅 시 메뉴 생략)")
    pargs, _ = parser.parse_known_args()

    print()
    print("=" * 55)
    print("  🔥 불사조 봇 감시자 (Phoenix Watchdog) 🔥")
    print("  네트워크 끊김 / 충돌 / PC 재부팅 → 자동 재시작")
    print("  완전 종료: Ctrl+C")
    print("=" * 55)
    print()

    quick_fail_count = 0
    restart_count    = 0
    first_run        = True

    while not _stop:
        # 첫 실행 + --auto 아님 → 메뉴 있는 일반 시작
        use_menu = first_run and not pargs.auto
        first_run = False

        # 재시작 전 네트워크 확인
        if not is_network_up():
            if not wait_for_network():
                break
            _countdown(RESTART_DELAY, "네트워크 안정화 대기")

        if restart_count > 0:
            logger.info(f"[재시작 #{restart_count}] 봇 시작 (자동 이어하기)")
        else:
            logger.info(f"봇 시작 {'(메뉴)' if use_menu else '(자동 이어하기)'}")

        start_time  = time.time()
        exit_code   = run_bot(no_menu=not use_menu)
        run_seconds = time.time() - start_time

        # 감시자 종료 요청
        if _stop or exit_code == -999:
            break

        # 사용자가 정상 종료 (Ctrl+C 또는 메뉴에서 '종료' 선택)
        # --auto 모드에서는 정상 종료도 재시작 (PC 부팅 자동실행 시 무한 유지)
        if exit_code == 0 and not pargs.auto:
            logger.info("봇 정상 종료 → 감시자도 종료합니다.")
            break
        elif exit_code == 0 and pargs.auto:
            logger.warning("봇 정상 종료 감지 (--auto 모드) → 재시작합니다.")

        # 빠른 실패 감지 (크래시루프 방지)
        if run_seconds < MIN_RUN_SEC:
            quick_fail_count += 1
            logger.error(
                f"빠른 실패 {quick_fail_count}/{MAX_QUICK_FAIL} "
                f"(실행 {run_seconds:.1f}초, 종료코드 {exit_code})"
            )
            if quick_fail_count >= MAX_QUICK_FAIL:
                logger.critical(
                    f"★ 연속 빠른 실패 {MAX_QUICK_FAIL}회 → 감시자 종료\n"
                    "  오류 원인을 확인 후 수동으로 다시 시작하세요."
                )
                break
        else:
            quick_fail_count = 0

        restart_count += 1

        if exit_code == -2:
            # 네트워크 오류 → 복구 대기
            if not wait_for_network():
                break
            _countdown(RESTART_DELAY, "재시작 대기")
        else:
            logger.warning(
                f"봇 비정상 종료 (코드: {exit_code}, "
                f"실행: {run_seconds:.0f}초) → 재시작 대기"
            )
            _countdown(RESTART_DELAY, "재시작 대기")

    logger.info("감시자 종료 완료.")


if __name__ == "__main__":
    main()
