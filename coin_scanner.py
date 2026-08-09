"""
코인 선정 모듈
────────────────
RECENCY_ENABLED=True (기본) 일 때의 선정 순서:

  [싼 필터 — 티커 한 번으로 끝나는 것]
  1. 거래량     : 24h $50M ~ $300M (유동성 있고, 너무 둔하지 않은 구간)
  2. 대형코인·블랙리스트·지수상품 제외
  3. 과열 제외  : 24h 등락 ±15% 초과 (급등·급락 직후)

  [일봉 — 성질을 본다. 방향은 안 본다]
  4. 변동성     : 일봉 ATR/가격 3~12%  (물타기가 작동할 만큼 움직이는가)

  [1시간·15분봉 — 방향과 자리를 본다]
  5. 방향 결정  : 1h MA 기울기 (보조 15m). 4h·일봉은 거부권만.
  6. 현재 변동성: 최근 6개 1h·15m 캔들이 실제로 움직이는가
  7. 최근 역행  : 최근 1.5시간 실제 가격이 방향과 반대로 갔으면 제외
  8. 추세 강도  : **1시간봉** ADX + 최근 10개 1h 캔들의 방향 일관성
  9. 신선도     : 과신장(평균에서 몇 ATR) / 노후(몇 봉째) / 감쇠(식는 중)

  10. 최소 점수 미달이면 전부 제외 → 다음 스캔까지 대기

왜 이렇게 바꿨나
---------------
예전에는 **20일 일봉 MA 기울기**가 매매 방향을 정했다. 그런데 실제
보유 시간은 1~2시간이다. 20일 평균이 아직 위를 보고 있어도 어제부터
꺾였으면, 그 라벨로 롱을 잡는 건 지나간 흐름에 올라타는 것이다.
4h·1h "역행 차단"을 덧붙여 증상은 막았지만 방향을 고르는 근거는
그대로였다. 이제 위계를 뒤집었다 — 방향은 최근이 정하고, 과거는
정면충돌할 때만 거부한다. 자세한 내용은 recency.py 참조.

RECENCY_ENABLED=false 로 두면 예전 방식(일봉 주도)으로 돌아간다.

최종 점수 = 변동성 × 추세강도 × 정렬 × 모멘텀 × 현재활성도
            × ADX × 일관성 × 신선도
"""

import logging
import numpy as np
import recency
from bingx_api import BingXAPI
from config import (MIN_VOLUME_USDT, MAX_VOLUME_USDT, TOP_N_COINS, MA_PERIOD,
                    ADX_MIN_THRESHOLD, CONSISTENCY_MIN,
                    RECENCY_ENABLED, RECENCY_MAX_EXTENSION_ATR,
                    RECENCY_MAX_TREND_AGE, RECENCY_MIN_MOMENTUM,
                    RECENCY_ADX_MIN)

logger = logging.getLogger(__name__)

# ── 변동성 필터 범위 ──────────────────────────────────────
# ATR/가격 비율: 일봉 기준 평균 (고가-저가)/종가
# 물타기가 작동하려면 코인이 하루에 최소 ±3% 이상 움직여야 함
VOLATILITY_MIN = 0.030   # ATR/가격 최소 3.0% (코인 일 1% 이상 움직이는 종목만)
VOLATILITY_MAX = 0.12    # ATR/가격 최대 12% (너무 극단적인 코인 제외)

# ── 현재 변동성 필터 ──────────────────────────────────────
# 1h 캔들 기준 (고가-저가)/종가 평균 → 최근 6시간 활성도
RECENT_VOL_MIN_1H = 0.005  # 최근 1h 평균 변동폭 최소 0.5% (시장 조용할 때 대응 완화)
# 15m 캔들 기준 (고가-저가)/종가 평균 → 지금 이 순간 활성도
RECENT_VOL_MIN_15M = 0.002 # 최근 15m 평균 변동폭 최소 0.2% (시장 조용할 때 대응 완화)

# ── 철지난 흐름 차단 ──────────────────────────────────────
# 매매 방향은 20일 일봉 MA 기울기로 정하는데 실제 보유 시간은 1~2시간이다.
# 20일 추세가 이미 꺾였는데 그 라벨로 들어가면 지나간 흐름에 올라타는 셈이다.
REQUIRE_4H_ALIGN = True     # 4h 봉이 반대로 돌았으면 진입 차단
                            # (기존: 점수만 0.4배 깎고 통과시켰다)
RECENT_MOVE_LOOKBACK_15M = 6      # 최근 15m 캔들 N개 = 1.5시간
RECENT_MOVE_MAX_ADVERSE  = 0.003  # 그 구간 실제 가격이 방향과 반대로
                                  # 0.3% 이상 갔으면 차단 (MA 가 아닌 실제 이동)

# ── 최소 점수 기준 ────────────────────────────────────────
MIN_SCORE = 0.01  # ADX·일관성 보너스 추가로 점수 스케일 낮아짐 → 기준 하향 (0.05→0.01)

# ── 과열 필터 ─────────────────────────────────────────────
MAX_24H_CHANGE = 0.15    # 24h 등락률 ±15% 초과 시 제외

# ── 제외 키워드 (지수·원자재 추종 상품) ─────────────────
EXCLUDE_KEYWORDS = ["GOLD", "NASDAQ", "NCC", "NCSK", "USD2USD", "SP500", "OIL"]

# ── 대형 코인 직접 제외 (유통량 과다 → 움직임 둔함) ───────
EXCLUDE_LARGE_CAPS = {
    "BTC-USDT", "ETH-USDT", "BNB-USDT", "XRP-USDT",
    "SOL-USDT", "ADA-USDT", "DOGE-USDT", "TRX-USDT",
    "LINK-USDT", "AVAX-USDT", "TON-USDT", "SHIB-USDT",
    "DOT-USDT", "MATIC-USDT", "LTC-USDT", "BCH-USDT",
    "1000PEPE-USDT", "1000SHIB-USDT",
}

# ── 손절 반복 코인 영구 블랙리스트 ────────────────────────
# 고단계(3+) 최대손실손절 반복으로 누적 대규모 손실을 유발한 코인
BLACKLIST = {
    "SAGA-USDT",          # 3× 최대손실손절 (-$305, -$283, -$259) 누적 -$847
    "ZRO-USDT",           # 4단계 최대손실손절 (-$495)
    "ZEC-USDT",           # 4단계 최대손실손절 (-$409)
    "NCSKMRVL2USD-USDT",  # 4단계 최대손실손절 (-$270)
    "ARB-USDT",           # 4단계 최대손실손절 (-$268)
    "TAO-USDT",           # 5단계 마지막결전청산 3회 반복 (-$55, -$124, -$169) 누적 -$349
}


def calc_adx(klines: list, period: int = 14) -> float:
    """
    ADX (Average Directional Index) — 추세 강도 측정
    반환값: 0~100 (25 이상 = 강한 추세, 20 미만 = 횡보)
    """
    if len(klines) < period * 2:
        return 0.0

    highs  = [_kline_val(k, "high",  2) for k in klines]
    lows   = [_kline_val(k, "low",   3) for k in klines]
    closes = [_kline_val(k, "close", 4) for k in klines]

    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(1, len(highs)):
        up   = highs[i]  - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up   if up   > down and up   > 0 else 0.0)
        minus_dm.append(down if down > up  and down > 0 else 0.0)
        tr_list.append(max(
            highs[i] - lows[i],
            abs(highs[i]  - closes[i - 1]),
            abs(lows[i]   - closes[i - 1])
        ))

    if len(tr_list) < period:
        return 0.0

    def wilder_sum(data: list, n: int) -> list:
        """Wilder 누적 평활 — TR·DM 용.
        DI 계산에서 서로 나누므로 누적 형태여도 비율은 정확하다."""
        s = [sum(data[:n])]
        for v in data[n:]:
            s.append(s[-1] - s[-1] / n + v)
        return s

    def wilder_avg(data: list, n: int) -> list:
        """Wilder 이동평균 — ADX(=DX 의 평균) 용.

        ⚠ 여기에 wilder_sum 을 쓰면 값이 n배(=14배) 부풀려진다.
          완벽한 추세에서 DX 는 매 봉 100 이고, 누적형은 1400 을 낸다.
          그러면 ADX 가 항상 상한(0~100)을 훌쩍 넘어
          adx_bonus = min(adx/25, 2.0) 이 늘 2.0 으로 고정되고,
          ADX 필터도 사실상 무력해진다.
        """
        a = [sum(data[:n]) / n]
        for v in data[n:]:
            a.append((a[-1] * (n - 1) + v) / n)
        return a

    atr_s = wilder_sum(tr_list, period)
    pdm_s = wilder_sum(plus_dm, period)
    mdm_s = wilder_sum(minus_dm, period)

    dx_list = []
    for a, p, m in zip(atr_s, pdm_s, mdm_s):
        if a == 0:
            continue
        pdi   = 100.0 * p / a
        mdi   = 100.0 * m / a
        denom = pdi + mdi
        dx_list.append(100.0 * abs(pdi - mdi) / denom if denom > 0 else 0.0)

    if len(dx_list) < period:
        return 0.0

    adx_s = wilder_avg(dx_list, period)
    return round(adx_s[-1], 2) if adx_s else 0.0


def calc_trend_consistency(closes: list, trend: str, period: int = 10) -> float:
    """
    최근 N캔들 중 트렌드 방향 캔들 비율 (0.0 ~ 1.0)
    예) UP 트렌드에서 10캔들 중 7개가 상승 마감 → 0.7
    """
    if len(closes) < period + 1:
        return 0.5
    recent = closes[-(period + 1):]
    count  = sum(
        1 for i in range(1, len(recent))
        if (trend == "UP"   and recent[i] > recent[i - 1]) or
           (trend == "DOWN" and recent[i] < recent[i - 1])
    )
    return count / period


def _kline_val(k, key_dict: str, idx_list: int):
    """BingX kline이 dict일 수도 list일 수도 있어서 통일"""
    if isinstance(k, dict):
        return float(k[key_dict])
    return float(k[idx_list])


def calc_atr_ratio(daily: list) -> float:
    """
    일봉 ATR / 현재가 비율 (최근 14일)
    ATR = 평균 (high - low) / close
    """
    if len(daily) < 2:
        return 0.0
    ratios = []
    for k in daily[-14:]:
        h = _kline_val(k, "high",  2)
        l = _kline_val(k, "low",   3)
        c = _kline_val(k, "close", 4)
        if c > 0:
            ratios.append((h - l) / c)
    return float(np.mean(ratios)) if ratios else 0.0


def calc_ma(closes: list, period: int) -> float:
    if len(closes) < period:
        return 0.0
    return float(np.mean(closes[-period:]))


def calc_ma_slope(closes: list, period: int = MA_PERIOD) -> float:
    """이동평균 기울기 (변화율)"""
    if len(closes) < period + 1:
        return 0.0
    arr    = np.array(closes[-(period + 1):], dtype=float)
    ma_old = float(np.mean(arr[:period]))
    ma_now = float(np.mean(arr[1:]))
    return (ma_now - ma_old) / ma_old


def get_trend(slope: float) -> str:
    if slope > 0.002:
        return "UP"
    elif slope < -0.002:
        return "DOWN"
    return "SIDEWAYS"


class CoinScanner:
    def __init__(self, api: BingXAPI):
        self.api = api

    def scan(self) -> list[dict]:
        tickers = self.api.get_all_tickers()
        candidates = []

        # 필터별 탈락 카운터
        _f = {
            "거래량부족": 0, "거래량초과": 0, "대형코인": 0,
            "블랙리스트": 0, "키워드제외": 0, "과열": 0,
            "데이터부족": 0, "ATR범위외": 0, "SIDEWAYS": 0,
            "ADX부족": 0, "일관성부족": 0, "4h역행": 0, "1h역행": 0, "최근역행": 0,
            "1h변동성": 0, "15m변동성": 0,
            # 최근 흐름 엔진
            "최근흐름없음": 0,   # 15m·1h 가 횡보거나 서로 충돌 / 큰 축이 거부
            "과신장": 0,        # 단기 평균에서 너무 벌어짐 = 늦은 자리
            "모멘텀감쇠": 0,     # 식어가는 흐름 (나이는 차단 안 함 — 점수만 조정)
        }

        for t in tickers:
            symbol = t.get("symbol", "")
            if not symbol.endswith("-USDT"):
                continue

            # ── 1. 거래량 필터 (최소~최대 범위) ──────────
            volume_usdt = float(t.get("quoteVolume", 0))
            if volume_usdt < MIN_VOLUME_USDT:
                _f["거래량부족"] += 1
                continue
            if volume_usdt > MAX_VOLUME_USDT:
                _f["거래량초과"] += 1
                logger.debug(f"{symbol} 거래량 초과 제외: ${volume_usdt/1e6:.0f}M")
                continue

            # ── 대형 코인 제외 ─────────────────────────
            if symbol in EXCLUDE_LARGE_CAPS:
                _f["대형코인"] += 1
                continue

            # ── 블랙리스트 제외 ────────────────────────
            if symbol in BLACKLIST:
                _f["블랙리스트"] += 1
                continue

            # ── 지수·원자재 추종 상품 제외 ───────────────
            if any(kw in symbol for kw in EXCLUDE_KEYWORDS):
                _f["키워드제외"] += 1
                continue

            # ── 5. 과열 필터 (24h 등락률) ───────────────
            change_24h = float(t.get("priceChangePercent", 0)) / 100
            if abs(change_24h) > MAX_24H_CHANGE:
                _f["과열"] += 1
                logger.debug(f"{symbol} 과열 제외: 24h {change_24h:.1%}")
                continue

            try:
                # 일봉 캔들
                daily = self.api.get_klines(symbol, "1d", limit=max(MA_PERIOD + 5, 35))
                if len(daily) < MA_PERIOD + 1:
                    _f["데이터부족"] += 1
                    continue

                closes_d = [_kline_val(k, "close", 4) for k in daily]

                # ── 2. 변동성 필터 (ATR 기반) ────────────
                atr_ratio = calc_atr_ratio(daily)
                if atr_ratio < VOLATILITY_MIN or atr_ratio > VOLATILITY_MAX:
                    _f["ATR범위외"] += 1
                    logger.debug(
                        f"{symbol} 변동성 범위 외: ATR비율 {atr_ratio:.3f}"
                    )
                    continue

                slope_d = calc_ma_slope(closes_d, MA_PERIOD)
                adx_d   = calc_adx(daily)

                # ── 3. 단기 캔들 확보 ────────────────────
                # 1h 는 60개 받는다. ADX(14)를 1시간봉에서 계산하려면
                # 최소 28개가 필요하고, 흐름 나이는 최대 18봉까지 거슬러
                # 올라간다. 12개로는 둘 다 불가능했다.
                closes_4h, hourly1, closes_1h, hl_1h, m15, closes_15m = [], [], [], [], [], []
                try:
                    hourly4   = self.api.get_klines(symbol, "4h", limit=20)
                    closes_4h = [_kline_val(k, "close", 4) for k in hourly4]
                except Exception:
                    pass
                try:
                    hourly1   = self.api.get_klines(symbol, "1h", limit=60)
                    closes_1h = [_kline_val(k, "close", 4) for k in hourly1]
                    hl_1h     = [(_kline_val(k, "high", 2), _kline_val(k, "low", 3),
                                  _kline_val(k, "close", 4)) for k in hourly1]
                except Exception:
                    pass
                try:
                    m15        = self.api.get_klines(symbol, "15m", limit=20)
                    closes_15m = [_kline_val(k, "close", 4) for k in m15]
                except Exception:
                    pass

                slope_4h = calc_ma_slope(closes_4h, 10) if len(closes_4h) >= 11 else 0.0
                slope_1h = calc_ma_slope(closes_1h, 6)  if len(closes_1h) >= 7  else 0.0
                trend_4h = get_trend(slope_4h)
                trend_1h = get_trend(slope_1h)

                # ── 4. 방향 결정 ─────────────────────────
                if RECENCY_ENABLED:
                    if len(closes_1h) < 20:
                        _f["데이터부족"] += 1
                        continue
                    verdict = recency.decide_direction(
                        closes_15m, closes_1h, closes_4h, closes_d)
                    trend = verdict["trend"]
                    if trend is None:
                        _f["최근흐름없음"] += 1
                        logger.debug(f"{symbol} 방향 없음: {verdict['reason']}")
                        continue
                    dir_src   = verdict["src"]
                    dir_agree = verdict["agree"]
                else:
                    # 예전 방식 — 20일 일봉 MA 가 방향을 정한다
                    trend = get_trend(slope_d)
                    if trend == "SIDEWAYS":
                        _f["SIDEWAYS"] += 1
                        continue
                    if adx_d < ADX_MIN_THRESHOLD:
                        _f["ADX부족"] += 1
                        continue
                    if REQUIRE_4H_ALIGN and trend_4h != "SIDEWAYS" and trend_4h != trend:
                        _f["4h역행"] += 1
                        continue
                    if trend_1h != "SIDEWAYS" and trend_1h != trend:
                        _f["1h역행"] += 1
                        continue
                    dir_src, dir_agree = "1d", (trend_4h == trend and trend_1h == trend)

                # ── 5. 현재 변동성 (지금 움직이는 코인만) ──
                recent_vol_1h = 0.0
                if len(hourly1) >= 6:
                    rr = [(h - l) / c for h, l, c in hl_1h[-6:] if c > 0]
                    if rr:
                        recent_vol_1h = float(np.mean(rr))
                        if recent_vol_1h < RECENT_VOL_MIN_1H:
                            _f["1h변동성"] += 1
                            continue

                recent_move_15m = None
                if len(m15) >= 6:
                    rr15 = []
                    for k in m15[-6:]:
                        h = _kline_val(k, "high",  2)
                        l = _kline_val(k, "low",   3)
                        c = _kline_val(k, "close", 4)
                        if c > 0:
                            rr15.append((h - l) / c)
                    if rr15:
                        recent_vol_15m = float(np.mean(rr15))
                        if recent_vol_15m < RECENT_VOL_MIN_15M:
                            _f["15m변동성"] += 1
                            continue
                    seg = closes_15m[-RECENT_MOVE_LOOKBACK_15M:]
                    if len(seg) >= 2 and seg[0] > 0:
                        raw = (seg[-1] - seg[0]) / seg[0]
                        recent_move_15m = raw if trend == "UP" else -raw

                # 최근 1.5시간 실제 가격이 방향과 반대로 갔으면 차단.
                # MA 기울기가 아니라 **실제 이동**을 본다 — 급반전을 잡는다.
                if (recent_move_15m is not None
                        and recent_move_15m < -RECENT_MOVE_MAX_ADVERSE):
                    _f["최근역행"] += 1
                    continue

                price = float(t.get("lastPrice", 0))

                # ── 6. 추세 강도·일관성 ──────────────────
                # 최근 흐름 모드에서는 1시간봉 기준으로 본다.
                # 일봉 ADX 는 "며칠짜리 추세"를 재는 값이라, 1~2시간 보유하는
                # 매매의 진입 근거로는 시간축이 맞지 않는다.
                if RECENCY_ENABLED:
                    adx = calc_adx(hourly1) if len(hourly1) >= 30 else 0.0
                    if adx < RECENCY_ADX_MIN:
                        _f["ADX부족"] += 1
                        logger.debug(f"{symbol} 1h ADX 부족: {adx:.1f} < {RECENCY_ADX_MIN}")
                        continue
                    consistency = calc_trend_consistency(closes_1h, trend, period=10)
                    adx_ref = 22.0
                else:
                    adx = adx_d
                    consistency = calc_trend_consistency(closes_d, trend, period=10)
                    adx_ref = 25.0

                if consistency < CONSISTENCY_MIN:
                    _f["일관성부족"] += 1
                    continue

                # ── 7. 신선도 판정 (과신장 / 노후 / 감쇠) ──
                fresh = {"ok": True, "bonus": 1.0, "ext": 0.0, "age": 0, "mom": 1.0,
                         "why": "판정 안 함"}
                if RECENCY_ENABLED:
                    fresh = recency.freshness_verdict(
                        price, closes_1h, hl_1h, trend,
                        RECENCY_MAX_EXTENSION_ATR,
                        RECENCY_MAX_TREND_AGE,
                        RECENCY_MIN_MOMENTUM)
                    if not fresh["ok"]:
                        key = "과신장" if "과신장" in fresh["why"] else "모멘텀감쇠"
                        _f[key] += 1
                        logger.debug(f"{symbol} {fresh['why']}")
                        continue

                # ── 8. 큰 그림 모멘텀 (보너스만) ─────────
                # 일봉 MA 대비 위치. 이제 진입을 막지는 않고 점수만 조정한다.
                ma20_d      = calc_ma(closes_d, 20)
                momentum_ok = ((trend == "UP"   and price >= ma20_d) or
                               (trend == "DOWN" and price <= ma20_d))
                momentum_bonus = 1.2 if momentum_ok else 0.85

                # ── 9. 점수 ──────────────────────────────
                align_bonus       = 1.8 if dir_agree else 1.0
                consistency_bonus = 0.5 + consistency
                adx_bonus         = min(adx / adx_ref, 2.0)
                # 변동성 최우선: ATR 6% 근처 최고점
                vol_score         = atr_ratio * (1 - abs(atr_ratio - 0.06) / 0.10)
                # 추세 점수: 최근 시간축에 가중치를 싣는다 (기존은 일봉 위주)
                if RECENCY_ENABLED:
                    trend_score = (abs(slope_1h) * 1.0 + abs(slope_4h) * 0.3
                                   + abs(slope_d) * 0.1)
                else:
                    trend_score = abs(slope_d) + abs(slope_4h) * 0.5 + abs(slope_1h) * 1.0
                live_bonus = 1.0 + min(recent_vol_1h / RECENT_VOL_MIN_1H, 3.0)

                score = (vol_score * trend_score * align_bonus * momentum_bonus
                         * live_bonus * adx_bonus * consistency_bonus
                         * fresh["bonus"] * 1000)

                candidates.append({
                    "symbol":        symbol,
                    "trend":         trend,
                    "dir_src":       dir_src,
                    "dir_agree":     dir_agree,
                    "trend_4h":      trend_4h,
                    "trend_1h":      trend_1h,
                    "slope_d":       slope_d,
                    "slope_4h":      slope_4h,
                    "slope_1h":      slope_1h,
                    "atr_ratio":     atr_ratio,
                    "recent_vol_1h": round(recent_vol_1h, 4),
                    "recent_move_15m": (round(recent_move_15m, 6)
                                        if recent_move_15m is not None else None),
                    "change_24h":    change_24h,
                    "momentum_ok":   momentum_ok,
                    "adx":           adx,
                    "adx_d":         adx_d,
                    "consistency":   round(consistency, 2),
                    "fresh_ext":     round(fresh["ext"], 2),
                    "fresh_age":     fresh["age"],
                    "fresh_mom":     round(fresh["mom"], 2),
                    "volume":        volume_usdt,
                    "price":         price,
                    "score":         score,
                })

            except Exception as e:
                logger.debug(f"{symbol} 스캔 실패: {e}")
                continue

        candidates.sort(key=lambda x: x["score"], reverse=True)

        # ── 최소 점수 필터: 기준 미달이면 진입 안 함 ──────────
        qualified = [c for c in candidates if c["score"] >= MIN_SCORE]
        top = qualified[:TOP_N_COINS]

        total_usdt = len([t for t in tickers if t.get("symbol", "").endswith("-USDT")])
        logger.info(
            f"코인 스캔 완료: 전체 {total_usdt}개 "
            f"→ 조건 통과 {len(candidates)}개 "
            f"→ 점수기준({MIN_SCORE}) 통과 {len(qualified)}개 "
            f"→ 상위 {len(top)}개"
        )
        # 필터별 탈락 통계 — 항상 남긴다.
        # 어떤 필터가 실제로 일하고 있는지 봐야 선정 기준을 판단할 수 있다.
        stats = " / ".join(f"{k}:{v}" for k, v in _f.items() if v > 0)
        logger.info(f"  [필터통계] {stats if stats else '탈락 없음'}")

        if not top:
            logger.info(f"  ※ 최소 점수({MIN_SCORE}) 충족 코인 없음 → 진입 대기")
            if candidates:
                top3 = candidates[:3]
                scores_str = " / ".join(
                    f"{c['symbol']}={c['score']:.4f}(ADX:{c['adx']:.0f},일관성:{c['consistency']:.0%},1h:{c['trend_1h']})"
                    for c in top3
                )
                logger.info(f"  [점수TOP3] {scores_str}")

        def _arrow(t4, t):
            if t4 == t: return "↑↑"
            if t4 == "SIDEWAYS": return "→"
            return "↑↓"

        for c in top:
            mo = "✓" if c["momentum_ok"] else "△"
            fresh_str = (f" | 신선도(나이{c.get('fresh_age', 0)}봉 "
                         f"신장{c.get('fresh_ext', 0):+.1f}ATR "
                         f"모멘텀{c.get('fresh_mom', 0):.2f})"
                         if RECENCY_ENABLED else "")
            logger.info(
                f"  {c['symbol']:22s} | {c['trend']:4s}({c.get('dir_src', '?')}) | "
                f"4h:{_arrow(c['trend_4h'], c['trend'])} "
                f"1h:{_arrow(c['trend_1h'], c['trend'])} | "
                f"ATR {c['atr_ratio']:.3f} | ADX {c['adx']:.1f} | "
                f"일관성 {c['consistency']:.0%} | "
                f"1h변동 {c['recent_vol_1h']:.4f} | "
                f"24h {c['change_24h']:+.1%} | 모멘텀{mo}{fresh_str} | "
                f"점수 {c['score']:.4f}"
            )
        return top

    def pick_best(self) -> dict | None:
        result = self.scan()
        return result[0] if result else None

    def scan_crash_shorts(self, blocked: set = None) -> list[dict]:
        """
        BTC 급락 시 빠른 스캔 — 단일 API 호출로 가장 많이 떨어지는 코인 선정.
        풀 스캔(30~60초) 대신 5~10초 만에 완료.
        점수 = |24h 낙폭| × 거래량 (낙폭 크고 유동성 높은 코인 우선)
        """
        blocked = blocked or set()
        try:
            tickers = self.api.get_all_tickers()
        except Exception as e:
            logger.warning(f"[급락스캔] 티커 조회 실패: {e}")
            return []

        candidates = []
        for t in tickers:
            symbol = t.get("symbol", "")
            if not symbol.endswith("-USDT"):
                continue
            if symbol in EXCLUDE_LARGE_CAPS:
                continue
            if symbol in BLACKLIST:
                continue
            if any(kw in symbol for kw in EXCLUDE_KEYWORDS):
                continue
            if symbol in blocked:
                continue

            volume_usdt = float(t.get("quoteVolume", 0))
            if volume_usdt < MIN_VOLUME_USDT:
                continue

            change_24h = float(t.get("priceChangePercent", 0)) / 100
            if change_24h >= -0.01:   # 1% 미만 하락이면 제외
                continue

            price = float(t.get("lastPrice", 0))
            if price <= 0:
                continue

            # 낙폭 × 거래량 기반 점수 (가장 강하게 떨어지는 코인 우선)
            score = abs(change_24h) * (volume_usdt / 1e8)
            candidates.append({
                "symbol":    symbol,
                "trend":     "DOWN",
                "change_24h": change_24h,
                "volume":    volume_usdt,
                "price":     price,
                "score":     score,
            })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        top = candidates[:5]

        logger.info(f"[급락스캔] {len(candidates)}개 하락 코인 → 상위 {len(top)}개")
        for c in top:
            logger.info(
                f"  {c['symbol']:22s} | 24h {c['change_24h']:+.2%} | "
                f"거래량 ${c['volume']/1e6:.0f}M | 점수 {c['score']:.4f}"
            )
        return top
