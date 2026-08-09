"""
거래소 강제 손절(백스톱) 테스트
────────────────────────────────
    python tests/test_exchange_stop.py

가짜 거래소로 주문 흐름과 STOP 가격 계산을 검증한다.
실주문은 일절 나가지 않는다.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import trade_journal                                   # noqa: E402
import paper_trader                                    # noqa: E402
import risk_governor                                   # noqa: E402
import config                                          # noqa: E402

_TMP = tempfile.mkdtemp(prefix="stoptest_")
trade_journal.JOURNAL_FILE = os.path.join(_TMP, "trades.jsonl")
paper_trader.STATE_FILE    = os.path.join(_TMP, "state.json")
risk_governor.STATE_FILE   = os.path.join(_TMP, "gov.json")

config.LIVE_TRADING = True            # 백스톱 경로를 타도록
config.EXCHANGE_STOP_ENABLED = True


class FakeAPI:
    """주문을 기록만 하는 가짜 거래소"""

    def __init__(self):
        self.stops, self.cancels, self.cancel_all = [], [], []
        self._next_id = 1000

    def get_contracts(self):
        return [{"symbol": "XUSDT", "tradeMinQuantity": 0.1, "pricePrecision": 4}]

    def get_price_precision(self, symbol):  return 4
    def get_price(self, symbol):            return 100.0
    def get_positions(self, symbol=None):   return []
    def get_balance(self):                  return 5000.0
    def set_leverage(self, *a, **k):        return {"code": 0}
    def place_order(self, *a, **k):         return {"code": 0}
    def close_position(self, *a, **k):      return {"code": 0}

    def place_stop_market(self, symbol, pos_side, stop_price, qty):
        self._next_id += 1
        self.stops.append({"symbol": symbol, "side": pos_side,
                           "stop": stop_price, "qty": qty, "id": self._next_id})
        return {"code": 0, "data": {"order": {"orderId": self._next_id}}}

    def cancel_order(self, symbol, order_id):
        self.cancels.append(order_id)
        return {"code": 0}

    def cancel_all_open_orders(self, symbol):
        self.cancel_all.append(symbol)
        return {"code": 0}


def new_trader(tag: str, api=None):
    """테스트마다 독립된 state.json — 앞 테스트의 포지션이 복원되면
    생성 시점에 STOP 이 한 번 더 걸려 주문 개수가 어긋난다."""
    paper_trader.STATE_FILE = os.path.join(_TMP, f"state_{tag}.json")
    api = api or FakeAPI()
    return paper_trader.PaperTrader(live_api=api), api


def expected_loss(trader, pos):
    """STOP 은 금액 기준과 변동률 상한 중 **가까운 쪽**에 놓여야 한다"""
    notional = pos.total_invested * config.LEVERAGE
    x = min(trader._stop_loss_usd() / notional, config.EXCHANGE_STOP_MAX_COIN_PCT)
    return -x * notional


def test_placed_on_entry():
    print("\n[1] 진입 즉시 배치")
    pt, api = new_trader("entry")
    pt.open_position("XUSDT", "UP", 100.0)
    p, s = pt.position, api.stops[-1]

    print(f"    평단 {p.avg_price:.4f} → STOP {s['stop']:.4f} "
          f"(코인 {(s['stop']/p.avg_price-1)*100:+.2f}%)")
    print(f"    체결 시 손실 ${p.gross_pnl(s['stop']):+.2f} "
          f"(예상 ${expected_loss(pt, p):+.2f})")
    assert s["stop"] < p.avg_price, "LONG 인데 STOP 이 평단 위"
    assert p.stop_order_id, "주문 ID 미저장"
    assert abs(p.gross_pnl(s["stop"]) - expected_loss(pt, p)) < 1.0
    print("    ✅ 0단계는 금액기준(-75%)이 도달 불가라 변동률 상한 8% 적용")


def test_replaced_on_dca():
    print("\n[2] DCA 시 재배치 (기존 취소 → 신규)")
    pt, api = new_trader("dca")
    pt.open_position("XUSDT", "UP", 100.0)
    old_id = pt.position.stop_order_id
    pt.execute_avg_down(99.0)
    p, s = pt.position, api.stops[-1]

    print(f"    투입 ${p.total_invested:.0f} / 평단 {p.avg_price:.4f} "
          f"→ STOP {s['stop']:.4f}")
    assert len(api.stops) == 2,   "STOP 재배치 안 됨"
    assert len(api.cancels) == 1, f"기존 STOP 미취소: {api.cancels}"
    assert str(api.cancels[0]) == old_id
    assert abs(p.gross_pnl(s["stop"]) - expected_loss(pt, p)) < 1.5
    print("    ✅ 평단·수량이 바뀌면 STOP 도 따라간다")


def test_short_direction():
    print("\n[3] SHORT 방향")
    pt, api = new_trader("short")
    pt.open_position("XUSDT", "DOWN", 100.0)
    p, s = pt.position, api.stops[-1]
    print(f"    평단 {p.avg_price:.4f} → STOP {s['stop']:.4f} "
          f"(코인 {(s['stop']/p.avg_price-1)*100:+.2f}%)")
    assert s["stop"] > p.avg_price, "SHORT 인데 STOP 이 평단 아래"
    assert abs(p.gross_pnl(s["stop"]) - expected_loss(pt, p)) < 1.0
    print("    ✅")


def test_cleanup_on_close():
    print("\n[4] 청산 후 고아 주문 정리")
    pt, api = new_trader("cleanup")
    pt.open_position("XUSDT", "UP", 100.0)
    pt.close_position(101.0, "테스트청산")
    print(f"    cancel_all_open_orders 호출 {len(api.cancel_all)}회")
    assert api.cancel_all, "고아 STOP 이 남으면 재진입 시 오폭한다"
    print("    ✅")


def test_failure_does_not_block_trading():
    print("\n[5] 거래소 오류가 매매를 막지 않는가")

    class BrokenAPI(FakeAPI):
        def place_stop_market(self, *a, **k):
            raise RuntimeError("API 다운")

    pt, _ = new_trader("broken", BrokenAPI())
    pos = pt.open_position("XUSDT", "UP", 100.0)
    print(f"    STOP 실패 → 진입 성공 여부: {pos is not None}")
    assert pos is not None, "백스톱 실패가 진입을 막았다"
    assert pos.stop_order_id == "", "실패했는데 주문 ID 가 남음"
    print("    ✅ 백스톱이 없을 뿐 봇 자체 손절은 살아 있다")


def test_capital_cap():
    print("\n[6] 자본 급감 시 상한")
    pt, _ = new_trader("capcap")
    pt.total_pnl = -900.0          # 확정 자본 $150
    equity = pt.total_capital + pt.total_pnl
    loss   = pt._stop_loss_usd()
    print(f"    자본 ${equity:.0f} → 백스톱 손실 ${loss:.2f} "
          f"({loss/equity:.0%})")
    assert loss <= equity * config.EXCHANGE_STOP_MAX_CAPITAL_RATIO + 0.01
    print("    ✅ 확정 자본의 35% 를 넘지 않는다")


def test_per_stage_placement():
    print("\n[7] 단계별 배치 위치")
    pt, _ = new_trader("stages")
    pt.open_position("XUSDT", "UP", 100.0)
    p  = pt.position
    cap = pt.total_capital + pt.total_pnl
    print(f"    {'단계':<6}{'투입':>8}{'명목':>10}{'STOP':>9}{'손실':>10}{'자본대비':>10}")
    for stage in range(5):
        if stage:
            p.apply_avg_down(100.0, p.next_avg_down_amount())
        x    = pt._stop_distance(p)
        loss = p.gross_pnl(pt._calc_stop_price(p))
        print(f"    {stage}단계{'':<2}{p.total_invested:>8.0f}"
              f"{p.total_invested*config.LEVERAGE:>10.0f}"
              f"{-x*100:>8.1f}%{loss:>+10.0f}{loss/cap*100:>9.1f}%")
        assert loss < 0
        assert abs(loss) <= cap * config.EXCHANGE_STOP_MAX_CAPITAL_RATIO + 1
    print("    ✅ 전 단계에서 자본의 35% 이내 (4단계 청산 예상 -12% 보다 앞섬)")


def test_reconcile_ghost_position():
    print("\n[8] 백스톱 체결 후 유령 포지션 정리")

    class GhostAPI(FakeAPI):
        """포지션을 열어주지만 조회하면 '없음' — STOP 체결 직후 상태"""
        def get_positions(self, symbol=None):
            return []

    pt, _ = new_trader("ghost", GhostAPI())
    pt.open_position("XUSDT", "UP", 100.0)
    pt.position.stop_price = 92.0
    handled = pt.reconcile_with_exchange()
    last = pt.closed_trades[-1]
    print(f"    불일치 처리 = {handled} / 사유 '{last['reason']}' "
          f"체결가 {last['exit_price']}")
    assert handled and pt.position is None, "유령 포지션이 정리되지 않음"
    assert "백스톱" in last["reason"]

    print("    ── 조회 실패를 '포지션 없음' 으로 오판하지 않는가 ──")

    class FlakyAPI(FakeAPI):
        def get_positions(self, symbol=None):
            raise RuntimeError("API 타임아웃")

    pt2, _ = new_trader("flaky", FlakyAPI())
    pt2.open_position("XUSDT", "UP", 100.0)
    assert not pt2.reconcile_with_exchange(), "조회 실패인데 처리했다"
    assert pt2.position is not None, "일시적 오류로 멀쩡한 포지션을 지웠다"
    print("    ✅ 조회 실패 시에는 아무것도 하지 않는다")


def main():
    print("=" * 62)
    print("  거래소 강제 손절(백스톱) 테스트")
    print("=" * 62)
    for fn in [test_placed_on_entry, test_replaced_on_dca, test_short_direction,
               test_cleanup_on_close, test_failure_does_not_block_trading,
               test_capital_cap, test_per_stage_placement,
               test_reconcile_ghost_position]:
        fn()
    print("\n" + "=" * 62)
    print("  ✅ 전부 통과")
    print("=" * 62)


if __name__ == "__main__":
    main()
