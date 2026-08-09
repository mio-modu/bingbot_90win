#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
#  BingX Bot — 안드로이드(Termux) 설치
# ------------------------------------------------------------
#  폰에서 봇을 직접 돌린다. VPS 없이 쓰는 경우용.
#
#  ⚠ 먼저 읽을 것
#    안드로이드는 백그라운드 프로세스를 적극적으로 죽인다.
#    아래 세 가지를 하지 않으면 봇이 조용히 멈춘다:
#      1) 배터리 최적화에서 Termux 제외 (안드로이드 설정)
#      2) termux-wake-lock  (이 스크립트가 자동 실행)
#      3) Termux:Boot 앱 설치 (재부팅 후 자동 시작)
#
#    그래도 VPS 만큼 안정적이지 않다. 봇이 멈춰도 거래소에 걸어둔
#    STOP_MARKET 백스톱은 살아 있으므로 손실은 막히지만, 포지션이
#    방치되고 물타기·익절 관리가 끊긴다.
#
#  사용법:
#    pkg install git
#    git clone -b claude/github-push-time-check-ioamx1 \
#        https://github.com/mio-modu/bingbot_90win ~/bingx-bot
#    cd ~/bingx-bot && bash setup_termux.sh
# ============================================================
set -euo pipefail

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "======================================"
echo "  BingX Bot — Termux 설치"
echo "  위치: $BOT_DIR"
echo "======================================"

# ── 0. 올바른 코드인지 확인 ─────────────────────────────────
# 기본 브랜치(master)는 옛 페이퍼 봇이다. 브랜치를 지정하지 않고 clone 하면
# 실거래 코드가 아닌 것이 받아진다.
#
# 값(True/False)이 아니라 "실거래 코드에만 있는 파일"로 판별한다.
# config.py 의 LIVE_TRADING 은 이제 .env 로 덮어쓸 수 있어서
# `LIVE_TRADING = True` 라는 글자를 찾는 방식은 정상 코드도 걸러버렸다.
echo "[0/5] 코드 확인..."
_missing=""
for _f in config.py main.py watchdog.py risk_governor.py trade_journal.py tests/run_all.py; do
    [ -f "$_f" ] || _missing="$_missing $_f"
done
if [ -n "$_missing" ] || ! grep -qE '^LIVE_TRADING[[:space:]]*=' config.py 2>/dev/null; then
    echo "  ✗ 실거래 코드가 아닙니다 (기본 브랜치 master 는 옛 페이퍼 봇)."
    [ -n "$_missing" ] && echo "    없는 파일:$_missing"
    echo ""
    echo "    올바른 받기:"
    echo "      rm -rf ~/bingx-bot"
    echo "      git clone -b claude/github-push-time-check-ioamx1 \\"
    echo "          https://github.com/mio-modu/bingbot_90win ~/bingx-bot"
    exit 1
fi
echo "  ✅ 실거래 코드 확인 (안전장치·저널·리스크 관리 포함)"
# .env 로 실거래를 꺼둔 상태면 알려준다 (설치는 계속)
if [ -f "$BOT_DIR/.env" ] && grep -qiE '^LIVE_TRADING[[:space:]]*=[[:space:]]*(false|0|no)' "$BOT_DIR/.env"; then
    echo "  ⚠ .env 에서 LIVE_TRADING 이 꺼져 있습니다 — 모의 거래로 돕니다"
fi

# ── 1. 패키지 ───────────────────────────────────────────────
echo "[1/5] 패키지 설치..."
pkg update -y >/dev/null 2>&1 || true
# numpy 는 pip 로 빌드하면 안드로이드에서 실패하기 쉽다.
# Termux 가 미리 빌드해 둔 python-numpy 를 쓴다.
pkg install -y python python-pip python-numpy git >/dev/null
# ⚠ Termux 에서 `pip install --upgrade pip` 는 금지돼 있다.
#   ("Installing pip is forbidden, this will break the python-pip package")
#   pip 는 pkg 가 관리하므로 건드리지 않는다.
pip install --quiet requests python-dotenv
echo "  ✅ python / numpy / requests / python-dotenv"

# ── 2. API 키 ───────────────────────────────────────────────
echo "[2/5] API 키..."
if [ ! -f "$BOT_DIR/.env" ]; then
    echo ""
    read -rp "  BINGX_API_KEY: " api_key
    read -rsp "  BINGX_SECRET_KEY: " secret_key   # 화면에 안 보이게
    echo ""
    umask 077
    printf 'BINGX_API_KEY=%s\nBINGX_SECRET_KEY=%s\n' "$api_key" "$secret_key" > "$BOT_DIR/.env"
    chmod 600 "$BOT_DIR/.env"
    echo "  ✅ .env 생성 (권한 600)"
else
    chmod 600 "$BOT_DIR/.env"
    echo "  ✅ 기존 .env 사용"
fi

# ── 3. 자체 점검 ────────────────────────────────────────────
echo "[3/5] 안전장치 테스트 (실주문 없음)..."
if ! python tests/run_all.py > "$BOT_DIR/tests.log" 2>&1; then
    echo "  ✗ 테스트 실패 — 설치를 중단합니다. tests.log 확인"
    tail -20 "$BOT_DIR/tests.log"
    exit 1
fi
echo "  ✅ 전부 통과"

# ── 4. 실행 스크립트 ────────────────────────────────────────
echo "[4/5] 실행 스크립트 생성..."
cat > "$BOT_DIR/run.sh" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
# 봇 실행 — 화면을 꺼도 안드로이드가 프로세스를 재우지 않게 wake-lock 을 건다
cd "$BOT_DIR"
termux-wake-lock 2>/dev/null || true
exec python -u watchdog.py --auto
EOF
chmod +x "$BOT_DIR/run.sh"

# 재부팅 후 자동 시작 (Termux:Boot 앱이 설치돼 있어야 동작)
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/bingx-bot" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock 2>/dev/null || true
cd "$BOT_DIR" && python -u watchdog.py --auto >> "$BOT_DIR/boot.log" 2>&1
EOF
chmod +x "$HOME/.termux/boot/bingx-bot"
echo "  ✅ run.sh / 부팅 스크립트"

# ── 5. 안내 ─────────────────────────────────────────────────
echo "[5/5] 완료"
cat <<EOF

======================================
  설치 완료 — 아직 봇은 켜지지 않았습니다
======================================

  ⚠ 켜기 전에 반드시 (안 하면 봇이 조용히 멈춥니다)

    1) 안드로이드 설정 → 앱 → Termux → 배터리
       → "제한 없음" / "최적화 안 함" 으로 변경
    2) Termux:Boot 앱 설치 (F-Droid) — 재부팅 후 자동 시작용
       설치 후 한 번 실행해야 활성화됩니다
    3) 노트북 봇이 같은 계좌로 돌고 있다면 먼저 끄세요

  ★ 켜기 전에 연결 점검 (조회만, 주문 없음)
      cd $BOT_DIR && python check_connection.py

  봇 시작
      cd $BOT_DIR && ./run.sh

  백그라운드로 시작 (Termux 세션을 닫아도 유지)
      cd $BOT_DIR && nohup ./run.sh > run.log 2>&1 &

  상태 보기
      tail -f $BOT_DIR/bot.log
      grep 청산 $BOT_DIR/bot.log | tail -20

  중지
      pkill -f watchdog.py

  긴급 청산
      cd $BOT_DIR && python close_all.py

  분석
      cd $BOT_DIR && python analyze.py

  갱신
      cd $BOT_DIR && git pull && pkill -f watchdog.py && ./run.sh

EOF
