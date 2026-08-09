"""
페이퍼 트레이딩 엔진
────────────────────
실제 주문 없이 로컬에서 전략을 시뮬레이션.
손익 및 포지션 상태는 state.json 에 저장/복원.

전략 요약:
  진입        : $40, 레버리지 5x  (슬리피지 0.05% 반영)
  익절 조건   : ① 포지션 +1% 이상  AND  ② 순수익 $1 이상
  트레일링    : 포지션 +3% 도달 시 활성화 → 코인 1.2% 되돌리면 청산
  물타기 1    : 포지션 -10% → +$40  (누적 $80)
  물타기 2    : 추가  -10% → +$80  (누적 $160)
  물타기 3    : 추가  -20% → +$160 (누적 $320)
  물타기 4    : 추가  -20% → +$320 (누적 $640 하드캡)
  하드캡 손절 : 하드캡 도달 후 코인 추가 -5% → 전량 청산
  급락 손절   : 30분 내 코인 -5% + 거래량 200% → 즉시 청산
  코인 교체   : 20일선 반전 + 거래량 50% 이하 1.5시간 → 재스캔
  수수료      : 0.05% per side (진입·청산 각각)
  슬리피지    : 0.05% per side (불리한 방향 체결 시뮬레이션)
"""

from __future__ import annotations

import json
import math
import os
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np

import config
import trade_journal

logger = logging.getLogger(__name__)

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")


# ────────────────────────────────────────────────────────────
#  포지션 데이터
# ────────────────────────────────────────────────────────────

@dataclass
class Position:
    symbol:          str
    trend:           str    # UP / DOWN
    entry_price:     float  # 슬리피지 적용된 실제 체결가
    avg_price:       float  # 평균 진입가 (물타기 반영, 슬리피지 포함)
    total_invested:  float  # 총 투입 USD (마진)
    total_qty:       float  # 총 보유 수량
    avg_down_step:   int    # 현재 물타기 단계 (0=최초)
    step_ref_pnl:    float  # 각 단계 시작 시 포지션 손익률 기준값
    hard_cap_price:  float  # 하드캡 도달 시점의 코인 가격
    open_time:       float  = field(default_factory=time.time)

    # 트레일링 상태
    peak_price:      float  = 0.0    # 롱: 최고가 / 숏: 최저가
    trail_active:    bool   = False
    trail_sl:        float  = 0.0    # 현재 트레일링 손절가

    # 진입 시 기준 시드 (DCA 배율·하드캡 계산용)
    initial_invest:  float  = 0.0

    # 횡보 DCA 관련
    sideways_dca:    bool   = False  # 횡보 DCA 강제 투입 여부 (TP·코인차단 분기용)
    step_enter_time: float  = 0.0    # 현재 단계 진입 시각 (횡보 DCA 타이머 기준)

    # 급락 SHORT 모드
    crash_short:     bool   = False  # BTC 충격 시 역방향 단타 여부

    # 불타기 (트레일 활성화 시 1회 추가 진입)
    pyramid_done:     bool  = False  # 불타기 완료 여부 (1회만)
    pyramid_qty:      float = 0.0    # 추가 진입 수량
    pyramid_invested: float = 0.0    # 추가 투입금액

    # 진입 시 확정 자본 (하드캡 상한을 자본 이내로 제한)
    capital_at_open:  float = 0.0

    # 급락 감지용 가격 히스토리: [timestamp, price, volume]
    price_hist:      list   = field(default_factory=list)
    low_vol_start:   float  = 0.0

    # ── 저널용 계측 (매매 판단에는 일절 관여하지 않음) ──────────
    # MFE = 보유 중 최대 유리 지점 / MAE = 최대 불리 지점 (순손익 $ 기준)
    # 손실 거래의 MFE → "익절할 수 있었는데 놓친 폭"
    # 익절 거래의 MAE → "손절선을 조이면 죽었을 거래"
    mfe_usd:         float  = 0.0    # 최대 유리 순손익 ($)
    mae_usd:         float  = 0.0    # 최대 불리 순손익 ($, 음수)
    mfe_coin_pct:    float  = 0.0    # 그때의 코인 변화율
    mae_coin_pct:    float  = 0.0
    mfe_at_s:        float  = 0.0    # 진입 후 몇 초 만에 MFE 도달했는지
    mae_at_s:        float  = 0.0
    # 수익 보존 락(profit lock)용 — 청산비용까지 뺀 실현 기준 최고 순수익
    peak_realized:   float  = 0.0
    # DCA 단계 이력: [{step, at_s, price, add_usd, total_invested, kind}]
    step_history:    list   = field(default_factory=list)

    # 거래소 강제 손절 주문 (봇이 죽어도 거래소가 집행하는 백스톱)
    stop_order_id:   str    = ""
    stop_price:      float  = 0.0

    @property
    def max_position(self) -> float:
        """하드캡: 진입 시 시드 × 20, 단 실제 보유 자본을 초과하지 않음"""
        base     = self.initial_invest if self.initial_invest > 0 else config.INITIAL_POSITION_USD
        hard_cap = base * 20
        if self.capital_at_open > 0:
            return min(hard_cap, self.capital_at_open)
        return hard_cap

    def __post_init__(self):
        if self.peak_price == 0.0:
            self.peak_price = self.entry_price

    # ── 손익 계산 ────────────────────────────────────────────

    def coin_pct(self, price: float) -> float:
        """코인 가격 변화율 (트렌드 방향 기준, 슬리피지 포함 avg_price 기준)"""
        if self.avg_price == 0:
            return 0.0
        if self.trend == "UP":
            return (price - self.avg_price) / self.avg_price
        return (self.avg_price - price) / self.avg_price

    def pnl_pct(self, price: float) -> float:
        """포지션 손익률 (레버리지 반영, 수수료·슬리피지 미차감)"""
        return self.coin_pct(price) * config.LEVERAGE

    def _entry_cost(self) -> float:
        """진입 시 비용 (수수료+슬리피지) — 보유 중 표시용"""
        notional = self.total_invested * config.LEVERAGE
        return notional * (config.TAKER_FEE_RATE + config.SLIPPAGE_RATE)

    def _total_cost(self) -> float:
        """진입+청산 전체 비용 — 청산 시 최종 계산용"""
        notional = self.total_invested * config.LEVERAGE
        return notional * (config.TAKER_FEE_RATE + config.SLIPPAGE_RATE) * 2

    def gross_pnl(self, price: float) -> float:
        """총손익 (비용 미차감)"""
        return self.total_invested * self.pnl_pct(price)

    def net_pnl(self, price: float) -> float:
        """보유 중 순손익 (진입 비용만 차감 — 청산 비용은 청산 시 추가)"""
        return self.gross_pnl(price) - self._entry_cost()

    def realized_pnl(self, exit_price: float) -> float:
        """청산 시 최종 순손익 (진입+청산 비용 전부 차감)"""
        return self.gross_pnl(exit_price) - self._total_cost()

    # ── 비례 트레일링 거리 계산 ─────────────────────────────

    def _stepped_trail_distance(self, profit_pct: float) -> float:
        """
        수익의 20%를 숨통으로 허용하는 동적 트레일 거리.
        peak $100 → $80에서 청산 (어느 수익 수준에서도 20% 숨통)
        공식: distance = profit_pct × (BREATHING / LEVERAGE)
        """
        if profit_pct <= 0:
            return config.DCA_TRAIL_DIST_MIN
        dynamic = profit_pct * (config.DCA_TRAIL_BREATHING_RATIO / config.LEVERAGE)
        return max(config.DCA_TRAIL_DIST_MIN, min(config.DCA_TRAIL_DIST_MAX, dynamic))

    # ── 트레일링 업데이트 ────────────────────────────────────

    def update_trail(self, price: float):
        """
        매 틱마다 호출.
        - 일반: 포지션 +3% 도달 시 트레일 활성화
        - 하드캡: 포지션 +2% 도달 시 트레일 활성화 (일반 익절 비활성 상태)
        활성화 후 peak 대비 TRAIL_DISTANCE_PCT 되돌리면 trail_sl 갱신.
        DCA 1단계 이상: 비례 동적 트레일 (수익의 20% 숨통)
        """
        if self.crash_short:
            activate = config.CRASH_TRAIL_ACTIVATE_PCT
            distance = config.CRASH_TRAIL_DISTANCE_PCT
        elif self.avg_down_step >= config.DCA_TRAIL_STEP_THRESHOLD:
            activate = config.DCA_TRAIL_ACTIVATE_PCT
            distance = config.DCA_TRAIL_DISTANCE_PCT
        else:
            activate = config.TRAIL_ACTIVATE_PCT
            distance = config.TRAIL_DISTANCE_PCT
        use_dca_trail = (self.avg_down_step >= config.DCA_TRAIL_STEP_THRESHOLD
                         and not self.crash_short)

        if self.trend == "UP":
            if price > self.peak_price:
                self.peak_price = price
            profit_pct = (self.peak_price - self.avg_price) / self.avg_price * config.LEVERAGE
            if profit_pct >= activate:
                self.trail_active = True
            if self.trail_active:
                if use_dca_trail:
                    distance = self._stepped_trail_distance(profit_pct)
                new_sl = self.peak_price * (1 - distance)
                if new_sl > self.trail_sl:
                    self.trail_sl = new_sl
        else:  # DOWN (숏)
            if price < self.peak_price:
                self.peak_price = price
            profit_pct = (self.avg_price - self.peak_price) / self.avg_price * config.LEVERAGE
            if profit_pct >= activate:
                self.trail_active = True
            if self.trail_active:
                if use_dca_trail:
                    distance = self._stepped_trail_distance(profit_pct)
                new_sl = self.peak_price * (1 + distance)
                if new_sl < self.trail_sl or self.trail_sl == 0:
                    self.trail_sl = new_sl

    def is_trail_hit(self, price: float) -> bool:
        """
        트레일링 손절가 터치 여부.
        trail_sl에서 청산 시 realized_pnl > 0 이어야만 유효
        (수수료+슬리피지 포함 실제 수익이 나는 구간에서만 발동)
        """
        if not self.trail_active or self.trail_sl == 0:
            return False
        if self.trend == "UP":
            if self.trail_sl <= self.avg_price:
                return False   # 평균단가 아래 → 무조건 손실
            if price > self.trail_sl:
                return False   # 아직 trail_sl 미도달
            # trail_sl에서 실제 청산 시 수익 여부 확인 (슬리피지 포함)
            fill = self.trail_sl * (1 - config.SLIPPAGE_RATE)
            if self.realized_pnl(fill) <= 0:
                return False   # trail_sl이 손익분기선 미만 → 발동 보류
            return True
        else:
            if self.trail_sl >= self.avg_price:
                return False
            if price < self.trail_sl:
                return False
            fill = self.trail_sl * (1 + config.SLIPPAGE_RATE)
            if self.realized_pnl(fill) <= 0:
                return False
            return True

    # ── 다음 물타기 금액 ─────────────────────────────────────

    def next_avg_down_amount(self) -> float:
        base = self.initial_invest if self.initial_invest > 0 else config.INITIAL_POSITION_USD
        amounts = {0: base, 1: base * 2, 2: base * 4, 3: base * 8, 4: base * 16}
        return amounts.get(self.avg_down_step, 0.0)

    # ── 물타기 실행 ──────────────────────────────────────────

    def apply_avg_down(self, raw_price: float, add_usd: float, kind: str = "price"):
        """물타기: 슬리피지 적용 후 평균단가 재계산
        kind: 저널 기록용 발동 사유 ("price" = 가격 트리거 / "sideways" = 횡보 강제)
        """
        fill_price = _apply_slip(raw_price, self.trend, entry=True)
        add_qty    = (add_usd * config.LEVERAGE) / fill_price
        prev_cost  = self.avg_price * self.total_qty
        new_cost   = prev_cost + add_qty * fill_price

        self.total_qty      += add_qty
        self.total_invested += add_usd
        self.avg_price       = new_cost / self.total_qty
        self.avg_down_step  += 1
        self.step_ref_pnl    = self.pnl_pct(fill_price)
        self.step_enter_time = time.time()   # 이 단계 진입 시각 기록

        # 저널용 단계 이력 (어느 단계에서 돈이 새는지 분석)
        self.step_history.append({
            "step":           self.avg_down_step,
            "at_s":           round(time.time() - self.open_time, 1),
            "price":          round(fill_price, 8),
            "add_usd":        round(add_usd, 2),
            "total_invested": round(self.total_invested, 2),
            "kind":           kind,
        })

        if self.total_invested >= self.max_position:
            self.hard_cap_price = raw_price

        # DCA 후 peak/trail 리셋 (trail_active 여부 무관):
        # trail이 비활성 상태에서도 이전 peak가 남아 있으면 DCA 직후 trail이
        # 즉시 활성화+발동하는 버그 방지 (DCA로 avg_price 변동 시 profit_pct 급등)
        old_sl = self.trail_sl
        was_active = self.trail_active
        self.trail_active = False
        self.trail_sl     = 0.0
        self.peak_price   = fill_price  # DCA 체결가부터 peak 재추적
        # 수익 보존 락도 리셋 — 포지션 크기가 달라졌으므로 이전 고점 순수익은
        # 더 이상 이 포지션의 기준이 아니다. 남겨두면 DCA 직후 즉시 락이 걸린다.
        self.peak_realized = 0.0
        if was_active:
            logger.info(
                f"[트레일 리셋] DCA {self.avg_down_step}단계 후 "
                f"trail_sl({old_sl:.6f}) → 무효화, 새 평단 {self.avg_price:.6f} 기준으로 재출발"
            )
        else:
            logger.info(
                f"[피크 리셋] DCA {self.avg_down_step}단계 후 "
                f"peak → {fill_price:.6f} 재설정 (이전 peak 제거, trail 즉시 발동 방지)"
            )

        slip_usd = add_usd * config.LEVERAGE * config.SLIPPAGE_RATE
        fee_usd  = add_usd * config.LEVERAGE * config.TAKER_FEE_RATE
        logger.info(
            f"[물타기 {self.avg_down_step}단계] {self.symbol} | "
            f"추가: ${add_usd:.0f} | 체결가: {fill_price:.6f} | "
            f"평균단가: {self.avg_price:.6f} | 총투입: ${self.total_invested:.0f} | "
            f"슬리피지: ${slip_usd:.3f} | 수수료: ${fee_usd:.3f}"
        )

    def apply_pyramid(self, raw_price: float, add_usd: float):
        """불타기: 트레일 활성화(수익권) 시 추가 진입, avg_price 재계산"""
        fill_price = _apply_slip(raw_price, self.trend, entry=True)
        add_qty    = (add_usd * config.LEVERAGE) / fill_price
        prev_cost  = self.avg_price * self.total_qty
        new_cost   = prev_cost + add_qty * fill_price
        self.total_qty       += add_qty
        self.total_invested  += add_usd
        self.avg_price        = new_cost / self.total_qty
        self.pyramid_done     = True
        self.pyramid_qty      = add_qty
        self.pyramid_invested = add_usd
        slip_usd = add_usd * config.LEVERAGE * config.SLIPPAGE_RATE
        fee_usd  = add_usd * config.LEVERAGE * config.TAKER_FEE_RATE
        logger.info(
            f"[불타기🔥] {self.symbol} | 트레일 활성 → "
            f"추가 ${add_usd:.0f} | 체결가: {fill_price:.6f} | "
            f"새 평균단가: {self.avg_price:.6f} | 총투입: ${self.total_invested:.0f} | "
            f"수수료: ${fee_usd:.3f} | 슬리피지: ${slip_usd:.3f}"
        )


# ────────────────────────────────────────────────────────────
#  슬리피지 헬퍼
# ────────────────────────────────────────────────────────────

def _apply_slip(price: float, trend: str, entry: bool) -> float:
    """
    진입: 롱은 조금 비싸게, 숏은 조금 싸게 체결
    청산: 롱은 조금 싸게, 숏은 조금 비싸게 체결
    """
    s = config.SLIPPAGE_RATE
    if entry:
        return price * (1 + s) if trend == "UP" else price * (1 - s)
    else:
        return price * (1 - s) if trend == "UP" else price * (1 + s)


# ────────────────────────────────────────────────────────────
#  페이퍼 트레이더
# ────────────────────────────────────────────────────────────

class PaperTrader:
    def __init__(self, live_api=None, governor=None):
        self.live_api = live_api
        self.governor = governor         # RiskGovernor (없으면 시드 축소 없음)
        self._qty_precision: dict = {}   # {symbol: 소수점 자릿수} 캐시
        self.total_capital:      float = config.TOTAL_CAPITAL
        self.position: Optional[Position] = None
        self.total_pnl:          float = 0.0
        self.win_count:          int   = 0
        self.loss_count:         int   = 0
        self.closed_trades:      list  = []
        self.total_withdrawn:    float = 0.0
        self.withdrawal_count:   int   = 0
        self.withdrawal_history: list  = []
        self._load_state()

    # ── 실거래 헬퍼 ─────────────────────────────────────────────

    def _get_qty_precision(self, symbol: str) -> int:
        """심볼의 수량 소수점 자릿수 (캐시)"""
        if symbol in self._qty_precision:
            return self._qty_precision[symbol]
        try:
            for c in self.live_api.get_contracts():
                if c.get("symbol") == symbol:
                    step = float(c.get("tradeMinQuantity", 1))
                    precision = 0 if step >= 1 else max(0, int(round(-math.log10(step))))
                    self._qty_precision[symbol] = precision
                    return precision
        except Exception as e:
            logger.warning(f"[qty_precision] {symbol} 조회 실패: {e}")
        self._qty_precision[symbol] = 1
        return 1

    def _round_qty(self, symbol: str, qty: float):
        """수량을 거래소 최소단위로 반올림 (정수면 int 반환)"""
        precision = self._get_qty_precision(symbol)
        rounded = round(qty, precision)
        return int(rounded) if precision == 0 else rounded

    # ── 거래소 강제 손절 백스톱 ─────────────────────────────────
    #
    # 봇의 자체 손절은 프로세스가 살아 있고 API가 응답할 때만 작동한다.
    # 봇이 죽거나 회선이 끊기면 포지션은 무방비다 — 4단계에서 코인 -12%면
    # 계좌가 청산된다. 그래서 거래소 서버 쪽에 STOP_MARKET 을 걸어둔다.
    # 정상 상황에선 봇 손절이 먼저 발동하므로 이 주문은 체결되지 않는다.

    def _stop_loss_usd(self) -> float:
        """백스톱이 발동할 손실 금액 (양수 USD)"""
        bot_cap  = abs(self._get_max_loss_usd()) * config.EXCHANGE_STOP_MULT
        capital  = self.total_capital + self.total_pnl
        hard_cap = capital * config.EXCHANGE_STOP_MAX_CAPITAL_RATIO
        return max(1.0, min(bot_cap, hard_cap))

    def _stop_distance(self, p: Position) -> float:
        """평단 대비 STOP 까지의 코인 변동률 (양수)

        두 기준 중 **가까운 쪽**을 쓴다:
          ① 금액 기준 — gross_pnl = 투입 × coin_pct × LEVERAGE = -L
                        → coin_pct = L / (투입 × LEVERAGE)
          ② 변동률 상한 — 저단계에서는 ①이 -75% 같은 도달 불가 지점이 되므로,
                        "코인이 이만큼 움직이면 무조건 나간다"는 절대선을 둔다.
        """
        notional = p.total_invested * config.LEVERAGE
        if notional <= 0:
            return 0.0
        by_usd = self._stop_loss_usd() / notional
        return min(by_usd, config.EXCHANGE_STOP_MAX_COIN_PCT, 0.95)

    def _calc_stop_price(self, p: Position) -> float:
        """STOP 주문을 걸 코인 가격"""
        x = self._stop_distance(p)
        if x <= 0:
            return 0.0
        return p.avg_price * (1 - x) if p.trend == "UP" else p.avg_price * (1 + x)

    def _cancel_exchange_stop(self, symbol: str, order_id: str):
        """기존 STOP 주문 취소 (실패해도 무시 — 이미 체결·소멸했을 수 있음)"""
        if not order_id:
            return
        try:
            self.live_api.cancel_order(symbol, order_id)
        except Exception as e:
            logger.debug(f"[백스톱] 기존 주문 취소 실패(무시): {e}")

    def sync_exchange_stop(self):
        """진입·DCA 직후 호출. 평단이 바뀌었으므로 STOP 주문을 다시 건다.

        실패해도 매매는 계속된다 — 백스톱이 없을 뿐 봇 자체 손절은 살아 있다.
        다만 보호막이 없는 상태이므로 WARNING 으로 남긴다.
        """
        if not config.EXCHANGE_STOP_ENABLED:
            return
        if not self.live_api or not config.LIVE_TRADING:
            return
        p = self.position
        if not p:
            return

        new_price = self._calc_stop_price(p)
        if new_price <= 0:
            return

        try:
            precision = self.live_api.get_price_precision(p.symbol)
            new_price = round(new_price, precision)
            pos_side  = "LONG" if p.trend == "UP" else "SHORT"
            qty       = self._round_qty(p.symbol, p.total_qty)

            self._cancel_exchange_stop(p.symbol, p.stop_order_id)
            p.stop_order_id, p.stop_price = "", 0.0

            result = self.live_api.place_stop_market(p.symbol, pos_side, new_price, qty)
            if str(result.get("code", "?")) != "0":
                logger.warning(
                    f"[백스톱 실패] {p.symbol} code={result.get('code')} "
                    f"msg={result.get('msg','')} — 거래소 손절 없이 진행 "
                    f"(봇 자체 손절은 정상 작동)"
                )
                return

            order = result.get("data", {}).get("order", {})
            p.stop_order_id = str(order.get("orderId", ""))
            p.stop_price    = new_price
            implied_loss = p.gross_pnl(new_price)
            logger.info(
                f"[백스톱] {p.symbol} STOP_MARKET @ {new_price:.8f} "
                f"(코인 {self._stop_distance(p)*100:.1f}% → 손실 ${implied_loss:+.0f}) | "
                f"{p.avg_down_step}단계 투입 ${p.total_invested:.0f} | id={p.stop_order_id}"
            )
        except Exception as e:
            logger.warning(f"[백스톱 실패] {p.symbol}: {e} — 거래소 손절 없이 진행")

    def reconcile_with_exchange(self) -> bool:
        """봇은 포지션이 있다고 믿는데 거래소에는 없는 경우를 감지·정리한다.

        백스톱 STOP_MARKET 이 체결되면 거래소 포지션은 사라지지만 봇은 모른다.
        그 상태로 두면 봇이 유령 포지션을 계속 관리하며 신규 진입도 하지 않는다.
        감지되면 장부를 닫는다 — 체결가는 알 수 없으므로 걸어둔 STOP 가격을
        추정치로 쓴다 (없으면 현재가).

        반환: 불일치를 처리했으면 True
        """
        p = self.position
        if not p or not self.live_api or not config.LIVE_TRADING:
            return False
        pos_side = "LONG" if p.trend == "UP" else "SHORT"
        try:
            positions = self.live_api.get_positions(p.symbol)
        except Exception as e:
            logger.debug(f"[정합성] 포지션 조회 실패(건너뜀): {e}")
            return False   # 조회 실패를 "포지션 없음"으로 오판하면 안 된다

        still_open = any(
            x.get("positionSide") == pos_side and float(x.get("positionAmt", 0)) != 0
            for x in positions
        )
        if still_open:
            return False

        # 체결가 추정: 백스톱이 걸려 있었다면 그 가격에서 나갔을 가능성이 높다
        est_price = p.stop_price if p.stop_price > 0 else 0.0
        if est_price <= 0:
            try:
                est_price = self.live_api.get_price(p.symbol)
            except Exception:
                est_price = p.avg_price
        logger.critical(
            f"[정합성⚠] {p.symbol} {pos_side} — 봇은 보유 중이나 거래소에 포지션 없음. "
            f"거래소 강제 손절(백스톱) 체결로 추정 → 장부를 닫는다. "
            f"추정 체결가 {est_price:.8f} "
            f"(실제 체결가와 다를 수 있으니 거래소 내역과 대조할 것)"
        )
        self.close_position(est_price, "거래소청산감지(백스톱추정)")
        return True

    def clear_exchange_stop(self, symbol: str, order_id: str = None):
        """청산 후 남은 STOP 주문 정리 (고아 주문이 다음 포지션을 오폭하지 않도록)"""
        if not self.live_api or not config.LIVE_TRADING:
            return
        try:
            self.live_api.cancel_all_open_orders(symbol)
        except Exception as e:
            logger.debug(f"[백스톱] 정리 실패(무시) {symbol}: {e}")

    def _live_entry(self, symbol: str, trend: str, qty: float) -> bool:
        """실거래 진입 주문 (헤지모드 LONG/SHORT). 성공 True / 실패 False"""
        if not self.live_api or not config.LIVE_TRADING:
            return True
        try:
            pos_side = "LONG" if trend == "UP" else "SHORT"
            side     = "BUY"  if trend == "UP" else "SELL"
            rounded_qty = self._round_qty(symbol, qty)
            self.live_api.set_leverage(symbol, config.LEVERAGE, pos_side)
            result = self.live_api.place_order(symbol, side, pos_side, rounded_qty)
            code = result.get("code", "?")
            logger.info(f"[실거래진입] {symbol} {pos_side} qty={rounded_qty} code={code}")
            if str(code) != "0":
                logger.error(f"[실거래진입오류] code={code} msg={result.get('msg','')}")
                return False
            return True
        except Exception as e:
            logger.error(f"[실거래진입실패] {symbol}: {e}")
            return False

    def _live_close(self, symbol: str, trend: str) -> bool:
        """실거래 청산 주문 (헤지모드 LONG/SHORT). 성공 True / 실패 False
        최대 3번 재시도. 포지션이 이미 없으면 성공으로 처리."""
        if not self.live_api or not config.LIVE_TRADING:
            return True
        pos_side = "LONG" if trend == "UP" else "SHORT"
        # 거래소 실제 수량 조회 (closePosition=true 가 거부될 경우 대비)
        exchange_qty = None
        try:
            for p in self.live_api.get_positions(symbol):
                if p.get("positionSide") == pos_side:
                    q = abs(float(p.get("positionAmt", 0)))
                    if q > 0:
                        exchange_qty = q
                        break
        except Exception:
            pass
        for attempt in range(3):
            try:
                result = self.live_api.close_position(symbol, pos_side, qty=exchange_qty)
                code = result.get("code", "?")
                logger.info(f"[실거래청산] {symbol} {pos_side} code={code} (시도 {attempt+1}/3)")
                if str(code) == "0":
                    return True
                logger.error(
                    f"[실거래청산오류] code={code} msg={result.get('msg','')} "
                    f"(시도 {attempt+1}/3)"
                )
                # 오류 시 실제 포지션이 남아있는지 확인
                try:
                    positions = self.live_api.get_positions(symbol)
                    still_open = any(
                        p.get("positionSide") == pos_side
                        and float(p.get("positionAmt", 0)) != 0
                        for p in positions
                    )
                    if not still_open:
                        logger.info(f"[청산확인] {symbol} 거래소에 {pos_side} 포지션 없음 → 이미 청산됨")
                        return True
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"[실거래청산실패] {symbol}: {e} (시도 {attempt+1}/3)")
            if attempt < 2:
                time.sleep(1)
        logger.critical(
            f"[청산완전실패⚠] {symbol} {pos_side} — 3번 재시도 후에도 실패! "
            f"거래소에서 수동 청산 필요!"
        )
        return False

    def _live_add(self, symbol: str, trend: str, qty: float) -> bool:
        """실거래 DCA 추가 주문 (헤지모드 LONG/SHORT). 성공 True / 실패 False"""
        if not self.live_api or not config.LIVE_TRADING:
            return True
        try:
            pos_side = "LONG" if trend == "UP" else "SHORT"
            side     = "BUY"  if trend == "UP" else "SELL"
            rounded_qty = self._round_qty(symbol, qty)
            result = self.live_api.place_order(symbol, side, pos_side, rounded_qty)
            code = result.get("code", "?")
            logger.info(f"[실거래DCA] {symbol} {pos_side} qty={rounded_qty} code={code}")
            if str(code) != "0":
                logger.error(f"[실거래DCA오류] code={code} msg={result.get('msg','')}")
                return False
            return True
        except Exception as e:
            logger.error(f"[실거래DCA실패] {symbol}: {e}")
            return False

    # ── 상태 저장 / 복원 ─────────────────────────────────────

    def save_state(self):
        pos_data = None
        if self.position:
            p = self.position
            pos_data = {
                "symbol":         p.symbol,
                "trend":          p.trend,
                "entry_price":    p.entry_price,
                "avg_price":      p.avg_price,
                "total_invested": p.total_invested,
                "total_qty":      p.total_qty,
                "avg_down_step":  p.avg_down_step,
                "step_ref_pnl":   p.step_ref_pnl,
                "hard_cap_price": p.hard_cap_price,
                "open_time":      p.open_time,
                "peak_price":     p.peak_price,
                "trail_active":   p.trail_active,
                "trail_sl":       p.trail_sl,
                "price_hist":      p.price_hist[-60:],
                "low_vol_start":   p.low_vol_start,
                "initial_invest":  p.initial_invest,
                "sideways_dca":    p.sideways_dca,
                "step_enter_time": p.step_enter_time,
                "crash_short":     p.crash_short,
                "pyramid_done":     p.pyramid_done,
                "pyramid_qty":      p.pyramid_qty,
                "pyramid_invested": p.pyramid_invested,
                "capital_at_open":  p.capital_at_open,
                # 저널 계측 — 재시작해도 MAE/MFE·단계이력이 이어지도록 함께 저장
                "mfe_usd":      p.mfe_usd,
                "mae_usd":      p.mae_usd,
                "mfe_coin_pct": p.mfe_coin_pct,
                "mae_coin_pct": p.mae_coin_pct,
                "mfe_at_s":     p.mfe_at_s,
                "mae_at_s":     p.mae_at_s,
                "peak_realized": p.peak_realized,
                "step_history": p.step_history,
                "stop_order_id": p.stop_order_id,
                "stop_price":    p.stop_price,
            }
        data = {
            "total_pnl":          self.total_pnl,
            "win_count":          self.win_count,
            "loss_count":         self.loss_count,
            "closed_trades":      self.closed_trades[-200:],
            "position":           pos_data,
            "total_withdrawn":    round(self.total_withdrawn, 4),
            "withdrawal_count":   self.withdrawal_count,
            "withdrawal_history": self.withdrawal_history[-100:],
        }
        tmp = STATE_FILE + ".tmp"
        bak = STATE_FILE + ".bak"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            if os.path.exists(STATE_FILE):
                os.replace(STATE_FILE, bak)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            logger.error(f"[state] 저장 실패: {e}")

    def _load_state(self):
        for path in [STATE_FILE, STATE_FILE + ".bak"]:
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.total_pnl          = float(data.get("total_pnl", 0.0))
                self.win_count          = int(data.get("win_count", 0))
                self.loss_count         = int(data.get("loss_count", 0))
                self.closed_trades      = list(data.get("closed_trades", []))
                self.total_withdrawn    = float(data.get("total_withdrawn", 0.0))
                self.withdrawal_count   = int(data.get("withdrawal_count", 0))
                self.withdrawal_history = list(data.get("withdrawal_history", []))
                pd = data.get("position")
                if pd:
                    p = Position(
                        symbol         = pd["symbol"],
                        trend          = pd["trend"],
                        entry_price    = float(pd["entry_price"]),
                        avg_price      = float(pd["avg_price"]),
                        total_invested = float(pd["total_invested"]),
                        total_qty      = float(pd["total_qty"]),
                        avg_down_step  = int(pd["avg_down_step"]),
                        step_ref_pnl   = float(pd["step_ref_pnl"]),
                        hard_cap_price = float(pd["hard_cap_price"]),
                        open_time      = float(pd["open_time"]),
                        peak_price     = float(pd.get("peak_price", pd["entry_price"])),
                        trail_active   = bool(pd.get("trail_active", False)),
                        trail_sl       = float(pd.get("trail_sl", 0.0)),
                        price_hist      = pd.get("price_hist", []),
                        low_vol_start   = float(pd.get("low_vol_start", 0.0)),
                        initial_invest  = float(pd.get("initial_invest", 0.0)),
                        sideways_dca    = bool(pd.get("sideways_dca", False)),
                        step_enter_time = float(pd.get("step_enter_time", 0.0)),
                        crash_short     = bool(pd.get("crash_short", False)),
                        pyramid_done     = bool(pd.get("pyramid_done", False)),
                        pyramid_qty      = float(pd.get("pyramid_qty", 0.0)),
                        pyramid_invested = float(pd.get("pyramid_invested", 0.0)),
                        capital_at_open  = float(pd.get("capital_at_open", 0.0)),
                        mfe_usd      = float(pd.get("mfe_usd", 0.0)),
                        mae_usd      = float(pd.get("mae_usd", 0.0)),
                        mfe_coin_pct = float(pd.get("mfe_coin_pct", 0.0)),
                        mae_coin_pct = float(pd.get("mae_coin_pct", 0.0)),
                        mfe_at_s     = float(pd.get("mfe_at_s", 0.0)),
                        mae_at_s     = float(pd.get("mae_at_s", 0.0)),
                        peak_realized = float(pd.get("peak_realized", 0.0)),
                        step_history = list(pd.get("step_history", [])),
                        stop_order_id = str(pd.get("stop_order_id", "")),
                        stop_price    = float(pd.get("stop_price", 0.0)),
                    )
                    # initial_invest 마이그레이션
                    # (구버전 state.json에 필드 없을 때 → 현재 config 시드로 설정)
                    if p.initial_invest == 0:
                        p.initial_invest = config.INITIAL_POSITION_USD
                        logger.info(
                            f"[state] initial_invest 복원: ${p.initial_invest:.2f} (config 기준)"
                        )
                    # step_enter_time 마이그레이션
                    # (구버전 state.json — 재시작 직후 횡보DCA 즉시 발동 방지)
                    if p.step_enter_time == 0 and p.avg_down_step >= 1:
                        p.step_enter_time = time.time()
                        logger.info(
                            f"[state] step_enter_time 복원: 현재 시간으로 초기화 (즉시 횡보DCA 방지)"
                        )
                    # 재시작 시 트레일 유효성 검증
                    # trail_sl이 평균단가보다 유리한 위치에 있어야만 유지
                    if p.trail_active:
                        valid = (
                            (p.trend == "UP"   and p.trail_sl > p.avg_price) or
                            (p.trend == "DOWN" and p.trail_sl < p.avg_price)
                        )
                        if not valid:
                            p.trail_active = False
                            p.trail_sl     = 0.0
                            p.peak_price   = p.avg_price
                            logger.info(f"[state] 트레일 리셋 (재시작 시 유효하지 않은 trail_sl)")
                    self.position = p
                    # 봇이 죽어 있던 동안 STOP 주문이 취소·체결됐을 수 있다.
                    # 재시작 시 무조건 다시 걸어 백스톱 공백을 없앤다.
                    self.sync_exchange_stop()
                total = self.win_count + self.loss_count
                logger.info(
                    f"[state] 복원 완료 | 누적손익 ${self.total_pnl:+.2f} | "
                    f"거래 {total}회 ({self.win_count}W/{self.loss_count}L)"
                )
                if self.position:
                    trail_str = " [트레일중]" if self.position.trail_active else ""
                    logger.info(
                        f"[state] 포지션 복원: {self.position.symbol} "
                        f"({self.position.trend}) | 투입 ${self.position.total_invested:.0f}"
                        f"{trail_str}"
                    )
                return
            except Exception as e:
                logger.warning(f"[state] {path} 로드 실패: {e}")

    # ── 진입 ─────────────────────────────────────────────────

    def _get_initial_position_usd(self) -> float:
        """자본 증가에 따른 동적 시드 계산
        상한 1: DYNAMIC_SEED_MAX_USD ($270 절대 상한)
        상한 2: capital × DYNAMIC_SEED_CAPITAL_RATIO (4단계 투입금 = 시드×16이 자본 96% 이하)
        """
        capital    = self.total_capital + self.total_pnl
        increments = int((capital - config.DYNAMIC_SEED_BASE_CAPITAL)
                         / config.DYNAMIC_SEED_STEP_CAPITAL)
        base_seed = config.INITIAL_POSITION_USD + increments * config.DYNAMIC_SEED_STEP_USD
        base_seed = max(config.DYNAMIC_SEED_MIN_USD, min(base_seed, config.DYNAMIC_SEED_MAX_USD))
        capital_ratio_cap = capital * config.DYNAMIC_SEED_CAPITAL_RATIO
        seed = min(base_seed, capital_ratio_cap)

        # 리스크 거버너: 낙폭·연속손실 중이면 시드를 줄인다.
        # 배율 0(진입 중지)이면 최소 시드로 떨어뜨린다 — 진입은 can_enter() 가
        # 따로 막지만, 이 함수를 쓰는 다른 경로가 전액 시드를 집어가지 않도록.
        if self.governor is not None:
            mult = self.governor.seed_multiplier(capital)
            seed = max(config.DYNAMIC_SEED_MIN_USD, seed * mult)
        return seed

    def _get_max_loss_usd(self) -> float:
        """동적 손절 한도: -min(시드 × SEED_MULT, CEILING)
        CEILING은 절대 상한 — 자본이 커져도 1회 손실이 이 금액을 넘지 않는다.
        시드 $60 → min(240, 250) = $240 / 시드 $135 → min(540, 250) = $250

        보유 중에는 **진입 당시 시드**를 기준으로 한다. 현재 시드로 계산하면
        거버너가 시드를 줄이는 순간 이미 열려 있는 포지션의 손절선이 함께
        당겨져 조기 손절되기 때문이다."""
        if self.position and self.position.initial_invest > 0:
            seed = self.position.initial_invest
        else:
            seed = self._get_initial_position_usd()
        cap  = min(seed * config.MAX_NET_LOSS_SEED_MULT, config.MAX_NET_LOSS_CEILING)
        return -cap

    def open_position(self, symbol: str, trend: str, raw_price: float,
                      invest_override: float = None,
                      crash_short: bool = False) -> Optional[Position]:
        invest     = invest_override if invest_override is not None \
                     else self._get_initial_position_usd()
        fill_price = _apply_slip(raw_price, trend, entry=True)
        qty        = (invest * config.LEVERAGE) / fill_price
        fee        = invest * config.LEVERAGE * config.TAKER_FEE_RATE
        slip_usd   = invest * config.LEVERAGE * config.SLIPPAGE_RATE

        # 실거래: 진입 주문 먼저 — 실패 시 paper state 업데이트 없이 None 반환
        if not self._live_entry(symbol, trend, qty):
            logger.error(f"[진입취소] {symbol} 실거래 주문 실패 → 진입 중단")
            return None

        tag = "[급락SHORT진입]" if crash_short else "[진입]"
        self.position = Position(
            symbol          = symbol,
            trend           = trend,
            entry_price     = fill_price,
            avg_price       = fill_price,
            total_invested  = invest,
            total_qty       = qty,
            avg_down_step   = 0,
            step_ref_pnl    = 0.0,
            hard_cap_price  = 0.0,
            peak_price      = fill_price,
            initial_invest  = invest,
            step_enter_time = time.time(),
            crash_short     = crash_short,
            capital_at_open = self.total_capital + self.total_pnl,
        )
        self.position.step_history.append({
            "step":           0,
            "at_s":           0.0,
            "price":          round(fill_price, 8),
            "add_usd":        round(invest, 2),
            "total_invested": round(invest, 2),
            "kind":           "crash_short" if crash_short else "entry",
        })
        logger.info(
            f"{tag} {symbol} ({trend}) | 호가: {raw_price:.6f} → "
            f"체결가: {fill_price:.6f} | 투입: ${invest:.0f} | "
            f"수수료: ${fee:.3f} | 슬리피지: ${slip_usd:.3f}"
        )
        self.sync_exchange_stop()   # 진입 즉시 거래소 백스톱 배치
        self.save_state()
        return self.position

    # ── 청산 ─────────────────────────────────────────────────

    def close_position(self, raw_price: float, reason: str) -> dict:
        p = self.position
        if not p:
            return {}

        # 실거래: 청산 주문 (실패 시 paper state 유지 — 수동 청산 후 봇 재시작 필요)
        if not self._live_close(p.symbol, p.trend):
            return {}  # 청산 실패 → paper state 그대로 유지 (새 포지션 진입 차단)

        # 청산 성공 → 거래소에 남은 STOP 주문 정리.
        # 고아 STOP 이 남으면 같은 코인에 재진입했을 때 엉뚱한 가격에 오폭한다.
        self.clear_exchange_stop(p.symbol, p.stop_order_id)

        fill_price = _apply_slip(raw_price, p.trend, entry=False)
        pnl        = p.realized_pnl(fill_price)
        cost       = p._total_cost()
        self.total_pnl += pnl

        if pnl > 0:
            self.win_count += 1
        else:
            self.loss_count += 1

        trade = {
            "symbol":         p.symbol,
            "trend":          p.trend,
            "avg_price":      round(p.avg_price, 6),
            "exit_price":     round(fill_price, 6),
            "total_invested": p.total_invested,
            "avg_down_step":  p.avg_down_step,
            "trail_active":   p.trail_active,
            "peak_price":     round(p.peak_price, 6),
            "reason":         reason,
            "gross_pnl":      round(p.gross_pnl(fill_price), 4),
            "cost":           round(cost, 4),
            "pnl":            round(pnl, 4),
            "duration_s":     round(time.time() - p.open_time, 1),
        }
        self.closed_trades.append(trade)

        # ── 영구 저널 기록 (분석·진화용) ────────────────────────
        # closed_trades 는 state.json 에서 최근 200건만 살아남으므로
        # 학습 데이터는 trades.jsonl 에 별도로 append 한다.
        journal_rec = dict(trade)
        journal_rec.update({
            "open_time":      round(p.open_time, 1),
            "close_time":     round(time.time(), 1),
            "open_kst":       trade_journal._iso(p.open_time),
            "close_kst":      trade_journal._iso(time.time()),
            "entry_price":    round(p.entry_price, 8),
            "initial_invest": round(p.initial_invest, 2),
            "leverage":       config.LEVERAGE,
            "sideways_dca":   p.sideways_dca,
            "crash_short":    p.crash_short,
            "mfe_usd":        round(p.mfe_usd, 4),
            "mae_usd":        round(p.mae_usd, 4),
            "mfe_coin_pct":   round(p.mfe_coin_pct, 6),
            "mae_coin_pct":   round(p.mae_coin_pct, 6),
            "mfe_at_s":       round(p.mfe_at_s, 1),
            "mae_at_s":       round(p.mae_at_s, 1),
            "peak_realized":  round(p.peak_realized, 4),
            "steps":          p.step_history,
            "capital_before": round(self.total_capital + self.total_pnl - pnl, 2),
            "capital_after":  round(self.total_capital + self.total_pnl, 2),
        })
        trade_journal.append(journal_rec)

        # 거버너에 결과 통보 — 연속 손실 카운터·고점 갱신
        if self.governor is not None:
            try:
                self.governor.record_trade(pnl)
                self.governor.update_equity(self.total_capital + self.total_pnl)
            except Exception as e:
                logger.warning(f"[거버너] 통보 실패(무시): {e}")

        self.position = None

        icon = "✅" if pnl > 0 else "❌"
        logger.info(
            f"[청산 {icon}] {trade['symbol']} | 사유: {reason} | "
            f"호가: {raw_price:.6f} → 체결가: {fill_price:.6f} | "
            f"총손익: ${trade['gross_pnl']:+.4f} | 비용: ${cost:.4f} | "
            f"순손익: ${pnl:+.4f} | 누적: ${self.total_pnl:+.2f}"
        )
        self.save_state()
        self._check_auto_withdraw()
        return trade

    def _check_auto_withdraw(self):
        """확정 자본이 출금 기준선 이상이면 자동 출금 시뮬레이션"""
        if not config.AUTO_WITHDRAW_ENABLED:
            return
        confirmed = self.total_capital + self.total_pnl
        if confirmed < config.AUTO_WITHDRAW_THRESHOLD:
            return
        withdraw_amt = confirmed - config.AUTO_WITHDRAW_BASE
        self.total_pnl    -= withdraw_amt
        self.total_withdrawn  += withdraw_amt
        self.withdrawal_count += 1
        entry = {
            "no":     self.withdrawal_count,
            "time":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "amount": round(withdraw_amt, 2),
            "total":  round(self.total_withdrawn, 2),
        }
        self.withdrawal_history.append(entry)
        logger.info(
            f"[자동출금] #{self.withdrawal_count}회 | "
            f"출금: ${withdraw_amt:+.2f} | "
            f"누적 출금: ${self.total_withdrawn:.2f} | "
            f"잔여 확정 자본: ${self.total_capital + self.total_pnl:.2f}"
        )
        self.save_state()

    # ── 트레일링 업데이트 ────────────────────────────────────

    def update_trail(self, price: float):
        if self.position:
            was_active = self.position.trail_active
            self.position.update_trail(price)
            if not was_active and self.position.trail_active:
                logger.info(
                    f"[트레일 활성] {self.position.symbol} | "
                    f"고점: {self.position.peak_price:.6f} | "
                    f"트레일SL: {self.position.trail_sl:.6f}"
                )

    # ── 익절 체크 ────────────────────────────────────────────

    def is_profit_lock_hit(self, price: float) -> bool:
        """수익 보존 락 — 고점 순수익의 일정 비율 아래로 떨어지면 즉시 확정

        왜 필요한가
          DCA 단계에서는 고정 익절이 꺼져 있고 트레일에만 의존한다. 그런데
          트레일 되돌림 거리의 하한(DCA_TRAIL_DIST_MIN = 코인 0.3%)이
          8배 레버리지에서 **포지션 2.4%포인트**에 해당해서, 비례 공식
          (수익 × 25%)은 수익 9.6% 이상에서만 작동한다. 그 아래에서는
          항상 2.4%포인트를 고정 반납하므로 고점이 +4% 를 넘지 않으면
          트레일 익절은 구조적으로 이익을 낼 수 없다.

          실제 사례: 2단계 $240 에서 순수익 $6 → 트레일 청산 $1.
          이 락이 있었다면 $3 에서 확정된다.

        안전성
          peak_realized 는 청산 비용까지 뺀 값이라 발동 시점에 반드시 이익이다.
          즉 이 규칙은 손실을 만들 수 없고, 이익을 줄일 수만 있다 —
          그것도 "더 큰 이익이 될 수도 있었던" 경우에 한해서.
        """
        if not config.PROFIT_LOCK_ENABLED:
            return False
        p = self.position
        if not p or p.peak_realized < config.PROFIT_LOCK_TRIGGER_USD:
            return False
        floor = p.peak_realized * config.PROFIT_LOCK_KEEP_RATIO
        if p.realized_pnl(price) <= floor:
            logger.info(
                f"[수익보존락] {p.symbol} 고점 순수익 ${p.peak_realized:+.2f} → "
                f"${p.realized_pnl(price):+.2f} (하한 ${floor:+.2f}) → 확정 청산"
            )
            return True
        return False

    def should_take_profit(self, price: float) -> bool:
        """
        일반 익절: ① pnl_pct >= TAKE_PROFIT_PCT  AND  ② realized_pnl >= MIN_PROFIT_USD
        트레일 익절: trail_active 상태에서 trail_sl 터치
        ※ USD 조건은 realized_pnl (진입+청산 비용 모두 차감) 기준
          → 청산 비용 미반영으로 손실 기록되는 문제 방지
        """
        p = self.position
        if not p:
            return False

        # 수익 보존 락 — 손에 쥘 수 있었던 수익을 되돌려주지 않는다.
        # 트레일보다 먼저 본다. 이 조건은 **이익 구간에서만** 발동하므로
        # 손실을 만들 수 없고, 늦게 반응하는 트레일의 구멍만 메운다.
        if self.is_profit_lock_hit(price):
            return True

        # 트레일링 손절가 터치 → 트레일 익절 (하드캡 포함 항상 유효)
        if p.is_trail_hit(price):
            return True

        # 급락 SHORT 모드: 트레일링만 사용 (일반 TP 조건 무시)
        if p.crash_short:
            return False   # 트레일 or 시간초과(engine에서 처리)만으로 청산

        # 불타기 완료 후 0단계: 트레일 전용 (standard TP 비활성)
        # 불타기 직후 같은 틱에서 standard TP가 발동해 손실 청산되는 버그 방지
        if p.pyramid_done and p.avg_down_step < config.DCA_TRAIL_STEP_THRESHOLD:
            return False

        # DCA 1단계 이상($120+): 횡보DCA 여부 무관하게 트레일링 전용
        #   sideways_dca는 항상 1단계 이상에서만 발동 → 고정TP 대신 트레일에 맡김
        #   +9% 이상 급등 → 트레일링에 맡겨 수익 극대화 (5% 익절 건너뜀)
        #   +5% ~ +8.9%  → 5% 확정 익절 (trail_sl이 avg 아래라 trail 못 믿음)
        if p.avg_down_step >= config.DCA_TRAIL_STEP_THRESHOLD:
            pnl = p.pnl_pct(price)
            if pnl >= config.DCA_TRAIL_SWITCH_PCT:
                return False   # 급등 → 트레일링이 잡을 때까지 보유
            if pnl >= config.DCA_TRAIL_MAX_PROFIT_PCT:
                return True    # 5~8.9% → 확정 익절
            return False

        # 일반 익절: % 조건 + $ 조건 동시 충족 (realized_pnl = 진입+청산 비용 모두 반영)
        # USD 조건은 예상 체결가(슬리피지 반영)로 계산 →
        # TP 체크 후 실제 체결 시 슬리피지로 손실 확정되는 문제 방지
        # (고단계 DCA에서 notional이 커질수록 slippage gap이 커짐: $16투입→gap $0.064)
        if p.trend == "DOWN":
            check_price = price * (1 + config.SLIPPAGE_RATE)  # SHORT 청산: 더 높은 가격으로 매수
        else:
            check_price = price * (1 - config.SLIPPAGE_RATE)  # LONG 청산: 더 낮은 가격으로 매도
        pct_ok = p.pnl_pct(price)            >= config.TAKE_PROFIT_PCT
        usd_ok = p.realized_pnl(check_price) >= config.MIN_PROFIT_USD
        return pct_ok and usd_ok

    def take_profit_reason(self, price: float) -> str:
        p = self.position
        if p and self.is_profit_lock_hit(price):
            return (f"수익보존락(고점${p.peak_realized:+.2f}→"
                    f"순${p.realized_pnl(price):+.2f})")
        if p and p.is_trail_hit(price):
            use_dca_trail = p.avg_down_step >= config.DCA_TRAIL_STEP_THRESHOLD
            prefix = f"DCA{p.avg_down_step}트레일" if use_dca_trail else "트레일"
            # 실제 청산가(trail_sl) 기준 realized_pnl로 익절/손절 판단
            # (현재가가 trail_sl 아래로 떨어진 시점의 net_pnl은 음수일 수 있어 오레이블 발생)
            exit_net = p.realized_pnl(p.trail_sl)
            if exit_net >= 0:
                return f"{prefix}익절(고점{p.peak_price:.4f}→SL{p.trail_sl:.4f}/순${exit_net:+.2f})"
            else:
                return f"{prefix}손절(SL{p.trail_sl:.4f}/순${exit_net:+.2f})"
        if p and p.sideways_dca and p.avg_down_step >= config.DCA_TRAIL_STEP_THRESHOLD:
            net = p.realized_pnl(price)
            return f"횡보DCA{p.avg_down_step}트레일익절(+{p.pnl_pct(price):.1%}/순${net:+.2f})"
        if (p and p.avg_down_step >= config.DCA_TRAIL_STEP_THRESHOLD
                and p.pnl_pct(price) >= config.DCA_TRAIL_MAX_PROFIT_PCT):
            return f"DCA{p.avg_down_step}최대익절(+{p.pnl_pct(price):.1%})"
        return "익절"

    # ── 물타기 ───────────────────────────────────────────────

    def should_avg_down(self, price: float) -> bool:
        p = self.position
        if not p:
            return False
        if p.total_invested >= p.max_position:
            return False
        if p.avg_down_step >= config.MAX_DCA_STAGES:
            return False
        # 트레일이 수익권일 때만 물타기 차단
        # LONG: trail_sl > avg_price → 수익권 보호 중
        # SHORT: trail_sl < avg_price → 수익권 보호 중 (반대 방향)
        if p.trail_active:
            trail_protecting = (
                p.trail_sl > p.avg_price if p.trend == "UP"
                else p.trail_sl < p.avg_price
            )
            if trail_protecting:
                return False

        triggers = {
            0: config.AVG_DOWN_STEP1_TRIGGER,
            1: config.AVG_DOWN_STEP2_TRIGGER,
            2: config.AVG_DOWN_STEP3_TRIGGER,
            3: config.AVG_DOWN_STEP4_TRIGGER,
            4: config.AVG_DOWN_STEP5_TRIGGER,
        }
        delta = p.pnl_pct(price) - p.step_ref_pnl
        if delta <= triggers.get(p.avg_down_step, -999):
            return True

        # 손실 기반 강제 투입 트리거
        if p.avg_down_step == 3 and p.net_pnl(price) <= config.DCA_STEP4_NET_LOSS_TRIGGER:
            logger.info(
                f"[4단계DCA-손실트리거] {p.symbol} | 순손익 ${p.net_pnl(price):.2f} "
                f"≤ ${config.DCA_STEP4_NET_LOSS_TRIGGER:.0f} → 4단계 투입"
            )
            return True
        if p.avg_down_step == 4 and p.net_pnl(price) <= config.DCA_STEP5_NET_LOSS_TRIGGER:
            logger.info(
                f"[5단계DCA-손실트리거] {p.symbol} | 순손익 ${p.net_pnl(price):.2f} "
                f"≤ ${config.DCA_STEP5_NET_LOSS_TRIGGER:.0f} → 5단계 투입"
            )
            return True

        return False

    def _adjust_dca_to_margin(self, symbol: str, planned_usd: float) -> float:
        """가용 마진 기반 DCA 금액 조정 (마진 부족 시 부분 투입).
        가용 마진 × PARTIAL_DCA_MARGIN_BUFFER 와 계획 금액 중 작은 값 사용.
        최소 금액(PARTIAL_DCA_MIN_USD) 미달 시 0 반환 → 호출부에서 DCA 포기.
        """
        try:
            available = self.live_api.get_balance()
            usable = available * config.PARTIAL_DCA_MARGIN_BUFFER
            if usable >= planned_usd:
                return planned_usd          # 마진 충분 → 계획대로
            if usable >= config.PARTIAL_DCA_MIN_USD:
                logger.warning(
                    f"[부분DCA] {symbol} | 가용마진 ${available:.2f} "
                    f"→ 계획 ${planned_usd:.2f} 대신 ${usable:.2f} 부분 투입"
                )
                return usable               # 부분 투입
            logger.warning(
                f"[DCA마진부족] {symbol} | 가용마진 ${available:.2f} < "
                f"최소 ${config.PARTIAL_DCA_MIN_USD:.2f} → DCA 포기"
            )
            return 0.0                      # 너무 적음 → 포기
        except Exception as e:
            logger.debug(f"[마진조회실패] {e} → 계획대로 진행")
            return planned_usd              # 조회 실패 시 계획대로

    def execute_avg_down(self, price: float):
        p = self.position
        if not p:
            return
        add_usd = p.next_avg_down_amount()
        add_usd = min(add_usd, p.max_position - p.total_invested)
        if add_usd <= 0:
            return
        # 실거래: 가용 마진 확인 후 부분 DCA 허용 → 주문 먼저 — 실패 시 DCA 건너뜀
        if self.live_api and config.LIVE_TRADING:
            add_usd = self._adjust_dca_to_margin(p.symbol, add_usd)
            if add_usd < config.PARTIAL_DCA_MIN_USD:
                return
            fill_price = _apply_slip(price, p.trend, entry=True)
            add_qty = (add_usd * config.LEVERAGE) / fill_price
            if not self._live_add(p.symbol, p.trend, add_qty):
                logger.error(f"[DCA건너뜀] {p.symbol} 실거래 주문 실패")
                return
        p.apply_avg_down(price, add_usd)
        self.sync_exchange_stop()   # 평단·수량 변경 → STOP 재배치
        self.save_state()

    def execute_sideways_avg_down(self, price: float):
        """횡보 DCA: 단계 유지 시간 초과 시 강제 평단 낮추기"""
        p = self.position
        if not p:
            return
        add_usd = p.next_avg_down_amount()
        add_usd = min(add_usd, p.max_position - p.total_invested)
        if add_usd <= 0:
            return
        # 실거래: 가용 마진 확인 후 부분 DCA 허용 → 주문 먼저 — 실패 시 DCA 건너뜀
        if self.live_api and config.LIVE_TRADING:
            add_usd = self._adjust_dca_to_margin(p.symbol, add_usd)
            if add_usd < config.PARTIAL_DCA_MIN_USD:
                return
            fill_price = _apply_slip(price, p.trend, entry=True)
            add_qty = (add_usd * config.LEVERAGE) / fill_price
            if not self._live_add(p.symbol, p.trend, add_qty):
                logger.error(f"[횡보DCA건너뜀] {p.symbol} 실거래 주문 실패")
                return
        p.apply_avg_down(price, add_usd, kind="sideways")
        p.sideways_dca = True   # TP 조건을 단순 +1%로 전환
        self.sync_exchange_stop()   # 평단·수량 변경 → STOP 재배치
        self.save_state()

    def execute_pyramid(self, price: float):
        """불타기: 트레일 활성화 시 자본 비례 추가 진입 (0·1단계 각 1회)
        기준 $1000 → $30(0단계)/$18(1단계), $7800 → $234/$140
        """
        p = self.position
        if not p or p.pyramid_done:
            return
        current_cap = self.total_capital + self.total_pnl
        scale       = current_cap / config.DYNAMIC_SEED_BASE_CAPITAL
        ratio       = (config.PYRAMID_RATIO if p.avg_down_step == 0
                       else config.PYRAMID_RATIO_S1)
        add_usd     = config.INITIAL_POSITION_USD * ratio * scale
        p.apply_pyramid(price, add_usd)
        self.sync_exchange_stop()   # 수량 변경 → STOP 재배치
        self.save_state()

    # ── 급락 감지 ────────────────────────────────────────────

    def update_price_hist(self, price: float, volume: float):
        if not self.position:
            return
        self.position.price_hist.append((time.time(), price, volume))
        if len(self.position.price_hist) > 300:
            self.position.price_hist = self.position.price_hist[-300:]
        self._track_excursion(price)

    def _track_excursion(self, price: float):
        """MFE/MAE 갱신 — 저널 분석 전용. 매매 판단에는 쓰이지 않는다."""
        p = self.position
        if not p:
            return
        try:
            net   = p.net_pnl(price)
            at_s  = time.time() - p.open_time
            if net > p.mfe_usd:
                p.mfe_usd      = net
                p.mfe_coin_pct = p.coin_pct(price)
                p.mfe_at_s     = at_s
            if net < p.mae_usd:
                p.mae_usd      = net
                p.mae_coin_pct = p.coin_pct(price)
                p.mae_at_s     = at_s
            # 수익 보존 락 기준 — 계측이 아니라 실제 청산 로직이 쓰는 값이다.
            # 청산 비용까지 뺀 realized 기준이라 "지금 나가면 손에 쥐는 돈".
            realized = p.realized_pnl(price)
            if realized > p.peak_realized:
                p.peak_realized = realized
        except Exception:
            pass  # 계측 실패가 매매를 막아선 안 된다

    def is_flash_crash(self) -> bool:
        p = self.position
        if not p or len(p.price_hist) < 2:
            return False
        now    = time.time()
        window = config.FLASH_CRASH_WINDOW_MIN * 60
        recent = [(t, pr, v) for t, pr, v in p.price_hist if now - t <= window]
        if len(recent) < 2:
            return False
        prices  = [pr for _, pr, _ in recent]
        volumes = [v  for _, _,  v in recent]
        change   = (prices[-1] - prices[0]) / prices[0]
        avg_vol  = float(np.mean(volumes))
        last_vol = volumes[-1]
        crash    = change <= config.FLASH_CRASH_COIN_PCT
        vol_spike= avg_vol > 0 and last_vol >= avg_vol * config.FLASH_CRASH_VOL_MULT
        if crash and vol_spike:
            logger.warning(
                f"[급락감지] 변화: {change:.2%} | 거래량배율: {last_vol/avg_vol:.1f}x"
            )
            return True
        return False

    # ── 최대 손실 한도 손절 ──────────────────────────────────

    def is_max_loss_stop(self, price: float) -> bool:
        """단계 무관 순손익이 동적 손절 한도 이하면 즉시 손절"""
        p = self.position
        if not p:
            return False
        threshold = self._get_max_loss_usd()
        net = p.net_pnl(price)
        if net <= threshold:
            logger.warning(
                f"[최대손실손절] {p.symbol} | 순손익 ${net:.2f} ≤ ${threshold:.0f} "
                f"→ 즉시 손절 (투입 ${p.total_invested:.0f} / {p.avg_down_step}단계)"
            )
            return True
        return False

    # ── 하드캡 손절 ──────────────────────────────────────────

    def is_hard_cap_stop(self, price: float) -> bool:
        p = self.position
        if not p or p.total_invested < p.max_position:
            return False
        if p.hard_cap_price == 0:
            return False
        ref = p.hard_cap_price
        change = ((price - ref) / ref) if p.trend == "UP" else ((ref - price) / ref)
        if change <= config.HARD_CAP_STOP_COIN_PCT:
            logger.warning(
                f"[하드캡손절] 기준가: {ref:.6f} | 현재가: {price:.6f} | 변화: {change:.2%}"
            )
            return True
        return False

    # ── 코인 교체 체크 ───────────────────────────────────────

    def check_trend_reversal(self, closes: list, strict: bool = False) -> bool:
        """
        MA 기울기로 추세 반전 감지
        strict=True : DCA 손실 중 이탈용 — 임계값 3배 강화 (오신호 방지)
        strict=False: 수익 구간 코인 교체용 — 기본 민감도
        """
        period = config.MA_PERIOD
        p = self.position
        if not p or len(closes) < period + 1:
            return False
        arr    = np.array(closes, dtype=float)
        ma_old = float(np.mean(arr[-(period + 1):-1]))
        ma_now = float(np.mean(arr[-period:]))
        slope  = (ma_now - ma_old) / ma_old
        threshold = 0.003 if strict else 0.001
        tag = "[트렌드반전-강]" if strict else "[트렌드반전]"
        if p.trend == "UP" and slope < -threshold:
            logger.info(f"{tag} {p.symbol} 상승→하락 (slope {slope:.4f})")
            return True
        if p.trend == "DOWN" and slope > threshold:
            logger.info(f"{tag} {p.symbol} 하락→상승 (slope {slope:.4f})")
            return True
        return False

    def check_volume_dryup(self, hourly_volumes: list) -> bool:
        p = self.position
        if not p or len(hourly_volumes) < 5:
            return False
        avg_vol = float(np.mean(hourly_volumes[:-2]))
        low     = all(v <= avg_vol * config.LOW_VOLUME_THRESHOLD for v in hourly_volumes[-2:])
        if low:
            if p.low_vol_start == 0:
                p.low_vol_start = time.time()
            if (time.time() - p.low_vol_start) / 60 >= config.LOW_VOLUME_DURATION_MIN:
                logger.info(f"[거래량고갈] {p.symbol}")
                return True
        else:
            p.low_vol_start = 0.0
        return False

    # ── 현황 ─────────────────────────────────────────────────

    def get_status(self, current_price: float = 0.0) -> dict:
        total    = self.win_count + self.loss_count
        win_rate = (self.win_count / total * 100) if total else 0.0
        status = {
            "current_capital":  round(self.total_capital + self.total_pnl, 2),
            "total_pnl":        round(self.total_pnl, 2),
            "total_trades":     total,
            "win_count":        self.win_count,
            "loss_count":       self.loss_count,
            "win_rate":         round(win_rate, 1),
            "total_withdrawn":  round(self.total_withdrawn, 2),
            "withdrawal_count": self.withdrawal_count,
            "position":         None,
        }
        if self.position and current_price > 0:
            p = self.position
            net = p.realized_pnl(current_price)   # 진입+청산 비용 모두 반영
            status["position"] = {
                "symbol":         p.symbol,
                "trend":          p.trend,
                "avg_price":      round(p.avg_price, 6),
                "current_price":  round(current_price, 6),
                "total_invested": round(p.total_invested, 2),
                "avg_down_step":  p.avg_down_step,
                "pnl_pct":        round(p.pnl_pct(current_price) * 100, 2),
                "gross_pnl":      round(p.gross_pnl(current_price), 2),
                "cost":           round(p._total_cost(), 4),  # 전체 비용 표시
                "net_pnl":        round(net, 2),
                "trail_active":   p.trail_active,
                "peak_price":     round(p.peak_price, 6),
                "trail_sl":       round(p.trail_sl, 6) if p.trail_active else 0.0,
                "max_position":   round(p.max_position, 2),
                "initial_invest": round(p.initial_invest, 2),
            }
        return status

    def print_report(self):
        s   = self.get_status()
        sep = "=" * 56
        logger.info(sep)
        logger.info("  [결산 리포트]")
        logger.info(sep)
        logger.info(f"  현재 자본:     ${s['current_capital']:.2f}")
        logger.info(f"  총 순손익:     ${s['total_pnl']:+.2f}")
        if s['withdrawal_count'] > 0:
            logger.info(f"  총 출금:       ${s['total_withdrawn']:.2f} ({s['withdrawal_count']}회)")
        logger.info(f"  총 거래:       {s['total_trades']}회")
        logger.info(f"  익절/손절:     {s['win_count']}W / {s['loss_count']}L")
        logger.info(f"  승률:          {s['win_rate']}%")
        if self.closed_trades:
            wins     = [t["pnl"] for t in self.closed_trades if t["pnl"] > 0]
            losses   = [t["pnl"] for t in self.closed_trades if t["pnl"] < 0]
            costs    = sum(t.get("cost", 0) for t in self.closed_trades)
            trail_wins = sum(1 for t in self.closed_trades if "트레일" in t.get("reason",""))
            if wins:
                logger.info(f"  평균 익절:     ${sum(wins)/len(wins):.2f}")
            if losses:
                logger.info(f"  평균 손절:     ${sum(losses)/len(losses):.2f}")
            logger.info(f"  누적 비용:     ${costs:.2f}  (수수료+슬리피지)")
            logger.info(f"  트레일 익절:   {trail_wins}회")
        logger.info(sep)
