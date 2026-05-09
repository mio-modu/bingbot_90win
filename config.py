# ============================================================
#  BingX Trading Bot - 실물거래 설정 ($242 리셋 기준)
# ============================================================

import os
from dotenv import load_dotenv
load_dotenv()

API_KEY    = os.getenv("BINGX_API_KEY", "")
SECRET_KEY = os.getenv("BINGX_SECRET_KEY", "")
BASE_URL   = "https://open-api.bingx.com"

# ────────────────────────────────────────────────
#  자금 설정
#  시드는 항상 "전체 잔액 × SEED_RATIO" 로 결정됨.
#  거래 청산마다 BingX 실제 잔고를 조회해 자동 재계산.
# ────────────────────────────────────────────────
TOTAL_CAPITAL        = 242.0   # 초기 자본 (USD) — 재시작 기준값
INITIAL_POSITION_USD = 14.5    # 초기 시드 ($242 × 6%)
LEVERAGE             = 10      # 레버리지 10배
SEED_RATIO           = 0.06    # 시드 비율: 전체 잔액의 6%

# ────────────────────────────────────────────────
#  수수료 & 슬리피지
# ────────────────────────────────────────────────
TAKER_FEE_RATE  = 0.0005   # BingX Taker 0.05%
SLIPPAGE_RATE   = 0.0005   # 슬리피지 시뮬레이션 0.05%

# ────────────────────────────────────────────────
#  익절 설정
# ────────────────────────────────────────────────
TAKE_PROFIT_PCT = 0.01     # 포지션 기준 최소 +1%
MIN_PROFIT_USD  = 1.10     # 최소 순수익 (seed × 7.5% ≈ $14.5 × 0.075)

# ────────────────────────────────────────────────
#  트레일링 익절
# ────────────────────────────────────────────────
TRAIL_ACTIVATE_PCT  = 0.03
TRAIL_DISTANCE_PCT  = 0.012

# ────────────────────────────────────────────────
#  DCA 트레일링
# ────────────────────────────────────────────────
DCA_TRAIL_STEP_THRESHOLD  = 1
DCA_TRAIL_ACTIVATE_PCT    = 0.02
DCA_TRAIL_DISTANCE_PCT    = 0.008
DCA_TRAIL_MAX_PROFIT_PCT  = 0.99
DCA_TRAIL_SWITCH_PCT      = 0.02
DCA_TRAIL_BREATHING_RATIO = 0.30
DCA_TRAIL_DIST_MIN        = 0.003
DCA_TRAIL_DIST_MAX        = 0.020

# ────────────────────────────────────────────────
#  물타기 설정 ($242 기준: 최대 3단계, 총 $116)
#
#  단계  | 추가투입  | 누적투입  | 노셔널(×10)
#  진입  |  $14.5   |  $14.5   |   $145
#  1단계 |  $14.5   |  $29     |   $290
#  2단계 |  $29     |  $58     |   $580
#  3단계 |  $58     |  $116    |  $1160  ← 하드캡
# ────────────────────────────────────────────────
AVG_DOWN_STEP1_TRIGGER = -0.03
AVG_DOWN_STEP2_TRIGGER = -0.04
AVG_DOWN_STEP3_TRIGGER = -0.08
AVG_DOWN_STEP4_TRIGGER = -0.12
AVG_DOWN_STEP5_TRIGGER = -0.12
MAX_DCA_STAGES         = 3
MAX_TOTAL_POSITION     = 121.0  # 하드캡 (자본의 50%)

# ────────────────────────────────────────────────
#  손절 설정
#  거래 청산마다 "전체 잔액 × 비율"로 자동 재계산됨.
#
#  MAX_NET_LOSS_USD          = 잔액 × 23%
#  DCA_STEP4_NET_LOSS_TRIGGER= 잔액 × 9%  (SEED_RATIO × 1.5)
#  DCA_STEP5_NET_LOSS_TRIGGER= 잔액 × 21% (SEED_RATIO × 3.5)
# ────────────────────────────────────────────────
MAX_NET_LOSS_USD        = -55.7  # 최대 손실 ($242 × 23%)

DCA_STEP4_NET_LOSS_TRIGGER = -21.8  # $242 × 9%
DCA_STEP5_NET_LOSS_TRIGGER = -50.8  # $242 × 21%

ADVERSE_CANDLE_BLOCK_STEP4 = 2
DCA_STEP4_DAILY_MAX        = 2
DCA_STEP5_DAILY_MAX        = 2
BTC_DCA4_DROP_PCT          = -0.01
BTC_DCA4_WINDOW_MIN        = 15

HARD_CAP_STOP_COIN_PCT  = -0.02

FLASH_CRASH_COIN_PCT    = -0.05
FLASH_CRASH_VOL_MULT    = 2.0
FLASH_CRASH_WINDOW_MIN  = 30

BTC_SHOCK_PCT        = -0.02
BTC_SHOCK_WINDOW_MIN =  15
BTC_SHOCK_BLOCK_MIN  =  30

# ────────────────────────────────────────────────
#  급락 SHORT 시스템
# ────────────────────────────────────────────────
CRASH_SHORT_SEED_RATIO   = 0.3
CRASH_TRAIL_ACTIVATE_PCT = 0.015
CRASH_TRAIL_DISTANCE_PCT = 0.008
CRASH_MAX_DURATION_MIN   = 15
CRASH_MAX_RETRIES        = 2

ADVERSE_CANDLE_BLOCK = 9

# ────────────────────────────────────────────────
#  코인 교체 설정
# ────────────────────────────────────────────────
MA_PERIOD              = 20
LOW_VOLUME_THRESHOLD   = 0.5
LOW_VOLUME_DURATION_MIN= 90

# ────────────────────────────────────────────────
#  코인 선정 기준
# ────────────────────────────────────────────────
ADX_MIN_THRESHOLD      = 25
SCAN_INTERVAL_MIN      = 60
RESCAN_AFTER_EXIT_MIN  = 0
MAX_COIN_DURATION_MIN  = 45
TOP_N_COINS            = 5

UPGRADE_SCAN_MIN       = 20
UPGRADE_SCORE_MULT     = 2.5
UPGRADE_MAX_LOSS_USD   = -1.10

FLAT_TIMEOUT_MIN   = 12
FLAT_THRESHOLD_USD = 0.10
FLAT_BLOCK_MIN     = 120

# ────────────────────────────────────────────────
#  횡보 DCA 설정
# ────────────────────────────────────────────────
SIDEWAYS_DCA_WAIT_PER_STEP = {1: 20, 2: 15, 3: 12, 4: 10}
SIDEWAYS_LAST_STAGE_TIMEOUT_MIN = 20
SIDEWAYS_DCA_MIN_STEP  = 1
SIDEWAYS_BLOCK_MIN     = 120
SIDEWAYS_DCA_TP_PCT    = 0.025
SIDEWAYS_DCA_MIN_NET   = 1.10
SIDEWAYS_DCA_MAX_LOSS_RATIO    = 0.125
SIDEWAYS_DCA_ADVERSE_CANDLES   = 2
SIDEWAYS_DCA_STAGE_TIMEOUT_MIN = {1: 60, 2: 45, 3: 30, 4: 20}

MIN_VOLUME_USDT        = 10_000_000
MAX_VOLUME_USDT        = 300_000_000

MAIN_LOOP_INTERVAL_SEC = 10
LOG_LEVEL              = "INFO"

# ────────────────────────────────────────────────
#  구출 DCA 설정
# ────────────────────────────────────────────────
RESCUE_DCA_MAX_LOSS_RATIO = 0.20   # 투입금 20% 이상 손실이면 구출 포기 → 손절
RESCUE_DCA_TIMEOUT_MIN    = 15     # 구출 DCA 후 15분 내 회복 못하면 손절
