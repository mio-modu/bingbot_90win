#!/bin/bash
# BingX Bot 서버 자동 설치 스크립트 (Ubuntu 22.04 기준)
set -e

BOT_DIR="/opt/bingx-bot"
SERVICE_USER="ubuntu"

echo "======================================"
echo "  BingX Bot 서버 설치 시작"
echo "======================================"

# 1. 시스템 패키지
echo "[1/5] 패키지 업데이트..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv

# 2. 봇 디렉토리 준비
echo "[2/5] 봇 디렉토리 준비..."
sudo mkdir -p "$BOT_DIR"
sudo chown "$SERVICE_USER:$SERVICE_USER" "$BOT_DIR"

# 스크립트를 봇 디렉토리 안에서 실행했을 때만 복사 생략
if [ "$(realpath $(pwd))" != "$(realpath $BOT_DIR)" ]; then
    echo "  현재 디렉토리 파일을 $BOT_DIR 로 복사..."
    cp -r . "$BOT_DIR/"
fi

cd "$BOT_DIR"

# 3. 가상환경 + 패키지 설치
echo "[3/5] Python 가상환경 설치..."
python3 -m venv venv
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -r requirements.txt -q

# 4. .env 파일 존재 여부 확인
echo "[4/5] 환경 변수 확인..."
if [ ! -f "$BOT_DIR/.env" ]; then
    echo ""
    echo "⚠️  .env 파일이 없습니다! API 키를 입력하세요:"
    read -p "  BINGX_API_KEY: " api_key
    read -p "  BINGX_SECRET_KEY: " secret_key
    echo "BINGX_API_KEY=$api_key" > "$BOT_DIR/.env"
    echo "BINGX_SECRET_KEY=$secret_key" >> "$BOT_DIR/.env"
    echo "  .env 파일 생성 완료"
fi

# 5. systemd 서비스 등록
echo "[5/5] 서비스 등록..."
# 서비스 파일의 User를 현재 사용자로 교체
sed "s/User=ubuntu/User=$SERVICE_USER/" bingx-bot.service > /tmp/bingx-bot.service
sudo cp /tmp/bingx-bot.service /etc/systemd/system/bingx-bot.service
sudo systemctl daemon-reload
sudo systemctl enable bingx-bot
sudo systemctl restart bingx-bot

echo ""
echo "======================================"
echo "  ✅ 설치 완료!"
echo "======================================"
echo ""
echo "  유용한 명령어:"
echo "  상태 확인  : sudo systemctl status bingx-bot"
echo "  실시간 로그: tail -f $BOT_DIR/bot.log"
echo "  감시자 로그: tail -f $BOT_DIR/watchdog.log"
echo "  서비스 중지: sudo systemctl stop bingx-bot"
echo "  서비스 시작: sudo systemctl start bingx-bot"
echo ""
