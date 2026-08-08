"""
진화 제안 생성기 (Proposal Only)
────────────────────────────────
trades.jsonl 을 분석해 config.py 변경안을 **제안만** 한다.
이 스크립트는 config.py 를 절대 수정하지 않는다. 적용은 사람이 판단한다.

    python evolve.py              # 제안 출력 + proposals/ 에 저장
    python evolve.py --stdout     # 파일 저장 없이 출력만

왜 자동 적용을 하지 않는가
  과거 데이터에 맞춰 파라미터를 자동으로 깎으면 과최적화된다.
  표본 30건짜리 "발견"은 대개 우연이고, 그 우연에 맞춰 봇을 바꾸면
  다음 30건에서 더 크게 잃는다. 제안에는 항상 표본 수를 함께 적는다.

가드레일
  · 전체 표본 MIN_TRADES 미만 → 아무 제안도 하지 않음
  · 근거가 되는 부분집합이 MIN_SUBGROUP 미만 → 제안하되 "저신뢰"로 표시
  · 한 번에 현재값 대비 MAX_CHANGE_RATIO 이상 바꾸는 제안은 그 선까지만
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone

import analyze
import config
import trade_journal

KST = timezone(timedelta(hours=9))
PROPOSAL_DIR = os.path.join(os.path.dirname(__file__), "proposals")

MIN_TRADES       = 30    # 이 미만이면 제안 자체를 하지 않는다
MIN_SUBGROUP     = 10    # 근거 부분집합이 이 미만이면 저신뢰 표시
MAX_CHANGE_RATIO = 0.30  # 한 번에 현재값의 30% 초과 변경 금지


class Proposal:
    def __init__(self, param, current, proposed, why, evidence_n,
                 caveat="", impact=""):
        self.param      = param
        self.current    = current
        self.proposed   = proposed
        self.why        = why
        self.evidence_n = evidence_n
        self.caveat     = caveat
        self.impact     = impact

    @property
    def low_confidence(self) -> bool:
        return self.evidence_n < MIN_SUBGROUP

    def clamp(self):
        """현재값 대비 과도한 변경을 MAX_CHANGE_RATIO 선까지 제한"""
        cur, new = self.current, self.proposed
        if not isinstance(cur, (int, float)) or not isinstance(new, (int, float)):
            return
        if cur == 0:
            return
        limit = abs(cur) * MAX_CHANGE_RATIO
        if abs(new - cur) > limit:
            capped = cur + (limit if new > cur else -limit)
            capped = round(capped, 4)
            self.caveat = (f"제안값 {new} → {capped} 로 축소 "
                           f"(1회 변경 상한 {MAX_CHANGE_RATIO:.0%}). "
                           + self.caveat).strip()
            self.proposed = capped


def _rule_stop_ceiling(rep, trades) -> Proposal | None:
    """손절 한도: 스캔 최적값과 익절거래 MAE 하한선 사이에서 제안"""
    scan = rep["stop_scan"]
    ex   = rep["excursion"]
    if not scan or not ex.get("measured"):
        return None
    best = max(scan, key=lambda r: r["total"])
    cur  = config.MAX_NET_LOSS_CEILING
    if best["total"] <= 0 or best["cap"] >= cur:
        return None

    # 익절 거래의 90% 가 견딘 역행폭보다 아래로는 내리지 않는다.
    # 그보다 조이면 멀쩡히 이겼을 거래까지 잘라낸다.
    floor = ex["win_mae_p90"]
    target = max(best["cap"], floor)
    if target >= cur:
        return None

    return Proposal(
        "MAX_NET_LOSS_CEILING", cur, round(target),
        why=(f"손절선 스캔에서 -${best['cap']} 가 총손익 최대 "
             f"(${best['total']:+.2f}). 다만 익절 거래의 90%가 "
             f"-${floor:.2f} 이내 역행에서 살아났으므로 그 아래로는 내리지 않음."),
        evidence_n=ex["measured"],
        caveat=("스캔은 근사치다 — 조기 손절 시 사라졌을 DCA 회복분이 "
                "반영돼 있지 않아 실제 개선폭은 이보다 작다."),
        impact=f"1회 최대 손실이 자본의 {cur/config.TOTAL_CAPITAL:.0%} → "
               f"{target/config.TOTAL_CAPITAL:.0%} 로 축소",
    )


def _rule_trail_activate(rep) -> Proposal | None:
    """놓친 익절이 많으면 트레일 활성화 시점을 앞당긴다"""
    ex = rep["excursion"]
    if not ex.get("losers") or ex["missed_ratio"] < 0.30:
        return None
    cur = config.TRAIL_ACTIVATE_PCT
    return Proposal(
        "TRAIL_ACTIVATE_PCT", cur, round(cur * 0.75, 4),
        why=(f"손실 거래 {ex['losers']}건 중 {ex['missed_n']}건"
             f"({ex['missed_ratio']:.0%})이 한때 익절선을 넘겼다가 손실로 마감. "
             f"합계 MFE ${ex['missed_value']:.2f} 를 흘려보냈다."),
        evidence_n=ex["missed_n"],
        caveat="트레일을 일찍 켜면 큰 추세를 일찍 놓칠 수 있다 — "
               "평균 익절액이 함께 줄어드는지 다음 리포트에서 확인할 것.",
        impact="수익 확정은 빨라지고, 대신 대박 거래의 크기는 줄어든다",
    )


def _rule_symbol_blacklist(rep) -> list[Proposal]:
    """반복적으로 잃는 코인 차단 제안"""
    out = []
    for sym, a in rep["by_symbol"][:3]:
        if a["n"] < MIN_SUBGROUP or a["total"] >= 0:
            continue
        if a["total"] > -config.MIN_PROFIT_USD * 10:
            continue   # 손실이 미미하면 굳이 차단하지 않음
        out.append(Proposal(
            f"코인 차단 목록에 {sym} 추가", "(없음)", sym,
            why=(f"{a['n']}건 거래에서 총 ${a['total']:+.2f} "
                 f"(승률 {a['win_rate']:.0%}, 평균 ${a['avg']:+.2f})."),
            evidence_n=a["n"],
            caveat="코인 성격은 시장 국면에 따라 변한다 — 영구 차단보다 "
                   "일정 기간 차단 후 재평가를 권한다.",
            impact=f"해당 코인을 뺐다면 총손익이 ${-a['total']:+.2f} 개선",
        ))
    return out


def _rule_dca_stage(rep) -> Proposal | None:
    """마지막 DCA 단계가 계속 손해면 단계 수 축소 제안"""
    cur = config.MAX_DCA_STAGES
    key = f"DCA {cur}단계"
    for k, a in rep["by_step"]:
        if k != key:
            continue
        if a["n"] < MIN_SUBGROUP or a["total"] >= 0:
            return None
        return Proposal(
            "MAX_DCA_STAGES", cur, cur - 1,
            why=(f"{key} 도달 거래 {a['n']}건이 총 ${a['total']:+.2f} "
                 f"(평균 ${a['avg']:+.2f}, 최악 ${a['worst']:+.2f}). "
                 f"이 단계까지 끌고 가서 얻는 것보다 잃는 것이 많다."),
            evidence_n=a["n"],
            caveat="단계를 줄이면 최대 노출도 함께 줄어 승률이 내려간다. "
                   "승률 하락분보다 손실 축소분이 큰지 확인 필요.",
            impact=f"최대 투입이 시드×{2**cur} → 시드×{2**(cur-1)} 로 축소",
        )
    return None


def _rule_direction(rep) -> Proposal | None:
    """한쪽 방향만 크게 잃으면 그 방향 일일 한도 강화"""
    rows = [(k, a) for k, a in rep["by_trend"] if a["n"] >= MIN_SUBGROUP]
    if len(rows) < 2:
        return None
    worst, best = rows[0], rows[-1]
    if worst[1]["total"] >= 0 or best[1]["total"] <= 0:
        return None
    gap = best[1]["avg"] - worst[1]["avg"]
    if gap < config.MIN_PROFIT_USD:
        return None
    cur = config.DAILY_DIR_LOSS_LIMIT
    return Proposal(
        "DAILY_DIR_LOSS_LIMIT", cur, round(cur * 0.7),
        why=(f"{worst[0]} {worst[1]['n']}건 ${worst[1]['total']:+.2f} vs "
             f"{best[0]} {best[1]['n']}건 ${best[1]['total']:+.2f}. "
             f"거래당 ${gap:.2f} 차이 — 한쪽 방향이 일방적으로 손실을 낸다."),
        evidence_n=min(worst[1]["n"], best[1]["n"]),
        caveat="방향 편향은 시장 추세의 반영일 뿐 전략의 결함이 아닐 수 있다. "
               "추세가 바뀌면 반대로 뒤집힌다.",
        impact="손실 방향이 하루에 낼 수 있는 손실 총량이 30% 축소",
    )


def collect(trades) -> list[Proposal]:
    rep = analyze.build_report(trades, config.MIN_PROFIT_USD)
    out = []
    for r in (_rule_stop_ceiling(rep, trades), _rule_trail_activate(rep),
              _rule_dca_stage(rep), _rule_direction(rep)):
        if r:
            out.append(r)
    out.extend(_rule_symbol_blacklist(rep))
    for p in out:
        p.clamp()
    return out, rep


def render(proposals, rep, trades) -> str:
    now = datetime.now(KST)
    s = rep["summary"]
    L = []
    L.append(f"# 진화 제안 — {now:%Y-%m-%d %H:%M} KST\n")
    L.append(f"표본 {s['n']}건 | 승률 {s['win_rate']:.1%} | "
             f"총손익 ${s['total']:+,.2f} | 기대값 ${s['avg']:+.2f}\n")
    L.append(f"손익분기 승률 {s['breakeven_wr']:.1%} "
             f"(여유 {s['wr_margin']*100:+.1f}%p) | "
             f"Profit Factor {s['profit_factor']:.2f}\n")

    if s["n"] < MIN_TRADES:
        L.append(f"\n> **표본 부족** — {MIN_TRADES}건 이상 쌓인 뒤에 제안합니다. "
                 f"지금 파라미터를 바꾸면 우연에 맞추게 됩니다.\n")
        return "\n".join(L)

    if not proposals:
        L.append("\n제안할 변경 사항이 없습니다. 현재 설정이 데이터와 크게 "
                 "어긋나지 않습니다.\n")
        return "\n".join(L)

    L.append(f"\n제안 {len(proposals)}건. **어느 것도 자동 적용되지 않았습니다.**\n")
    for i, p in enumerate(proposals, 1):
        tag = " ⚠️저신뢰" if p.low_confidence else ""
        L.append(f"\n## {i}. `{p.param}`{tag}\n")
        L.append(f"| | |\n|---|---|")
        L.append(f"| 현재 | `{p.current}` |")
        L.append(f"| 제안 | `{p.proposed}` |")
        L.append(f"| 근거 표본 | {p.evidence_n}건 |")
        L.append(f"\n**왜**: {p.why}\n")
        if p.impact:
            L.append(f"**효과**: {p.impact}\n")
        if p.caveat:
            L.append(f"**주의**: {p.caveat}\n")

    L.append("\n---\n")
    L.append("## 적용 전 확인\n")
    L.append("1. 한 번에 **하나만** 바꾸세요. 두 개를 동시에 바꾸면 "
             "어느 쪽이 효과였는지 영영 알 수 없습니다.\n")
    L.append("2. 바꾼 뒤 최소 30건이 쌓일 때까지 기다렸다가 다시 `analyze.py` "
             "를 돌려 비교하세요.\n")
    L.append("3. 개선되지 않았다면 되돌리세요. 되돌릴 수 있도록 변경 전 "
             "`config.py` 를 커밋해두세요.\n")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="config 변경안 제안 (적용 안 함)")
    ap.add_argument("--days", type=float, default=0, help="최근 N일만 사용")
    ap.add_argument("--stdout", action="store_true", help="파일 저장 없이 출력만")
    ap.add_argument("--file", default=trade_journal.JOURNAL_FILE)
    args = ap.parse_args()

    trades = trade_journal.load(args.file)
    if args.days > 0:
        import time
        cutoff = time.time() - args.days * 86400
        trades = [t for t in trades if t.get("close_time", 0) >= cutoff]

    proposals, rep = collect(trades)
    text = render(proposals, rep, trades)
    print(text)

    if not args.stdout and trades:
        os.makedirs(PROPOSAL_DIR, exist_ok=True)
        path = os.path.join(PROPOSAL_DIR,
                            f"{datetime.now(KST):%Y-%m-%d_%H%M}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"\n저장됨: {path}")


if __name__ == "__main__":
    main()
