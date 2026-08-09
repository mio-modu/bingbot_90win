"""
코인 선정 · 장부 대조 테스트
────────────────────────────
    python tests/test_coin_scanner.py

두 가지 실제 버그를 검증한다.
  · ADX 필터가 config 에 정의돼 있는데 코드에 없었다 (import 만 하고 미사용)
  · 체결가가 추정값이라 봇 장부와 거래소 실제 잔고가 어긋난다
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import trade_journal                                   # noqa: E402
import risk_governor                                   # noqa: E402
import paper_trader                                    # noqa: E402
import config                                          # noqa: E402
import coin_scanner                                    # noqa: E402
import recency                                         # noqa: E402
from coin_scanner import CoinScanner, calc_adx         # noqa: E402

_TMP = tempfile.mkdtemp(prefix="scantest_")
trade_journal.JOURNAL_FILE = os.path.join(_TMP, "trades.jsonl")
risk_governor.STATE_FILE   = os.path.join(_TMP, "gov.json")


def trending_klines(n=40, start=100.0, step=1.2, rng=1.0):
    """꾸준히 오르는 캔들 → ADX 높음"""
    out, price = [], start
    for _ in range(n):
        o = price
        c = price + step
        out.append([0, o, max(o, c) + rng, min(o, c) - rng, c, 1000])
        price = c
    return out


def choppy_klines(n=40, base=100.0, amp=1.0, rng=1.0, drift=0.0):
    """위아래로만 흔들리는 캔들 → ADX 낮음

    drift > 0 이면 20일 MA 기울기는 상승(=SIDEWAYS 필터 통과)이면서
    ADX 는 낮은 '추세 없는 상승 표류' 상태를 만든다.
    ADX 필터가 없으면 정확히 이런 코인이 뽑힌다.
    """
    out = []
    for i in range(n):
        mid   = base + drift * i
        # 캔들 **전체**(고가·저가)를 번갈아 밀어야 ADX 가 낮아진다.
        # 시가·종가만 흔들면 고가·저가는 단조 상승이라 ADX 는 완벽한 추세로 읽는다.
        swing = amp if i % 2 else -amp
        o = mid + swing
        c = mid - swing * 0.5
        out.append([0, o, mid + amp + rng + swing, mid - amp - rng + swing, c, 1000])
    return out


class ScanAPI:
    """심볼별로 다른 캔들을 주는 가짜 거래소"""

    def __init__(self, book: dict):
        self.book = book            # {symbol: klines}

    def get_all_tickers(self):
        return [{
            "symbol": s,
            "quoteVolume": str((config.MIN_VOLUME_USDT + config.MAX_VOLUME_USDT) / 2),
            "priceChangePercent": "3.0",
            "lastPrice": str(k[-1][4]),
        } for s, k in self.book.items()]

    def get_klines(self, symbol, interval, limit=100):
        k = self.book.get(symbol, [])
        return k[-limit:] if limit else k


def test_adx_calculation_separates_trend_from_chop():
    print("\n[1] ADX 계산 — 추세와 횡보를 구분하는가")
    adx_trend = calc_adx(trending_klines())
    adx_chop  = calc_adx(choppy_klines(drift=0.35))
    print(f"    추세 캔들 ADX {adx_trend:.1f} / 횡보 캔들 ADX {adx_chop:.1f}")
    print(f"    기준 ADX_MIN_THRESHOLD = {config.ADX_MIN_THRESHOLD}")
    assert 0 <= adx_trend <= 100, f"ADX 는 0~100 범위여야 한다: {adx_trend}"
    assert 0 <= adx_chop  <= 100, f"ADX 는 0~100 범위여야 한다: {adx_chop}"
    assert adx_trend > config.ADX_MIN_THRESHOLD, "추세인데 ADX 가 기준 미달"
    assert adx_chop < config.ADX_MIN_THRESHOLD, "횡보인데 ADX 가 기준 통과"
    print("    ✅")


def test_adx_filter_blocks_chop():
    """★ 이전에는 이 필터가 아예 없어서 횡보 코인이 그대로 통과했다"""
    print("\n[2] ★ ADX 필터가 횡보 코인을 걸러내는가")
    api = ScanAPI({
        "TRENDUSDT-USDT": trending_klines(),
        "CHOPUSDT-USDT":  choppy_klines(drift=0.35),   # 상승 표류 + 무추세
    })
    picked = {c["symbol"] for c in CoinScanner(api).scan()}
    print(f"    선정된 코인: {picked or '없음'}")
    assert "CHOPUSDT-USDT" not in picked, \
        "횡보 코인이 선정됐다 — ADX 필터가 작동하지 않는다"
    print("    ✅ 횡보 코인 제외됨")

    # 품질 게이트를 전부 무력화하면 다시 통과하는지 확인한다.
    # (횡보 코인을 막는 건 ADX 뿐이 아니다 — 최근 흐름 엔진의 모멘텀·신장도
    #  판정도 함께 막는다. 어느 하나가 아니라 "품질 게이트 묶음"이 원인임을
    #  보이는 것이 이 테스트의 목적이다.)
    saved = (coin_scanner.ADX_MIN_THRESHOLD, coin_scanner.RECENCY_ADX_MIN,
             coin_scanner.RECENCY_MIN_MOMENTUM, coin_scanner.RECENCY_MAX_EXTENSION_ATR)
    coin_scanner.ADX_MIN_THRESHOLD = 0
    coin_scanner.RECENCY_ADX_MIN = 0.0
    coin_scanner.RECENCY_MIN_MOMENTUM = -99.0
    coin_scanner.RECENCY_MAX_EXTENSION_ATR = 999.0
    try:
        picked_off = {c["symbol"] for c in CoinScanner(api).scan()}
        print(f"    품질 게이트 해제 시: {picked_off or '없음'}")
        assert "CHOPUSDT-USDT" in picked_off, \
            "게이트를 껐는데도 안 뽑힌다 — 다른 이유로 걸린 것"
    finally:
        (coin_scanner.ADX_MIN_THRESHOLD, coin_scanner.RECENCY_ADX_MIN,
         coin_scanner.RECENCY_MIN_MOMENTUM,
         coin_scanner.RECENCY_MAX_EXTENSION_ATR) = saved
    print("    ✅ 품질 게이트가 원인임을 확인 (껐더니 통과)")


def test_min_score_is_effectively_no_filter():
    """MIN_SCORE 가 실질 필터로 작동하는지 — 현재는 아니라는 것을 기록해 둔다"""
    print("\n[3] MIN_SCORE 실효성 점검")
    api = ScanAPI({"TRENDUSDT-USDT": trending_klines()})
    got = CoinScanner(api).scan()
    if got:
        score = got[0]["score"]
        ratio = score / coin_scanner.MIN_SCORE
        print(f"    통과 코인 점수 {score:.3f} vs MIN_SCORE {coin_scanner.MIN_SCORE}")
        print(f"    → 기준의 {ratio:,.0f}배")
        print("    ※ MIN_SCORE 는 사실상 아무것도 거르지 않는다 (문서화 목적 테스트)")
    else:
        print("    통과 코인 없음 — 다른 필터가 먼저 걸렀다")
    print("    ✅")


# ── 장부 대조 ────────────────────────────────────────────────

class BalanceAPI:
    def __init__(self, equity):
        self._equity = equity
        self.calls = 0

    def get_equity(self):
        self.calls += 1
        return self._equity

    # 포지션 생성에 필요한 최소 인터페이스
    def get_contracts(self):               return []
    def get_price_precision(self, s):      return 4
    def get_positions(self, s=None):       return []
    def set_leverage(self, *a, **k):       return {"code": 0}
    def place_order(self, *a, **k):        return {"code": 0}
    def close_position(self, *a, **k):     return {"code": 0}
    def cancel_order(self, *a, **k):       return {"code": 0}
    def cancel_all_open_orders(self, *a, **k): return {"code": 0}
    def place_stop_market(self, *a, **k):
        return {"code": 0, "data": {"order": {"orderId": 1}}}


def new_trader(tag, api):
    paper_trader.STATE_FILE = os.path.join(_TMP, f"state_{tag}.json")
    return paper_trader.PaperTrader(live_api=api)


def test_ledger_detects_drift():
    """★ 체결가가 추정값이라 장부와 실제 잔고가 어긋난다"""
    print("\n[4] ★ 장부 대조 — 봇 손익 vs 거래소 실제 잔고")
    prev = config.LIVE_TRADING
    config.LIVE_TRADING = True
    try:
        api = BalanceAPI(equity=1030.0)          # 거래소 실제 $1,030
        pt  = new_trader("ledger", api)
        pt.total_pnl = -20.0                     # 봇 장부는 $1,050 - $20 = $1,030
        same = pt.reconcile_balance()
        print(f"    일치 상황 → 차이 ${same:+.2f}")
        assert abs(same) < 0.01

        api._equity = 1022.5                     # 실제로는 $7.5 더 잃었다
        drift = pt.reconcile_balance()
        print(f"    거래소 $1,022.50 vs 봇 $1,030.00 → 차이 ${drift:+.2f}")
        assert abs(drift + 7.5) < 0.01, drift
        assert abs(drift) >= config.LEDGER_DRIFT_WARN_USD, "경고 기준을 넘어야 함"
        print("    ✅ 추정 체결가로 누적된 오차를 잡아낸다")
    finally:
        config.LIVE_TRADING = prev


def test_ledger_skips_when_unsafe():
    """포지션 보유 중·조회 실패 시에는 대조하지 않는다"""
    print("\n[5] 대조를 건너뛰어야 하는 상황")
    prev = config.LIVE_TRADING
    config.LIVE_TRADING = True
    try:
        api = BalanceAPI(equity=1030.0)
        pt  = new_trader("ledger_pos", api)
        pt.open_position("XUSDT", "UP", 100.0)
        r = pt.reconcile_balance()
        print(f"    포지션 보유 중 → {r} (미실현 손익 때문에 비교 불가)")
        assert r is None, "보유 중인데 대조했다"

        pt.close_position(100.0, "테스트청산")

        class Flaky(BalanceAPI):
            def get_equity(self):
                raise RuntimeError("API 타임아웃")

        pt2 = new_trader("ledger_flaky", Flaky(0))
        r2 = pt2.reconcile_balance()
        print(f"    잔고 조회 실패 → {r2}")
        assert r2 is None, "조회 실패인데 값을 냈다"

        pt3 = new_trader("ledger_zero", BalanceAPI(equity=0.0))
        r3 = pt3.reconcile_balance()
        print(f"    잔고 0 응답 → {r3} (이상값으로 보고 무시)")
        assert r3 is None
        print("    ✅")
    finally:
        config.LIVE_TRADING = prev


def main():
    print("=" * 62)
    print("  코인 선정 · 장부 대조 테스트")
    print("=" * 62)
    for fn in [test_adx_calculation_separates_trend_from_chop,
               test_adx_filter_blocks_chop,
               test_min_score_is_effectively_no_filter,
               test_ledger_detects_drift,
               test_ledger_skips_when_unsafe,
               test_stale_trend_blocked,
               test_fresh_trend_still_passes,
               test_max_loss_capped_by_capital_ratio,
               test_capital_sync,
               test_capital_sync_off_by_default,
               test_capital_sync_survives_bad_api]:
        fn()
    print("\n" + "=" * 62)
    print("  ✅ 전부 통과")
    print("=" * 62)




# ── 철지난 흐름 차단 ─────────────────────────────────────────

def series(n, start, step, rng=1.0):
    """단조 추세 캔들"""
    out, p = [], start
    for _ in range(n):
        o, c = p, p + step
        out.append([0, o, max(o, c) + rng, min(o, c) - rng, c, 1000])
        p = c
    return out


class StaleAPI(ScanAPI):
    """타임프레임별로 다른 흐름을 주는 가짜 거래소.

    일봉은 하락(20일 추세) 인데 4h·15m 은 이미 반등한 상황을 만든다.
    = 사용자가 지적한 "철지난 흐름" 그대로.
    """
    def __init__(self, daily, h4, h1, m15):
        self.data = {"1d": daily, "4h": h4, "1h": h1, "15m": m15}

    def get_all_tickers(self):
        # 현재가는 1시간봉 마지막 종가로 준다. 일봉 마지막 종가를 쓰면
        # 시간축끼리 값이 어긋나 신장도(ATR 배수) 계산이 엉뚱해진다.
        last = self.data["1h"][-1][4] if self.data.get("1h") else self.data["1d"][-1][4]
        return [{"symbol": "STALE-USDT",
                 "quoteVolume": str((config.MIN_VOLUME_USDT + config.MAX_VOLUME_USDT) / 2),
                 "priceChangePercent": "-3.0",
                 "lastPrice": str(last)}]

    def get_klines(self, symbol, interval, limit=100):
        k = self.data.get(interval, [])
        return k[-limit:] if limit else k


def test_stale_trend_blocked():
    """★ 일봉은 하락인데 4h 가 이미 반등 → 진입하면 안 된다"""
    print("\n[6] ★ 철지난 흐름 차단 — 일봉 DOWN, 4h 이미 반등")
    daily = series(40, 200.0, -3.0, rng=4.0)      # 20일 하락 (강한 하락)
    h4    = series(20, 100.0, +1.5, rng=1.0)      # 4h 는 상승 반전
    h1    = series(60, 100.0, +0.2, rng=0.5)      # 1h 도 약상승
    m15   = series(20, 100.0, +0.5, rng=0.4)      # 최근 1.5h 상승

    api = StaleAPI(daily, h4, h1, m15)
    picked = {c["symbol"] for c in CoinScanner(api).scan()}
    print(f"    선정 결과: {picked or '없음'}")
    assert "STALE-USDT" not in picked, \
        "일봉 하락 라벨로 숏 진입했다 — 철지난 흐름 차단 실패"
    print("    ✅ 차단됨 (예전 구조라면 일봉 라벨을 따라 SHORT 로 들어갔다)")

    # 일봉 거부권을 풀면 통과하는지 — 그 거부권이 원인임을 확인
    saved = recency.OPPOSE_MAX_1D
    recency.OPPOSE_MAX_1D = 9.0
    try:
        picked_off = {c["symbol"] for c in CoinScanner(api).scan()}
        print(f"    일봉 거부권 해제 시: {picked_off or '없음'}")
        assert "STALE-USDT" in picked_off, "해제했는데도 안 뽑힘 — 다른 이유로 걸림"
        got = [c for c in CoinScanner(api).scan() if c["symbol"] == "STALE-USDT"][0]
        assert got["trend"] == "UP", \
            f"방향이 {got['trend']} — 최근 흐름(상승)을 따라야 한다"
        print(f"    방향 {got['trend']} ({got['dir_src']} 기준) "
              "← 일봉 라벨(DOWN)이 아니라 최근 흐름을 따른다")
    finally:
        recency.OPPOSE_MAX_1D = saved
    print("    ✅ 일봉 거부권이 원인임을 확인")


def test_fresh_trend_still_passes():
    """모든 타임프레임이 같은 방향이면 통과해야 한다"""
    print("\n[7] 흐름이 살아있으면 통과하는가")
    daily = series(40, 200.0, -3.0, rng=4.0)      # 하락
    h4    = series(20, 100.0, -1.5, rng=1.0)      # 4h 도 하락
    h1    = series(60, 100.0, -0.3, rng=0.5)      # 1h 도 하락
    m15   = series(20, 100.0, -0.5, rng=0.4)      # 최근도 하락
    api = StaleAPI(daily, h4, h1, m15)
    got = CoinScanner(api).scan()
    picked = {c["symbol"] for c in got}
    print(f"    선정 결과: {picked or '없음'}")
    assert "STALE-USDT" in picked, "전 구간 하락인데 차단됐다 — 너무 엄격"
    c = got[0]
    print(f"    방향 {c['trend']}({c['dir_src']}) | 1h ADX {c['adx']:.0f} | "
          f"신장 {c['fresh_ext']:+.2f}ATR | 나이 {c['fresh_age']}봉")
    assert c["trend"] == "DOWN"
    print("    ✅ 통과")



# ── 확신도 시드가 커져도 손실 한도는 자본에 묶여 있는가 ─────

def test_max_loss_capped_by_capital_ratio():
    """★ 확신도로 시드를 키우면 시드×4 손절선도 함께 커진다.
    자본 대비 상한이 없으면 자본 $500 에서 1회 손실이 절반까지 간다."""
    print("\n[7b] ★ 손실 한도는 자본 비율로 한 번 더 묶인다")
    prev_live = config.LIVE_TRADING
    config.LIVE_TRADING = False
    try:
        pt = new_trader("maxloss", BalanceAPI(equity=500.0))
        pt.total_capital, pt.total_pnl = 500.0, 0.0

        ratio   = config.MAX_NET_LOSS_CAPITAL_RATIO
        seed    = 45.0                      # 확신도 1.35 로 부풀린 시드
        by_seed = seed * config.MAX_NET_LOSS_SEED_MULT      # $180
        by_cap  = 500.0 * ratio                             # $110

        pt.open_position("XUSDT", "UP", 100.0, invest_override=seed)
        got = -pt._get_max_loss_usd()
        print(f"    시드 ${seed:.0f} → 시드×4 = ${by_seed:.0f}")
        print(f"    자본 $500 × {ratio:.0%} = ${by_cap:.0f}")
        print(f"    실제 적용 손절 한도 = ${got:.2f}")
        assert abs(got - by_cap) < 0.01, f"자본 비율 상한이 안 걸렸다: {got}"
        assert got < by_seed, "시드 기준보다 작아야 한다"
        print(f"    ✅ 자본의 {got/500:.0%} 로 묶임")

        # 손실이 커져도 손절선이 따라 당겨지면 안 된다 (자기 조기 손절 방지)
        pt.total_pnl = -200.0
        still = -pt._get_max_loss_usd()
        print(f"    자본이 $300 으로 줄어든 뒤에도 한도 ${still:.2f} (진입 시점 기준 유지)")
        assert abs(still - by_cap) < 0.01, "보유 중 손절선이 당겨졌다"
        print("    ✅")
    finally:
        config.LIVE_TRADING = prev_live


# ── 기동 시 자본 동기화 (봇 전용 서브계좌용) ──────────────────

def test_capital_sync():
    """★ 서브계좌 잔고에 자본을 맞춘다 — total_pnl 은 보존"""
    print("\n[8] ★ 기동 시 자본 동기화")
    prev_live, prev_sync = config.LIVE_TRADING, config.CAPITAL_AUTO_SYNC
    config.LIVE_TRADING = True
    config.CAPITAL_AUTO_SYNC = True
    try:
        # 서브계좌 실제 잔고 $980, 봇은 자본 $1050 + 누적손익 -$23.37 = $1026.63
        pt = new_trader("capsync", BalanceAPI(equity=980.0))
        pt.total_pnl = -23.37
        before_book = pt.total_capital + pt.total_pnl
        print(f"    조정 전: 기준자본 ${pt.total_capital:,.2f} + "
              f"누적손익 ${pt.total_pnl:+,.2f} = ${before_book:,.2f}")

        changed = pt.sync_capital_with_exchange()
        after_book = pt.total_capital + pt.total_pnl
        print(f"    조정 후: 기준자본 ${pt.total_capital:,.2f} + "
              f"누적손익 ${pt.total_pnl:+,.2f} = ${after_book:,.2f}")

        assert changed, "동기화가 일어나지 않음"
        assert abs(after_book - 980.0) < 0.01, "확정자본이 거래소와 안 맞음"
        assert abs(pt.total_pnl + 23.37) < 1e-9, \
            "누적손익이 바뀌었다 — 승패 기록의 의미가 깨진다"
        print("    ✅ 확정자본은 거래소와 일치, 누적손익은 그대로")
    finally:
        config.LIVE_TRADING, config.CAPITAL_AUTO_SYNC = prev_live, prev_sync


def test_capital_sync_off_by_default():
    """꺼져 있으면 경고만 하고 아무것도 바꾸지 않는다"""
    print("\n[9] CAPITAL_AUTO_SYNC = False (기본값)")
    prev_live, prev_sync = config.LIVE_TRADING, config.CAPITAL_AUTO_SYNC
    config.LIVE_TRADING = True
    config.CAPITAL_AUTO_SYNC = False
    try:
        pt = new_trader("capsync_off", BalanceAPI(equity=980.0))
        cap_before = pt.total_capital
        changed = pt.sync_capital_with_exchange()
        print(f"    변경 여부 {changed} / 기준자본 ${pt.total_capital:,.2f} 유지")
        assert not changed and pt.total_capital == cap_before
        print("    ✅ 본계좌처럼 입출금이 섞이는 계좌를 보호한다")
    finally:
        config.LIVE_TRADING, config.CAPITAL_AUTO_SYNC = prev_live, prev_sync


def test_capital_sync_survives_bad_api():
    """조회 실패·잔고 0 에서는 설정값을 지킨다"""
    print("\n[10] 잔고 조회가 이상할 때")
    prev_live, prev_sync = config.LIVE_TRADING, config.CAPITAL_AUTO_SYNC
    config.LIVE_TRADING = True
    config.CAPITAL_AUTO_SYNC = True
    try:
        class Dead(BalanceAPI):
            def get_equity(self):
                raise RuntimeError("API 다운")

        pt = new_trader("capsync_dead", Dead(0))
        cap = pt.total_capital
        assert not pt.sync_capital_with_exchange() and pt.total_capital == cap
        print(f"    조회 실패 → 기준자본 ${cap:,.2f} 유지")

        pt2 = new_trader("capsync_zero", BalanceAPI(equity=0.0))
        cap2 = pt2.total_capital
        assert not pt2.sync_capital_with_exchange() and pt2.total_capital == cap2
        print(f"    잔고 0 응답 → 기준자본 ${cap2:,.2f} 유지")
        print("    ✅ 일시적 오류로 자본을 0 으로 만들지 않는다")
    finally:
        config.LIVE_TRADING, config.CAPITAL_AUTO_SYNC = prev_live, prev_sync

if __name__ == "__main__":
    main()
