"""
손절 & 코인 교체 감지 모듈
1. 급락 감지    : 30분 내 -5% + 거래량 200%
2. 하드캡 손절  : $400 투입 후 코인 추가 -5%
3. 코인 교체    : 20일선 반전 + 거래량 50% 이하 1.5시간
"""

import logging
import time
import numpy as np
from collections import deque
from bingx_api import BingXAPI
from config import (
    FLASH_CRASH_COIN_PCT, FLASH_CRASH_VOL_MULT, FLASH_CRASH_WINDOW_MIN,
    HARD_CAP_STOP_COIN_PCT, MAX_TOTAL_POSITION,
    LOW_VOLUME_THRESHOLD, LOW_VOLUME_DURATION_MIN, MA_PERIOD
)

logger = logging.getLogger(__name__)


class StopLossEngine:
    def __init__(self, api: BingXAPI):
        self.api = api
        # 급락 감지용 가격 히스토리: (timestamp_sec, price, volume)
        self._price_hist: deque = deque(maxlen=200)
        self._low_vol_start: float | None = None   # 저거래량 시작 시각

    def update_price(self, price: float, volume: float):
        """매 루프마다 가격/거래량 기록 (급락 감지용)"""
        self._price_hist.append((time.time(), price, volume))

    # ────────────────────────────────────────────────
    #  1. 급락 감지
    # ────────────────────────────────────────────────
    def is_flash_crash(self) -> bool:
        """
        최근 FLASH_CRASH_WINDOW_MIN 분 내에
        - 코인 가격 -FLASH_CRASH_COIN_PCT 이상 하락
        - 거래량이 평균 대비 FLASH_CRASH_VOL_MULT 이상
        두 조건 동시 충족 시 True
        """
        if len(self._price_hist) < 2:
            return False

        now = time.time()
        window_sec = FLASH_CRASH_WINDOW_MIN * 60
        recent = [(t, p, v) for t, p, v in self._price_hist if now - t <= window_sec]

        if len(recent) < 2:
            return False

        prices = [p for _, p, _ in recent]
        volumes = [v for _, _, v in recent]

        price_change = (prices[-1] - prices[0]) / prices[0]
        avg_volume   = np.mean(volumes)
        last_volume  = volumes[-1]

        crash = price_change <= FLASH_CRASH_COIN_PCT
        vol_spike = (avg_volume > 0) and (last_volume >= avg_volume * FLASH_CRASH_VOL_MULT)

        if crash and vol_spike:
            logger.warning(
                f"[급락 감지] 가격변화: {price_change:.2%} | "
                f"거래량 배율: {last_volume / avg_volume:.1f}x"
            )
            return True
        return False

    # ────────────────────────────────────────────────
    #  2. 하드캡 손절
    # ────────────────────────────────────────────────
    def is_hard_cap_stop(self, position_state) -> bool:
        """
        하드캡($400) 도달 후 코인이 추가로 -5% 하락 시 True
        """
        if position_state.total_invested < MAX_TOTAL_POSITION:
            return False
        if position_state.hard_cap_price == 0:
            return False

        from bingx_api import BingXAPI
        current_price = self.api.get_price(position_state.symbol)
        ref_price     = position_state.hard_cap_price

        if position_state.trend == "UP":
            change = (current_price - ref_price) / ref_price
        else:
            change = (ref_price - current_price) / ref_price

        if change <= HARD_CAP_STOP_COIN_PCT:
            logger.warning(
                f"[하드캡 손절] 기준가: {ref_price:.6f} | "
                f"현재가: {current_price:.6f} | 변화율: {change:.2%}"
            )
            return True
        return False

    # ────────────────────────────────────────────────
    #  3. 코인 교체 감지
    # ────────────────────────────────────────────────
    def check_trend_reversal(self, symbol: str, trend: str) -> bool:
        """
        20일선 반전 여부 확인
        """
        try:
            daily = self.api.get_klines(symbol, "1d", limit=MA_PERIOD + 5)
            if len(daily) < MA_PERIOD + 1:
                return False

            closes = [float(k[4]) for k in daily]
            arr    = np.array(closes, dtype=float)
            ma_old = float(np.mean(arr[-(MA_PERIOD + 1):-1]))
            ma_now = float(np.mean(arr[-MA_PERIOD:]))
            slope  = (ma_now - ma_old) / ma_old

            if trend == "UP" and slope < -0.001:
                logger.info(f"[트렌드 반전] {symbol} 상승→하락 전환 감지")
                return True
            if trend == "DOWN" and slope > 0.001:
                logger.info(f"[트렌드 반전] {symbol} 하락→상승 전환 감지")
                return True
        except Exception as e:
            logger.debug(f"트렌드 반전 체크 실패: {e}")
        return False

    def check_volume_dryup(self, symbol: str) -> bool:
        """
        1시간봉 거래량이 평균 50% 이하로 1.5시간(90분) 지속 시 True
        """
        try:
            # 최근 30개 1시간봉
            hourly = self.api.get_klines(symbol, "1h", limit=30)
            if len(hourly) < 10:
                return False

            volumes   = [float(k[5]) for k in hourly]
            avg_vol   = float(np.mean(volumes[:-3]))   # 최근 3개 제외한 평균
            recent_vols = volumes[-2:]                  # 최근 2개봉 (~2시간)

            low = all(v <= avg_vol * LOW_VOLUME_THRESHOLD for v in recent_vols)

            if low:
                if self._low_vol_start is None:
                    self._low_vol_start = time.time()
                elapsed = (time.time() - self._low_vol_start) / 60
                if elapsed >= LOW_VOLUME_DURATION_MIN:
                    logger.info(
                        f"[거래량 고갈] {symbol} | "
                        f"평균 대비 {[v/avg_vol for v in recent_vols]} | "
                        f"{elapsed:.0f}분 지속"
                    )
                    return True
            else:
                self._low_vol_start = None
        except Exception as e:
            logger.debug(f"거래량 체크 실패: {e}")
        return False

    def should_replace_coin(self, symbol: str, trend: str) -> bool:
        """코인 교체 트리거: 트렌드 반전 AND 거래량 고갈"""
        reversal   = self.check_trend_reversal(symbol, trend)
        volume_dry = self.check_volume_dryup(symbol)
        if reversal and volume_dry:
            logger.warning(f"[코인 교체] {symbol} 교체 신호 발생")
            return True
        return False
