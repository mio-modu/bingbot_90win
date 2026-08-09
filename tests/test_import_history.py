"""
과거 기록 가져오기 테스트
─────────────────────────
    python tests/test_import_history.py

핵심은 **MFE 역산**이다. 구 기록에는 실시간 계측값이 없지만 peak_price 가
남아 있어서 "보유 중 최고 수익"을 되살릴 수 있다.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import trade_journal                                   # noqa: E402
import import_history as imp                           # noqa: E402
import analyze                                         # noqa: E402

_TMP = tempfile.mkdtemp(prefix="imptest_")


def write_state(name: str, trades: list) -> str:
    path = os.path.join(_TMP, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"total_pnl": sum(t["pnl"] for t in trades),
                   "closed_trades": trades}, f)
    return path


def test_mfe_from_invested():
    """90win 계열 — total_invested 가 있으면 레버리지로 역산"""
    print("\n[1] MFE 역산 · 투입금 경로 (90win 계열)")
    # $240 투입, 8배, 진입 100 → 고점 101 (코인 +1%) → 포지션 +8% = $19.2 gross
    rec = {"avg_price": 100.0, "peak_price": 101.0, "exit_price": 99.0,
           "total_invested": 240.0, "cost": 3.84, "pnl": -23.0, "trend": "UP"}
    gross, net = imp._estimate_mfe(rec, "UP", 8)
    print(f"    투입 $240 × 코인 +1% × 8배 = gross ${gross:.2f} / net ${net:.2f}")
    assert abs(gross - 19.2) < 0.01, gross
    assert abs(net - (19.2 - 3.84)) < 0.01
    print("    ✅")


def test_mfe_from_qty_derivation():
    """슬롯형 — 투입금이 없어도 pnl·비용·가격으로 수량을 역산"""
    print("\n[2] MFE 역산 · 수량 역산 경로 (슬롯형)")
    # qty 를 1000 으로 가정하고 역산이 그 값을 되찾는지 본다
    entry, peak, exit_ = 10.0, 10.5, 9.8
    qty, fee = 1000.0, 2.0
    pnl = qty * (exit_ - entry) - fee          # = -202.0
    rec = {"entry_price": entry, "peak_price": peak, "exit_price": exit_,
           "fee": fee, "pnl": pnl, "direction": "long"}
    gross, net = imp._estimate_mfe(rec, "UP", 5)
    expected = qty * (peak - entry)            # = 500
    print(f"    실제 qty {qty:.0f} → 역산 gross ${gross:.2f} (정답 ${expected:.2f})")
    assert abs(gross - expected) < 0.01, gross
    print("    ✅ 레버리지를 몰라도 정확히 복원된다")


def test_mfe_skips_never_profitable():
    """한 번도 유리한 적 없는 거래는 역산하지 않는다"""
    print("\n[3] 이익난 적 없는 거래")
    rec = {"avg_price": 100.0, "peak_price": 99.5, "exit_price": 98.0,
           "total_invested": 100.0, "cost": 1.6, "pnl": -17.6, "trend": "UP"}
    gross, net = imp._estimate_mfe(rec, "UP", 8)
    print(f"    고점이 진입가보다 불리 → gross ${gross:.2f}")
    assert gross == 0.0
    # SHORT 도 방향이 뒤집혀야 한다
    rec_s = {"avg_price": 100.0, "peak_price": 98.0, "exit_price": 101.0,
             "total_invested": 100.0, "cost": 1.6, "pnl": -9.6, "trend": "DOWN"}
    g2, _ = imp._estimate_mfe(rec_s, "DOWN", 8)
    print(f"    SHORT 고점 98 (진입 100 대비 유리) → gross ${g2:.2f}")
    assert g2 > 0, "SHORT 방향 역산이 뒤집혔다"
    print("    ✅")


def test_dedupe():
    """state.json 과 .bak 에 같은 거래가 중복으로 들어 있다"""
    print("\n[4] 중복 제거")
    trades = [
        {"symbol": "AAA", "trend": "UP", "pnl": 5.0, "duration_s": 100.0,
         "exit_price": 1.05, "avg_price": 1.0, "peak_price": 1.06,
         "total_invested": 60.0, "cost": 0.96, "reason": "익절"},
        {"symbol": "BBB", "trend": "DOWN", "pnl": -3.0, "duration_s": 200.0,
         "exit_price": 2.02, "avg_price": 2.0, "peak_price": 1.98,
         "total_invested": 60.0, "cost": 0.96, "reason": "손절"},
    ]
    d = os.path.join(_TMP, "dup")
    os.makedirs(d, exist_ok=True)
    for name in ("state.json", "state.json.bak"):
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            json.dump({"closed_trades": trades}, f)

    recs = imp.collect(d, "test", 8)
    keys = {imp._key(r) for r in recs}
    print(f"    파일 2개 × 거래 2건 = {len(recs)}건 수집 → 고유 {len(keys)}건")
    assert len(recs) == 4 and len(keys) == 2
    print("    ✅")


def test_log_parsing():
    """bot.log 청산 줄에서 시각을 건진다"""
    print("\n[5] 로그 파싱 (시각 확보)")
    log = os.path.join(_TMP, "bot.log")
    with open(log, "w", encoding="utf-8") as f:
        f.write(
            "2026-08-09 07:50:12,345 [INFO] [청산 ✅] CFXUSDT | 사유: 트레일익절 | "
            "호가: 0.123456 → 체결가: 0.123400 | 총손익: $+6.1234 | "
            "비용: $3.8400 | 순손익: $+2.2834 | 누적: $+12.34\n"
            "2026-08-09 08:10:00,000 [INFO] [청산 ❌] TIAUSDT | 사유: 최대손실손절 | "
            "호가: 1.000000 → 체결가: 0.990000 | 총손익: $-40.0000 | "
            "비용: $3.8400 | 순손익: $-43.8400 | 누적: $-31.50\n"
            "2026-08-09 08:11:00,000 [INFO] [진입] TIAUSDT (UP) | 무관한 줄\n"
        )
    recs = imp._from_log(log, "test")
    for r in recs:
        print(f"    {r['close_kst']}  {r['symbol']:<9} {r['reason']:<12} "
              f"${r['pnl']:+.2f}")
    assert len(recs) == 2, f"청산 줄만 2개여야 함: {len(recs)}"
    assert all(r["close_kst"] for r in recs), "시각 미추출"
    assert abs(recs[0]["pnl"] - 2.2834) < 1e-6
    print("    ✅ 진입 줄은 무시하고 청산 줄에서 시각·손익을 얻는다")


def test_end_to_end_and_analyzer():
    print("\n[6] 가져오기 → 분석 연결")
    trades = []
    for i in range(12):
        win   = i % 3 != 0
        trend = "UP" if i % 2 else "DOWN"
        sign  = 1 if trend == "UP" else -1     # 유리한 방향
        base  = 100.0
        trades.append({
            "symbol": f"C{i%4}USDT", "trend": trend,
            "avg_price": base,
            # 손실 거래도 한때 유리한 구간을 지났다 (코인 +1.0% → 포지션 +8%)
            "peak_price": base + sign * (1.5 if win else 1.0),
            "exit_price": base + sign * (0.4 if win else -0.5),
            "total_invested": 60.0, "cost": 0.96,
            "pnl": 3.0 if win else -4.0,
            "duration_s": 300.0 + i, "avg_down_step": 0,
            "reason": "트레일익절" if win else "최대손실손절",
        })
    path = write_state("state_e2e.json", trades)

    out = os.path.join(_TMP, "journal_e2e.jsonl")
    recs = imp.collect(path, "paper", 8)
    saved = trade_journal.JOURNAL_FILE
    trade_journal.JOURNAL_FILE = out
    try:
        for r in recs:
            trade_journal.append(r)
    finally:
        trade_journal.JOURNAL_FILE = saved

    loaded = trade_journal.load(out)
    rep = analyze.build_report(loaded, 2.0)
    est = rep["estimated"]
    print(f"    {rep['summary']['n']}건 | 승률 {rep['summary']['win_rate']:.0%} | "
          f"총손익 ${rep['summary']['total']:+.2f}")
    print(f"    MFE 역산 대상 {est['n']}건 · 손실 {est['losers']}건 중 "
          f"{est['missed_n']}건이 한때 이익 (${est['missed_value']:.2f})")
    assert rep["summary"]["n"] == 12
    assert est["n"] == 12, "역산이 안 된 기록이 있다"
    assert est["missed_n"] > 0, "놓친 익절이 하나도 안 잡혔다"
    # 출처 라벨이 분리 집계되는지
    assert any(k == "paper" for k, _ in rep["by_source"])
    print("    ✅ 출처 라벨까지 분리 집계된다")


def main():
    print("=" * 62)
    print("  과거 기록 가져오기 테스트")
    print("=" * 62)
    for fn in [test_mfe_from_invested, test_mfe_from_qty_derivation,
               test_mfe_skips_never_profitable, test_dedupe,
               test_log_parsing, test_end_to_end_and_analyzer]:
        fn()
    print("\n" + "=" * 62)
    print("  ✅ 전부 통과")
    print("=" * 62)


if __name__ == "__main__":
    main()
