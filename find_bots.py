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

# 로그가 이 시간 내에 갱신됐으면 "실행 중" 으로 본다
RUNNING_WINDOW_MIN = 10

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


def _run(cmd: list) -> str:
    """서브프로세스 실행 — 한글 커밋 메시지가 있어도 깨지지 않게 utf-8 강제.

    Windows 기본 인코딩(cp949)으로 읽으면 한글 커밋에서 UnicodeDecodeError 가
    스레드 안에서 터진다.
    """
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=10)
        return (r.stdout or b"").decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def git_info(folder: str) -> str:
    if not os.path.isdir(os.path.join(folder, ".git")):
        return "git 아님"
    branch = _run(["git", "-C", folder, "rev-parse", "--abbrev-ref", "HEAD"]) or "?"
    last   = _run(["git", "-C", folder, "log", "-1",
                   "--date=format:%Y-%m-%d %H:%M", "--pretty=%ad %s"])[:64] or "?"
    return f"{branch}  |  {last}"


def state_info(folder: str) -> tuple[str, dict]:
    """(표시문자열, {pnl, wins, losses, symbol}) — 판정 단계에서 재활용"""
    path = os.path.join(folder, "state.json")
    if not os.path.exists(path):
        return "state.json 없음", {}
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return f"{_fmt_time(os.path.getmtime(path))}  (읽기 실패)", {}

    pos = d.get("position")
    pos_s = (f"{pos['symbol']} {pos.get('trend','')} "
             f"${pos.get('total_invested',0):.0f} {pos.get('avg_down_step',0)}단계"
             if pos else "없음")
    pnl  = d.get("total_pnl", 0.0)
    w, l = d.get("win_count", 0), d.get("loss_count", 0)
    n    = w + l
    # 승률과 거래당 기대값을 함께 본다 — 승률만 보면 판단을 그르친다.
    # 승률 88% 인데 거래당 -$0.18 인 봇이 실제로 존재한다.
    perf = (f"승률 {w/n*100:.1f}% | 거래당 ${pnl/n:+.3f}" if n else "거래 없음")
    return (f"{_fmt_time(os.path.getmtime(path))}\n"
            f"      누적손익 ${pnl:+,.2f} | {w}W/{l}L ({n}건) | {perf}\n"
            f"      기록 보관 {len(d.get('closed_trades', []))}건 | 포지션 {pos_s}",
            {"pnl": pnl, "wins": w, "losses": l,
             "symbol": pos.get("symbol") if pos else None})


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

    now = datetime.now(KST).timestamp()
    rows = []
    for f in folders:
        cfg = parse_config(os.path.join(f, "config.py"))
        log_s, log_ts = log_info(f)
        st_s, st = state_info(f)
        # 로그가 RUNNING_WINDOW_MIN 분 이내에 갱신됐으면 "돌고 있음"으로 본다.
        # 가장 최근 하나만 고르면 여러 봇이 동시에 도는 상황을 놓친다.
        rows.append({"path": f, "cfg": cfg, "log": log_s, "log_ts": log_ts,
                     "state": st_s, "st": st,
                     "running": log_ts > 0 and (now - log_ts) <= RUNNING_WINDOW_MIN * 60})

    for r in rows:
        cfg, live_flag = r["cfg"], r["cfg"].get("LIVE_TRADING")
        marks = []
        if live_flag is True:
            marks.append("실거래설정")
        elif live_flag is False:
            marks.append("모의투자")
        else:
            marks.append("LIVE_TRADING 미정의")
        if r["running"]:
            marks.append("★실행 중")

        cap = cfg.get("TOTAL_CAPITAL")
        cap_s = f"${cap:,.0f}" if isinstance(cap, (int, float)) else "?"
        print(f"\n▸ {os.path.basename(r['path'])}   "
              f"{'  '.join('['+m+']' for m in marks)}")
        print(f"    경로   : {r['path']}")
        print(f"    설정   : LIVE_TRADING={live_flag} | 자본 {cap_s} | "
              f"레버리지 {cfg.get('LEVERAGE','?')}배 | "
              f"시드 ${cfg.get('INITIAL_POSITION_USD','?')} | "
              f"DCA {cfg.get('MAX_DCA_STAGES','?')}단계")
        print(f"    git    : {git_info(r['path'])}")
        print(f"    로그   : {r['log']}")
        print(f"    상태   : {r['state']}")

    print("\n" + "=" * 70)
    print("  판정")
    print("=" * 70)

    running  = [r for r in rows if r["running"]]
    live_cfg = [r for r in rows if r["cfg"].get("LIVE_TRADING") is True]

    if running:
        print(f"  · 지금 돌고 있는 봇 ({len(running)}개, "
              f"최근 {RUNNING_WINDOW_MIN}분 내 로그 갱신):")
        for r in running:
            tag = " ← 실거래" if r["cfg"].get("LIVE_TRADING") is True else ""
            print(f"    - {os.path.basename(r['path'])}{tag}")
    else:
        print("  · 최근 로그 갱신이 없습니다 — 돌고 있는 봇이 없어 보입니다.")

    live_running = [r for r in running if r["cfg"].get("LIVE_TRADING") is True]
    if len(live_running) > 1:
        print("\n  ⛔ 실거래 봇이 둘 이상 동시에 돌고 있습니다. 같은 계좌에")
        print("     두 봇이 주문을 냅니다 — 지금 하나를 끄세요.")
        for r in live_running:
            print(f"     - {r['path']}")
    elif live_running:
        print(f"\n  · 실거래 봇 : {os.path.basename(live_running[0]['path'])}")
        print(f"    → {live_running[0]['path']}")

    if len(live_cfg) > 1:
        print(f"\n  ⚠ LIVE_TRADING=True 폴더가 {len(live_cfg)}개입니다 "
              f"(실행 여부와 무관).")
        for r in live_cfg:
            state = "실행 중" if r["running"] else "정지"
            print(f"    - [{state}] {r['path']}")
        print("    실수로 켜면 실주문이 나갑니다. 쓰지 않는 폴더는")
        print("    config.py 의 LIVE_TRADING 을 False 로 바꿔두세요.")

    # 같은 코인을 여러 봇이 동시에 들고 있으면 알려준다
    holding = {}
    for r in rows:
        sym = r["st"].get("symbol")
        if sym:
            holding.setdefault(sym, []).append(os.path.basename(r["path"]))
    dupes = {s: v for s, v in holding.items() if len(v) > 1}
    if dupes:
        print("\n  · 같은 코인을 여러 봇이 동시 보유 중:")
        for s, v in dupes.items():
            print(f"    {s}: {', '.join(v)}")
        print("    (스캐너가 같은 기준을 쓰니 자연스러운 현상. 다만 실거래 봇이")
        print("     둘이면 같은 코인에 노출이 두 배가 된다)")

    # 성과 요약 — 승률만 보면 판단을 그르친다
    perf = [(os.path.basename(r["path"]), r["st"])
            for r in rows if r["st"].get("wins", 0) + r["st"].get("losses", 0) > 0]
    if len(perf) > 1:
        print("\n  · 성과 비교 (승률 높은 순)")
        print(f"    {'봇':<32}{'승률':>7}{'누적손익':>12}{'거래당':>10}")
        for name, st in sorted(
                perf, key=lambda x: -x[1]["wins"] / max(1, x[1]["wins"] + x[1]["losses"])):
            n = st["wins"] + st["losses"]
            print(f"    {name:<32}{st['wins']/n*100:>6.1f}%"
                  f"{st['pnl']:>+12.2f}{st['pnl']/n:>+10.3f}")
        print("    ※ 승률과 수익은 별개다. 승률이 가장 높은 봇이 가장 크게")
        print("      잃고 있을 수 있다 — 손익비가 나쁘면 그렇게 된다.")

    print("\n  ※ 실행 중인 프로세스를 직접 확인하려면 PowerShell 에서:")
    print("      Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" |")
    print("        Select-Object ProcessId, CommandLine | Format-List")
    print()


if __name__ == "__main__":
    main()
