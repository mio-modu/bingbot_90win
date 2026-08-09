"""
거래 저널 · MAE/MFE 계측 테스트
────────────────────────────────
    python tests/test_journal.py
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import trade_journal                                   # noqa: E402
import paper_trader                                    # noqa: E402
import risk_governor                                   # noqa: E402
import config                                          # noqa: E402
import analyze                                         # noqa: E402

_TMP = tempfile.mkdtemp(prefix="jrntest_")
trade_journal.JOURNAL_FILE = os.path.join(_TMP, "trades.jsonl")
risk_governor.STATE_FILE   = os.path.join(_TMP, "gov.json")
config.LIVE_TRADING = False


def new_trader(tag: str):
    paper_trader.STATE_FILE = os.path.join(_TMP, f"state_{tag}.json")
    return paper_trader.PaperTrader(live_api=None)


def run_trade(pt, symbol, trend, entry, path, reason, dca_at=None):
    """path: 진입 후 거쳐가는 가격들 / dca_at: 물타기를 넣을 인덱스"""
    pt.open_position(symbol, trend, entry)
    for i, px in enumerate(path):
        pt.update_price_hist(px, 1_000_000)
        if dca_at and i in dca_at:
            pt.position.apply_avg_down(
                px, pt.position.next_avg_down_amount(),
                kind="sideways" if i % 2 else "price")
    return pt.close_position(path[-1], reason)


def test_records_written():
    print("\n[1] 청산마다 저널에 기록되는가")
    trade_journal.JOURNAL_FILE = os.path.join(_TMP, "t1.jsonl")
    pt = new_trader("t1")
    run_trade(pt, "AAAUSDT", "UP",   100.0, [99.6, 99.4, 100.5, 101.2, 101.0], "익절+1%")
    run_trade(pt, "BBBUSDT", "UP",    50.0, [50.4, 50.6, 49.8, 49.0, 48.6], "최대손실손절")
    run_trade(pt, "CCCUSDT", "DOWN",  10.0, [10.05, 10.12, 10.3, 10.45, 10.5],
              "하드캡손절", dca_at=[0, 2, 3])
    recs = trade_journal.load(trade_journal.JOURNAL_FILE)
    for r in recs:
        print(f"    {r['symbol']:<9} {r['reason']:<14} "
              f"pnl=${r['pnl']:+8.2f} step={r['avg_down_step']}")
    assert len(recs) == 3, f"저널 건수 불일치: {len(recs)}"
    print("    ✅")


def test_excursion_measured():
    """손실 거래의 MFE = 놓친 익절 / 익절 거래의 MAE = 손절선 하한선"""
    print("\n[2] MAE/MFE 계측")
    trade_journal.JOURNAL_FILE = os.path.join(_TMP, "t2.jsonl")
    pt = new_trader("t2")
    # 한때 유리했다가 무너지는 거래
    run_trade(pt, "BBBUSDT", "UP", 50.0, [50.4, 50.6, 49.8, 49.0, 48.6], "최대손실손절")
    r = trade_journal.load(trade_journal.JOURNAL_FILE)[0]
    print(f"    pnl ${r['pnl']:+.2f} | MFE ${r['mfe_usd']:+.2f} "
          f"| MAE ${r['mae_usd']:+.2f}")
    print(f"    시각 {r['open_kst']} → {r['close_kst']}")
    assert r["pnl"] < 0,        "손실 거래여야 함"
    assert r["mfe_usd"] > 0,    "손실 거래인데 MFE 가 기록되지 않음 (놓친 익절 측정 불가)"
    assert r["mae_usd"] < 0,    "MAE 미기록"
    assert r["open_kst"] and r["close_kst"], "시각 미기록"
    assert r["capital_before"] != r["capital_after"], "자본 스냅샷 미기록"
    print("    ✅ 손실 거래인데 한때 +$%.2f 였다 → '놓친 익절' 로 집계된다"
          % r["mfe_usd"])


def test_step_history():
    print("\n[3] DCA 단계 이력")
    trade_journal.JOURNAL_FILE = os.path.join(_TMP, "t3.jsonl")
    pt = new_trader("t3")
    run_trade(pt, "CCCUSDT", "DOWN", 10.0, [10.05, 10.12, 10.3, 10.45, 10.5],
              "하드캡손절", dca_at=[0, 2, 3])
    steps = trade_journal.load(trade_journal.JOURNAL_FILE)[0]["steps"]
    for s in steps:
        print(f"    {s['step']}단계 {s['kind']:<9} +${s['add_usd']:>6.0f} "
              f"→ 누적 ${s['total_invested']:.0f}")
    assert len(steps) == 4, f"단계 이력 누락: {steps}"
    assert steps[0]["kind"] == "entry"
    assert any(s["kind"] == "sideways" for s in steps), \
        "횡보 DCA 태깅 실패 — 발동 사유를 사후 구분할 수 없다"
    print("    ✅ 가격 트리거와 횡보 강제 투입이 구분된다")


def test_survives_restart():
    print("\n[4] 재시작 후에도 계측이 이어지는가")
    trade_journal.JOURNAL_FILE = os.path.join(_TMP, "t4.jsonl")
    pt = new_trader("t4")
    pt.open_position("DDDUSDT", "UP", 20.0)
    pt.update_price_hist(19.5, 1_000_000)
    pt.save_state()
    mae_before   = pt.position.mae_usd
    steps_before = len(pt.position.step_history)

    pt2 = paper_trader.PaperTrader(live_api=None)   # 같은 state 파일에서 복원
    print(f"    MAE {mae_before:.4f} → {pt2.position.mae_usd:.4f} / "
          f"단계이력 {steps_before} → {len(pt2.position.step_history)}")
    assert abs(pt2.position.mae_usd - mae_before) < 1e-9, "재시작 후 MAE 소실"
    assert len(pt2.position.step_history) == steps_before, "재시작 후 단계이력 소실"
    print("    ✅")


def test_journal_failure_is_harmless():
    """저널 기록 실패가 매매를 막으면 안 된다"""
    print("\n[5] 저널 장애가 매매를 막지 않는가")
    trade_journal.JOURNAL_FILE = "/nonexistent-dir/cannot-write.jsonl"
    pt = new_trader("t5")
    pt.open_position("EEEUSDT", "UP", 10.0)
    trade = pt.close_position(10.5, "익절+1%")
    print(f"    저널 경로 불가 상태에서 청산: pnl ${trade.get('pnl', 0):+.2f}")
    assert trade and pt.position is None, "저널 실패가 청산을 막았다"
    print("    ✅ 경고만 남기고 매매는 계속된다")


def test_analyzer_runs():
    print("\n[6] 분석기가 저널을 읽는가")
    trade_journal.JOURNAL_FILE = os.path.join(_TMP, "t6.jsonl")
    pt = new_trader("t6")
    run_trade(pt, "AAAUSDT", "UP", 100.0, [100.5, 101.2, 101.0], "익절+1%")
    run_trade(pt, "BBBUSDT", "UP",  50.0, [50.4, 49.0, 48.6], "최대손실손절")
    trades = trade_journal.load(trade_journal.JOURNAL_FILE)
    rep = analyze.build_report(trades, config.MIN_PROFIT_USD)
    s = rep["summary"]
    print(f"    {s['n']}건 | 승률 {s['win_rate']:.0%} | "
          f"총손익 ${s['total']:+.2f} | 손익분기 승률 {s['breakeven_wr']:.1%}")
    assert s["n"] == 2
    assert rep["by_reason"] and rep["excursion"]["measured"] == 2
    # 빈 저널에서도 죽지 않아야 한다
    empty = analyze.build_report([], config.MIN_PROFIT_USD)
    assert empty["summary"]["n"] == 0
    print("    ✅ 빈 저널에서도 예외 없이 동작")


def main():
    print("=" * 62)
    print("  거래 저널 · MAE/MFE 테스트")
    print("=" * 62)
    for fn in [test_records_written, test_excursion_measured, test_step_history,
               test_survives_restart, test_journal_failure_is_harmless,
               test_analyzer_runs]:
        fn()
    print("\n" + "=" * 62)
    print("  ✅ 전부 통과")
    print("=" * 62)


if __name__ == "__main__":
    main()
