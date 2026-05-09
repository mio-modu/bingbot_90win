"""
전략 엔진 (BingX 실물거래 버전)
상태: IDLE → SCANNING → IN_POSITION → (익절/손절/교체) → IDLE
"""

import json
import logging
import os
import time
from enum import Enum

from bingx_api import BingXAPI
from coin_scanner import CoinScanner
from bingx_trader import BingxTrader
import config
from config import (
    SCAN_INTERVAL_MIN, RESCAN_AFTER_EXIT_MIN,
    MAX_COIN_DURATION_MIN,
    FLAT_TIMEOUT_MIN, FLAT_THRESHOLD_USD, FLAT_BLOCK_MIN,
    UPGRADE_SCAN_MIN, UPGRADE_SCORE_MULT, UPGRADE_MAX_LOSS_USD,
    SIDEWAYS_DCA_WAIT_PER_STEP, SIDEWAYS_LAST_STAGE_TIMEOUT_MIN,
    SIDEWAYS_DCA_MIN_STEP, SIDEWAYS_BLOCK_MIN,
    SIDEWAYS_DCA_TP_PCT, SIDEWAYS_DCA_MIN_NET,
    BTC_SHOCK_PCT, BTC_SHOCK_WINDOW_MIN, BTC_SHOCK_BLOCK_MIN,
    ADVERSE_CANDLE_BLOCK,
    CRASH_SHORT_SEED_RATIO, CRASH_TRAIL_ACTIVATE_PCT,
    CRASH_TRAIL_DISTANCE_PCT, CRASH_MAX_DURATION_MIN, CRASH_MAX_RETRIES,
)

ENGINE_STATE_FILE = os.path.join(os.path.dirname(__file__), "engine_state.json")

logger = logging.getLogger(__name__)


class BotState(Enum):
    IDLE        = "IDLE"
    SCANNING    = "SCANNING"
    IN_POSITION = "IN_POSITION"


class StrategyEngine:
    def __init__(self):
        self.api     = BingXAPI()
        self.scanner = CoinScanner(self.api)
        self.pt      = BingxTrader(self.api)
        self.state   = BotState.IN_POSITION if self.pt.position else BotState.IDLE
        self._last_scan_time  = 0.0
        self._last_exit_time  = 0.0
        self._current_coin_enter_time = (
            self.pt.position.open_time if self.pt.position else 0.0
        )
        # 횡보/교체로 차단된 코인: {symbol: 차단해제_timestamp}
        self._blocked_symbols: dict = {}
        # 포지션 중 업그레이드 스캔
        self._last_upgrade_scan_time = 0.0
        self._current_coin_score: float = 0.0  # 진입 시 코인 점수 저장
        self._last_reversal_check_time = 0.0   # 트렌드 반전 체크 쓰로틀 (5분 1회)
        # BTC 시장 충격 감지
        self._btc_price_hist: list = []         # [(timestamp, btc_price), ...]
        self._market_shock_until: float = 0.0  # 시장충격 진입차단 해제 시간
        # 급락 SHORT 모드
        self._in_crash_mode: bool  = False      # 급락 SHORT 모드 활성
        self._crash_retry_count: int = 0        # 이번 급락에서 재진입 횟수
        self._crash_mode_start: float = 0.0    # 급락 모드 시작 시각
        # 4·5단계 DCA 일일 횟수 제한
        self._dca_step4_today_count: int = 0
        self._dca_step4_date: str = ""
        self._dca_step5_today_count: int = 0
        self._dca_step5_date: str = ""
        self._load_engine_state()

    # ────────────────────────────────────────────────
    #  엔진 상태 저장/복원 (blocked_symbols 재시작 보존)
    # ────────────────────────────────────────────────
    def _save_engine_state(self):
        now = time.time()
        # 이미 만료된 항목 제외하고 저장
        active = {s: t for s, t in self._blocked_symbols.items() if t > now}
        try:
            with open(ENGINE_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"blocked_symbols": active}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[engine_state] 저장 실패: {e}")

    def _load_engine_state(self):
        if not os.path.exists(ENGINE_STATE_FILE):
            return
        try:
            with open(ENGINE_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            now = time.time()
            self._blocked_symbols = {
                s: float(t)
                for s, t in data.get("blocked_symbols", {}).items()
                if float(t) > now
            }
            if self._blocked_symbols:
                remaining = {s: round((t - now) / 60, 1)
                             for s, t in self._blocked_symbols.items()}
                logger.info(f"[engine_state] 차단 코인 복원: {remaining} (분 남음)")
        except Exception as e:
            logger.warning(f"[engine_state] 로드 실패: {e}")

    # ────────────────────────────────────────────────
    #  BTC 시장 충격 감지
    # ────────────────────────────────────────────────
    def _update_btc_and_check_shock(self) -> bool:
        """
        BTC 가격 히스토리 업데이트 + 시장 충격 감지.
        BTC_SHOCK_WINDOW_MIN 내 BTC_SHOCK_PCT 이상 급락 시 True 반환.
        충격 감지 시 _market_shock_until 설정 → IDLE 상태에서 진입 차단.
        """
        try:
            btc_price = self.api.get_price("BTC-USDT")
            now = time.time()
            self._btc_price_hist.append((now, btc_price))
            cutoff = now - BTC_SHOCK_WINDOW_MIN * 60
            self._btc_price_hist = [(t, p) for t, p in self._btc_price_hist if t >= cutoff]

            if len(self._btc_price_hist) < 2:
                return False

            oldest_price = self._btc_price_hist[0][1]
            change = (btc_price - oldest_price) / oldest_price

            if change <= BTC_SHOCK_PCT:
                logger.warning(
                    f"[BTC시장충격] BTC {BTC_SHOCK_WINDOW_MIN}분 변화: {change:.2%} "
                    f"(기준 {BTC_SHOCK_PCT:.0%}) → 급락SHORT모드 진입"
                )
                self._market_shock_until = now + BTC_SHOCK_BLOCK_MIN * 60
                # 급락 SHORT 모드 활성화 (아직 아닐 때만)
                if not self._in_crash_mode:
                    self._in_crash_mode    = True
                    self._crash_retry_count = 0
                    self._crash_mode_start  = now
                    logger.warning(
                        f"[급락모드진입] BTC {change:.2%} 급락 → "
                        f"전체자본 {CRASH_SHORT_SEED_RATIO:.0%}(${(self.pt.total_capital + self.pt.total_pnl) * CRASH_SHORT_SEED_RATIO:.0f}) SHORT 준비"
                    )
                return True
        except Exception as e:
            logger.debug(f"BTC 가격 조회 실패: {e}")
        return False

    # ────────────────────────────────────────────────
    #  4단계 DCA 강화 조건 체크
    # ────────────────────────────────────────────────
    def _is_btc_declining(self) -> bool:
        """BTC가 최근 N분간 X% 이상 하락 중이면 True (4단계 DCA 차단용)"""
        if len(self._btc_price_hist) < 2:
            return False
        now = time.time()
        window = config.BTC_DCA4_WINDOW_MIN * 60
        recent = [(t, p) for t, p in self._btc_price_hist if now - t <= window]
        if len(recent) < 2:
            return False
        change = (recent[-1][1] - recent[0][1]) / recent[0][1]
        if change <= config.BTC_DCA4_DROP_PCT:
            logger.info(
                f"[BTC하락감지] {change:.2%} ({config.BTC_DCA4_WINDOW_MIN}분) "
                f"→ 4단계 DCA 차단"
            )
            return True
        return False

    def _check_step4_daily_limit(self) -> bool:
        today = time.strftime("%Y-%m-%d")
        if self._dca_step4_date != today:
            self._dca_step4_date = today
            self._dca_step4_today_count = 0
        return self._dca_step4_today_count < config.DCA_STEP4_DAILY_MAX

    def _record_step4_dca(self):
        today = time.strftime("%Y-%m-%d")
        if self._dca_step4_date != today:
            self._dca_step4_date = today
            self._dca_step4_today_count = 0
        self._dca_step4_today_count += 1
        logger.info(f"[4단계DCA] 오늘 {self._dca_step4_today_count}/{config.DCA_STEP4_DAILY_MAX}회 사용")

    def _check_step5_daily_limit(self) -> bool:
        today = time.strftime("%Y-%m-%d")
        if self._dca_step5_date != today:
            self._dca_step5_date = today
            self._dca_step5_today_count = 0
        return self._dca_step5_today_count < config.DCA_STEP5_DAILY_MAX

    def _record_step5_dca(self):
        today = time.strftime("%Y-%m-%d")
        if self._dca_step5_date != today:
            self._dca_step5_date = today
            self._dca_step5_today_count = 0
        self._dca_step5_today_count += 1
        logger.info(f"[5단계DCA] 오늘 {self._dca_step5_today_count}/{config.DCA_STEP5_DAILY_MAX}회 사용")

    # ────────────────────────────────────────────────
    #  역방향 연속 캔들 카운트
    # ────────────────────────────────────────────────
    def _count_adverse_candles(self, symbol: str, trend: str) -> int:
        """
        15m 완성 캔들 중 트렌드 반대 방향 연속 캔들 수 반환.
        (마지막 캔들은 미완성이므로 제외)
        """
        try:
            klines = self.api.get_klines(symbol, "15m", limit=9)
            completed = klines[:-1] if len(klines) > 1 else klines
            count = 0
            for k in reversed(completed):
                if isinstance(k, list):
                    o, c = float(k[1]), float(k[4])
                else:
                    o, c = float(k["open"]), float(k["close"])
                is_adverse = (trend == "UP" and c < o) or (trend == "DOWN" and c > o)
                if is_adverse:
                    count += 1
                else:
                    break
            return count
        except Exception as e:
            logger.debug(f"역방향 캔들 조회 실패: {e}")
            return 0

    # ────────────────────────────────────────────────
    #  급락 SHORT 모드
    # ────────────────────────────────────────────────
    def _enter_crash_short(self):
        """
        빠른 스캔으로 가장 급락하는 코인 선택 → 전체 자본의 50%로 SHORT 진입.
        DCA 없음, 급락 전용 트레일링, 15분 강제 탈출.
        """
        blocked = set(self._blocked_symbols.keys())
        candidates = self.scanner.scan_crash_shorts(blocked=blocked)
        if not candidates:
            logger.warning("[급락SHORT] 적합한 코인 없음 → 급락모드 종료")
            self._exit_crash_mode()
            return

        coin  = candidates[0]
        price = self.api.get_price(coin["symbol"])
        invest = (self.pt.total_capital + self.pt.total_pnl) * CRASH_SHORT_SEED_RATIO

        logger.warning(
            f"[급락SHORT] {coin['symbol']} | 24h {coin['change_24h']:+.2%} | "
            f"투입 ${invest:.0f} (전체자본의 {CRASH_SHORT_SEED_RATIO:.0%}) | "
            f"재진입 {self._crash_retry_count}/{CRASH_MAX_RETRIES}회차"
        )
        self.pt.open_position(coin["symbol"], "DOWN", price,
                              invest_override=invest, crash_short=True)
        self._current_coin_enter_time = time.time()
        self._last_upgrade_scan_time  = time.time()
        self._current_coin_score      = coin.get("score", 0.0)
        self.state = BotState.IN_POSITION

    def _exit_crash_mode(self):
        """급락 SHORT 모드 종료 → 일반 모드 복귀"""
        self._in_crash_mode    = False
        self._crash_retry_count = 0
        self._crash_mode_start  = 0.0
        logger.info("[급락모드종료] 일반 트레이딩 모드로 복귀")

    # ────────────────────────────────────────────────
    #  진입
    # ────────────────────────────────────────────────
    def _enter(self, coin: dict):
        symbol = coin["symbol"]
        trend  = coin["trend"]
        price  = self.api.get_price(symbol)
        self.pt.open_position(symbol, trend, price)
        self._current_coin_enter_time = time.time()
        self._last_upgrade_scan_time  = time.time()
        self._current_coin_score      = coin.get("score", 0.0)
        self.state = BotState.IN_POSITION

    # ────────────────────────────────────────────────
    #  청산
    # ────────────────────────────────────────────────
    def _close(self, reason: str, exit_price: float = None):
        p = self.pt.position
        if exit_price is None:
            exit_price = self.api.get_price(p.symbol)
        self.pt.close_position(exit_price, reason)
        self._last_exit_time = time.time()
        self.state = BotState.IDLE
        # 청산 직후 실제 잔고 동기화 (입금 반영 + 설정값 자동 재계산)
        self.pt.sync_capital_from_api()

    # ────────────────────────────────────────────────
    #  메인 틱
    # ────────────────────────────────────────────────
    def tick(self):
        now = time.time()

        # ── 매 틱: BTC 가격 히스토리 업데이트 (시장충격 감지용) ──
        btc_shock_this_tick = self._update_btc_and_check_shock()

        # ── IDLE: 청산 후 즉시 or 정기 스캔 후 진입 ──
        if self.state == BotState.IDLE:
            # ── 급락 SHORT 모드: 빠른 스캔 후 SHORT 진입 ──
            if self._in_crash_mode:
                if self._crash_retry_count > CRASH_MAX_RETRIES:
                    logger.info(f"[급락모드] 최대 재진입 {CRASH_MAX_RETRIES}회 소진 → 모드 종료")
                    self._exit_crash_mode()
                elif now >= self._market_shock_until:
                    logger.info("[급락모드] BTC 회복 감지 → 모드 종료")
                    self._exit_crash_mode()
                else:
                    self._enter_crash_short()
                return

            # 시장 충격 중 (급락모드 아님): 일반 진입 차단
            if now < self._market_shock_until:
                remaining = (self._market_shock_until - now) / 60
                logger.debug(f"[시장충격차단] BTC급락 진입차단 ({remaining:.1f}분 남음)")
                return

            # 청산 직후: RESCAN_AFTER_EXIT_MIN 이후 즉시 재스캔
            since_exit = (now - self._last_exit_time) / 60
            since_scan = (now - self._last_scan_time) / 60
            ready = (self._last_exit_time > 0 and since_exit >= RESCAN_AFTER_EXIT_MIN) \
                 or (since_scan >= SCAN_INTERVAL_MIN)

            if ready:
                self.state = BotState.SCANNING
                logger.info("코인 스캔 시작...")
                self._last_scan_time = now
                self._last_exit_time = 0.0

                # 만료된 차단 해제
                self._blocked_symbols = {
                    s: t for s, t in self._blocked_symbols.items() if t > now
                }
                blocked = set(self._blocked_symbols.keys())
                if blocked:
                    logger.info(f"차단 코인 제외: {blocked}")

                # 차단 코인 제외하고 선택
                candidates = self.scanner.scan()
                coin = next((c for c in candidates if c["symbol"] not in blocked), None)

                if coin:
                    self._enter(coin)
                else:
                    logger.info("적합한 코인 없음 (차단 제외), 30초 후 재시도...")
                    self._last_scan_time = now - (SCAN_INTERVAL_MIN * 60 - 30)
                    self.state = BotState.IDLE
            return

        # ── IN_POSITION: 포지션 관리 ────────────────
        if self.state == BotState.IN_POSITION:
            p = self.pt.position
            if not p:
                self.state = BotState.IDLE
                return

            # 현재가 + 거래량 가져오기
            try:
                ticker = self.api.get_ticker(p.symbol)
                price  = float(ticker.get("lastPrice", 0)) or self.api.get_price(p.symbol)
                volume = float(ticker.get("volume", 0))
            except Exception as e:
                logger.warning(f"시세 조회 실패: {e}")
                return

            # 급락 감지용 히스토리 업데이트 + 트레일링 업데이트
            self.pt.update_price_hist(price, volume)
            self.pt.update_trail(price)

            # 로그 (net_pnl = realized_pnl: 진입+청산 비용 모두 반영한 실질 손익)
            net_pnl     = p.realized_pnl(price)
            pnl_pct     = p.pnl_pct(price)
            asset_value = (self.pt.total_capital + self.pt.total_pnl) + net_pnl
            trail_str   = f" [트레일SL:{p.trail_sl:.4f}]" if p.trail_active else ""
            logger.info(
                f"[모니터] {p.symbol}({p.trend}) | 현재가: {price:.6f} | "
                f"자산평가액: ${asset_value:.2f} | "
                f"순손익: {pnl_pct:.2%}(${net_pnl:+.2f}) | "
                f"투입: ${p.total_invested:.0f} | 단계: {p.avg_down_step}{trail_str}"
            )

            # 1. 급락 손절
            if self.pt.is_flash_crash():
                self._close("급락손절")
                self._last_scan_time = 0
                return

            # 1.5. BTC 시장 충격 → 일반 포지션만 즉시 청산 (급락SHORT는 오히려 유리)
            if btc_shock_this_tick and not p.crash_short:
                self._close("BTC시장충격")
                self._last_scan_time = 0
                return

            # 2. 최대 손실 한도 손절 (단계 무관)
            if self.pt.is_max_loss_stop(price):
                was_crash  = p.crash_short
                symbol     = p.symbol
                stage      = p.avg_down_step
                self._close("최대손실손절")
                self._last_scan_time = 0
                # 고단계(3+) 손절 → 해당 코인 48시간 차단 (반복 손실 방지)
                if not was_crash and stage >= 3:
                    block_until = now + 48 * 3600
                    self._blocked_symbols[symbol] = block_until
                    self._save_engine_state()
                    logger.warning(
                        f"[손절코인차단] {symbol} | {stage}단계 최대손실손절 → 48시간 차단"
                    )
                # 급락 SHORT 손절: BTC 아직 급락 중이고 재진입 여력 있으면 즉시 재스캔
                if was_crash:
                    if (now < self._market_shock_until and
                            self._crash_retry_count < CRASH_MAX_RETRIES):
                        self._crash_retry_count += 1
                        logger.warning(
                            f"[급락SHORT재진입] 손절 후 즉시 재스캔 "
                            f"({self._crash_retry_count}/{CRASH_MAX_RETRIES}회차)"
                        )
                    else:
                        self._exit_crash_mode()
                return

            # 2.5. 하드캡 손절
            if self.pt.is_hard_cap_stop(price):
                self._close("하드캡손절")
                self._last_scan_time = 0
                return

            # 2.7. 급락 SHORT: 15분 강제 탈출 + 손절 후 재진입 처리
            if p.crash_short:
                crash_age_min = (now - p.open_time) / 60
                if crash_age_min >= CRASH_MAX_DURATION_MIN:
                    logger.warning(
                        f"[급락SHORT시간초과] {p.symbol} | {crash_age_min:.1f}분 경과 → 강제 청산"
                    )
                    self._close("급락SHORT시간초과")
                    # BTC 아직 급락 중이고 재진입 여력 있으면 재시도
                    if (now < self._market_shock_until and
                            self._crash_retry_count < CRASH_MAX_RETRIES):
                        self._crash_retry_count += 1
                    else:
                        self._exit_crash_mode()
                    return

            # 3. 익절 (일반 or 트레일)
            if self.pt.should_take_profit(price):
                reason       = self.pt.take_profit_reason(price)
                was_sideways = p.sideways_dca
                was_crash    = p.crash_short
                symbol       = p.symbol
                # 트레일 발동 시 trail_sl 가격으로 청산 (갭 하락 방지)
                # 그 외: TP 체크한 가격 그대로 사용 (재조회 시 가격 하락으로 순손익 역전 방지)
                if p.trail_active and p.is_trail_hit(price):
                    self._close(reason, exit_price=p.trail_sl)
                else:
                    self._close(reason, exit_price=price)
                # 횡보 DCA 모드로 청산 → 수익 났을 때만 코인 차단 (손실이면 즉시 재진입 허용)
                if was_sideways:
                    # 청산 후 total_pnl이 이미 반영됐으므로 마지막 거래 pnl 확인
                    last_pnl = (self.pt.closed_trades[-1]["pnl"]
                                if self.pt.closed_trades else 0.0)
                    if last_pnl >= config.SIDEWAYS_DCA_MIN_NET:
                        self._blocked_symbols[symbol] = time.time() + SIDEWAYS_BLOCK_MIN * 60
                        self._save_engine_state()
                        logger.info(f"[횡보코인차단] {symbol} → {SIDEWAYS_BLOCK_MIN}분 차단 (순수익 ${last_pnl:+.2f})")
                    else:
                        logger.info(f"[횡보코인차단생략] {symbol} 순수익 ${last_pnl:+.2f} < ${config.SIDEWAYS_DCA_MIN_NET} → 즉시 재진입 허용")
                # 급락 SHORT 익절: BTC 아직 급락 중이면 연속 재진입, 아니면 모드 종료
                if was_crash:
                    if (now < self._market_shock_until and
                            self._crash_retry_count < CRASH_MAX_RETRIES):
                        self._crash_retry_count += 1
                        logger.info(
                            f"[급락SHORT익절후재진입] BTC 아직 급락 중 → "
                            f"연속 재스캔 ({self._crash_retry_count}/{CRASH_MAX_RETRIES}회차)"
                        )
                    else:
                        self._exit_crash_mode()
                return

            # 급락 SHORT 포지션: DCA·업그레이드·횡보교체 등 일반 로직 전부 건너뜀
            if p.crash_short:
                return

            # 4. 물타기 (가격 기반)
            #    4·5단계: BTC 하락 차단 / 일일 한도 / 역방향 캔들 2개 차단
            adverse_candles = None   # lazy 계산 (step 5에서도 재사용)
            if self.pt.should_avg_down(price):
                adverse_candles = self._count_adverse_candles(p.symbol, p.trend)
                going_to_step4  = (p.avg_down_step == 3)
                going_to_step5  = (p.avg_down_step == 4)
                is_final_stages = going_to_step4 or going_to_step5
                candle_limit    = (config.ADVERSE_CANDLE_BLOCK_STEP4
                                   if is_final_stages else ADVERSE_CANDLE_BLOCK)

                if adverse_candles >= candle_limit:
                    step_label = "4단계" if going_to_step4 else ("5단계" if going_to_step5 else "")
                    logger.info(
                        f"[DCA차단] {p.symbol} | 역방향 15m 캔들 {adverse_candles}개 연속 "
                        f"→ {step_label}가격DCA 보류 (추세전환 가능성)"
                    )
                elif is_final_stages and self._is_btc_declining():
                    step_label = "4단계" if going_to_step4 else "5단계"
                    logger.info(f"[{step_label}DCA차단] {p.symbol} | BTC 하락 중 → 투입 보류")
                elif going_to_step4 and not self._check_step4_daily_limit():
                    logger.info(
                        f"[4단계DCA차단] {p.symbol} | 오늘 {self._dca_step4_today_count}/"
                        f"{config.DCA_STEP4_DAILY_MAX}회 한도 초과 → 보류"
                    )
                elif going_to_step5 and not self._check_step5_daily_limit():
                    logger.info(
                        f"[5단계DCA차단] {p.symbol} | 오늘 {self._dca_step5_today_count}/"
                        f"{config.DCA_STEP5_DAILY_MAX}회 한도 초과 → 보류"
                    )
                else:
                    if going_to_step4:
                        self._record_step4_dca()
                    elif going_to_step5:
                        self._record_step5_dca()
                    self.pt.execute_avg_down(price)
                    return

            # 5. 횡보 DCA (단계별 대기시간) + 마지막 단계 횡보 청산
            #    1 ~ MAX_DCA_STAGES-1 단계: N분 경과 → 다음 단계 DCA
            #    MAX_DCA_STAGES 단계(마지막): N분 경과 → 청산 (마지막 결전)
            below_avg = (p.trend == "UP"   and price < p.avg_price) or \
                        (p.trend == "DOWN" and price > p.avg_price)
            is_last_stage = (p.avg_down_step == config.MAX_DCA_STAGES)

            if (below_avg and
                    p.avg_down_step >= SIDEWAYS_DCA_MIN_STEP and
                    p.avg_down_step <= config.MAX_DCA_STAGES and
                    p.step_enter_time > 0):
                step_age_min = (now - p.step_enter_time) / 60

                # ── 단계 타임아웃: 2단계 이상, 손실 중 N분 초과 → 청산 ──
                # 1단계는 무조건 2단계로 진행하므로 타임아웃 미적용
                if not is_last_stage and p.avg_down_step >= 2 and net_pnl < 0:
                    timeout_min = config.SIDEWAYS_DCA_STAGE_TIMEOUT_MIN.get(
                        p.avg_down_step, 60
                    )
                    if step_age_min >= timeout_min:
                        symbol = p.symbol
                        stage  = p.avg_down_step
                        logger.warning(
                            f"[단계타임아웃] {symbol} | {stage}단계 "
                            f"{step_age_min:.0f}분 손실 중(${net_pnl:+.2f}) → 청산"
                        )
                        self._close("단계타임아웃청산")
                        self._last_scan_time = 0
                        # 2단계 이상 타임아웃 손절 → 24시간 차단 (반복 손실 방지)
                        block_until = now + 24 * 3600
                        self._blocked_symbols[symbol] = block_until
                        self._save_engine_state()
                        logger.warning(
                            f"[손절코인차단] {symbol} | {stage}단계 단계타임아웃청산 → 24시간 차단"
                        )
                        return

                wait_min = (SIDEWAYS_LAST_STAGE_TIMEOUT_MIN if is_last_stage
                            else SIDEWAYS_DCA_WAIT_PER_STEP.get(p.avg_down_step, 10))

                if step_age_min >= wait_min:
                    # ── 마지막 단계: 청산 ──────────────────────────
                    if is_last_stage:
                        symbol = p.symbol
                        stage  = p.avg_down_step
                        logger.warning(
                            f"[마지막결전] {symbol} | {stage}단계 "
                            f"{step_age_min:.0f}분 횡보 → 손절 청산 | 순손익 ${net_pnl:+.2f}"
                        )
                        self._close("마지막결전청산")
                        self._last_scan_time = 0
                        # 최종단계 손절 코인 48시간 차단 (TAO처럼 반복 손실 방지)
                        block_until = now + 48 * 3600
                        self._blocked_symbols[symbol] = block_until
                        self._save_engine_state()
                        logger.warning(
                            f"[손절코인차단] {symbol} | {stage}단계 마지막결전청산 → 48시간 차단"
                        )
                        return

                    # ── 중간 단계: 다음 단계 DCA ─────────────────
                    if adverse_candles is None:
                        adverse_candles = self._count_adverse_candles(p.symbol, p.trend)
                    going_to_step4 = (p.avg_down_step == 3)
                    going_to_step5 = (p.avg_down_step == 4)
                    is_final_stages = going_to_step4 or going_to_step5
                    # 1→2단계는 무조건 진행 (추세 체크 비활성화)
                    candle_limit   = (config.ADVERSE_CANDLE_BLOCK_STEP4
                                      if is_final_stages
                                      else ADVERSE_CANDLE_BLOCK  # =9, 사실상 비활성
                                      if p.avg_down_step == 1
                                      else config.SIDEWAYS_DCA_ADVERSE_CANDLES)
                    if adverse_candles >= candle_limit:
                        logger.info(
                            f"[횡보DCA추세차단] {p.symbol} | 역방향 15m 캔들 {adverse_candles}개 연속 "
                            f"→ 추세 진행 중, 횡보DCA 보류"
                        )
                        return
                    if is_final_stages and self._is_btc_declining():
                        step_label = "4단계" if going_to_step4 else "5단계"
                        logger.info(f"[횡보{step_label}DCA차단] {p.symbol} | BTC 하락 중 → 보류")
                        return
                    if going_to_step4 and not self._check_step4_daily_limit():
                        logger.info(
                            f"[횡보4단계DCA차단] {p.symbol} | 오늘 {self._dca_step4_today_count}/"
                            f"{config.DCA_STEP4_DAILY_MAX}회 한도 초과 → 보류"
                        )
                        return
                    if going_to_step5 and not self._check_step5_daily_limit():
                        logger.info(
                            f"[횡보5단계DCA차단] {p.symbol} | 오늘 {self._dca_step5_today_count}/"
                            f"{config.DCA_STEP5_DAILY_MAX}회 한도 초과 → 보류"
                        )
                        return
                    if p.avg_down_step >= 2:
                        max_loss_thr = -p.total_invested * config.SIDEWAYS_DCA_MAX_LOSS_RATIO
                        if net_pnl <= max_loss_thr:
                            logger.info(
                                f"[횡보DCA건너뜀] {p.symbol} | 순손익 ${net_pnl:+.2f} ≤ "
                                f"${max_loss_thr:.0f} (투입금 ${p.total_invested:.0f} × "
                                f"{config.SIDEWAYS_DCA_MAX_LOSS_RATIO:.1%}) → 손실 중 강제투입 보류"
                            )
                            return
                    next_amt = p.next_avg_down_amount()
                    logger.info(
                        f"[횡보DCA] {p.symbol} | {p.avg_down_step}단계 "
                        f"{step_age_min:.1f}분 경과 | 현재가({price:.6f}) vs 평단({p.avg_price:.6f}) "
                        f"→ {p.avg_down_step+1}단계 투입 +${next_amt:.0f} "
                        f"(총 ${p.total_invested+next_amt:.0f})"
                    )
                    if going_to_step4:
                        self._record_step4_dca()
                    elif going_to_step5:
                        self._record_step5_dca()
                    self.pt.execute_sideways_avg_down(price)
                    return

            # 6. 더 좋은 코인 업그레이드 체크 (0단계, 손실 $0.30 이내)
            if (p.avg_down_step == 0 and
                    net_pnl >= UPGRADE_MAX_LOSS_USD and
                    (now - self._last_upgrade_scan_time) / 60 >= UPGRADE_SCAN_MIN):
                self._last_upgrade_scan_time = now
                try:
                    blocked = set(self._blocked_symbols.keys())
                    candidates = self.scanner.scan()
                    best = next((c for c in candidates
                                 if c["symbol"] not in blocked
                                 and c["symbol"] != p.symbol), None)
                    if best and best["score"] >= self._current_coin_score * UPGRADE_SCORE_MULT:
                        logger.info(
                            f"[업그레이드] {p.symbol}(점수:{self._current_coin_score:.3f}) "
                            f"→ {best['symbol']}(점수:{best['score']:.3f}) "
                            f"| 배율 {best['score']/max(self._current_coin_score,0.001):.1f}x "
                            f"| 순손익 ${net_pnl:+.2f}"
                        )
                        self._close("업그레이드교체")
                        self._enter(best)
                        return
                    else:
                        best_info = f"{best['symbol']} {best['score']:.3f}" if best else "없음"
                        logger.info(
                            f"[업그레이드 체크] 현재:{p.symbol}({self._current_coin_score:.3f}) "
                            f"| 최고후보:{best_info} → 유지"
                        )
                except Exception as e:
                    logger.debug(f"업그레이드 스캔 실패: {e}")

            # 7. 횡보 타임아웃 (초기 진입만 적용 — 물타기 이후엔 유지)
            #    더 좋은 코인이 있을 때만 교체 (없으면 유지 → 불필요한 수수료 방지)
            if p.avg_down_step == 0:
                age_min   = (now - self._current_coin_enter_time) / 60
                gross_abs = abs(p.gross_pnl(price))
                if age_min >= FLAT_TIMEOUT_MIN and gross_abs < FLAT_THRESHOLD_USD and net_pnl >= 0:
                    # 더 좋은 코인 스캔 (현재 코인 점수보다 높아야 교체 의미 있음)
                    try:
                        blocked    = set(self._blocked_symbols.keys())
                        candidates = self.scanner.scan()
                        best = next((c for c in candidates
                                     if c["symbol"] not in blocked
                                     and c["symbol"] != p.symbol), None)
                        if best and best["score"] > self._current_coin_score:
                            logger.info(
                                f"[횡보교체] {p.symbol}(점수:{self._current_coin_score:.3f}) | "
                                f"{age_min:.1f}분 횡보 → {best['symbol']}(점수:{best['score']:.3f}) "
                                f"교체 | {FLAT_BLOCK_MIN}분 차단"
                            )
                            self._blocked_symbols[p.symbol] = now + FLAT_BLOCK_MIN * 60
                            self._save_engine_state()
                            self._close("횡보교체")
                            self._enter(best)
                        else:
                            best_info = f"{best['symbol']}({best['score']:.3f})" if best else "없음"
                            logger.info(
                                f"[횡보유지] {p.symbol} {age_min:.1f}분 횡보지만 "
                                f"더 좋은 코인 없음(최고후보:{best_info}) → 계속 보유"
                            )
                            # 타이머 리셋 (다음 체크까지 FLAT_TIMEOUT_MIN 만큼 대기)
                            self._current_coin_enter_time = now - (FLAT_TIMEOUT_MIN - 5) * 60
                    except Exception as e:
                        logger.debug(f"횡보교체 스캔 실패: {e}")
                    return

            # 8. 최대 보유 시간 초과 → 수익 구간이면 교체
            coin_age_min = (now - self._current_coin_enter_time) / 60
            if coin_age_min >= MAX_COIN_DURATION_MIN and net_pnl >= 0:
                logger.info(
                    f"[시간교체] {p.symbol} {coin_age_min:.0f}분 보유 → "
                    f"순손익 ${net_pnl:+.2f} → 재스캔"
                )
                self._close("시간교체")
                return

            # 9. 트렌드 반전 체크 (5분 1회 쓰로틀)
            #    ① 수익 구간: 반전 + 거래량 고갈 → 코인 교체
            #    ② DCA 2단계 이상 손실 중: 강한 반전 감지 → 추가손실 방지 탈출
            REVERSAL_CHECK_INTERVAL = 300  # 초 (5분)
            should_check_reversal = (
                (p.total_invested < config.MAX_TOTAL_POSITION and pnl_pct >= 0) or
                (p.avg_down_step >= 2 and pnl_pct < 0)
            )
            if (should_check_reversal and
                    now - self._last_reversal_check_time >= REVERSAL_CHECK_INTERVAL):
                self._last_reversal_check_time = now
                try:
                    daily  = self.api.get_klines(p.symbol, "1d", limit=25)
                    closes = [float(k["close"] if isinstance(k, dict) else k[4]) for k in daily]
                    hourly = self.api.get_klines(p.symbol, "1h", limit=30)
                    hvols  = [float(k["volume"] if isinstance(k, dict) else k[5]) for k in hourly]

                    if pnl_pct >= 0:
                        # ① 수익 구간 교체: 반전 + 거래량 고갈
                        if (self.pt.check_trend_reversal(closes) and
                                self.pt.check_volume_dryup(hvols)):
                            logger.info(f"[코인교체] {p.symbol} 트렌드반전 → 재스캔")
                            self._close("코인교체")
                    else:
                        # ② DCA 2단계+ 손실 중 강한 추세 전환 → 탈출
                        if self.pt.check_trend_reversal(closes, strict=True):
                            logger.warning(
                                f"[DCA반전탈출] {p.symbol} | {p.avg_down_step}단계 손실 중 "
                                f"강한 추세전환 감지 (pnl {pnl_pct:.2%}) → 추가손실 방지 청산"
                            )
                            self._close("DCA반전탈출")
                            self._last_scan_time = 0
                except Exception as e:
                    logger.debug(f"코인교체 체크 실패: {e}")

    # ────────────────────────────────────────────────
    #  상태 출력
    # ────────────────────────────────────────────────
    def print_status(self):
        p     = self.pt.position
        price = self.api.get_price(p.symbol) if p else 0.0
        s     = self.pt.get_status(price)

        # 현 자산평가액 = 실현자본 + 미실현 순손익
        net_unrealized   = s["position"]["net_pnl"] if s["position"] else 0.0
        asset_value      = s["current_capital"] + net_unrealized
        asset_change_pct = (asset_value - self.pt.total_capital) / self.pt.total_capital * 100

        logger.info("=" * 60)
        logger.info(f"  봇 상태      : {self.state.value}")
        logger.info(f"  ★ 현 자산평가액: ${asset_value:.2f}  "
                    f"({asset_change_pct:+.2f}%  초기 ${self.pt.total_capital:.0f} 대비)")
        logger.info(f"  확정 자본    : ${s['current_capital']:.2f}  "
                    f"(실현 순손익 ${s['total_pnl']:+.2f})")
        logger.info(f"  거래 횟수    : {s['total_trades']}회 "
                    f"({s['win_count']}W / {s['loss_count']}L) "
                    f"승률 {s['win_rate']}%")
        if s["position"]:
            pos = s["position"]
            trail_info = (f"  트레일SL     : {pos['trail_sl']:.6f}  "
                          f"(고점 {pos['peak_price']:.6f})")
            logger.info(f"  ─────────────────────────────────────────────")
            logger.info(f"  보유 코인    : {pos['symbol']} ({pos['trend']})")
            logger.info(f"  평균단가     : {pos['avg_price']}")
            logger.info(f"  현재가       : {pos['current_price']}")
            logger.info(f"  총손익(gross): ${pos['gross_pnl']:+.2f}")
            logger.info(f"  비용(수수료+슬): -${pos['cost']:.4f}")
            logger.info(f"  순손익(net)  : ${pos['net_pnl']:+.2f}")
            logger.info(f"  총투입       : ${pos['total_invested']:.0f} / "
                        f"${pos['max_position']:.0f} | 물타기 {pos['avg_down_step']}단계")
            if pos["trail_active"]:
                logger.info(trail_info)
        logger.info("=" * 60)
