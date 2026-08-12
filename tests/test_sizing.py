"""
포지션 크기 설계 테스트
────────────────────────
    python tests/test_sizing.py

시드·물타기 깊이·손절선은 서로 맞물려 있다. 하나를 바꾸면 나머지가
조용히 어긋난다. 그 관계를 숫자로 고정해 둔다.

  · 시드      = 자본의 약 10%
  · 총투입    = 시드 × 8 (3단계) ≈ 자본의 80% — 마진 여유 20% 확보
  · 1회 손절  = 자본의 22% 이하
  · 손절 거리 = 총투입 기준 코인 2~6% (8배 레버리지)

마지막 항목이 중요하다. 절대금액 상한을 자본과 무관하게 두면 계좌가
커질수록 손절선이 코인 1% 미만으로 조여져 정상적인 흔들림에도 잘린다.
실제로 MAX_NET_LOSS_CEILING=$250 이 그런 상태였다.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import trade_journal                                    # noqa: E402
import paper_trader                                     # noqa: E402
import config                                           # noqa: E402

_TMP = tempfile.mkdtemp(prefix="sizing_")
trade_journal.JOURNAL_FILE = os.path.join(_TMP, "trades.jsonl")
paper_trader.STATE_FILE    = os.path.join(_TMP, "state.json")
config.LIVE_TRADING = False

CAPITALS = [500, 1020, 1200, 1500, 2000, 3000, 5000]


def trader(capital: float):
    pt = paper_trader.PaperTrader(live_api=None)
    pt.total_capital, pt.total_pnl = capital, 0.0
    pt.position = None
    return pt


def test_seed_is_a_stable_share_of_capital():
    print("\n[1] 시드가 자본의 일정 비율로 따라 오르는가")
    print(f"    {'자본':>8} {'시드':>8} {'비율':>7}")
    for cap in CAPITALS:
        seed = trader(cap)._get_initial_position_usd()
        ratio = seed / cap
        print(f"    ${cap:>7,} ${seed:>7.0f} {ratio:>7.1%}")
        assert 0.085 <= ratio <= 0.105, \
            f"자본 ${cap} 에서 시드 비율이 {ratio:.1%} — 계단과 상한이 어긋났다"
    print("    ✅ 어느 구간에서도 10% 안팎")


def test_full_ladder_fits_inside_capital():
    """총투입이 자본을 넘으면 거래소가 주문을 거절한다"""
    print("\n[2] 물타기 사다리가 자본 안에 들어오는가")
    mult = 2 ** config.MAX_DCA_STAGES      # 3단계 → 시드 × 8
    print(f"    최대 단계 {config.MAX_DCA_STAGES} → 총투입 = 시드 × {mult}")
    for cap in CAPITALS:
        seed  = trader(cap)._get_initial_position_usd()
        total = seed * mult
        print(f"    ${cap:>7,} → 총투입 ${total:>8,.0f} ({total/cap:>4.0%})")
        assert total <= cap * 0.90, \
            f"총투입이 자본의 {total/cap:.0%} — 마진 여유가 없다"
    print("    ✅ 80% 안팎, 마진 여유 확보")


def test_max_loss_is_capped_by_capital_ratio():
    print("\n[3] 1회 손절 한도가 자본 비율로 묶여 있는가")
    ratio = config.MAX_NET_LOSS_CAPITAL_RATIO
    for cap in CAPITALS:
        loss = -trader(cap)._get_max_loss_usd()
        print(f"    ${cap:>7,} → 손절 ${loss:>8,.0f} ({loss/cap:>5.1%})")
        assert loss <= cap * ratio + 0.01, \
            f"자본 ${cap} 에서 손절 {loss/cap:.1%} > 상한 {ratio:.0%}"
    print(f"    ✅ 전 구간 자본의 {ratio:.0%} 이하")


def test_stop_distance_stays_tradeable_as_capital_grows():
    """★ 절대금액 상한이 자본과 무관하면 큰 계좌에서 손절선이 질식한다"""
    print("\n[4] ★ 자본이 커져도 손절 거리가 정상 범위인가")
    print(f"    {'자본':>8} {'총투입':>10} {'손절':>9} {'코인 기준':>10}")
    for cap in CAPITALS:
        pt    = trader(cap)
        seed  = pt._get_initial_position_usd()
        total = seed * (2 ** config.MAX_DCA_STAGES)
        loss  = -pt._get_max_loss_usd()
        coin  = loss / (total * config.LEVERAGE)
        print(f"    ${cap:>7,} ${total:>9,.0f} ${loss:>8,.0f} {coin:>9.2%}")
        assert 0.02 <= coin <= 0.06, (
            f"자본 ${cap} 에서 손절이 코인 {coin:.2%} — "
            "너무 좁으면 흔들림에 잘리고, 너무 넓으면 손절이 아니다")
    print("    ✅ 전 구간 코인 2~6%")


def test_conviction_range_keeps_ladder_sane():
    """확신도로 시드를 키워도 하드캡(=자본)이 사다리를 잘라준다"""
    print("\n[5] 확신도 최대 배율에서도 자본을 넘지 않는가")
    cap = 1200.0
    pt  = trader(cap)
    seed = pt._get_initial_position_usd() * config.CONVICTION_MAX_MULT
    pt.open_position("XUSDT", "UP", 100.0, invest_override=seed)
    p = pt.position
    print(f"    자본 ${cap:,.0f} / 확신 최대 시드 ${seed:.0f} "
          f"/ 하드캡 ${p.max_position:,.0f}")
    assert p.max_position <= cap, "하드캡이 자본을 넘는다"
    assert p.max_position >= seed, "하드캡이 진입금보다 작다"
    print("    ✅ 사다리는 자본에서 잘린다 (의도된 동작 — 시드가 크면 얕게)")


def test_small_capital_still_trades():
    """손실로 자본이 줄어도 최소 시드는 남아야 한다 — 죽으면 회복 못 한다"""
    print("\n[6] 자본이 크게 줄어도 진입은 가능한가")
    for cap in (300, 150, 60):
        seed = trader(cap)._get_initial_position_usd()
        print(f"    ${cap:>5,} → 시드 ${seed:>6.2f}")
        assert seed >= config.DYNAMIC_SEED_MIN_USD
    print("    ✅ 최소 시드 보장")


def main():
    print("=" * 62)
    print("  포지션 크기 설계 테스트")
    print("=" * 62)
    for fn in [test_seed_is_a_stable_share_of_capital,
               test_full_ladder_fits_inside_capital,
               test_max_loss_is_capped_by_capital_ratio,
               test_stop_distance_stays_tradeable_as_capital_grows,
               test_conviction_range_keeps_ladder_sane,
               test_small_capital_still_trades]:
        fn()
    print("\n" + "=" * 62)
    print("  전부 통과")
    print("=" * 62)


if __name__ == "__main__":
    main()
