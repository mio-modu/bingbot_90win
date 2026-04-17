"""
포지션 상태 관리 모듈
- 물타기 단계 추적
- 평균 진입가 계산
- 현재 손익률 계산
"""

import logging
from dataclasses import dataclass
from config import (
    INITIAL_POSITION_USD, LEVERAGE,
    AVG_DOWN_STEP1_TRIGGER, AVG_DOWN_STEP2_TRIGGER,
    AVG_DOWN_STEP3_TRIGGER, AVG_DOWN_STEP4_TRIGGER,
    MAX_TOTAL_POSITION, TAKE_PROFIT_PCT
)

logger = logging.getLogger(__name__)


@dataclass
class PositionState:
    symbol:          str   = ""
    trend:           str   = ""          # UP / DOWN
    avg_entry_price: float = 0.0
    total_invested:  float = 0.0         # 실제 투입 USD
    total_quantity:  float = 0.0         # 보유 수량
    avg_down_step:   int   = 0           # 0=최초진입, 1,2,3,4=물타기단계
    step_ref_pnl:    float = 0.0         # 각 단계 시작 시 포지션 손익률 기준값
    hard_cap_price:  float = 0.0         # 하드캡 도달 시점의 코인 가격
    is_open:         bool  = False
    realized_pnl:    float = 0.0         # 이번 세션 누적 실현손익


class PositionManager:
    def __init__(self):
        self.state = PositionState()

    def reset(self):
        pnl = self.state.realized_pnl
        self.state = PositionState()
        self.state.realized_pnl = pnl

    # ────────────────────────────────────────────────
    #  포지션 오픈 기록
    # ────────────────────────────────────────────────
    def record_open(self, symbol: str, trend: str, price: float, invested: float = INITIAL_POSITION_USD):
        qty = (invested * LEVERAGE) / price
        self.state.symbol          = symbol
        self.state.trend           = trend
        self.state.avg_entry_price = price
        self.state.total_invested  = invested
        self.state.total_quantity  = qty
        self.state.avg_down_step   = 0
        self.state.step_ref_pnl    = 0.0
        self.state.hard_cap_price  = 0.0
        self.state.is_open         = True
        logger.info(
            f"[진입] {symbol} | 가격: {price:.6f} | 투입: ${invested:.2f} | "
            f"수량: {qty:.6f} | 트렌드: {trend}"
        )

    # ────────────────────────────────────────────────
    #  물타기 기록
    # ────────────────────────────────────────────────
    def record_avg_down(self, price: float, add_invested: float):
        add_qty = (add_invested * LEVERAGE) / price
        prev_total_cost = self.state.avg_entry_price * self.state.total_quantity
        new_total_cost  = prev_total_cost + add_qty * price

        self.state.total_quantity += add_qty
        self.state.total_invested += add_invested
        self.state.avg_entry_price = new_total_cost / self.state.total_quantity
        self.state.avg_down_step  += 1
        self.state.step_ref_pnl    = self.get_pnl_pct(price)

        if self.state.total_invested >= MAX_TOTAL_POSITION:
            self.state.hard_cap_price = price

        logger.info(
            f"[물타기 {self.state.avg_down_step}단계] 가격: {price:.6f} | "
            f"추가투입: ${add_invested:.2f} | 총투입: ${self.state.total_invested:.2f} | "
            f"평균단가: {self.state.avg_entry_price:.6f}"
        )

    # ────────────────────────────────────────────────
    #  손익률 계산
    # ────────────────────────────────────────────────
    def get_pnl_pct(self, current_price: float) -> float:
        """포지션 전체 손익률 (레버리지 반영)"""
        if self.state.avg_entry_price == 0:
            return 0.0
        if self.state.trend == "UP":
            coin_chg = (current_price - self.state.avg_entry_price) / self.state.avg_entry_price
        else:
            coin_chg = (self.state.avg_entry_price - current_price) / self.state.avg_entry_price
        return coin_chg * LEVERAGE

    def get_pnl_usd(self, current_price: float) -> float:
        """실현 전 손익 (USD)"""
        return self.state.total_invested * self.get_pnl_pct(current_price)

    # ────────────────────────────────────────────────
    #  다음 물타기 금액 계산
    # ────────────────────────────────────────────────
    def next_avg_down_amount(self) -> float:
        """
        물타기 금액 (직전 누적 투입액만큼 추가)
        step0 진입: $40
        step1:      $40  → 누적 $80
        step2:      $80  → 누적 $160
        step3:      $160 → 누적 $320
        step4:      $320 → 누적 $640 (하드캡)
        """
        step = self.state.avg_down_step
        base = INITIAL_POSITION_USD  # $40
        amounts = {0: base, 1: base * 2, 2: base * 4, 3: base * 8}
        return amounts.get(step, 0.0)

    # ────────────────────────────────────────────────
    #  물타기 트리거 체크
    # ────────────────────────────────────────────────
    def should_avg_down(self, current_price: float) -> bool:
        """현재 단계에서 물타기 진입 조건 충족 여부"""
        if not self.state.is_open:
            return False
        if self.state.total_invested >= MAX_TOTAL_POSITION:
            return False
        if self.state.avg_down_step >= 4:
            return False

        step = self.state.avg_down_step
        current_pnl = self.get_pnl_pct(current_price)
        delta = current_pnl - self.state.step_ref_pnl

        triggers = {
            0: AVG_DOWN_STEP1_TRIGGER,  # -10%
            1: AVG_DOWN_STEP2_TRIGGER,  # 추가 -10%
            2: AVG_DOWN_STEP3_TRIGGER,  # 추가 -20%
            3: AVG_DOWN_STEP4_TRIGGER,  # 추가 -20%
        }
        threshold = triggers.get(step, 0)
        return delta <= threshold

    # ────────────────────────────────────────────────
    #  익절 트리거 체크
    # ────────────────────────────────────────────────
    def should_take_profit(self, current_price: float) -> bool:
        if not self.state.is_open:
            return False
        return self.get_pnl_pct(current_price) >= TAKE_PROFIT_PCT

    # ────────────────────────────────────────────────
    #  익절 기록
    # ────────────────────────────────────────────────
    def record_close(self, current_price: float):
        pnl = self.get_pnl_usd(current_price)
        self.state.realized_pnl += pnl
        logger.info(
            f"[청산] {self.state.symbol} | 청산가: {current_price:.6f} | "
            f"손익: ${pnl:+.2f} | 누적손익: ${self.state.realized_pnl:+.2f}"
        )
        self.reset()
