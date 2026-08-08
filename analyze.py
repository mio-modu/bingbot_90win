"""
손실 분석기 (Loss Autopsy)
─────────────────────────
trades.jsonl 을 읽어 "어디서 돈이 새는가"를 숫자로 보여준다.
매매 로직은 건드리지 않는다 — 읽기 전용 분석 도구.

사용법:
    python analyze.py                # 전체 기간 리포트
    python analyze.py --days 7       # 최근 7일
    python analyze.py --json         # 기계 판독용 (자동 튜너 입력)
    python analyze.py --min-trades 30

핵심 산출물:
  1. 기대값 / 손익분기 승률  — 현재 손익비로 살아남을 수 있는지
  2. 청산사유·DCA단계·코인·시간대별 손익 귀속 — 손실의 출처
  3. MFE/MAE 진단           — 놓친 익절 / 너무 넓은 손절
  4. 손절선 스캔            — 손절 한도를 얼마로 두면 총손익이 최대였나
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from datetime import datetime

import trade_journal

# 손절선 스캔 후보 ($)
STOP_CANDIDATES = [15, 20, 30, 40, 50, 60, 80, 100, 125, 150, 200, 250]


# ────────────────────────────────────────────────────────────
#  집계 도우미
# ────────────────────────────────────────────────────────────

def _agg(trades: list[dict]) -> dict:
    """거래 묶음의 기본 통계"""
    n      = len(trades)
    pnls   = [t.get("pnl", 0.0) for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total  = sum(pnls)
    return {
        "n":         n,
        "wins":      len(wins),
        "losses":    len(losses),
        "win_rate":  len(wins) / n if n else 0.0,
        "total":     total,
        "avg":       total / n if n else 0.0,
        "avg_win":   sum(wins) / len(wins) if wins else 0.0,
        "avg_loss":  sum(losses) / len(losses) if losses else 0.0,
        "best":      max(pnls) if pnls else 0.0,
        "worst":     min(pnls) if pnls else 0.0,
        "gross_win":  sum(wins),
        "gross_loss": -sum(losses),   # 양수
    }


def _group(trades: list[dict], keyfn) -> list[tuple]:
    """keyfn 으로 묶어 총손익 오름차순(손실 큰 순) 정렬"""
    buckets = defaultdict(list)
    for t in trades:
        buckets[keyfn(t)].append(t)
    rows = [(k, _agg(v)) for k, v in buckets.items()]
    rows.sort(key=lambda r: r[1]["total"])
    return rows


def _fmt_group(title: str, rows: list[tuple], limit: int = 12, note: str = ""):
    print(f"\n── {title} " + "─" * max(0, 52 - len(title)))
    if note:
        print(f"   {note}")
    print(f"   {'구분':<26}{'건수':>5}{'승률':>8}{'총손익':>11}{'평균':>9}{'최악':>9}")
    for k, a in rows[:limit]:
        label = str(k)
        if len(label) > 25:
            label = label[:24] + "…"
        print(f"   {label:<26}{a['n']:>5}{a['win_rate']*100:>7.0f}%"
              f"{a['total']:>+11.2f}{a['avg']:>+9.2f}{a['worst']:>+9.2f}")
    if len(rows) > limit:
        print(f"   … 외 {len(rows)-limit}개 그룹")


def _max_consec_loss(trades: list[dict]) -> tuple[int, float]:
    """최대 연속 손실 횟수 및 그 구간 손실액"""
    best_n, best_sum = 0, 0.0
    cur_n, cur_sum = 0, 0.0
    for t in trades:
        if t.get("pnl", 0.0) <= 0:
            cur_n += 1
            cur_sum += t["pnl"]
            if cur_n > best_n or (cur_n == best_n and cur_sum < best_sum):
                best_n, best_sum = cur_n, cur_sum
        else:
            cur_n, cur_sum = 0, 0.0
    return best_n, best_sum


def _max_drawdown(trades: list[dict]) -> float:
    """누적 손익 곡선 기준 최대 낙폭 ($)"""
    peak, equity, mdd = 0.0, 0.0, 0.0
    for t in trades:
        equity += t.get("pnl", 0.0)
        peak = max(peak, equity)
        mdd = min(mdd, equity - peak)
    return mdd


def _hour_kst(t: dict):
    iso = t.get("open_kst") or ""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).hour
    except ValueError:
        return None


# ────────────────────────────────────────────────────────────
#  MFE / MAE 진단
# ────────────────────────────────────────────────────────────

def _excursion_report(trades: list[dict], min_profit: float) -> dict:
    """
    MFE(최대 유리) / MAE(최대 불리) 기반 진단.
    계측값이 있는 거래(저널 도입 이후)만 대상으로 한다.
    """
    measured = [t for t in trades if not t.get("backfill") and "mfe_usd" in t]
    if not measured:
        return {"measured": 0}

    losers  = [t for t in measured if t.get("pnl", 0.0) <= 0]
    winners = [t for t in measured if t.get("pnl", 0.0) > 0]

    # 손실 거래인데 도중에 익절 조건($min_profit)을 넘긴 적이 있는 거래
    missed = [t for t in losers if t.get("mfe_usd", 0.0) >= min_profit]
    missed_value = sum(t.get("mfe_usd", 0.0) for t in missed)

    # 익절 거래가 견뎠던 최대 역행폭 (손절선을 조일 때 죽는 거래 판단용)
    win_maes = sorted(-t.get("mae_usd", 0.0) for t in winners)

    def _pct(vals, q):
        if not vals:
            return 0.0
        idx = min(len(vals) - 1, int(len(vals) * q))
        return vals[idx]

    return {
        "measured":       len(measured),
        "losers":         len(losers),
        "winners":        len(winners),
        "missed_n":       len(missed),
        "missed_ratio":   len(missed) / len(losers) if losers else 0.0,
        "missed_value":   missed_value,
        "win_mae_p50":    _pct(win_maes, 0.50),
        "win_mae_p90":    _pct(win_maes, 0.90),
        "win_mae_p99":    _pct(win_maes, 0.99),
        "win_mae_max":    win_maes[-1] if win_maes else 0.0,
    }


def _stop_scan(trades: list[dict]) -> list[dict]:
    """
    손절 한도 후보별 총손익 재시뮬레이션.

    ※ 근사치다. MAE 가 후보선을 넘었다면 그 거래는 해당 손실로 끝났다고 가정한다.
      실제로는 조기 손절 시 이후의 DCA·회복이 사라지므로, 여기서 나온 숫자는
      "방향"을 보는 용도이지 그대로 믿을 값이 아니다.
    """
    measured = [t for t in trades if not t.get("backfill") and "mae_usd" in t]
    if not measured:
        return []
    out = []
    for cap in STOP_CANDIDATES:
        total, stopped = 0.0, 0
        for t in measured:
            mae = t.get("mae_usd", 0.0)
            pnl = t.get("pnl", 0.0)
            if mae <= -cap:
                total += -cap
                stopped += 1
            else:
                total += pnl
        out.append({
            "cap":         cap,
            "total":       round(total, 2),
            "stopped":     stopped,
            "stop_ratio":  stopped / len(measured),
        })
    return out


# ────────────────────────────────────────────────────────────
#  리포트
# ────────────────────────────────────────────────────────────

def build_report(trades: list[dict], min_profit: float = 2.0) -> dict:
    a = _agg(trades)
    consec_n, consec_sum = _max_consec_loss(trades)
    payoff = (a["avg_win"] / abs(a["avg_loss"])) if a["avg_loss"] else 0.0
    breakeven_wr = (1 / (1 + payoff)) if payoff else 1.0
    pf = (a["gross_win"] / a["gross_loss"]) if a["gross_loss"] else float("inf")

    return {
        "summary": {
            **a,
            "payoff":            payoff,
            "breakeven_wr":      breakeven_wr,
            "wr_margin":         a["win_rate"] - breakeven_wr,
            "profit_factor":     pf,
            "max_consec_losses": consec_n,
            "max_consec_loss_usd": consec_sum,
            "max_drawdown":      _max_drawdown(trades),
        },
        "by_reason":    [(k, v) for k, v in _group(trades, lambda t: t.get("reason", "?"))],
        "by_step":      [(k, v) for k, v in _group(trades, lambda t: f"DCA {t.get('avg_down_step', 0)}단계")],
        "by_trend":     [(k, v) for k, v in _group(trades, lambda t: t.get("trend", "?"))],
        "by_symbol":    [(k, v) for k, v in _group(trades, lambda t: t.get("symbol", "?"))],
        "by_hour":      [(k, v) for k, v in _group(
                            [t for t in trades if _hour_kst(t) is not None],
                            lambda t: f"{_hour_kst(t):02d}시 (KST)")],
        "excursion":    _excursion_report(trades, min_profit),
        "stop_scan":    _stop_scan(trades),
    }


def _suggestions(rep: dict, min_profit: float) -> list[str]:
    """리포트에서 기계적으로 도출되는 개선 후보. 판단은 사람이 한다."""
    s = rep["summary"]
    ex = rep["excursion"]
    out = []

    if s["n"] == 0:
        return ["거래 기록이 없습니다."]

    if s["wr_margin"] < 0:
        out.append(
            f"승률 {s['win_rate']*100:.1f}% < 손익분기 승률 {s['breakeven_wr']*100:.1f}% "
            f"— 현재 손익비({s['avg_win']:+.2f} vs {s['avg_loss']:+.2f})로는 구조적 적자입니다. "
            f"손절을 줄이거나 익절을 키워야 하며, 승률을 더 올리는 방향은 한계가 있습니다."
        )
    else:
        out.append(
            f"승률 {s['win_rate']*100:.1f}%가 손익분기 {s['breakeven_wr']*100:.1f}%를 "
            f"{s['wr_margin']*100:+.1f}%p 상회 — 현 손익비에서는 유지 가능합니다. "
            f"단 여유가 좁으면 승률이 조금만 흔들려도 적자로 뒤집힙니다."
        )

    # 손실이 집중된 청산 사유
    worst_reasons = [(k, v) for k, v in rep["by_reason"] if v["total"] < 0][:3]
    for k, v in worst_reasons:
        share = v["total"] / -s["gross_loss"] * 100 if s["gross_loss"] else 0
        out.append(
            f"청산사유 '{k}' — {v['n']}건에서 ${v['total']:+.2f} "
            f"(총손실의 {share:.0f}%). 이 경로를 먼저 손보는 게 효율이 가장 큽니다."
        )

    # DCA 단계별 손실 집중
    worst_steps = [(k, v) for k, v in rep["by_step"] if v["total"] < 0][:2]
    for k, v in worst_steps:
        out.append(
            f"{k} — {v['n']}건 ${v['total']:+.2f} (평균 {v['avg']:+.2f}). "
            f"이 단계까지 끌고 가는 것이 이득인지 재검토 필요."
        )

    # 놓친 익절
    if ex.get("measured") and ex.get("losers"):
        if ex["missed_ratio"] >= 0.3:
            out.append(
                f"손실 거래 {ex['losers']}건 중 {ex['missed_n']}건"
                f"({ex['missed_ratio']*100:.0f}%)은 보유 중 한때 +${min_profit} 이상 수익이었습니다 "
                f"(합계 MFE ${ex['missed_value']:.2f}). 익절/트레일이 너무 늦게 반응합니다 — "
                f"TRAIL_ACTIVATE_PCT 하향 또는 부분 익절 도입 검토."
            )

    # 손절선 조이기 여지
    scan = rep["stop_scan"]
    if scan:
        best = max(scan, key=lambda r: r["total"])
        cur  = scan[-1]
        if best["cap"] < cur["cap"] and best["total"] > cur["total"]:
            out.append(
                f"손절선 스캔: -${best['cap']} 였다면 총손익 ${best['total']:+.2f} "
                f"(현재 수준 -${cur['cap']} 대비 {best['total']-cur['total']:+.2f}). "
                f"단 조기 손절로 사라졌을 회복분은 반영되지 않은 근사치입니다."
            )
        if ex.get("win_mae_p90"):
            out.append(
                f"익절 거래의 90%는 -${ex['win_mae_p90']:.2f} 이내 역행에서 살아났습니다 "
                f"(최악 -${ex['win_mae_max']:.2f}). 손절선을 이보다 위로 올리면 "
                f"멀쩡한 승리 거래까지 잘라내게 됩니다 — 이 값이 손절선의 하한선입니다."
            )

    if s["max_consec_losses"] >= 3:
        out.append(
            f"최대 연속 손실 {s['max_consec_losses']}회 (${s['max_consec_loss_usd']:+.2f}). "
            f"연속 손실 시 시드를 줄이는 규칙이 없다면 추가할 가치가 있습니다."
        )

    return out


def print_report(rep: dict, min_profit: float, period_label: str):
    s = rep["summary"]
    print()
    print("=" * 64)
    print(f"  손실 분석 리포트  —  {period_label}")
    print("=" * 64)

    if s["n"] == 0:
        print("\n  분석할 거래가 없습니다. (trades.jsonl 비어 있음)")
        print("  봇이 몇 건 청산하고 나면 다시 실행하세요.\n")
        return

    print(f"\n  거래       : {s['n']}건  ({s['wins']}W / {s['losses']}L, 승률 {s['win_rate']*100:.1f}%)")
    print(f"  총손익     : ${s['total']:+,.2f}   (거래당 기대값 ${s['avg']:+.2f})")
    print(f"  평균 익절  : ${s['avg_win']:+.2f}      평균 손절: ${s['avg_loss']:+.2f}")
    print(f"  손익비     : 1 : {1/s['payoff']:.1f}" if s["payoff"] else "  손익비     : n/a")
    print(f"  손익분기 승률: {s['breakeven_wr']*100:.1f}%  "
          f"(현재 {s['win_rate']*100:.1f}%, 여유 {s['wr_margin']*100:+.1f}%p)")
    print(f"  Profit Factor: {s['profit_factor']:.2f}   최대낙폭: ${s['max_drawdown']:+,.2f}")
    print(f"  최대 연속손실: {s['max_consec_losses']}회 (${s['max_consec_loss_usd']:+,.2f})")
    print(f"  최고/최악  : ${s['best']:+.2f} / ${s['worst']:+.2f}")

    _fmt_group("청산 사유별 (손실 큰 순)", rep["by_reason"])
    _fmt_group("DCA 단계별", rep["by_step"])
    _fmt_group("방향별", rep["by_trend"])
    _fmt_group("코인별 (손실 큰 순)", rep["by_symbol"], limit=10)
    if rep["by_hour"]:
        _fmt_group("시간대별 (진입 시각 KST)", rep["by_hour"], limit=24)

    ex = rep["excursion"]
    print("\n── MFE / MAE 진단 " + "─" * 39)
    if not ex.get("measured"):
        print("   계측 데이터 없음 — 저널 도입 이후 청산된 거래부터 집계됩니다.")
    else:
        print(f"   계측 거래           : {ex['measured']}건 "
              f"(승 {ex['winners']} / 패 {ex['losers']})")
        print(f"   놓친 익절           : {ex['missed_n']}건 "
              f"({ex['missed_ratio']*100:.0f}% of 손실거래) — "
              f"한때 +${min_profit} 이상이었다가 손실로 마감")
        print(f"   그 거래들의 MFE 합계 : ${ex['missed_value']:,.2f}  ← 잡을 수 있었던 수익")
        print(f"   익절거래 역행폭 분포 : p50 -${ex['win_mae_p50']:.2f} / "
              f"p90 -${ex['win_mae_p90']:.2f} / p99 -${ex['win_mae_p99']:.2f} / "
              f"최악 -${ex['win_mae_max']:.2f}")

    scan = rep["stop_scan"]
    if scan:
        print("\n── 손절 한도 스캔 " + "─" * 39)
        print("   ※ 근사치: 조기 손절 시 사라졌을 이후 회복분은 반영 안 됨. 방향 참고용.")
        print(f"   {'손절선':>8}{'총손익':>12}{'손절된 거래':>13}")
        best = max(scan, key=lambda r: r["total"])
        for r in scan:
            mark = "  ← 최대" if r is best else ""
            print(f"   {'-$'+str(r['cap']):>8}{r['total']:>+12.2f}"
                  f"{r['stopped']:>8} ({r['stop_ratio']*100:.0f}%){mark}")

    print("\n── 개선 후보 " + "─" * 44)
    for i, line in enumerate(_suggestions(rep, min_profit), 1):
        print(f"   {i}. {line}")
    print()
    print("   ※ 위는 과거 데이터에서 기계적으로 뽑은 후보일 뿐입니다.")
    print("     표본이 작으면 우연을 규칙으로 착각하기 쉽습니다 —")
    print("     최소 수십 건 이상 쌓인 뒤에 판단하세요.")
    print()


def main():
    ap = argparse.ArgumentParser(description="거래 저널 손실 분석")
    ap.add_argument("--days", type=float, default=0,
                    help="최근 N일만 분석 (0 = 전체)")
    ap.add_argument("--min-trades", type=int, default=1,
                    help="이 건수 미만이면 경고만 출력")
    ap.add_argument("--min-profit", type=float, default=None,
                    help="'놓친 익절' 판정 기준 ($). 기본값은 config.MIN_PROFIT_USD")
    ap.add_argument("--json", action="store_true", help="JSON 출력 (자동 튜너 입력용)")
    ap.add_argument("--file", default=trade_journal.JOURNAL_FILE,
                    help="분석할 저널 파일 (기본: trades.jsonl)")
    args = ap.parse_args()

    min_profit = args.min_profit
    if min_profit is None:
        try:
            import config
            min_profit = config.MIN_PROFIT_USD
        except Exception:
            min_profit = 2.0

    trades = trade_journal.load(args.file)
    label = "전체 기간"
    if args.days > 0:
        cutoff = time.time() - args.days * 86400
        trades = [t for t in trades if t.get("close_time", 0) >= cutoff]
        label = f"최근 {args.days:g}일"

    rep = build_report(trades, min_profit)

    if args.json:
        print(json.dumps({"period": label, "report": rep,
                          "suggestions": _suggestions(rep, min_profit)},
                         ensure_ascii=False, indent=2, default=str))
        return

    print_report(rep, min_profit, label)
    if 0 < len(trades) < args.min_trades:
        print(f"  ⚠ 표본 {len(trades)}건 — {args.min_trades}건 미만이라 통계적 의미가 약합니다.\n")


if __name__ == "__main__":
    main()
