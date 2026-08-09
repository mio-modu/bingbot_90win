"""
엔진 기동 스모크 테스트
───────────────────────
    python tests/test_engine_smoke.py

네트워크 없이 StrategyEngine 이 생성되고 tick 이 도는지 확인한다.
전략 로직을 검증하지는 않는다 — 배선(거버너·저널·백스톱 연결)이
끊기지 않았는지만 본다.
"""

import io
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import trade_journal                                   # noqa: E402
import risk_governor                                   # noqa: E402
import paper_trader                                    # noqa: E402
import config                                          # noqa: E402

_TMP = tempfile.mkdtemp(prefix="smoketest_")
trade_journal.JOURNAL_FILE = os.path.join(_TMP, "trades.jsonl")
risk_governor.STATE_FILE   = os.path.join(_TMP, "gov.json")
paper_trader.STATE_FILE    = os.path.join(_TMP, "state.json")
config.LIVE_TRADING = False


class StubAPI:
    """네트워크를 타지 않는 최소 API"""
    def get_price(self, symbol):            return 100.0
    def get_positions(self, symbol=None):   return []
    def get_ticker(self, symbol):           return {"lastPrice": "100"}
    def get_all_tickers(self):              return []
    def get_contracts(self):                return []
    def get_balance(self):                  return 1050.0
    def get_price_precision(self, symbol):  return 4
    def set_leverage(self, *a, **k):        return {"code": 0}
    def place_order(self, *a, **k):         return {"code": 0}
    def close_position(self, *a, **k):      return {"code": 0}
    def cancel_order(self, *a, **k):        return {"code": 0}
    def cancel_all_open_orders(self, *a, **k): return {"code": 0}
    def place_stop_market(self, *a, **k):
        return {"code": 0, "data": {"order": {"orderId": 1}}}

    def get_klines(self, symbol, interval, limit=100):
        return [[0, 100, 101, 99, 100 + (n % 3), 1000] for n in range(limit)]


class StubScanner:
    def __init__(self, api):                  pass
    def scan(self, *a, **k):                  return []
    def scan_crash_shorts(self, *a, **k):     return []


import strategy_engine                                 # noqa: E402
strategy_engine.BingXAPI    = StubAPI
strategy_engine.CoinScanner = StubScanner
strategy_engine.ENGINE_STATE_FILE = os.path.join(_TMP, "engine.json")


def test_boots():
    print("\n[1] 엔진 생성 및 배선")
    eng = strategy_engine.StrategyEngine()
    print(f"    상태   : {eng.state.value}")
    print(f"    거버너 : {eng.governor.status_line(eng.pt.total_capital + eng.pt.total_pnl)}")
    assert eng.governor is not None,          "거버너 미생성"
    assert eng.pt.governor is eng.governor,   "PaperTrader 에 거버너 미연결"
    print("    ✅")
    return eng


def test_ticks(eng):
    print("\n[2] tick 5회")
    for _ in range(5):
        eng.tick()
    print("    ✅ 예외 없이 완주")


def test_status_line(eng):
    print("\n[3] print_status 에 거버너 상태가 나오는가")
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    root = logging.getLogger()
    prev_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        eng.print_status()
    finally:
        root.removeHandler(handler)
        root.setLevel(prev_level)

    lines = [l.strip() for l in buf.getvalue().splitlines() if "거버너" in l]
    print(f"    {lines[0] if lines else '!! 거버너 줄 없음'}")
    assert lines, "print_status 에 거버너 상태가 없다"
    print("    ✅")


def test_governor_blocks_entry(eng):
    print("\n[4] 거버너 정지 상태에서 진입이 막히는가")
    eng.governor.peak_equity    = 5000.0     # 인위적 대낙폭
    eng.governor.recovery_mode  = False
    ok, why = eng.governor.can_enter(eng.pt.total_capital + eng.pt.total_pnl)
    print(f"    can_enter = {ok}  ({why})")
    assert not ok
    eng._enter({"symbol": "XUSDT", "trend": "UP", "score": 1.0})
    assert eng.pt.position is None, "거버너가 막았는데 진입됐다"
    print("    ✅ 진입 보류됨")


def main():
    print("=" * 62)
    print("  엔진 기동 스모크 테스트")
    print("=" * 62)
    eng = test_boots()
    test_ticks(eng)
    test_status_line(eng)
    test_governor_blocks_entry(eng)
    print("\n" + "=" * 62)
    print("  ✅ 전부 통과")
    print("=" * 62)


if __name__ == "__main__":
    main()
