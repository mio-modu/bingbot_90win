"""
손절 분석 엔진 (Loss Autopsy)
──────────────────────────────
    python loss_autopsy.py              # 전체
    python loss_autopsy.py --days 7     # 최근 7일
    python loss_autopsy.py --json       # 기계용 출력

무엇을 하나
-----------
진 거래를 그냥 세지 않는다. **왜 졌는지 원인별로 나눈다.**
원인이 다르면 고쳐야 할 곳도 다르기 때문이다.

    "10번 져서 -$80" 은 아무것도 말해주지 않는다.
    "이익 갔다가 되돌려준 게 6건 -$12,
     처음부터 반대로 간 게 3건 -$55,
     물타기 3단계까지 끌고 간 게 1건 -$13"
    은 무엇을 고칠지 말해준다. 각각 트레일링·진입근거·물타기의 문제다.

원인 분류 (앞에서부터 먼저 걸리는 것)
------------------------------------
  1. 시장충격   급락손절 · BTC시장충격 — 개별 판단의 문제가 아니다
  2. 되돌려줌   한때 비용의 1.5배 넘게 벌었는데 손실로 마감
                → 익절·트레일링의 문제
  3. 즉시역행   비용조차 못 넘겨봄. 진입 직후부터 반대로 갔다
                → 진입 근거(코인 선정·방향)의 문제
  4. 물타기심화 2단계 이상 끌고 가서 크게 짐
                → 물타기 깊이·손절선의 문제
  5. 횡보소모   양쪽으로 거의 안 움직이고 비용만 나감
                → 코인 선정(변동성)·보유시간의 문제
  6. 정상손절   0~1단계에서 규칙대로 잘림 — 구조적 문제 아님

비용(cost)을 기준 단위로 쓴다. 절대 금액은 자본 규모에 따라 달라지지만
"수수료의 몇 배를 벌었나"는 규모와 무관하게 비교할 수 있다.
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trade_journal   # noqa: E402

KST = timezone(timedelta(hours=9))

# ── 분류 임계값 ───────────────────────────────────────────
GIVEBACK_MFE_COST_MULT = 1.5   # 비용의 몇 배 넘게 벌었어야 "되돌려줌"인가
GIVEBACK_MFE_MIN_USD   = 1.0   # 다만 이 금액 미만이면 노이즈로 본다
IMMEDIATE_MFE_COST_MULT = 0.5  # 비용의 절반도 못 벌었으면 "즉시역행"
CHOP_RANGE_COST_MULT   = 1.0   # MFE+|MAE| 가 비용 이하면 "횡보소모"
DEEP_DCA_STEP          = 2     # 이 단계 이상이면 "물타기심화"

SHOCK_REASONS = ("급락손절", "BTC시장충격", "BTC급락")

CAUSES = ["시장충격", "되돌려줌", "즉시역행", "물타기심화", "횡보소모", "정상손절"]

FIX_HINT = {
    "되돌려줌":   "익절·트레일링 (DCA_TRAIL_ACTIVATE_PCT / PROFIT_LOCK_*)",
    "즉시역행":   "진입 근거 — 코인 선정·방향 판정 (recency / ADX / 신선도)",
    "물타기심화": "물타기 깊이·손절선 (MAX_DCA_STAGES / MAX_NET_LOSS_*)",
    "횡보소모":   "코인 선정(변동성)·보유시간 (RECENT_VOL_* / FLAT_TIMEOUT_MIN)",
    "시장충격":   "개별 판단 문제 아님 — 시장 전체. 거버너가 담당",
    "정상손절":   "구조적 문제 아님 — 규칙대로 잘렸다",
}


def _f(rec: dict, key: str, default=0.0) -> float:
    v = rec.get(key, default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def classify(rec: dict) -> str:
    """진 거래 하나의 패배 원인. 이긴 거래에는 쓰지 않는다."""
    reason = str(rec.get("reason", ""))
    if any(s in reason for s in SHOCK_REASONS):
        return "시장충격"

    cost = _f(rec, "cost")
    mfe  = _f(rec, "mfe_usd")
    mae  = abs(_f(rec, "mae_usd"))
    step = int(_f(rec, "avg_down_step"))

    # 비용이 기록되지 않은 옛 기록은 투입금으로 추정한다
    if cost <= 0:
        cost = max(0.01, _f(rec, "total_invested") * 0.016)

    if mfe >= max(cost * GIVEBACK_MFE_COST_MULT, GIVEBACK_MFE_MIN_USD):
        return "되돌려줌"
    if step >= DEEP_DCA_STEP:
        return "물타기심화"
    if (mfe + mae) <= cost * CHOP_RANGE_COST_MULT:
        return "횡보소모"
    if mfe < cost * IMMEDIATE_MFE_COST_MULT:
        return "즉시역행"
    return "정상손절"


def _median(xs: list):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def autopsy(trades: list[dict]) -> dict:
    wins   = [t for t in trades if _f(t, "pnl") > 0]
    losses = [t for t in trades if _f(t, "pnl") <= 0]

    buckets = defaultdict(list)
    for t in losses:
        buckets[classify(t)].append(t)

    rows = []
    for cause in CAUSES:
        group = buckets.get(cause, [])
        if not group:
            continue
        pnls = [_f(t, "pnl") for t in group]
        rows.append({
            "cause":     cause,
            "n":         len(group),
            "total":     sum(pnls),
            "avg":       sum(pnls) / len(group),
            "worst":     min(pnls),
            "med_step":  _median([_f(t, "avg_down_step") for t in group]),
            "med_mfe":   _median([_f(t, "mfe_usd") for t in group]),
            "med_mae":   _median([abs(_f(t, "mae_usd")) for t in group]),
            "med_min":   _median([_f(t, "duration_s") / 60 for t in group]),
            "symbols":   _top_symbols(group),
            "fix":       FIX_HINT.get(cause, ""),
        })
    rows.sort(key=lambda r: r["total"])   # 손실이 큰 원인부터

    return {
        "n_trades": len(trades),
        "n_wins":   len(wins),
        "n_losses": len(losses),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "gross_win":  sum(_f(t, "pnl") for t in wins),
        "gross_loss": sum(_f(t, "pnl") for t in losses),
        "net":        sum(_f(t, "pnl") for t in trades),
        "avg_win":    (sum(_f(t, "pnl") for t in wins) / len(wins)) if wins else 0.0,
        "avg_loss":   (sum(_f(t, "pnl") for t in losses) / len(losses)) if losses else 0.0,
        "causes":     rows,
        "symbols":    _symbol_table(trades),
        "entry_split": _entry_split(wins, losses),
    }


def _top_symbols(group: list[dict], k: int = 3) -> list[tuple]:
    agg = defaultdict(float)
    cnt = defaultdict(int)
    for t in group:
        agg[t.get("symbol", "?")] += _f(t, "pnl")
        cnt[t.get("symbol", "?")] += 1
    out = sorted(agg.items(), key=lambda kv: kv[1])[:k]
    return [(s, round(v, 2), cnt[s]) for s, v in out]


def _symbol_table(trades: list[dict]) -> list[dict]:
    agg = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0})
    for t in trades:
        s = t.get("symbol", "?")
        agg[s]["n"] += 1
        agg[s]["pnl"] += _f(t, "pnl")
        if _f(t, "pnl") > 0:
            agg[s]["wins"] += 1
    rows = [{"symbol": s, **v, "wr": v["wins"] / v["n"] if v["n"] else 0.0}
            for s, v in agg.items()]
    rows.sort(key=lambda r: r["pnl"])
    return rows


# 진입 시점 지표 — 이긴 거래와 진 거래에서 이 값들이 실제로 달랐는가.
# 달랐다면 그 지표는 쓸모가 있고, 같았다면 판단 근거로 못 쓴다.
_SPLIT_KEYS = ("score", "adx", "consistency", "atr_ratio", "recent_vol_1h",
               "fresh_ext", "fresh_age", "fresh_mom", "conv_mult", "change_24h")


def _entry_split(wins: list[dict], losses: list[dict]) -> list[dict]:
    out = []
    for k in _SPLIT_KEYS:
        wv = [_f(t.get("entry_ctx", {}), k, None) for t in wins]
        lv = [_f(t.get("entry_ctx", {}), k, None) for t in losses]
        wv = [x for x in wv if x is not None]
        lv = [x for x in lv if x is not None]
        if len(wv) < 3 or len(lv) < 3:
            continue
        mw, ml = _median(wv), _median(lv)
        if mw is None or ml is None:
            continue
        base = max(abs(mw), abs(ml), 1e-9)
        out.append({"key": k, "win": mw, "loss": ml,
                    "gap": (mw - ml) / base, "n_win": len(wv), "n_loss": len(lv)})
    out.sort(key=lambda r: -abs(r["gap"]))
    return out


# ────────────────────────────────────────────────────────────
#  출력
# ────────────────────────────────────────────────────────────
def print_report(rep: dict, period: str):
    W = 66
    print("=" * W)
    print(f"  손절 분석 — {period}")
    print("=" * W)

    if not rep["n_trades"]:
        print("\n  거래 기록이 없습니다.")
        print("  거래가 쌓이면 여기서 '왜 졌는지'가 원인별로 나옵니다.")
        print("  (옛 기록 가져오기: python import_history.py)")
        return

    print(f"\n  거래 {rep['n_trades']}건 | 승 {rep['n_wins']} 패 {rep['n_losses']} "
          f"| 승률 {rep['win_rate']:.1%}")
    print(f"  순손익 ${rep['net']:+,.2f} "
          f"(이익 ${rep['gross_win']:+,.2f} / 손실 ${rep['gross_loss']:+,.2f})")
    print(f"  평균 이익 ${rep['avg_win']:+.2f} | 평균 손실 ${rep['avg_loss']:+.2f}")

    # 손익비와 손익분기 승률 — 승률만 보면 안 되는 이유를 숫자로 보여준다
    if rep["avg_win"] > 0 and rep["avg_loss"] < 0:
        payoff = rep["avg_win"] / abs(rep["avg_loss"])
        be = 1 / (1 + payoff)
        print(f"  손익비 1 : {1/payoff:.1f}  →  본전 승률 {be:.1%} 필요 "
              f"(현재 {rep['win_rate']:.1%})")
        if rep["win_rate"] < be:
            print("     ⚠ 지금 승률로는 구조적으로 잃는다. 승률이 아니라 "
                  "손익비를 고쳐야 한다.")

    if not rep["causes"]:
        print("\n  진 거래가 없습니다.")
    else:
        print("\n" + "-" * W)
        print("  왜 졌나 (손실 큰 순)")
        print("-" * W)
        for r in rep["causes"]:
            share = r["total"] / rep["gross_loss"] if rep["gross_loss"] else 0
            print(f"\n  ▸ {r['cause']}  {r['n']}건  "
                  f"${r['total']:+,.2f} (전체 손실의 {share:.0%})")
            print(f"      1건 평균 ${r['avg']:+.2f} / 최악 ${r['worst']:+.2f}")
            print(f"      중앙값: 물타기 {r['med_step']:.0f}단계 · "
                  f"최고이익 ${r['med_mfe']:.2f} · 최대역행 ${r['med_mae']:.2f} · "
                  f"보유 {r['med_min']:.0f}분")
            if r["symbols"]:
                syms = " / ".join(f"{s} ${v:+.1f}({n}회)" for s, v, n in r["symbols"])
                print(f"      주로: {syms}")
            print(f"      고칠 곳: {r['fix']}")

    if rep["entry_split"]:
        print("\n" + "-" * W)
        print("  진입 지표 — 이긴 거래 vs 진 거래 (차이 큰 순)")
        print("-" * W)
        print("  차이가 작은 지표는 판단 근거로 쓸모가 없다는 뜻이다.")
        for r in rep["entry_split"][:8]:
            mark = "★" if abs(r["gap"]) >= 0.20 else " "
            print(f"   {mark} {r['key']:16s} 승 {r['win']:>9.4f} | "
                  f"패 {r['loss']:>9.4f} | 차이 {r['gap']:+.0%}")

    bad = [s for s in rep["symbols"] if s["n"] >= 3 and s["pnl"] < 0][:5]
    if bad:
        print("\n" + "-" * W)
        print("  반복해서 잃은 코인 (3회 이상)")
        print("-" * W)
        for s in bad:
            print(f"    {s['symbol']:20s} {s['n']:2d}회 승률 {s['wr']:5.0%} "
                  f"${s['pnl']:+8.2f}")
        print("\n  ※ 자동으로 차단하지는 않습니다. 확인 후 결정하세요:")
        print("     coin_scanner.py 의 BLACKLIST 에 추가")

    print("\n" + "=" * W)
    for line in suggestions(rep):
        print(f"  {line}")
    print("=" * W)


def suggestions(rep: dict) -> list[str]:
    """원인 분포에서 나오는 다음 행동. 추측이 아니라 데이터가 가리키는 것만."""
    out = []
    n = rep["n_trades"]
    if n < 20:
        out.append(f"거래 {n}건 — 아직 판단하기 이릅니다. 30건 넘으면 다시 보세요.")
        return out

    total_loss = rep["gross_loss"]
    if not total_loss:
        out.append("진 거래가 없습니다.")
        return out

    by = {r["cause"]: r for r in rep["causes"]}

    def share(c):
        r = by.get(c)
        return (r["total"] / total_loss) if r and total_loss else 0.0

    if share("되돌려줌") >= 0.30:
        r = by["되돌려줌"]
        out.append(
            f"손실의 {share('되돌려줌'):.0%} 가 '벌었다가 되돌려준' 것입니다 "
            f"({r['n']}건, 중앙값 최고이익 ${r['med_mfe']:.2f}).")
        out.append("  → 트레일 발동을 앞당기거나(DCA_TRAIL_ACTIVATE_PCT), "
                   "수익 보존 락을 강화하세요(PROFIT_LOCK_KEEP_RATIO).")
    if share("즉시역행") >= 0.30:
        out.append(
            f"손실의 {share('즉시역행'):.0%} 가 진입 직후부터 반대로 간 것입니다.")
        out.append("  → 방향 판정의 문제입니다. RECENCY_* 를 조이거나 "
                   "ADX 하한을 올리세요.")
    if share("물타기심화") >= 0.30:
        r = by["물타기심화"]
        out.append(
            f"손실의 {share('물타기심화'):.0%} 가 물타기 {r['med_step']:.0f}단계 "
            f"이상에서 났습니다 (최악 ${r['worst']:+.0f}).")
        out.append("  → MAX_DCA_STAGES 를 줄이거나 "
                   "MAX_NET_LOSS_CAPITAL_RATIO 를 낮추세요.")
    if share("횡보소모") >= 0.25:
        out.append(
            f"손실의 {share('횡보소모'):.0%} 가 움직이지 않는 코인에서 "
            "수수료만 나간 것입니다.")
        out.append("  → RECENT_VOL_MIN_1H 를 올리거나 "
                   "FLAT_TIMEOUT_MIN 을 줄이세요.")

    strong = [r for r in rep["entry_split"] if abs(r["gap"]) >= 0.20]
    if strong:
        names = ", ".join(r["key"] for r in strong[:3])
        out.append(f"승패를 실제로 가른 지표: {names} — 여기에 가중치를 더 주세요.")
    weak = [r for r in rep["entry_split"] if abs(r["gap"]) < 0.05]
    if len(weak) >= 3:
        names = ", ".join(r["key"] for r in weak[:3])
        out.append(f"승패와 무관한 지표: {names} — 점수 계산에서 빼도 됩니다.")

    if not out:
        out.append("특정 원인에 손실이 몰려 있지 않습니다. 구조 변경보다 "
                   "지금 설정을 유지하며 표본을 더 모으세요.")
    return out


# ────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="손절 분석 엔진")
    ap.add_argument("--days", type=int, default=0, help="최근 N일만 (0=전체)")
    ap.add_argument("--file", default=None, help="저널 파일 경로")
    ap.add_argument("--json", action="store_true", help="기계용 JSON 출력")
    args = ap.parse_args()

    path = args.file or trade_journal.JOURNAL_FILE
    trades = trade_journal.load(path)

    period = "전체 기간"
    if args.days > 0:
        cutoff = (datetime.now(KST) - timedelta(days=args.days)).timestamp()
        trades = [t for t in trades if _f(t, "close_time") >= cutoff]
        period = f"최근 {args.days}일"

    rep = autopsy(trades)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    else:
        print_report(rep, period)
    return 0


if __name__ == "__main__":
    sys.exit(main())
