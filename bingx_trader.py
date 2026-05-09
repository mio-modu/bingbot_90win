"""
BingX 실물 거래 엔진
PaperTrader 전략 로직을 그대로 유지하면서 BingX API로 실제 주문 실행
"""

import logging
import config
from paper_trader import PaperTrader, Position
from bingx_api import BingXAPI

logger = logging.getLogger(__name__)

def apply_capital_to_config(api_balance: float) -> float:
    """
    실제 잔고 기준으로 config 모듈 속성을 재계산.
    모든 값은 config.SEED_RATIO(=6%)를 기준으로 비례 계산됨.
    봇 재시작 없이 입금 즉시 반영.
    반환: 재계산된 초기 시드 (USD)
    """
    sr = config.SEED_RATIO
    new_seed = max(1.0, round(api_balance * sr * 2) / 2)   # $0.5 단위
    config.INITIAL_POSITION_USD       = new_seed
    config.MAX_TOTAL_POSITION         = round(api_balance * 0.50,      1)
    config.MAX_NET_LOSS_USD           = round(-api_balance * 0.23,     1)
    config.DCA_STEP4_NET_LOSS_TRIGGER = round(-api_balance * sr * 1.5, 1)
    config.DCA_STEP5_NET_LOSS_TRIGGER = round(-api_balance * sr * 3.5, 1)
    config.MIN_PROFIT_USD             = round( api_balance * sr * 0.075, 2)
    config.SIDEWAYS_DCA_MIN_NET       = round( api_balance * sr * 0.075, 2)
    config.UPGRADE_MAX_LOSS_USD       = round(-api_balance * sr * 0.075, 2)
    return new_seed


class BingxTrader(PaperTrader):
    def __init__(self, api: BingXAPI):
        super().__init__()
        self.api = api
        # 재시작 시: state.json에 저장된 total_capital 기준으로 config 재적용
        effective = self.total_capital + self.total_pnl
        if effective > 0:
            seed = apply_capital_to_config(effective)
            logger.info(
                f"[시작] 자본 ${effective:.2f} | 시드 ${seed:.2f} | "
                f"하드캡 ${config.MAX_TOTAL_POSITION:.1f} | 최대손실 ${config.MAX_NET_LOSS_USD:.1f}"
            )

    # ────────────────────────────────────────────────
    #  내부: USDT 마진 → 코인 수량 변환
    # ────────────────────────────────────────────────
    def _to_qty(self, symbol: str, usdt: float, price: float) -> float:
        """USDT 마진 × 레버리지 / 현재가 = 코인 수량"""
        qty = (usdt * config.LEVERAGE) / price
        # BingX 소수점 정밀도 맞추기 (6자리)
        return round(qty, 6)

    # ────────────────────────────────────────────────
    #  내부: 실제 주문 실행 헬퍼
    # ────────────────────────────────────────────────
    @staticmethod
    def _fmt(result: dict) -> str:
        order = result.get("data", {}).get("order", {})
        if not order:
            code = result.get("code", "?")
            msg  = result.get("msg", "")
            return f"ERR {code} {msg}"
        status = order.get("status", "?")
        avg    = order.get("avgPrice", "?")
        qty    = order.get("executedQty", "?")
        return f"{status} | 체결가 {avg} | {qty}개"

    def _real_open(self, symbol: str, trend: str, usdt: float) -> bool:
        side     = "BUY"  if trend == "UP" else "SELL"
        pos_side = "LONG" if trend == "UP" else "SHORT"
        try:
            self.api.set_leverage(symbol, config.LEVERAGE, pos_side)
            price  = self.api.get_price(symbol)
            qty    = self._to_qty(symbol, usdt, price)
            result = self.api.place_order(symbol, side, pos_side, qty)
            logger.info(f"[실주문-진입] {symbol} {side} ${usdt:.2f} | {self._fmt(result)}")
            return True
        except Exception as e:
            logger.error(f"[실주문-진입실패] {symbol}: {e}")
            return False

    def _real_add(self, symbol: str, trend: str, usdt: float) -> bool:
        side     = "BUY"  if trend == "UP" else "SELL"
        pos_side = "LONG" if trend == "UP" else "SHORT"
        try:
            price  = self.api.get_price(symbol)
            qty    = self._to_qty(symbol, usdt, price)
            result = self.api.place_order(symbol, side, pos_side, qty)
            logger.info(f"[실주문-추가] {symbol} {side} ${usdt:.2f} | {self._fmt(result)}")
            return True
        except Exception as e:
            logger.error(f"[실주문-추가실패] {symbol}: {e}")
            return False

    def _real_close(self, symbol: str, trend: str) -> bool:
        pos_side = "LONG" if trend == "UP" else "SHORT"
        try:
            result = self.api.close_position(symbol, pos_side)
            logger.info(f"[실주문-청산] {symbol} {pos_side} | {self._fmt(result)}")
            return True
        except Exception as e:
            logger.error(f"[실주문-청산실패] {symbol}: {e}")
            return False

    # ────────────────────────────────────────────────
    #  오버라이드: 진입
    # ────────────────────────────────────────────────
    def open_position(self, symbol: str, trend: str, raw_price: float,
                      invest_override: float = None,
                      crash_short: bool = False) -> Position:
        # 1) 시뮬레이션 상태 업데이트
        pos = super().open_position(symbol, trend, raw_price,
                                    invest_override, crash_short)
        # 2) 실제 주문
        ok = self._real_open(symbol, trend, pos.initial_invest)
        if not ok:
            logger.error(f"[경고] {symbol} 실제 진입 실패 — 시뮬레이션만 진행")
        return pos

    # ────────────────────────────────────────────────
    #  오버라이드: 청산
    # ────────────────────────────────────────────────
    def close_position(self, raw_price: float, reason: str) -> dict:
        p = self.position
        if not p:
            return {}
        # 1) 실제 청산 먼저
        self._real_close(p.symbol, p.trend)
        # 2) 시뮬레이션 상태 업데이트
        return super().close_position(raw_price, reason)

    # ────────────────────────────────────────────────
    #  오버라이드: 물타기 (가격 기반)
    # ────────────────────────────────────────────────
    def execute_avg_down(self, price: float):
        p = self.position
        if not p:
            return
        add_usd = p.next_avg_down_amount()
        add_usd = min(add_usd, p.max_position - p.total_invested)
        if add_usd <= 0:
            return

        ok = self._real_add(p.symbol, p.trend, add_usd)
        if not ok:
            logger.error(f"[경고] {p.symbol} DCA {p.avg_down_step+1}단계 실패 — 건너뜀")
            return

        p.apply_avg_down(price, add_usd)
        self.save_state()

    # ────────────────────────────────────────────────
    #  오버라이드: 횡보 DCA
    # ────────────────────────────────────────────────
    def execute_sideways_avg_down(self, price: float):
        p = self.position
        if not p:
            return
        add_usd = p.next_avg_down_amount()
        add_usd = min(add_usd, p.max_position - p.total_invested)
        if add_usd <= 0:
            return

        ok = self._real_add(p.symbol, p.trend, add_usd)
        if not ok:
            logger.error(f"[경고] {p.symbol} 횡보DCA 실패 — 건너뜀")
            return

        p.apply_avg_down(price, add_usd)
        p.sideways_dca = True
        self.save_state()

    # ────────────────────────────────────────────────
    #  자본 동기화: 청산 후 BingX 실제 잔고 반영
    # ────────────────────────────────────────────────
    def sync_capital_from_api(self):
        """
        청산 직후 BingX 실제 잔고를 조회해 총자본과 설정값을 갱신.
        입금이 있었다면 자동으로 시드·손절·하드캡 등이 비례 재계산됨.
        """
        try:
            api_balance = self.api.get_balance()
            if api_balance <= 0:
                logger.warning("[자본동기화] BingX 잔고 조회 실패 (잔고=0)")
                return

            prev_effective = self.total_capital + self.total_pnl
            prev_seed      = self._get_initial_position_usd()
            prev_hardcap   = config.MAX_TOTAL_POSITION

            # total_capital 재설정 (total_pnl 누적 기록 유지)
            # total_capital + total_pnl = api_balance 가 되도록 조정
            self.total_capital = api_balance - self.total_pnl

            # config 모듈 속성 재계산
            new_seed = apply_capital_to_config(api_balance)

            deposit = api_balance - prev_effective
            deposit_str = (f"  ★입금감지 +${deposit:.2f}" if deposit > 0.5
                           else f"  손익반영 {deposit:+.2f}")

            logger.info(
                f"[자본동기화] BingX잔고 ${api_balance:.2f} (이전 ${prev_effective:.2f}){deposit_str} | "
                f"시드 ${prev_seed:.2f} → ${new_seed:.2f} | "
                f"하드캡 ${prev_hardcap:.1f} → ${config.MAX_TOTAL_POSITION:.1f} | "
                f"최대손실 ${config.MAX_NET_LOSS_USD:.1f} | "
                f"최소수익 ${config.MIN_PROFIT_USD:.2f}"
            )
            self.save_state()

        except Exception as e:
            logger.warning(f"[자본동기화] 실패: {e}")
