"""
연결 점검 (읽기 전용)

봇을 켜기 전에 "키가 맞는지 / 어느 계좌에 붙었는지 / 잔고가 얼마인지"를
확인한다. 주문은 절대 내지 않는다. 조회만 한다.

    python check_connection.py

실패하면 무엇이 문제인지 한국어로 알려준다.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

OK   = "  ✅ "
BAD  = "  ✗  "
WARN = "  ⚠  "

# BingX 는 키가 틀려도 HTTP 200 을 주고 본문의 code 로 알려준다.
# 그래서 상태코드만 봐서는 "잔고 0" 과 "권한 없음" 을 구분할 수 없다.
AUTH_CODES = {
    100001: "서명이 맞지 않습니다 (SECRET_KEY 오타 또는 폰 시계 오차)",
    100413: "API_KEY 가 틀렸습니다",
    100419: "허용되지 않은 IP 입니다 (키의 IP 화이트리스트를 비우세요)",
    100202: "선물 계좌 권한이 없습니다",
}


def _mask(s: str) -> str:
    """키를 화면에 찍을 때 앞뒤만 남긴다. 로그 캡처로 새는 걸 막는다."""
    if not s:
        return "(없음)"
    if len(s) <= 12:
        return s[:2] + "…"
    return f"{s[:4]}…{s[-4:]} (총 {len(s)}자)"


def main() -> int:
    print("=" * 46)
    print("  BingX 연결 점검 (조회만, 주문 없음)")
    print("=" * 46)

    # ── 1. 키가 읽혔는가 ────────────────────────────────
    print("\n[1/5] API 키 읽기")
    try:
        import config
    except Exception as e:
        print(f"{BAD}config.py 를 못 읽음: {e}")
        return 1

    if not config.API_KEY or not config.SECRET_KEY:
        print(f"{BAD}.env 에서 키를 못 찾았습니다.")
        print("     .env 파일이 봇 폴더에 있는지, 아래 두 줄이 있는지 확인하세요:")
        print("       BINGX_API_KEY=...")
        print("       BINGX_SECRET_KEY=...")
        return 1
    print(f"{OK}API_KEY    {_mask(config.API_KEY)}")
    print(f"{OK}SECRET_KEY {_mask(config.SECRET_KEY)}")

    # ── 2. 서버 연결 + 시계 ─────────────────────────────
    # 인증이 필요 없는 요청으로 "인터넷이 되는가"부터 확인한다.
    # 이걸 건너뛰면 아래 3단계의 실패가 '키 문제'인지 '네트워크 문제'인지
    # 구분이 안 된다.
    print("\n[2/5] 거래소 연결")
    import requests
    try:
        r = requests.get(f"{config.BASE_URL}/openApi/swap/v2/server/time", timeout=10)
        r.raise_for_status()
        server_ms = int(r.json().get("data", {}).get("serverTime", 0))
    except requests.exceptions.RequestException as e:
        print(f"{BAD}거래소에 닿지 못했습니다: {e}")
        print("     → 폰의 인터넷 연결을 확인하세요 (와이파이/데이터).")
        return 1
    if not server_ms:
        print(f"{BAD}서버 응답이 이상합니다: {r.text[:200]}")
        return 1

    import time as _time
    off = server_ms - int(_time.time() * 1000)
    print(f"{OK}접속 성공 (서버 시계 차이 {off}ms)")
    if abs(off) > 5000:
        print(f"{WARN}폰 시계가 {off/1000:.1f}초 어긋나 있습니다. 봇이 자동 보정하지만,")
        print("     폰 설정에서 '자동 날짜 및 시간'을 켜두는 편이 안전합니다.")

    try:
        from bingx_api import BingXAPI
        api = BingXAPI()
    except Exception as e:
        print(f"{BAD}API 클라이언트 생성 실패: {e}")
        return 1

    # ── 3. 계좌 (여기서 키 권한이 검증된다) ─────────────
    print("\n[3/5] 계좌 조회")
    try:
        raw = api._get("/openApi/swap/v2/user/balance")
    except requests.exceptions.RequestException as e:
        print(f"{BAD}조회 실패(통신): {e}")
        return 1

    code = int(raw.get("code", 0) or 0)
    if code != 0:
        why = AUTH_CODES.get(code, raw.get("msg", "알 수 없는 오류"))
        print(f"{BAD}거래소가 거부했습니다 (code {code}): {why}")
        print("     확인할 것:")
        print("       · .env 의 키/시크릿에 공백이나 줄바꿈이 섞이지 않았는지")
        print("       · 키 권한에 Perpetual Futures 가 켜져 있는지")
        print("       · IP 화이트리스트가 비어 있는지")
        return 1

    try:
        equity = api.get_equity()
        avail  = api.get_balance()
    except requests.exceptions.RequestException as e:
        print(f"{BAD}조회 실패(통신): {e}")
        return 1
    print(f"{OK}순자산  ${equity:,.2f}")
    print(f"{OK}가용    ${avail:,.2f}")
    if equity <= 0:
        print(f"{WARN}잔고가 0 입니다.")
        print("     서브계좌로 만들었다면, 자금을 '선물(Perpetual)' 계좌로")
        print("     이체했는지 확인하세요. 현물 계좌에 있으면 봇이 못 씁니다.")

    # ── 4. 지금 열려 있는 것 ────────────────────────────
    print("\n[4/5] 미청산 포지션 / 주문")
    try:
        positions = [p for p in api.get_positions()
                     if abs(float(p.get("positionAmt", 0) or 0)) > 0]
        orders = api.get_open_orders()
    except (requests.exceptions.RequestException, ValueError, TypeError) as e:
        print(f"{WARN}조회 실패(치명적이지 않음): {e}")
        positions, orders = [], []
    if positions:
        print(f"{WARN}이미 열린 포지션 {len(positions)}개:")
        for p in positions:
            print(f"       {p.get('symbol')} {p.get('positionSide')} "
                  f"{p.get('positionAmt')} / 미실현 {p.get('unrealizedProfit')}")
        print("     봇은 이걸 자기 포지션으로 인식하지 않습니다.")
        print("     시작 전에 정리하는 편이 깔끔합니다.")
    else:
        print(f"{OK}포지션 없음 (깨끗한 상태)")
    print(f"{OK}미체결 주문 {len(orders)}건")

    # ── 5. 봇이 쓸 자본 ─────────────────────────────────
    print("\n[5/5] 봇 설정")
    print(f"{OK}실거래       {'예 (실제 주문 나감)' if config.LIVE_TRADING else '아니오 (모의)'}")
    print(f"{OK}레버리지     {config.LEVERAGE}x")
    auto = getattr(config, "CAPITAL_AUTO_SYNC", False)
    if auto:
        print(f"{OK}운용 자본    ${equity:,.2f}  (거래소 잔고 자동 인식)")
    else:
        print(f"{OK}운용 자본    ${config.TOTAL_CAPITAL:,.2f}  (config 고정값)")
        if equity > 0 and abs(equity - config.TOTAL_CAPITAL) / config.TOTAL_CAPITAL > 0.3:
            print(f"{WARN}실제 잔고(${equity:,.2f})와 30% 이상 차이납니다.")
            print("     아래를 실행하면 거래소 잔고를 자동으로 따라갑니다:")
            print('       echo "CAPITAL_AUTO_SYNC=true" >> .env')

    print("\n" + "=" * 46)
    print("  점검 완료 — 주문은 하나도 내지 않았습니다")
    print("=" * 46)
    return 0


if __name__ == "__main__":
    sys.exit(main())
