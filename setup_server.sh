#!/bin/bash
# ============================================================
#  BingX Bot 서버 설치 (Ubuntu 22.04 / 24.04)
# ------------------------------------------------------------
#  노트북이 아니라 클라우드 서버에서 24시간 돌리기 위한 설치본.
#  설치 후에는 폰의 SSH 앱만으로 상태 확인·중지·재시작이 가능하다.
#
#  사용법 (브랜치를 반드시 지정할 것 — 기본 브랜치는 옛 페이퍼 봇이다):
#    git clone -b claude/github-push-time-check-ioamx1 \
#        https://github.com/mio-modu/bingbot_90win /opt/bingx-bot
#    cd /opt/bingx-bot && bash setup_server.sh
#
#  설치만 하고 봇은 나중에 켜려면:
#    bash setup_server.sh --no-start
#    (노트북 봇이 아직 돌고 있다면 반드시 이걸 쓸 것 — 같은 계좌에 두 봇이
#     주문을 내면 서로의 포지션을 오인한다)
# ============================================================
set -euo pipefail

NO_START=0
for arg in "$@"; do
    case "$arg" in
        --no-start) NO_START=1 ;;
        *) echo "알 수 없는 옵션: $arg"; exit 1 ;;
    esac
done

BOT_DIR="${BOT_DIR:-/opt/bingx-bot}"
# 실제 로그인 사용자를 쓴다. 예전 버전은 "ubuntu" 로 하드코딩돼 있어서
# 사용자명이 다른 서버(root, debian, ec2-user…)에서는 chown 과 서비스가
# 통째로 어긋났다. sudo 로 실행해도 원래 사용자를 찾아낸다.
SERVICE_USER="${SUDO_USER:-$(id -un)}"

echo "======================================"
echo "  BingX Bot 서버 설치"
echo "  설치 위치 : $BOT_DIR"
echo "  실행 사용자: $SERVICE_USER"
echo "======================================"

# ── 0. 올바른 코드인지 확인 ─────────────────────────────────
# 이 저장소의 기본 브랜치(master)는 옛 페이퍼 봇이다.
# 브랜치를 지정하지 않고 clone 하면 실거래 코드가 아닌 것이 받아진다.
echo "[0/6] 코드 확인..."
if [ ! -f config.py ] || [ ! -f main.py ]; then
    echo "  ✗ config.py / main.py 가 없습니다. 봇 폴더에서 실행하세요."
    exit 1
fi
CUR_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '(git 아님)')"
echo "  브랜치: $CUR_BRANCH"
if ! grep -qE '^LIVE_TRADING\s*=\s*True' config.py; then
    echo ""
    echo "  ✗ config.py 에 LIVE_TRADING = True 가 없습니다."
    echo "    실거래 코드가 아닌 브랜치를 받은 것 같습니다."
    echo "    (이 저장소의 기본 브랜치 master 는 옛 페이퍼 봇입니다)"
    echo ""
    echo "    올바른 받기:"
    echo "      git clone -b claude/github-push-time-check-ioamx1 \\"
    echo "          https://github.com/mio-modu/bingbot_90win $BOT_DIR"
    exit 1
fi
if [ ! -d tests ]; then
    echo "  ✗ tests/ 폴더가 없습니다 — 안전장치가 없는 옛 코드입니다."
    exit 1
fi
echo "  ✅ 실거래 코드 확인 (LIVE_TRADING=True, 테스트 포함)"

# ── 1. 시스템 패키지 ────────────────────────────────────────
echo "[1/6] 패키지 설치..."
sudo apt-get update -y -qq
# git 포함 — 나중에 'git pull' 로 봇을 갱신하려면 필요하다
sudo apt-get install -y -qq python3 python3-pip python3-venv git

# ── 2. 봇 디렉터리 ──────────────────────────────────────────
echo "[2/6] 디렉터리 준비..."
sudo mkdir -p "$BOT_DIR"
sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$BOT_DIR"

SRC_DIR="$(realpath "$(pwd)")"
if [ "$SRC_DIR" != "$(realpath "$BOT_DIR")" ]; then
    echo "  $SRC_DIR → $BOT_DIR 복사"
    # .git 과 캐시는 제외. state.json 은 계정 데이터라 덮어쓰지 않는다.
    sudo rsync -a --exclude '.git' --exclude '__pycache__' \
               --exclude 'venv' --ignore-existing \
               "$SRC_DIR"/ "$BOT_DIR"/ 2>/dev/null || sudo cp -rn "$SRC_DIR"/. "$BOT_DIR"/
    sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$BOT_DIR"
fi

cd "$BOT_DIR"

# ── 3. 가상환경 ─────────────────────────────────────────────
echo "[3/6] Python 가상환경..."
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -r requirements.txt -q

# ── 4. API 키 ───────────────────────────────────────────────
echo "[4/6] API 키 확인..."
if [ ! -f "$BOT_DIR/.env" ]; then
    echo ""
    echo "  .env 파일이 없습니다. BingX API 키를 입력하세요."
    read -rp "  BINGX_API_KEY: " api_key
    read -rsp "  BINGX_SECRET_KEY: " secret_key   # -s: 화면에 안 보이게
    echo ""
    umask 077                                     # 키 파일은 본인만 읽게
    printf 'BINGX_API_KEY=%s\nBINGX_SECRET_KEY=%s\n' "$api_key" "$secret_key" \
        > "$BOT_DIR/.env"
    chmod 600 "$BOT_DIR/.env"
    echo "  .env 생성 완료 (권한 600)"
else
    chmod 600 "$BOT_DIR/.env"
    echo "  기존 .env 사용"
fi

# ── 5. 자체 점검 ────────────────────────────────────────────
echo "[5/6] 안전장치 테스트 (실주문 없음)..."
if ! ./venv/bin/python tests/run_all.py > /tmp/bingx-tests.log 2>&1; then
    echo ""
    echo "  ⚠ 테스트 실패 — 설치를 중단합니다."
    echo "    자세한 내용: /tmp/bingx-tests.log"
    tail -20 /tmp/bingx-tests.log
    exit 1
fi
echo "  ✅ 전부 통과"

# ── 6. systemd 서비스 ───────────────────────────────────────
echo "[6/6] 서비스 등록..."
sed -e "s|^User=.*|User=$SERVICE_USER|" \
    -e "s|/opt/bingx-bot|$BOT_DIR|g" \
    bingx-bot.service > /tmp/bingx-bot.service
sudo cp /tmp/bingx-bot.service /etc/systemd/system/bingx-bot.service
sudo systemctl daemon-reload
sudo systemctl enable bingx-bot

echo ""
echo "======================================"
if [ "$NO_START" = "1" ]; then
    sudo systemctl stop bingx-bot 2>/dev/null || true
    echo "  ✅ 설치 완료 — 봇은 **시작하지 않았습니다** (--no-start)"
    echo ""
    echo "  다른 봇(노트북 등)이 같은 계좌로 돌고 있지 않은지 확인한 뒤"
    echo "  아래로 시작하세요:"
    echo "      sudo systemctl start bingx-bot"
else
    sudo systemctl restart bingx-bot
    sleep 3
    sudo systemctl is-active --quiet bingx-bot \
        && echo "  ✅ 설치 완료 — 봇이 돌고 있습니다" \
        || echo "  ⚠ 서비스가 뜨지 않았습니다. 아래 status 로 확인하세요"
fi
echo "======================================"
cat <<EOF

  폰에서 쓸 명령어 (SSH 앱: Termius / JuiceSSH)

    상태     : sudo systemctl status bingx-bot
    중지     : sudo systemctl stop bingx-bot
    시작     : sudo systemctl start bingx-bot
    실시간   : tail -f $BOT_DIR/bot.log
    최근청산 : grep 청산 $BOT_DIR/bot.log | tail -20
    분석     : cd $BOT_DIR && ./venv/bin/python analyze.py
    긴급청산 : cd $BOT_DIR && ./venv/bin/python close_all.py

  봇 갱신 (새 코드 받기)
    cd $BOT_DIR && git pull && sudo systemctl restart bingx-bot

  ※ 노트북의 봇은 반드시 꺼두세요.
    같은 계좌에 두 봇이 주문을 내면 서로의 포지션을 오인합니다.

EOF
