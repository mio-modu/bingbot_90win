"""
봇 폴더 식별기 (Which bot is which?)
────────────────────────────────────
여러 봇 폴더 중 **무엇이 실거래 봇이고 무엇이 백업/모의투자인지** 가려낸다.
읽기만 한다 — 어떤 파일도 수정하지 않는다.

    python find_bots.py                       # 상위 폴더부터 자동 탐색
    python find_bots.py "C:\\...\\영상자동화"   # 경로 지정

각 폴더에 대해 보여주는 것
  · LIVE_TRADING / TOTAL_CAPITAL / LEVERAGE   (config.py 를 import 없이 정적 파싱)
  · git 브랜치·마지막 커밋                     (.git 이 있으면)
  · state.json 최종 수정 시각·누적손익·현재 포지션
  · bot.log 최종 수정 시각                     ← **지금 돌고 있는 봇의 결정적 단서**

가장 최근에 bot.log 가 갱신된 폴더가 실행 중인 봇이다.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

# config.py 에서 뽑아볼 값
KEYS = ["LIVE_TRADING", "TOTAL_CAPITAL", "LEVERAGE",
        "INITIAL_POSITION_USD", "MAX_DCA_STAGES"]


def _fmt_time(ts: float) -> str:
    try:
        dt = datetime.fromtimestamp(ts, KST)
    except (OSError, OverflowError, ValueError):
        return "?"
    delta = datetime.now(KST) - dt
    mins = delta.total_seconds() / 60
    if mins < 60:
        ago = f"{mins:.0f}분 전"
    elif mins < 60 * 24:
        ago = f"{mins/60:.1f}시간 전"
    else:
        ago = f"{mins/1440:.1f}일 전"
    return f"{dt:%Y-%m-%d %H:%M} ({ago})"


def parse_config(path: str) -> dict:
    """config.py 를 import 하지 않고 정적으로 읽는다 (.env·의존성 불필요)"""
    out = {}
    try:
        tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
    except (SyntaxError, OSError):
        return out
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in KEYS:
                try:
                    out[t.id] = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    pass
    return out


def git_info(folder: str) -> str:
    if not os.path.isdir(os.path.join(folder, ".git")):
        return "git 아님"
    try:
        br = subprocess.run(["git", "-C", folder, "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True, timeout=10)
        log = subprocess.run(["git", "-C", folder, "log", "-1",
                              "--date=format:%Y-%m-%d %H:%M", "--pretty=%ad %s"],
                             capture_output=True, text=True, timeout=10)
        branch = br.stdout.strip() or "?"
        last   = (log.stdout.strip() or "?")[:64]
        return f"{branch}  |  {last}"
    except Exception as e:
        return f"git 조회 실패: {e}"


def state_info(folder: str) -> str:
    path = os.path.join(folder, "state.json")
    if not os.path.exists(path):
        return "state.json 없음"
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return f"{_fmt_time(os.path.getmtime(path))}  (읽기 실패)"
    pos = d.get("position")
    pos_s = (f"{pos['symbol']} {pos.get('trend','')} "
             f"${pos.get('total_invested',0):.0f} {pos.get('avg_down_step',0)}단계"
             if pos else "없음")
    return (f"{_fmt_time(os.path.getmtime(path))}\n"
            f"      누적손익 ${d.get('total_pnl', 0):+,.2f} | "
            f"{d.get('win_count', 0)}W/{d.get('loss_count', 0)}L | "
            f"거래 {len(d.get('closed_trades', []))}건 기록 | 포지션 {pos_s}")


def log_info(folder: str) -> tuple[str, float]:
    """가장 최근 로그 파일의 수정 시각 → (표시문자열, epoch)"""
    newest, newest_ts = None, 0.0
    for name in os.listdir(folder):
        if not name.endswith(".log"):
            continue
        p = os.path.join(folder, name)
        try:
            ts = os.path.getmtime(p)
        except OSError:
            continue
        if ts > newest_ts:
            newest, newest_ts = name, ts
    if not newest:
        return "로그 없음", 0.0
    return f"{newest} — {_fmt_time(newest_ts)}", newest_ts


def find_bot_folders(root: str) -> list[str]:
    """config.py 와 main.py 가 함께 있는 폴더 = 봇 폴더"""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "__pycache__", "node_modules", ".venv", "venv")]
        if "config.py" in filenames and "main.py" in filenames:
            found.append(dirpath)
            dirnames[:] = []          # 봇 폴더 안쪽은 더 파고들지 않는다
    return sorted(found)


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        print(f"폴더를 찾을 수 없습니다: {root}")
        raise SystemExit(1)

    print("=" * 70)
    print(f"  봇 폴더 식별  —  {root}")
    print("=" * 70)

    folders = find_bot_folders(root)
    if not folders:
        print("\n  config.py + main.py 가 함께 있는 폴더를 찾지 못했습니다.")
        print("  상위 폴더를 인자로 넘겨보세요:")
        print('    python find_bots.py "C:\\Users\\...\\영상자동화"')
        return

    rows = []
    for f in folders:
        cfg = parse_config(os.path.join(f, "config.py"))
        log_s, log_ts = log_info(f)
        rows.append((f, cfg, log_s, log_ts))

    # 로그가 가장 최근인 폴더 = 실행 중인 봇
    live_ts = max((ts for _, _, _, ts in rows), default=0.0)

    for f, cfg, log_s, log_ts in rows:
        live_flag = cfg.get("LIVE_TRADING")
        is_running = log_ts > 0 and log_ts == live_ts
        marks = []
        if live_flag is True:
            marks.append("실거래설정")
        elif live_flag is False:
            marks.append("모의투자")
        if is_running:
            marks.append("★가장 최근 활동")

        print(f"\n▸ {os.path.basename(f)}   {'  '.join('['+m+']' for m in marks)}")
        print(f"    경로   : {f}")
        cap = cfg.get("TOTAL_CAPITAL")
        print(f"    설정   : LIVE_TRADING={live_flag} | "
              f"자본 ${cap:,.0f}" if isinstance(cap, (int, float))
              else f"    설정   : LIVE_TRADING={live_flag} | 자본 ?")
        print(f"             레버리지 {cfg.get('LEVERAGE','?')}배 | "
              f"시드 ${cfg.get('INITIAL_POSITION_USD','?')} | "
              f"DCA {cfg.get('MAX_DCA_STAGES','?')}단계")
        print(f"    git    : {git_info(f)}")
        print(f"    로그   : {log_s}")
        print(f"    상태   : {state_info(f)}")

    print("\n" + "=" * 70)
    print("  판정")
    print("=" * 70)
    running = [f for f, _, _, ts in rows if ts > 0 and ts == live_ts]
    live_cfg = [f for f, c, _, _ in rows if c.get("LIVE_TRADING") is True]

    if running:
        print(f"  · 지금 돌고 있는 봇  : {os.path.basename(running[0])}")
        print(f"    → {running[0]}")
        print("    (로그가 가장 최근에 갱신된 폴더. 봇이 켜져 있다면 몇 분 이내여야 한다)")
    else:
        print("  · 로그 파일이 없어 실행 중인 봇을 특정하지 못했습니다.")

    if live_cfg:
        print(f"\n  · LIVE_TRADING=True 폴더 ({len(live_cfg)}개):")
        for f in live_cfg:
            print(f"    - {f}")
        if len(live_cfg) > 1:
            print("    ⚠ 실거래 설정 폴더가 둘 이상입니다. 동시에 돌면 같은 계좌에")
            print("      두 봇이 주문을 냅니다 — 반드시 하나만 켜져 있어야 합니다.")
    else:
        print("\n  · LIVE_TRADING=True 인 폴더가 없습니다 (전부 모의투자).")

    print("\n  ※ 실행 중인 프로세스를 직접 확인하려면 PowerShell 에서:")
    print("      Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" |")
    print("        Select-Object ProcessId, CommandLine | Format-List")
    print()


if __name__ == "__main__":
    main()
