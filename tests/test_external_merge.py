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

    # 체결 조회 — 실제 체결가를 돌려준다
    fill_price = 0.0        # 0 이면 조회 실패를 흉내낸다
    def get_order(self, symbol, order_id):
        if self.fill_price <= 0:
            raise RuntimeError("조회 실패 흉내")
        return {"orderId": order_id, "avgPrice": str(self.fill_price)}

    # 기타
    def get_equity(self):                  return 500.0
    def get_balance(self):                 return 500.0
    def get_price(self, s):                return self.avg or 0.002711
    def get_price_precision(self, s):      return 8
    def get_contracts(self):               return []
    def set_leverage(self, *a, **k):       return {"code": 0}
    def place_order(self, *a, **k):        return {"code": 0}
    def close_position(self, *a, **k):
        return {"code": 0, "data": {"order": {"orderId": "CLOSE-1"}}}


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


def test_offline_backstop_fill_is_queried():
    """★ 봇이 꺼져 있는 사이(폰 배터리 방전 등) 백스톱이 체결된 경우

    다시 켜면 거래소에는 포지션이 없다. 그때 '걸어둔 손절 가격'으로
    추정해 장부를 닫으면 오차가 그대로 남는다. 백스톱 주문 ID 를 들고
    있으므로 **실제 체결가**를 물어볼 수 있다.
    """
    print("\n[7b] ★ 봇이 없는 사이 백스톱이 체결됐을 때 실제 체결가 조회")
    api, pt, prev = setup("offline")
    try:
        p = pt.position
        p.stop_order_id = "STOP-9"
        p.stop_price    = p.avg_price * 0.92     # 걸어둔 손절 가격
        api.fill_price  = p.avg_price * 0.90     # 실제로는 더 아래에서 체결
        api.qty = 0.0                            # 봇이 꺼진 사이 청산됨

        pt.reconcile_with_exchange()
        rec = pt.closed_trades[-1]
        print(f"    걸어둔 손절 {p.stop_price:.8f} / "
              f"실제 체결 {api.fill_price:.8f}")
        print(f"    기록 {rec['exit_price']:.8f} | 사유 {rec['reason']}")
        assert abs(rec["exit_price"] - api.fill_price) < 1e-6, \
            "걸어둔 가격으로 기록했다 — 슬리피지만큼 장부가 낙관적이 된다"
        assert "체결가확인" in rec["reason"]
        print("    ✅ 실제 체결가로 기록")
    finally:
        config.LIVE_TRADING = prev


def test_offline_backstop_falls_back_when_query_fails():
    print("\n[7c] 백스톱 체결가 조회가 안 되면 걸어둔 가격으로 닫는다")
    api, pt, prev = setup("offline2")
    try:
        p = pt.position
        p.stop_order_id = "STOP-9"
        p.stop_price    = p.avg_price * 0.92
        api.fill_price  = 0.0                    # 조회 실패
        api.qty = 0.0

        pt.reconcile_with_exchange()
        rec = pt.closed_trades[-1]
        print(f"    기록 {rec['exit_price']:.8f} | 사유 {rec['reason']}")
        assert "추정" in rec["reason"]
        assert pt.position is None, "장부를 못 닫으면 신규 진입이 영영 막힌다"
        print("    ✅ 추정으로 닫되 장부는 반드시 닫는다")
    finally:
        config.LIVE_TRADING = prev


def test_trail_is_reset_when_average_changes():
    """★ 실제 사고 재현 — 평단이 바뀌자 트레일이 즉시 발동해 전량 청산됐다

    사람이 추가 매수 → 평단 하락 → pnl_pct 급등 → 옛 고점 기준 트레일이
    곧바로 활성화·발동 → 2초 만에 '트레일익절' 로 시장가 전량 청산.
    실제로는 -$56 손실이었다.
    """
    print("\n[8] ★ 평단이 바뀌면 트레일·고점을 초기화하는가")
    api, pt, prev = setup("trail")
    try:
        p = pt.position
        # 트레일이 활성화돼 고점을 들고 있는 상태를 만든다
        p.trail_active  = True
        p.peak_price    = p.avg_price * 1.05
        p.trail_sl      = p.avg_price * 1.03
        p.peak_realized = 8.0
        print(f"    개입 전: 트레일 활성 / 고점 {p.peak_price:.8f} / "
              f"SL {p.trail_sl:.8f}")

        # 사람이 더 싸게 추가 매수 → 평단 하락
        api.qty = p.total_qty * 4
        api.avg = p.avg_price * 0.99
        pt.reconcile_with_exchange()

        p = pt.position
        print(f"    개입 후: 트레일 활성 {p.trail_active} / "
              f"고점 {p.peak_price:.8f} / SL {p.trail_sl:.8f} / "
              f"고점순익 ${p.peak_realized:.2f}")
        assert p.trail_active is False, "옛 트레일이 살아 있으면 즉시 오발동한다"
        assert p.trail_sl == 0.0
        assert abs(p.peak_price - api.avg) < 1e-12, "고점을 새 평단에서 다시 잡아야"
        assert p.peak_realized == 0.0, "옛 고점 순수익이 남으면 수익보존락이 오발동"
        assert p.is_trail_hit(api.avg) is False
        print("    ✅ 새 평단에는 새 고점 — 오발동 차단")
    finally:
        config.LIVE_TRADING = prev


def test_uses_actual_fill_price():
    """★ 장부를 추정값이 아니라 거래소 실제 체결가로 쓴다"""
    print("\n[9] ★ 청산 손익을 실제 체결가로 계산하는가")
    api, pt, prev = setup("fill")
    try:
        p = pt.position
        entry = p.avg_price
        api.fill_price = entry * 0.98          # 실제로는 2% 낮게 체결됐다
        # 봇이 보는 시세는 아직 진입가 근처 → 추정으로는 이익처럼 보인다
        trade = pt.close_position(entry, "테스트청산")
        print(f"    봇에 전달된 시세 {entry:.8f} / 실제 체결 {api.fill_price:.8f}")
        print(f"    기록된 체결가 {trade['exit_price']:.8f} / "
              f"순손익 ${trade['pnl']:+.2f}")
        assert abs(trade["exit_price"] - api.fill_price) < 1e-6, \
            "추정값으로 기록했다 — 장부가 거짓이 된다"
        assert trade["pnl"] < 0, "실제로는 손실인데 이익으로 기록됐다"
        print("    ✅ 거래소 실제 체결가로 기록")
    finally:
        config.LIVE_TRADING = prev


def test_falls_back_to_estimate_when_query_fails():
    """조회가 안 되면 추정값으로라도 장부를 닫아야 한다 (멈추면 안 됨)"""
    print("\n[10] 체결가 조회 실패 시 추정값으로 진행")
    api, pt, prev = setup("fillfail")
    try:
        p = pt.position
        entry = p.avg_price
        api.fill_price = 0.0                    # 조회 실패
        trade = pt.close_position(entry, "테스트청산")
        expected = entry * (1 - config.SLIPPAGE_RATE)
        print(f"    기록된 체결가 {trade['exit_price']:.8f} "
              f"(추정 {expected:.8f})")
        assert abs(trade["exit_price"] - expected) < 1e-9
        assert pt.position is None, "장부가 안 닫히면 신규 진입이 영영 막힌다"
        print("    ✅ 추정값으로 닫되 경고를 남긴다")
    finally:
        config.LIVE_TRADING = prev


def main():
    print("=" * 62)
    print("  외부(사람) 개입 · 체결가 정확도 테스트")
    print("=" * 62)
    for fn in [test_detects_manual_add,
               test_loss_is_now_measured_correctly,
               test_backstop_is_replaced_with_new_size,
               test_dca_stops_after_intervention,
               test_small_difference_is_ignored,
               test_survives_restart,
               test_position_gone_still_closes_book,
               test_offline_backstop_fill_is_queried,
               test_offline_backstop_falls_back_when_query_fails,
               test_trail_is_reset_when_average_changes,
               test_uses_actual_fill_price,
               test_falls_back_to_estimate_when_query_fails]:
        fn()
    print("\n" + "=" * 62)
    print("  전부 통과")
    print("=" * 62)


if __name__ == "__main__":
    main()
