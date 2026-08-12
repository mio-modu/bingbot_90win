"""
전체 테스트 실행
────────────────
    python tests/run_all.py

각 테스트는 별도 프로세스로 돌린다. config 를 전역으로 건드리는 테스트가
있어서 같은 프로세스에서 이어 돌리면 서로 오염된다.
실주문은 어느 테스트에서도 나가지 않는다 (전부 가짜 거래소).
"""

import os
import subprocess
import sys

HERE  = os.path.dirname(os.path.abspath(__file__))
SUITE = [
    ("거래 저널 · MAE/MFE",    "test_journal.py"),
    ("거래소 강제 손절 백스톱", "test_exchange_stop.py"),
    ("리스크 거버너",          "test_risk_governor.py"),
    ("수익 보존 락",           "test_profit_lock.py"),
    ("과거 기록 가져오기",     "test_import_history.py"),
    ("포지션 크기 설계",       "test_sizing.py"),
    ("최근 흐름 엔진",         "test_recency.py"),
    ("코인 선정 · 장부 대조",  "test_coin_scanner.py"),
    ("손절 분석 엔진",         "test_loss_autopsy.py"),
    ("외부(사람) 개입 감지",   "test_external_merge.py"),
    ("엔진 기동 스모크",       "test_engine_smoke.py"),
]


def main():
    results = []
    for label, fname in SUITE:
        path = os.path.join(HERE, fname)
        if not os.path.exists(path):
            results.append((label, "없음"))
            continue
        proc = subprocess.run([sys.executable, path],
                              capture_output=True, text=True)
        ok = proc.returncode == 0
        results.append((label, "통과" if ok else "실패"))
        if not ok:
            print(f"\n{'='*62}\n  실패: {label} ({fname})\n{'='*62}")
            print(proc.stdout[-4000:])
            print(proc.stderr[-2000:])

    print("\n" + "=" * 62)
    print("  결과")
    print("=" * 62)
    for label, status in results:
        mark = {"통과": "✅", "실패": "❌", "없음": "⚠️ "}[status]
        print(f"  {mark} {label:<28} {status}")

    failed = [r for r in results if r[1] == "실패"]
    print()
    if failed:
        print(f"  {len(failed)}개 실패")
        sys.exit(1)
    print("  전부 통과")


if __name__ == "__main__":
    main()
