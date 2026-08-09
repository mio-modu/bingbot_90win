"""
과거 기록 가져오기 (Import History)
───────────────────────────────────
다른 봇·다른 PC 의 모의투자/실거래 기록을 trades.jsonl 로 합친다.
읽기 전용 — 원본 파일은 건드리지 않는다.

    python import_history.py <경로>              # 폴더나 파일
    python import_history.py <경로> --dry-run    # 무엇이 들어올지만 확인
    python import_history.py <경로> --tag paper  # 출처 라벨 지정

지원 형식
  · state.json / state*.json / *.bak / *.old   (closed_trades 배열)
  · trades.jsonl                                (이미 저널 형식)
  · bot.log / *.log                             (청산 로그 줄 → 시각 확보)

핵심: 구 기록에도 **peak_price 가 들어 있다.**
  MFE(보유 중 최고 수익)를 역산할 수 있다는 뜻이고,
  그러면 "놓친 익절" 분석을 과거 데이터에도 적용할 수 있다.
  단 peak_price 는 DCA 때마다 리셋되므로, DCA 를 거친 거래에서는
  **마지막 단계 이후의 고점**만 반영된다 — 실제 MFE 의 하한선이다.
  역산값은 mfe_est_usd 로 따로 기록하고 estimated=True 로 표시한다.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
from datetime import datetime, timedelta, timezone

import trade_journal

KST = timezone(timedelta(hours=9))

# bot.log 청산 줄 — paper_trader.close_position() 의 로그 포맷을 따른다
LOG_CLOSE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[,.]?\d*\s+"
    r"\[[A-Z]+\]\s+\[청산\s*(?P<icon>[^\]]*)\]\s+"
    r"(?P<symbol>\S+)\s*\|\s*사유:\s*(?P<reason>[^|]+?)\s*\|"
    r".*?총손익:\s*\$(?P<gross>[-+]?[\d.]+)"
    r".*?비용:\s*\$(?P<cost>[-+]?[\d.]+)"
    r".*?순손익:\s*\$(?P<pnl>[-+]?[\d.]+)"
)


def _norm_trend(rec: dict) -> str:
    """trend(UP/DOWN) 와 direction(long/short) 두 형식을 통일"""
    t = rec.get("trend")
    if t in ("UP", "DOWN"):
        return t
    d = str(rec.get("direction", "")).lower()
    if d in ("long", "buy"):
        return "UP"
    if d in ("short", "sell"):
        return "DOWN"
    return "?"


def _favorable(base: float, price: float, trend: str) -> float:
    """진입 대비 **유리한 방향**으로 움직인 폭 (양수 = 이익 방향)"""
    return (price - base) if trend == "UP" else (base - price)


def _estimate_mfe(rec: dict, trend: str, leverage: int) -> tuple[float, float]:
    """peak_price 로 MFE 를 역산한다 → (gross, net)

    두 경로:
      ① total_invested 가 있으면 (90win 계열)
         gross = 투입 × 유리변화율 × 레버리지
      ② 없으면 (슬롯형 기록) pnl·fee·가격으로 **수량을 역산**한다.
         gross(청산) = qty × 유리변화폭(청산) = pnl + fee
         → qty = (pnl + fee) / 유리변화폭(청산)
         → gross(고점) = qty × 유리변화폭(고점)
         레버리지를 가정할 필요가 없어 오히려 더 정확하다.

    반환 (0.0, 0.0) = 역산 불가
    """
    peak = rec.get("peak_price") or 0.0
    base = rec.get("avg_price") or rec.get("entry_price") or 0.0
    if not peak or not base or trend not in ("UP", "DOWN"):
        return 0.0, 0.0

    peak_move = _favorable(base, peak, trend)
    if peak_move <= 0:
        return 0.0, 0.0          # 고점이 진입가보다 불리 → 이익난 적 없음

    cost = rec.get("cost")
    if cost is None:
        cost = rec.get("fee", 0.0) or 0.0

    invested = rec.get("total_invested")
    if invested:
        gross = invested * (peak_move / base) * leverage
        return gross, gross - cost

    # ② 수량 역산 경로
    exit_price = rec.get("exit_price")
    pnl = rec.get("pnl")
    if exit_price is None or pnl is None:
        return 0.0, 0.0
    exit_move = _favorable(base, exit_price, trend)
    if abs(exit_move) < base * 1e-9:
        return 0.0, 0.0          # 진입가와 청산가가 사실상 같음 → 수량 역산 불가
    qty = (pnl + cost) / exit_move
    if qty <= 0:
        return 0.0, 0.0
    gross = qty * peak_move
    return gross, gross - cost


def _from_closed_trades(path: str, tag: str, leverage: int) -> list[dict]:
    """state.json 계열에서 closed_trades 추출"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"  ! {os.path.basename(path)} 읽기 실패: {e}")
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("closed_trades") or []
    out = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        trend = _norm_trend(r)
        mfe_gross, mfe_net = _estimate_mfe(r, trend, leverage)
        rec = {
            "symbol":         r.get("symbol", "?"),
            "trend":          trend,
            "reason":         r.get("reason") or r.get("exit_type") or "?",
            "avg_down_step":  r.get("avg_down_step", 0),
            "total_invested": r.get("total_invested"),
            "avg_price":      r.get("avg_price") or r.get("entry_price"),
            "entry_price":    r.get("entry_price"),
            "exit_price":     r.get("exit_price"),
            "peak_price":     r.get("peak_price"),
            "trail_active":   r.get("trail_active"),
            "gross_pnl":      r.get("gross_pnl"),
            "cost":           r.get("cost", r.get("fee")),
            "pnl":            r.get("pnl", 0.0),
            "duration_s":     r.get("duration_s"),
            "leverage":       leverage,
            # ── 가져온 기록 표시 ──
            "imported":   True,
            "backfill":   True,          # 실시간 MAE/MFE 계측값이 아님
            "source":     os.path.basename(path),
            "source_tag": tag,
        }
        if mfe_gross > 0:
            rec["mfe_est_usd"]   = round(mfe_net, 4)
            rec["mfe_est_gross"] = round(mfe_gross, 4)
            rec["mfe_estimated"] = True
        out.append(rec)
    return out


def _from_jsonl(path: str, tag: str) -> list[dict]:
    """이미 저널 형식인 파일"""
    out = []
    for r in trade_journal.load(path):
        r = dict(r)
        r.setdefault("imported", True)
        r.setdefault("source", os.path.basename(path))
        r.setdefault("source_tag", tag)
        out.append(r)
    return out


def _from_log(path: str, tag: str) -> list[dict]:
    """bot.log 청산 줄 — state.json 에 없는 **시각**을 준다"""
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        print(f"  ! {os.path.basename(path)} 읽기 실패: {e}")
        return []
    for line in lines:
        m = LOG_CLOSE_RE.match(line.strip())
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
            ts = ts.replace(tzinfo=KST)
        except ValueError:
            continue
        out.append({
            "symbol":     m.group("symbol"),
            "trend":      "?",
            "reason":     m.group("reason").strip(),
            "gross_pnl":  float(m.group("gross")),
            "cost":       float(m.group("cost")),
            "pnl":        float(m.group("pnl")),
            "close_time": ts.timestamp(),
            "close_kst":  ts.isoformat(timespec="seconds"),
            "avg_down_step": 0,
            "imported":   True,
            "backfill":   True,
            "from_log":   True,
            "source":     os.path.basename(path),
            "source_tag": tag,
        })
    return out


def _key(r: dict) -> tuple:
    """중복 판정 키 — 같은 거래가 state.json 과 .bak 양쪽에 있을 수 있다"""
    return (
        r.get("symbol"),
        round(float(r.get("pnl") or 0), 6),
        round(float(r.get("duration_s") or 0), 1),
        round(float(r.get("exit_price") or 0), 10),
        r.get("close_kst") or "",
    )


def collect(root: str, tag: str, leverage: int) -> list[dict]:
    """경로(파일 또는 폴더)에서 거래를 모두 긁어온다"""
    if os.path.isfile(root):
        paths = [root]
    else:
        paths = []
        for pat in ("state*.json*", "*.jsonl", "*.log", "*.log.*", "*.old", "*.bak"):
            paths += glob.glob(os.path.join(root, "**", pat), recursive=True)
        paths = sorted(set(paths))

    found = []
    for p in paths:
        name = os.path.basename(p)
        if name == os.path.basename(trade_journal.JOURNAL_FILE):
            continue                       # 자기 자신은 건너뛴다
        if p.endswith(".jsonl"):
            recs = _from_jsonl(p, tag)
        elif ".log" in name:
            recs = _from_log(p, tag)
        else:
            recs = _from_closed_trades(p, tag, leverage)
        if recs:
            print(f"  · {name:<34} {len(recs):>5}건")
            found += recs
    return found


def summarize(records: list[dict]):
    if not records:
        print("\n  가져올 거래가 없습니다.")
        return
    pnls = [float(r.get("pnl") or 0) for r in records]
    wins = [p for p in pnls if p > 0]
    est  = [r for r in records if r.get("mfe_estimated")]
    withtime = [r for r in records if r.get("close_kst")]

    print(f"\n  총 {len(records)}건")
    print(f"  승/패        : {len(wins)}W / {len(pnls)-len(wins)}L "
          f"(승률 {len(wins)/len(pnls)*100:.1f}%)")
    print(f"  총손익       : ${sum(pnls):+,.2f}")
    print(f"  MFE 역산 가능: {len(est)}건 "
          f"({len(est)/len(records)*100:.0f}% — 놓친 익절 분석 대상)")
    print(f"  시각 있음    : {len(withtime)}건 "
          f"({len(withtime)/len(records)*100:.0f}% — 시간대 분석 대상)")

    if est:
        missed = [r for r in est
                  if float(r.get("pnl") or 0) <= 0
                  and float(r.get("mfe_est_usd") or 0) > 0]
        if missed:
            total = sum(float(r["mfe_est_usd"]) for r in missed)
            losers = [r for r in est if float(r.get("pnl") or 0) <= 0]
            print(f"\n  ▸ 손실 거래 {len(losers)}건 중 {len(missed)}건은 "
                  f"보유 중 이익 구간을 지났습니다")
            print(f"    합계 ${total:,.2f} — 잡을 수 있었던 수익의 하한선")


def main():
    ap = argparse.ArgumentParser(description="과거 거래 기록을 저널로 가져오기")
    ap.add_argument("path", help="기록이 있는 폴더 또는 파일")
    ap.add_argument("--tag", default="imported",
                    help="출처 라벨 (예: paper, notebook, gateio)")
    ap.add_argument("--leverage", type=int, default=None,
                    help="그 기록을 만든 봇의 레버리지 (기본: 현재 config)")
    ap.add_argument("--out", default=trade_journal.JOURNAL_FILE,
                    help="저장할 저널 파일")
    ap.add_argument("--dry-run", action="store_true",
                    help="무엇이 들어올지 확인만 하고 쓰지 않음")
    args = ap.parse_args()

    leverage = args.leverage
    if leverage is None:
        try:
            import config
            leverage = config.LEVERAGE
        except Exception:
            leverage = 8

    if not os.path.exists(args.path):
        print(f"경로를 찾을 수 없습니다: {args.path}")
        raise SystemExit(1)

    print(f"\n탐색: {args.path}  (레버리지 {leverage}배 기준으로 MFE 역산)")
    records = collect(args.path, args.tag, leverage)

    # 기존 저널 및 자기들끼리 중복 제거
    existing = {_key(r) for r in trade_journal.load(args.out)}
    fresh, dup = [], 0
    seen = set()
    for r in records:
        k = _key(r)
        if k in existing or k in seen:
            dup += 1
            continue
        seen.add(k)
        fresh.append(r)

    summarize(fresh)
    if dup:
        print(f"\n  중복 {dup}건 제외")

    if args.dry_run:
        print("\n  [dry-run] 아무것도 쓰지 않았습니다.")
        return
    if not fresh:
        return

    saved = trade_journal.JOURNAL_FILE
    trade_journal.JOURNAL_FILE = args.out
    try:
        for r in fresh:
            trade_journal.append(r)
    finally:
        trade_journal.JOURNAL_FILE = saved

    print(f"\n  ✔ {len(fresh)}건을 {args.out} 에 추가했습니다.")
    print(f"    이제 분석하세요:  python analyze.py")


if __name__ == "__main__":
    main()
