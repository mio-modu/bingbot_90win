#!/bin/bash
# ============================================================
#  봇 갱신 + 재시작 (한 방)
# ------------------------------------------------------------
#    cd ~/bingx-bot && bash restart.sh
#
#  하는 일
#    1. 새 코드 받기 (git pull)
#    2. 안전장치 테스트 — 실패하면 **재시작하지 않는다**
#       (돌고 있던 봇은 그대로 살아 있다. 깨진 코드로 갈아타는 것보다
#        옛 코드로 계속 도는 편이 낫다)
#    3. 기존 봇 종료 → 새로 시작
#    4. 상태 확인
#
#  옵션
#    --no-pull   코드는 그대로 두고 재시작만
#    --no-test   테스트 건너뛰기 (급할 때만)
#    --stop      멈추기만 하고 켜지 않음
#
#  ※ 포지션은 건드리지 않는다. 거래소에 걸린 강제 손절도 살아 있다.
#     전량 정리는 python close_all.py
# ============================================================
set -uo pipefail

cd "$(dirname "$0")" || exit 1
BOT_DIR="$(pwd)"

DO_PULL=1; DO_TEST=1; STOP_ONLY=0
for a in "$@"; do
    case "$a" in
        --no-pull) DO_PULL=0 ;;
        --no-test) DO_TEST=0 ;;
        --stop)    STOP_ONLY=1 ;;
        *) echo "알 수 없는 옵션: $a"; exit 1 ;;
    esac
done

echo "======================================"
echo "  BingX Bot 재시작"
echo "======================================"

# ── 1. 코드 갱신 ────────────────────────────────────────────
if [ "$STOP_ONLY" = "0" ] && [ "$DO_PULL" = "1" ]; then
    echo "[1/4] 새 코드 받기..."
    if ! git pull; then
        echo "  ✗ git pull 실패 — 재시작을 중단합니다."
        echo "    돌고 있던 봇은 그대로 둡니다."
        exit 1
    fi
fi

# ── 2. 자체 점검 ────────────────────────────────────────────
if [ "$STOP_ONLY" = "0" ] && [ "$DO_TEST" = "1" ]; then
    echo "[2/4] 안전장치 테스트 (실주문 없음)..."
    if ! python tests/run_all.py > "$BOT_DIR/tests.log" 2>&1; then
        echo "  ✗ 테스트 실패 — 재시작하지 않습니다."
        echo "    돌고 있던 봇은 옛 코드로 계속 돕니다 (그편이 안전)."
        tail -20 "$BOT_DIR/tests.log"
        exit 1
    fi
    echo "  ✅ 전부 통과"
fi

# ── 3. 종료 ─────────────────────────────────────────────────
echo "[3/4] 기존 봇 종료..."
pkill -f watchdog.py 2>/dev/null
pkill -f main.py     2>/dev/null
for _ in 1 2 3 4 5 6 7 8 9 10; do
    pgrep -f watchdog.py >/dev/null || break
    sleep 1
done
if pgrep -f watchdog.py >/dev/null; then
    echo "  ⚠ 아직 살아 있습니다 — 강제 종료"
    pkill -9 -f watchdog.py 2>/dev/null
    pkill -9 -f main.py     2>/dev/null
    sleep 1
fi
echo "  ✅ 종료됨"

if [ "$STOP_ONLY" = "1" ]; then
    echo ""
    echo "  봇만 멈췄습니다. 포지션과 거래소 손절 주문은 그대로입니다."
    echo "  다시 켜기:  bash restart.sh --no-pull"
    exit 0
fi

# ── 4. 시작 ─────────────────────────────────────────────────
echo "[4/4] 시작..."
nohup ./run.sh > "$BOT_DIR/run.log" 2>&1 &
sleep 8

if pgrep -f watchdog.py >/dev/null; then
    echo "  ✅ 돌고 있습니다 (PID $(pgrep -f watchdog.py | head -1))"
else
    echo "  ✗ 시작 실패 — run.log 확인"
    tail -20 "$BOT_DIR/run.log"
    exit 1
fi

echo ""
echo "── 최근 로그 ──────────────────────────"
tail -15 "$BOT_DIR/bot.log" 2>/dev/null || echo "(아직 로그 없음 — 잠시 후 tail -f bot.log)"
echo ""
echo "  실시간 보기 :  tail -f $BOT_DIR/bot.log"
echo "  손익 분석   :  python loss_autopsy.py"
echo "  멈추기      :  bash restart.sh --stop"
