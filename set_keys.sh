#!/bin/bash
# ============================================================
#  API 키 다시 입력
# ------------------------------------------------------------
#  왜 따로 있나:
#    설치 스크립트는 시크릿을 화면에 감추고 받는다(read -rs).
#    그런데 아무것도 안 보이니 "안 들어갔나?" 싶어 한 번 더 붙여넣게
#    되고, 그러면 시크릿이 두세 번 이어붙어 서명이 깨진다.
#    (실제로 246자짜리 = 82자 × 3 이 만들어진 적이 있다)
#
#    그래서 여기서는 입력 직후 길이와 앞뒤 4자를 보여주고 확인을 받는다.
#    키 전체는 화면에 찍지 않는다.
#
#  사용법:
#    cd ~/bingx-bot && bash set_keys.sh
# ============================================================
set -uo pipefail

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$BOT_DIR/.env"

# 붙여넣기가 반복된 문자열인지 본다. "abcabc" 처럼 같은 조각이 2~5번
# 이어붙은 형태면 중복 붙여넣기로 판단한다.
looks_repeated() {
    # ⚠ `local s="$1" n=${#s}` 로 한 줄에 쓰면 안 된다.
    #    한 줄의 단어들은 local 이 대입하기 "전에" 모두 전개되므로
    #    ${#s} 가 바깥 범위의 (없는) s 를 보고 0 이 된다.
    local s="$1"
    local n=${#s}
    local k unit
    for k in 2 3 4 5; do
        (( n % k )) && continue
        unit="${s:0:n/k}"
        local rebuilt="" i
        for ((i=0; i<k; i++)); do rebuilt+="$unit"; done
        [ "$rebuilt" = "$s" ] && { echo "$k"; return 0; }
    done
    return 1
}

show() {  # 이름 값 → 앞뒤 4자와 길이만
    local name="$1" v="$2" n=${#2}
    if (( n <= 12 )); then
        echo "    $name: (너무 짧음) 총 ${n}자"
    else
        echo "    $name: ${v:0:4}…${v: -4}  (총 ${n}자)"
    fi
}

echo "======================================"
echo "  BingX API 키 입력"
echo "======================================"
echo ""
echo "  · 붙여넣은 뒤 엔터를 한 번만 누르세요."
echo "  · 시크릿은 화면에 보이지 않습니다. 정상입니다."
echo "    입력 후 길이를 보여주므로 그때 확인하면 됩니다."
echo ""

while true; do
    read -rp "  BINGX_API_KEY: " api_key
    read -rsp "  BINGX_SECRET_KEY: " secret_key
    echo ""

    # 공백·줄바꿈·탭 제거 (복사할 때 딸려오는 경우가 많다)
    api_key="$(printf '%s' "$api_key"    | tr -d '[:space:]')"
    secret_key="$(printf '%s' "$secret_key" | tr -d '[:space:]')"

    echo ""
    echo "  입력된 값:"
    show "API_KEY   " "$api_key"
    show "SECRET_KEY" "$secret_key"
    echo ""

    problem=0
    if [ -z "$api_key" ] || [ -z "$secret_key" ]; then
        echo "  ✗ 비어 있습니다."
        problem=1
    fi
    if (( ${#api_key} < 20 )) || (( ${#secret_key} < 20 )); then
        echo "  ✗ 너무 짧습니다 — 붙여넣기가 잘린 것 같습니다."
        problem=1
    fi
    if rep="$(looks_repeated "$secret_key")"; then
        echo "  ✗ SECRET_KEY 가 같은 내용이 ${rep}번 이어붙은 형태입니다."
        echo "    (여러 번 붙여넣으셨습니다 — 한 번만 붙여넣고 엔터)"
        problem=1
    fi
    if rep="$(looks_repeated "$api_key")"; then
        echo "  ✗ API_KEY 가 같은 내용이 ${rep}번 이어붙은 형태입니다."
        problem=1
    fi
    if (( ${#secret_key} > 200 )); then
        echo "  ⚠ SECRET_KEY 가 ${#secret_key}자로 비정상적으로 깁니다."
        problem=1
    fi

    if (( problem )); then
        echo ""
        echo "  다시 입력합니다. (그만두려면 Ctrl+C)"
        echo ""
        continue
    fi

    read -rp "  이 값으로 저장할까요? [y/N] " yn
    case "$yn" in
        [yY]*) break ;;
        *) echo ""; echo "  다시 입력합니다."; echo "" ;;
    esac
done

# 기존 .env 의 다른 설정(CAPITAL_AUTO_SYNC 등)은 살린다
OTHERS=""
if [ -f "$ENV_FILE" ]; then
    OTHERS="$(grep -vE '^[[:space:]]*(BINGX_API_KEY|BINGX_SECRET_KEY)[[:space:]]*=' "$ENV_FILE" || true)"
    cp "$ENV_FILE" "$ENV_FILE.bak"
    chmod 600 "$ENV_FILE.bak"
fi

umask 077
{
    printf 'BINGX_API_KEY=%s\n'    "$api_key"
    printf 'BINGX_SECRET_KEY=%s\n' "$secret_key"
    [ -n "$OTHERS" ] && printf '%s\n' "$OTHERS"
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"

echo ""
echo "  ✅ .env 저장 완료 (권한 600)"
[ -f "$ENV_FILE.bak" ] && echo "     이전 파일은 .env.bak 로 남겨뒀습니다"
echo ""
echo "  이제 연결을 확인하세요:"
echo "      python check_connection.py"
echo ""
