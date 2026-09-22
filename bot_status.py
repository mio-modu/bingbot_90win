"""
봇 상태 스냅샷 (Multi-Bot Status Reporter)
──────────────────────────────────────────
여러 대의 봇 상태를 한 파일로 모아 깃에 올린다.

왜 필요한가:
  .gitignore 가 state.json / bot.log / engine_state.json 을 전부 막고 있어서
  봇 상태가 실행 중인 PC 안에 갇혀 있다. PC 가 절전이거나 꺼지면
  밖에서는 봇이 돌았는지조차 확인할 수 없다.

무엇을 하는가:
  1. 각 봇 폴더의 state.json 을 읽어 손익·포지션·승패를 모은다
  2. 파일 수정 시각과 로그 마지막 줄로 "정말 돌고 있었는지"를 판정한다
     → 절전·중단으로 생긴 공백(gap)을 잡아낸다
  3. BOT_STATUS.md / bot_status.json 으로 저장 (--push 시 깃 푸시)

보안:
  .env 와 API 키는 읽지도, 쓰지도 않는다. state.json 의 손익·포지션 정보만 다룬다.

사용 (봇 폴더 6개가 한 상위 폴더에 나란히 있는 구조):
  C:\\...\\영상자동화\\
      ├─ Bingx_bot_certain\\   state.json, bot.log
      ├─ Bingx_bot_1000\\      state.json, bot.log
      └─ ... (총 6개)

  상위 폴더 하나만 주면 6개를 전부 자동으로 찾는다:
    python bot_status.py --roots "C:\\Users\\psen7\\OneDrive\\바탕 화면\\영상자동화" --expect 6

  그 밖에:
    python bot_status.py --push           # 스냅샷 생성 후 깃 커밋·푸시
    python bot_status.py --stale-min 15   # 15분 넘게 갱신 없으면 '정지' 판정
    python bot_status.py --no-proc        # 프로세스 감지 건너뜀

주의 — OneDrive 폴더에서 돌릴 때:
  state.json 은 봇이 수 초마다 덮어쓴다. OneDrive 동기화와 겹치면
  파일 잠금·충돌 사본(.conflict)·지연이 생길 수 있다.
  '정지' 로 잘못 뜨면 --stale-min 을 넉넉히(15~20분) 주고,
  가능하면 봇 폴더를 OneDrive 밖으로 옮기는 편이 안전하다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys

# 봇 1대를 식별하는 기준 파일
STATE_FILE  = "state.json"
ENGINE_FILE = "engine_state.json"
LOG_FILE    = "bot.log"

# 절대 읽지 않는 파일 (실수 방지용 명시)
NEVER_READ = {".env", "secrets.json", "credentials.json"}

OUT_JSON = "bot_status.json"
OUT_MD   = "BOT_STATUS.md"


# ════════════════════════════════════════════════════════════
#  탐색
# ════════════════════════════════════════════════════════════

def find_bots(roots: list[str], depth: int = 2) -> list[str]:
    """state.json 을 가진 폴더 = 봇 1대. roots 아래를 depth 단계까지 탐색"""
    found: list[str] = []
    seen: set[str] = set()

    for root in roots:
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip(os.sep).count(os.sep)
        for cur, dirs, files in os.walk(root):
            # 캐시·가상환경·깃 폴더는 건너뛴다
            dirs[:] = [d for d in dirs
                       if d not in {".git", "__pycache__", "venv", ".venv", "node_modules"}]
            if cur.rstrip(os.sep).count(os.sep) - base_depth >= depth:
                dirs[:] = []
            if STATE_FILE in files and cur not in seen:
                seen.add(cur)
                found.append(cur)

    found.sort()
    return found


# ════════════════════════════════════════════════════════════
#  판정
# ════════════════════════════════════════════════════════════

def _mtime(path: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromtimestamp(os.path.getmtime(path))
    except OSError:
        return None


def _last_log_time(log_path: str) -> dt.datetime | None:
    """bot.log 마지막 줄의 타임스탬프 (형식: 'YYYY-MM-DD HH:MM:SS,mmm [LEVEL] ...')"""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(size - 8192, 0))
            tail = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None

    for line in reversed(tail):
        stamp = line[:19]
        try:
            return dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def liveness(last_seen: dt.datetime | None, stale_min: int) -> tuple[str, float]:
    """(판정, 경과 분) — 마지막 활동으로부터 얼마나 지났는지"""
    if last_seen is None:
        return "알 수 없음", -1.0
    gap_min = (dt.datetime.now() - last_seen).total_seconds() / 60.0
    if gap_min <= stale_min:
        return "가동 중", gap_min
    if gap_min <= stale_min * 4:
        return "지연", gap_min
    return "정지", gap_min


def read_bot(path: str, stale_min: int, procs: list[dict] | None = None) -> dict:
    """봇 1대의 상태를 읽는다 (.env 등 민감 파일은 접근하지 않음)"""
    name = os.path.basename(path.rstrip(os.sep)) or path
    info: dict = {"name": name, "path": path}

    state_path = os.path.join(path, STATE_FILE)
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        info["error"] = f"{STATE_FILE} 읽기 실패: {e}"
        return info

    wins   = int(state.get("win_count", 0) or 0)
    losses = int(state.get("loss_count", 0) or 0)
    total  = wins + losses
    pos    = state.get("position")

    info.update({
        "total_pnl":   round(float(state.get("total_pnl", 0) or 0), 2),
        "wins":        wins,
        "losses":      losses,
        "trades":      total,
        "win_rate":    round(wins / total * 100, 1) if total else None,
        "withdrawn":   round(float(state.get("total_withdrawn", 0) or 0), 2),
        "position":    None,
    })

    if isinstance(pos, dict):
        info["position"] = {
            "symbol":     pos.get("symbol"),
            "trend":      pos.get("trend"),
            "invested":   round(float(pos.get("total_invested", 0) or 0), 2),
            "dca_step":   pos.get("avg_down_step"),
            "avg_price":  pos.get("avg_price"),
        }

    # ── 생존 판정: 로그 마지막 줄 우선, 없으면 파일 수정 시각 ──
    log_time   = _last_log_time(os.path.join(path, LOG_FILE))
    state_time = _mtime(state_path)
    candidates = [t for t in (log_time, state_time) if t is not None]
    last_seen  = max(candidates) if candidates else None

    verdict, gap = liveness(last_seen, stale_min)

    # 실행 중인 프로세스와 교차 검증 — 파일은 오래됐는데 프로세스가 살아있거나 그 반대
    proc = match_process(path, procs or [])
    info["pid"] = proc["pid"] if proc else None

    # 워치독 뮤텍스 — 이름과 실제 점유 여부
    info["mutex"]       = read_mutex_name(path)
    info["mutex_alive"] = mutex_alive(info["mutex"]) if info["mutex"] else None

    info.update({
        "last_seen":  last_seen.isoformat(timespec="seconds") if last_seen else None,
        "gap_min":    round(gap, 1) if gap >= 0 else None,
        "status":     verdict,
    })
    return info


# ════════════════════════════════════════════════════════════
#  실행 중인 봇 프로세스 감지
# ════════════════════════════════════════════════════════════
# 폴더(state.json) 기준 집계만으로는 "봇 6대가 진짜 돌고 있나"를 알 수 없다.
# 한 폴더에서 여러 프로세스를 띄우거나, 폴더는 있는데 프로세스가 죽었을 수 있다.
# 그래서 실제 python 프로세스를 직접 세어 폴더 상태와 교차 검증한다.

# 봇 프로세스로 인정할 명령줄 키워드 (소문자 비교)
BOT_CMD_HINTS = ("main.py", "watchdog.py", "paper_trader", "strategy_engine")


def _is_python_exe(cmdline: str) -> bool:
    """명령줄의 실행 파일이 실제 python 인가 (bash -c 래퍼 오탐 방지)"""
    first = cmdline.split()[0] if cmdline.split() else ""
    exe   = os.path.basename(first.strip('"')).lower()
    return exe.startswith("python")


def _proc_cwd(pid: int) -> str | None:
    """프로세스의 작업 폴더 (리눅스/맥). 실패 시 None"""
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def find_processes(name_filter: str = "") -> list[dict]:
    """
    실행 중인 파이썬 봇 프로세스 목록 [{pid, cmdline}, ...].
    Windows 는 PowerShell(Get-CimInstance), 그 외는 ps 를 사용한다.
    감지 실패는 오류가 아니라 빈 목록으로 처리 (권한 등).
    """
    procs: list[dict] = []

    try:
        if sys.platform == "win32":
            ps_cmd = (
                "Get-CimInstance Win32_Process "
                "-Filter \"name='python.exe' or name='pythonw.exe'\" "
                "| Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
            )
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=30,
            ).stdout.strip()
            if not out:
                return []
            data = json.loads(out)
            if isinstance(data, dict):      # 결과가 1건이면 dict 로 온다
                data = [data]
            for item in data:
                cmd = (item.get("CommandLine") or "").strip()
                if cmd:
                    procs.append({"pid": item.get("ProcessId"), "cmdline": cmd,
                                  "cwd": None})
        else:
            out = subprocess.run(
                ["ps", "-eo", "pid=,args="],
                capture_output=True, text=True, timeout=30,
            ).stdout
            for line in out.splitlines():
                line = line.strip()
                if not line:
                    continue
                pid, _, cmd = line.partition(" ")
                cmd = cmd.strip()
                if not pid.isdigit() or not _is_python_exe(cmd):
                    continue
                procs.append({"pid": int(pid), "cmdline": cmd,
                              "cwd": _proc_cwd(int(pid))})
    except Exception:
        return []

    # 봇으로 보이는 것만 남긴다
    result = []
    for p in procs:
        low = p["cmdline"].lower()
        if not any(h in low for h in BOT_CMD_HINTS):
            continue
        if name_filter and name_filter.lower() not in low:
            continue
        result.append(p)
    return result


def match_process(bot_path: str, procs: list[dict]) -> dict | None:
    """봇 폴더 경로가 명령줄에 들어있는 프로세스를 찾는다"""
    target = os.path.realpath(bot_path)
    folder = os.path.basename(bot_path.rstrip(os.sep)).lower()
    full   = target.replace("\\", "/").lower()

    # 1순위: 프로세스의 실제 작업 폴더가 봇 폴더와 같은가 (가장 확실)
    for p in procs:
        cwd = p.get("cwd")
        if cwd and os.path.realpath(cwd) == target:
            return p

    # 2순위: 명령줄에 봇 폴더 경로가 박혀 있는가
    for p in procs:
        cmd = p["cmdline"].replace("\\", "/").lower()
        if full and full in cmd:
            return p
        if folder and f"/{folder}/" in cmd:
            return p
    return None


# ════════════════════════════════════════════════════════════
#  워치독 뮤텍스 검사
# ════════════════════════════════════════════════════════════
# watchdog.py 는 전역 뮤텍스로 중복 실행을 막는다:
#     _MUTEX_NAME = "Global\\BingX_Phoenix_Certain_v1"
# 그런데 이 이름이 하드코딩이라, 여러 봇 폴더가 같은 레포를 받으면
# 뮤텍스 이름이 겹친다 → 먼저 뜬 1대만 살고 나머지는 중복으로 종료된다.
# 봇을 6대 띄웠는데 1대만 도는 전형적인 원인이므로 반드시 검사한다.

_MUTEX_RE = re.compile(r'_MUTEX_NAME\s*=\s*[\'"](.+?)[\'"]')


def read_mutex_name(bot_path: str) -> str | None:
    """봇 폴더의 watchdog.py 에서 뮤텍스 이름을 뽑아낸다"""
    wd = os.path.join(bot_path, "watchdog.py")
    try:
        with open(wd, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    m = _MUTEX_RE.search(text)
    if not m:
        return None
    # 소스에 쓰인 "Global\\Name" 은 실제 값 "Global\Name"
    return m.group(1).replace("\\\\", "\\")


def mutex_alive(name: str) -> bool | None:
    """
    해당 전역 뮤텍스가 지금 잡혀 있는가 (= 워치독 실행 중).
    Windows 전용. 그 외 OS 나 실패 시 None.
    """
    if sys.platform != "win32" or not name:
        return None
    ps = (
        "try { $m=[System.Threading.Mutex]::OpenExisting('%s'); $m.Close(); 'YES' } "
        "catch { 'NO' }" % name.replace("'", "''")
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        return None
    if out == "YES":
        return True
    if out == "NO":
        return False
    return None


def find_mutex_collisions(bots: list[dict]) -> dict[str, list[str]]:
    """같은 뮤텍스 이름을 쓰는 봇 폴더들을 모은다 (2개 이상이면 충돌)"""
    by_name: dict[str, list[str]] = {}
    for b in bots:
        name = b.get("mutex")
        if name:
            by_name.setdefault(name, []).append(b["name"])
    return {n: v for n, v in by_name.items() if len(v) > 1}


# ════════════════════════════════════════════════════════════
#  출력
# ════════════════════════════════════════════════════════════

_ICON = {"가동 중": "🟢", "지연": "🟡", "정지": "🔴", "알 수 없음": "⚪"}


def build_markdown(bots: list[dict], stale_min: int,
                   procs: list[dict] | None = None, expect: int = 0) -> str:
    now  = dt.datetime.now()
    live = sum(1 for b in bots if b.get("status") == "가동 중")
    pnl  = sum(b.get("total_pnl", 0) or 0 for b in bots)

    lines = [
        "# 봇 상태 스냅샷",
        "",
        f"- 생성 시각: **{now.strftime('%Y-%m-%d %H:%M:%S')}**",
        f"- 감지된 봇: **{len(bots)}대** / 가동 중 **{live}대**",
        f"- 합산 손익: **${pnl:+,.2f}**",
        (f"- 실행 중인 프로세스: **{len(procs)}개**" if procs is not None else ""),
        f"- 정지 판정 기준: 마지막 활동 후 {stale_min}분 초과",
        "",
        "| | 봇 | 상태 | 마지막 활동 | 공백(분) | 손익 | 거래 | 승률 | 보유 포지션 |",
        "|---|---|---|---|---:|---:|---:|---:|---|",
    ]

    for b in bots:
        if "error" in b:
            lines.append(f"| ⚪ | {b['name']} | 읽기 실패 | - | - | - | - | - | {b['error']} |")
            continue
        p = b.get("position")
        pos_txt = (f"{p['symbol']} {p['trend']} ${p['invested']:,.0f} ({p['dca_step']}단계)"
                   if p else "없음")
        wr = f"{b['win_rate']:.1f}%" if b.get("win_rate") is not None else "-"
        lines.append(
            f"| {_ICON.get(b['status'], '⚪')} | {b['name']} | {b['status']} | "
            f"{b['last_seen'] or '-'} | {b['gap_min'] if b['gap_min'] is not None else '-'} | "
            f"${b['total_pnl']:+,.2f} | {b['trades']}회 ({b['wins']}W/{b['losses']}L) | {wr} | {pos_txt} |"
        )

    if procs is not None:
        lines += ["", f"## 실행 중인 봇 프로세스: {len(procs)}개", ""]
        if expect and len(procs) != expect:
            lines.append(f"> ⚠ **기대 {expect}대 ≠ 실제 {len(procs)}개** — 죽은 봇이 있습니다.")
            lines.append("")
        for pr in procs:
            lines.append(f"- `PID {pr['pid']}` — `{pr['cmdline'][:160]}`")
        if not procs:
            lines.append("_감지된 봇 프로세스가 없습니다. 전부 정지 상태이거나 권한 문제입니다._")

    collisions = find_mutex_collisions(bots)
    if collisions:
        lines += ["", "## 🔴 워치독 뮤텍스 충돌", "",
                  "뮤텍스 이름이 같으면 **먼저 뜬 1대만 살아남고 나머지는 중복으로 즉시 종료**됩니다.", ""]
        for name, owners in collisions.items():
            lines.append(f"- `{name}` ← {', '.join(owners)} (**{len(owners)}개 폴더**가 같은 이름)")
        lines += ["", "**해결**: 각 폴더의 `watchdog.py` 에서 `_MUTEX_NAME` 을 폴더마다 다르게 수정하세요.", ""]

    stalled = [b for b in bots if b.get("status") in ("정지", "지연")]
    if stalled:
        lines += ["", "## ⚠ 확인 필요", ""]
        for b in stalled:
            lines.append(
                f"- **{b['name']}** — {b['status']} "
                f"(마지막 활동 후 {b['gap_min']}분). "
                f"PC 절전·재부팅·프로세스 종료를 확인하세요."
            )

    lines += ["", "---", "", "_이 파일은 `bot_status.py` 가 생성합니다. 손익·포지션 정보만 포함하며 API 키는 다루지 않습니다._"]
    return "\n".join(lines) + "\n"


def print_console(bots: list[dict], stale_min: int,
                  procs: list[dict] | None = None, expect: int = 0):
    print("\n" + "═" * 96)
    print(f"  봇 상태 — {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  "
          f"(정지 기준: {stale_min}분)")
    print("═" * 96)
    if not bots:
        print("  state.json 을 가진 봇 폴더를 찾지 못했습니다. --roots 로 경로를 지정하세요.")
        print("═" * 96 + "\n")
        return

    print(f"  {'봇':<22}{'상태':<10}{'공백(분)':>10}{'손익':>12}{'거래':>14}{'포지션':<24}")
    print("  " + "─" * 92)
    for b in bots:
        if "error" in b:
            print(f"  {b['name']:<22}{'읽기실패':<10}{'-':>10}{'-':>12}{'-':>14}{b['error']:<24}")
            continue
        p = b.get("position")
        pos_txt = f"{p['symbol']} {p['dca_step']}단계" if p else "없음"
        gap = f"{b['gap_min']:.0f}" if b['gap_min'] is not None else "-"
        pnl_txt    = "$" + format(b["total_pnl"], "+,.2f")
        trades_txt = "{}회({}W/{}L)".format(b["trades"], b["wins"], b["losses"])
        icon       = _ICON.get(b["status"], "⚪")
        print(f"  {icon} {b['name']:<20}{b['status']:<10}{gap:>10}"
              f"{pnl_txt:>12}{trades_txt:>14}  {pos_txt:<24}")

    live = sum(1 for b in bots if b.get("status") == "가동 중")
    pnl  = sum(b.get("total_pnl", 0) or 0 for b in bots)
    print("  " + "─" * 92)
    print(f"  합계: {len(bots)}대 중 가동 {live}대  |  합산 손익 ${pnl:+,.2f}")

    if procs is not None:
        print("  " + "─" * 92)
        print(f"  실행 중인 봇 프로세스: {len(procs)}개")
        print(f"    {'PID':<12}{'매칭된 봇':<14} 명령줄")
        matched = {b["pid"]: b["name"] for b in bots if b.get("pid")}
        for pr in procs[:12]:
            tag = matched.get(pr["pid"], "―")
            print(f"    PID {pr['pid']:<8} {tag:<14} {pr['cmdline'][:60]}")
        if expect and len(procs) != expect:
            print(f"  ⚠ 기대 {expect}대 ≠ 실제 {len(procs)}개 — 죽은 봇이 있습니다")

    collisions = find_mutex_collisions(bots)
    if collisions:
        print("  " + "─" * 92)
        print("  🔴 워치독 뮤텍스 충돌 — 이름이 같으면 1대만 살고 나머지는 즉시 종료됩니다")
        for name, owners in collisions.items():
            print(f"     {name}")
            print(f"       ↳ {', '.join(owners)}  ({len(owners)}개 폴더가 같은 이름 사용)")
        print("     해결: 각 폴더의 watchdog.py 에서 _MUTEX_NAME 을 폴더마다 다르게 수정")
    print("═" * 96 + "\n")


# ════════════════════════════════════════════════════════════
#  깃 푸시
# ════════════════════════════════════════════════════════════

def git_push(repo_dir: str, files: list[str]) -> int:
    """스냅샷 파일만 커밋·푸시 (다른 변경사항은 건드리지 않음)"""
    def run(*args) -> tuple[int, str]:
        p = subprocess.run(["git", "-C", repo_dir, *args],
                           capture_output=True, text=True)
        return p.returncode, (p.stdout + p.stderr).strip()

    rc, branch = run("rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:
        print(f"  깃 저장소가 아닙니다: {repo_dir}")
        return 1

    rc, _ = run("add", "--", *files)
    if rc != 0:
        print("  git add 실패")
        return 1

    rc, out = run("diff", "--cached", "--quiet")
    if rc == 0:
        print("  변경 없음 — 커밋 생략")
        return 0

    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    rc, out = run("commit", "-m", f"봇 상태 스냅샷 {stamp}")
    if rc != 0:
        print(f"  커밋 실패: {out}")
        return 1

    rc, out = run("push", "-u", "origin", branch)
    if rc != 0:
        print(f"  푸시 실패: {out}")
        return 1

    print(f"  → {branch} 브랜치에 푸시 완료")
    return 0


# ════════════════════════════════════════════════════════════
#  CLI
# ════════════════════════════════════════════════════════════

def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(
        description="여러 봇의 상태를 모아 스냅샷으로 저장 (PC 밖에서 확인용)")
    parser.add_argument("--roots", nargs="*", default=None,
                        help="봇 폴더를 찾을 최상위 경로들 (기본: 이 스크립트 폴더와 그 상위)")
    parser.add_argument("--depth", type=int, default=2,
                        help="탐색 깊이 (기본 2단계)")
    parser.add_argument("--stale-min", type=int, default=10,
                        help="마지막 활동 후 N분 초과 시 '정지' 판정 (기본 10)")
    parser.add_argument("--expect", type=int, default=0,
                        help="기대하는 봇 대수 (예: 6). 실제와 다르면 경보 표시")
    parser.add_argument("--name-filter", default="",
                        help="프로세스 명령줄에 이 문자열이 있는 것만 봇으로 집계 (예: Bingx_bot)")
    parser.add_argument("--no-proc", action="store_true",
                        help="프로세스 감지를 건너뜀")
    parser.add_argument("--push", action="store_true",
                        help="스냅샷을 깃에 커밋·푸시")
    parser.add_argument("--out-dir", default=here,
                        help="스냅샷 저장 폴더 (기본: 스크립트 폴더)")
    args = parser.parse_args()

    roots = args.roots or [here, os.path.dirname(here)]
    procs = None if args.no_proc else find_processes(args.name_filter)
    paths = find_bots(roots, args.depth)
    bots  = [read_bot(p, args.stale_min, procs) for p in paths]

    print_console(bots, args.stale_min, procs, args.expect)

    payload = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "stale_min":    args.stale_min,
        "bot_count":    len(bots),
        "live_count":   sum(1 for b in bots if b.get("status") == "가동 중"),
        "total_pnl":    round(sum(b.get("total_pnl", 0) or 0 for b in bots), 2),
        "expect":       args.expect or None,
        "process_count": len(procs) if procs is not None else None,
        "processes":    procs,
        "bots":         bots,
    }

    json_path = os.path.join(args.out_dir, OUT_JSON)
    md_path   = os.path.join(args.out_dir, OUT_MD)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(build_markdown(bots, args.stale_min, procs, args.expect))
    print(f"  저장: {md_path}")
    print(f"  저장: {json_path}")

    if args.push:
        return git_push(here, [OUT_JSON, OUT_MD])
    return 0


if __name__ == "__main__":
    sys.exit(main())
