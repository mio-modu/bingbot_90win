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
import os
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

import config

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

    # 급락 감지용 가격 히스토리: [timestamp, price, volume]
    price_hist:      list   = field(default_factory=list)
    low_vol_start:   float  = 0.0

    @property
    def max_position(self) -> float:
        """하드캡: 진입 시 시드 × 20 (5단계 기준)"""
        base = self.initial_invest if self.initial_invest > 0 else config.INITIAL_POSITION_USD
        return base * 20

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
        amounts = {0: base, 1: base * 2, 2: base * 4, 3: base * 8, 4: base * 4}
        return amounts.get(self.avg_down_step, 0.0)

    # ── 물타기 실행 ──────────────────────────────────────────

    def apply_avg_down(self, raw_price: float, add_usd: float):
        """물타기: 슬리피지 적용 후 평균단가 재계산"""
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

        if self.total_invested >= self.max_position:
            self.hard_cap_price = raw_price

        # DCA로 avg_price가 낮아져 trail_sl이 평균단가 아래로 내려간 경우
        # 트레일이 더 이상 수익을 지킬 수 없으므로 리셋
        if self.trail_active and self.trail_sl <= self.avg_price:
            old_sl = self.trail_sl
            self.trail_active = False
            self.trail_sl     = 0.0
            self.peak_price   = self.avg_price
            logger.info(
                f"[트레일 리셋] DCA {self.avg_down_step}단계 후 "
                f"trail_sl({old_sl:.6f}) < avg_price({self.avg_price:.6f}) "
                f"→ 트레일 무효화"
            )

        slip_usd = add_usd * config.LEVERAGE * config.SLIPPAGE_RATE
        fee_usd  = add_usd * config.LEVERAGE * config.TAKER_FEE_RATE
        logger.info(
            f"[물타기 {self.avg_down_step}단계] {self.symbol} | "
            f"추가: ${add_usd:.0f} | 체결가: {fill_price:.6f} | "
            f"평균단가: {self.avg_price:.6f} | 총투입: ${self.total_invested:.0f} | "
            f"슬리피지: ${slip_usd:.3f} | 수수료: ${fee_usd:.3f}"
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
    def __init__(self):
        self.total_capital: float = config.TOTAL_CAPITAL
        self.position: Optional[Position] = None
        self.total_pnl:  float = 0.0
        self.win_count:  int   = 0
        self.loss_count: int   = 0
        self.closed_trades: list = []
        self._load_state()

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
            }
        data = {
            "total_pnl":     self.total_pnl,
            "win_count":     self.win_count,
            "loss_count":    self.loss_count,
            "closed_trades": self.closed_trades[-200:],
            "position":      pos_data,
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
                self.total_pnl     = float(data.get("total_pnl", 0.0))
                self.win_count     = int(data.get("win_count", 0))
                self.loss_count    = int(data.get("loss_count", 0))
                self.closed_trades = list(data.get("closed_trades", []))
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
        """자본 증가에 따른 동적 시드 계산"""
        capital    = self.total_capital + self.total_pnl
        increments = max(0, int((capital - config.DYNAMIC_SEED_BASE_CAPITAL)
                                / config.DYNAMIC_SEED_STEP_CAPITAL))
        return config.INITIAL_POSITION_USD + increments * config.DYNAMIC_SEED_STEP_USD

    def open_position(self, symbol: str, trend: str, raw_price: float,
                      invest_override: float = None,
                      crash_short: bool = False) -> Position:
        invest     = invest_override if invest_override is not None \
                     else self._get_initial_position_usd()
        fill_price = _apply_slip(raw_price, trend, entry=True)
        qty        = (invest * config.LEVERAGE) / fill_price
        fee        = invest * config.LEVERAGE * config.TAKER_FEE_RATE
        slip_usd   = invest * config.LEVERAGE * config.SLIPPAGE_RATE

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
        )
        logger.info(
            f"{tag} {symbol} ({trend}) | 호가: {raw_price:.6f} → "
            f"체결가: {fill_price:.6f} | 투입: ${invest:.0f} | "
            f"수수료: ${fee:.3f} | 슬리피지: ${slip_usd:.3f}"
        )
        self.save_state()
        return self.position

    # ── 청산 ─────────────────────────────────────────────────

    def close_position(self, raw_price: float, reason: str) -> dict:
        p = self.position
        if not p:
            return {}

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
        self.position = None

        icon = "✅" if pnl > 0 else "❌"
        logger.info(
            f"[청산 {icon}] {trade['symbol']} | 사유: {reason} | "
            f"호가: {raw_price:.6f} → 체결가: {fill_price:.6f} | "
            f"총손익: ${trade['gross_pnl']:+.4f} | 비용: ${cost:.4f} | "
            f"순손익: ${pnl:+.4f} | 누적: ${self.total_pnl:+.2f}"
        )
        self.save_state()
        return trade

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

        # 트레일링 손절가 터치 → 트레일 익절 (하드캡 포함 항상 유효)
        if p.is_trail_hit(price):
            return True

        # 급락 SHORT 모드: 트레일링만 사용 (일반 TP 조건 무시)
        if p.crash_short:
            return False   # 트레일 or 시간초과(engine에서 처리)만으로 청산

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
        pct_ok = p.pnl_pct(price)    >= config.TAKE_PROFIT_PCT
        usd_ok = p.realized_pnl(price) >= config.MIN_PROFIT_USD
        return pct_ok and usd_ok

    def take_profit_reason(self, price: float) -> str:
        p = self.position
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
        # 트레일이 수익권(trail_sl > avg_price)일 때만 물타기 차단
        # trail_sl이 avg_price 아래면 이미 손실 구간 → 물타기 허용
        if p.trail_active and p.trail_sl > p.avg_price:
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

    def execute_avg_down(self, price: float):
        p = self.position
        if not p:
            return
        add_usd = p.next_avg_down_amount()
        add_usd = min(add_usd, p.max_position - p.total_invested)
        if add_usd <= 0:
            return
        p.apply_avg_down(price, add_usd)
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
        p.apply_avg_down(price, add_usd)
        p.sideways_dca = True   # TP 조건을 단순 +1%로 전환
        self.save_state()

    # ── 급락 감지 ────────────────────────────────────────────

    def update_price_hist(self, price: float, volume: float):
        if not self.position:
            return
        self.position.price_hist.append((time.time(), price, volume))
        if len(self.position.price_hist) > 300:
            self.position.price_hist = self.position.price_hist[-300:]

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
        """단계 무관 순손익이 MAX_NET_LOSS_USD 이하면 즉시 손절"""
        p = self.position
        if not p:
            return False
        net = p.net_pnl(price)
        if net <= config.MAX_NET_LOSS_USD:
            logger.warning(
                f"[최대손실손절] {p.symbol} | 순손익 ${net:.2f} ≤ ${config.MAX_NET_LOSS_USD:.0f} "
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
            "current_capital": round(self.total_capital + self.total_pnl, 2),
            "total_pnl":       round(self.total_pnl, 2),
            "total_trades":    total,
            "win_count":       self.win_count,
            "loss_count":      self.loss_count,
            "win_rate":        round(win_rate, 1),
            "position":        None,
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
