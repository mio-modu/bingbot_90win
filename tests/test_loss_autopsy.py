"""
손절 분석 엔진 테스트
──────────────────────
    python tests/test_loss_autopsy.py

원인 분류가 실제로 구분해내는지 확인한다.
분류가 틀리면 "무엇을 고칠지"를 정반대로 가리키게 된다 —
트레일링을 고쳐야 할 상황에서 진입 필터를 조이는 식으로.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import loss_autopsy as la                                # noqa: E402


def rec(pnl, *, mfe=0.0, mae=0.0, step=0, cost=1.6, reason="손절",
        symbol="X-USDT", dur=600, ctx=None):
    return {"symbol": symbol, "pnl": pnl, "mfe_usd": mfe, "mae_usd": -abs(mae),
            "avg_down_step": step, "cost": cost, "reason": reason,
            "duration_s": dur, "total_invested": 100.0,
            "entry_ctx": ctx or {}}


def test_giveback():
    print("\n[1] ★ 벌었다가 되돌려준 것 → '되돌려줌'")
    #  비용 $1.6 인데 한때 $6 까지 벌었다 → 익절 문제
    c = la.classify(rec(-2.0, mfe=6.0, mae=3.0))
    print(f"    최고이익 $6.00 / 비용 $1.60 → {c}")
    assert c == "되돌려줌", c
    print("    ✅")


def test_immediate_adverse():
    print("\n[2] ★ 진입 직후부터 반대 → '즉시역행'")
    #  비용의 절반도 못 벌어봤다 → 진입 근거 문제
    c = la.classify(rec(-9.0, mfe=0.3, mae=9.5, step=1))
    print(f"    최고이익 $0.30 / 최대역행 $9.50 → {c}")
    assert c == "즉시역행", c
    print("    ✅")


def test_deep_dca():
    print("\n[3] 물타기 2단계 이상 → '물타기심화'")
    c = la.classify(rec(-22.0, mfe=1.0, mae=24.0, step=3))
    print(f"    3단계 / 손실 -$22 → {c}")
    assert c == "물타기심화", c
    print("    ✅")


def test_chop():
    print("\n[4] 양쪽으로 거의 안 움직임 → '횡보소모'")
    c = la.classify(rec(-1.7, mfe=0.4, mae=0.5, cost=1.6, reason="횡보교체"))
    print(f"    최고이익 $0.40 + 최대역행 $0.50 ≤ 비용 $1.60 → {c}")
    assert c == "횡보소모", c
    print("    ✅")


def test_shock_wins_over_everything():
    print("\n[5] 급락손절은 개별 판단 문제가 아니다")
    c = la.classify(rec(-30.0, mfe=8.0, mae=31.0, step=3, reason="급락손절"))
    print(f"    사유 '급락손절' (되돌려줌·물타기 조건도 만족) → {c}")
    assert c == "시장충격", c
    print("    ✅ 사유가 우선")


def test_clean_stop():
    print("\n[6] 0~1단계에서 규칙대로 잘린 것 → '정상손절'")
    #  비용의 절반은 넘겼지만 되돌려줌 기준에는 못 미치고, 움직임은 있었다
    c = la.classify(rec(-5.0, mfe=1.4, mae=5.5, step=1, cost=1.6))
    print(f"    최고이익 $1.40 (비용 $1.60 의 0.9배) → {c}")
    assert c == "정상손절", c
    print("    ✅")


def test_missing_cost_is_estimated():
    print("\n[7] 옛 기록에 비용이 없어도 분류된다")
    r = rec(-3.0, mfe=6.0, mae=4.0, cost=0)
    r["total_invested"] = 240.0        # 비용 ≈ 240 × 1.6% = $3.84
    c = la.classify(r)
    print(f"    비용 미기록 → 투입 $240 에서 추정 → {c}")
    assert c == "되돌려줌", c
    print("    ✅")


def test_report_shape():
    print("\n[8] 집계 — 원인별로 나뉘고 손실 큰 순으로 정렬")
    trades = [
        rec(+3.0, mfe=4.0, symbol="A-USDT"),
        rec(+2.0, mfe=3.0, symbol="A-USDT"),
        rec(-2.0, mfe=6.0, mae=3.0, symbol="B-USDT"),   # 되돌려줌
        rec(-3.0, mfe=7.0, mae=4.0, symbol="B-USDT"),   # 되돌려줌
        rec(-25.0, mfe=1.0, mae=26.0, step=3, symbol="C-USDT"),  # 물타기심화
    ]
    r = la.autopsy(trades)
    causes = [c["cause"] for c in r["causes"]]
    print(f"    거래 {r['n_trades']}건 / 승률 {r['win_rate']:.0%} / "
          f"순손익 ${r['net']:+.2f}")
    print(f"    원인 순서(손실 큰 순): {causes}")
    assert r["n_trades"] == 5 and r["n_wins"] == 2 and r["n_losses"] == 3
    assert causes[0] == "물타기심화", "손실이 가장 큰 원인이 앞에 와야 한다"
    assert "되돌려줌" in causes
    gb = [c for c in r["causes"] if c["cause"] == "되돌려줌"][0]
    assert gb["n"] == 2 and abs(gb["total"] + 5.0) < 0.01
    print("    ✅")


def test_entry_split_needs_enough_samples():
    print("\n[9] 표본이 적으면 진입 지표 비교를 하지 않는다")
    trades = [rec(+1.0, ctx={"adx": 40}), rec(-1.0, ctx={"adx": 20})]
    r = la.autopsy(trades)
    print(f"    승1 패1 → 비교 항목 {len(r['entry_split'])}개")
    assert r["entry_split"] == [], "표본 2건으로 결론을 내면 안 된다"

    many = ([rec(+1.0, mfe=2.0, ctx={"adx": 40}) for _ in range(5)]
            + [rec(-1.0, mfe=0.1, mae=2.0, ctx={"adx": 20}) for _ in range(5)])
    r2 = la.autopsy(many)
    adx = [x for x in r2["entry_split"] if x["key"] == "adx"]
    assert adx, "표본이 충분한데 비교가 없다"
    print(f"    승5 패5 → adx 승 {adx[0]['win']:.0f} vs 패 {adx[0]['loss']:.0f} "
          f"(차이 {adx[0]['gap']:+.0%})")
    assert adx[0]["gap"] > 0.4
    print("    ✅")


def test_empty_and_garbage_are_safe():
    print("\n[10] 빈 입력·깨진 기록에서 터지지 않는다")
    r = la.autopsy([])
    assert r["n_trades"] == 0 and r["causes"] == []
    la.print_report(r, "테스트")
    bad = [{"symbol": "X", "pnl": "이상한값"}, {}, {"pnl": None}]
    r2 = la.autopsy(bad)
    print(f"    깨진 기록 3건 → 손실 {r2['n_losses']}건으로 처리")
    assert r2["n_trades"] == 3
    print("    ✅")


def test_suggestions_wait_for_sample_size():
    print("\n[11] 표본이 적으면 제안하지 않는다")
    small = la.autopsy([rec(-1.0, mfe=5.0) for _ in range(5)])
    s = la.suggestions(small)
    print(f"    5건 → {s[0]}")
    assert "이릅니다" in s[0], s

    big = la.autopsy([rec(-1.0, mfe=5.0, mae=2.0) for _ in range(25)]
                     + [rec(+1.0, mfe=2.0) for _ in range(5)])
    s2 = la.suggestions(big)
    print(f"    30건 (전부 되돌려줌) → {s2[0]}")
    assert any("되돌려준" in x for x in s2), s2
    print("    ✅")


def main():
    print("=" * 62)
    print("  손절 분석 엔진 테스트")
    print("=" * 62)
    for fn in [test_giveback, test_immediate_adverse, test_deep_dca,
               test_chop, test_shock_wins_over_everything, test_clean_stop,
               test_missing_cost_is_estimated, test_report_shape,
               test_entry_split_needs_enough_samples,
               test_empty_and_garbage_are_safe,
               test_suggestions_wait_for_sample_size]:
        fn()
    print("\n" + "=" * 62)
    print("  전부 통과")
    print("=" * 62)


if __name__ == "__main__":
    main()
