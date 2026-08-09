"""
한 번에 실행 (One Click)
────────────────────────
봇 폴더가 여러 개일 때 해야 할 안전 점검·기록 수집·분석을 한 번에 처리한다.

    python oneclick.py "C:\\Users\\...\\영상자동화"

하는 일
  1. 봇 폴더 전수 조사 — 무엇이 실거래이고 무엇이 돌고 있는지
  2. **안전**: 실거래 봇 하나만 남기고 나머지 LIVE_TRADING 을 False 로
     (원본은 config.py.bak_날짜 로 백업. 되돌리기 쉽다)
  3. 모든 봇의 과거 거래 기록을 trades.jsonl 로 수집
  4. 손실 분석 리포트 생성 → report.txt 저장

하지 않는 일 (일부러)
  · 돌고 있는 봇을 끄거나 재시작하지 않는다
  · 실거래 봇의 코드를 병합하지 않는다 — 워치독이 재시작하면 지켜보지 않는
    사이에 새 로직이 적용된다. 그건 사람이 보고 있을 때 해야 한다.
  · 포지션을 청산하지 않는다
"""

from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import analyze                      # noqa: E402
import find_bots                    # noqa: E402
import import_history               # noqa: E402
import trade_journal                # noqa: E402

KST = timezone(timedelta(hours=9))

# 이 이름의 폴더만 실거래로 남긴다 (다른 이름이면 --live-bot 으로 지정)
DEFAULT_LIVE_BOT = "Bingx_bot_live"


def step(n: int, title: str):
    print()
    print("=" * 70)
    print(f"  {n}. {title}")
    print("=" * 70)


def survey(root: str) -> list[dict]:
    """봇 폴더 전수 조사"""
    folders = find_bots.find_bot_folders(root)
    now = datetime.now(KST).timestamp()
    rows = []
    for f in folders:
        cfg = find_bots.parse_config(os.path.join(f, "config.py"))
        _, log_ts = find_bots.log_info(f)
        _, st = find_bots.state_info(f)
        rows.append({
            "path": f,
            "name": os.path.basename(f),
            "live": cfg.get("LIVE_TRADING") is True,
            "running": log_ts > 0 and (now - log_ts) <= find_bots.RUNNING_WINDOW_MIN * 60,
            "st": st,
        })
    return rows


def disable_extra_live(rows: list[dict], keep: str, dry: bool) -> list[str]:
    """실거래 봇 하나만 남기고 나머지 LIVE_TRADING 을 끈다"""
    changed = []
    for r in rows:
        if not r["live"] or r["name"] == keep:
            continue
        path = os.path.join(r["path"], "config.py")
        try:
            src = open(path, encoding="utf-8").read()
        except OSError as e:
            print(f"  ! {r['name']}: config.py 읽기 실패 — {e}")
            continue

        new, n = re.subn(
            r"^LIVE_TRADING(\s*)=(\s*)True",
            lambda m: (f"LIVE_TRADING{m.group(1)}={m.group(2)}False"
                       f"   # oneclick.py 자동 비활성화 "
                       f"({datetime.now(KST):%Y-%m-%d}) — 실거래 봇은 {keep}"),
            src, count=1, flags=re.M)
        if n == 0:
            print(f"  ! {r['name']}: 'LIVE_TRADING = True' 줄을 찾지 못해 건너뜀")
            continue

        if dry:
            print(f"  [dry-run] {r['name']} → LIVE_TRADING = False 로 바꿀 예정")
            changed.append(r["name"])
            continue

        bak = path + f".bak_{datetime.now(KST):%Y%m%d_%H%M%S}"
        shutil.copy2(path, bak)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
        print(f"  ✔ {r['name']} → LIVE_TRADING = False")
        print(f"    원본 백업: {os.path.basename(bak)}")
        changed.append(r["name"])
    return changed


def main():
    ap = argparse.ArgumentParser(description="봇 점검·기록 수집·분석 한 번에")
    ap.add_argument("root", help="봇 폴더들이 들어 있는 상위 폴더")
    ap.add_argument("--live-bot", default=DEFAULT_LIVE_BOT,
                    help=f"실거래로 유지할 폴더명 (기본: {DEFAULT_LIVE_BOT})")
    ap.add_argument("--dry-run", action="store_true",
                    help="아무것도 바꾸지 않고 무엇을 할지만 보여줌")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"폴더를 찾을 수 없습니다: {root}")
        raise SystemExit(1)

    print()
    print("#" * 70)
    print(f"#  봇 일괄 점검  —  {datetime.now(KST):%Y-%m-%d %H:%M} KST")
    print(f"#  대상: {root}")
    if args.dry_run:
        print("#  [DRY-RUN] 아무 파일도 바꾸지 않습니다")
    print("#" * 70)

    # ── 1. 전수 조사 ──────────────────────────────────────────
    step(1, "봇 폴더 전수 조사")
    rows = survey(root)
    if not rows:
        print("  봇 폴더를 찾지 못했습니다.")
        raise SystemExit(1)

    print(f"  {'봇':<34}{'실거래':>7}{'실행중':>7}{'승률':>8}{'누적손익':>12}")
    for r in sorted(rows, key=lambda x: x["name"]):
        st = r["st"]
        n = st.get("wins", 0) + st.get("losses", 0)
        wr = f"{st['wins']/n*100:.1f}%" if n else "-"
        pnl = f"{st.get('pnl', 0):+,.2f}" if n else "-"
        print(f"  {r['name']:<34}{'예' if r['live'] else '-':>7}"
              f"{'예' if r['running'] else '-':>7}{wr:>8}{pnl:>12}")

    live_running = [r for r in rows if r["live"] and r["running"]]
    if len(live_running) > 1:
        print("\n  ⛔ 실거래 봇이 둘 이상 돌고 있습니다 — 하나를 즉시 끄세요:")
        for r in live_running:
            print(f"     {r['path']}")

    # ── 2. 안전 조치 ──────────────────────────────────────────
    step(2, f"안전 조치 — 실거래는 '{args.live_bot}' 하나만 유지")
    extra = [r for r in rows if r["live"] and r["name"] != args.live_bot]
    if not extra:
        print("  손댈 것 없습니다 (실거래 설정 폴더가 하나뿐).")
    else:
        print(f"  실거래 설정이 켜진 다른 폴더 {len(extra)}개를 끕니다.")
        print("  → 실수로 실행해도 실주문이 나가지 않습니다.\n")
        disable_extra_live(rows, args.live_bot, args.dry_run)

    if not any(r["name"] == args.live_bot for r in rows):
        print(f"\n  ⚠ '{args.live_bot}' 폴더를 찾지 못했습니다.")
        print("    --live-bot 으로 정확한 폴더명을 지정하세요.")

    # ── 3. 기록 수집 ──────────────────────────────────────────
    step(3, "과거 거래 기록 수집")
    records = import_history.collect(root, "paper", 8)
    existing = {import_history._key(r) for r in trade_journal.load()}
    fresh, seen = [], set()
    for r in records:
        k = import_history._key(r)
        if k in existing or k in seen:
            continue
        seen.add(k)
        fresh.append(r)
    import_history.summarize(fresh)

    if fresh and not args.dry_run:
        for r in fresh:
            trade_journal.append(r)
        print(f"\n  ✔ {len(fresh)}건을 {trade_journal.JOURNAL_FILE} 에 추가")
    elif args.dry_run:
        print("\n  [dry-run] 저장하지 않음")

    # ── 4. 분석 ───────────────────────────────────────────────
    step(4, "손실 분석")
    trades = trade_journal.load()
    if not trades:
        print("  분석할 거래가 없습니다.")
        return

    try:
        import config
        min_profit = config.MIN_PROFIT_USD
    except Exception:
        min_profit = 2.0

    rep = analyze.build_report(trades, min_profit)
    buf = io.StringIO()
    with redirect_stdout(buf):
        analyze.print_report(rep, min_profit, "전체 기간 (가져온 기록 포함)")
    text = buf.getvalue()
    print(text)

    out = os.path.join(HERE, "report.txt")
    if not args.dry_run:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"  ✔ 리포트 저장: {out}")

    # ── 마무리 ────────────────────────────────────────────────
    print()
    print("#" * 70)
    print("#  다음 할 일 (지켜볼 수 있을 때)")
    print("#" * 70)
    print(f"""
  1) 실거래 봇에 안전장치 적용
       cd "{os.path.join(root, args.live_bot)}"
       git fetch origin
       git merge origin/claude/github-push-time-check-ioamx1
       python tests\\run_all.py
       # 그 다음 봇 재시작

     ※ 지금 자동으로 하지 않았습니다. 워치독이 재시작하면 지켜보지 않는
       사이에 새 로직(ADX 필터·수익보존락·거버너·거래소 STOP)이 적용됩니다.
       코인 선정 기준이 바뀌므로 처음 몇 시간은 보고 있는 게 좋습니다.

  2) report.txt 를 저장해 두었습니다. 내용을 붙여넣어 주시면
     파라미터 조정안을 근거와 함께 만들어 드립니다.
""")


if __name__ == "__main__":
    main()
