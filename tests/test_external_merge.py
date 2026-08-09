"""
사람이 손으로 끼어들었을 때 (외부 개입) 테스트
──────────────────────────────────────────────
    python tests/test_external_merge.py

실제 상황:
  봇이 PUMP-USDT 를 $40 로 잡고 있는데, 사람이 같은 계좌에서 손으로
  $120 을 더 샀다. 거래소 실물은 $160, 봇 장부는 $40.

그대로 두면 세 가지가 동시에 망가진다.
  1. 봇은 자기 수량으로 손익을 재므로 손절선이 4배 늦게 발동한다
     (장부 -$110 = 실제 -$440. 자본 $500 에서 치명적)
  2. 거래소 백스톱이 봇 수량으로 걸려 있어 1/4 만 닫힌다
  3. 물타기 사다리가 이미 무의미해졌는데 계속 태운다
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import trade_journal                                    # noqa: E402
import risk_governor                                    # noqa: E402
import paper_trader                                     # noqa: E402
import config                                           # noqa: E402

_TMP = tempfile.mkdtemp(prefix="extmerge_")
trade_journal.JOURNAL_FILE = os.path.join(_TMP, "trades.jsonl")
risk_governor.STATE_FILE   = os.path.join(_TMP, "gov.json")


class FakeAPI:
    """포지션 수량을 마음대로 바꿀 수 있는 가짜 거래소"""

    def __init__(self):
        self.qty = 0.0
        self.avg = 0.0
        self.side = "LONG"
        self.stop_orders = []          # (수량, 가격)
        self.cancelled = []

    # 포지션
    def get_positions(self, symbol=None):
        if self.qty == 0:
            return []
        return [{"symbol": symbol or "PUMP-USDT", "positionSide": self.side,
                 "positionAmt": str(self.qty), "avgPrice": str(self.avg)}]

    # 주문
    def place_stop_market(self, symbol, position_side, stop_price, quantity):
        self.stop_orders.append((quantity, stop_price))
        return {"code": 0, "data": {"order": {"orderId": len(self.stop_orders)}}}

    def cancel_order(self, symbol, order_id):
        self.cancelled.append(order_id)
        return {"code": 0}

    def cancel_all_open_orders(self, *a, **k):
        return {"code": 0}

    def get_open_orders(self, symbol=None):
        return []

    # 기타
    def get_equity(self):                  return 500.0
    def get_balance(self):                 return 500.0
    def get_price(self, s):                return self.avg or 0.002711
    def get_price_precision(self, s):      return 8
    def get_contracts(self):               return []
    def set_leverage(self, *a, **k):       return {"code": 0}
    def place_order(self, *a, **k):        return {"code": 0}
    def close_position(self, *a, **k):     return {"code": 0}


def new_trader(tag, api):
    paper_trader.STATE_FILE = os.path.join(_TMP, f"state_{tag}.json")
    pt = paper_trader.PaperTrader(live_api=api)
    pt.total_capital, pt.total_pnl = 500.0, 0.0
    return pt


def setup(tag):
    """봇이 $40 로 진입한 상태를 만든다"""
    api = FakeAPI()
    pt  = new_trader(tag, api)
    prev = config.LIVE_TRADING
    config.LIVE_TRADING = True
    pt.open_position("PUMP-USDT", "UP", 0.002711, invest_override=40.0)
    p = pt.position
    api.qty, api.avg = p.total_qty, p.avg_price      # 거래소도 같은 상태
    return api, pt, prev


def test_detects_manual_add():
    print("\n[1] ★ 사람이 손으로 추가 매수한 것을 잡아내는가")
    api, pt, prev = setup("detect")
    try:
        p = pt.position
        bot_qty = p.total_qty
        print(f"    봇 장부: 수량 {bot_qty:,.0f} / 투입 ${p.total_invested:.0f}")

        # 사람이 손으로 $120 추가 (더 싼 가격에)
        api.qty = bot_qty * 4
        api.avg = p.avg_price * 0.98
        print(f"    거래소 실물: 수량 {api.qty:,.0f} (4배) / 평단 {api.avg:.8f}")

        pt.reconcile_with_exchange()
        p = pt.position
        print(f"    → 봇 장부 갱신: 수량 {p.total_qty:,.0f} / "
              f"투입 ${p.total_invested:.0f} / 평단 {p.avg_price:.8f}")
        assert p.total_qty == api.qty, "수량을 거래소에 맞추지 않았다"
        assert abs(p.avg_price - api.avg) < 1e-12, "평단을 맞추지 않았다"
        assert 150 < p.total_invested < 160, p.total_invested
        assert p.external_merge is True
        print("    ✅ 거래소를 진실로 삼는다")
    finally:
        config.LIVE_TRADING = prev


def test_loss_is_now_measured_correctly():
    """★ 이게 핵심 — 손절선이 4배 늦게 발동하던 문제"""
    print("\n[2] ★ 손익이 실제 크기로 계산되는가")
    api, pt, prev = setup("loss")
    try:
        p = pt.position
        drop = p.avg_price * 0.97          # 코인 -3%
        before = p.realized_pnl(drop)

        api.qty = p.total_qty * 4
        api.avg = p.avg_price
        pt.reconcile_with_exchange()
        after = pt.position.realized_pnl(drop)

        print(f"    코인 -3% 일 때 손익: 갱신 전 ${before:+.2f} → 갱신 후 ${after:+.2f}")
        assert after < before * 3.5, "손익이 실제 크기를 반영하지 않는다"
        print(f"    봇 손절 한도 ${-pt._get_max_loss_usd():.2f}")
        print("    ✅ 4배 커진 포지션을 4배로 잰다 "
              "(예전이라면 -$110 손절이 실제 -$440 에서 발동)")
    finally:
        config.LIVE_TRADING = prev


def test_backstop_is_replaced_with_new_size():
    print("\n[3] ★ 거래소 백스톱을 새 수량으로 다시 거는가")
    api, pt, prev = setup("stop")
    try:
        p = pt.position
        assert api.stop_orders, "진입 시 백스톱이 안 걸렸다"
        first_qty = api.stop_orders[-1][0]
        print(f"    진입 시 백스톱 수량 {first_qty:,.0f}")

        api.qty = p.total_qty * 4
        api.avg = p.avg_price
        pt.reconcile_with_exchange()

        last_qty = api.stop_orders[-1][0]
        print(f"    갱신 후 백스톱 수량 {last_qty:,.0f} "
              f"(취소 {len(api.cancelled)}건)")
        assert last_qty > first_qty * 3, \
            "옛 수량 그대로면 체결돼도 1/4 만 닫힌다"
        print("    ✅ 전량을 덮는 백스톱으로 교체")
    finally:
        config.LIVE_TRADING = prev


def test_dca_stops_after_intervention():
    print("\n[4] ★ 사람이 개입한 포지션에 물타기를 더 하지 않는가")
    api, pt, prev = setup("dca")
    try:
        p = pt.position
        p.step_ref_pnl = 0.0
        deep = p.avg_price * 0.90          # 물타기 트리거를 훌쩍 넘는 하락
        print(f"    개입 전 물타기 판단: {pt.should_avg_down(deep)}")
        assert pt.should_avg_down(deep) is True, "재현 실패 — 원래 물타기 조건이어야"

        api.qty = p.total_qty * 4
        api.avg = p.avg_price
        pt.reconcile_with_exchange()
        after = pt.should_avg_down(deep)
        print(f"    개입 후 물타기 판단: {after}")
        assert after is False, "사람이 비중을 바꿨는데 계획대로 더 태운다"
        print("    ✅ 물타기 중단 (손절·익절은 계속 관리)")
    finally:
        config.LIVE_TRADING = prev


def test_small_difference_is_ignored():
    print("\n[5] 반올림·부분체결 수준의 차이는 개입으로 보지 않는다")
    api, pt, prev = setup("small")
    try:
        p = pt.position
        api.qty = p.total_qty * 1.02       # 2% 차이
        api.avg = p.avg_price
        pt.reconcile_with_exchange()
        print(f"    2% 차이 → external_merge = {pt.position.external_merge}")
        assert pt.position.external_merge is False
        print("    ✅ 무시")
    finally:
        config.LIVE_TRADING = prev


def test_survives_restart():
    print("\n[6] 재시작해도 '개입됨' 표시가 남는가")
    api, pt, prev = setup("persist")
    try:
        p = pt.position
        api.qty = p.total_qty * 4
        api.avg = p.avg_price
        pt.reconcile_with_exchange()
        pt.save_state()

        pt2 = paper_trader.PaperTrader(live_api=api)
        print(f"    재시작 후 external_merge = {pt2.position.external_merge}")
        assert pt2.position.external_merge is True, \
            "재시작하면 물타기가 다시 켜진다"
        print("    ✅ 유지됨")
    finally:
        config.LIVE_TRADING = prev


def test_position_gone_still_closes_book():
    """기존 동작(백스톱 체결 감지)이 깨지지 않았는지"""
    print("\n[7] 거래소에 포지션이 없어지면 장부를 닫는다 (기존 동작)")
    api, pt, prev = setup("gone")
    try:
        api.qty = 0.0
        handled = pt.reconcile_with_exchange()
        print(f"    처리됨 {handled} / 포지션 {pt.position}")
        assert handled is True and pt.position is None
        print("    ✅")
    finally:
        config.LIVE_TRADING = prev


def main():
    print("=" * 62)
    print("  외부(사람) 개입 감지 테스트")
    print("=" * 62)
    for fn in [test_detects_manual_add,
               test_loss_is_now_measured_correctly,
               test_backstop_is_replaced_with_new_size,
               test_dca_stops_after_intervention,
               test_small_difference_is_ignored,
               test_survives_restart,
               test_position_gone_still_closes_book]:
        fn()
    print("\n" + "=" * 62)
    print("  전부 통과")
    print("=" * 62)


if __name__ == "__main__":
    main()
