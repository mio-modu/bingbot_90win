"""
리스크 거버너 — 자본을 지키는 상위 감독자
─────────────────────────────────────────
전략 엔진 위에 앉아서 "지금 얼마를 걸어도 되는가 / 걸어도 되는가"만 판단한다.
진입 여부·코인 선택·청산 타이밍에는 일절 관여하지 않는다.

왜 필요한가 (LESSONS.md 참고)
  gateio_90win 은 $100 → $518 (3일) → $234 (2일) 를 겪었다.
  수익을 내는 능력은 증명됐고, 무너진 건 그걸 지키는 쪽이었다.
  이 모듈이 그때 있었다면 $518 고점에서 15% 빠진 $440 부근에서 시드가 반으로
  줄고, 20% 빠진 $414 에서 매매가 멈췄을 것이다.

세 가지 브레이크
  ① 고점 대비 낙폭 (drawdown)  — 계좌 최고점에서 얼마나 내려왔나
  ② 연속 손실                  — 연달아 지고 있으면 판돈을 줄인다
  ③ 당일 수익 반납             — 오늘 번 것을 되돌려주기 시작하면 그날은 접는다

세 가지 모두 **구조적 규칙**이다. 과거 데이터에 맞춰 깎은 값이 아니라
"많이 잃는 중이면 적게 건다"는 원칙의 구현이므로 과최적화 위험이 없다.

동작 방식
  각 브레이크가 시드 배율(0.0 ~ 1.0)을 내놓고, 그중 **가장 낮은 값**을 쓴다.
  배율 0.0 = 신규 진입 금지 (보유 포지션은 건드리지 않는다 — 청산은 전략의 몫).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
STATE_FILE = os.path.join(os.path.dirname(__file__), "governor_state.json")


class RiskGovernor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.peak_equity:      float = 0.0   # 역대 최고 확정 자본
        self.day_start_equity: float = 0.0   # 오늘 시작 자본
        self.day_peak_equity:  float = 0.0   # 오늘 중 최고 자본
        self.day_key:          str   = ""    # YYYY-MM-DD (KST)
        self.consec_losses:    int   = 0
        self.halt_until:       float = 0.0   # 이 시각까지 신규 진입 금지
        self.halt_reason:      str   = ""
        self.recovery_mode:    bool  = False # 중지 해제 후 최소 규모 재개 중
        self._last_logged_mult: float = -1.0
        self._load()

    # ── 상태 저장/복원 ────────────────────────────────────────
    # 재시작해도 고점과 연속 손실이 리셋되면 브레이크가 풀려버린다.

    def _load(self):
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                d = json.load(f)
            self.peak_equity      = float(d.get("peak_equity", 0.0))
            self.day_start_equity = float(d.get("day_start_equity", 0.0))
            self.day_peak_equity  = float(d.get("day_peak_equity", 0.0))
            self.day_key          = str(d.get("day_key", ""))
            self.consec_losses    = int(d.get("consec_losses", 0))
            self.halt_until       = float(d.get("halt_until", 0.0))
            self.halt_reason      = str(d.get("halt_reason", ""))
            self.recovery_mode    = bool(d.get("recovery_mode", False))
            logger.info(
                f"[거버너] 복원 | 고점 ${self.peak_equity:,.2f} | "
                f"연속손실 {self.consec_losses}회"
                + (f" | 매매중지 {(self.halt_until-time.time())/60:.0f}분 남음"
                   if self.halt_until > time.time() else "")
            )
        except Exception as e:
            logger.warning(f"[거버너] 상태 복원 실패(초기값 사용): {e}")

    def _save(self):
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "peak_equity":      round(self.peak_equity, 4),
                    "day_start_equity": round(self.day_start_equity, 4),
                    "day_peak_equity":  round(self.day_peak_equity, 4),
                    "day_key":          self.day_key,
                    "consec_losses":    self.consec_losses,
                    "halt_until":       round(self.halt_until, 1),
                    "halt_reason":      self.halt_reason,
                    "recovery_mode":    self.recovery_mode,
                }, f, ensure_ascii=False, indent=2)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            logger.warning(f"[거버너] 상태 저장 실패: {e}")

    # ── 자본 갱신 ─────────────────────────────────────────────

    def update_equity(self, equity: float):
        """확정 자본(= total_capital + total_pnl)을 매 틱 전달받는다."""
        if equity <= 0:
            return
        today = datetime.now(KST).strftime("%Y-%m-%d")
        if today != self.day_key:
            # 날짜가 바뀌면 당일 기준선을 새로 잡는다.
            # 전날 수익반납으로 걸린 중지도 함께 푼다 (하루 단위 규칙이므로).
            self.day_key          = today
            self.day_start_equity = equity
            self.day_peak_equity  = equity
            if self.halt_until > time.time() and "당일" in self.halt_reason:
                self.halt_until  = 0.0
                self.halt_reason = ""
                logger.info("[거버너] 날짜 변경 — 당일 수익반납 중지 해제")
            logger.info(f"[거버너] {today} 시작 자본 ${equity:,.2f}")
            self._save()

        # tick 마다(10초) 호출되므로 실제로 값이 바뀔 때만 디스크에 쓴다
        new_peak     = max(self.peak_equity, equity)
        new_day_peak = max(self.day_peak_equity, equity)
        if new_peak != self.peak_equity or new_day_peak != self.day_peak_equity:
            self.peak_equity     = new_peak
            self.day_peak_equity = new_day_peak
            self._save()

    def record_trade(self, pnl: float):
        """청산 결과를 알려준다 — 연속 손실 카운터 및 회복 모드 갱신"""
        if pnl > 0:
            if self.consec_losses or self.recovery_mode:
                logger.info(
                    f"[거버너] 익절 — 연속손실 {self.consec_losses} → 0"
                    + (", 회복 모드 해제 (시드 100% 복귀)" if self.recovery_mode else "")
                )
            self.consec_losses = 0
            self.recovery_mode = False
            self.halt_until    = 0.0
            self.halt_reason   = ""
        else:
            self.consec_losses += 1
            logger.info(f"[거버너] 손절 — 연속손실 {self.consec_losses}회")
            # 회복 모드(최소 규모 재개) 중에 또 졌다면 다시 중지한다.
            # 그대로 두면 최소 규모로 무한정 흘리게 된다.
            if self.recovery_mode:
                self.halt_until = 0.0   # _halt 의 "더 긴 중지 유지" 가드 우회
                self._halt(self.cfg.GOVERNOR_HALT_MIN,
                           f"회복 모드 재진입 실패 (연속손실 {self.consec_losses}회)")
        self._save()

    # ── 브레이크 계산 ─────────────────────────────────────────

    def _drawdown_mult(self, equity: float) -> tuple[float, str]:
        """고점 대비 낙폭에 따른 시드 배율"""
        if self.peak_equity <= 0:
            return 1.0, ""
        dd = (self.peak_equity - equity) / self.peak_equity
        for threshold, mult in self.cfg.GOVERNOR_DRAWDOWN_LADDER:
            if dd >= threshold:
                return mult, (f"고점 ${self.peak_equity:,.0f} 대비 {dd:.1%} 하락")
        return 1.0, ""

    def _consec_loss_mult(self) -> tuple[float, str]:
        """연속 손실에 따른 시드 배율"""
        for n, mult in self.cfg.GOVERNOR_CONSEC_LOSS_LADDER:
            if self.consec_losses >= n:
                return mult, f"연속 손실 {self.consec_losses}회"
        return 1.0, ""

    def _giveback_check(self, equity: float) -> tuple[bool, str]:
        """당일 수익 반납 감지 — 벌었다가 되돌려주기 시작하면 그날은 접는다

        오늘 고점까지 올린 수익 중 몇 %를 반납했는지 본다.
        수익이 GOVERNOR_GIVEBACK_MIN_GAIN 미만이면 판단하지 않는다
        (푼돈 수익에 대고 하루를 접을 이유는 없다).
        """
        gain_at_peak = self.day_peak_equity - self.day_start_equity
        if gain_at_peak < self.cfg.GOVERNOR_GIVEBACK_MIN_GAIN:
            return False, ""
        given_back = self.day_peak_equity - equity
        ratio = given_back / gain_at_peak
        if ratio >= self.cfg.GOVERNOR_GIVEBACK_RATIO:
            return True, (f"당일 고점 ${self.day_peak_equity:,.0f} 에서 "
                          f"수익 ${gain_at_peak:,.0f} 중 {ratio:.0%}(${given_back:,.0f}) 반납")
        return False, ""

    # ── 외부 인터페이스 ───────────────────────────────────────

    def seed_multiplier(self, equity: float) -> float:
        """지금 걸어도 되는 시드 배율 (0.0 = 진입 금지)

        ※ 데드락 방지가 이 함수의 핵심이다.
          "4연패 → 정지" 를 그대로 두면 진입이 막혀 이길 기회가 없고,
          이기지 못하니 연패 카운터가 영영 안 풀린다. 낙폭 정지도 같다 —
          자본은 거래로만 회복되는데 거래가 막혀 있으면 낙폭이 영구히 남는다.
          그래서 중지가 끝나면 조건이 아직 살아 있어도 **최소 규모로 재개**한다.
          완전 복귀는 익절 1회로만 이루어진다.
        """
        if not self.cfg.GOVERNOR_ENABLED:
            return 1.0

        now = time.time()
        if self.halt_until > now:
            return 0.0

        dd_mult, dd_why = self._drawdown_mult(equity)
        cl_mult, cl_why = self._consec_loss_mult()
        mult, why = (dd_mult, dd_why) if dd_mult <= cl_mult else (cl_mult, cl_why)

        # 수익 반납 감지 → 당일 매매 중지 (익일 자동 해제)
        giveback, gb_why = self._giveback_check(equity)
        if giveback and not self.recovery_mode:
            self._halt(self.cfg.GOVERNOR_GIVEBACK_HALT_MIN, gb_why)
            return 0.0

        if mult == 0.0:
            if self.recovery_mode:
                # 중지를 이미 겪었다 → 재중지하지 않고 최소 규모로 재개.
                # 여기서 다시 지면 record_trade() 가 또 중지를 건다.
                mult = self.cfg.GOVERNOR_RECOVERY_MULT
                why  = f"{why or self.halt_reason} — 회복 모드(익절 1회 시 복귀)"
            else:
                self._halt(self.cfg.GOVERNOR_HALT_MIN, why or "리스크 한도 도달")
                return 0.0

        if mult != self._last_logged_mult:
            if mult < 1.0:
                logger.warning(f"[거버너] 시드 {mult:.0%} 로 축소 — {why}")
            else:
                logger.info("[거버너] 시드 100% 복귀 — 브레이크 해제")
            self._last_logged_mult = mult
        return mult

    def _halt(self, minutes: float, reason: str):
        until = time.time() + minutes * 60
        if until <= self.halt_until:
            return   # 이미 더 긴 중지가 걸려 있음
        self.halt_until    = until
        self.halt_reason   = reason
        self.recovery_mode = True   # 중지 해제 후엔 최소 규모로 재개
        logger.warning(
            f"[거버너] 신규 진입 {minutes:.0f}분 중지 — {reason} "
            f"(해제 후 시드 {self.cfg.GOVERNOR_RECOVERY_MULT:.0%} 로 재개, "
            f"익절 1회 시 완전 복귀 / 보유 포지션은 전략이 계속 관리한다)"
        )
        self._save()

    def can_enter(self, equity: float) -> tuple[bool, str]:
        """신규 진입 허용 여부"""
        if not self.cfg.GOVERNOR_ENABLED:
            return True, ""
        if self.seed_multiplier(equity) <= 0.0:
            rem = max(0.0, (self.halt_until - time.time()) / 60)
            return False, f"{self.halt_reason} ({rem:.0f}분 남음)"
        return True, ""

    def status_line(self, equity: float) -> str:
        """상태 출력용 한 줄"""
        if not self.cfg.GOVERNOR_ENABLED:
            return "거버너 비활성"
        dd = ((self.peak_equity - equity) / self.peak_equity
              if self.peak_equity > 0 else 0.0)
        mult = self.seed_multiplier(equity)
        day_pnl = equity - self.day_start_equity if self.day_start_equity else 0.0
        s = (f"고점 ${self.peak_equity:,.0f} (낙폭 {dd:.1%}) | "
             f"당일 ${day_pnl:+,.0f} | 연속손실 {self.consec_losses} | "
             f"시드 {mult:.0%}")
        if self.halt_until > time.time():
            s += f" | 중지 {(self.halt_until-time.time())/60:.0f}분"
        return s
