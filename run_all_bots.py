"""
6개 봇 일괄 기동 (Multi-Bot Launcher)
─────────────────────────────────────
문제:
  watchdog.py 의 _MUTEX_NAME 이 "Global\\BingX_Phoenix_Certain_v1" 로
  하드코딩돼 있다. 봇 폴더 6개가 같은 레포를 받으면 이름이 전부 같아져,
  먼저 뜬 1대만 살고 나머지 5대는 ERROR_ALREADY_EXISTS(183) 로 즉시 종료된다.
  → 6개를 다 실행해도 실제로는 1개만 돈다.

해결:
  뮤텍스 이름을 폴더 이름에서 자동으로 만들어 폴더마다 달라지게 한다.
  watchdog.py 와 health_check.ps1 이 "같은 규칙"으로 이름을 계산해야 하므로
  둘을 함께 고친다. (한쪽만 고치면 헬스체크가 워치독을 죽은 것으로 오판해
  계속 재시작시켜 중복 기동 사고가 난다.)

실행 순서 (--start 시):
  1) 돌고 있는 워치독·봇을 전부 정지   ← 먼저 멈춰야 안전하다
  2) 폴더마다 뮤텍스 이름을 고유하게 패치 (.bak 백업, 이미 패치됐으면 건너뜀)
  3) 폴더별로 워치독 기동
  4) 결과 확인

사용:
  python run_all_bots.py --roots "C:\\...\\영상자동화"            # 계획만 출력 (기본)
  python run_all_bots.py --roots "C:\\...\\영상자동화" --fix-mutex # 패치만
  python run_all_bots.py --roots "C:\\...\\영상자동화" --start     # 정지→패치→기동
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time

# ── 폴더 이름으로 고유 뮤텍스를 만드는 규칙 ────────────────
# watchdog.py(파이썬)와 health_check.ps1(파워셸)이 반드시 동일한 결과를 내야 한다.
#   폴더명 "Bingx_bot_1000"  →  "Global\BingX_Phoenix_Bingx_bot_1000_v1"
MUTEX_PREFIX_PS = "Global\\BingX_Phoenix_"    # 파워셸용 (역슬래시 1개)
MUTEX_PREFIX_PY = "Global\\\\BingX_Phoenix_"  # 파이썬 소스용 (이스케이프된 2개)
MUTEX_PREFIX    = MUTEX_PREFIX_PS
MUTEX_SUFFIX = "_v1"

PY_PATCH = '''_MUTEX_NAME = "{prefix}" + __import__("re").sub(r"[^A-Za-z0-9_]", "_", os.path.basename(BASE_DIR)) + "{suffix}"'''

# PowerShell: -split 는 정규식이라 백슬래시 이스케이프가 깨지기 쉽다.
# Split-Path -Leaf 를 쓰면 정규식이 개입하지 않아 안전하다.
# (PowerShell 큰따옴표 문자열에서 역슬래시는 이스케이프 문자가 아니므로 그대로 쓴다)
PS_PATCH = '''$MUTEX_NAME = "{prefix}" + ((Split-Path -Leaf $dir) -replace '[^A-Za-z0-9_]','_') + "{suffix}"'''

_PY_MUTEX_RE = re.compile(r'^(\s*)_MUTEX_NAME\s*=\s*.+$', re.M)
_PS_MUTEX_RE = re.compile(r'^(\s*)\$MUTEX_NAME\s*=\s*.+$', re.M)

PATCH_MARK = "BASE_DIR"          # 파이썬 패치 적용 여부 표식
PS_PATCH_MARK = "Split-Path"     # 파워셸 패치 적용 여부 표식


def expected_mutex(folder: str) -> str:
    """패치 후 이 폴더가 갖게 될 뮤텍스 이름 (미리보기용)"""
    base = re.sub(r"[^A-Za-z0-9_]", "_", os.path.basename(folder.rstrip(os.sep)))
    return MUTEX_PREFIX_PS + base + MUTEX_SUFFIX


def find_bot_folders(roots: list[str], depth: int = 2) -> list[str]:
    """watchdog.py 를 가진 폴더 = 봇 1대"""
    found, seen = [], set()
    for root in roots:
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            continue
        base = root.rstrip(os.sep).count(os.sep)
        for cur, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in
                       {".git", "__pycache__", "venv", ".venv", "node_modules"}]
            if cur.rstrip(os.sep).count(os.sep) - base >= depth:
                dirs[:] = []
            if "watchdog.py" in files and cur not in seen:
                seen.add(cur)
                found.append(cur)
    found.sort()
    return found


# ════════════════════════════════════════════════════════════
#  1) 정지
# ════════════════════════════════════════════════════════════

def stop_all(folders: list[str], dry: bool) -> int:
    """돌고 있는 워치독·봇 프로세스를 정지 (패치 전에 반드시 선행)"""
    if sys.platform != "win32":
        print("  [정지] Windows 전용 단계 — 건너뜀")
        return 0

    killed = 0
    for folder in folders:
        ps = (
            "Get-CimInstance Win32_Process -Filter \"name='python.exe' or name='pythonw.exe'\" "
            "| Where-Object { $_.CommandLine -like '*%s*' } "
            "| Select-Object -ExpandProperty ProcessId" % os.path.basename(folder)
        )
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=30).stdout.split()
        except Exception as e:
            print(f"  [정지] {os.path.basename(folder)} 조회 실패: {e}")
            continue
        for pid in out:
            if not pid.strip().isdigit():
                continue
            if dry:
                print(f"  [정지-예정] {os.path.basename(folder)} PID {pid}")
            else:
                subprocess.run(["taskkill", "/PID", pid.strip(), "/F", "/T"],
                               capture_output=True)
                print(f"  [정지] {os.path.basename(folder)} PID {pid}")
            killed += 1
    if killed == 0:
        print("  [정지] 실행 중인 봇 프로세스 없음")
    return killed


# ════════════════════════════════════════════════════════════
#  2) 뮤텍스 패치
# ════════════════════════════════════════════════════════════

def patch_folder(folder: str, dry: bool) -> str:
    """watchdog.py + health_check.ps1 의 뮤텍스 이름을 폴더 고유값으로"""
    name = os.path.basename(folder)
    wd   = os.path.join(folder, "watchdog.py")
    hc   = os.path.join(folder, "health_check.ps1")
    done = []

    # ── watchdog.py ─────────────────────────────
    if os.path.exists(wd):
        src = open(wd, encoding="utf-8", errors="replace").read()
        m = _PY_MUTEX_RE.search(src)
        if not m:
            done.append("watchdog.py: _MUTEX_NAME 없음")
        elif PATCH_MARK in m.group(0):
            done.append("watchdog.py: 이미 패치됨")
        else:
            new_line = m.group(1) + PY_PATCH.format(prefix=MUTEX_PREFIX_PY, suffix=MUTEX_SUFFIX)
            # 치환은 반드시 "함수"로 넘긴다. 문자열로 넘기면 re 가 치환문의
            # 백슬래시를 이스케이프로 해석해 \B 에서 예외가 나거나 \\ 가 \ 로 줄어든다.
            new_src = _PY_MUTEX_RE.sub(lambda _m: new_line, src, count=1)
            if not dry:
                # 새 내용을 먼저 완성한 뒤에 파일을 연다.
                # 열자마자 예외가 나면 파일이 0바이트로 날아가기 때문.
                shutil.copy(wd, wd + ".bak")
                with open(wd, "w", encoding="utf-8") as f:
                    f.write(new_src)
            done.append("watchdog.py: 패치" + ("(예정)" if dry else " 완료"))

    # ── health_check.ps1 ────────────────────────
    if os.path.exists(hc):
        src = open(hc, encoding="utf-8", errors="replace").read()
        m = _PS_MUTEX_RE.search(src)
        if not m:
            done.append("health_check.ps1: $MUTEX_NAME 없음")
        elif PS_PATCH_MARK in m.group(0):
            done.append("health_check.ps1: 이미 패치됨")
        else:
            new_line = m.group(1) + PS_PATCH.format(prefix=MUTEX_PREFIX_PS, suffix=MUTEX_SUFFIX)
            new_src = _PS_MUTEX_RE.sub(lambda _m: new_line, src, count=1)
            if not dry:
                shutil.copy(hc, hc + ".bak")
                with open(hc, "w", encoding="utf-8") as f:
                    f.write(new_src)
            done.append("health_check.ps1: 패치" + ("(예정)" if dry else " 완료"))
    else:
        done.append("health_check.ps1: 없음")

    return f"  [{name}] " + " / ".join(done)


# ════════════════════════════════════════════════════════════
#  3) 기동
# ════════════════════════════════════════════════════════════

def start_folder(folder: str, dry: bool) -> bool:
    """폴더에서 워치독을 --auto 모드로 백그라운드 기동"""
    name = os.path.basename(folder)
    wd   = os.path.join(folder, "watchdog.py")
    if not os.path.exists(wd):
        print(f"  [{name}] watchdog.py 없음 — 건너뜀")
        return False

    if dry:
        print(f"  [{name}] 기동 예정: watchdog.py --auto")
        return True

    exe = sys.executable
    if sys.platform == "win32":
        # 창 없이 돌도록 pythonw 사용 (있으면)
        cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(cand):
            exe = cand
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | \
                getattr(subprocess, "DETACHED_PROCESS", 0)
        kwargs = {"creationflags": flags}
    else:
        kwargs = {"start_new_session": True}

    try:
        subprocess.Popen([exe, "-u", wd, "--auto"], cwd=folder,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **kwargs)
        print(f"  [{name}] 기동  (뮤텍스 {expected_mutex(folder)})")
        return True
    except Exception as e:
        print(f"  [{name}] 기동 실패: {e}")
        return False


# ════════════════════════════════════════════════════════════
#  CLI
# ════════════════════════════════════════════════════════════

def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="봇 폴더 일괄 정지·뮤텍스 패치·기동")
    ap.add_argument("--roots", nargs="*", default=[os.path.dirname(here)],
                    help="봇 폴더들이 들어있는 상위 폴더 (기본: 이 폴더의 상위)")
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--fix-mutex", action="store_true", help="뮤텍스 이름만 패치")
    ap.add_argument("--start", action="store_true",
                    help="정지 → 패치 → 기동 전체 수행")
    args = ap.parse_args()

    dry = not (args.fix_mutex or args.start)

    folders = find_bot_folders(args.roots, args.depth)
    print("\n" + "═" * 78)
    print(f"  봇 폴더 {len(folders)}개 발견" + ("   [미리보기 — 실제 변경 없음]" if dry else ""))
    print("═" * 78)
    for f in folders:
        print(f"  {os.path.basename(f):<24} → {expected_mutex(f)}")
    if not folders:
        print("  watchdog.py 를 가진 폴더가 없습니다. --roots 경로를 확인하세요.")
        print("═" * 78 + "\n")
        return 1

    # 뮤텍스 이름이 겹치는지 미리 확인
    names = [expected_mutex(f) for f in folders]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        print("\n  ⚠ 패치 후에도 이름이 겹칩니다 (폴더명이 동일):")
        for d in dupes:
            print(f"     {d}")
        print("     폴더 이름을 서로 다르게 바꾸세요.")

    if args.start:
        print("\n── 1) 실행 중인 봇 정지 " + "─" * 50)
        stop_all(folders, dry=False)
        time.sleep(2)

    if args.start or args.fix_mutex:
        print("\n── 2) 뮤텍스 이름 고유화 " + "─" * 49)
    else:
        print("\n── 뮤텍스 패치 계획 " + "─" * 54)
    for f in folders:
        print(patch_folder(f, dry=dry))

    if args.start:
        print("\n── 3) 봇 기동 " + "─" * 60)
        ok = sum(start_folder(f, dry=False) for f in folders)
        time.sleep(3)
        print(f"\n  기동 요청 {ok}/{len(folders)}개")
        print("\n  확인:  python bot_status.py --roots \"%s\" --expect %d"
              % (args.roots[0], len(folders)))
    elif dry:
        print("\n  실제로 적용하려면:  --start  (정지→패치→기동)")
        print("  패치만 하려면    :  --fix-mutex")

    print("═" * 78 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
