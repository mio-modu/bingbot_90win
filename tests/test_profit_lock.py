"""
수익 보존 락 테스트
───────────────────
    python tests/test_profit_lock.py

실측 사례를 재현한다: 2단계 $240 에서 순수익 $6 까지 갔다가
트레일 청산 $1. 왜 그렇게 되는지, 락이 무엇을 바꾸는지 검증한다.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import trade_journal                                   # noqa: E402
import risk_governor                                   # noqa: E402
import paper_trader                                    # noqa: E402
import config                                          # noqa: E402

_TMP = tempfile.mkdtemp(prefix="locktest_")
trade_journal.JOURNAL_FILE = os.path.join(_TMP, "trades.jsonl")
risk_governor.STATE_FILE   = os.path.join(_TMP, "gov.json")
config.LIVE_TRADING = False

LEV = config.LEVERAGE


def new_trader(tag: str):
    paper_trader.STATE_FILE = os.path.join(_TMP, f"state_{tag}.json")
    return paper_trader.PaperTrader(live_api=None)


def stage2_position(tag: str, entry: float = 1.0):
    """$240 투입(2단계) 포지션을 만든다"""
    pt = new_trader(tag)
    pt.open_position("XUSDT", "UP", entry)
    p = pt.position
    # 물타기 2회 → $60 → $120 → $240
    p.apply_avg_down(entry, p.next_avg_down_amount())
    p.apply_avg_down(entry, p.next_avg_down_amount())
    assert abs(p.total_invested - 240) < 1e-6, p.total_invested
    return pt, p


def price_for_position_pct(p, pct: float) -> float:
    """포지션 손익률 pct 에 해당하는 코인 가격 (LONG)"""
    return p.avg_price * (1 + pct / LEV)


def test_cost_and_breakeven():
    print("\n[1] 비용 구조")
    pt, p = stage2_position("cost")
    cost = p._total_cost()
    print(f"    투입 ${p.total_invested:.0f} / 명목 ${p.total_invested*LEV:,.0f}")
    print(f"    왕복 비용 ${cost:.2f} = 투입의 {cost/p.total_invested:.2%}")
    assert abs(cost / p.total_invested - 0.016) < 1e-6, "비용이 1.6% 가 아님"
    print(f"    트레일 활성화 {config.DCA_TRAIL_ACTIVATE_PCT:.1%} "
          f"> 분기점 1.6% ? {config.DCA_TRAIL_ACTIVATE_PCT > 0.016}")
    assert config.DCA_TRAIL_ACTIVATE_PCT > 0.016, \
        "활성화가 분기점 아래 — 트레일이 적자 구간에서 무장한다"
    print("    ✅ 활성화가 손익분기점 위에 있다")


def test_trail_min_distance_dominates():
    """비례 공식이 실제로는 거의 작동하지 않음을 문서화"""
    print("\n[2] 트레일 되돌림 — 비례 공식 vs 하한")
    pt, p = stage2_position("dist")
    print(f"    {'수익(포지션)':>12}{'비례계산':>12}{'실제거리':>10}{'포지션 반납':>12}")
    for pct in [0.02, 0.04, 0.06, 0.096, 0.15]:
        dynamic = pct * (config.DCA_TRAIL_BREATHING_RATIO / LEV)
        actual  = p._stepped_trail_distance(pct)
        print(f"    {pct:>11.1%}{dynamic:>12.5f}{actual:>10.5f}"
              f"{actual*LEV:>11.1%}")
    assert p._stepped_trail_distance(0.04) == config.DCA_TRAIL_DIST_MIN, \
        "수익 4% 에서 하한이 안 걸림"
    assert p._stepped_trail_distance(0.15) > config.DCA_TRAIL_DIST_MIN, \
        "수익 15% 에서도 하한이면 비례 공식이 완전히 죽은 것"
    print(f"    ✅ 수익 9.6% 미만에서는 하한이 지배 → 포지션 "
          f"{config.DCA_TRAIL_DIST_MIN*LEV:.1%}포인트 고정 반납")


def test_trail_alone_cannot_profit():
    """락이 없으면 고점 +4% 미만에서 트레일은 이익을 못 낸다"""
    print("\n[3] 락 없이 트레일만 — 고점별 결과")
    config.PROFIT_LOCK_ENABLED = False
    try:
        print(f"    {'고점':>8}{'청산':>9}{'순손익':>10}")
        results = {}
        for peak_pct in [0.02, 0.03, 0.04, 0.06]:
            pt, p = stage2_position(f"noLock{int(peak_pct*1000)}")
            peak_price = price_for_position_pct(p, peak_pct)
            pt.update_trail(peak_price)                 # 고점 찍기
            dist = p._stepped_trail_distance(peak_pct)
            exit_price = p.trail_sl if p.trail_sl else peak_price * (1 - dist)
            net = p.realized_pnl(exit_price)
            print(f"    {peak_pct:>7.1%}{p.pnl_pct(exit_price):>9.1%}{net:>+10.2f}")
            results[peak_pct] = net
        assert results[0.02] < 0, "고점 +2% 인데 이익이 났다 — 계산 확인 필요"
        assert results[0.06] > 0
        print("    ✅ 고점 +4% 부근이 손익분기 — 그 아래는 트레일이 적자를 만든다")
    finally:
        config.PROFIT_LOCK_ENABLED = True


def test_lock_saves_the_real_trade():
    """★ 실측 재현 — 순수익 $6 고점에서 무엇이 달라지는가"""
    print("\n[4] ★ 실측 재현: 2단계 $240, 순수익 $6 고점")
    target_net = 6.0

    # 락 없이
    config.PROFIT_LOCK_ENABLED = False
    pt, p = stage2_position("real_off")
    cost = p._total_cost()
    peak_pct = (target_net + cost) / p.total_invested      # 순 $6 이 되는 포지션 %
    peak_price = price_for_position_pct(p, peak_pct)
    pt.update_trail(peak_price)
    exit_no_lock = p.realized_pnl(p.trail_sl)
    print(f"    고점 포지션 {peak_pct:.2%} (순수익 ${target_net:+.2f})")
    print(f"    락 OFF → 트레일 SL 에서 청산 시 순 ${exit_no_lock:+.2f}")

    # 락 있음 — 가격이 내려오는 과정을 틱 단위로 재생
    config.PROFIT_LOCK_ENABLED = True
    pt2, p2 = stage2_position("real_on")
    TICKS = 400          # 봇은 10초마다 판단한다 — 가격 하락을 촘촘히 재생
    for step in range(0, TICKS + 1):
        pct = peak_pct * (1 - step / float(TICKS))       # 고점 → 0 까지 하락
        px  = price_for_position_pct(p2, pct)
        pt2.update_price_hist(px, 1_000_000)
        pt2.update_trail(px)
        if pt2.should_take_profit(px):
            reason = pt2.take_profit_reason(px)
            exit_lock = p2.realized_pnl(px)
            print(f"    락 ON  → {reason}")
            print(f"             순 ${exit_lock:+.2f} "
                  f"(고점 기록 ${p2.peak_realized:+.2f})")
            break
    else:
        raise AssertionError("락 ON 인데 청산 조건이 한 번도 안 걸렸다")

    print(f"    개선폭: ${exit_no_lock:+.2f} → ${exit_lock:+.2f} "
          f"({exit_lock - exit_no_lock:+.2f})")
    floor = p2.peak_realized * config.PROFIT_LOCK_KEEP_RATIO
    assert exit_lock > exit_no_lock, "락이 오히려 나쁜 결과를 냈다"
    # 틱 단위로 판단하므로 하한을 정확히 맞추지는 못하고 한 틱만큼 밑돈다.
    # 그 오차가 하한의 2% 이내인지만 본다.
    assert exit_lock >= floor * 0.98, \
        f"락이 고점의 절반을 지키지 못했다 (하한 ${floor:.2f}, 실제 ${exit_lock:.2f})"
    print("    ✅")


def test_lock_never_creates_a_loss():
    """락은 이익 구간에서만 발동 — 손실을 만들 수 없다"""
    print("\n[5] 락이 손실을 만들 수 있는가")
    pt, p = stage2_position("safety")
    # 한 번도 이익이 난 적 없는 포지션
    for pct in [-0.01, -0.03, -0.05]:
        px = price_for_position_pct(p, pct)
        pt.update_price_hist(px, 1_000_000)
        assert not pt.is_profit_lock_hit(px), \
            f"손실 구간({pct:.0%})에서 락이 발동했다"
    print(f"    손실 구간 전 구간에서 미발동 (peak_realized ${p.peak_realized:+.2f})")

    # 트리거 미만의 소액 이익에서도 발동하지 않아야 한다
    small = (config.PROFIT_LOCK_TRIGGER_USD * 0.5 + p._total_cost()) / p.total_invested
    px = price_for_position_pct(p, small)
    pt.update_price_hist(px, 1_000_000)
    pt.update_price_hist(price_for_position_pct(p, small * 0.1), 1_000_000)
    assert not pt.is_profit_lock_hit(price_for_position_pct(p, small * 0.1)), \
        "트리거 미만 수익에서 락이 발동했다"
    print(f"    트리거 ${config.PROFIT_LOCK_TRIGGER_USD} 미만 수익에서도 미발동")
    print("    ✅ 락은 이익을 줄일 수만 있고 손실을 만들 수 없다")


def test_lock_resets_on_dca():
    """DCA 후에는 이전 고점이 무의미 — 리셋되지 않으면 즉시 오발동"""
    print("\n[6] DCA 시 락 리셋")
    pt = new_trader("reset")
    pt.open_position("XUSDT", "UP", 1.0)
    p = pt.position
    up = price_for_position_pct(p, 0.10)
    pt.update_price_hist(up, 1_000_000)
    before = p.peak_realized
    print(f"    DCA 전 고점 순수익 ${before:+.2f}")
    assert before > 0
    p.apply_avg_down(1.0, p.next_avg_down_amount())
    print(f"    DCA 후 고점 순수익 ${p.peak_realized:+.2f}")
    assert p.peak_realized == 0.0, "DCA 후에도 이전 고점이 남아 즉시 락이 걸린다"
    print("    ✅")


def test_persists_across_restart():
    print("\n[7] 재시작 후에도 고점이 유지되는가")
    pt = new_trader("persist")
    pt.open_position("XUSDT", "UP", 1.0)
    pt.update_price_hist(price_for_position_pct(pt.position, 0.08), 1_000_000)
    before = pt.position.peak_realized
    pt.save_state()
    pt2 = paper_trader.PaperTrader(live_api=None)
    print(f"    ${before:+.2f} → ${pt2.position.peak_realized:+.2f}")
    assert abs(pt2.position.peak_realized - before) < 1e-9, \
        "재시작으로 고점이 사라져 락이 풀린다"
    print("    ✅")


def main():
    print("=" * 62)
    print("  수익 보존 락 테스트")
    print("=" * 62)
    for fn in [test_cost_and_breakeven, test_trail_min_distance_dominates,
               test_trail_alone_cannot_profit, test_lock_saves_the_real_trade,
               test_lock_never_creates_a_loss, test_lock_resets_on_dca,
               test_persists_across_restart]:
        fn()
    print("\n" + "=" * 62)
    print("  ✅ 전부 통과")
    print("=" * 62)


if __name__ == "__main__":
    main()
