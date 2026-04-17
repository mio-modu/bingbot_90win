"""
코인 선정 모듈
────────────────
선정 기준:
  1. 거래량   : 24h $50M 이상 (유동성)
  2. 변동성   : 일봉 ATR/가격 3~12% 사이 (물타기 작동에 필요한 변동성)
  3. 방향성   : 20일 MA 기울기로 UP/DOWN 트렌드 확인
  4. 트렌드 강도: 단기(4h) + 중기(일봉) MA가 같은 방향으로 정렬
  5. 과열 제외: 24h 변동폭 ±15% 초과 코인 제외 (급등/급락 직후)
  6. 현재 변동성: 1h 캔들 기준 최소 0.8% + 15m 캔들 기준 최소 0.4%
               → 지금 이 순간 실제로 움직이는 코인만 선택
  7. 최소 점수: 기준 미달 시 전부 제외 → 다음 스캔까지 대기

최종 점수 = 트렌드강도 × 변동성 × 모멘텀 보정 × 현재변동성 보정
"""

import logging
import numpy as np
from bingx_api import BingXAPI
from config import MIN_VOLUME_USDT, MAX_VOLUME_USDT, TOP_N_COINS, MA_PERIOD, ADX_MIN_THRESHOLD

logger = logging.getLogger(__name__)

# ── 변동성 필터 범위 ──────────────────────────────────────
# ATR/가격 비율: 일봉 기준 평균 (고가-저가)/종가
# 물타기가 작동하려면 코인이 하루에 최소 ±3% 이상 움직여야 함
VOLATILITY_MIN = 0.030   # ATR/가격 최소 3.0% (코인 일 1% 이상 움직이는 종목만)
VOLATILITY_MAX = 0.12    # ATR/가격 최대 12% (너무 극단적인 코인 제외)

# ── 현재 변동성 필터 ──────────────────────────────────────
# 1h 캔들 기준 (고가-저가)/종가 평균 → 최근 6시간 활성도
RECENT_VOL_MIN_1H = 0.008  # 최근 1h 평균 변동폭 최소 0.8%
# 15m 캔들 기준 (고가-저가)/종가 평균 → 지금 이 순간 활성도
RECENT_VOL_MIN_15M = 0.004 # 최근 15m 평균 변동폭 최소 0.4% (횡보 즉시 차단)

# ── 최소 점수 기준 ────────────────────────────────────────
MIN_SCORE = 0.01  # ADX·일관성 보너스 추가로 점수 스케일 낮아짐 → 기준 하향 (0.05→0.01)

# ── 과열 필터 ─────────────────────────────────────────────
MAX_24H_CHANGE = 0.15    # 24h 등락률 ±15% 초과 시 제외

# ── 제외 키워드 (지수·원자재 추종 상품) ─────────────────
EXCLUDE_KEYWORDS = ["GOLD", "NASDAQ", "NCC", "USD2USD", "SP500", "OIL"]

# ── 대형 코인 직접 제외 (유통량 과다 → 움직임 둔함) ───────
EXCLUDE_LARGE_CAPS = {
    "BTC-USDT", "ETH-USDT", "BNB-USDT", "XRP-USDT",
    "SOL-USDT", "ADA-USDT", "DOGE-USDT", "TRX-USDT",
    "LINK-USDT", "AVAX-USDT", "TON-USDT", "SHIB-USDT",
    "DOT-USDT", "MATIC-USDT", "LTC-USDT", "BCH-USDT",
    "1000PEPE-USDT", "1000SHIB-USDT",
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

    def wilder(data: list, n: int) -> list:
        """Wilder 평활화 (Wilder's Smoothing)"""
        s = [sum(data[:n])]
        for v in data[n:]:
            s.append(s[-1] - s[-1] / n + v)
        return s

    atr_s = wilder(tr_list, period)
    pdm_s = wilder(plus_dm, period)
    mdm_s = wilder(minus_dm, period)

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

    adx_s = wilder(dx_list, period)
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

        for t in tickers:
            symbol = t.get("symbol", "")
            if not symbol.endswith("-USDT"):
                continue

            # ── 1. 거래량 필터 (최소~최대 범위) ──────────
            # 너무 작으면 유동성 부족, 너무 크면 움직임 둔함
            volume_usdt = float(t.get("quoteVolume", 0))
            if volume_usdt < MIN_VOLUME_USDT:
                continue
            if volume_usdt > MAX_VOLUME_USDT:
                logger.debug(f"{symbol} 거래량 초과 제외: ${volume_usdt/1e6:.0f}M")
                continue

            # ── 대형 코인 제외 (유통량 과다 → 둔한 움직임) ─
            if symbol in EXCLUDE_LARGE_CAPS:
                continue

            # ── 지수·원자재 추종 상품 제외 ───────────────
            if any(kw in symbol for kw in EXCLUDE_KEYWORDS):
                continue

            # ── 5. 과열 필터 (24h 등락률) ───────────────
            change_24h = float(t.get("priceChangePercent", 0)) / 100
            if abs(change_24h) > MAX_24H_CHANGE:
                logger.debug(f"{symbol} 과열 제외: 24h {change_24h:.1%}")
                continue

            try:
                # 일봉 캔들
                daily = self.api.get_klines(symbol, "1d", limit=max(MA_PERIOD + 5, 35))
                if len(daily) < MA_PERIOD + 1:
                    continue

                closes_d = [_kline_val(k, "close", 4) for k in daily]

                # ── 2. 변동성 필터 (ATR 기반) ────────────
                atr_ratio = calc_atr_ratio(daily)
                if atr_ratio < VOLATILITY_MIN or atr_ratio > VOLATILITY_MAX:
                    logger.debug(
                        f"{symbol} 변동성 범위 외: ATR비율 {atr_ratio:.3f}"
                    )
                    continue

                # ── 3. 방향성 (20일 MA 기울기) ───────────
                slope_d = calc_ma_slope(closes_d, MA_PERIOD)
                trend   = get_trend(slope_d)
                if trend == "SIDEWAYS":
                    continue

                # ── 3.5. ADX 추세 강도 계산 (하드필터 아님 → 점수 보너스로만 사용)
                # 하드필터로 쓰면 시장 횡보 시 진입 가능 코인 0개 될 수 있음
                adx = calc_adx(daily)

                # ── 4. 단기 트렌드 정렬 (4h + 1h) ──────────
                slope_4h = slope_1h = 0.0
                trend_4h = trend_1h = "SIDEWAYS"
                try:
                    hourly4   = self.api.get_klines(symbol, "4h", limit=20)
                    closes_4h = [_kline_val(k, "close", 4) for k in hourly4]
                    if len(closes_4h) >= 10:
                        slope_4h = calc_ma_slope(closes_4h, 10)
                        trend_4h = get_trend(slope_4h)
                except Exception:
                    pass

                hourly1 = []
                try:
                    hourly1   = self.api.get_klines(symbol, "1h", limit=12)
                    closes_1h = [_kline_val(k, "close", 4) for k in hourly1]
                    if len(closes_1h) >= 6:
                        slope_1h = calc_ma_slope(closes_1h, 6)
                        trend_1h = get_trend(slope_1h)
                except Exception:
                    pass

                # ── 현재 변동성 체크 1: 최근 6개 1h 캔들 ────
                recent_vol_1h = 0.0
                if len(hourly1) >= 6:
                    recent_ranges = []
                    for k in hourly1[-6:]:
                        h = _kline_val(k, "high",  2)
                        l = _kline_val(k, "low",   3)
                        c = _kline_val(k, "close", 4)
                        if c > 0:
                            recent_ranges.append((h - l) / c)
                    if recent_ranges:
                        recent_vol_1h = float(np.mean(recent_ranges))
                        if recent_vol_1h < RECENT_VOL_MIN_1H:
                            logger.debug(
                                f"{symbol} 1h 횡보 제외: {recent_vol_1h:.4f} < {RECENT_VOL_MIN_1H}"
                            )
                            continue

                # ── 현재 변동성 체크 2: 최근 8개 15m 캔들 (지금 이 순간) ──
                try:
                    m15 = self.api.get_klines(symbol, "15m", limit=8)
                    if len(m15) >= 6:
                        ranges_15m = []
                        for k in m15[-6:]:
                            h = _kline_val(k, "high",  2)
                            l = _kline_val(k, "low",   3)
                            c = _kline_val(k, "close", 4)
                            if c > 0:
                                ranges_15m.append((h - l) / c)
                        if ranges_15m:
                            recent_vol_15m = float(np.mean(ranges_15m))
                            if recent_vol_15m < RECENT_VOL_MIN_15M:
                                logger.debug(
                                    f"{symbol} 15m 횡보 제외: {recent_vol_15m:.4f} < {RECENT_VOL_MIN_15M}"
                                )
                                continue
                except Exception:
                    pass

                # 일봉·4h·1h 방향이 모두 일치하면 최고 점수
                matches = sum([
                    trend_4h == trend,
                    trend_1h == trend,
                ])
                align_bonus = {0: 0.4, 1: 0.9, 2: 1.8}[matches]

                # 1h 역행이면 지금 이 흐름 아님 → 제외
                if trend_1h != "SIDEWAYS" and trend_1h != trend:
                    logger.debug(f"{symbol} 1h 역행 제외: 일봉={trend} 1h={trend_1h}")
                    continue

                # ── 6. 모멘텀 (현재가 위치) ──────────────
                price   = float(t.get("lastPrice", 0))
                ma20_d  = calc_ma(closes_d, 20)
                momentum_ok = (
                    (trend == "UP"   and price >= ma20_d) or
                    (trend == "DOWN" and price <= ma20_d)
                )
                momentum_bonus = 1.3 if momentum_ok else 0.7

                # ── 추세 일관성 (최근 10일봉 중 트렌드 방향 비율) ──
                consistency      = calc_trend_consistency(closes_d, trend, period=10)
                # 0.5(50%)~1.5(100%) 범위 보정: 70% 이상이면 보너스
                consistency_bonus = 0.5 + consistency

                # ── ADX 보너스: ADX=25 → ×1.0, ADX=50 → ×2.0 ──
                adx_bonus = min(adx / 25.0, 2.0)

                # ── 점수 계산 ─────────────────────────────
                # 변동성 최우선: ATR 6% 근처 최고점
                vol_score    = atr_ratio * (1 - abs(atr_ratio - 0.06) / 0.10)
                trend_score  = abs(slope_d) + abs(slope_4h) * 0.5 + abs(slope_1h) * 1.0
                # 현재 변동성이 높을수록 보정 가중치 증가 (지금 움직이는 코인 우선)
                live_bonus   = 1.0 + min(recent_vol_1h / RECENT_VOL_MIN_1H, 3.0)
                score        = (vol_score * trend_score * align_bonus * momentum_bonus
                                * live_bonus * adx_bonus * consistency_bonus * 1000)

                candidates.append({
                    "symbol":        symbol,
                    "trend":         trend,
                    "trend_4h":      trend_4h,
                    "trend_1h":      trend_1h,
                    "slope_d":       slope_d,
                    "slope_4h":      slope_4h,
                    "slope_1h":      slope_1h,
                    "atr_ratio":     atr_ratio,
                    "recent_vol_1h": round(recent_vol_1h, 4),
                    "change_24h":    change_24h,
                    "momentum_ok":   momentum_ok,
                    "adx":           adx,
                    "consistency":   round(consistency, 2),
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
            logger.info(
                f"  {c['symbol']:22s} | {c['trend']:4s} | "
                f"4h:{_arrow(c['trend_4h'], c['trend'])} "
                f"1h:{_arrow(c['trend_1h'], c['trend'])} | "
                f"ATR {c['atr_ratio']:.3f} | ADX {c['adx']:.1f} | "
                f"일관성 {c['consistency']:.0%} | "
                f"1h변동 {c['recent_vol_1h']:.4f} | "
                f"24h {c['change_24h']:+.1%} | 모멘텀{mo} | 점수 {c['score']:.4f}"
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
