"""
BingX 열린 포지션 전량 청산 + state.json 초기화
실행: python close_all.py
"""
import json
import os
import sys
import time

if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from bingx_api import BingXAPI
import config


def close_all_positions(api: BingXAPI):
    print("[1] 열린 포지션 조회 중...")
    try:
        positions = api.get_positions()
    except Exception as e:
        print(f"  오류: {e}")
        return False

    open_pos = [p for p in positions if abs(float(p.get("positionAmt", 0))) > 0]

    if not open_pos:
        print("  열린 포지션 없음 (거래소 깨끗함)")
        return True

    print(f"  {len(open_pos)}개 포지션 발견 -> 청산 시작")
    all_ok = True
    for p in open_pos:
        symbol   = p.get("symbol", "?")
        pos_side = p.get("positionSide", "LONG")
        amt      = float(p.get("positionAmt", 0))
        pnl      = float(p.get("unrealizedProfit", 0))
        print(f"  청산: {symbol} {pos_side}  qty={amt}  미실현손익=${pnl:+.4f}")
        for attempt in range(3):
            try:
                # 실제 수량 직접 전달 (closePosition=true 는 109400 오류 발생)
                result = api.close_position(symbol, pos_side, qty=abs(amt))
                code   = result.get("code", "?")
                if str(code) == "0":
                    print(f"  OK {symbol} {pos_side} 청산 완료")
                    break
                else:
                    print(f"  실패 code={code} {result.get('msg','')}  (시도 {attempt+1}/3)")
                    # 이미 없는 포지션이면 성공으로 처리
                    check = api.get_positions(symbol)
                    still = [x for x in check
                             if x.get("positionSide") == pos_side
                             and abs(float(x.get("positionAmt", 0))) > 0]
                    if not still:
                        print(f"  확인: {symbol} 이미 청산돼 있음 -> 정상")
                        break
            except Exception as e:
                print(f"  오류: {e}  (시도 {attempt+1}/3)")
            time.sleep(1)
        else:
            print(f"  !! {symbol} {pos_side} 청산 실패 — 거래소에서 수동 확인 필요")
            all_ok = False
    return all_ok


def reset_state():
    print("[2] state.json 초기화...")
    empty = {
        "total_pnl": 0.0, "win_count": 0, "loss_count": 0,
        "closed_trades": [], "position": None,
        "total_withdrawn": 0.0, "withdrawal_count": 0, "withdrawal_history": []
    }
    for path in [
        os.path.join(BASE_DIR, "state.json"),
        os.path.join(BASE_DIR, "state.json.bak"),
    ]:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(empty, f, ensure_ascii=False, indent=2)
    print(f"  state.json 초기화 완료  (자본 ${config.TOTAL_CAPITAL:.2f})")

    engine_path = os.path.join(BASE_DIR, "engine_state.json")
    with open(engine_path, "w", encoding="utf-8") as f:
        json.dump(
            {"blocked_symbols": {}, "consec_wins": {}, "consec_win_blocked": {}},
            f, ensure_ascii=False, indent=2
        )
    print("  engine_state.json 초기화 완료")


def main():
    print()
    print("=" * 50)
    print("  BingX 포지션 전량 청산 + 상태 초기화")
    print(f"  설정 자본: ${config.TOTAL_CAPITAL:.2f}")
    print("=" * 50)
    print()

    api = BingXAPI()
    ok  = close_all_positions(api)
    print()
    reset_state()
    print()

    if ok:
        print("완료!  run_hidden.vbs 를 더블클릭하면 자동 매매 시작됩니다.")
    else:
        print("일부 청산 실패 — 거래소에서 수동 확인 후 run_hidden.vbs 실행하세요.")
        print()
        sys.exit(1)
    print()


if __name__ == "__main__":
    main()
