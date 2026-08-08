"""실거래봇 헬스체크 (파이썬판) — watchdog이 죽어있으면 재시작.

certain 봇 등 다른 인스턴스의 watchdog이 떠 있어도 오탐하지 않도록
'이 폴더의 watchdog.py'가 떠 있는지 커맨드라인 경로로 판정한다.
(권장: health_check.ps1 — 전역 뮤텍스로 판정해 더 정확함)
"""
import os
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

python   = r"C:\Users\psen7\AppData\Local\Programs\Python\Python313\python.exe"
watchdog = os.path.join(BASE_DIR, "watchdog.py")

# 이 폴더의 watchdog.py가 실행 중인지 커맨드라인으로 확인
proc = subprocess.run(
    ["wmic", "process", "where", "name='python.exe'", "get", "CommandLine"],
    capture_output=True, text=True, errors="replace"
)
cmdlines = proc.stdout or ""

target = os.path.basename(BASE_DIR).lower()
running = any(
    "watchdog" in line.lower() and target in line.lower()
    for line in cmdlines.splitlines()
)

if not running:
    subprocess.Popen(
        [python, watchdog, "--auto"],
        cwd=BASE_DIR,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
