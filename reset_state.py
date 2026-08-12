"""
기록 초기화 (완전 새 출발)
──────────────────────────
    python reset_state.py            # 확인 후 초기화
    python reset_state.py --yes      # 묻지 않고 실행
    python reset_state.py --force    # 포지션이 있어도 강행 (권장하지 않음)

무엇을 하나
-----------
봇의 상태·손익·거래 기록을 모두 비워 완전히 새 계좌처럼 시작하게 한다.
자본은 다음 기동 때 거래소 잔고를 읽어 자동으로 맞춘다.

  · state.json           포지션 · 누적손익 · 승패
  · engine_state.json    차단 코인 · 쿨다운 · 방향 차단
  · governor_state.json  고점 · 낙폭 · 연속손실
  · trades.jsonl         거래 저널

⚠ **지우지 않는다. history/reset_<날짜>/ 로 옮긴다.**
  왜: 저 파일들은 "어디서 얼마를 잃었는가"의 유일한 근거다. 지워버리면
  같은 실수를 반복해도 알 수 없다. 봇은 어차피 파일이 없으면 새로
  시작하므로, 옮기는 것만으로 초기화 목적은 완전히 달성된다.
  같은 폴더에 손절 분석 요약(summary.txt)도 함께 남긴다.

⚠ 미청산 포지션이 있으면 **거부한다.**
  state.json 을 치우는 순간 봇은 그 포지션을 모르게 되고, 거래소에는
  주인 없는 포지션과 옛 손절 주문만 남는다. 먼저 정리해야 한다.
      python close_all.py
"""

import argparse
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

KST = timezone(timedelta(hours=9))

TARGETS = [
    ("state.json",           "포지션 · 누적손익 · 승패"),
    ("engine_state.json",    "차단 코인 · 쿨다운 · 방향 차단"),
    ("governor_state.json",  "고점 · 낙폭 · 연속손실"),
    ("trades.jsonl",         "거래 저널"),
    ("state.json.bak",       "이전 상태 백업"),
]


def _open_positions():
    """거래소 미청산 포지션. 조회 실패 시 None (모름)."""
    try:
        import config
        if not config.LIVE_TRADING or not config.API_KEY:
            return []
        from bingx_api import BingXAPI
        api = BingXAPI()
        return [p for p in api.get_positions()
                if abs(float(p.get("positionAmt", 0) or 0)) > 0]
    except Exception as e:
        print(f"  ⚠ 거래소 포지션 조회 실패: {e}")
        return None


def _write_summary(dest: str):
    """옮기기 전에 손절 분석 요약을 남긴다 — 숫자가 사라지지 않게."""
    try:
        import trade_journal
        import loss_autopsy as la
        trades = trade_journal.load()
        if not trades:
            return 0
        rep = la.autopsy(trades)
        path = os.path.join(dest, "summary.txt")
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            la.print_report(rep, "초기화 직전 기록 전체")
        with open(path, "w", encoding="utf-8") as f:
            f.write(buf.getvalue())
        return len(trades)
    except Exception as e:
        print(f"  ⚠ 요약 생성 실패(무시): {e}")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="봇 기록 초기화")
    ap.add_argument("--yes",   action="store_true", help="확인 없이 실행")
    ap.add_argument("--force", action="store_true",
                    help="미청산 포지션이 있어도 강행")
    args = ap.parse_args()

    print("=" * 52)
    print("  기록 초기화 — 완전 새 출발")
    print("=" * 52)

    # ── 1. 봇이 돌고 있으면 막는다 ──────────────────────
    # 돌고 있는 봇은 메모리의 상태를 언제든 다시 저장한다.
    # 파일만 치워봐야 몇 초 뒤 되살아난다.
    try:
        import subprocess
        r = subprocess.run(["pgrep", "-f", "watchdog.py"],
                           capture_output=True, text=True)
        if r.stdout.strip():
            print("\n  ✗ 봇이 돌고 있습니다 (PID "
                  f"{r.stdout.split()[0]}).")
            print("    먼저 멈추세요:  bash restart.sh --stop")
            return 1
    except Exception:
        pass

    # ── 2. 미청산 포지션 확인 ──────────────────────────
    print("\n[1/3] 거래소 포지션 확인...")
    pos = _open_positions()
    if pos is None:
        if not args.force:
            print("  조회에 실패해 안전하게 중단합니다 "
                  "(강행하려면 --force)")
            return 1
        print("  ⚠ 확인 못 했지만 --force 로 진행합니다")
    elif pos:
        print(f"  ✗ 미청산 포지션 {len(pos)}개:")
        for p in pos:
            print(f"      {p.get('symbol')} {p.get('positionSide')} "
                  f"{p.get('positionAmt')} / 미실현 {p.get('unrealizedProfit')}")
        print("\n    기록을 비우면 봇은 이 포지션을 모르게 되고,")
        print("    거래소에는 주인 없는 포지션과 옛 손절 주문만 남습니다.")
        print("    먼저 정리하세요:")
        print("        python close_all.py")
        if not args.force:
            return 1
        print("  ⚠ --force 로 강행합니다")
    else:
        print("  ✅ 포지션 없음 (깨끗)")

    # ── 3. 무엇을 옮길지 보여준다 ──────────────────────
    found = [(f, d) for f, d in TARGETS if os.path.exists(os.path.join(BASE, f))]
    print("\n[2/3] 보관할 파일")
    if not found:
        print("  (없음 — 이미 깨끗한 상태입니다)")
        return 0
    for f, d in found:
        size = os.path.getsize(os.path.join(BASE, f))
        print(f"  · {f:22s} {size:>8,}B   {d}")

    if not args.yes:
        print("\n  이 파일들을 history/ 로 옮기고 봇을 새 출발시킵니다.")
        print("  (지우지 않습니다. 되돌리려면 다시 옮겨오면 됩니다)")
        try:
            ans = input("  진행할까요? [y/N] ").strip().lower()
        except EOFError:
            ans = "n"
        if not ans.startswith("y"):
            print("  취소했습니다.")
            return 1

    # ── 4. 옮기기 ──────────────────────────────────────
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M")
    dest  = os.path.join(BASE, "history", f"reset_{stamp}")
    os.makedirs(dest, exist_ok=True)

    print(f"\n[3/3] history/reset_{stamp}/ 로 이동")
    n_trades = _write_summary(dest)
    if n_trades:
        print(f"  · summary.txt          거래 {n_trades}건 손절 분석 요약")

    for f, _ in found:
        shutil.move(os.path.join(BASE, f), os.path.join(dest, f))
        print(f"  · {f}")

    print("\n" + "=" * 52)
    print("  ✅ 초기화 완료")
    print("=" * 52)
    print("""
  다음 기동 때 봇이 거래소 잔고를 읽어 자본을 맞춥니다.
  누적손익 0 / 승패 0 / 고점 = 현재 자본 에서 새로 시작합니다.

  시작:
      bash restart.sh

  보관된 기록 보기:
      cat history/reset_%s/summary.txt
""" % stamp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
